"""性能缓存与维护命令测试；全部使用临时目录，不碰真实用户缓存。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral.cache import PerformanceCache, history_signature
from corral.models import ConversationMessage


class PerformanceCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "cache.sqlite3"
        self.cache = PerformanceCache(self.path)
        self.env = mock.patch.dict(os.environ, {"CORRAL_CACHE": "1"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_session_cache_invalidates_on_append(self):
        history = Path(self.temp.name) / "session.jsonl"
        history.write_text("{}\n", encoding="utf-8")
        payload = {"source": "claude", "id": "abc", "live": False}
        self.cache.put_session("claude", str(history), payload)
        self.cache.flush_pending()
        self.assertEqual(self.cache.get_session("claude", str(history)), payload)
        with history.open("a", encoding="utf-8") as file:
            file.write("{}\n")
        self.assertIsNone(self.cache.get_session("claude", str(history)))

    def test_conversation_round_trip_and_clear_is_idempotent(self):
        history = Path(self.temp.name) / "session.jsonl"
        history.write_text("{}\n", encoding="utf-8")
        messages = [ConversationMessage("user", "你好", 123.0)]
        self.cache.put_conversation("claude", "claude:abc", str(history), messages)
        self.assertEqual(self.cache.get_conversation("claude", "claude:abc", str(history)), messages)
        self.assertEqual(self.cache.clear()["status"], "cleared")
        self.assertEqual(self.cache.clear()["status"], "unchanged")

    def test_conversation_cache_misses_when_sqlite_wal_grows(self):
        db = Path(self.temp.name) / "store.db"
        conn = sqlite3.connect(str(db))
        self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        conn.execute("INSERT INTO blobs VALUES ('a', ?)", (b"{}",))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        messages = [ConversationMessage("user", "旧", 1.0)]
        self.cache.put_conversation("cursor", "cursor:abc", str(db), messages)
        self.assertEqual(self.cache.get_conversation("cursor", "cursor:abc", str(db)), messages)
        before = history_signature(str(db))
        conn.execute("INSERT INTO blobs VALUES ('b', ?)", (b"{}",))
        conn.commit()
        self.assertNotEqual(history_signature(str(db)), before)
        self.assertIsNone(self.cache.get_conversation("cursor", "cursor:abc", str(db)))
        conn.close()

    def test_dry_run_does_not_change_database(self):
        history = Path(self.temp.name) / "session.jsonl"
        history.write_text("{}\n", encoding="utf-8")
        self.cache.put_session("claude", str(history), {"id": "abc"})
        self.cache.flush_pending()
        before = self.cache.status()
        result = self.cache.clear(dry_run=True)
        after = self.cache.status()
        self.assertEqual(result["status"], "would_clear")
        self.assertEqual(before["session_count"], after["session_count"])

    def test_corrupt_database_degrades_to_cache_miss(self):
        self.path.write_bytes("这不是 SQLite 数据库".encode())
        history = Path(self.temp.name) / "session.jsonl"
        history.write_text("{}\n", encoding="utf-8")
        broken = PerformanceCache(self.path)
        self.assertIsNone(broken.get_session("claude", str(history)))
        self.assertEqual(broken.status()["session_count"], 0)


class DefaultCacheLocationTests(unittest.TestCase):
    """The default instance exists from import; it must follow CORRAL_CACHE_DIR set later."""

    def test_default_instance_follows_cache_dir_set_after_creation(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            history = Path(first) / "session.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            messages = [ConversationMessage("user", "fixture", 1.0)]
            with mock.patch.dict(os.environ, {"CORRAL_CACHE": "1", "CORRAL_CACHE_DIR": first}):
                cache = PerformanceCache()
                self.assertEqual(cache.path, Path(first) / "performance-cache.sqlite3")
                cache.put_conversation("claude", "claude:abc", str(history), messages)
                os.environ["CORRAL_CACHE_DIR"] = second
                self.assertEqual(cache.path, Path(second) / "performance-cache.sqlite3")
                self.assertIsNone(cache.get_conversation("claude", "claude:abc", str(history)))
                cache.put_conversation("claude", "claude:abc", str(history), messages)
            self.assertTrue((Path(second) / "performance-cache.sqlite3").exists())

    def test_ui_test_import_order_does_not_pin_real_cache(self):
        with tempfile.TemporaryDirectory() as isolated:
            env = {k: v for k, v in os.environ.items() if not k.endswith("CACHE_DIR")}
            script = (
                "import os\n"
                "from corral import split_layout, cache\n"
                f"os.environ['CORRAL_CACHE_DIR'] = {isolated!r}\n"
                "print(cache.get_cache().path)\n"
            )
            out = subprocess.run(
                [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
            ).stdout.strip()
            self.assertEqual(out, str(Path(isolated) / "performance-cache.sqlite3"))


class StaleSessionPurgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cache.sqlite3"
        self.cache = PerformanceCache(self.path)
        self.env = mock.patch.dict(os.environ, {"CORRAL_CACHE": "1"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        PerformanceCache._last_stale_session_purge = 0.0
        self.addCleanup(setattr, PerformanceCache, "_last_stale_session_purge", 0.0)

    def test_purge_drops_stale_parser_rows_and_vanished_paths(self):
        live = Path(self.temp.name) / "live.jsonl"
        live.write_text("{}\n", encoding="utf-8")
        gone = Path(self.temp.name) / "gone.jsonl"
        gone.write_text("{}\n", encoding="utf-8")
        self.cache.put_session("claude", str(live), {"id": "live"})
        self.cache.put_session("claude", str(gone), {"id": "gone"})
        self.cache.flush_pending()
        gone.unlink()
        with self.cache._connect() as conn:
            assert conn is not None
            conn.execute(
                "UPDATE session_meta SET parser_version='ancient' WHERE path=?",
                (str(live),),
            )
            conn.commit()
        # flush_pending above already ran the hourly-bounded purge; reset so the
        # explicit call below exercises the real path.
        PerformanceCache._last_stale_session_purge = 0.0
        removed = self.cache.prune_stale_sessions()
        self.assertEqual(removed, 2)
        with self.cache._connect() as conn:
            assert conn is not None
            self.assertEqual(
                conn.execute("SELECT count(*) FROM session_meta").fetchone()[0], 0,
            )
        # Hourly throttle: immediate second call is a noop.
        self.assertEqual(self.cache.prune_stale_sessions(), 0)

    def test_purge_failure_degrades_silently(self):
        broken = PerformanceCache(Path(self.temp.name) / "no-such-dir" / "c.sqlite3")
        with mock.patch.object(
            PerformanceCache, "_connect", side_effect=OSError("disk gone"),
        ):
            self.assertEqual(broken.prune_stale_sessions(), 0)


class CacheCliTests(unittest.TestCase):
    def _run(self, *args: str):
        env = dict(os.environ)
        env["CORRAL_CACHE_DIR"] = self.temp.name
        return subprocess.run(
            [sys.executable, "-m", "corral", "cache", *args],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def test_status_json_uses_agent_envelope(self):
        result = self._run("status", "--json")
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload), {"ok", "data", "error", "meta"})
        self.assertTrue(payload["ok"])

    def test_usage_error_returns_two_without_hanging(self):
        result = self._run("unknown", "--json")
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(result.stderr, "")
