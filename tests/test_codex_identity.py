"""Codex hosted panes must bind to an exact thread, never a nearby rollout."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import codex_identity, codex_proxy, keepalive, liveness
from corral.models import Handoff, LaunchPlan
from corral.runtime.codex import CodexRuntime
from corral.store import SessionStore


class CodexClaimTests(unittest.TestCase):
    def test_exact_thread_claim_invalidates_scan_and_rejects_wrong_file(self) -> None:
        thread_id = "01a0d806-55d8-76a3-bb24-3e3b74232588"
        other_id = "01a0d805-4a23-7843-b326-97750355dee5"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            claims = home / ".cache" / "corral" / "codex-claims"
            sessions = home / ".codex" / "sessions" / "2026" / "09" / "25"
            sessions.mkdir(parents=True)
            rollout = sessions / f"rollout-2026-09-25T10-00-00-{thread_id}.jsonl"
            rollout.write_text(json.dumps({
                "type": "session_meta", "payload": {"id": thread_id},
            }) + "\n", encoding="utf-8")
            claim_path = claims / "pane.json"
            with (
                mock.patch.object(Path, "home", return_value=home),
                mock.patch.object(codex_identity, "CLAIM_DIR", claims),
                mock.patch.dict("os.environ", {
                    codex_identity.CLAIM_PATH_ENV: str(claim_path),
                }),
            ):
                rollout.unlink()
                self.assertTrue(codex_identity.write_claim(thread_id, str(rollout), 123))
                self.assertNotEqual(codex_identity.claim_signature(), ())
                claim_path.unlink()
                rollout.write_text(json.dumps({
                    "type": "session_meta", "payload": {"id": thread_id},
                }) + "\n", encoding="utf-8")
                self.assertFalse(codex_identity.write_claim(other_id, str(rollout), 123))
                self.assertEqual(codex_identity.claim_signature(), ())
                self.assertTrue(codex_identity.write_claim(thread_id, str(rollout), 123))
                self.assertNotEqual(codex_identity.claim_signature(), ())
                self.assertEqual(json.loads(claim_path.read_text())["thread_id"], thread_id)
                self.assertEqual(claim_path.stat().st_mode & 0o777, 0o600)

    def test_only_response_to_root_thread_request_writes_claim(self) -> None:
        pending: dict[str, str] = {}
        thread = {"id": "01a0d806-55d8-76a3-bb24-3e3b74232588", "path": "/tmp/rollout"}
        with mock.patch.object(codex_proxy, "write_claim") as writer:
            codex_proxy._observe_response({"method": "thread/started", "params": {"thread": thread}}, pending)
            codex_proxy._observe_response({"id": 2, "result": {"thread": thread}}, pending)
            writer.assert_not_called()
            pending["2"] = "thread/start"
            codex_proxy._observe_response({"id": 2, "result": {"thread": thread}}, pending)
            writer.assert_called_once()
            self.assertEqual(pending, {})

    def test_codex_launch_uses_corral_owned_bridge(self) -> None:
        with mock.patch.object(keepalive, "_ensure_config_file", return_value="/tmp/tmux.conf"):
            plan = keepalive.wrap_plan(
                LaunchPlan(("codex", "--dangerously-bypass-approvals-and-sandbox"), "/tmp"),
                "codex", "pane1234",
            )
        self.assertIn("corral.codex_proxy", plan.argv)
        self.assertIn("CORRAL_CODEX_CLAIM_PATH=", " ".join(plan.argv))
        self.assertEqual(plan.argv[-2:], ("codex", "--dangerously-bypass-approvals-and-sandbox"))

    def test_remote_resume_keeps_default_auto_approval_on_app_server(self) -> None:
        plan = CodexRuntime().build_resume_plan({"id": "thread-id", "cwd": "/tmp"})
        self.assertEqual(plan.argv[:3], ("codex", "resume", "--no-daemon"))
        tui_args, server_args = codex_proxy._remote_launch_args(list(plan.argv))
        self.assertEqual(tui_args, ["resume", "thread-id"])
        self.assertEqual(server_args[:2], ["app-server", "--stdio"])
        self.assertIn("approval_policy=never", server_args)
        self.assertIn("sandbox_mode=danger-full-access", server_args)

    def test_hosted_handoff_moves_history_root_to_server_without_changing_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            history = Path(directory) / 'history with "quotes" 和中文' / "session.jsonl"
            handoff = Handoff("claude", "Claude Code", "Resume work", str(history),
                              str(project), "Read JSONL")
            plan = CodexRuntime().build_new_plan(handoff)
            self.assertEqual(plan.cwd, str(project))
            self.assertIn("--add-dir", plan.argv)
            tui_args, server_args = codex_proxy._remote_launch_args(list(plan.argv))
            self.assertEqual(tui_args, [handoff.render_prompt()])
            roots_arg = next(arg for arg in server_args if arg.startswith(
                "sandbox_workspace_write.writable_roots="))
            self.assertEqual(json.loads(roots_arg.split("=", 1)[1]), [str(history.parent)])
            self.assertIn("approval_policy=never", server_args)
            self.assertIn("sandbox_mode=danger-full-access", server_args)

    def test_remote_roots_support_repeated_equals_and_relative_paths(self) -> None:
        for command in ([], ["resume", "thread-id"], ["fork", "thread-id"]):
            with self.subTest(command=command):
                tui_args, server_args = codex_proxy._remote_launch_args([
                    "codex", *command, "--add-dir", "relative history",
                    "--add-dir=/tmp/other history", "--no-daemon",
                ])
                self.assertEqual(tui_args, command)
                self.assertEqual(json.loads(server_args[-1].split("=", 1)[1]), [
                    str(Path("relative history").absolute()), "/tmp/other history",
                ])
                self.assertNotIn("approval_policy=never", server_args)

    def test_remote_argument_terminator_preserves_prompt_literals(self) -> None:
        args = ["--", "--add-dir", "--no-daemon", *CodexRuntime.auto_approve_args]
        self.assertEqual(codex_proxy._remote_launch_args(["codex", *args]),
                         (args, ["app-server", "--stdio"]))

    def test_remote_missing_history_root_is_reported(self) -> None:
        for option in ("--add-dir", "--add-dir="):
            with self.subTest(option=option), self.assertRaisesRegex(
                ValueError, "--add-dir requires a directory",
            ):
                codex_proxy._remote_launch_args(["codex", option])

    def test_short_pane_name_cannot_claim_a_codex_history(self) -> None:
        session = {"source": "codex", "id": "01a0d813-c110-7ff0-893d-27f06fffa28d"}
        with mock.patch.object(
            liveness, "_list_tmux_sessions", return_value=[["corral-codex-01a0d813", "7444"]],
        ):
            liveness.annotate([session])
        self.assertNotIn("keepalive_name", session)

        store = object.__new__(SessionStore)
        self.assertIsNone(store._claim_unique_hosted_newcomer(
            "codex:01a0d813", {"source": "codex", "cwd": "/tmp", "id": "01a0d813"},
        ))

    def test_legacy_resume_pane_binds_only_full_matching_thread_id(self) -> None:
        thread_id = "01a0dc04-5b42-75d2-9b3f-27ddb25e1c3b"
        other_id = "01a0dc04-1111-2222-3333-444444444444"
        sessions = [
            {"source": "codex", "id": thread_id, "live": True, "pid": 17033},
            {"source": "codex", "id": other_id, "live": True, "pid": 17033},
        ]
        command = f"node /opt/homebrew/bin/codex resume --dangerously-bypass-approvals-and-sandbox {thread_id}\n"
        with (
            mock.patch.object(liveness, "_list_tmux_sessions", return_value=[["corral-codex-01a0dc04", "35450"]]),
            mock.patch.object(liveness, "_build_ppid_map", return_value={}),
            mock.patch.object(liveness.subprocess, "check_output", return_value=command.encode()),
        ):
            liveness.annotate(sessions)
        self.assertEqual(sessions[0]["keepalive_name"], "corral-codex-01a0dc04")
        self.assertNotIn("keepalive_name", sessions[1])

    def test_external_codex_process_is_not_adopted_by_short_pane_name(self) -> None:
        session = {"source": "codex", "id": "01a0dc04-5b42-75d2-9b3f-27ddb25e1c3b"}
        with (
            mock.patch.object(liveness, "_list_tmux_sessions", return_value=[["corral-codex-01a0dc04", "35450"]]),
            mock.patch.object(liveness.subprocess, "check_output", return_value=b"python unrelated.py\n"),
        ):
            liveness.annotate([session])
        self.assertNotIn("keepalive_name", session)


if __name__ == "__main__":
    unittest.main()
