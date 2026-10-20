"""Agent delegation control layer: store contract, correlation, admission and skills."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sesskit.models import ActivityEvent, Evidence, Turn, TurnOutcome

from corral import control, control_cli, control_store, control_worker, skill_bundle
from corral.control_store import ControlError, RunStore

SRC = str(Path(__file__).resolve().parents[1] / "src")
_EV = Evidence(origin="native")


def _event(seq: int, kind: str, text: str | None = None, **extra) -> ActivityEvent:
    return ActivityEvent(seq=seq, type=kind, evidence=_EV, text=text, **extra)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RunStore(Path(self.tmp.name) / "control")

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self, key="k1", prompt="fix it"):
        return self.store.create("agent:a", key, {"prompt": prompt},
                                 {"delivery": "accepted", "execution": "queued"})

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
        message, created = self.store.enqueue(run["run_id"], "agent:a", "m1", {"action": "steer", "prompt": "x"},
                                              run["version"])
        again, created_again = self.store.enqueue(run["run_id"], "agent:a", "m1",
                                                  {"action": "steer", "prompt": "x"}, "v2:any")
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
        return [Turn(turn_id=f"t{i}", index=i, start_seq=a, end_seq=b, user_seq=a,
                     outcome=TurnOutcome(status=status, evidence=Evidence(origin=origin)))
                for i, (a, b) in enumerate(spans)]

    def test_binds_only_own_marker_after_baseline(self):
        old = _event(1, "user_message", control.wire_prompt(self.RUN, "initial", "old copy"))
        events = [old, _event(2, "assistant_message", "x"),
                  _event(3, "user_message", control.wire_prompt(self.RUN, "initial", "fix")),
                  _event(4, "assistant_message", "fixed")]
        result = control.correlate(events, self._turns((1, 2), (3, 4)), self.RUN, baseline_seq=2)
        self.assertTrue(result["recorded"])
        self.assertEqual(result["first_seq"], 3)
        self.assertEqual(result["attribution"], "correlated")
        self.assertEqual(control.build_result(result)["final_response"], "fixed")

    def test_quoted_marker_in_assistant_output_is_not_delivery(self):
        quoted = _event(3, "assistant_message", control.marker(self.RUN, "initial"))
        result = control.correlate([quoted], self._turns((3, 3)), self.RUN, baseline_seq=0)
        self.assertFalse(result["recorded"])

    def test_foreign_input_in_bound_turn_is_mixed(self):
        events = [_event(3, "user_message", control.wire_prompt(self.RUN, "initial", "fix")),
                  _event(4, "user_message", "human says something else"),
                  _event(5, "assistant_message", "done")]
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
        self.session = {"source": "claude", "id": "s-1", "cwd": str(self.project),
                        "path": str(self.project / "h.jsonl")}
        resolve = mock.patch.object(control_cli, "resolve_target", side_effect=lambda registry, ref: dict(self.session))
        resolve.start()
        self.addCleanup(resolve.stop)
        spawn = mock.patch.object(control_cli, "_spawn_supervisor", return_value=True)
        self.spawn = spawn.start()
        self.addCleanup(spawn.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _args(self, **extra):
        base = dict(target="claude:s-1", prompt="fix the bug", prompt_file=None, idempotency_key="key-1",
                    if_version=None, on_busy="queue", queue_timeout=3600, allow_concurrent=False, task_ref=None,
                    dry_run=False)
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
        allowed = control_cli.cmd_submit(self._args(if_version=version, idempotency_key="key-3",
                                                    allow_concurrent=True), None)["data"]
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
        self.patches = [mock.patch.object(skill_bundle, "runtime_roots", return_value=roots),
                        mock.patch.object(skill_bundle, "manifest_path", return_value=self.home / "manifest.json")]
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
        with mock.patch.object(skill_bundle, "digest_roots", return_value="changed"), \
                mock.patch.object(skill_bundle, "install") as install:
            skill_bundle.auto_register()
        install.assert_called_once_with(dry_run=False)



class SupervisorReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = RunStore(Path(self.tmp.name) / "control")
        self.spec = {"mode": "submit", "runtime": "claude", "session_ref": "claude:one"}
        self.run, _ = self.store.create("a", "k", self.spec,
                                       {"session_ref": "claude:one", "delivery": "native_recorded",
                                        "execution": "running", "created_at": time.time()})
        self.run_id = self.run["run_id"]
        self.hub = mock.Mock()
        self.hub.registry.get.return_value = SimpleNamespace(id="claude")
        self.supervisor = control_worker.Supervisor(self.store, self.run_id, self.hub)
        self.addCleanup(self.supervisor.close)

    def enqueue(self, action="steer"):
        return self.store.enqueue(self.run_id, "a", "m", {"action": action, "prompt": "more"},
                                  self.store.get(self.run_id)["version"])[0]["message_id"]

    def test_crash_after_send_does_not_replay_followup(self):
        ident = self.enqueue()
        self.hub.send_text.side_effect = RuntimeError("crash after successful send")
        with mock.patch("corral.embed.input_transaction", return_value=__import__('contextlib').nullcontext()):
            with self.assertRaises(RuntimeError):
                self.supervisor._process_messages(None)
            self.assertEqual(self.store.messages(self.run_id)[0]["state"], "dispatching")
            self.supervisor._recover_messages()
            self.supervisor._process_messages(None)
        self.assertEqual(self.hub.send_text.call_count, 1)
        self.assertEqual(self.store.messages(self.run_id)[0]["state"], "unknown")
        self.supervisor._record_messages({"messages": [ident]})
        self.assertEqual(self.store.messages(self.run_id)[0]["state"], "native_recorded")

    def test_completion_waits_for_every_accepted_followup(self):
        ident = self.enqueue()
        for state in ("queued", "dispatching", "unknown", "input_submitted", "native_recorded"):
            self.store.message_state(ident, state)
            result = self.store.update(self.run_id, {"execution": "completed", "result": {"final_response": "old"}},
                                       settled_messages={"initial"})
            self.assertEqual(result["execution"], "running", state)
            self.assertNotIn("result", result)
        done = self.store.update(self.run_id, {"execution": "completed"}, settled_messages={"initial", ident})
        self.assertEqual(done["execution"], "completed")

    def test_interrupt_requires_positive_current_turn_ownership(self):
        self.enqueue("interrupt")
        self.supervisor._process_messages({"recorded": False})
        self.hub.interrupt_session.assert_not_called()
        self.assertEqual(self.store.messages(self.run_id)[0]["state"], "rejected")

    def test_interrupt_rejects_old_owned_turn_with_new_foreign_input(self):
        events = [_event(1, "user_message", control.wire_prompt(self.run_id, "initial", "fix")),
                  _event(2, "assistant_message", "done"), _event(3, "user_message", "other work")]
        turn = Turn(turn_id="t1", index=0, start_seq=1, end_seq=2, user_seq=1,
                    outcome=TurnOutcome(status="completed", evidence=_EV))
        observation = control.correlate(events, [turn], self.run_id, -1)
        self.assertFalse(observation["current_turn"])
        self.enqueue("interrupt")
        self.supervisor._process_messages(observation)
        self.hub.interrupt_session.assert_not_called()

    def test_fresh_binding_lock_contention_stops_control_and_closes_locks(self):
        self.supervisor.key = "claude:new"
        with control_worker.file_lock(self.store, "target:claude:new") as owned:
            self.assertTrue(owned)
            with self.assertRaises(ControlError) as caught:
                self.supervisor._bind_target_lock()
            self.assertEqual(caught.exception.code, "target_locked")
        self.supervisor._bind_target_lock()
        self.assertTrue(control_worker.lock_held(self.store, "target:claude:new"))
        self.supervisor.close()
        self.assertFalse(control_worker.lock_held(self.store, "target:claude:new"))

    def test_queued_cancellation_does_not_wait_for_target_lock(self):
        self.store.update(self.run_id, {"delivery": "accepted", "execution": "queued"})
        self.enqueue("interrupt")
        with mock.patch.object(control_worker, "RunStore", return_value=self.store), \
                mock.patch("corral.remote.sessions.SessionHub", return_value=self.hub), \
                mock.patch.object(control_worker, "_earlier_runs", return_value=True), \
                mock.patch.object(control_worker.time, "sleep") as sleep:
            self.assertEqual(control_worker.main(self.run_id), 0)
        sleep.assert_not_called()
        self.hub.interrupt_session.assert_not_called()
        self.assertEqual(self.store.get(self.run_id)["execution"], "interrupted")


class SearchCoverageTests(unittest.TestCase):
    def test_history_read_failures_are_incomplete_coverage(self):
        from corral import agent_api

        session = {"source": "claude", "id": "one", "mtime": 1}
        runtime = SimpleNamespace(id="claude")
        args = SimpleNamespace(keywords=["missing-word"], scope="activity", deep=True, runtime=None,
                               project=None, compact=False, top=None, fields=None, limit=20,
                               live=False, keepalive=False)
        with mock.patch.object(agent_api, "_scan_runtimes", return_value=({"claude": [session]}, {})), \
                mock.patch.object(agent_api, "_load_conversation", side_effect=OSError("conversation unreadable")), \
                mock.patch.object(agent_api.liveness, "annotate"), \
                mock.patch.object(agent_api, "_attach_attention"), \
                mock.patch.object(agent_api.titles, "load_cache", return_value={}), \
                mock.patch.object(agent_api.titles, "resolve_initial_title", return_value=("other", None)), \
                mock.patch("sesskit.load_activity", side_effect=OSError("activity unreadable")):
            data = agent_api.cmd_search(args, [runtime])["data"]
        self.assertEqual(data["count"], 0)
        self.assertTrue(data["potentially_limited"])
        self.assertIn("claude", data["failed_runtimes"])
        self.assertEqual({f["scope"] for f in data["history_read_failures"]}, {"conversation", "activity"})



class GateTests(unittest.TestCase):
    def _describe(self, env: dict) -> list[str]:
        out = subprocess.run([sys.executable, "-m", "corral", "describe"], capture_output=True, text=True,
                             env={**os.environ, "PYTHONPATH": SRC, **env}, check=True).stdout
        return [c["name"] for c in json.loads(out)["data"]["commands"]]

    def test_control_commands_available_without_preview_environment(self):
        env = {k: v for k, v in os.environ.items() if k != "CORRAL_CONTROL_PREVIEW"}
        with mock.patch.dict(os.environ, env, clear=True):
            shown = self._describe({})
        for name in ("run submit", "run create", "run wait", "run answer", "skills install"):
            self.assertIn(name, shown)


if __name__ == "__main__":
    unittest.main()
