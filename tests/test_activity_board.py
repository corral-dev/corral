"""活跃标记：侧栏圆点与「活跃」快捷筛选的共用判定。"""

from __future__ import annotations

import time
import unittest

from corral.activity_board import (
    active_marker_style,
    resolve_active_marker,
)


class ResolveActiveMarkerTests(unittest.TestCase):
    """圆点与活跃筛选同源，共用 resolve_active_marker。"""

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
