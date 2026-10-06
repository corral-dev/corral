"""活跃会话看板：成员资格、当前页冻结、翻页与侧栏文案。"""

from __future__ import annotations

import time
import unittest

from corral import i18n
from corral.activity_board import (
    RECENT_ACTIVE_SECONDS,
    ActivityBoard,
    BoardCandidate,
    BoardSnapshot,
    active_marker_style,
    collect_candidates,
    collect_hosted_keys,
    resolve_active_marker,
)
from corral.attention import AttentionState
from corral.split_layout import MAX_PANES
from corral.ui.session_list import (
    ActivityBoardCard,
    activity_board_label,
    activity_board_pager_text,
    layout_activity_board_row,
)


def _cand(key: str, kind: str, updated_at: float = 0.0) -> BoardCandidate:
    return BoardCandidate(key=key, kind=kind, updated_at=updated_at)  # type: ignore[arg-type]


class ResolveActiveMarkerTests(unittest.TestCase):
    """圆点必须跟上 Active sessions，共用 resolve_active_marker。"""

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


class CollectCandidatesTests(unittest.TestCase):
    def test_interrupted_host_is_not_a_recent_board_candidate(self) -> None:
        from sesskit.titles import STATUS_ABORTED

        now = time.time()

        class _Store:
            def all_sessions(self):
                return [{
                    "source": "claude", "id": "limited", "keepalive_name": "k1",
                    "status_tag": STATUS_ABORTED, "mtime": now, "live": True,
                }]

            def attention_for(self, key):
                return AttentionState(kind="none")

        self.assertEqual(collect_candidates(_Store(), now=now), [])

    def test_only_hosted_waiting_working_unread(self) -> None:
        class _Store:
            def all_sessions(self):
                return [
                    {"source": "claude", "id": "wait", "keepalive_name": "k1"},
                    {"source": "claude", "id": "work", "keepalive_name": "k2"},
                    {"source": "claude", "id": "unread", "keepalive_name": "k3"},
                    {"source": "claude", "id": "idle", "keepalive_name": "k4"},
                    {"source": "claude", "id": "external", "live": True},
                    {"source": "shell", "id": "term", "keepalive_name": "k5"},
                ]

            def attention_for(self, key: str) -> AttentionState:
                kind = {
                    "claude:wait": "waiting",
                    "claude:work": "working",
                    "claude:unread": "unread",
                    "claude:idle": "none",
                    "claude:external": "waiting",
                    "shell:term": "working",
                }.get(key, "none")
                return AttentionState(kind=kind)  # type: ignore[arg-type]

        keys = [item.key for item in collect_candidates(_Store())]
        self.assertEqual(keys, ["claude:wait", "claude:work", "claude:unread"])

    def test_recently_active_hosted_session_joins_board(self) -> None:
        """「刚刚」窗口内还在动的无信号托管会话也算活跃成员。"""
        now = time.time()

        class _Store:
            def all_sessions(self):
                return [
                    {
                        "source": "claude", "id": "fresh", "keepalive_name": "k1",
                        "mtime": now - 60,
                    },
                    {
                        "source": "claude", "id": "future", "keepalive_name": "k4",
                        "mtime": now + 30,
                    },
                    {
                        "source": "claude", "id": "stale", "keepalive_name": "k2",
                        "mtime": now - RECENT_ACTIVE_SECONDS - 60,
                    },
                    {
                        "source": "claude", "id": "nomtime", "keepalive_name": "k3",
                    },
                    # 刚活跃但不是本窗口托管：不进格。
                    {"source": "claude", "id": "freshext", "mtime": now - 10},
                ]

            def attention_for(self, key: str) -> AttentionState:
                return AttentionState(kind="none")

        keys = [item.key for item in collect_candidates(_Store())]
        self.assertEqual(keys, ["claude:future", "claude:fresh"])

    def test_recent_member_ranks_after_unread(self) -> None:
        now = time.time()

        class _Store:
            def all_sessions(self):
                return [
                    {
                        "source": "claude", "id": "fresh", "keepalive_name": "k1",
                        "mtime": now,
                    },
                    {"source": "claude", "id": "unread", "keepalive_name": "k2"},
                ]

            def attention_for(self, key: str) -> AttentionState:
                kind = "unread" if key == "claude:unread" else "none"
                return AttentionState(kind=kind)  # type: ignore[arg-type]

        items = collect_candidates(_Store())
        self.assertEqual(
            [(item.key, item.kind) for item in items],
            [("claude:unread", "unread"), ("claude:fresh", "recent")],
        )


