"""Plain-conversation retention budget and one-shot index loading.

These tests are headless and deterministic: no Textual app, no real runtime,
no product history. They pin the measured memory contract for
``SessionStore.conversations`` (byte-weighted LRU, oversized bypass, single
flight, invalidation) and the search index's unretained conversation read.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import corral
from corral.conversation_cache import (
    DEFAULT_CONVERSATION_CACHE_BYTES,
    ConversationCache,
    estimate_conversation_bytes,
)
from corral.models import ConversationMessage
from corral.search import ConversationIndex


def _session(
    session_id: str = "s1",
    *,
    path: str | None = None,
    mtime: float = 100.0,
    live: bool = False,
) -> dict:
    session = {
        "source": "claude",
        "id": session_id,
        "short_id": session_id,
        "mtime": mtime,
        "file_mtime": mtime,
        "size_bytes": 10,
        "size_kb": 1,
        "native_title": None,
        "fallback_title": "内存测试",
        "cwd": "/tmp/demo",
        "cwd_display": "/tmp/demo",
        "live": live,
    }
    if path is not None:
        session["path"] = path
    return session


def _conversation(text: str = "正文", *, role: str = "user", timestamp: float = 1.0):
    return [ConversationMessage(role, text, timestamp)]


class _StoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        isolated = tempfile.TemporaryDirectory()
        self.addCleanup(isolated.cleanup)
        self.addCleanup(mock.patch.stopall)
        self.cache_dir = Path(isolated.name)
        mock.patch.dict(
            os.environ,
            {"CORRAL_CACHE_DIR": str(self.cache_dir), "CORRAL_CACHE": "1"},
        ).start()

    def _make_store(self, messages=None, *, runtime=None):
        runtime = runtime or mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = []
        if messages is not None:
            runtime.load_conversation.return_value = messages
        registry = corral.RuntimeRegistry((runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
        return store, runtime


class ConversationCacheBudgetTests(unittest.TestCase):
    def test_unicode_weight_uses_encoded_string_size_not_character_count(self) -> None:
        ascii_messages = _conversation("a" * 2000)
        cjk_messages = _conversation("汉" * 2000)
        emoji_messages = _conversation("🙂" * 2000)

        ascii_bytes = estimate_conversation_bytes(ascii_messages)
        cjk_bytes = estimate_conversation_bytes(cjk_messages)
        emoji_bytes = estimate_conversation_bytes(emoji_messages)

        self.assertGreaterEqual(cjk_bytes, 2 * 2000)
        self.assertGreater(cjk_bytes, ascii_bytes)
        self.assertGreater(emoji_bytes, cjk_bytes)

    def test_hit_recency_evicts_the_least_recently_used_entry(self) -> None:
        messages = _conversation("x" * 400)
        # The cache retains a list copy, whose allocation can differ by one slot
        # from the literal list used to build the fixture.
        unit = estimate_conversation_bytes(list(messages))
        cache = ConversationCache(max_bytes=3 * unit)
        for key in ("a", "b", "c"):
            cache[key] = (1.0, messages)
        self.assertEqual(list(cache), ["a", "b", "c"])

        self.assertEqual(cache["a"][1][0].text, "x" * 400)  # promote a
        cache["d"] = (1.0, messages)

        self.assertEqual(list(cache), ["c", "a", "d"])
        self.assertNotIn("b", cache)
        self.assertLessEqual(cache.total_bytes, cache.max_bytes)

    def test_budget_never_exceeded_and_direct_mapping_calls_still_work(self) -> None:
        small = _conversation("small")
        medium = _conversation("汉" * 500)
        cache = ConversationCache()
        budget = (
            estimate_conversation_bytes(list(small))
            + estimate_conversation_bytes(list(medium))
            + 10
        )
        cache.max_bytes = budget
        cache["small"] = (None, small)
        cache["medium"] = (None, medium)

        self.assertIn("small", cache)
        self.assertIn("medium", cache)
        self.assertLessEqual(cache.total_bytes, budget)
        self.assertEqual(cache.get("small")[1], small)
        self.assertIsNone(cache.pop("missing", None))
        self.assertEqual(cache.pop("small")[0], None)
        self.assertNotIn("small", cache)
        cache.clear()
        self.assertEqual(len(cache), 0)
        self.assertEqual(cache.total_bytes, 0)

    def test_oversized_entry_bypasses_retention_without_evicting_others(self) -> None:
        small = _conversation("small")
        small_bytes = estimate_conversation_bytes(list(small))
        cache = ConversationCache(max_bytes=small_bytes + 32)
        cache["small"] = (1.0, small)

        oversized = _conversation("汉" * 4000)
        self.assertGreater(estimate_conversation_bytes(oversized), cache.max_bytes)
        cache["oversized"] = (2.0, oversized)

        self.assertIn("small", cache)
        self.assertNotIn("oversized", cache)
        self.assertEqual(cache.total_bytes, small_bytes)

    def test_shrinking_budget_evicts_until_within_budget(self) -> None:
        messages = _conversation("y" * 300)
        unit = estimate_conversation_bytes(list(messages))
        cache = ConversationCache(max_bytes=3 * unit)
        for key in ("a", "b", "c"):
            cache[key] = (1.0, messages)
        cache.max_bytes = unit

        self.assertEqual(list(cache), ["c"])
        self.assertLessEqual(cache.total_bytes, unit)


class SessionConversationCacheTests(_StoreFixture):
    def test_default_budget_is_an_internal_measured_policy(self) -> None:
        store, _ = self._make_store([])
        self.assertEqual(
            store.conversations.max_bytes, DEFAULT_CONVERSATION_CACHE_BYTES,
        )
        self.assertEqual(DEFAULT_CONVERSATION_CACHE_BYTES, 8 * 1024 * 1024)

    def test_get_conversation_caches_and_reuses_the_second_read(self) -> None:
        messages = _conversation("问题")
        store, runtime = self._make_store(messages)
        session = _session()

        self.assertEqual(store.get_conversation(session), messages)
        self.assertEqual(store.get_conversation(session), messages)

        runtime.load_conversation.assert_called_once_with(session)
        self.assertIn("claude:s1", store.conversations)

    def test_oversized_conversation_returns_complete_data_without_retention(self) -> None:
        messages = _conversation("汉" * 4000)
        store, runtime = self._make_store(messages)
        session = _session()
        store.conversations.max_bytes = 200

        self.assertEqual(store.get_conversation(session), messages)
        self.assertNotIn("claude:s1", store.conversations)
        self.assertEqual(store.conversations.total_bytes, 0)

        self.assertEqual(store.get_conversation(session), messages)
        self.assertEqual(runtime.load_conversation.call_count, 2)

    def test_history_change_invalidates_the_cached_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            session = _session(path=str(path))
            runtime = mock.Mock()
            runtime.load_conversation.side_effect = [
                _conversation("旧内容"),
                _conversation("新内容"),
            ]
            store, _ = self._make_store(runtime=runtime)

            self.assertEqual(store.get_conversation(session)[0].text, "旧内容")
            self.assertEqual(store.get_conversation(session)[0].text, "旧内容")
            self.assertEqual(runtime.load_conversation.call_count, 1)

            future = os.stat(path).st_mtime + 5
            os.utime(path, (future, future))

            self.assertEqual(store.get_conversation(session)[0].text, "新内容")
            self.assertEqual(store.get_conversation(session)[0].text, "新内容")
            self.assertEqual(runtime.load_conversation.call_count, 2)

    def test_concurrent_reads_share_one_native_load(self) -> None:
        messages = _conversation("并发正文")
        store, runtime = self._make_store()
        session = _session()
        barrier = threading.Barrier(2)
        results = []
        errors = []
        calls = []

        def slow_load(loaded_session):
            calls.append(loaded_session)
            time.sleep(0.05)
            return list(messages)

        runtime.load_conversation.side_effect = slow_load

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                results.append(store.get_conversation(session))
            except BaseException as exc:  # pragma: no cover - surfaced via assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], messages)
        self.assertEqual(results[1], messages)
        self.assertEqual(len(calls), 1)
        self.assertIn("claude:s1", store.conversations)

    def test_removal_during_load_does_not_resurrect_the_cache_entry(self) -> None:
        messages = _conversation("删除中")
        store, runtime = self._make_store()
        session = _session()
        started = threading.Event()
        release = threading.Event()
        results = []

        def slow_load(_session):
            started.set()
            release.wait(5)
            return list(messages)

        runtime.load_conversation.side_effect = slow_load
        thread = threading.Thread(target=lambda: results.append(store.get_conversation(session)))
        thread.start()
        self.assertTrue(started.wait(5))

        store.remove_session("claude:s1")
        release.set()
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [messages])
        self.assertNotIn("claude:s1", store.conversations)
        self.assertEqual(store.conversations.total_bytes, 0)

        # A later explicit read is a fresh load and may be cached again.
        self.assertEqual(store.get_conversation(session), messages)
        self.assertIn("claude:s1", store.conversations)

    def test_disabled_disk_cache_still_uses_the_bounded_memory_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            session = _session(path=str(path))
            messages = _conversation("离线也完整")
            store, runtime = self._make_store(messages)

            with mock.patch.dict(os.environ, {"CORRAL_CACHE": "0"}):
                self.assertEqual(store.get_conversation(session), messages)
                self.assertEqual(store.get_conversation(session), messages)

            runtime.load_conversation.assert_called_once_with(session)
            self.assertIn("claude:s1", store.conversations)
            self.assertFalse((self.cache_dir / "performance-cache.sqlite3").exists())


class SearchIndexConversationLoadingTests(_StoreFixture):
    def test_index_build_reads_without_polluting_conversation_cache(self) -> None:
        messages = [
            ConversationMessage("user", "第一行提到了正文关键词", 11.0),
            ConversationMessage("assistant", "回答里也有正文关键词", 12.0),
        ]
        store, runtime = self._make_store(messages)
        session = _session()
        index = ConversationIndex()

        self.assertEqual(index.refresh(store, [session]), 1)
        self.assertEqual(runtime.load_conversation.call_count, 1)
        self.assertNotIn("claude:s1", store.conversations)
        self.assertEqual(store.conversations.total_bytes, 0)

        outcome = index.search([session], "正文关键词", top=6)
        self.assertEqual(outcome.total, 1)
        self.assertEqual(len(outcome.matches), 1)
        match = outcome.matches[0]
        self.assertEqual(match.total_hits, 2)
        self.assertEqual(len(match.lines), 2)
        self.assertEqual(match.lines[0].role, "user")
        self.assertEqual(match.lines[0].timestamp, 11.0)
        start, end = match.lines[0].spans[0]
        self.assertEqual(match.lines[0].text[start:end], "正文关键词")
        self.assertEqual(match.lines[1].role, "assistant")

    def test_restored_index_needs_no_conversation_read_or_retention(self) -> None:
        messages = _conversation("持久化恢复正文", timestamp=21.0)
        store, runtime = self._make_store(messages)
        session = _session()
        ConversationIndex().refresh(store, [session])
        self.assertNotIn("claude:s1", store.conversations)

        restored = ConversationIndex()
        restored.refresh(store, [session])
        self.assertEqual(runtime.load_conversation.call_count, 1)
        self.assertNotIn("claude:s1", store.conversations)
        self.assertEqual(restored.search([session], "持久化恢复").total, 1)

    def test_index_reuses_cached_entries_without_changing_lru_order(self) -> None:
        sessions = [_session("s1"), _session("s2")]
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = []
        runtime.load_conversation.side_effect = lambda session: [
            ConversationMessage("user", f"正文 {session['id']}", 1.0),
        ]
        store, _ = self._make_store(runtime=runtime)

        self.assertEqual(store.get_conversation(sessions[0])[0].text, "正文 s1")
        self.assertEqual(store.get_conversation(sessions[1])[0].text, "正文 s2")
        before = (
            list(store.conversations),
            store.conversations.total_bytes,
            runtime.load_conversation.call_count,
        )

        ConversationIndex().refresh(store, sessions)

        self.assertEqual(list(store.conversations), before[0])
        self.assertEqual(store.conversations.total_bytes, before[1])
        self.assertEqual(runtime.load_conversation.call_count, before[2])


if __name__ == "__main__":
    unittest.main()
