"""全文搜索索引（`corral.search`）的纯逻辑测试。

这一层不碰 Textual：索引怎么建、命中行怎么挑、高亮区间算得对不对、长行怎么
开窗、排序是不是按会话时间由新到旧，全部在这里锁死。弹窗的交互行为在
test_ui.py 里用 Pilot 验证。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from corral.models import ConversationMessage
from corral.search import ConversationIndex, split_keywords


def _session(session_id: str, *, title: str, cwd: str = "/tmp/demo", mtime: float | None = None):
    return {
        "source": "claude",
        "id": session_id,
        "short_id": session_id,
        "mtime": time.time() if mtime is None else mtime,
        "file_mtime": 100.0,
        "size_bytes": 10,
        "size_kb": 1,
        "native_title": None,
        "fallback_title": title,
        "cwd": cwd,
        "cwd_display": cwd,
        "path": f"/tmp/{session_id}.jsonl",
        "live": False,
    }


class _FakeStore:
    """只提供索引需要的两个能力：列出会话、按会话取对话。"""

    def __init__(self, sessions, conversations):
        self._sessions = sessions
        self._conversations = conversations
        self.reads: list[str] = []

    def all_sessions(self):
        return list(self._sessions)

    def get_conversation(self, session):
        self.reads.append(session["id"])
        return list(self._conversations.get(session["id"], []))

    def snapshot(self):
        return {}


class SplitKeywordsTests(unittest.TestCase):
    def test_splits_and_lowercases(self) -> None:
        self.assertEqual(split_keywords("  Filter  Session "), ["filter", "session"])

    def test_blank_query_has_no_keywords(self) -> None:
        self.assertEqual(split_keywords("   "), [])
        self.assertEqual(split_keywords(""), [])


class ConversationIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        isolated = tempfile.TemporaryDirectory()
        self.addCleanup(isolated.cleanup)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": isolated.name, "CORRAL_CACHE": "1"}).start()
        self.sessions = [
            _session("a", title="侧边栏改造", cwd="/Users/x/corral", mtime=300),
            _session("b", title="字幕优化", cwd="/Users/x/LiveCaption", mtime=200),
            _session("c", title="节点选择", cwd="/Users/x/ProxyAgent", mtime=100),
        ]
        self.conversations = {
            "a": [
                ConversationMessage("user", "左上角的筛选想做成支持全文搜索\n顺便看看性能", 1.0),
                ConversationMessage("assistant", "可以先量一下对话正文的总量", 2.0),
            ],
            "b": [
                ConversationMessage("user", "字幕断句不准", 3.0),
                ConversationMessage("assistant", "先看看识别结果", 4.0),
            ],
            "c": [
                ConversationMessage("user", "节点延迟高", 5.0),
            ],
        }
        self.store = _FakeStore(self.sessions, self.conversations)
        self.index = ConversationIndex()

    def test_index_starts_unready_and_becomes_ready_after_refresh(self) -> None:
        self.assertFalse(self.index.ready)
        self.assertEqual(self.index.refresh(self.store), 3)
        self.assertTrue(self.index.ready)
        self.assertEqual(self.index.indexed_count, 3)

    def test_matches_conversation_body_and_returns_hit_line(self) -> None:
        self.index.refresh(self.store)
        matches = self.index.search(self.sessions, "全文搜索")
        self.assertEqual([m.session["id"] for m in matches], ["a"])
        match = matches[0]
        self.assertFalse(match.meta_only)
        self.assertEqual(len(match.lines), 1)
        line = match.lines[0]
        self.assertEqual(line.role, "user")
        self.assertIn("全文搜索", line.text)
        # 高亮区间必须正好圈住关键词
        start, end = line.spans[0]
        self.assertEqual(line.text[start:end], "全文搜索")

    def test_blank_query_returns_nothing(self) -> None:
        self.index.refresh(self.store)
        self.assertEqual(list(self.index.search(self.sessions, "   ")), [])

    def test_multiple_keywords_require_all_of_them(self) -> None:
        self.index.refresh(self.store)
        self.assertEqual(
            [m.session["id"] for m in self.index.search(self.sessions, "筛选 性能")], ["a"],
        )
        # "筛选" 在 a 里，"字幕" 在 b 里，没有会话同时满足
        self.assertEqual(list(self.index.search(self.sessions, "筛选 字幕")), [])

    def test_keywords_may_be_split_across_title_and_body(self) -> None:
        """一个词命中标题、另一个词命中正文时也算命中。"""
        self.index.refresh(self.store)
        matches = self.index.search(self.sessions, "侧边栏 性能")
        self.assertEqual([m.session["id"] for m in matches], ["a"])

    def test_title_only_match_reports_meta_only(self) -> None:
        self.index.refresh(self.store)
        matches = self.index.search(self.sessions, "字幕优化")
        self.assertEqual([m.session["id"] for m in matches], ["b"])
        # 「字幕优化」四个字连起来只出现在标题里，正文没有整串
        self.assertTrue(matches[0].meta_only)
        self.assertEqual(matches[0].lines, ())

    def test_project_name_still_matches(self) -> None:
        """弹窗不能比侧边栏筛选弱：项目名/路径照样能搜到。"""
        self.index.refresh(self.store)
        matches = self.index.search(self.sessions, "proxyagent")
        self.assertEqual([m.session["id"] for m in matches], ["c"])

    def test_results_sorted_by_session_time_newest_first(self) -> None:
        for messages in self.conversations.values():
            messages.append(ConversationMessage("user", "共同关键词 corral", 9.0))
        self.index.refresh(self.store)
        matches = self.index.search(self.sessions, "共同关键词")
        self.assertEqual([m.session["id"] for m in matches], ["a", "b", "c"])

    def test_hit_count_counts_all_lines_but_display_is_capped(self) -> None:
        self.conversations["a"] = [
            ConversationMessage("user", "\n".join(f"第{i}行 关键词" for i in range(10)), 1.0),
        ]
        self.index.refresh(self.store)
        match = self.index.search(self.sessions, "关键词", max_lines=3)[0]
        self.assertEqual(match.total_hits, 10)
        self.assertEqual(len(match.lines), 3)

    def test_lines_matching_all_keywords_win_over_partial_ones(self) -> None:
        self.conversations["a"] = [
            ConversationMessage("user", "只有筛选\n筛选加性能都在这一行\n只有性能", 1.0),
        ]
        self.index.refresh(self.store)
        match = self.index.search(self.sessions, "筛选 性能", max_lines=1)[0]
        self.assertEqual(match.lines[0].text, "筛选加性能都在这一行")

    def test_long_line_is_windowed_around_the_hit(self) -> None:
        body = "前" * 500 + "关键词" + "后" * 500
        self.conversations["a"] = [ConversationMessage("user", body, 1.0)]
        self.index.refresh(self.store)
        line = self.index.search(self.sessions, "关键词")[0].lines[0]
        self.assertLess(len(line.text), 250)
        self.assertTrue(line.text.startswith("…"))
        start, end = line.spans[0]
        self.assertEqual(line.text[start:end], "关键词")

    def test_top_caps_returned_matches_but_total_still_counts_everything(self) -> None:
        """截断必须如实报总数：状态行要能告诉用户「还有多少条没显示」。"""
        for messages in self.conversations.values():
            messages.append(ConversationMessage("user", "共同关键词", 9.0))
        self.index.refresh(self.store)
        outcome = self.index.search(self.sessions, "共同关键词", top=2)
        self.assertEqual(outcome.total, 3)
        self.assertEqual([m.session["id"] for m in outcome.matches], ["a", "b"])

    def test_top_does_not_change_which_sessions_come_first(self) -> None:
        """先排序后截断：截断不能影响前几条的内容或顺序。"""
        for messages in self.conversations.values():
            messages.append(ConversationMessage("user", "共同关键词", 9.0))
        self.index.refresh(self.store)
        full = self.index.search(self.sessions, "共同关键词")
        capped = self.index.search(self.sessions, "共同关键词", top=2)
        self.assertEqual(
            [m.session["id"] for m in full.matches][:2],
            [m.session["id"] for m in capped.matches],
        )
        self.assertEqual(full.matches[0].lines, capped.matches[0].lines)

    def test_windowed_line_stays_within_the_character_budget(self) -> None:
        """开窗后的行（含两端省略号）不能超过预算，否则窄终端会硬截掉正文。"""
        from corral.search import _MAX_LINE_CHARS

        body = "前" * 500 + "关键词" + "后" * 500
        self.conversations["a"] = [ConversationMessage("user", body, 1.0)]
        self.index.refresh(self.store)
        line = self.index.search(self.sessions, "关键词")[0].lines[0]
        self.assertLessEqual(len(line.text), _MAX_LINE_CHARS)
        self.assertTrue(line.text.startswith("…"))
        self.assertTrue(line.text.endswith("…"))
        start, end = line.spans[0]
        self.assertEqual(line.text[start:end], "关键词")

    def test_unchanged_sessions_are_not_read_twice(self) -> None:
        self.index.refresh(self.store)
        self.assertEqual(sorted(self.store.reads), ["a", "b", "c"])
        self.store.reads.clear()
        self.index.refresh(self.store)
        self.assertEqual(self.store.reads, [])

    def test_changed_session_is_reindexed(self) -> None:
        self.index.refresh(self.store)
        self.store.reads.clear()
        self.sessions[0]["file_mtime"] = 999.0
        self.conversations["a"] = [ConversationMessage("user", "换了新内容 磁盘写入", 1.0)]
        self.index.refresh(self.store)
        self.assertEqual(self.store.reads, ["a"])
        self.assertEqual(
            [m.session["id"] for m in self.index.search(self.sessions, "磁盘写入")], ["a"],
        )

    def test_unindexed_sessions_still_match_on_title(self) -> None:
        """索引还没建完时不能把会话整个吞掉：标题/项目仍要能搜到。"""
        empty_index = ConversationIndex()
        matches = empty_index.search(self.sessions, "字幕优化")
        self.assertEqual([m.session["id"] for m in matches], ["b"])
        self.assertTrue(matches[0].meta_only)

    def test_display_title_from_store_snapshot_is_searchable(self) -> None:
        """标题补全后的展示标题（缓存里的）也要能搜到，而不是只搜兜底标题。"""
        self.index.refresh(self.store)
        titles = {"claude:a": "全文检索能力评估"}
        matches = self.index.search(self.sessions, "检索能力", titles=titles)
        self.assertEqual([m.session["id"] for m in matches], ["a"])
        self.assertEqual(matches[0].title, "全文检索能力评估")

    def test_broken_conversation_does_not_break_the_whole_index(self) -> None:
        class _AngryStore(_FakeStore):
            def get_conversation(self, session):
                if session["id"] == "b":
                    raise OSError("历史文件坏了")
                return super().get_conversation(session)

        store = _AngryStore(self.sessions, self.conversations)
        index = ConversationIndex()
        self.assertEqual(index.refresh(store), 3)
        self.assertEqual(
            [m.session["id"] for m in index.search(self.sessions, "筛选")], ["a"],
        )

    def test_new_index_restores_text_without_loading_conversations(self) -> None:
        self.index.refresh(self.store)
        expected = self.index.search(self.sessions, "全文搜索 性能")
        self.store.reads.clear()
        restored = ConversationIndex()
        restored.refresh(self.store)
        self.assertEqual(self.store.reads, [])
        self.assertEqual(restored.search(self.sessions, "全文搜索 性能"), expected)

    def test_persistent_index_invalidates_only_changed_session(self) -> None:
        self.index.refresh(self.store)
        self.store.reads.clear()
        self.sessions[0]["file_mtime"] += 1
        self.conversations["a"] = [ConversationMessage("user", "新内容", 10)]
        restored = ConversationIndex()
        restored.refresh(self.store)
        self.assertEqual(self.store.reads, ["a"])
        self.assertEqual(restored.search(self.sessions, "新内容").total, 1)
        self.assertEqual(restored.search(self.sessions, "全文搜索").total, 0)

    def test_corrupt_entry_falls_back_to_original_conversation(self) -> None:
        from corral.cache import get_cache

        self.index.refresh(self.store)
        signature, _ = get_cache().get_search_entries(["claude:a"])["claude:a"]
        get_cache().put_search_entries([("claude:a", signature, b"corrupt")])
        self.store.reads.clear()
        restored = ConversationIndex()
        restored.refresh(self.store)
        self.assertEqual(self.store.reads, ["a"])
        self.assertEqual(restored.search(self.sessions, "全文搜索").total, 1)

    def test_provider_change_invalidates_persisted_entries(self) -> None:
        self.index.refresh(self.store)
        self.store.reads.clear()
        with mock.patch("corral.cache.provider_cohort", return_value="changed-provider"):
            ConversationIndex().refresh(self.store)
        self.assertEqual(sorted(self.store.reads), ["a", "b", "c"])

    def test_disabled_cache_does_not_restore_previous_index(self) -> None:
        self.index.refresh(self.store)
        self.store.reads.clear()
        with mock.patch.dict(os.environ, {"CORRAL_CACHE": "0"}):
            ConversationIndex().refresh(self.store)
        self.assertEqual(sorted(self.store.reads), ["a", "b", "c"])

    def test_transient_read_failure_is_retried_without_signature_change(self) -> None:
        with mock.patch.object(self.store, "get_conversation", side_effect=OSError("temporary")):
            self.index.refresh(self.store)
        self.index.refresh(self.store)
        self.assertEqual(self.index.search(self.sessions, "全文搜索").total, 1)

    def test_wal_append_invalidates_text_between_catalog_scans(self) -> None:
        path = Path(os.environ["CORRAL_CACHE_DIR"]) / "conversation.db"
        path.write_bytes(b"database")
        self.sessions[0]["path"] = str(path)
        self.index.refresh(self.store)
        Path(str(path) + "-wal").write_bytes(b"new message")
        self.store.reads.clear()
        self.conversations["a"] = [ConversationMessage("user", "新增正文", 10)]
        self.index.refresh(self.store)
        self.assertEqual(self.store.reads, ["a"])
        self.assertEqual(self.index.search(self.sessions, "新增正文").total, 1)

    def test_shared_opencode_database_write_does_not_reload_unchanged_session(self) -> None:
        path = Path(os.environ["CORRAL_CACHE_DIR"]) / "opencode.db"
        path.write_bytes(b"database")
        self.sessions[0].update(source="opencode", path=str(path))
        self.index.refresh(self.store)
        path.write_bytes(b"unrelated session changed")
        self.store.reads.clear()
        self.index.refresh(self.store)
        self.assertEqual(self.store.reads, [])

    def test_short_chinese_literals_and_punctuation_survive_restore(self) -> None:
        self.conversations["a"] = [ConversationMessage("user", "搜 搜索 %_ \\\n部署成功", 1)]
        self.index.refresh(self.store)
        restored = ConversationIndex()
        restored.refresh(self.store)
        for query in ("搜", "搜索", "%_", "部署 成功"):
            self.assertEqual(restored.search(self.sessions, query).total, 1, query)

    def test_only_displayed_lines_compute_highlight_spans(self) -> None:
        from corral.search import _spans_in

        self.conversations["a"] = [ConversationMessage("user", "关键词\n" * 2000, 1)]
        self.index.refresh(self.store)
        with mock.patch("corral.search._spans_in", wraps=_spans_in) as spans:
            match = self.index.search(self.sessions, "关键词", max_lines=3)[0]
        self.assertEqual(match.total_hits, 2000)
        self.assertEqual(len(match.lines), 3)
        self.assertEqual(spans.call_count, 3)


if __name__ == "__main__":
    unittest.main()
