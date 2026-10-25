"""Abort settle window: retryable/unknown aborts notify only after persistence."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from sesskit import titles as sesskit_titles

from corral import split_layout
from corral.remote import sessions as remote_sessions
from corral.remote.sessions import SessionHub


def _aborted_session(
    *,
    cid: str = "c1",
    retryable: object = True,
    mtime: float = 1_700_000_000.0,
) -> dict:
    session = {
        "source": "pi",
        "id": "s1",
        "short_id": "s1",
        "cwd": "/tmp/proj",
        "cwd_display": "/tmp/proj",
        "mtime": mtime,
        "display_time": "01-01 12:00",
        "size_kb": 1.0,
        "status_tag": sesskit_titles.STATUS_ABORTED,
        "live": False,
        "keepalive_name": None,
        "fallback_title": "probe",
        "attention_kind": "none",
        "last_user_msg": "ping",
        "last_agent_msg": "Request timed out.",
        "path": "/tmp/hist.jsonl",
        "completion_id": cid,
    }
    if retryable is not None:
        session["terminal_error_retryable"] = retryable
    return session


class AbortSettleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"CORRAL_CACHE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.hub = SessionHub(scan_limit=10)
        self.hub.layout_db = split_layout.SidebarLayoutDB()
        self.addCleanup(self.hub.stop)
        self.events: list[tuple[str, dict]] = []
        self.hub._on_event = lambda channel, data: self.events.append((channel, data))
        self.hub._sessions_watchers = 1
        self.hook = mock.Mock()
        self.hub.set_status_hook(self.hook)
        self.clock = 1_000.0
        self._time = mock.patch.object(
            remote_sessions.time, "time", side_effect=lambda: self.clock
        )
        self._time.start()
        self.addCleanup(self._time.stop)

    def _abort_hooks(self) -> list:
        return [
            call
            for call in self.hook.call_args_list
            if call.args[2] == sesskit_titles.STATUS_ABORTED
        ]

    def _sessions(self, session: dict) -> None:
        self.hub.store.sessions = {"pi": [session]}

    def _pending(self, session: dict) -> None:
        # Baseline scan, then enter ABORTED through a real transition.
        pending = dict(session, status_tag=sesskit_titles.STATUS_PENDING)
        self._sessions(pending)
        self.hub._detect_status_changes()
        self._sessions(session)
        self.hub._detect_status_changes()

    def test_classified_terminal_abort_notifies_immediately(self) -> None:
        self._pending(_aborted_session(retryable=False))
        self.assertEqual(self.hook.call_count, 1)
        self.assertEqual(self.events[0][1]["notification_kind"], "aborted")
        self.assertEqual(self.hub._pending_abort, {})

    def test_retryable_abort_waits_for_settle_window(self) -> None:
        self._pending(_aborted_session(retryable=True))
        self.assertEqual(self.hook.call_count, 0)
        self.assertEqual(self.events, [])
        self.clock += remote_sessions._ABORT_SETTLE_SECONDS - 1
        self.hub._detect_status_changes()
        self.assertEqual(self.hook.call_count, 0)
        self.clock += 1
        self.hub._detect_status_changes()
        self.assertEqual(self.hook.call_count, 1)
        self.assertEqual(self.events[-1][1]["notification_kind"], "transient")
        # Emitted once only; the identity stays terminal afterwards.
        self.clock += 10
        self.hub._detect_status_changes()
        self.assertEqual(self.hook.call_count, 1)

    def test_missing_classification_waits_as_aborted(self) -> None:
        self._pending(_aborted_session(retryable=None))
        self.assertEqual(self.hook.call_count, 0)
        self.clock += remote_sessions._ABORT_SETTLE_SECONDS
        self.hub._detect_status_changes()
        self.assertEqual(self.hook.call_count, 1)
        self.assertEqual(self.events[-1][1]["notification_kind"], "aborted")

    def test_recovery_before_window_cancels_notification(self) -> None:
        session = _aborted_session(retryable=True)
        self._pending(session)
        self.clock += 30
        self._sessions(dict(session, status_tag=sesskit_titles.STATUS_PENDING))
        self.hub._detect_status_changes()
        self.assertEqual(self._abort_hooks(), [])
        self.assertEqual(self.hub._pending_abort, {})
        self.clock += remote_sessions._ABORT_SETTLE_SECONDS
        self.hub._detect_status_changes()
        self.assertEqual(self._abort_hooks(), [])

    def test_new_abort_round_restarts_the_window(self) -> None:
        session = _aborted_session(retryable=True, cid="c1")
        self._pending(session)
        self.clock += 40
        self._sessions(dict(session, completion_id="c2"))
        self.hub._detect_status_changes()
        self.clock += 40
        self.hub._detect_status_changes()
        self.assertEqual(self.hook.call_count, 0)
        self.clock += remote_sessions._ABORT_SETTLE_SECONDS - 40
        self.hub._detect_status_changes()
        self.assertEqual(self.hook.call_count, 1)

    def test_stale_abort_at_startup_is_not_scheduled(self) -> None:
        stale = self.clock - remote_sessions._STATUS_NOTIFY_FRESH_SECONDS - 1
        self._sessions(_aborted_session(retryable=True, mtime=stale))
        self.hub._detect_status_changes()
        self.assertEqual(self.hook.call_count, 0)
        self.assertEqual(self.hub._pending_abort, {})


if __name__ == "__main__":
    unittest.main()
