"""Retained client visits must reconcile beyond list windows and pane lifetime."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

from test_remote_sessions import _session

from corral import split_layout
from corral.models import session_key
from corral.remote.sessions import ActionError, SessionHub


class RetainedSessionReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": scratch.name})
        env.start()
        self.addCleanup(env.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.hub = SessionHub(scan_limit=10)
        self.addCleanup(self.hub.stop)
        self.addCleanup(self.hub.layout_db.close)
        self.catalog = self.hub.store.search_catalog
        self.catalog.ready = True
        self.catalog._prepared_at = time.monotonic()
        self.hub.store.get_title = lambda row: row["fallback_title"]

    def test_restart_without_panes_or_aliases_retires_exact_orphans(self) -> None:
        row = _session(source="opencode", sid="ses_finished", title="Finished")
        self.hub.store.sessions = {"opencode": [row]}
        self.catalog._sessions = {session_key(row): row}
        first = self.hub.list_snapshot()
        keys = ["opencode:72e038c8", "opencode:3ffbe30a", "opencode:b0b148e2"]
        repaired = self.hub.list_snapshot(since_version=first["version"], retained_keys=keys)
        self.assertTrue(repaired["unchanged"])
        self.assertNotIn("sessions", repaired)
        self.assertEqual(repaired["missing_keys"], keys)
        self.assertEqual(repaired["retained_sessions"], [])
        self.assertEqual(repaired["aliases"], {})
        self.assertEqual(self.hub.store.find_session(session_key(row)), row)

    def test_old_real_history_outside_window_is_refreshed_not_retired(self) -> None:
        listed = _session(sid="today")
        old = _session(source="opencode", sid="ses_old", title="Old result", mtime=1)
        self.hub.store.sessions = {"claude": [listed]}
        self.catalog._sessions = {session_key(old): old}
        result = self.hub.list_snapshot(retained_keys=[session_key(old)])
        self.assertEqual([s["key"] for s in result["sessions"]], [session_key(listed)])
        self.assertEqual([s["key"] for s in result["retained_sessions"]], [session_key(old)])
        self.assertEqual(result["missing_keys"], [])
        self.assertFalse(result["retained_sessions"][0]["live"])
        self.assertEqual(result["retained_sessions"][0]["marker"], "")

    def test_empty_hosted_creation_is_preserved_and_promoted_exactly(self) -> None:
        key = "opencode:creation"
        row = self.hub.store.register_hosted_session(
            runtime_id="opencode", keepalive_name="corral-opencode-creation",
            title="New session", cwd="/tmp", ident="creation",
        )
        result = self.hub.list_snapshot(retained_keys=[key])
        self.assertEqual(result["missing_keys"], [])
        self.assertEqual(result["retained_sessions"][0]["key"], key)
        canonical = _session(source="opencode", sid="ses_real", title="Real result")
        canonical["keepalive_name"] = row["keepalive_name"]
        self.hub.store.sessions = {"opencode": [canonical]}
        result = self.hub.list_snapshot(retained_keys=[key, session_key(canonical)])
        self.assertEqual(result["aliases"][key], session_key(canonical))
        self.assertEqual(len(result["retained_sessions"]), 1)
        self.assertEqual(result["retained_sessions"][0]["title"], "Real result")
        self.assertEqual(result["missing_keys"], [])

    def test_incomplete_stale_or_failed_catalog_never_confirms_absence(self) -> None:
        for state in ("incomplete", "stale", "failed"):
            with self.subTest(state=state):
                self.catalog.ready = state != "incomplete"
                self.catalog.failed = state == "failed"
                self.catalog._prepared_at = time.monotonic() - (60 if state == "stale" else 0)
                self.hub._search_refresh.clear()
                result = self.hub.list_snapshot(retained_keys=["opencode:old"])
                self.assertEqual(result["missing_keys"], [])
                self.assertTrue(self.hub._search_refresh.is_set())

    def test_validation_is_bounded_and_never_silently_truncates(self) -> None:
        for value in ("not-an-array", [1], [""], ["x" * 257], [f"pi:{i}" for i in range(101)]):
            with self.subTest(value=str(value)[:50]), self.assertRaises(ActionError):
                self.hub.list_snapshot(retained_keys=value)
        result = self.hub.list_snapshot(retained_keys=["pi:old", "pi:old"])
        self.assertEqual(result["missing_keys"], ["pi:old"])
