"""会话关注状态测试；全部使用临时目录，不接触真实用户缓存。"""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from corral.attention import AttentionEvidence, AttentionStore


def _session(runtime: str, session_id: str, *, live: bool = False) -> dict:
    return {"source": runtime, "id": session_id, "live": live}


class AttentionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "attention.sqlite3"
        self.store = AttentionStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_views_are_recorded_separately_and_bounded(self):
        self.assertEqual(self.store.record_view("codex", "one", at=1_000.0), 1_000.0)
        # An older stamp never moves the open time backwards.
        self.store.record_view("codex", "one", at=900.0)
        self.store.record_view("pi", "two", at=1_500.0)
        self.assertEqual(
            self.store.recent_views(950.0),
            {("codex", "one"): 1_000.0, ("pi", "two"): 1_500.0},
        )
        self.assertEqual(self.store.recent_views(1_200.0), {("pi", "two"): 1_500.0})
        # Recording prunes rows older than a day.
        self.store.record_view("pi", "three", at=1_000.0 + 2 * 86_400)
        self.assertNotIn(("codex", "one"), self.store.recent_views(0.0))
        # Views never create or change unread state.
        self.assertEqual(self.store.mark_read("pi", "two").kind, "none")

    def test_state_priority_and_mark_read_only_clear_unread(self):
        states = self.store.reconcile(
            [_session("codex", "one")],
            {"codex:one": AttentionEvidence(activity_token="old", observed_at=1)},
        )
        self.assertEqual(states["codex:one"].kind, "none")

        state = self.store.record_event(
            "codex",
            "one",
            AttentionEvidence(
                phase="working", activity_token="reply-1", observed_at=2, source="observer"
            ),
        )
        self.assertEqual(state.kind, "working")
        self.assertEqual(self.store.mark_read("codex", "one").kind, "working")

        state = self.store.record_event(
            "codex",
            "one",
            AttentionEvidence(
                phase="waiting",
                activity_token="question-1",
                question_token="call-1",
                observed_at=3,
                source="observer",
            ),
        )
        self.assertEqual(state.kind, "waiting")
        self.assertEqual(self.store.mark_read("codex", "one").kind, "waiting")

        state = self.store.record_event(
            "codex",
            "one",
            AttentionEvidence(phase="idle", activity_token="final-1", observed_at=4),
        )
        self.assertEqual(state.kind, "unread")
        self.assertEqual(self.store.mark_read("codex", "one").kind, "none")

    def test_working_pairs_lists_only_working_phase(self):
        self.store.reconcile([_session("codex", "one"), _session("claude", "two")], {})
        self.store.record_event(
            "codex",
            "one",
            AttentionEvidence(
                phase="working", activity_token="w1", observed_at=2, source="observer"
            ),
        )
        self.store.record_event(
            "claude",
            "two",
            AttentionEvidence(
                phase="waiting",
                activity_token="q1",
                question_token="c1",
                observed_at=3,
                source="observer",
            ),
        )
        self.assertEqual(self.store.working_pairs(), [("codex", "one")])

    def test_waiting_requires_structured_question_token(self):
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        state = self.store.record_event(
            "claude",
            "one",
            AttentionEvidence(phase="waiting", activity_token="reply", observed_at=1),
        )
        self.assertEqual(state.kind, "unread")

    def test_first_reconcile_baselines_old_activity_but_later_session_is_unread(self):
        states = self.store.reconcile(
            [_session("claude", "old")],
            {"claude:old": AttentionEvidence(activity_token="old-reply", observed_at=1)},
        )
        self.assertEqual(states["claude:old"].kind, "none")
        state = self.store.record_event(
            "claude",
            "new",
            AttentionEvidence(activity_token="new-reply", observed_at=2),
        )
        self.assertEqual(state.kind, "unread")

    def test_newer_observer_is_not_overwritten_by_older_history(self):
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(phase="working", observed_at=20, source="observer"),
        )
        state = self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(phase="idle", observed_at=10, source="history"),
        )
        self.assertEqual(state.kind, "working")

        state = self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(phase="idle", observed_at=30, source="history"),
        )
        self.assertEqual(state.kind, "unread")

    def test_live_waiting_overrides_older_terminal_idle(self):
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(phase="idle", activity_token="terminal:20", observed_at=20),
        )
        states = self.store.reconcile(
            [_session("cursor", "one", live=True)],
            {
                "cursor:one": AttentionEvidence(
                    phase="waiting",
                    question_token="ask-1",
                    observed_at=10,
                    source="history",
                )
            },
        )
        self.assertEqual(states["cursor:one"].kind, "waiting")

    def test_observer_stop_does_not_clear_unanswered_waiting(self):
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(
                phase="waiting",
                question_token="ask-1",
                observed_at=1,
                source="history",
            ),
        )
        state = self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(
                phase="idle",
                activity_token="gen:stop",
                observed_at=2,
                source="observer",
            ),
        )
        self.assertEqual(state.kind, "waiting")
        state = self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(
                phase="idle",
                activity_token="gen:sessionEnd",
                observed_at=3,
                source="observer",
            ),
        )
        self.assertEqual(state.kind, "unread")

    def test_live_history_clears_waiting_to_working_when_question_gone(self):
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(
                phase="waiting",
                question_token="ask-1",
                observed_at=1,
                source="history",
            ),
        )
        states = self.store.reconcile(
            [_session("cursor", "one", live=True)],
            {"cursor:one": AttentionEvidence(phase="unknown", observed_at=1, source="history")},
        )
        self.assertEqual(states["cursor:one"].kind, "working")

    def test_live_history_idle_does_not_force_working_when_question_gone(self) -> None:
        """提问消失且历史已表明本轮结束时，常驻进程不得再被强行标成执行中。"""
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(
                phase="waiting",
                question_token="ask-1",
                observed_at=1,
                source="history",
            ),
        )
        states = self.store.reconcile(
            [_session("cursor", "one", live=True)],
            {"cursor:one": AttentionEvidence(phase="idle", observed_at=2, source="history")},
        )
        self.assertNotEqual(states["cursor:one"].kind, "working")
        self.assertNotEqual(states["cursor:one"].kind, "waiting")

    def test_history_idle_does_not_clear_open_before_submit_turn(self) -> None:
        """用户提交后的中间助手正文不得熄灭本轮绿点；收束仍靠观察器结束事件。"""
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(
                phase="working",
                activity_token="gen-1:beforeSubmitPrompt",
                observed_at=1,
                source="observer",
            ),
        )
        state = self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(
                phase="idle",
                activity_token="assistant-text",
                observed_at=5,
                source="history",
            ),
        )
        self.assertEqual(state.kind, "working")

    def test_after_agent_response_clears_working_even_while_live(self):
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(
                phase="working",
                activity_token="gen-1:beforeSubmitPrompt",
                observed_at=1,
                source="observer",
            ),
        )
        state = self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(
                phase="idle",
                activity_token="gen-1:afterAgentResponse",
                observed_at=2,
                source="observer",
            ),
        )
        self.assertNotEqual(state.kind, "working")
        states = self.store.reconcile(
            [_session("cursor", "one", live=True)],
            {"cursor:one": AttentionEvidence(phase="unknown", observed_at=2, source="history")},
        )
        self.assertNotEqual(states["cursor:one"].kind, "working")

    def test_stale_after_agent_response_working_self_heals_while_live(self):
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        self.store.record_event(
            "cursor",
            "one",
            AttentionEvidence(
                phase="working",
                activity_token="gen-1:afterAgentResponse",
                observed_at=10,
                source="observer",
            ),
        )
        states = self.store.reconcile(
            [_session("cursor", "one", live=True)],
            {"cursor:one": AttentionEvidence(phase="unknown", observed_at=10, source="history")},
        )
        self.assertNotEqual(states["cursor:one"].kind, "working")

    def test_non_live_reconcile_clears_active_phase_and_surfaces_terminal_unread(self):
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        self.store.record_event(
            "kimi",
            "one",
            AttentionEvidence(phase="working", observed_at=1, source="observer"),
        )
        states = self.store.reconcile([_session("kimi", "one", live=False)], {})
        self.assertEqual(states["kimi:one"].kind, "unread")
        self.assertEqual(self.store.mark_read("kimi", "one").kind, "none")

    def test_non_live_fact_overrides_newer_observer_once_without_red_reappearing(self):
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        self.store.record_event(
            "codex",
            "one",
            AttentionEvidence(
                phase="working", observed_at=200, source="observer",
            ),
        )
        stopped = {"source": "codex", "id": "one", "live": False}
        old_history = {
            "codex:one": AttentionEvidence(
                phase="idle", observed_at=100, source="history",
            )
        }
        first = self.store.reconcile([stopped], old_history)
        self.assertEqual(first["codex:one"].kind, "unread")
        self.assertEqual(self.store.mark_read("codex", "one").kind, "none")

        second = self.store.reconcile([stopped], old_history)
        self.assertEqual(second["codex:one"].kind, "none")

    def test_state_persists_across_store_instances(self):
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        self.store.record_event(
            "opencode",
            "one",
            AttentionEvidence(activity_token="reply", observed_at=1),
        )
        reopened = AttentionStore(self.path)
        self.assertEqual(reopened.get("opencode", "one").kind, "unread")
        self.assertEqual(reopened.get_many(["opencode:one"])["opencode:one"].kind, "unread")

    def test_multiple_instances_can_write_concurrently(self):
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})

        def write(index: int) -> None:
            store = AttentionStore(self.path)
            store.record_event(
                "codex",
                str(index),
                AttentionEvidence(activity_token=f"reply-{index}", observed_at=index + 1),
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(32)))

        states = self.store.get_many([f"codex:{index}" for index in range(32)])
        self.assertTrue(all(state.kind == "unread" for state in states.values()))

    def test_migrate_remove_and_prune(self):
        self.store.reconcile([{"source": "test", "id": "baseline", "live": False}], {})
        self.store.record_event(
            "cursor",
            "temporary",
            AttentionEvidence(phase="working", observed_at=1, source="observer"),
        )
        self.store.migrate_session("cursor", "temporary", "real")
        self.assertEqual(self.store.get("cursor", "temporary").kind, "none")
        self.assertEqual(self.store.get("cursor", "real").kind, "working")
        self.store.remove_session("cursor", "real")
        self.assertEqual(self.store.get("cursor", "real").kind, "none")

        self.store.record_event(
            "codex", "old", AttentionEvidence(activity_token="reply", observed_at=1)
        )
        self.assertEqual(self.store.prune(max_age_seconds=0), 1)
        self.assertEqual(self.store.get("codex", "old").kind, "none")

    def test_corrupt_database_degrades_without_raising(self):
        self.path.write_bytes("这不是 SQLite 数据库".encode())
        broken = AttentionStore(self.path)
        self.assertEqual(broken.get("codex", "one").kind, "none")
        self.assertEqual(
            broken.record_event(
                "codex", "one", AttentionEvidence(activity_token="reply")
            ).kind,
            "none",
        )
        self.assertEqual(
            broken.reconcile([_session("codex", "one")], {})["codex:one"].kind,
            "none",
        )
        broken.remove_session("codex", "one")
        broken.migrate_session("codex", "one", "two")
        self.assertEqual(broken.prune(), 0)


if __name__ == "__main__":
    unittest.main()