class CollectHostedKeysTests(unittest.TestCase):
    def test_hosted_keys_cover_only_hosted_non_shell_sessions(self) -> None:
        class _Store:
            def all_sessions(self):
                return [
                    {"source": "claude", "id": "a", "keepalive_name": "k1"},
                    {"source": "claude", "id": "ended"},
                    {"source": "codex", "id": "b", "keepalive_name": "k2"},
                    {"source": "shell", "id": "term", "keepalive_name": "k3"},
                ]

            def attention_for(self, key: str) -> AttentionState:
                return AttentionState(kind="none")

        self.assertEqual(
            collect_hosted_keys(_Store()), {"claude:a", "codex:b"}
        )


class ActivityBoardSyncTests(unittest.TestCase):
    def test_first_sync_takes_priority_prefix(self) -> None:
        board = ActivityBoard()
        snap = board.sync([
            _cand("w", "waiting", 3),
            _cand("g", "working", 2),
            _cand("r", "unread", 1),
        ])
        self.assertEqual(snap.keys, ("w", "g", "r"))
        self.assertEqual(snap.page, 0)
        self.assertEqual(snap.total, 3)
        self.assertEqual(snap.waiting_off_page, 0)

    def test_new_urgent_does_not_bump_full_page(self) -> None:
        board = ActivityBoard()
        first = [_cand(f"s{i}", "working", 10 - i) for i in range(MAX_PANES)]
        board.sync(first)
        later = [_cand("urgent", "waiting", 99), *first]
        snap = board.sync(later)
        self.assertNotIn("urgent", snap.keys)
        self.assertEqual(snap.waiting_off_page, 1)
        self.assertEqual(snap.total, MAX_PANES + 1)
        self.assertEqual(snap.page_count, 2)

    def test_empty_slot_fills_after_member_no_longer_hosted(self) -> None:
        """不够格但仍托管：钉住；会话结束（不再托管）后让位补意件。"""
        board = ActivityBoard()
        first = [_cand(f"s{i}", "working", 10 - i) for i in range(MAX_PANES)]
        hosted = {f"s{i}" for i in range(MAX_PANES)}
        board.sync(first, hosted_keys=hosted)
        remaining = first[1:]
        overflow = [_cand("urgent", "waiting", 99), *remaining]
        still = board.sync(overflow, hosted_keys=hosted)
        self.assertIn("s0", still.keys)
        self.assertNotIn("urgent", still.keys)
        gone = board.sync(overflow, hosted_keys=hosted - {"s0"})
        self.assertIn("urgent", gone.keys)
        self.assertNotIn("s0", gone.keys)
        self.assertEqual(len(gone.keys), MAX_PANES)

    def test_typing_pane_stays_when_no_longer_eligible(self) -> None:
        board = ActivityBoard()
        board.sync([_cand("a", "working"), _cand("b", "working")])
        board.set_typing_key("a")
        snap = board.sync([_cand("b", "working"), _cand("c", "waiting")])
        self.assertIn("a", snap.keys)
        self.assertIn("c", snap.keys)

    def test_turn_page_slices_queue(self) -> None:
        board = ActivityBoard()
        items = [_cand(f"s{i}", "working", 20 - i) for i in range(MAX_PANES + 2)]
        board.sync(items)
        board.turn_page(1)
        snap = board.sync(items)
        self.assertEqual(snap.page, 1)
        self.assertEqual(list(snap.keys), [f"s{i}" for i in range(MAX_PANES, MAX_PANES + 2)])

    def test_turn_page_wraps_around(self) -> None:
        """末页再下一页回到首页，首页再上一页到末页。"""
        board = ActivityBoard()
        items = [_cand(f"s{i}", "working", 20 - i) for i in range(MAX_PANES + 2)]
        board.sync(items)
        board.turn_page(-1)
        snap = board.sync(items)
        self.assertEqual(snap.page, 1)
        self.assertEqual(
            list(snap.keys), [f"s{i}" for i in range(MAX_PANES, MAX_PANES + 2)]
        )
        board.turn_page(1)
        snap = board.sync(items)
        self.assertEqual(snap.page, 0)
        self.assertEqual(list(snap.keys), [f"s{i}" for i in range(MAX_PANES)])

    def test_later_page_does_not_pull_earlier_members_when_queue_shifts(self) -> None:
        board = ActivityBoard()
        first = [_cand(f"s{i}", "working", 20 - i) for i in range(MAX_PANES + 2)]
        board.sync(first)
        board.turn_page(1)
        shifted = [_cand("urgent", "waiting", 99), *first]
        snap = board.sync(shifted)
        self.assertEqual(snap.page, 1)
        self.assertEqual(list(snap.keys), [f"s{i}" for i in range(MAX_PANES, MAX_PANES + 2)])
        self.assertNotIn("urgent", snap.keys)
        self.assertNotIn("s0", snap.keys)

    def test_dismiss_skips_until_reset(self) -> None:
        board = ActivityBoard()
        board.sync([_cand("a", "waiting"), _cand("b", "working")])
        board.dismiss("a")
        snap = board.sync([_cand("a", "waiting"), _cand("b", "working")])
        self.assertEqual(snap.keys, ("b",))
        board.reset()
        snap = board.sync([_cand("a", "waiting"), _cand("b", "working")])
        self.assertEqual(snap.keys, ("a", "b"))

    def test_dismiss_while_typing_does_not_return(self) -> None:
        board = ActivityBoard()
        board.sync([_cand("a", "waiting"), _cand("b", "working")])
        board.set_typing_key("a")
        board.dismiss("a")
        board.set_typing_key("a")
        snap = board.sync([_cand("a", "waiting"), _cand("b", "working")])
        self.assertEqual(snap.keys, ("b",))
        self.assertNotIn("a", snap.keys)

    def test_empty_snapshot(self) -> None:
        board = ActivityBoard()
        snap = board.sync([])
        self.assertEqual(snap.keys, ())
        self.assertEqual(snap.page_count, 1)
        self.assertEqual(snap.total, 0)


