"""Agent-to-agent send: --from-agent attribution and default reply footer.

Isolated: tmp control home, stubbed target resolution, no supervisor spawn,
no real sessions. Covers run submit / run message only.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from corral import agent_api, control, control_cli, control_store
from corral.control_store import ControlError, RunStore

_AGENT_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID", "PI_SESSION_ID",
              "ANTIGRAVITY_CONVERSATION_ID", "CORRAL_RUNTIME", "CORRAL_SESSION_ID")


def _clean_agent_env(case: unittest.TestCase) -> None:
    """Tests often run inside an agent shell; never let its identity leak in."""
    patcher = mock.patch.dict(os.environ, {})
    patcher.start()
    case.addCleanup(patcher.stop)
    for name in _AGENT_ENV:
        os.environ.pop(name, None)


def _submit_args(**extra):
    base = dict(
        target="claude:s-1",
        prompt="fix the bug",
        prompt_file=None,
        idempotency_key="key-1",
        if_version=None,
        on_busy="queue",
        queue_timeout=3600,
        allow_concurrent=False,
        task_ref=None,
        dry_run=False,
        from_agent=None,
        no_reply_footer=False,
    )
    base.update(extra)
    return SimpleNamespace(**base)


class ParseFromAgentTests(unittest.TestCase):
    def test_blank_means_no_attribution(self):
        self.assertIsNone(control.parse_from_agent(None))
        self.assertIsNone(control.parse_from_agent(""))
        self.assertIsNone(control.parse_from_agent("   "))

    def test_valid_runtime_id_is_normalized(self):
        self.assertEqual(control.parse_from_agent("opencode:ses_abc123"), "opencode:ses_abc123")
        self.assertEqual(control.parse_from_agent("  claude:abc  "), "claude:abc")
        for rid in ("claude", "codex", "opencode", "cursor", "pi", "antigravity"):
            self.assertEqual(control.parse_from_agent(f"{rid}:x"), f"{rid}:x")

    def test_rejects_bad_shape(self):
        for bad in ("no-colon", "claude:", ":abc", "claude:  "):
            with self.assertRaises(ControlError) as caught:
                control.parse_from_agent(bad)
            self.assertEqual(caught.exception.code, "usage_error")

    def test_rejects_unknown_runtime_including_kimi(self):
        for bad in ("kimi:k1", "gemini:abc", "CLaude:abc"):
            with self.assertRaises(ControlError) as caught:
                control.parse_from_agent(bad)
            self.assertEqual(caught.exception.code, "usage_error")

    def test_rejects_control_chars_and_overlong(self):
        with self.assertRaises(ControlError):
            control.parse_from_agent("claude:a\x01b")
        with self.assertRaises(ControlError):
            control.parse_from_agent("claude:" + "x" * 300)


class BuildAgentTextTests(unittest.TestCase):
    def test_no_sender_returns_prompt_verbatim(self):
        self.assertEqual(control.build_agent_text("hello", from_agent=None), "hello")
        self.assertEqual(control.build_agent_text("hello", from_agent=None, no_footer=False), "hello")

    def test_header_and_footer_by_default(self):
        text = control.build_agent_text("do the thing", from_agent="opencode:ses_abc")
        self.assertEqual(
            text,
            "[from opencode:ses_abc · peer, not user]\n\ndo the thing\n\n"
            '[Corral] Reply only if needed: corral send opencode:ses_abc "<your reply>"',
        )
        self.assertEqual(control.parse_agent_header(text), "opencode:ses_abc")

    def test_report_back_has_no_echo_invitation_and_legacy_headers_still_parse(self):
        text = control.build_agent_text("check", from_agent="pi:p1", report_back=True)
        self.assertEqual(text, "[from pi:p1 · peer, not user]\n\ncheck")
        self.assertNotIn("corral send", text)
        self.assertEqual(control.parse_agent_header("[From agent pi:legacy via Corral]\n\nold"), "pi:legacy")

    def test_unattributed_flag_marks_the_envelope_without_a_sender(self):
        self.assertEqual(control.build_agent_text("hi", from_agent=None), "hi")
        self.assertEqual(control.build_agent_text("hi", from_agent=None, unattributed=True),
                         "[from unknown · peer, not user]\n\nhi")

    def test_opt_out_keeps_header_drops_footer(self):
        text = control.build_agent_text("do it", from_agent="claude:abc", no_footer=True)
        self.assertEqual(text, "[from claude:abc · peer, not user]\n\ndo it")


class DetectSenderTests(unittest.TestCase):
    def test_each_runtime_variable_names_its_session(self):
        for var, ref in (
            ("CLAUDE_CODE_SESSION_ID", "claude:c-1"),
            ("CODEX_THREAD_ID", "codex:c-1"),
            ("OPENCODE_SESSION_ID", "opencode:c-1"),
            ("PI_SESSION_ID", "pi:c-1"),
            ("ANTIGRAVITY_CONVERSATION_ID", "antigravity:c-1"),
        ):
            self.assertEqual(control.detect_sender(env={var: "c-1"}), ref, var)

    def test_human_terminal_is_unattributed(self):
        self.assertIsNone(control.detect_sender(env={"TERM": "xterm"}))
        self.assertIsNone(control.detect_sender(env={"CLAUDE_CODE_SESSION_ID": "  "}))

    def test_nested_agents_use_nearest_runtime_process(self):
        env = {"CLAUDE_CODE_SESSION_ID": "outer", "CODEX_THREAD_ID": "inner"}
        with mock.patch.object(control, "_nearest_runtime", return_value="codex") as nearest:
            self.assertEqual(control.detect_sender(env=env), "codex:inner")
        nearest.assert_called_once_with({"claude", "codex"})

    def test_nested_agents_fall_back_to_hosted_runtime(self):
        env = {"CLAUDE_CODE_SESSION_ID": "outer", "PI_SESSION_ID": "inner", "CORRAL_RUNTIME": "pi"}
        with mock.patch.object(control, "_nearest_runtime", return_value=None):
            self.assertEqual(control.detect_sender(env=env), "pi:inner")

    def test_hosted_cursor_pane_resolves_through_keepalive_name(self):
        from corral import keepalive, liveness

        name = keepalive.session_name("cursor", "ab12cd34")
        sessions = [{"id": "other", "keepalive_name": "x"}, {"id": "chat-9", "keepalive_name": name}]
        runtime = mock.Mock()
        runtime.scan_sessions.return_value = sessions
        registry = mock.Mock()
        registry.get.return_value = runtime
        env = {"CORRAL_RUNTIME": "cursor", "CORRAL_SESSION_ID": "ab12cd34"}
        with mock.patch.object(liveness, "annotate"):
            self.assertEqual(control.detect_sender(registry, env=env), "cursor:chat-9")
        registry.get.assert_called_once_with("cursor")

    def test_hosted_lookup_needs_a_registry(self):
        env = {"CORRAL_RUNTIME": "cursor", "CORRAL_SESSION_ID": "ab12cd34"}
        self.assertIsNone(control.detect_sender(None, env=env))

    def test_runtime_of_process_args(self):
        cases = {
            "/opt/homebrew/bin/claude --resume x": "claude",
            "/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe": "claude",
            "/opt/homebrew/lib/node_modules/@openai/codex/vendor/bin/codex resume": "codex",
            "node /opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js": "codex",
            "/opt/homebrew/lib/node_modules/@opencode/cli/bin/opencode.exe -s ses": "opencode",
            "/Users/u/.local/share/cursor-agent/versions/1/cursor-agent --resume": "cursor",
            "node /opt/homebrew/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js": "pi",
            "/bin/zsh -c ls": None,
            "": None,
        }
        for args, expected in cases.items():
            self.assertEqual(control._runtime_of_args(args), expected, args)

    def test_resolve_sender_explicit_none_and_auto(self):
        self.assertEqual(control.resolve_sender("codex:t1"), "codex:t1")
        self.assertIsNone(control.resolve_sender("none"))
        self.assertIsNone(control.resolve_sender(""))
        with mock.patch.object(control, "detect_sender", return_value="pi:auto") as detect:
            self.assertEqual(control.resolve_sender(None, "reg"), "pi:auto")
        detect.assert_called_once_with("reg")


class SubmitSendTests(unittest.TestCase):
    def setUp(self):
        _clean_agent_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.root = Path(self.tmp.name) / "control"
        patcher = mock.patch.object(control_store, "control_home", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session = {
            "source": "claude",
            "id": "s-1",
            "cwd": str(self.project),
            "path": str(self.project / "h.jsonl"),
        }
        resolve = mock.patch.object(control_cli, "resolve_target", side_effect=lambda registry, ref: dict(self.session))
        resolve.start()
        self.addCleanup(resolve.stop)
        spawn = mock.patch.object(control_cli, "_spawn_supervisor", return_value=True)
        spawn.start()
        self.addCleanup(spawn.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _version(self):
        return control.target_payload(self.session)["version"]

    def test_submit_with_sender_stores_attribution_and_transforms_prompt(self):
        data = control_cli.cmd_submit(
            _submit_args(if_version=self._version(), from_agent="opencode:ses_abc"), None
        )["data"]
        self.assertEqual(data["status"], "created")
        self.assertEqual(data["run"]["from_agent"], "opencode:ses_abc")
        saved = RunStore().get(data["run"]["run_id"], private=True)
        self.assertEqual(saved["spec"]["from_agent"], "opencode:ses_abc")
        self.assertTrue(saved["spec"]["prompt"].startswith("[from opencode:ses_abc · peer, not user]"))
        self.assertTrue(saved["spec"]["prompt"].endswith('corral send opencode:ses_abc "<your reply>"'))

    def test_submit_detects_the_calling_agent(self):
        os.environ["CODEX_THREAD_ID"] = "thread-7"
        data = control_cli.cmd_submit(_submit_args(if_version=self._version()), None)["data"]
        self.assertEqual(data["run"]["from_agent"], "codex:thread-7")
        saved = RunStore().get(data["run"]["run_id"], private=True)
        self.assertTrue(saved["spec"]["prompt"].startswith("[from codex:thread-7 · peer, not user]"))

    def test_submit_from_agent_none_overrides_detection(self):
        os.environ["CODEX_THREAD_ID"] = "thread-7"
        data = control_cli.cmd_submit(_submit_args(if_version=self._version(), from_agent="none"), None)["data"]
        self.assertIsNone(data["run"]["from_agent"])
        saved = RunStore().get(data["run"]["run_id"], private=True)
        self.assertEqual(saved["spec"]["prompt"], "[from unknown · peer, not user]\n\nfix the bug")
        self.assertTrue(control.is_unattributed_header(saved["spec"]["prompt"]))

    def test_send_submits_in_one_step_with_generated_key(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "me-1"
        args = SimpleNamespace(target="claude:s-1", message="are you done?", prompt_file=None, from_agent=None,
                               no_reply_footer=False, on_busy="queue", queue_timeout=3600, idempotency_key=None,
                               compact=False)
        with mock.patch.object(control_cli, "resolve_target", return_value=dict(self.session)) as resolve:
            data = control_cli.cmd_send(args, None)["data"]
        resolve.assert_called_once()
        self.assertEqual(data["status"], "created")
        self.assertEqual(data["run"]["from_agent"], "claude:me-1")
        self.assertEqual(data["target"]["session_ref"], "claude:s-1")
        saved = RunStore().get(data["run"]["run_id"], private=True)
        self.assertEqual(
            saved["spec"]["prompt"],
            "[from claude:me-1 · peer, not user]\n\nare you done?",
        )
        self.assertTrue(saved["spec"]["report_back"])
        self.assertIn("claude:me-1", data["delivery_note"])

    def _send_args(self, **extra):
        base = dict(target="claude:s-1", message="hi", prompt_file=None, from_agent=None, no_reply_footer=False,
                    no_report_back=False, on_busy="queue", queue_timeout=3600, idempotency_key=None, compact=False)
        base.update(extra)
        return SimpleNamespace(**base)

    def test_send_without_report_back_names_the_reply_command(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "me-1"
        data = control_cli.cmd_send(self._send_args(no_report_back=True), None)["data"]
        saved = RunStore().get(data["run"]["run_id"], private=True)
        self.assertTrue(saved["spec"]["prompt"].endswith('corral send claude:me-1 "<your reply>"'))
        self.assertNotIn("report_back", saved["spec"])

    def test_unattributed_send_is_marked_and_has_no_report_back(self):
        data = control_cli.cmd_send(self._send_args(), None)["data"]
        saved = RunStore().get(data["run"]["run_id"], private=True)
        self.assertEqual(saved["spec"]["prompt"], "[from unknown · peer, not user]\n\nhi")
        self.assertNotIn("report_back", saved["spec"])
        self.assertIsNone(data["run"]["from_agent"])
        self.assertIsNone(control.parse_agent_header(saved["spec"]["prompt"]))
        self.assertTrue(control.is_unattributed_header(saved["spec"]["prompt"]))

    def test_send_to_own_session_is_refused(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "s-1"
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_send(self._send_args(), None)
        self.assertEqual(caught.exception.code, "self_target")

    def test_send_burst_is_rate_limited(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "me-1"
        with mock.patch.object(control_cli, "SEND_BURST_LIMIT", 2):
            control_cli.cmd_send(self._send_args(message="one"), None)
            control_cli.cmd_send(self._send_args(message="two"), None)
            with self.assertRaises(ControlError) as caught:
                control_cli.cmd_send(self._send_args(message="three"), None)
        self.assertEqual(caught.exception.code, "rate_limited")

    def test_send_rejects_inline_and_file_together(self):
        args = SimpleNamespace(target="claude:s-1", message="x", prompt_file="brief.md", from_agent=None,
                               no_reply_footer=False, on_busy="queue", queue_timeout=3600, idempotency_key=None)
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_send(args, None)
        self.assertEqual(caught.exception.code, "usage_error")

    def test_send_without_text_is_usage_error(self):
        args = SimpleNamespace(target="claude:s-1", message=None, prompt_file=None, from_agent=None,
                               no_reply_footer=False, on_busy="queue", queue_timeout=3600, idempotency_key=None)
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_send(args, None)
        self.assertEqual(caught.exception.code, "usage_error")

    def test_submit_without_sender_is_byte_compatible(self):
        data = control_cli.cmd_submit(_submit_args(if_version=self._version()), None)["data"]
        self.assertIsNone(data["run"].get("from_agent"))
        saved = RunStore().get(data["run"]["run_id"], private=True)
        self.assertEqual(saved["spec"]["prompt"], "fix the bug")
        self.assertNotIn("from_agent", saved["spec"])

    def test_dry_run_with_sender_writes_nothing(self):
        data = control_cli.cmd_submit(
            _submit_args(dry_run=True, from_agent="pi:s1"), None
        )["data"]
        self.assertEqual(data["status"], "planned")
        self.assertEqual(data["run"]["from_agent"], "pi:s1")
        self.assertFalse(self.root.exists())

    def test_retry_same_sender_and_footer_returns_existing(self):
        version = self._version()
        first = control_cli.cmd_submit(
            _submit_args(if_version=version, from_agent="opencode:ses_abc"), None
        )["data"]
        retry = control_cli.cmd_submit(
            _submit_args(if_version=version, from_agent="opencode:ses_abc"), None
        )["data"]
        self.assertEqual(retry["status"], "existing")
        self.assertEqual(first["run"]["run_id"], retry["run"]["run_id"])

    def test_retry_different_sender_conflicts(self):
        version = self._version()
        control_cli.cmd_submit(
            _submit_args(if_version=version, from_agent="opencode:ses_abc"), None
        )
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_submit(
                _submit_args(if_version=version, from_agent="claude:other"), None
            )
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_retry_different_footer_choice_conflicts(self):
        version = self._version()
        control_cli.cmd_submit(
            _submit_args(if_version=version, from_agent="opencode:ses_abc"), None
        )
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_submit(
                _submit_args(if_version=version, from_agent="opencode:ses_abc", no_reply_footer=True),
                None,
            )
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_duplicate_detection_uses_unwrapped_body(self):
        version = self._version()
        control_cli.cmd_submit(
            _submit_args(idempotency_key="k-a", if_version=version, from_agent="opencode:s1"), None
        )
        # Envelope/sender choice does not make the same body new information.
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_submit(
                _submit_args(idempotency_key="k-b", if_version=version, from_agent="claude:s2"), None
            )
        self.assertEqual(caught.exception.code, "duplicate_active_run")
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_submit(
                _submit_args(idempotency_key="k-c", if_version=version, from_agent="opencode:s1"), None
            )
        self.assertEqual(caught.exception.code, "duplicate_active_run")

    def test_invalid_sender_fails_before_target(self):
        with mock.patch.object(control_cli, "resolve_target", side_effect=AssertionError("must not rescan")):
            with self.assertRaises(ControlError) as caught:
                control_cli.cmd_submit(_submit_args(from_agent="kimi:k1"), None)
            self.assertEqual(caught.exception.code, "usage_error")


class MessageSendTests(unittest.TestCase):
    def setUp(self):
        _clean_agent_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.root = Path(self.tmp.name) / "control"
        patcher = mock.patch.object(control_store, "control_home", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.session = {
            "source": "claude",
            "id": "s-1",
            "cwd": str(self.project),
            "path": str(self.project / "h.jsonl"),
        }
        resolve = mock.patch.object(control_cli, "resolve_target", side_effect=lambda registry, ref: dict(self.session))
        resolve.start()
        self.addCleanup(resolve.stop)
        spawn = mock.patch.object(control_cli, "_spawn_supervisor", return_value=True)
        spawn.start()
        self.addCleanup(spawn.stop)
        alive = mock.patch.object(control_cli, "_supervisor_alive", return_value=True)
        alive.start()
        self.addCleanup(alive.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _submit(self, **extra):
        version = control.target_payload(self.session)["version"]
        params = dict(idempotency_key="run-k1", if_version=version)
        params.update(extra)
        return control_cli.cmd_submit(_submit_args(**params), None)["data"]["run"]["run_id"]

    def _message_args(self, run_id, version, **extra):
        base = dict(
            run_id=run_id,
            prompt="follow-up",
            prompt_file=None,
            if_version=version,
            idempotency_key="m-1",
            from_agent=None,
            no_reply_footer=False,
        )
        base.update(extra)
        return SimpleNamespace(**base)

    def test_message_with_sender_transforms_and_surfaces_last(self):
        run_id = self._submit(from_agent="opencode:ses_abc")
        version = RunStore().get(run_id)["version"]
        out = control_cli.cmd_message(
            self._message_args(run_id, version, from_agent="claude:reply-1"), None
        )["data"]
        self.assertEqual(out["status"], "created")
        self.assertEqual(out["run"]["from_agent"], "opencode:ses_abc")
        self.assertEqual(out["run"]["last_from_agent"], "claude:reply-1")
        status = control_cli.cmd_status(SimpleNamespace(run_ids=[run_id], compact=False), None)["data"]
        self.assertEqual(status["runs"][0]["last_from_agent"], "claude:reply-1")
        events = control_cli.cmd_events(SimpleNamespace(run_id=run_id, after=0, limit=50), None)["data"]
        self.assertEqual(events["from_agent"], "opencode:ses_abc")
        self.assertEqual(events["last_from_agent"], "claude:reply-1")

    def test_message_idempotent_retry(self):
        run_id = self._submit()
        version = RunStore().get(run_id)["version"]
        first = control_cli.cmd_message(
            self._message_args(run_id, version, from_agent="pi:s9"), None
        )["data"]
        retry = control_cli.cmd_message(
            self._message_args(run_id, version, from_agent="pi:s9"), None
        )["data"]
        self.assertEqual(retry["status"], "existing")
        self.assertEqual(first["message"]["message_id"], retry["message"]["message_id"])

    def test_message_different_sender_conflicts(self):
        run_id = self._submit()
        version = RunStore().get(run_id)["version"]
        control_cli.cmd_message(
            self._message_args(run_id, version, from_agent="pi:s9"), None
        )
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_message(
                self._message_args(run_id, version, from_agent="pi:other"), None
            )
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_message_without_sender_leaves_attribution(self):
        run_id = self._submit(from_agent="opencode:ses_abc")
        version = RunStore().get(run_id)["version"]
        out = control_cli.cmd_message(self._message_args(run_id, version), None)["data"]
        self.assertEqual(out["run"]["from_agent"], "opencode:ses_abc")
        self.assertIsNone(out["run"].get("last_from_agent"))


class DescribeSyncTests(unittest.TestCase):
    def _spec(self, name):
        return next(c for c in agent_api.COMMANDS if c["name"] == name)

    def test_submit_and_message_expose_send_flags(self):
        for name in ("send", "run submit", "run create", "run message"):
            flags = [f for arg in self._spec(name)["args"] for f in arg["flags"]]
            self.assertIn("--from-agent", flags, name)
            self.assertIn("--no-reply-footer", flags, name)

    def test_read_commands_stay_flag_free(self):
        for name in (
            "list", "search", "show", "export", "share", "context",
            "describe", "diagnose",
        ):
            flags = [f for arg in self._spec(name)["args"] for f in arg["flags"]]
            self.assertNotIn("--from-agent", flags, name)
            self.assertNotIn("--no-reply-footer", flags, name)

    def test_runtime_choices_unchanged_no_kimi(self):
        for name in ("run create", "skills status", "skills install", "skills uninstall"):
            spec = self._spec(name)
            for arg in spec["args"]:
                if "--runtime" in arg["flags"] and "choices" in arg["kwargs"]:
                    self.assertEqual(
                        sorted(arg["kwargs"]["choices"]),
                        ["antigravity", "claude", "codex", "cursor", "opencode", "pi"],
                        name,
                    )
        # --from-agent itself carries no hard-coded model or runtime choices.
        for name in ("run submit", "run message"):
            flags = [f for arg in self._spec(name)["args"] for f in arg["flags"]]
            self.assertNotIn("--model", flags, name)
            for arg in self._spec(name)["args"]:
                if "--from-agent" in arg["flags"]:
                    self.assertNotIn("choices", arg["kwargs"], name)


if __name__ == "__main__":
    unittest.main()


class ReportBackTests(unittest.TestCase):
    def setUp(self):
        _clean_agent_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(control_store, "control_home", return_value=Path(self.tmp.name) / "control")
        patcher.start()
        self.addCleanup(patcher.stop)
        project = Path(self.tmp.name) / "proj"
        project.mkdir()
        self.session = {"source": "claude", "id": "s-1", "cwd": str(project), "path": str(project / "h.jsonl")}
        for name, value in (("resolve_target", None), ("_spawn_supervisor", True)):
            if name == "resolve_target":
                p = mock.patch.object(control_cli, name, side_effect=lambda registry, ref: dict(self.session))
            else:
                p = mock.patch.object(control_cli, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _settled_send(self, execution="completed", answer="Done: 4", key="k1"):
        from corral import control_worker

        args = SimpleNamespace(target="claude:s-1", message="what is 2+2?", prompt_file=None,
                               from_agent="pi:p1", no_reply_footer=False, no_report_back=False,
                               on_busy="queue", queue_timeout=3600, idempotency_key=key, compact=False)
        run_id = control_cli.cmd_send(args, None)["data"]["run"]["run_id"]
        store = RunStore()
        store.update(run_id, {"execution": execution, "delivery": "native_recorded",
                              "result": {"final_response": answer}}, allow_settled=True)
        return control_worker, store, run_id

    def test_completed_answer_is_sent_back_once_from_the_receiver(self):
        control_worker, store, run_id = self._settled_send()
        sent = []

        os.environ["CORRAL_RUNTIME"] = "pi"
        os.environ["CORRAL_SESSION_ID"] = "p1"  # the sender's pane identity, inherited by the supervisor

        def fake_send(args, registry):
            sent.append(args)
            self.assertNotIn("CORRAL_SESSION_ID", os.environ)
            self.assertEqual(control.caller_identity()["actor"], "claude:s-1")
            return {"data": {"run": {"run_id": "run_reply"}}}

        with mock.patch.object(control_cli, "cmd_send", side_effect=fake_send):
            first = control_worker.report_back(store, run_id, registry="reg")
            second = control_worker.report_back(store, run_id, registry="reg")
        self.assertEqual(first, {"state": "sent", "run_id": "run_reply"})
        self.assertEqual(second, first)
        self.assertEqual(len(sent), 1)
        args = sent[0]
        self.assertEqual(args.target, "pi:p1")
        self.assertEqual(args.from_agent, "claude:s-1")
        self.assertTrue(args.no_report_back and args.no_reply_footer)
        self.assertIn("claude:s-1 answered your message", args.message)
        self.assertIn("Done: 4", args.message)
        self.assertEqual(RunStore().get(run_id)["report_back"]["state"], "sent")

    def test_failure_is_reported_with_reason_and_send_errors_are_recorded(self):
        control_worker, store, run_id = self._settled_send(execution="failed", answer="")
        with mock.patch.object(control_cli, "cmd_send",
                               side_effect=ControlError("external_session", "owned elsewhere", 5)) as send:
            outcome = control_worker.report_back(store, run_id, registry="reg")
        self.assertIn("ended as failed", send.call_args[0][0].message)
        self.assertEqual(outcome["state"], "failed")
        self.assertEqual(outcome["code"], "external_session")

    def test_interrupted_unknown_and_empty_outcomes_never_wake_the_sender(self):
        for index, (execution, answer, reason) in enumerate((("interrupted", "", "interrupted"),
                                                            ("unknown", "", "unknown"),
                                                            ("completed", "", "empty_answer"))):
            control_worker, store, run_id = self._settled_send(execution=execution, answer=answer,
                                                               key=f"k-{index}")
            with mock.patch.object(control_cli, "cmd_send") as send:
                outcome = control_worker.report_back(store, run_id, registry="reg")
            send.assert_not_called()
            self.assertEqual(outcome, {"state": "suppressed", "reason": reason})
            self.assertEqual(store.get(run_id)["report_back"], {"state": "suppressed", "reason": reason})

    def test_unsettled_or_unrequested_runs_are_not_reported(self):
        from corral import control_worker

        args = SimpleNamespace(target="claude:s-1", message="x", prompt_file=None, from_agent="pi:p1",
                               no_reply_footer=False, no_report_back=True, on_busy="queue", queue_timeout=3600,
                               idempotency_key="k2", compact=False)
        run_id = control_cli.cmd_send(args, None)["data"]["run"]["run_id"]
        store = RunStore()
        with mock.patch.object(control_cli, "cmd_send") as send:
            self.assertIsNone(control_worker.report_back(store, run_id))
            store.update(run_id, {"execution": "completed"}, allow_settled=True)
            self.assertIsNone(control_worker.report_back(store, run_id))
        send.assert_not_called()

    def test_long_answers_are_truncated_with_a_pointer(self):
        from corral import control_worker

        text = control_worker.report_back_text({"session_ref": "codex:t", "run_id": "run_1", "execution": "completed",
                                                "result": {"final_response": "x" * 9000}})
        self.assertIn("corral run result run_1", text)
        self.assertLess(len(text), 6300)


class CreateBriefEnvelopeTests(unittest.TestCase):
    """A creation brief is owner input; only peer messages carry the envelope."""

    def setUp(self):
        _clean_agent_env(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        patcher = mock.patch.object(control_store, "control_home", return_value=Path(self.tmp.name) / "control")
        patcher.start()
        self.addCleanup(patcher.stop)
        for target, value in (("_spawn_supervisor", True), ("_supervisor_alive", True), ("_guard_capacity", None)):
            patcher = mock.patch.object(control_cli, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        plan = SimpleNamespace(argv=["pi"], cwd=str(self.project))
        patcher = mock.patch.object(control_cli, "fresh_plan", return_value=plan)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("corral.embed.available", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        class _Runtime:
            id = "pi"
            display_name = "Pi"

            def is_available(self):
                return True

        class _Registry:
            def get(self, runtime_id):
                return _Runtime()

        self.registry = _Registry()

    def tearDown(self):
        self.tmp.cleanup()

    def _args(self, **extra):
        base = dict(runtime="pi", cwd=str(self.project), model=None, prompt="You are the peer.", prompt_file=None,
                    from_agent="pi:creator", no_reply_footer=False, idempotency_key="create-1",
                    dry_run=False, task_ref=None, compact=False)
        base.update(extra)
        return SimpleNamespace(**base)

    def test_creation_brief_is_stored_verbatim_and_keeps_attribution(self):
        data = control_cli.cmd_create(self._args(), self.registry)["data"]
        saved = RunStore().get(data["run"]["run_id"], private=True)
        self.assertEqual(saved["spec"]["prompt"], "You are the peer.")
        self.assertEqual(saved["spec"]["from_agent"], "pi:creator")
        self.assertEqual(data["run"]["from_agent"], "pi:creator")


class PeersTests(unittest.TestCase):
    def test_peers_lists_running_agents_with_state_and_reachability(self):
        _clean_agent_env(self)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "me"
        here = os.path.realpath(os.getcwd())
        sessions = {
            "claude": [
                {"source": "claude", "id": "me", "cwd": here, "live": True, "mtime": 9},
                {"source": "claude", "id": "busy", "cwd": here, "live": True, "keepalive_name": "corral-claude-busy",
                 "mtime": 5, "last_user_msg": "fix the parser", "last_agent_msg": "Editing parser.py"},
            ],
            "codex": [
                {"source": "codex", "id": "ext", "cwd": "/elsewhere", "live": True, "mtime": 8},
                {"source": "codex", "id": "old", "cwd": here, "live": False, "mtime": 1},
            ],
        }
        registry = [SimpleNamespace(id="claude"), SimpleNamespace(id="codex")]

        def attention(flat):
            for item in flat:
                item["attention_kind"] = "working" if item["id"] == "busy" else "none"

        args = SimpleNamespace(project=None, recent=None, limit=60, top=None)
        with (
            mock.patch.object(agent_api, "_list_scan", return_value=(sessions, {}, {})),
            mock.patch.object(agent_api.liveness, "annotate"),
            mock.patch.object(agent_api, "_attach_attention", side_effect=attention),
            mock.patch.object(agent_api.titles, "load_cache", return_value={}),
            mock.patch.object(agent_api.titles, "resolve_initial_title", return_value=("T", None)),
        ):
            data = agent_api.cmd_peers(args, registry)["data"]
        self.assertEqual(data["self"], "claude:me")
        refs = [p["ref"] for p in data["peers"]]
        self.assertEqual(refs, ["claude:busy", "codex:ext"])
        busy, ext = data["peers"]
        self.assertEqual((busy["state"], busy["reachable"], busy["same_project"]), ("working", True, True))
        self.assertEqual(busy["last_user"], "fix the parser")
        self.assertEqual((ext["state"], ext["reachable"], ext["control"]), ("idle", False, "external"))
