"""Agent delegation control layer: store contract, correlation, admission and skills."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sesskit.models import ActivityEvent, Evidence, Turn, TurnOutcome

from corral import control, control_cli, control_store, control_worker, skill_bundle
from corral.control_store import ControlError, RunStore

try:
    from tests.hub_stub import (
        _append_fixture_row,
        _ClaudeComposerStub,
        _FakeHub,
        _StubCodexRuntime,
        _StubRuntime,
        _write_fixture_rows,
    )
except ImportError:  # CI discovery / pytest rootdir mode: tests/ itself is on sys.path
    from hub_stub import (
        _append_fixture_row,
        _ClaudeComposerStub,
        _FakeHub,
        _StubCodexRuntime,
        _StubRuntime,
        _write_fixture_rows,
    )

SRC = str(Path(__file__).resolve().parents[1] / "src")
_EV = Evidence(origin="native")


def _event(seq: int, kind: str, text: str | None = None, **extra) -> ActivityEvent:
    return ActivityEvent(seq=seq, type=kind, evidence=_EV, text=text, **extra)


class FirstTurnFailureTests(unittest.TestCase):
    def test_claude_failed_first_turn_remains_discoverable(self):
        from sesskit.parsers import claude

        from corral.runtime.host_extension import corral_host_extension

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "histories" / "project"
            project.mkdir(parents=True)
            prompt = "Repair the billing discount formula."
            wire = control.wire_prompt("run_aabb", "initial", prompt)
            path = project / "session.jsonl"
            rows = [
                {"type": "user", "cwd": str(root), "message": {"content": wire}},
                {
                    "type": "assistant",
                    "isApiErrorMessage": True,
                    "apiErrorStatus": 401,
                    "message": {"content": [{"type": "text", "text": "Authentication failed"}]},
                },
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with (
                mock.patch.object(claude, "PROJECTS_DIR", str(project.parent)),
                mock.patch.object(claude, "_live_session_ids", return_value={}),
            ):
                sessions = claude.scan_sessions(host=corral_host_extension())
            self.assertEqual([s["id"] for s in sessions], ["session"])
            self.assertTrue(sessions[0]["fallback_title"].startswith(prompt))
            self.assertTrue(wire.startswith(prompt))
            self.assertIn(control.marker("run_aabb", "initial"), wire)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self.tmp.name) / "control")

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self, key="k1", prompt="fix it"):
        return self.store.create("agent:a", key, {"prompt": prompt}, {"delivery": "accepted", "execution": "queued"})

    def test_reads_never_create_storage(self):
        with self.assertRaises(ControlError) as caught:
            self.store.get("run_missing")
        self.assertEqual(caught.exception.code, "not_found")
        self.assertFalse(self.store.root.exists())
        self.assertEqual(self.store.active_hosts(), set())
        self.assertFalse(self.store.root.exists())

    def test_same_key_same_spec_returns_existing_run(self):
        first, created = self._create()
        again, created_again = self._create()
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["run_id"], again["run_id"])

    def test_same_key_other_spec_conflicts_even_for_another_actor(self):
        self._create()
        with self.assertRaises(ControlError) as caught:
            self.store.create("agent:b", "k1", {"prompt": "other"}, {"delivery": "accepted", "execution": "queued"})
        self.assertEqual(caught.exception.code, "idempotency_conflict")
        self.assertEqual(caught.exception.exit_code, control_store.EXIT_CONFLICT)

    def test_version_ignores_progress_and_tracks_state(self):
        run, _ = self._create()
        unchanged = self.store.update(run["run_id"], {"turn_id": "t1", "host": "corral-x"})
        self.assertEqual(run["version"], unchanged["version"])
        moved = self.store.update(run["run_id"], {"execution": "running"})
        self.assertNotEqual(run["version"], moved["version"])

    def test_finished_run_is_immutable_and_rejects_messages(self):
        run, _ = self._create()
        done = self.store.update(run["run_id"], {"execution": "completed"})
        with self.assertRaises(ControlError) as caught:
            self.store.update(run["run_id"], {"execution": "running"})
        self.assertEqual(caught.exception.code, "terminal_run")
        with self.assertRaises(ControlError) as caught:
            self.store.enqueue(run["run_id"], "agent:a", "m1", {"action": "steer", "prompt": "x"}, done["version"])
        self.assertEqual(caught.exception.code, "terminal_run")

    def test_enqueue_requires_fresh_version_and_is_idempotent(self):
        run, _ = self._create()
        with self.assertRaises(ControlError) as caught:
            self.store.enqueue(run["run_id"], "agent:a", "m1", {"action": "steer", "prompt": "x"}, "v2:stale")
        self.assertEqual(caught.exception.code, "conflict")
        message, created = self.store.enqueue(
            run["run_id"], "agent:a", "m1", {"action": "steer", "prompt": "x"}, run["version"]
        )
        again, created_again = self.store.enqueue(
            run["run_id"], "agent:a", "m1", {"action": "steer", "prompt": "x"}, "v2:any"
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(message["message_id"], again["message_id"])

    def test_every_write_is_audited(self):
        run, _ = self._create()
        self.store.update(run["run_id"], {"execution": "running"})
        with self.store.connection() as conn:
            rows = conn.execute("SELECT actor, action, target FROM audit ORDER BY seq").fetchall()
        actions = [(r["actor"], r["action"], r["target"]) for r in rows]
        self.assertIn(("agent:a", "run.create", run["run_id"]), actions)
        self.assertIn((control_store.SUPERVISOR, "run.update", run["run_id"]), actions)

    def test_events_page_with_cursor(self):
        run, _ = self._create()
        for index in range(5):
            self.store.append(run["run_id"], f"e{index}", {"kind": "activity", "native_seq": index})
        page = self.store.events(run["run_id"], 0, 3)
        self.assertTrue(page["has_more"])
        rest = self.store.events(run["run_id"], page["next_cursor"], 10)
        seqs = [e["seq"] for e in page["events"] + rest["events"]]
        self.assertEqual(seqs, sorted(set(seqs)))
        self.assertFalse(rest["has_more"])

    def test_active_hosts_lists_only_unsettled_runs(self):
        run, _ = self._create()
        self.store.update(run["run_id"], {"host": "corral-claude-a"})
        other, _ = self._create(key="k2", prompt="two")
        self.store.update(other["run_id"], {"host": "corral-claude-b", "execution": "completed"})
        self.assertEqual(self.store.active_hosts(), {"corral-claude-a"})


class CorrelationTests(unittest.TestCase):
    RUN = "run_" + "a" * 32

    def _turns(self, *spans, status="completed", origin="native"):
        return [
            Turn(
                turn_id=f"t{i}",
                index=i,
                start_seq=a,
                end_seq=b,
                user_seq=a,
                outcome=TurnOutcome(status=status, evidence=Evidence(origin=origin)),
            )
            for i, (a, b) in enumerate(spans)
        ]

    def test_binds_only_own_marker_after_baseline(self):
        old = _event(1, "user_message", control.wire_prompt(self.RUN, "initial", "old copy"))
        events = [
            old,
            _event(2, "assistant_message", "x"),
            _event(3, "user_message", control.wire_prompt(self.RUN, "initial", "fix")),
            _event(4, "assistant_message", "fixed"),
        ]
        result = control.correlate(events, self._turns((1, 2), (3, 4)), self.RUN, baseline_seq=2)
        self.assertTrue(result["recorded"])
        self.assertEqual(result["first_seq"], 3)
        self.assertEqual(result["attribution"], "correlated")
        self.assertEqual(control.build_result(result)["final_response"], "fixed")

    def test_long_trailing_marker_binds_completion_after_quoted_other_run(self):
        other = "run_" + "b" * 32
        prompt = control.marker(other, "initial") + "\n" + "需求与背景。" * 300
        text = control.wire_prompt(self.RUN, "initial", prompt)
        events = [_event(1, "user_message", text), _event(2, "assistant_message", "report ready")]
        result = control.correlate(events, self._turns((1, 2)), self.RUN, baseline_seq=-1)
        self.assertTrue(result["recorded"])
        self.assertEqual(result["messages"], ["initial"])
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(control.build_result(result)["final_response"], "report ready")

    def test_legacy_prefix_marker_still_matches(self):
        text = control.marker(self.RUN, "initial") + "\n" + "legacy brief " * 100
        self.assertEqual(control.own_message(text, self.RUN), "initial")

    def test_quoted_marker_in_assistant_output_is_not_delivery(self):
        quoted = _event(3, "assistant_message", control.marker(self.RUN, "initial"))
        result = control.correlate([quoted], self._turns((3, 3)), self.RUN, baseline_seq=0)
        self.assertFalse(result["recorded"])

    def test_foreign_input_in_bound_turn_is_mixed(self):
        events = [
            _event(3, "user_message", control.wire_prompt(self.RUN, "initial", "fix")),
            _event(4, "user_message", "human says something else"),
            _event(5, "assistant_message", "done"),
        ]
        result = control.correlate(events, self._turns((3, 5)), self.RUN, baseline_seq=0)
        self.assertEqual(result["attribution"], "mixed")

    def test_other_run_marker_does_not_match(self):
        other = "run_" + "b" * 32
        events = [_event(3, "user_message", control.wire_prompt(other, "initial", "fix"))]
        self.assertFalse(control.correlate(events, self._turns((3, 3)), self.RUN, baseline_seq=0)["recorded"])

    def test_model_check(self):
        self.assertIsNone(control_worker.model_check(None, ["x"]))
        self.assertEqual(control_worker.model_check("gpt-6", []), "unobserved")
        self.assertEqual(control_worker.model_check("claude-opus-5-5", ["claude-opus-5-5"]), "match")
        self.assertEqual(control_worker.model_check("opus", ["claude-opus-5-5"]), "alias_match")
        self.assertEqual(control_worker.model_check("haiku", ["claude-opus-5-5"]), "mismatch")


class AdmissionTests(unittest.TestCase):
    def setUp(self):
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
        self.spawn = spawn.start()
        self.addCleanup(spawn.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _args(self, **extra):
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
        )
        base.update(extra)
        return SimpleNamespace(**base)

    def test_dry_run_writes_nothing_and_returns_version(self):
        data = control_cli.cmd_submit(self._args(dry_run=True), None)["data"]
        self.assertEqual(data["status"], "planned")
        self.assertEqual(data["target"]["control"], "resume")
        self.assertTrue(data["target"]["version"].startswith("t1:"))
        self.assertFalse(self.root.exists())
        self.spawn.assert_not_called()

    def test_submit_requires_matching_version(self):
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_submit(self._args(), None)
        self.assertEqual(caught.exception.code, "confirmation_required")
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_submit(self._args(if_version="t1:stale"), None)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertFalse(self.root.exists())

    def test_submit_creates_once_and_rejects_duplicate_brief_under_new_key(self):
        version = control.target_payload(self.session)["version"]
        first = control_cli.cmd_submit(self._args(if_version=version), None)["data"]
        retry = control_cli.cmd_submit(self._args(if_version=version), None)["data"]
        self.assertEqual(first["status"], "created")
        self.assertEqual(retry["status"], "existing")
        self.assertEqual(first["run"]["run_id"], retry["run"]["run_id"])
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_submit(self._args(if_version=version, idempotency_key="key-2"), None)
        self.assertEqual(caught.exception.code, "duplicate_active_run")
        allowed = control_cli.cmd_submit(
            self._args(if_version=version, idempotency_key="key-3", allow_concurrent=True), None
        )["data"]
        self.assertEqual(allowed["status"], "created")

    def test_external_live_session_is_never_targeted(self):
        self.session.update(live=True)
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_submit(self._args(dry_run=True), None)
        self.assertEqual(caught.exception.code, "external_session")

    def test_create_rejects_protected_directories(self):
        with self.assertRaises(ControlError) as caught:
            control_cli._guard_create_directory(str(Path.home().resolve()))
        self.assertEqual(caught.exception.code, "directory_not_allowed")
        control_cli._guard_create_directory(str(self.project))

    def test_wait_timeout_keeps_snapshot_and_exit_six(self):
        version = control.target_payload(self.session)["version"]
        run_id = control_cli.cmd_submit(self._args(if_version=version), None)["data"]["run"]["run_id"]
        with mock.patch.object(control_cli, "_supervisor_alive", return_value=True):
            with self.assertRaises(ControlError) as caught:
                control_cli.cmd_wait(SimpleNamespace(run_ids=[run_id], timeout=0, until="actionable"), None)
        self.assertEqual(caught.exception.code, "wait_timeout")
        self.assertEqual(caught.exception.exit_code, 6)
        self.assertEqual(caught.exception.details["runs"][0]["run_id"], run_id)

    def test_wait_reports_lost_supervisor(self):
        version = control.target_payload(self.session)["version"]
        run_id = control_cli.cmd_submit(self._args(if_version=version), None)["data"]["run"]["run_id"]
        data = control_cli.cmd_wait(SimpleNamespace(run_ids=[run_id], timeout=1, until="actionable"), None)["data"]
        self.assertEqual(data["ready"][run_id], "supervisor_lost")

    def test_result_before_settlement_is_not_ready(self):
        version = control.target_payload(self.session)["version"]
        run_id = control_cli.cmd_submit(self._args(if_version=version), None)["data"]["run"]["run_id"]
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_result(SimpleNamespace(run_id=run_id), None)
        self.assertEqual(caught.exception.code, "result_not_ready")

    def test_steer_rejected_while_question_pending(self):
        version = control.target_payload(self.session)["version"]
        run_id = control_cli.cmd_submit(self._args(if_version=version), None)["data"]["run"]["run_id"]
        RunStore().update(run_id, {"execution": "waiting_input", "questions": [{"request_id": "q1"}]})
        args = SimpleNamespace(run_id=run_id, prompt="more", prompt_file=None, if_version="x", idempotency_key="m")
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_message(args, None)
        self.assertEqual(caught.exception.code, "waiting_input")

    def test_prompt_validation(self):
        for bad in ("", "   ", "a\x00b", "x" * (control.MAX_PROMPT_CHARS + 1)):
            with self.assertRaises(ControlError):
                control.validate_prompt(bad)

    def test_retry_returns_existing_before_target_version_or_availability(self):
        version = control.target_payload(self.session)["version"]
        first = control_cli.cmd_submit(self._args(if_version=version), None)["data"]
        with mock.patch.object(control_cli, "resolve_target", side_effect=AssertionError("must not rescan")):
            retry = control_cli.cmd_submit(self._args(if_version=version), None)["data"]
            self.assertEqual(retry["run"]["run_id"], first["run"]["run_id"])
            with self.assertRaises(ControlError) as caught:
                control_cli.cmd_submit(self._args(prompt="different", if_version=version), None)
            self.assertEqual(caught.exception.code, "idempotency_conflict")


class SupervisorHelpersTests(unittest.TestCase):
    def test_queue_expiry_only_before_dispatch(self):
        now = time.time()
        spec = {"queue_timeout": 10}
        self.assertTrue(control_worker.expired(spec, {"delivery": "accepted", "created_at": now - 11}))
        self.assertFalse(control_worker.expired(spec, {"delivery": "input_submitted", "created_at": now - 11}))
        self.assertFalse(control_worker.expired({"queue_timeout": 0}, {"delivery": "accepted", "created_at": 0}))

    def test_composer_state_reads_only_non_dim_text(self):
        from corral.runtime.claude import ClaudeRuntime

        rule = "─" * 20
        empty = f"{rule}\n❯ \x1b[2mTry something\x1b[0m\n{rule}\n"
        draft = f"{rule}\n❯ half typed\n{rule}\n"
        with mock.patch("corral.embed.capture", return_value=empty):
            self.assertEqual(control_worker.composer_state(ClaudeRuntime(), "pane"), "empty")
        with mock.patch("corral.embed.capture", return_value=draft):
            self.assertEqual(control_worker.composer_state(ClaudeRuntime(), "pane"), "draft")
        with mock.patch("corral.embed.capture", return_value=None):
            self.assertEqual(control_worker.composer_state(ClaudeRuntime(), "pane"), "unknown")

    def test_file_lock_excludes_second_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp))
            with control_worker.file_lock(store, "target:x") as first:
                self.assertTrue(first)
                self.assertTrue(control_worker.lock_held(store, "target:x"))
            self.assertFalse(control_worker.lock_held(store, "target:x"))


class SkillBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        shared = self.home / "shared"
        shared.mkdir()
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "skills").symlink_to(shared)
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "skills").symlink_to(shared)
        self.codex = self.home / ".codex" / "skills"
        self.codex.mkdir(parents=True)
        roots = [
            {"runtime": "claude", "root": str(self.home / ".claude/skills"), "available": True},
            {"runtime": "cursor", "root": str(self.home / ".cursor/skills"), "available": True},
            {"runtime": "codex", "root": str(self.codex), "available": True},
            {"runtime": "pi", "root": str(self.home / ".pi/agent/skills"), "available": False},
        ]
        for entry in roots:
            entry["resolved_root"] = os.path.realpath(entry["root"])
            entry["root_exists"] = Path(entry["resolved_root"]).is_dir()
        self.patches = [
            mock.patch.object(skill_bundle, "runtime_roots", return_value=roots),
            mock.patch.object(skill_bundle, "manifest_path", return_value=self.home / "manifest.json"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def test_dry_run_is_read_only_and_aliases_dedupe(self):
        plan = skill_bundle.install(dry_run=True)
        paths = {a["path"] for a in plan["actions"]}
        self.assertEqual(len(paths), 4)  # two skills x two physical roots, absent pi untouched
        self.assertFalse((self.home / "shared" / "corral-search").exists())
        self.assertFalse((self.home / "manifest.json").exists())
        self.assertFalse((self.home / ".pi").exists())

    def test_install_converges_and_uninstall_removes_only_owned(self):
        first = skill_bundle.install(dry_run=False)
        self.assertEqual(first["status"], "applied")
        self.assertTrue((self.home / "shared" / "corral-delegate" / "SKILL.md").is_file())
        second = skill_bundle.install(dry_run=False)
        self.assertEqual(second["status"], "unchanged")
        edited = self.codex / "corral-search" / "SKILL.md"
        edited.write_text("user edit", encoding="utf-8")
        removed = skill_bundle.uninstall(dry_run=False)
        actions = {a["path"]: a["action"] for a in removed["actions"]}
        self.assertEqual(actions[str(self.codex / "corral-search")], "keep_modified")
        self.assertTrue(edited.is_file())
        self.assertFalse((self.home / "shared" / "corral-search").exists())

    def test_foreign_same_name_skill_is_a_conflict(self):
        foreign = self.codex / "corral-search"
        foreign.mkdir()
        (foreign / "SKILL.md").write_text("someone else's skill", encoding="utf-8")
        result = skill_bundle.install(dry_run=False)
        self.assertEqual([c["path"] for c in result["conflicts"]], [str(foreign)])
        self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), "someone else's skill")

    def test_bundled_skill_frontmatter_is_portable(self):
        for name in skill_bundle.BUNDLE_NAMES:
            text = skill_bundle.bundle_text(name)
            self.assertTrue(text.startswith(f"---\nname: {name}\ndescription: "))
            self.assertNotIn(str(Path.home()), text)

    def test_identical_unowned_skill_is_not_adopted(self):
        target = self.codex / "corral-search"
        target.mkdir()
        (target / "SKILL.md").write_text(skill_bundle.bundle_text("corral-search"))
        (target / "user-notes.md").write_text("keep me")
        result = skill_bundle.install(dry_run=False)
        self.assertIn(str(target), [c["path"] for c in result["conflicts"]])
        skill_bundle.uninstall(dry_run=False)
        self.assertEqual((target / "user-notes.md").read_text(), "keep me")
        self.assertTrue((target / "SKILL.md").is_file())

    def test_update_and_uninstall_preserve_added_user_files(self):
        skill_bundle.install(dry_run=False)
        target = self.codex / "corral-search"
        notes = target / "user-notes.md"
        notes.write_text("keep me")
        original = skill_bundle.bundle_text
        with mock.patch.object(skill_bundle, "bundle_text", side_effect=lambda name: original(name) + "\nUpdate\n"):
            skill_bundle.install(dry_run=False)
            self.assertEqual(notes.read_text(), "keep me")
            skill_bundle.uninstall(dry_run=False)
        self.assertEqual(notes.read_text(), "keep me")
        self.assertFalse((target / "SKILL.md").exists())

    def test_skill_and_file_symlinks_conflict(self):
        skill_bundle.install(dry_run=False)
        target = self.codex / "corral-search" / "SKILL.md"
        external = self.home / "external.md"
        external.write_text(target.read_text())
        target.unlink()
        target.symlink_to(external)
        result = skill_bundle.install(dry_run=False)
        self.assertIn(str(target.parent), [c["path"] for c in result["conflicts"]])
        skill_bundle.uninstall(dry_run=False)
        self.assertTrue(target.is_symlink())
        self.assertTrue(external.is_file())

    def test_interrupted_registration_reconciles_journal(self):
        original = skill_bundle._write_skill

        def crash_after_write(path, name):
            original(path, name)
            raise RuntimeError("crash after rename")

        with mock.patch.object(skill_bundle, "_write_skill", side_effect=crash_after_write):
            with self.assertRaises(RuntimeError):
                skill_bundle.install(dry_run=False)
        self.assertTrue(skill_bundle._load_manifest()["pending"])
        result = skill_bundle.install(dry_run=False)
        self.assertEqual(result["conflicts"], [])
        self.assertFalse(skill_bundle._load_manifest()["pending"])

    def test_auto_registration_detects_new_runtime_without_version_change(self):
        skill_bundle.auto_register()
        manifest = skill_bundle._load_manifest()
        self.assertIn("bundle_version", manifest)
        with (
            mock.patch.object(skill_bundle, "digest_roots", return_value="changed"),
            mock.patch.object(skill_bundle, "install") as install,
        ):
            skill_bundle.auto_register()
        install.assert_called_once_with(dry_run=False)


class SupervisorReviewTests(unittest.TestCase):
    """Port-level supervision: shared runner plus Corral host hooks.

    Terminal effects go through a stub hub; observation goes through real
    SessKit reads of a fixture history that the stub hub appends to when it
    "delivers" (simulating the runtime persisting input, nothing more).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = str(Path(self.tmp.name) / "proj")
        os.makedirs(self.project)
        self.store = RunStore(Path(self.tmp.name) / "control")
        watcher = mock.patch("corral.attention_signals.inspect_session", return_value=SimpleNamespace(phase="unknown"))
        watcher.start()
        self.addCleanup(watcher.stop)

    def _history(self, name="h.jsonl"):
        return str(Path(self.tmp.name) / name)

    def _session(self, path, live=True):
        return {
            "source": "claude",
            "id": "fix-1",
            "cwd": self.project,
            "path": path,
            "live": live,
            "keepalive_name": "corral-claude-fix",
        }

    def _submit(self, key, session, prompt="fix the bug", run_key="k1", target_control="hosted"):
        spec = {
            "mode": "submit",
            "runtime": "claude",
            "session_ref": f"claude:{session['id']}",
            "cwd": self.project,
            "history_path": session["path"],
            "prompt": prompt,
            "on_busy": "queue",
            "queue_timeout": 0,
            "caller": {"actor": "t"},
            "task_ref": None,
        }
        initial = {
            "mode": "submit",
            "runtime": "claude",
            "session_ref": spec["session_ref"],
            "cwd": self.project,
            "history_path": session["path"],
            "delivery": "accepted",
            "execution": "queued",
            "acceptance": "unverified",
            "requested_model": None,
            "target_control": target_control,
        }
        run, _ = self.store.create("t", key, spec, initial)
        return run["run_id"]

    def _run_shared(self, run_id, hub, **kwargs):
        from agent_session_control.supervisor import run_supervisor

        hooks, state = control_worker.corral_hooks(self.store, run_id, hub)
        if "history_wait" not in kwargs:
            kwargs["history_wait"] = control_worker._runner_history_wait()
        rc = run_supervisor(self.store, run_id, hooks, marker_namespace="Corral", **kwargs)
        control_worker.finalize_run(self.store, run_id, state)
        try:
            state["locks"].close()
        except Exception:  # noqa: BLE001 - teardown must not fail assertions
            pass
        return rc

    def test_submit_settles_completed_through_shared_runner(self):
        from corral import control as control_mod

        path = self._history()
        _write_fixture_rows(
            path,
            [
                {"type": "user", "cwd": self.project, "message": {"content": "old question"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "old answer"}]}},
            ],
        )
        session = self._session(path)
        hub = _FakeHub(session)
        run_id = self._submit("k-settle", session)

        def _persist(text):
            _append_fixture_row(path, {"type": "user", "cwd": self.project, "message": {"content": text}})
            _append_fixture_row(
                path, {"type": "assistant", "message": {"content": [{"type": "text", "text": "ACK done"}]}}
            )

        hub.append_on_send = _persist
        with (
            mock.patch("corral.embed.capture", return_value=""),
            mock.patch("corral.embed.pane_liveness", return_value="alive"),
            mock.patch("corral.embed.input_transaction", return_value=nullcontext()),
        ):
            self.assertEqual(self._run_shared(run_id, hub), 0)
        record = self.store.get(run_id)
        self.assertEqual(record["execution"], "completed")
        self.assertEqual(record["delivery"], "native_recorded")
        self.assertEqual(record["finality"], "native" if record.get("finality") == "native" else "inferred")
        self.assertEqual(record["result"]["final_response"], "ACK done")
        self.assertEqual(record["result"]["acceptance"], "unverified")
        self.assertEqual(len(hub.sent), 1)
        self.assertIn(control_mod.marker(run_id, "initial"), hub.sent[0][1])
        self.assertEqual(record["host"], "corral-claude-fix")

    def test_queued_cancellation_needs_no_target_and_no_resend(self):
        path = self._history()
        _write_fixture_rows(
            path,
            [
                {"type": "user", "cwd": self.project, "message": {"content": "old"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "old"}]}},
            ],
        )
        session = self._session(path)
        hub = _FakeHub(session)
        run_id = self._submit("k-cancel", session)
        version = self.store.get(run_id)["version"]
        self.store.enqueue(run_id, "t", "m-cancel", {"action": "interrupt"}, version)
        self.assertEqual(self._run_shared(run_id, hub), 0)
        record = self.store.get(run_id)
        self.assertEqual(record["execution"], "interrupted")
        self.assertEqual(record["delivery"], "rejected")
        self.assertEqual(hub.sent, [])
        self.assertEqual(hub.interrupted, [])

    def test_unowned_input_rejects_interrupt_and_stays_recoverable(self):
        path = self._history()
        _write_fixture_rows(
            path,
            [
                {"type": "user", "cwd": self.project, "message": {"content": "someone else"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
            ],
        )
        session = self._session(path)
        hub = _FakeHub(session)

        def _persist_foreign(text):
            _append_fixture_row(
                path, {"type": "user", "cwd": self.project, "message": {"content": "foreign follow-up"}}
            )

        hub.append_on_send = _persist_foreign
        run_id = self._submit("k-foreign", session)

        def _interrupt_mid_turn():
            for _ in range(200):
                record = self.store.get(run_id)
                if record.get("delivery") not in ("accepted",):
                    break
                time.sleep(0.1)
            version = self.store.get(run_id)["version"]
            self.store.enqueue(run_id, "t", "m-int", {"action": "interrupt"}, version)

        interrupter = threading.Thread(target=_interrupt_mid_turn, daemon=True)
        interrupter.start()
        try:
            with (
                mock.patch("corral.embed.capture", return_value=""),
                mock.patch("corral.embed.pane_liveness", return_value="alive"),
                mock.patch("corral.embed.input_transaction", return_value=nullcontext()),
            ):
                self.assertEqual(self._run_shared(run_id, hub, history_wait=4), 0)
        finally:
            interrupter.join(timeout=30)
        record = self.store.get(run_id)
        self.assertEqual(hub.interrupted, [])
        messages = {m["message_id"]: m["state"] for m in self.store.messages(run_id)}
        self.assertTrue(any(state == "rejected" for state in messages.values()))
        self.assertEqual(record["execution"], "unknown")

    def test_composer_draft_blocks_dispatch(self):
        path = self._history()
        Path(path).write_text("")
        session = self._session(path)
        hub = _FakeHub(session, runtime=_ClaudeComposerStub([session]))
        run_id = self._submit("k-draft", session)
        rule = "─" * 20
        screen = f"{rule}\n❯ half typed\n{rule}\n"
        with mock.patch("corral.embed.capture", return_value=screen):
            self.assertEqual(self._run_shared(run_id, hub), 0)
        record = self.store.get(run_id)
        self.assertEqual(record["execution"], "failed")
        self.assertEqual(record["delivery"], "rejected")
        self.assertEqual(record["error"]["code"], "composer_not_empty")
        self.assertEqual(hub.sent, [])

    def test_missing_history_settles_unknown_with_exact_evidence(self):
        hub = _FakeHub(runtime=_StubCodexRuntime())
        spec = {
            "mode": "create",
            "runtime": "codex",
            "cwd": self.project,
            "model": None,
            "prompt": "fresh work",
            "on_busy": "steer",
            "queue_timeout": 0,
            "caller": {"actor": "t"},
            "task_ref": None,
        }
        initial = {
            "mode": "create",
            "runtime": "codex",
            "session_ref": None,
            "cwd": self.project,
            "delivery": "accepted",
            "execution": "queued",
            "acceptance": "unverified",
            "requested_model": None,
        }
        run, _ = self.store.create("t", "k-fresh", spec, initial)
        run_id = run["run_id"]
        with (
            mock.patch.dict(os.environ, {"CORRAL_HISTORY_WAIT_TIMEOUT": "4"}),
            mock.patch("corral.embed.pane_liveness", return_value="alive"),
        ):
            self.assertEqual(self._run_shared(run_id, hub), 0)
            record = self.store.get(run_id)
            self.assertEqual(record["execution"], "unknown")
            self.assertEqual(record["error"], control_worker._history_wait_error("native_history_missing"))
        self.assertEqual(len(hub.sent), 1)

    def test_target_lock_contention_reports_target_locked(self):
        hub = _FakeHub(runtime=_StubCodexRuntime(), planned_key="codex:fix-abc")
        bound = {
            "source": "codex",
            "id": "fix-abc",
            "cwd": self.project,
            "path": "/nonexistent/h.jsonl",
            "live": True,
            "keepalive_name": "corral-codex-abc",
        }
        hub.store.by_key["codex:fix-abc"] = bound
        spec = {
            "mode": "create",
            "runtime": "codex",
            "cwd": self.project,
            "model": None,
            "prompt": "fresh work",
            "on_busy": "steer",
            "queue_timeout": 0,
            "caller": {"actor": "t"},
            "task_ref": None,
        }
        initial = {
            "mode": "create",
            "runtime": "codex",
            "session_ref": None,
            "cwd": self.project,
            "delivery": "accepted",
            "execution": "queued",
            "acceptance": "unverified",
            "requested_model": None,
        }
        run, _ = self.store.create("t", "k-lock", spec, initial)
        run_id = run["run_id"]
        with control_worker.file_lock(self.store, "target:codex:fix-abc") as owned:
            self.assertTrue(owned)
            with mock.patch("corral.embed.pane_liveness", return_value="alive"):
                self.assertEqual(self._run_shared(run_id, hub, history_wait=30), 0)
        record = self.store.get(run_id)
        self.assertEqual(record["execution"], "unknown")
        self.assertEqual(record["error"]["code"], "target_locked")
        self.assertFalse(control_worker.lock_held(self.store, "target:codex:fix-abc"))

    def test_completion_waits_for_every_accepted_followup(self):
        run, _ = self.store.create("a", "kf", {"prompt": "x"}, {"delivery": "accepted", "execution": "running"})
        run_id = run["run_id"]
        version = self.store.get(run_id)["version"]
        ident = self.store.enqueue(run_id, "a", "m", {"action": "steer", "prompt": "more"}, version)[0]["message_id"]
        for state in ("queued", "dispatching", "unknown", "input_submitted", "native_recorded"):
            self.store.message_state(ident, state)
            result = self.store.update(
                run_id, {"execution": "completed", "result": {"final_response": "old"}}, settled_messages={"initial"}
            )
            self.assertEqual(result["execution"], "running", state)
            self.assertNotIn("result", result)
        done = self.store.update(run_id, {"execution": "completed"}, settled_messages={"initial", ident})
        self.assertEqual(done["execution"], "completed")

    def test_queued_cancellation_does_not_wait_for_target_lock(self):
        path = self._history()
        Path(path).write_text("")
        session = self._session(path)
        hub = _FakeHub(session)
        run_id = self._submit("k-main", session)
        version = self.store.get(run_id)["version"]
        self.store.enqueue(run_id, "t", "m-main", {"action": "interrupt"}, version)
        with (
            mock.patch.object(control_worker, "RunStore", return_value=self.store),
            mock.patch("corral.remote.sessions.SessionHub", return_value=hub),
            mock.patch.object(control_worker, "_earlier_runs", return_value=True),
            mock.patch.object(control_worker.time, "sleep") as sleep,
        ):
            self.assertEqual(control_worker.main(run_id), 0)
        sleep.assert_not_called()
        self.assertEqual(hub.interrupted, [])
        self.assertEqual(self.store.get(run_id)["execution"], "interrupted")

    def _old_turn(self, path):
        _write_fixture_rows(
            path,
            [
                {"type": "user", "cwd": self.project, "message": {"content": "old question"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "old answer"}]}},
            ],
        )

    def _wait_for_thread_recorded(self, run_id):
        deadline = time.monotonic() + 60
        while self.store.get(run_id).get("delivery") != "native_recorded":
            if time.monotonic() >= deadline:
                raise AssertionError("timed out waiting for native recording")
            time.sleep(0.1)

    def test_main_submit_detached_settles_completed(self):
        import corral as corral_pkg
        from corral import control as control_mod

        path = self._history()
        _write_fixture_rows(
            path,
            [
                {"type": "user", "cwd": self.project, "message": {"content": "old question"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "old answer"}]}},
            ],
        )
        session = self._session(path)
        hub = _FakeHub(session)
        run_id = self._submit("k-detached", session)

        def _persist(text):
            _append_fixture_row(path, {"type": "user", "cwd": self.project, "message": {"content": text}})

        hub.append_on_send = _persist
        outcome: dict = {}

        def _main():
            with (
                mock.patch.object(control_worker, "RunStore", return_value=self.store),
                mock.patch("corral.remote.sessions.SessionHub", return_value=hub),
            ):
                outcome["rc"] = control_worker.main(run_id)

        thread = threading.Thread(target=_main, daemon=True)
        cap = mock.patch("corral.embed.capture", return_value="")
        pane = mock.patch("corral.embed.pane_liveness", return_value="alive")
        txn = mock.patch("corral.embed.input_transaction", return_value=nullcontext())
        with cap, pane, txn:
            thread.start()
            self._wait_for_thread_recorded(run_id)
            _append_fixture_row(
                path, {"type": "assistant", "message": {"content": [{"type": "text", "text": "detached done"}]}}
            )
            thread.join(timeout=120)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome.get("rc"), 0)
        record = self.store.get(run_id)
        self.assertEqual(record["execution"], "completed")
        self.assertEqual(record["result"]["final_response"], "detached done")
        self.assertIn(control_mod.marker(run_id, "initial"), hub.sent[0][1])
        self.assertEqual(record.get("supervisor_version"), corral_pkg.__version__)

    def test_main_create_detached_binds_and_settles(self):
        path = self._history()
        self._old_turn(path)
        hub = _FakeHub(runtime=_StubRuntime(), planned_key="claude:fix-det")
        hub.store.by_key["claude:fix-det"] = {
            "source": "claude",
            "id": "fix-det",
            "cwd": self.project,
            "path": path,
            "live": True,
            "keepalive_name": "corral-claude-det",
        }
        spec = {
            "mode": "create",
            "runtime": "claude",
            "cwd": self.project,
            "model": None,
            "prompt": "fresh detached",
            "on_busy": "steer",
            "queue_timeout": 0,
            "caller": {"actor": "t"},
            "task_ref": None,
        }
        initial = {
            "mode": "create",
            "runtime": "claude",
            "session_ref": None,
            "cwd": self.project,
            "delivery": "accepted",
            "execution": "queued",
            "acceptance": "unverified",
            "requested_model": None,
        }
        run, _ = self.store.create("t", "k-detached-create", spec, initial)
        run_id = run["run_id"]

        def _persist(text):
            _append_fixture_row(path, {"type": "user", "cwd": self.project, "message": {"content": text}})

        hub.append_on_send = _persist
        outcome: dict = {}

        def _main():
            with (
                mock.patch.object(control_worker, "RunStore", return_value=self.store),
                mock.patch("corral.remote.sessions.SessionHub", return_value=hub),
            ):
                outcome["rc"] = control_worker.main(run_id)

        thread = threading.Thread(target=_main, daemon=True)
        cap = mock.patch("corral.embed.capture", return_value="")
        pane = mock.patch("corral.embed.pane_liveness", return_value="alive")
        txn = mock.patch("corral.embed.input_transaction", return_value=nullcontext())
        trust = mock.patch("corral.control_worker._plain", return_value="composer ready")
        with cap, pane, txn, trust:
            thread.start()
            self._wait_for_thread_recorded(run_id)
            _append_fixture_row(
                path, {"type": "assistant", "message": {"content": [{"type": "text", "text": "fresh detached done"}]}}
            )
            thread.join(timeout=120)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome.get("rc"), 0)
        record = self.store.get(run_id)
        self.assertEqual(record["execution"], "completed")
        self.assertEqual(record["session_ref"], "claude:fix-det")
        self.assertEqual(record["result"]["final_response"], "fresh detached done")