class ActivityBoardComboTests(unittest.TestCase):
    """翻页 / dismiss / 打字钉住 / 队列收缩的组合场景。"""

    def test_turn_page_fill_only_from_current_page_start(self) -> None:
        """翻页后本页有空位时，只从当前页起点之后的队列补位。"""
        board = ActivityBoard()
        items = [_cand(f"s{i}", "working", 20 - i) for i in range(MAX_PANES + 2)]
        board.sync(items)
        board.turn_page(1)
        snap = board.sync(items)
        self.assertEqual(snap.page, 1)
        self.assertEqual(
            list(snap.keys), [f"s{i}" for i in range(MAX_PANES, MAX_PANES + 2)]
        )
        # 本页只有 2 个成员、还有空位，但前页成员不被拉回补位。
        for i in range(MAX_PANES):
            self.assertNotIn(f"s{i}", snap.keys)

    def test_turn_page_dismiss_member_stays_gone_and_front_page_not_backfilled(
        self,
    ) -> None:
        """翻页后 dismiss 当前页成员：下一次 sync 不再出现，前页也不回填。"""
        board = ActivityBoard()
        items = [_cand(f"s{i}", "working", 20 - i) for i in range(MAX_PANES + 2)]
        board.sync(items)
        board.turn_page(1)
        last = f"s{MAX_PANES + 1}"
        board.dismiss(last)
        snap = board.sync(items)
        self.assertEqual(snap.page, 1)
        self.assertNotIn(last, snap.keys)
        # 当前页只剩 1 个成员、空位更多，前页成员依然不被拉回。
        self.assertEqual(list(snap.keys), [f"s{MAX_PANES}"])
        for i in range(MAX_PANES):
            self.assertNotIn(f"s{i}", snap.keys)

    def test_typing_pin_replaces_last_when_page_full(self) -> None:
        """打字格不够格且当前页已满：顶掉末位补位成员。"""
        board = ActivityBoard()
        items = [_cand(f"s{i}", "working", 10 - i) for i in range(MAX_PANES)]
        board.sync(items)
        board.set_typing_key("z")
        snap = board.sync(items)
        self.assertEqual(snap.page, 0)
        self.assertEqual(len(snap.keys), MAX_PANES)
        self.assertEqual(snap.keys[-1], "z")
        self.assertNotIn(f"s{MAX_PANES - 1}", snap.keys)
        self.assertEqual(
            list(snap.keys[:-1]), [f"s{i}" for i in range(MAX_PANES - 1)]
        )

    def test_typing_pin_appends_when_page_not_full(self) -> None:
        """打字格不够格且当前页未满：直接追加到本页。"""
        board = ActivityBoard()
        board.sync([_cand("b", "working"), _cand("c", "working")])
        board.set_typing_key("a")
        snap = board.sync([_cand("b", "working"), _cand("c", "working")])
        self.assertEqual(snap.keys, ("b", "c", "a"))
        self.assertEqual(snap.total, 2)

    def test_queue_shrink_pulls_page_back_after_held_members_leave(self) -> None:
        """翻到末页后队列整体收缩：钉住成员仍在时页码不动，托管结束后回退。"""
        board = ActivityBoard()
        items = [_cand(f"s{i}", "working", 20 - i) for i in range(MAX_PANES + 2)]
        hosted = {f"s{i}" for i in range(MAX_PANES + 2)}
        board.sync(items, hosted_keys=hosted)
        board.turn_page(1)
        self.assertEqual(board.sync(items, hosted_keys=hosted).page, 1)
        shrunk = [_cand("s0", "working", 20)]
        lingering = board.sync(shrunk, hosted_keys=hosted)
        self.assertEqual(lingering.page, 1)
        for i in range(MAX_PANES, MAX_PANES + 2):
            self.assertIn(f"s{i}", lingering.keys)
        snap = board.sync(shrunk, hosted_keys=set())
        self.assertEqual(snap.page, 0)
        self.assertEqual(snap.page_count, 1)
        self.assertEqual(snap.keys, ("s0",))
        for i in range(1, MAX_PANES + 2):
            self.assertNotIn(f"s{i}", snap.keys)
        self.assertLess(snap.page, snap.page_count)


