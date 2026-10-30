"""Bounded supervision of runs without usable native history.

Port-level tests through the shared runner plus Corral host hooks: a
submitted run whose native history never becomes readable settles as a
recoverable ``unknown`` instead of supervising forever, while a valid bound
long-running agent is never timed out for duration. Observation always goes
through REAL SessKit reads of fixture histories (present, missing or
materialized mid-run); only the terminal side effects go through the stub
hub. Small budgets come from CORRAL_HISTORY_WAIT_TIMEOUT plus the runner
margin, never from drum-tight sleeps. No real tmux, network or supervisor
process is involved.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from corral import control_cli, control_worker
from corral.control_store import RunStore

try:
    from tests.hub_stub import (
        _append_fixture_row,
        _FakeHub,
        _StubRuntime,
        _write_fixture_rows,
    )
except ImportError:  # CI discovery / pytest rootdir mode: tests/ itself is on sys.path
    from hub_stub import (
        _append_fixture_row,
        _FakeHub,
        _StubRuntime,
        _write_fixture_rows,
    )

_BOUND = {"source": "claude", "id": "one", "path": "/fixture/history.jsonl", "keepalive_name": None}


class HistoryBoundObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = str(Path(self.tmp.name) / "project")
        os.makedirs(self.project)
        watcher = mock.patch("corral.attention_signals.inspect_session", return_value=SimpleNamespace(phase="unknown"))
        watcher.start()
        self.addCleanup(watcher.stop)
        self.count = 0

    def make_store(self):
        self.count += 1
        return RunStore(Path(self.tmp.name) / f"control-{self.count}")

    def _session(self, path, live=True):
        return {
            "source": "claude",
            "id": "fix-mem",
            "cwd": self.project,
            "path": path,
            "live": live,
            "keepalive_name": "corral-claude-mem",
        }

    def _submit(self, store, key, session, prompt="fix it", on_busy="queue"):
        spec = {
            "mode": "submit",
            "runtime": "claude",
            "session_ref": "claude:fix-mem",
            "cwd": self.project,
            "history_path": session["path"],
            "prompt": prompt,
            "on_busy": on_busy,
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
            "target_control": "hosted",
        }
        run, _ = store.create("t", key, spec, initial)
        return run["run_id"]

    def _old_turn(self, path):
        _write_fixture_rows(
            path,
            [
                {"type": "user", "cwd": self.project, "message": {"content": "old question"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "old answer"}]}},
            ],
        )

    def _mocks(self, pane="alive"):
        return (
            mock.patch("corral.embed.capture", return_value=""),
            mock.patch("corral.embed.pane_liveness", return_value=pane),
            mock.patch("corral.embed.input_transaction", return_value=nullcontext()),
        )

    def _run_shared(self, store, run_id, hub, budget=4, **kwargs):
        """Shared runner with Corral hooks; engine budget always trails the hook budget."""
        from agent_session_control.supervisor import run_supervisor

        kwargs.setdefault("history_wait", budget + 3)
        with mock.patch.dict(os.environ, {"CORRAL_HISTORY_WAIT_TIMEOUT": str(budget)}):
            hooks, state = control_worker.corral_hooks(store, run_id, hub)
            try:
                return run_supervisor(store, run_id, hooks, marker_namespace="Corral", **kwargs)
            finally:
                try:
                    state["locks"].close()
                except Exception:  # noqa: BLE001 - teardown must not fail assertions
                    pass

    def _run_thread(self, store, run_id, hub, budget=4, **kwargs):
        outcome: dict = {}

        def _target():
            with mock.patch.dict(os.environ, {"CORRAL_HISTORY_WAIT_TIMEOUT": str(budget)}):
                from agent_session_control.supervisor import run_supervisor

                hooks, state = control_worker.corral_hooks(store, run_id, hub)
                try:
                    outcome["rc"] = run_supervisor(
                        store,
                        run_id,
                        hooks,
                        marker_namespace="Corral",
                        history_wait=kwargs.pop("history_wait", budget + 3),
                        **kwargs,
                    )
                except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                    outcome["error"] = exc
                finally:
                    try:
                        state["locks"].close()
                    except Exception:  # noqa: BLE001 - teardown must not fail assertions
                        pass

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        return thread, outcome

    @staticmethod
    def _wait_for(predicate, timeout=60.0, what="condition"):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError(f"timed out waiting for {what}")
            time.sleep(0.1)

    def assert_audited(self, store: RunStore, run_id: str):
        with store.connection() as conn:
            rows = conn.execute("SELECT actor, action, target FROM audit WHERE target=?", (run_id,)).fetchall()
        self.assertTrue(
            any(r["actor"] == "corral-supervisor" and r["action"] == "run.update" for r in rows),
            [dict(r) for r in rows],
        )
        states = [e for e in store.events(run_id, 0, 500)["events"] if e.get("kind") == "state"]
        self.assertTrue(any(s.get("execution") == "unknown" and s.get("error") for s in states), states)

    def test_unbound_run_settles_recoverable_unknown_when_pane_is_not_dead(self):
        # "alive" and "unknown" pane liveness both read as not dead; the
        # supervisor must infer neither death nor progress from that.
        for liveness in ("alive", "unknown"):
            with self.subTest(pane=liveness):
                store = self.make_store()
                path = str(Path(self.tmp.name) / f"h-{liveness}.jsonl")
                Path(path).write_text("", encoding="utf-8")
                session = self._session(None)
                hub = _FakeHub(session, runtime=_StubRuntime([session]))
                run_id = self._submit(store, f"k-{liveness}", session)
                cap, pane, txn = self._mocks(pane=liveness)
                with cap, pane, txn:
                    start = time.monotonic()
                    self.assertEqual(self._run_shared(store, run_id, hub, budget=4), 0)
                    elapsed = time.monotonic() - start
                record = store.get(run_id)
                self.assertEqual(record["execution"], "unknown")
                self.assertEqual(record["delivery"], "input_submitted")  # no replay, no fake rejection
                self.assertEqual(record["error"]["code"], "native_history_missing")
                self.assertIn("not judged dead", record["error"]["message"])
                self.assertGreaterEqual(elapsed, 4)
                self.assertLess(elapsed, 4 + 3 + 10)
                self.assertEqual(len(hub.sent), 1)  # dispatched exactly once, never resent
                self.assert_audited(store, run_id)

    def test_binding_before_the_deadline_restarts_the_wait(self):
        store = self.make_store()
        path = str(Path(self.tmp.name) / "h-late.jsonl")
        self._old_turn(path)
        hub = _FakeHub(runtime=_StubRuntime(), planned_key="claude:fix-late")
        spec = {
            "mode": "create",
            "runtime": "claude",
            "cwd": self.project,
            "model": None,
            "prompt": "fix it",
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
        run, _ = store.create("t", "k-late", spec, initial)
        run_id = run["run_id"]
        bound_session = {
            "source": "claude",
            "id": "fix-late",
            "cwd": self.project,
            "path": path,
            "live": True,
            "keepalive_name": "corral-claude-late",
        }
        refreshes: list = []
        real_refresh = hub.store.refresh

        def _late_refresh():
            refreshes.append(True)
            if len(refreshes) >= 3:
                hub.store.by_key["claude:fix-late"] = bound_session
            return real_refresh()

        def _persist_wire(text):
            _append_fixture_row(path, {"type": "user", "cwd": self.project, "message": {"content": text}})

        hub.append_on_send = _persist_wire
        cap, pane, txn = self._mocks()
        trust = mock.patch("corral.control_worker._plain", return_value="composer ready")
        with cap, pane, txn, trust, mock.patch.object(hub.store, "refresh", side_effect=_late_refresh):
            thread, outcome = self._run_thread(store, run_id, hub, budget=30, settle_quiet=2)
            self._wait_for(
                lambda: store.get(run_id).get("delivery") == "native_recorded",
                timeout=60,
                what="native recording after late bind",
            )
            record = store.get(run_id)
            self.assertEqual(record["execution"], "running")
            self.assertIsNone(record.get("error"))
            self.assertGreater(len(refreshes), 1)  # bounded retries, not one blind wait
            _append_fixture_row(
                path,
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "late done"}]}},
            )
            thread.join(timeout=120)
        self.assertFalse(thread.is_alive())
        record = store.get(run_id)
        self.assertEqual(record["execution"], "completed")
        self.assertEqual(record["result"]["final_response"], "late done")
        self.assertEqual(len(hub.sent), 1)

    def test_known_dead_pane_keeps_the_existing_terminal_exited_outcome(self):
        store = self.make_store()
        path = str(Path(self.tmp.name) / "h-dead.jsonl")
        Path(path).write_text("", encoding="utf-8")
        session = self._session(None)
        hub = _FakeHub(session, runtime=_StubRuntime([session]))
        run_id = self._submit(store, "k-dead", session)
        cap, pane, txn = self._mocks(pane="dead")
        with cap, pane, txn:
            start = time.monotonic()
            self.assertEqual(self._run_shared(store, run_id, hub, budget=30), 0)
            elapsed = time.monotonic() - start
        record = store.get(run_id)
        self.assertEqual(record["execution"], "unknown")
        self.assertEqual(record["delivery"], "unknown")
        self.assertEqual(record["error"]["code"], "terminal_exited")
        self.assertLess(elapsed, 30)  # positive death short-circuits any budget
        self.assertEqual(len(hub.sent), 1)  # dispatch ran; death arrived while awaiting history

    def test_bound_reader_that_never_becomes_readable_settles_unknown(self):
        store = self.make_store()
        missing = str(Path(self.tmp.name) / "gone.jsonl")
        session = self._session(missing)
        hub = _FakeHub(session, runtime=_StubRuntime([session]))
        run_id = self._submit(store, "k-unreadable", session)
        cap, pane, txn = self._mocks()
        with cap, pane, txn:
            start = time.monotonic()
            self.assertEqual(self._run_shared(store, run_id, hub, budget=4), 0)
            elapsed = time.monotonic() - start
        record = store.get(run_id)
        self.assertEqual(record["execution"], "unknown")
        self.assertEqual(record["delivery"], "input_submitted")
        self.assertEqual(record["error"]["code"], "native_history_unavailable")
        self.assertIn("unreadable", record["error"]["message"])
        self.assertGreaterEqual(elapsed, 4)
        self.assertLess(elapsed, 4 + 3 + 10)
        self.assertEqual(len(hub.sent), 1)
        self.assert_audited(store, run_id)

    def test_dead_pane_wins_over_an_unreadable_bound_history(self):
        store = self.make_store()
        missing = str(Path(self.tmp.name) / "gone2.jsonl")
        session = self._session(missing)
        hub = _FakeHub(session, runtime=_StubRuntime([session]))
        run_id = self._submit(store, "k-deadbound", session)
        cap, pane, txn = self._mocks(pane="dead")
        with cap, pane, txn:
            start = time.monotonic()
            self.assertEqual(self._run_shared(store, run_id, hub, budget=30), 0)
            elapsed = time.monotonic() - start
        record = store.get(run_id)
        self.assertEqual(record["execution"], "unknown")
        self.assertEqual(record["error"]["code"], "terminal_exited")
        self.assertLess(elapsed, 30)
        self.assertEqual(len(hub.sent), 1)

    def test_transiently_unreadable_reader_recovers_and_settles(self):
        store = self.make_store()
        path = str(Path(self.tmp.name) / "h-later.jsonl")
        session = self._session(path)
        hub = _FakeHub(session, runtime=_StubRuntime([session]))
        run_id = self._submit(store, "k-transient", session)

        def _persist_wire(text):
            _append_fixture_row(path, {"type": "user", "cwd": self.project, "message": {"content": text}})

        hub.append_on_send = _persist_wire
        cap, pane, txn = self._mocks()
        with cap, pane, txn:
            thread, outcome = self._run_thread(store, run_id, hub, budget=30, settle_quiet=2)
            self._wait_for(lambda: len(hub.sent) >= 1, timeout=30, what="brief submission")
            time.sleep(2)
            _append_fixture_row(
                path,
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "recovered done"}]}},
            )
            thread.join(timeout=120)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", outcome)
        record = store.get(run_id)
        self.assertEqual(record["execution"], "completed")
        self.assertEqual(record["result"]["final_response"], "recovered done")
        self.assertIsNone(record.get("error"))
        self.assertEqual(len(hub.sent), 1)

    def test_external_settlement_wins_over_the_history_budget(self):
        store = self.make_store()
        session = self._session(None)
        hub = _FakeHub(session, runtime=_StubRuntime([session]))
        run_id = self._submit(store, "k-external", session)
        refreshes: list = []
        real_refresh = hub.store.refresh

        def _settle_refresh():
            refreshes.append(True)
            if len(refreshes) >= 2:
                store.update(run_id, {"execution": "completed"})
            return real_refresh()

        cap, pane, txn = self._mocks()
        with cap, pane, txn, mock.patch.object(hub.store, "refresh", side_effect=_settle_refresh):
            start = time.monotonic()
            self.assertEqual(self._run_shared(store, run_id, hub, budget=30), 0)
            elapsed = time.monotonic() - start
        record = store.get(run_id)
        self.assertEqual(record["execution"], "completed")
        self.assertIsNone(record.get("error"))
        self.assertLess(elapsed, 30)
        self.assertEqual(len(hub.sent), 1)

    def test_recover_observes_without_resending_or_creating(self):
        store = self.make_store()
        session = self._session(None)
        hub = _FakeHub(session, runtime=_StubRuntime([session]))
        run_id = self._submit(store, "k-recover", session)
        store.update(run_id, {"delivery": "input_submitted"})
        store.update(
            run_id, {"execution": "unknown", "error": control_worker._history_wait_error("native_history_missing")}
        )
        # ``run recover`` on an unknown run only restarts observation.
        store.update(run_id, {"execution": "running"})
        cap, pane, txn = self._mocks()
        with cap, pane, txn:
            start = time.monotonic()
            self.assertEqual(self._run_shared(store, run_id, hub, budget=4), 0)
            elapsed = time.monotonic() - start
        record = store.get(run_id)
        self.assertEqual(record["execution"], "unknown")
        self.assertEqual(record["delivery"], "input_submitted")
        self.assertEqual(record["error"]["code"], "native_history_missing")
        self.assertGreaterEqual(elapsed, 4)
        self.assertEqual(hub.prepared, [])
        self.assertEqual(hub.required, [])
        self.assertEqual(hub.sent, [])
        self.assertEqual(hub.interrupted, [])
        self.assertEqual(hub.answered, [])

    def test_run_recover_keeps_unknown_recoverable_without_touching_delivery(self):
        store = self.make_store()
        session = self._session(None)
        run_id = self._submit(store, "k-recover-cli", session)
        store.update(run_id, {"delivery": "input_submitted"})
        store.update(
            run_id, {"execution": "unknown", "error": control_worker._history_wait_error("native_history_missing")}
        )
        with (
            mock.patch.object(control_cli, "RunStore", return_value=store),
            mock.patch.object(control_cli, "_supervisor_alive", return_value=False),
            mock.patch.object(control_cli, "_spawn_supervisor", return_value=True) as spawn,
        ):
            result = control_cli.cmd_recover(SimpleNamespace(run_id=run_id), None)
        self.assertEqual(result["data"]["status"], "recovered")
        record = store.get(run_id)
        self.assertEqual(record["execution"], "running")
        self.assertEqual(record["delivery"], "input_submitted")
        self.assertEqual(record["error"]["code"], "native_history_missing")
        spawn.assert_called_once_with(store, run_id)

    def test_valid_long_bound_task_is_never_timed_out_for_duration(self):
        store = self.make_store()
        path = str(Path(self.tmp.name) / "h-long.jsonl")

        def _persist_wire(text):
            _append_fixture_row(path, {"type": "user", "cwd": self.project, "message": {"content": text}})

        session = self._session(path)
        hub = _FakeHub(session, runtime=_StubRuntime([session]))
        hub.append_on_send = _persist_wire
        run_id = self._submit(store, "k-long", session, on_busy="steer")
        cap, pane, txn = self._mocks()
        phase_cell = {"phase": "working"}
        with (
            cap,
            pane,
            txn,
            mock.patch.object(control_worker, "_phase", side_effect=lambda session: phase_cell["phase"]),
        ):
            thread, outcome = self._run_thread(store, run_id, hub, budget=5, settle_quiet=2)
            self._wait_for(
                lambda: store.get(run_id).get("delivery") == "native_recorded",
                timeout=60,
                what="native recording of the long task",
            )
            time.sleep(8)  # well past the history budget: duration alone never settles
            record = store.get(run_id)
            self.assertEqual(record["execution"], "running")
            self.assertIsNone(record.get("error"))
            self.assertEqual(record["delivery"], "native_recorded")
            _append_fixture_row(
                path,
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "long done"}]}},
            )
            phase_cell["phase"] = "idle"
            thread.join(timeout=120)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", outcome)
        record = store.get(run_id)
        self.assertEqual(record["execution"], "completed")
        self.assertEqual(len(hub.sent), 1)


if __name__ == "__main__":
    unittest.main()