class SearchCoverageTests(unittest.TestCase):
    def test_history_read_failures_are_incomplete_coverage(self):
        from corral import agent_api

        session = {"source": "claude", "id": "one", "mtime": 1}
        runtime = SimpleNamespace(id="claude")
        args = SimpleNamespace(
            keywords=["missing-word"],
            scope="activity",
            deep=True,
            runtime=None,
            project=None,
            compact=False,
            top=None,
            fields=None,
            limit=20,
            live=False,
            keepalive=False,
        )
        with (
            mock.patch.object(agent_api, "_scan_runtimes", return_value=({"claude": [session]}, {})),
            mock.patch.object(agent_api, "_load_conversation", side_effect=OSError("conversation unreadable")),
            mock.patch.object(agent_api.liveness, "annotate"),
            mock.patch.object(agent_api, "_attach_attention"),
            mock.patch.object(agent_api.titles, "load_cache", return_value={}),
            mock.patch.object(agent_api.titles, "resolve_initial_title", return_value=("other", None)),
            mock.patch("sesskit.load_activity", side_effect=OSError("activity unreadable")),
        ):
            data = agent_api.cmd_search(args, [runtime])["data"]
        self.assertEqual(data["count"], 0)
        self.assertTrue(data["potentially_limited"])
        self.assertIn("claude", data["failed_runtimes"])
        self.assertEqual({f["scope"] for f in data["history_read_failures"]}, {"conversation", "activity"})


class GateTests(unittest.TestCase):
    def _describe(self, env: dict) -> list[str]:
        out = subprocess.run(
            [sys.executable, "-m", "corral", "describe"],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": SRC, **env},
            check=True,
        ).stdout
        return [c["name"] for c in json.loads(out)["data"]["commands"]]

    def test_control_commands_available_without_preview_environment(self):
        env = {k: v for k, v in os.environ.items() if k != "CORRAL_CONTROL_PREVIEW"}
        with mock.patch.dict(os.environ, env, clear=True):
            shown = self._describe({})
        for name in ("run submit", "run create", "run wait", "run answer", "skills install"):
            self.assertIn(name, shown)


if __name__ == "__main__":
    unittest.main()
