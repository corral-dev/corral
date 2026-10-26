"""侧栏圆点与「关注」快捷筛选的共用判定。"""

from __future__ import annotations

import time
import unittest

from corral.activity_board import (
    ATTENTION_RECENT_SECONDS,
    ATTENTION_UNREAD_SECONDS,
    active_marker_style,
    needs_attention,
    resolve_active_marker,
)


class ResolveActiveMarkerTests(unittest.TestCase):
    """圆点判定；关注筛选在其上叠加未读与近期窗口。"""

    def test_unread_is_not_activity_even_when_hosted(self) -> None:
        session = {"attention_kind": "unread", "keepalive_name": "fixture", "mtime": time.time(),
                   "status_tag": "done", "completion_id": "finished"}
        self.assertEqual(resolve_active_marker(session), "unread")

    def test_recent_hosted_just_now_gets_green_dot(self) -> None:
        now = time.time()
        self.assertEqual(
            resolve_active_marker(
                attention_kind="none", hosted=True, mtime=now - 30, now=now
            ),
            "recent",
        )
        self.assertEqual(active_marker_style("recent"), "bold green")
        self.assertEqual(active_marker_style("recent"), active_marker_style("working"))
        self.assertIsNone(
            resolve_active_marker(
                attention_kind="none", hosted=False, mtime=now - 30, now=now
            )
        )

    def test_waiting_external_still_gets_yellow_dot(self) -> None:
        self.assertEqual(
            resolve_active_marker(attention_kind="waiting", hosted=False, mtime=0),
            "waiting",
        )

    def test_interruption_blocks_recent_fallback_but_allows_resumption(self) -> None:
        from sesskit.titles import STATUS_ABORTED

        now = time.time()
        session = {
            "status_tag": STATUS_ABORTED, "keepalive_name": "retained",
            "live": True, "mtime": now, "attention_kind": "none",
        }
        self.assertIsNone(resolve_active_marker(session, now=now))
        for kind in ("working", "waiting", "unread"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    resolve_active_marker(session, attention_kind=kind, now=now), kind
                )

    def test_weak_completion_without_identity_keeps_recent_policy(self) -> None:
        from sesskit.titles import STATUS_DONE

        now = time.time()
        self.assertEqual(resolve_active_marker({
            "status_tag": STATUS_DONE, "keepalive_name": "retained",
            "mtime": now, "attention_kind": "none",
        }, now=now), "recent")

    def test_native_completion_blocks_recent_fallback_but_allows_resumption(self) -> None:
        from sesskit.titles import STATUS_DONE

        now = time.time()
        session = {
            "status_tag": STATUS_DONE, "completion_id": "turn-1",
            "keepalive_name": "retained", "live": True, "mtime": now,
            "attention_kind": "none",
        }
        self.assertIsNone(resolve_active_marker(session, now=now))
        for kind in ("working", "waiting", "unread"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    resolve_active_marker(session, attention_kind=kind, now=now), kind
                )


class NeedsAttentionTests(unittest.TestCase):
    """Attention filter membership (owner decision 2026-10-26)."""

    def setUp(self) -> None:
        self.now = 1_800_000_000.0

    def _ended(self, age: float, **extra) -> dict:
        session = {"attention_kind": "none", "status_tag": "done", "completion_id": "turn",
                   "keepalive_name": "retained", "mtime": self.now - age}
        session.update(extra)
        return session

    def test_activity_dots_always_belong(self) -> None:
        for kind in ("working", "waiting"):
            with self.subTest(kind=kind):
                self.assertTrue(needs_attention(self._ended(86_400, attention_kind=kind), now=self.now))
        recent = {"attention_kind": "none", "keepalive_name": "fixture", "mtime": self.now - 30}
        self.assertTrue(needs_attention(recent, now=self.now))

    def test_unread_belongs_for_a_week(self) -> None:
        inside = self._ended(ATTENTION_UNREAD_SECONDS - 60, attention_kind="unread")
        outside = self._ended(ATTENTION_UNREAD_SECONDS + 60, attention_kind="unread")
        self.assertTrue(needs_attention(inside, now=self.now))
        self.assertFalse(needs_attention(outside, now=self.now))

    def test_read_session_stays_half_an_hour_after_it_ended(self) -> None:
        self.assertTrue(needs_attention(self._ended(ATTENTION_RECENT_SECONDS - 60), now=self.now))
        self.assertFalse(needs_attention(self._ended(ATTENTION_RECENT_SECONDS + 60), now=self.now))

    def test_interrupted_and_external_sessions_use_the_same_window(self) -> None:
        from sesskit.titles import STATUS_ABORTED

        aborted = self._ended(600, status_tag=STATUS_ABORTED, completion_id="")
        external = {"attention_kind": "none", "mtime": self.now - 600}
        self.assertTrue(needs_attention(aborted, now=self.now))
        self.assertTrue(needs_attention(external, now=self.now))

    def test_open_restarts_the_window(self) -> None:
        old = self._ended(7_200)
        self.assertFalse(needs_attention(old, now=self.now))
        old["viewed_at"] = self.now - 300
        self.assertTrue(needs_attention(old, now=self.now))
        old["viewed_at"] = self.now - ATTENTION_RECENT_SECONDS - 60
        self.assertFalse(needs_attention(old, now=self.now))

    def test_missing_or_future_timestamps(self) -> None:
        self.assertFalse(needs_attention({"attention_kind": "none"}, now=self.now))
        self.assertTrue(needs_attention(self._ended(-120), now=self.now))


class FocusedLiveSessionKeyTests(unittest.TestCase):
    def test_live_pane_still_resolves_for_click_focus(self) -> None:
        from corral.ui.session_list import _focused_live_session_key

        class _Spec:
            def __init__(self) -> None:
                self.session_key = "claude:live"
                self.keepalive_name = "corral-live"

        class _Cell:
            def __init__(self) -> None:
                self.spec = _Spec()
                self.parent = None

        class _Embed:
            def __init__(self) -> None:
                self.dead = False
                self.parent = _Cell()

        self.assertEqual(_focused_live_session_key(_Embed()), "claude:live")


if __name__ == "__main__":
    unittest.main()
