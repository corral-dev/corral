"""SessionStore 与会话关注状态的集成测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral.attention import AttentionEvidence, AttentionStore
from corral.store import SessionStore


class _Registry:
    ids = ("claude", "codex", "cursor")


def _session(
    runtime: str,
    session_id: str,
    *,
    mtime: float = 1.0,
    live: bool = False,
    path: str = "",
) -> dict:
    return {
        "source": runtime,
        "id": session_id,
        "short_id": session_id[:12],
        "cwd": "/tmp/project",
        "cwd_display": "project",
        "mtime": mtime,
        "display_time": "01-01 00:00",
        "time_source": "file_mtime",
        "event_time": mtime,
        "file_mtime": mtime,
        "size_bytes": 1,
        "size_kb": 0.1,
        "native_title": None,
        "fallback_title": session_id,
        "status_tag": "已完成",
        "live": live,
        "pid": 1 if live else None,
        "first_user_msg": "任务",
        "last_user_msg": "任务",
        "last_agent_msg": "回复",
        "path": path,
    }


class SessionStoreAttentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "attention.sqlite3"
        self.attention = AttentionStore(path)
        self.store = SessionStore(50, _Registry(), self.attention)
        self.title_patch = mock.patch(
            "corral.store.titles.resolve_initial_title",
            side_effect=lambda session, cache: (session["fallback_title"], False),
        )
        self.keepalive_patch = mock.patch("corral.store.liveness.annotate")
        self.hosts_patch = mock.patch("corral.store.liveness.list_managed_hosts", return_value=[])
        self.title_patch.start()
        self.keepalive = self.keepalive_patch.start()
        self.hosts_patch.start()

    def tearDown(self):
        self.hosts_patch.stop()
        self.keepalive_patch.stop()
        self.title_patch.stop()
        self.temp.cleanup()

    def test_first_scan_baselines_old_reply_then_increment_is_unread(self):
        evidence = AttentionEvidence(activity_token="old", observed_at=1)
        with mock.patch("corral.store.inspect_session", return_value=evidence):
            self.store._merge_scanned({"claude": [_session("claude", "one")]})
        first = self.store.find_session("claude:one")
        self.assertNotIn("attention_kind", first)
        self.assertNotIn("attention_token", first)
        self.assertNotIn("attention_updated_at", first)

        evidence = AttentionEvidence(activity_token="new", observed_at=2)
        with mock.patch("corral.store.inspect_session", return_value=evidence):
            self.store._merge_scanned(
                {"claude": [_session("claude", "one", mtime=2)]}
            )
        updated = self.store.find_session("claude:one")
        self.assertEqual(updated["attention_kind"], "unread")
        self.assertEqual(updated["status_tag"], "已完成")
        self.assertEqual(self.store.mark_session_read("claude:one").kind, "none")
        self.assertNotIn("attention_kind", updated)

    def test_working_waiting_and_non_live_clear(self):
        sessions = [
            _session("codex", "working", live=True),
            _session("claude", "waiting", live=True),
        ]

        def active_evidence(session: dict) -> AttentionEvidence:
            if session["id"] == "waiting":
                return AttentionEvidence(
                    phase="waiting",
                    activity_token="question",
                    question_token="call",
                    observed_at=1,
                )
            return AttentionEvidence(phase="working", observed_at=1)

        with mock.patch("corral.store.inspect_session", side_effect=active_evidence):
            self.store._merge_scanned({"codex": [sessions[0]], "claude": [sessions[1]]})
        self.assertEqual(self.store.attention_for("codex:working").kind, "working")
        self.assertEqual(self.store.attention_for("claude:waiting").kind, "waiting")

        stopped = _session("codex", "working", live=False, mtime=2)
        with mock.patch(
            "corral.store.inspect_session",
            return_value=AttentionEvidence(observed_at=2),
        ):
            self.store._merge_scanned({"codex": [stopped], "claude": []})
        self.assertEqual(self.store.attention_for("codex:working").kind, "unread")

    def test_attention_changes_do_not_reorder_sessions(self):
        older = _session("claude", "older", mtime=10)
        newer = _session("claude", "newer", mtime=20)
        with mock.patch(
            "corral.store.inspect_session",
            return_value=AttentionEvidence(observed_at=1),
        ):
            self.store._merge_scanned({"claude": [newer, older]})
        before = [session["id"] for session in self.store.all_sessions()]

        self.attention.record_event(
            "claude",
            "older",
            AttentionEvidence(
                phase="idle",
                activity_token="reply",
                observed_at=2,
                source="observer",
            ),
        )
        with mock.patch("corral.store.inspect_session") as inspect:
            self.store._merge_scanned({"claude": [newer, older]})
        inspect.assert_not_called()
        after = [session["id"] for session in self.store.all_sessions()]
        self.assertEqual(after, before)
        self.assertEqual(self.store.attention_for("claude:older").kind, "unread")

    def test_attention_fields_participate_in_refresh_signature(self):
        session = _session("claude", "one")
        with mock.patch(
            "corral.store.inspect_session",
            return_value=AttentionEvidence(observed_at=1),
        ):
            self.store._merge_scanned({"claude": [session]})
        before = self.store._sessions_signature()
        self.store.find_session("claude:one")["attention_kind"] = "unread"
        self.assertNotEqual(self.store._sessions_signature(), before)

    def test_unchanged_evidence_is_reused_and_only_changed_session_is_reinspected(self):
        one = _session("claude", "one", mtime=10)
        two = _session("codex", "two", mtime=20)
        calls: list[str] = []

        def inspect(session: dict) -> AttentionEvidence:
            calls.append(f"{session['source']}:{session['id']}")
            return AttentionEvidence(
                activity_token=f"reply-{session['id']}",
                observed_at=float(session["mtime"]),
            )

        with mock.patch("corral.store.inspect_session", side_effect=inspect):
            self.store._merge_scanned({"claude": [one], "codex": [two]})
            self.assertEqual(calls, ["claude:one", "codex:two"])

            calls.clear()
            self.store._merge_scanned({"claude": [one], "codex": [two]})
            self.assertEqual(calls, [])

            calls.clear()
            changed_one = dict(one, mtime=11)
            self.store._merge_scanned({"claude": [changed_one], "codex": [two]})
            self.assertEqual(calls, ["claude:one"])

            calls.clear()
            live_two = dict(two, live=True, pid=os.getpid())
            self.store._merge_scanned({"claude": [changed_one], "codex": [live_two]})
            self.assertEqual(calls, ["codex:two"])

            calls.clear()
            self.store._merge_scanned({"claude": [], "codex": [live_two]})
            self.assertNotIn("claude:one", self.store._attention_evidence_cache)
            self.store._merge_scanned({"claude": [changed_one], "codex": [live_two]})
            self.assertEqual(calls, ["claude:one"])

    def test_cursor_probe_only_for_live_or_changed_history(self):
        chat_dir = Path(self.temp.name) / "chat"
        chat_dir.mkdir()
        store_db = chat_dir / "store.db"
        prompt_history = chat_dir / "prompt_history.json"
        store_db.write_bytes(b"first")
        prompt_history.write_text("[]", encoding="utf-8")
        probes: list[bool] = []

        def capture(session: dict) -> AttentionEvidence:
            probes.append(session.get("signal_probe") is True)
            return AttentionEvidence(observed_at=float(len(probes)))

        with mock.patch("corral.store.inspect_session", side_effect=capture):
            self.store._merge_scanned(
                {"cursor": [_session("cursor", "one", path=str(chat_dir))]}
            )
            self.store._merge_scanned(
                {"cursor": [_session("cursor", "one", path=str(chat_dir))]}
            )
            store_db.write_bytes(b"second-longer")
            os.utime(store_db, None)
            self.store._merge_scanned(
                {"cursor": [_session("cursor", "one", path=str(chat_dir))]}
            )
            self.store._merge_scanned(
                {"cursor": [_session("cursor", "one", live=True, path=str(chat_dir))]}
            )

        self.assertEqual(probes, [False, True, True])

    def test_provisional_state_migrates_to_real_session_and_delete_cleans_it(self):
        name = "corral-codex-temporary"
        self.attention.reconcile([], {})
        self.attention.record_event(
            "codex",
            "temporary",
            AttentionEvidence(phase="working", observed_at=1, source="observer"),
        )
        provisional = self.store.register_hosted_session(
            runtime_id="codex",
            keepalive_name=name,
            title="新会话",
            cwd="/tmp/project",
            ident="temporary",
        )
        self.assertEqual(provisional["attention_kind"], "working")

        def annotate(sessions: list[dict]) -> None:
            sessions[0]["keepalive_name"] = name

        self.keepalive.side_effect = annotate
        real = _session("codex", "real", live=True)
        with mock.patch(
            "corral.store.inspect_session",
            return_value=AttentionEvidence(observed_at=2),
        ):
            self.store._merge_scanned({"codex": [real]})
        self.assertEqual(self.store.attention_for("codex:temporary").kind, "none")
        self.assertEqual(self.store.attention_for("codex:real").kind, "working")

        self.store.remove_session("codex:real")
        self.assertEqual(self.store.attention_for("codex:real").kind, "none")
        self.assertEqual(self.attention.get("codex", "real").kind, "none")

    def test_refresh_uses_live_merge_when_scan_cache_hits(self) -> None:
        """Signature cache hits within the full-merge window skip whole-table replace."""
        session = _session("claude", "one", live=True, mtime=10)
        with mock.patch(
            "corral.store.inspect_session",
            return_value=AttentionEvidence(phase="working", observed_at=1),
        ):
            self.store._merge_scanned({"claude": [session]})
        self.store._last_full_merge_at = 1_000_000.0
        held = self.store.find_session("claude:one")

        class _HitRegistry:
            ids = ("claude", "codex", "cursor")
            last_scan_cache_hit_all = True
            last_scan_shared = False

            def scan_all(self, limit, keep_ids_by_runtime=None, *, prefer_shared=True):
                return {
                    "claude": [dict(session, last_agent_msg="should-not-replace")],
                    "codex": [],
                    "cursor": [],
                }

        self.store.registry = _HitRegistry()
        merges: list[str] = []
        live_calls: list[str] = []
        self.store._merge_scanned = lambda scanned: merges.append("full")  # type: ignore[method-assign]
        self.store._merge_live_state = lambda: live_calls.append("live")  # type: ignore[method-assign]
        with mock.patch("corral.store.time.monotonic", return_value=1_000_005.0):
            changed = self.store.refresh()
        self.assertEqual(live_calls, ["live"])
        self.assertEqual(merges, [])
        self.assertIs(self.store.find_session("claude:one"), held)
        self.assertFalse(changed)

        self.store.registry.last_scan_cache_hit_all = False
        live_calls.clear()
        with mock.patch("corral.store.time.monotonic", return_value=1_000_006.0):
            self.store.refresh()
        self.assertEqual(merges, ["full"])
        self.assertEqual(live_calls, [])

        # Optimistic delete empties memory while the scan cache still has the
        # session — live merge must not win, or refresh can never restore the card.
        merges.clear()
        live_calls.clear()
        self.store.registry.last_scan_cache_hit_all = True
        self.store.sessions = {"claude": [], "codex": [], "cursor": []}
        self.store._order = []
        with mock.patch("corral.store.time.monotonic", return_value=1_000_007.0):
            self.store.refresh()
        self.assertEqual(merges, ["full"])
        self.assertEqual(live_calls, [])

    def test_refresh_live_merge_ignores_hosted_placeholders(self) -> None:
        """Unmatched hosted placeholders must not force a whole-table merge."""
        session = _session("claude", "one", live=True, mtime=10)
        with mock.patch(
            "corral.store.inspect_session",
            return_value=AttentionEvidence(phase="working", observed_at=1),
        ):
            self.store._merge_scanned({"claude": [session]})
            self.store.register_hosted_session(
                runtime_id="cursor",
                keepalive_name="corral-cursor-deadbeef",
                title="New Cursor",
                cwd="/tmp/project",
                ident="deadbeef",
            )
        self.store._last_full_merge_at = 1_000_000.0
        held = self.store.find_session("claude:one")
        placeholder = self.store.find_session("cursor:deadbeef")
        self.assertTrue(placeholder.get("provisional"))

        class _HitRegistry:
            ids = ("claude", "codex", "cursor")
            last_scan_cache_hit_all = True
            last_scan_shared = False

            def scan_all(self, limit, keep_ids_by_runtime=None, *, prefer_shared=True):
                return {
                    "claude": [dict(session)],
                    "codex": [],
                    "cursor": [],
                }

        self.store.registry = _HitRegistry()
        merges: list[str] = []
        live_calls: list[str] = []
        self.store._merge_scanned = lambda scanned: merges.append("full")  # type: ignore[method-assign]
        self.store._merge_live_state = lambda: live_calls.append("live")  # type: ignore[method-assign]
        with mock.patch("corral.store.time.monotonic", return_value=1_000_005.0):
            changed = self.store.refresh()
        self.assertEqual(live_calls, ["live"])
        self.assertEqual(merges, [])
        self.assertIs(self.store.find_session("claude:one"), held)
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