class ActivityBoardHoldTests(unittest.TestCase):
    """观看期间不主动撤格：仍托管即钉住，直到离开 / 关格 / 不再托管。"""

    def test_finished_member_stays_while_still_hosted(self) -> None:
        board = ActivityBoard()
        board.sync([_cand("a", "working"), _cand("b", "working")], hosted_keys={"a", "b"})
        snap = board.sync([_cand("b", "working")], hosted_keys={"a", "b"})
        self.assertEqual(snap.keys, ("a", "b"))
        self.assertEqual(snap.total, 2)

    def test_held_member_drops_once_no_longer_hosted(self) -> None:
        board = ActivityBoard()
        board.sync([_cand("a", "working"), _cand("b", "working")], hosted_keys={"a", "b"})
        snap = board.sync([_cand("b", "working")], hosted_keys={"b"})
        self.assertEqual(snap.keys, ("b",))
        self.assertEqual(snap.total, 1)

    def test_held_member_keeps_page_when_queue_shrinks(self) -> None:
        """被钉住的末页成员让页码不越界回退，不因够格队列缩短被打回第 1 页。"""
        board = ActivityBoard()
        items = [_cand(f"s{i}", "working", 20 - i) for i in range(MAX_PANES + 1)]
        hosted = {f"s{i}" for i in range(MAX_PANES + 1)}
        board.sync(items, hosted_keys=hosted)
        board.turn_page(1)
        snap = board.sync([_cand("s0", "working", 20)], hosted_keys=hosted)
        self.assertEqual(snap.page, 1)
        self.assertIn(f"s{MAX_PANES}", snap.keys)

    def test_dismiss_skips_hold(self) -> None:
        board = ActivityBoard()
        board.sync([_cand("a", "waiting"), _cand("b", "working")], hosted_keys={"a", "b"})
        board.dismiss("a")
        snap = board.sync([_cand("b", "working")], hosted_keys={"a", "b"})
        self.assertEqual(snap.keys, ("b",))
        self.assertNotIn("a", snap.keys)

    def test_typing_pin_survives_after_session_unhosted(self) -> None:
        """正在打字的那格即使会话已结束也钉住，直到焦点离开这一格。"""
        board = ActivityBoard()
        board.sync([_cand("a", "working"), _cand("b", "working")], hosted_keys={"a", "b"})
        board.set_typing_key("a")
        still = board.sync([_cand("b", "working")], hosted_keys={"b"})
        self.assertIn("a", still.keys)
        board.set_typing_key(None)
        gone = board.sync([_cand("b", "working")], hosted_keys={"b"})
        self.assertNotIn("a", gone.keys)
        self.assertEqual(gone.keys, ("b",))

    def test_turn_page_releases_held_members(self) -> None:
        """显式翻页按当时队列重切：被钉住但已不在队列里的成员随之让位。"""
        board = ActivityBoard()
        board.sync([_cand("a", "working"), _cand("b", "working")], hosted_keys={"a", "b"})
        held = board.sync([_cand("b", "working"), _cand("c", "waiting")], hosted_keys={"a", "b", "c"})
        self.assertEqual(held.keys, ("a", "b", "c"))
        board.turn_page(1)
        snap = board.sync(
            [_cand("b", "working"), _cand("c", "waiting")],
            hosted_keys={"a", "b", "c"},
        )
        self.assertNotIn("a", snap.keys)
        self.assertEqual(snap.keys, ("b", "c"))


