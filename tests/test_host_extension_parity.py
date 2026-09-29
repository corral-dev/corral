"""Parity: Corral adapters with the host extension produce the same session
records as the legacy wired behavior, while bare SessKit scans stay neutral."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from corral.runtime.host_extension import (
    TITLE_PROMPT_MARKER,
    peel_handoff_text,
    split_handoff_text,
)

CODE_XID = "019efe42-6d51-7fb3-ad48-112a8eefaa01"


def _write_codex_session(root: Path, session_id: str, prompt: str, cwd: str) -> Path:
    path = root / f"rollout-2026-07-16T10-00-00-{session_id}.jsonl"
    entries = [
        {
            "timestamp": "2026-07-16T02:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": cwd},
        },
        {
            "timestamp": "2026-07-16T02:00:10.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": prompt},
        },
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def _write_claude_session(projects: Path, proj: str, name: str, cwd: str, text: str) -> Path:
    directory = projects / proj
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    entries = [
        {"type": "user", "cwd": cwd, "message": {"role": "user", "content": text}},
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def _write_pi_session(directory: Path, session_id: str, cwd: str, text: str) -> Path:
    path = directory / f"2026-09-29T00-00-00-000Z_{session_id}.jsonl"
    entries = [
        {"type": "session", "id": session_id, "cwd": cwd, "timestamp": "2026-09-29T00:00:00Z"},
        {
            "id": "m1",
            "type": "message",
            "timestamp": "2026-09-29T00:00:01Z",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        },
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


WRAPPER = (
    "Task: 实现\n\n"
    "You are picking up a session from Cursor. Start a new session of "
    "your own and continue the work. " + ("padding " * 40) + "\n\n"
    "Original session history file: /tmp/history.jsonl\n"
    "Original working directory: /tmp/proj\n"
    "History format hint: Codex rollout JSONL\n\n"
    "Below is a conversation excerpt automatically extracted from the "
    "original session (truncated; for quickly locating the task):\n"
    "User: 修复测试失败\n"
    "Assistant: 开始改相关用例"
)


class HandoffHelperTests(unittest.TestCase):
    def test_split_and_peel_match_legacy_expectations(self) -> None:
        inherited, digest = split_handoff_text(WRAPPER)
        self.assertEqual(inherited, "实现")
        self.assertIn("修复测试失败", digest)
        self.assertNotIn("You are picking up", digest)
        peeled = peel_handoff_text(WRAPPER)
        self.assertIn("修复测试失败", peeled)
        self.assertNotIn("You are picking up", peeled)


class CodexParityTests(unittest.TestCase):
    def test_managed_claim_marks_live_with_pane_pid(self) -> None:
        from corral.runtime.codex import CodexRuntime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            rollout = _write_codex_session(root, CODE_XID, "real prompt", cwd)
            claim_dir = root / "claims"
            claim_dir.mkdir()
            (claim_dir / f"{CODE_XID}.json").write_text(
                json.dumps(
                    {"thread_id": CODE_XID, "rollout_path": str(rollout), "pid": os.getpid()}
                ),
                encoding="utf-8",
            )
            from corral.scan import codex as scan_codex

            with (
                mock.patch.object(scan_codex, "SESSIONS_DIR", str(root)),
                mock.patch.object(scan_codex, "SESSION_INDEX", str(root / "index.jsonl")),
                mock.patch.object(scan_codex, "_live_session_ids", return_value={}),
                mock.patch("corral.codex_identity.CLAIM_DIR", claim_dir),
            ):
                sessions = CodexRuntime().scan_sessions(10)
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0]["live"])
        self.assertEqual(sessions[0]["pid"], os.getpid())

    def test_handoff_prompt_yields_digest_excerpt(self) -> None:
        from corral.runtime.codex import CodexRuntime

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            _write_codex_session(root, CODE_XID, WRAPPER, cwd)
            from corral.scan import codex as scan_codex

            with (
                mock.patch.object(scan_codex, "SESSIONS_DIR", str(root)),
                mock.patch.object(scan_codex, "SESSION_INDEX", str(root / "index.jsonl")),
                mock.patch.object(scan_codex, "_live_session_ids", return_value={}),
            ):
                sessions = CodexRuntime().scan_sessions(10)
        self.assertEqual(len(sessions), 1)
        excerpt = sessions[0]["first_user_msg"]
        self.assertIn("修复测试失败", excerpt)
        self.assertNotIn("You are picking up", excerpt)


class ClaudeParityTests(unittest.TestCase):
    def test_title_noise_session_filtered_by_adapter_only(self) -> None:
        from corral.runtime.claude import ClaudeRuntime
        from corral.scan import claude as scan_claude

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = str(root / "proj")
            os.makedirs(cwd)
            projects = root / "projects"
            _write_claude_session(
                projects, "proj", "noise.jsonl", cwd, f"{TITLE_PROMPT_MARKER} 摘录 1"
            )
            _write_claude_session(projects, "proj", "real.jsonl", cwd, "real prompt")
            with (
                mock.patch.object(scan_claude, "PROJECTS_DIR", str(projects)),
                mock.patch.object(scan_claude, "_live_session_ids", return_value={}),
            ):
                adapted = ClaudeRuntime().scan_sessions(10)
                bare = scan_claude.scan_sessions(limit=10)
        adapted_prompts = [s["first_user_msg"] for s in adapted]
        self.assertEqual(len(adapted), 1)
        self.assertIn("real prompt", adapted_prompts[0])
        # Bare SessKit stays neutral: the noise session lists normally.
        self.assertEqual(len(bare), 2)


class PiParityTests(unittest.TestCase):
    def test_managed_claim_binds_through_adapter(self) -> None:
        from corral import pi_identity
        from corral.runtime.pi import PiRuntime
        from corral.scan import pi as scan_pi

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        claim = {
            "protocolVersion": 1,
            "instanceId": "inst-a",
            "pid": 31,
            "state": "active",
            "sessionId": "claimed999",
            "sessionFile": None,
            "updatedAt": now,
            "sequence": 1,
        }
        with tempfile.TemporaryDirectory() as td:
            cwd = str(Path(td) / "proj")
            Path(cwd).mkdir()
            _write_pi_session(Path(td), "claimed999", cwd, "问题")
            real_cwd = os.path.realpath(cwd)
            with (
                mock.patch.object(scan_pi, "SESSIONS_DIR", td),
                mock.patch.object(scan_pi, "live_processes", return_value=[(31, real_cwd)]),
                mock.patch.object(scan_pi, "process_command_line", return_value="pi --approve"),
                mock.patch.object(scan_pi, "open_file_paths", return_value={31: []}),
                mock.patch.object(scan_pi, "process_start_time", return_value=None),
                mock.patch.object(
                    scan_pi,
                    "process_environ",
                    side_effect=lambda pid, **kwargs: (
                        {pi_identity.INSTANCE_ENV: "inst-a"} if pid == 31 else {}
                    ),
                ),
                mock.patch.object(pi_identity, "read_claims", return_value=[claim]),
            ):
                scan_pi.reset_live_session_overrides()
                try:
                    sessions = PiRuntime().scan_sessions(10)
                finally:
                    scan_pi.reset_live_session_overrides()
        by_id = {s["id"]: s for s in sessions}
        self.assertTrue(by_id["claimed999"]["live"])
        self.assertEqual(by_id["claimed999"]["pid"], 31)


if __name__ == "__main__":
    unittest.main()
