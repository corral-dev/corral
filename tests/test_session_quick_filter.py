"""Shared three-client quick-filter cycle, using the existing host predicates."""

import unittest

from corral.ui.nav import NavState, SessionQuickFilter


class SessionQuickFilterTests(unittest.TestCase):
    def test_repeated_cycle_and_labels(self):
        mode = SessionQuickFilter.ALL
        for _ in range(3):
            self.assertEqual(mode, SessionQuickFilter.ALL)
            self.assertEqual(mode.label_key, "filter.attention")
            mode = mode.next
            self.assertEqual(mode, SessionQuickFilter.FOCUS)
            self.assertEqual(mode.label_key, "filter.attention")
            mode = mode.next
            self.assertEqual(mode, SessionQuickFilter.ACTIVE)
            self.assertEqual(mode.label_key, "filter.active")
            mode = mode.next
        self.assertEqual(mode, SessionQuickFilter.ALL)

    def test_membership_uses_activity_not_unread_or_open_process(self):
        now = 10_000_000.0
        sessions = [
            {"id": "waiting", "attention_kind": "waiting", "mtime": now - 100_000},
            {"id": "working", "attention_kind": "working", "mtime": now - 100_000},
            {"id": "recent", "keepalive_name": "fixture", "mtime": now - 20},
            {"id": "unread", "attention_kind": "unread", "mtime": now - 3_600},
            {"id": "ended", "status_tag": "done", "completion_id": "complete", "mtime": now - 60},
            {"id": "opened", "viewed_at": now - 60, "mtime": now - 100_000},
            {"id": "idle", "keepalive_name": "fixture", "mtime": now - 100_000},
            {"id": "stale-unread", "attention_kind": "unread", "mtime": now - 9 * 86_400},
        ]
        self.assertEqual([s["id"] for s in sessions if SessionQuickFilter.FOCUS.matches(s, now=now)], [
            "waiting", "working", "recent", "unread", "ended", "opened",
        ])
        self.assertEqual([s["id"] for s in sessions if SessionQuickFilter.ACTIVE.matches(s, now=now)], [
            "waiting", "working", "recent",
        ])
        self.assertTrue(all(SessionQuickFilter.ALL.matches(s, now=now) for s in sessions))
        self.assertFalse(SessionQuickFilter.ACTIVE.matches(sessions[2], now=now + 181))

    def test_mode_is_the_only_quick_filter_state(self):
        nav = NavState("claude")
        self.assertFalse(nav.narrowing)
        for mode in (SessionQuickFilter.FOCUS, SessionQuickFilter.ACTIVE):
            nav.quick_filter = mode
            self.assertTrue(nav.narrowing)
        nav.quick_filter = SessionQuickFilter.ALL
        self.assertFalse(nav.narrowing)
        nav.runtime_filters.add("codex")
        self.assertTrue(nav.narrowing)