class FocusedBoardSessionKeyTests(unittest.TestCase):
    def test_dead_ended_pane_still_pins_on_board(self) -> None:
        from corral.ui.session_list import (
            _focused_board_session_key,
            _focused_live_session_key,
        )

        class _Spec:
            def __init__(self) -> None:
                self.session_key = "claude:done"
                self.keepalive_name = None

        class _Cell:
            def __init__(self) -> None:
                self.spec = _Spec()
                self.parent = None

        class _Embed:
            def __init__(self) -> None:
                self.dead = True
                self.parent = _Cell()

        embed = _Embed()
        self.assertIsNone(_focused_live_session_key(embed))
        self.assertEqual(_focused_board_session_key(embed), "claude:done")

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


class ActivityBoardLabelTests(unittest.TestCase):
    def tearDown(self) -> None:
        i18n.set_lang("en")

    def test_empty_is_name_only(self) -> None:
        i18n.set_lang("en")
        self.assertEqual(activity_board_label(None), "Active sessions")
        empty = BoardSnapshot(
            keys=(), page=0, page_count=1, total=0, waiting_off_page=0, waiting_total=0
        )
        self.assertEqual(activity_board_label(empty), "Active sessions")
        i18n.set_lang("zh")
        self.assertEqual(activity_board_label(None), "活跃会话")

    def test_shows_session_count_not_page_numbers_on_single_page(self) -> None:
        i18n.set_lang("en")
        one = BoardSnapshot(
            keys=("a",), page=0, page_count=1, total=1, waiting_off_page=0, waiting_total=0
        )
        self.assertEqual(activity_board_label(one), "Active sessions  ·  1 session")
        self.assertEqual(activity_board_pager_text(one), "")
        many = BoardSnapshot(
            keys=("a", "b", "c", "d"),
            page=1,
            page_count=2,
            total=5,
            waiting_off_page=1,
            waiting_total=2,
        )
        label = activity_board_label(many)
        self.assertEqual(label, "Active sessions  ·  5 sessions")
        self.assertNotIn("/", label)
        self.assertEqual(activity_board_pager_text(many), "2/2")
        i18n.set_lang("zh")
        self.assertEqual(activity_board_label(one), "活跃会话  ·  1 个会话")
        self.assertEqual(activity_board_label(many), "活跃会话  ·  5 个会话")

    def test_pager_is_second_row_and_clickable_when_multi_page(self) -> None:
        snap = BoardSnapshot(
            keys=("a", "b", "c", "d"),
            page=0,
            page_count=2,
            total=5,
            waiting_off_page=1,
            waiting_total=2,
        )
        i18n.set_lang("en")
        layout = layout_activity_board_row(snap, 39)
        self.assertTrue(layout.show_pager)
        self.assertEqual(layout.prev_label, "Prev page")
        self.assertEqual(layout.next_label, "Next page")
        self.assertEqual(layout.page_text, "1/2")
        self.assertEqual(layout.hit(layout.prev_start, 1), -1)
        self.assertEqual(layout.hit(layout.next_start, 1), 1)
        self.assertEqual(layout.hit(layout.page_start, 1), 0)
        self.assertIsNone(layout.hit(0, 0))
        self.assertIsNone(layout.hit(layout.prev_start, 2))
        i18n.set_lang("zh")
        zh = layout_activity_board_row(snap, 39)
        self.assertEqual(zh.prev_label, "上一页")
        self.assertEqual(zh.next_label, "下一页")

    def test_card_keeps_off_page_waiting_dot(self) -> None:
        class _Owner:
            board_snapshot = BoardSnapshot(
                keys=("a", "b", "c", "d"),
                page=0,
                page_count=2,
                total=5,
                waiting_off_page=1,
                waiting_total=2,
            )

        i18n.set_lang("en")
        card = ActivityBoardCard(_Owner())  # type: ignore[arg-type]
        plain = card.render().plain
        lines = plain.split("\n")
        self.assertIn("Active sessions  ·  5 sessions", lines[0])
        self.assertIn("●", lines[0])
        self.assertIn("Prev page", lines[1])
        self.assertIn("Next page", lines[1])
        self.assertIn("1/2", lines[1])
        self.assertNotIn("[1/2]", plain)
        self.assertEqual(lines[2], "")


if __name__ == "__main__":
    unittest.main()
