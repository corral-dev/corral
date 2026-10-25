"""Bounded supervisor observation of runs without usable native history.

Deterministic fake-monotonic tests for the memory-audit slice: a submitted run
whose native history never becomes readable settles as a recoverable
``unknown`` instead of keeping a supervisor (and its peak heap) alive forever,
while a valid bound long-running agent is never timed out for duration. No real
tmux, native history, network or supervisor process is involved.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sesskit.models import ActivityEvent, Evidence, SessionOutcome

from corral import control, control_cli, control_worker
from corral.control_store import RunStore

_NATIVE = Evidence(origin="native")
_UNKNOWN = Evidence(origin="unknown")
_BOUND = {"source": "claude", "id": "one", "path": "/fixture/history.jsonl", "keepalive_name": None}


class _LoopLimit(RuntimeError):
    """The test harness stopped an intentionally endless supervisor loop."""


class _FakeClock:
    """Monotonic clock advanced only by the supervisor's own sleeps."""

    def __init__(self, base: float = 1000.0, limit: float | None = None):
        self.now = base
        self.started = base
        self.limit = limit
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += max(0.0, float(seconds))
        if self.limit is not None and self.now - self.started >= self.limit:
            raise _LoopLimit
        if self.sleeps > 5000:
            raise AssertionError("supervisor observation loop never settled")


class _Reader:
    def __init__(self, poll):
        self._poll = poll

    def poll(self):
        return self._poll()


def _poll(*, state: str = "available", events=(), reset: bool = False):
    return SimpleNamespace(events=tuple(events), reset=reset, generation="1", cursor=None,
                           outcome=SessionOutcome("unknown", _UNKNOWN), state=state)


def _working_events(run_id: str) -> list[ActivityEvent]:
    """This run's marker plus non-final agent work: a live, unfinished turn."""
    return [
        ActivityEvent(seq=1, type="user_message", evidence=_NATIVE, origin="human",
                      text=control.wire_prompt(run_id, "initial", "fix it")),
        ActivityEvent(seq=2, type="thinking", evidence=_NATIVE, text="still working"),
    ]


def _completed_events(run_id: str) -> list[ActivityEvent]:
    """This run's marker plus native turn completion: settles at once."""
    return [
        *_working_events(run_id),
        ActivityEvent(seq=3, type="lifecycle", evidence=_NATIVE, text="turn complete"),
    ]


class HistoryBoundObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.count = 0

    def make(self, *, delivery: str = "input_submitted", host: str | None = None,
             session_ref: str | None = None):
        """One run plus a supervisor whose hub is a pure mock (no store/tmux scan)."""
        self.count += 1
        store = RunStore(Path(self.tmp.name) / f"control-{self.count}")
        cwd = str(Path(self.tmp.name) / "project")
        spec = {"mode": "create", "runtime": "claude", "session_ref": session_ref, "cwd": cwd,
                "prompt": "fix it", "on_busy": "steer", "queue_timeout": 0}
        initial = {"mode": "create", "runtime": "claude", "session_ref": session_ref, "host": host,
                   "cwd": cwd, "delivery": delivery, "execution": "running", "created_at": time.time()}
        run, _ = store.create("agent:a", f"k{self.count}", spec, initial)
        hub = mock.Mock()
        hub.registry.get.return_value = SimpleNamespace(id="claude")
        hub.store.find_session.return_value = None
        hub.resolve_session_key.side_effect = lambda key: key
        supervisor = control_worker.Supervisor(store, run["run_id"], hub)
        self.addCleanup(supervisor.close)
        return store, run["run_id"], hub, supervisor

    @staticmethod
    def _time(clock):
        return mock.patch.object(control_worker, "time", SimpleNamespace(
            monotonic=clock.monotonic, sleep=clock.sleep, time=time.time))

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
                store, run_id, hub, supervisor = self.make(host=f"corral-claude-{liveness}")
                clock = _FakeClock()
                pane = mock.Mock(return_value=False)
                with self._time(clock), \
                        mock.patch.object(supervisor, "pane_dead", pane), \
                        mock.patch.object(supervisor, "_bind_native", return_value=False) as bind:
                    start = clock.now
                    supervisor.observe(-1)
                record = store.get(run_id)
                self.assertEqual(record["execution"], "unknown")
                self.assertEqual(record["delivery"], "input_submitted")  # no replay, no fake rejection
                self.assertEqual(record["error"]["code"], "native_history_missing")
                self.assertIn("not judged dead", record["error"]["message"])
                elapsed = clock.now - start
                self.assertGreaterEqual(elapsed, control_worker._HISTORY_WAIT_TIMEOUT)
                self.assertLess(elapsed, control_worker._HISTORY_WAIT_TIMEOUT + 5)
                self.assertGreater(bind.call_count, 1)  # bounded retries, not one blind wait
                self.assertGreaterEqual(pane.call_count, 1)
                hub.send_text.assert_not_called()
                hub.prepare_text_delivery.assert_not_called()
                hub.interrupt_session.assert_not_called()
                self.assert_audited(store, run_id)

    def test_binding_before_the_deadline_restarts_the_wait(self):
        store, run_id, hub, supervisor = self.make()
        clock = _FakeClock(limit=400)
        start = clock.now
        bound: list[bool] = []

        def bind():
            if clock.now - start >= 140:
                supervisor.session = dict(_BOUND)
                bound.append(True)
                return True
            return False

        reader = _Reader(lambda: _poll(state="available", events=_working_events(run_id)))
        with self._time(clock), \
                mock.patch.object(supervisor, "pane_dead", return_value=False), \
                mock.patch.object(supervisor, "_bind_native", side_effect=bind), \
                mock.patch("sesskit.open_activity_reader", return_value=reader), \
                mock.patch.object(control_worker, "_phase", return_value="working"):
            with self.assertRaises(_LoopLimit):  # a valid long task keeps running past the harness limit
                supervisor.observe(-1)
        record = store.get(run_id)
        self.assertEqual(bound, [True])
        self.assertEqual(record["execution"], "running")
        self.assertIsNone(record.get("error"))
        self.assertEqual(record["delivery"], "native_recorded")
        self.assertGreater(clock.now - start, control_worker._HISTORY_WAIT_TIMEOUT + 100)
        hub.send_text.assert_not_called()

    def test_known_dead_pane_keeps_the_existing_terminal_exited_outcome(self):
        store, run_id, hub, supervisor = self.make()
        clock = _FakeClock()
        with self._time(clock), \
                mock.patch.object(supervisor, "pane_dead", return_value=True), \
                mock.patch.object(supervisor, "_bind_native", return_value=False) as bind:
            start = clock.now
            supervisor.observe(-1)
        record = store.get(run_id)
        self.assertEqual(record["execution"], "unknown")
        self.assertEqual(record["delivery"], "unknown")
        self.assertEqual(record["error"]["code"], "terminal_exited")
        self.assertLess(clock.now - start, control_worker._HISTORY_WAIT_TIMEOUT)
        bind.assert_not_called()  # positive death is checked before any budget
        hub.send_text.assert_not_called()

    def test_bound_reader_that_never_becomes_readable_settles_unknown(self):
        store, run_id, hub, supervisor = self.make()
        supervisor.session = dict(_BOUND)
        clock = _FakeClock()
        reader = _Reader(lambda: _poll(state="unavailable"))
        with self._time(clock), \
                mock.patch.object(supervisor, "pane_dead", return_value=False), \
                mock.patch("sesskit.open_activity_reader", return_value=reader), \
                mock.patch.object(control_worker, "_phase", return_value="unknown"):
            start = clock.now
            supervisor.observe(-1)
        record = store.get(run_id)
        self.assertEqual(record["execution"], "unknown")
        self.assertEqual(record["delivery"], "input_submitted")
        self.assertEqual(record["error"]["code"], "native_history_unavailable")
        self.assertIn("unreadable", record["error"]["message"])
        elapsed = clock.now - start
        self.assertGreaterEqual(elapsed, control_worker._HISTORY_WAIT_TIMEOUT)
        self.assertLess(elapsed, control_worker._HISTORY_WAIT_TIMEOUT + 5)
        hub.send_text.assert_not_called()
        hub.interrupt_session.assert_not_called()
        self.assert_audited(store, run_id)

    def test_real_sesskit_reader_on_a_missing_history_settles_unknown(self):
        # Same path without mocking SessKit: a real reader over a deleted/missing
        # history reports ``unavailable``, which is the product evidence for the
        # bound branch (still an isolated fixture, no live history is read).
        import sesskit

        store, run_id, hub, supervisor = self.make()
        missing = str(Path(self.tmp.name) / "gone.jsonl")
        supervisor.session = {**_BOUND, "path": missing}
        self.assertEqual(sesskit.open_activity_reader(supervisor.session).poll().state, "unavailable")
        clock = _FakeClock()
        with self._time(clock), \
                mock.patch.object(supervisor, "pane_dead", return_value=False), \
                mock.patch.object(control_worker, "_phase", return_value="unknown"):
            start = clock.now
            supervisor.observe(-1)
        record = store.get(run_id)
        self.assertEqual(record["execution"], "unknown")
        self.assertEqual(record["error"]["code"], "native_history_unavailable")
        self.assertGreaterEqual(clock.now - start, control_worker._HISTORY_WAIT_TIMEOUT)
        hub.send_text.assert_not_called()
        hub.interrupt_session.assert_not_called()

    def test_dead_pane_wins_over_an_unreadable_bound_history(self):
        store, run_id, hub, supervisor = self.make()
        supervisor.session = dict(_BOUND)
        clock = _FakeClock()
        reader = _Reader(lambda: _poll(state="unavailable"))
        with self._time(clock), \
                mock.patch.object(supervisor, "pane_dead", return_value=True), \
                mock.patch("sesskit.open_activity_reader", return_value=reader), \
                mock.patch.object(control_worker, "_phase", return_value="unknown"):
            start = clock.now
            supervisor.observe(-1)
        record = store.get(run_id)
        self.assertEqual(record["execution"], "unknown")
        self.assertEqual(record["error"]["code"], "terminal_exited")
        self.assertLess(clock.now - start, control_worker._HISTORY_WAIT_TIMEOUT)
        hub.send_text.assert_not_called()

    def test_transiently_unreadable_reader_recovers_and_settles_natively(self):
        store, run_id, hub, supervisor = self.make()
        supervisor.session = dict(_BOUND)
        clock = _FakeClock()
        start = clock.now
        reader = _Reader(lambda: _poll(state="unavailable") if clock.now - start < 100
                         else _poll(state="available", events=_completed_events(run_id)))
        with self._time(clock), \
                mock.patch.object(supervisor, "pane_dead", return_value=False), \
                mock.patch("sesskit.open_activity_reader", return_value=reader), \
                mock.patch.object(control_worker, "_phase", return_value="idle"):
            supervisor.observe(-1)
        record = store.get(run_id)
        self.assertEqual(record["execution"], "completed")
        self.assertEqual(record["finality"], "native")
        self.assertIsNone(record.get("error"))
        self.assertGreaterEqual(clock.now - start, 100)
        self.assertLess(clock.now - start, control_worker._HISTORY_WAIT_TIMEOUT)
        hub.send_text.assert_not_called()

    def test_external_settlement_wins_over_the_history_budget(self):
        store, run_id, hub, supervisor = self.make()
        clock = _FakeClock()
        start = clock.now

        def bind():
            if clock.now - start >= 30:
                store.update(run_id, {"execution": "completed"})
            return False

        with self._time(clock), \
                mock.patch.object(supervisor, "pane_dead", return_value=False), \
                mock.patch.object(supervisor, "_bind_native", side_effect=bind):
            supervisor.observe(-1)
        record = store.get(run_id)
        self.assertEqual(record["execution"], "completed")
        self.assertIsNone(record.get("error"))
        self.assertLess(clock.now - start, control_worker._HISTORY_WAIT_TIMEOUT)
        hub.send_text.assert_not_called()

    def test_recover_observes_without_resending_or_creating(self):
        store, run_id, hub, supervisor = self.make()
        store.update(run_id, {"execution": "unknown",
                              "error": control_worker._history_wait_error("native_history_missing")})
        # ``run recover`` on an unknown run only restarts observation.
        store.update(run_id, {"execution": "running"})
        clock = _FakeClock()
        with self._time(clock), \
                mock.patch.object(supervisor, "pane_dead", return_value=False), \
                mock.patch.object(supervisor, "_bind_native", return_value=False):
            start = clock.now
            supervisor.run()
        record = store.get(run_id)
        self.assertEqual(record["execution"], "unknown")
        self.assertEqual(record["delivery"], "input_submitted")
        self.assertEqual(record["error"]["code"], "native_history_missing")
        self.assertGreaterEqual(clock.now - start, control_worker._HISTORY_WAIT_TIMEOUT)
        for method in ("send_text", "prepare_text_delivery", "host_planned_session", "require_session"):
            getattr(hub, method).assert_not_called()
        hub.interrupt_session.assert_not_called()
        hub.answer_question.assert_not_called()

    def test_run_recover_keeps_unknown_recoverable_without_touching_delivery(self):
        store, run_id, _hub, _supervisor = self.make()
        store.update(run_id, {"execution": "unknown",
                              "error": control_worker._history_wait_error("native_history_missing")})
        with mock.patch.object(control_cli, "RunStore", return_value=store), \
                mock.patch.object(control_cli, "_supervisor_alive", return_value=False), \
                mock.patch.object(control_cli, "_spawn_supervisor", return_value=True) as spawn:
            result = control_cli.cmd_recover(SimpleNamespace(run_id=run_id), None)
        self.assertEqual(result["data"]["status"], "recovered")
        record = store.get(run_id)
        self.assertEqual(record["execution"], "running")
        self.assertEqual(record["delivery"], "input_submitted")
        self.assertEqual(record["error"]["code"], "native_history_missing")
        spawn.assert_called_once_with(store, run_id)

    def test_valid_long_bound_task_is_never_timed_out_for_duration(self):
        store, run_id, hub, supervisor = self.make()
        supervisor.session = dict(_BOUND)
        clock = _FakeClock(limit=900)
        reader = _Reader(lambda: _poll(state="available", events=_working_events(run_id)))
        with self._time(clock), \
                mock.patch.object(supervisor, "pane_dead", return_value=False), \
                mock.patch("sesskit.open_activity_reader", return_value=reader), \
                mock.patch.object(control_worker, "_phase", return_value="working"):
            start = clock.now
            with self.assertRaises(_LoopLimit):
                supervisor.observe(-1)
        record = store.get(run_id)
        self.assertEqual(record["execution"], "running")
        self.assertIsNone(record.get("error"))
        self.assertEqual(record["delivery"], "native_recorded")
        self.assertGreaterEqual(clock.now - start, 900)
        hub.send_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
