"""``session.userPrompts`` projection cache.

The host used to page a whole history back to its first message on every cold
open just to list "Your prompts". The projection now lives beside the cached
transcript, so a repeat or cold open answers from that row. These tests use the
deterministic fake readers from ``test_remote_transcript_concurrency`` (no real
history parsing, no machine-speed assumptions beyond a relative comparison).
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from test_remote_transcript_concurrency import _History, _HubCase, _messages, _session

from corral.remote import sessions, transcript_cache
from corral.remote.richmsg import RichMessage


def _prompt_texts(response: dict) -> list[str]:
    return [row["text"] for row in response["prompts"]]


class _FlakyConnection:
    """Duck-typed proxy over a real sqlite3 handle for failure injection.

    Everything delegates to the real connection, so statements run inside
    real transactions against the real file; only an armed commit/execute
    raises. Needed because the C connection type is immutable (its methods
    cannot be monkeypatched), and it keeps the failure inside a genuine
    open transaction instead of a mocked writer.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self._fail_next_commit = 0
        self._fail_next_execute = 0

    def commit(self) -> None:
        if self._fail_next_commit > 0:
            self._fail_next_commit -= 1
            raise sqlite3.OperationalError("injected commit failure")
        self._real.commit()

    def execute(self, *args, **kwargs):
        if self._fail_next_execute > 0:
            self._fail_next_execute -= 1
            raise sqlite3.OperationalError("injected execute failure")
        return self._real.execute(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class PromptProjectionCacheTests(_HubCase):
    def setUp(self) -> None:
        super().setUp()
        self.session = _session(self.directory, "prompts")
        self.key = sessions.session_key(self.session)
        self.history = _History(self.session["path"], _messages(300, users=1), window=10)
        # Five backfill chunks (300 messages, an 80-message first window) so a
        # walk is observable and countable instead of a single 400-message read.
        self.chunk = mock.patch.object(sessions, "_PROMPT_FILL_CHUNK", 50)
        self.chunk.start()
        self.addCleanup(self.chunk.stop)

    def cold_hub(self):
        """A hub with nothing in memory: the cold-open / restart case."""
        return self.hub([self.session], {"prompts": self.history})

    def expected_rows(self) -> list[dict]:
        from corral.ui.session_hud import is_injected_user_prompt

        return [
            sessions._prompt_row(item)
            for item in self.history.messages
            if item.role == "user"
            and (item.text or "").strip()
            and not is_injected_user_prompt(item.text)
        ]

    def test_cold_open_after_restart_serves_the_cached_rows(self) -> None:
        first = self.cold_hub()
        cold = first.user_prompts(self.key)
        self.assertEqual(cold["prompts"], self.expected_rows())
        self.assertEqual(cold["total"], len(cold["prompts"]))
        self.assertEqual(self.history.earlier_calls, 5, "fixture should need a real walk")
        self.assertEqual(first._transcript_locks.tracked(), 0)

        after_restart = self.cold_hub()
        warm = after_restart.user_prompts(self.key)

        self.assertEqual(warm, cold, "wire shape and rows must not change")
        self.assertEqual(self.history.parse_calls, 1, "restart must not re-parse the history")
        self.assertEqual(
            self.history.earlier_calls, 5, "restart must not walk the history again"
        )

    def test_grown_history_extends_rows_without_a_fresh_walk(self) -> None:
        first = self.cold_hub()
        before = first.user_prompts(self.key)
        walked = self.history.earlier_calls
        first._transcript_cache.close()

        # The session kept writing while no transcript was in memory (daemon
        # restart, eviction). A restart must resume the cached transcript and
        # extend the projection; a cache miss here re-parses a tail window and
        # loses the full list, which is when "Your prompts" re-walks.
        self.history.append(sessions.richmsg.RichMessage(301, "user", "new ask"))

        after_restart = self.cold_hub()
        grown = after_restart.user_prompts(self.key)
        self.assertEqual(_prompt_texts(grown), _prompt_texts(before) + ["new ask"])
        self.assertEqual(grown["prompts"][-1]["seq"], 301)
        self.assertEqual(self.history.earlier_calls, walked, "growth needs no backfill")
        self.assertEqual(self.history.parse_calls, 1, "growth must resume, not re-parse")
        self.assertEqual(grown["total"], before["total"] + 1)

        # The extended projection is what the next cold process reads.
        third = self.cold_hub()
        again = third.user_prompts(self.key)
        self.assertEqual(again["prompts"], grown["prompts"])
        self.assertEqual(self.history.earlier_calls, walked)
        self.assertEqual(self.history.parse_calls, 1)

    def test_missing_projection_rebuilds_from_the_loaded_transcript(self) -> None:
        hub = self.cold_hub()
        expected = hub.user_prompts(self.key)
        walked = self.history.earlier_calls
        hub._transcript_cache.close()

        with sqlite3.connect(str(self.directory / "cache.sqlite3")) as conn:
            conn.execute("DELETE FROM session_prompts")
            conn.commit()

        after_restart = self.cold_hub()
        rebuilt = after_restart.user_prompts(self.key)
        self.assertEqual(rebuilt, expected)
        self.assertEqual(self.history.earlier_calls, walked)
        self.assertEqual(self.history.parse_calls, 1)

        # ... and the rebuilt projection is stored for the next process.
        third = self.cold_hub()
        self.assertEqual(third.user_prompts(self.key), expected)
        self.assertEqual(self.history.earlier_calls, walked)

    def test_projection_is_dropped_when_the_file_shrinks_or_generation_moves(self) -> None:
        hub = self.cold_hub()
        hub.user_prompts(self.key)
        rows = self.expected_rows()
        upto = max(item.seq for item in self.history.messages)
        generation = hub._ensure_transcript(self.session).generation
        cache = hub._transcript_cache

        self.assertEqual(
            cache.get_prompts("codex", self.key, str(self.history.path), generation),
            (rows, upto),
        )
        # Different generation: the seq space may have been reassigned.
        self.assertIsNone(
            cache.get_prompts("codex", self.key, str(self.history.path), generation + 1)
        )
        # Truncated / replaced file: never reuse a projection over a lost tail.
        self.history.path.write_text("", encoding="utf-8")
        self.assertIsNone(
            cache.get_prompts("codex", self.key, str(self.history.path), generation)
        )

    def test_uncovered_projection_walks_instead_of_serving_a_partial_list(self) -> None:
        hub = self.cold_hub()
        transcript = hub._ensure_transcript(self.session)
        self.assertTrue(transcript.reader.has_earlier(), "fixture must start on a tail window")
        stale = [{"seq": 1, "role": "user", "text": "message 1"}]
        with mock.patch.object(
            hub._transcript_cache, "get_prompts", return_value=(stale, 1)
        ):
            response = hub.user_prompts(self.key)
        self.assertEqual(response["prompts"], self.expected_rows())
        self.assertGreaterEqual(self.history.earlier_calls, 5, "must walk, never return a window")

    def test_warm_projection_beats_a_full_walk(self) -> None:
        # 15 ms per chunk makes the one-time walk visible without depending on
        # this machine's speed; the assertion itself is relative.
        self.history.delay = 0.015
        hub = self.cold_hub()
        started = time.perf_counter()
        cold = hub.user_prompts(self.key)
        cold_ms = (time.perf_counter() - started) * 1000.0
        self.assertGreaterEqual(self.history.earlier_calls, 5)
        hub._transcript_cache.close()

        # The session grew while no transcript was in memory: this is the cold
        # open the projection exists for. Without it the restart re-parses a
        # tail window and walks the whole history again.
        append = sessions.richmsg.RichMessage(301, "user", "new ask")
        self.history.append(append)
        expected = cold["prompts"] + [sessions._prompt_row(append)]

        samples: list[float] = []
        for _ in range(7):
            fresh = self.cold_hub()
            started = time.perf_counter()
            warm = fresh.user_prompts(self.key)
            samples.append((time.perf_counter() - started) * 1000.0)
            self.assertEqual(warm["prompts"], expected)
        samples.sort()
        warm_ms = samples[len(samples) // 2]

        # Relative first: absolute microseconds move with machine load.
        self.assertLess(warm_ms, cold_ms / 5, f"cold {cold_ms:.1f} ms vs warm {warm_ms:.1f} ms")
        self.assertLess(
            warm_ms, 5.0,
            f"warm projection took {warm_ms:.1f} ms (samples {[round(s, 2) for s in samples]})",
        )


class CacheStorageBoundaryTests(unittest.TestCase):
    """TranscriptCache read/write storage boundary (rounds 2-3).

    Synthetic temp paths only: no real histories, no machine-speed
    assertions. Pins that reads never build storage, that a schema-less
    file still repairs through writes, and that failed writes keep no
    transaction or lock behind. Round 3 adds the real exists/open race,
    genuine post-INSERT commit failures, repair failures after a repair
    transaction starts, retry-path failures, and new-handle cleanup after
    initialization failure.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def _history(self, name: str = "h") -> str:
        path = self.directory / f"{name}.jsonl"
        path.write_text("1\tuser\thello\n", encoding="utf-8")
        return str(path)

    def _cache(self, name: str = "cache.sqlite3") -> transcript_cache.TranscriptCache:
        cache = transcript_cache.TranscriptCache(self.directory / name)
        self.addCleanup(cache.close)
        return cache

    @staticmethod
    def _message(seq: int = 1, text: str = "hello") -> RichMessage:
        return RichMessage(seq, "user", text)

    @staticmethod
    def _state(seq: int = 1) -> dict:
        return {"pos": seq, "consumed": seq, "seq": seq}

    def _assert_no_writer_lock(self, target: Path) -> None:
        """A second native connection must take a write lock immediately."""
        probe = sqlite3.connect(str(target), timeout=0.5)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        finally:
            probe.close()

    def test_missing_cache_reads_miss_without_creating_a_file(self) -> None:
        history = self._history()
        target = self.directory / "cache.sqlite3"
        cache = self._cache()
        self.assertIsNone(cache.get("codex", "k", history))
        self.assertIsNone(cache.get_prompts("codex", "k", history, 1))
        self.assertFalse(target.exists(), "reads must not create the DB file")
        self.assertFalse(Path(str(target) + "-wal").exists())
        self.assertFalse(Path(str(target) + "-shm").exists())

    def test_file_deleted_before_open_misses_without_recreating(self) -> None:
        history = self._history()
        target = self.directory / "cache.sqlite3"
        cache = self._cache()
        cache.put("codex", "k", history, [self._message()], self._state(), 7)
        self.assertIsNotNone(cache.get("codex", "k", history))
        cache.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        reopened = transcript_cache.TranscriptCache(target)
        self.addCleanup(reopened.close)
        self.assertIsNone(reopened.get("codex", "k", history))
        self.assertIsNone(reopened.get_prompts("codex", "k", history, 7))
        self.assertFalse(target.exists(), "a deleted file must not come back on reads")

    def test_schema_less_file_misses_then_repairs(self) -> None:
        history = self._history()
        (self.directory / "cache.sqlite3").touch()  # existing file, no tables
        cache = self._cache()
        self.assertIsNone(cache.get("codex", "k", history))
        self.assertIsNone(cache.get_prompts("codex", "k", history, 7))
        real_init = transcript_cache.TranscriptCache._init_schema
        with mock.patch.object(
            transcript_cache.TranscriptCache, "_init_schema", wraps=real_init
        ) as counted:
            cache.put("codex", "k", history, [self._message()], self._state(), 7)
            cache.put_prompts(
                "codex", "k", history, 7,
                [{"seq": 1, "role": "user", "text": "hello"}], upto_seq=1,
            )
            self.assertEqual(counted.call_count, 1, "schema repair must run exactly once")
        hit = cache.get("codex", "k", history)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual([item.text for item in hit.messages], ["hello"])
        self.assertEqual(hit.generation, 7)
        prompts = cache.get_prompts("codex", "k", history, 7)
        self.assertIsNotNone(prompts)
        assert prompts is not None
        self.assertEqual(prompts[0], [{"seq": 1, "role": "user", "text": "hello"}])
        self.assertEqual(prompts[1], 1)

    def test_corrupt_cache_misses_gracefully(self) -> None:
        history = self._history()
        (self.directory / "cache.sqlite3").write_bytes(b"not a database" * 64)
        cache = self._cache()
        self.assertIsNone(cache.get("codex", "k", history))
        self.assertIsNone(cache.get_prompts("codex", "k", history, 7))
        # Writes against a corrupt file stay silent misses, never raise.
        cache.put("codex", "k", history, [self._message()], self._state(), 7)
        cache.put_prompts(
            "codex", "k", history, 7,
            [{"seq": 1, "role": "user", "text": "hello"}], upto_seq=1,
        )
        self.assertIsNone(cache.get("codex", "k", history))

    def test_locked_write_rolls_back_and_later_writes_work(self) -> None:
        history = self._history()
        target = self.directory / "cache.sqlite3"
        cache = self._cache()
        cache.put("codex", "k", history, [self._message()], self._state(), 7)
        self.assertIsNotNone(cache.get("codex", "k", history))
        holder = sqlite3.connect(str(target), timeout=5.0)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        real_init = transcript_cache.TranscriptCache._init_schema
        with mock.patch.object(
            transcript_cache.TranscriptCache, "_init_schema", wraps=real_init
        ) as counted:
            # Must not raise, and must not run schema repair for a lock error.
            cache.put("codex", "k", history, [self._message(2, "again")], self._state(2), 8)
            self.assertEqual(counted.call_count, 0, "lock errors must not trigger schema repair")
        live = cache._local.connection
        self.assertIsNotNone(live)
        assert live is not None
        self.assertFalse(live.in_transaction, "failed write must not keep a txn/lock")
        holder.rollback()
        holder.close()
        cache.put("codex", "k", history, [self._message(2, "again")], self._state(2), 8)
        hit = cache.get("codex", "k", history)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual([item.seq for item in hit.messages], [2])
        self.assertEqual(hit.generation, 8)

    def test_space_in_path_round_trips(self) -> None:
        sub = self.directory / "with space"
        sub.mkdir()
        history = sub / "h.jsonl"
        history.write_text("1\tuser\thello\n", encoding="utf-8")
        cache = transcript_cache.TranscriptCache(sub / "cache.sqlite3")
        self.addCleanup(cache.close)
        cache.put("codex", "k", str(history), [self._message()], self._state(), 7)
        cache.put_prompts(
            "codex", "k", str(history), 7,
            [{"seq": 1, "role": "user", "text": "hello"}], upto_seq=1,
        )
        # Fresh handle exercises the URI read path (quoting) on the space path.
        cache.close()
        hit = cache.get("codex", "k", str(history))
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual([item.text for item in hit.messages], ["hello"])
        self.assertEqual(
            cache.get_prompts("codex", "k", str(history), 7),
            ([{"seq": 1, "role": "user", "text": "hello"}], 1),
        )

    def test_exists_open_race_misses_without_recreating(self) -> None:
        """A file removed between exists() and the mode=rw open still misses.

        The older deleted-before-open test takes the exists() fast path and
        never reaches the URI open. Here a wrapper removes the DB after the
        check passes, so both reads must ride the race-safe open to a miss
        without recreating the main file.
        """
        history = self._history()
        target = self.directory / "cache.sqlite3"
        seed = self._cache()
        seed.put("codex", "k", history, [self._message()], self._state(), 7)
        seed.put_prompts(
            "codex", "k", history, 7,
            [{"seq": 1, "role": "user", "text": "hello"}], upto_seq=1,
        )
        self.assertIsNotNone(seed.get("codex", "k", history))
        seed.close()

        real_connect = transcript_cache.sqlite3.connect

        def racing_connect(*args, **kwargs):
            for suffix in ("", "-wal", "-shm"):
                Path(str(target) + suffix).unlink(missing_ok=True)
            return real_connect(*args, **kwargs)

        raced = transcript_cache.TranscriptCache(target)
        self.addCleanup(raced.close)
        with mock.patch.object(
            transcript_cache.sqlite3, "connect", side_effect=racing_connect
        ):
            self.assertIsNone(raced.get("codex", "k", history))
        self.assertFalse(target.exists(), "race must not recreate the main DB")
        raced.close()

        rebuilt = self._cache()
        rebuilt.put("codex", "k", history, [self._message()], self._state(), 7)
        rebuilt.put_prompts(
            "codex", "k", history, 7,
            [{"seq": 1, "role": "user", "text": "hello"}], upto_seq=1,
        )
        rebuilt.close()

        raced_prompts = transcript_cache.TranscriptCache(target)
        self.addCleanup(raced_prompts.close)
        with mock.patch.object(
            transcript_cache.sqlite3, "connect", side_effect=racing_connect
        ):
            self.assertIsNone(raced_prompts.get_prompts("codex", "k", history, 7))
        self.assertFalse(target.exists(), "race must not recreate the main DB")

    def test_init_failure_closes_new_handle(self) -> None:
        """A PRAGMA failure on open must close the new handle, not leak it.

        A leaked handle still answers SELECT with DatabaseError; a closed one
        raises ProgrammingError. The failed open must also leave nothing in
        the thread-local cache.
        """
        history = self._history()
        target = self.directory / "cache.sqlite3"
        target.write_bytes(b"not a database" * 64)
        cache = transcript_cache.TranscriptCache(target)
        self.addCleanup(cache.close)
        real_connect = transcript_cache.sqlite3.connect
        captured: list[sqlite3.Connection] = []

        def spy(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            captured.append(conn)
            return conn

        with mock.patch.object(transcript_cache.sqlite3, "connect", side_effect=spy):
            self.assertIsNone(cache.get("codex", "k", history))
        self.assertTrue(captured, "expected a real open attempt")
        new_handle = captured[-1]
        self.assertIsNone(
            getattr(cache._local, "connection", None),
            "failed init must not cache the handle",
        )
        with self.assertRaises(sqlite3.ProgrammingError):
            new_handle.execute("SELECT 1")
        new_handle.close()

    def test_put_commit_failure_rolls_back_and_recovers(self) -> None:
        """A commit failure after a real INSERT keeps no txn or lock."""
        history = self._history()
        target = self.directory / "cache.sqlite3"
        cache = self._cache()
        cache.put("codex", "k", history, [self._message()], self._state(), 7)
        self.assertIsNotNone(cache.get("codex", "k", history))
        real = cache._local.connection
        self.assertIsNotNone(real)
        assert real is not None
        proxy = _FlakyConnection(real)
        proxy._fail_next_commit = 1
        cache._local.connection = proxy
        try:
            cache.put("codex", "k", history, [self._message(2, "again")], self._state(2), 8)
        finally:
            cache._local.connection = real
        self.assertFalse(real.in_transaction, "failed commit must not keep a txn")
        self._assert_no_writer_lock(target)
        stale = cache.get("codex", "k", history)
        self.assertIsNotNone(stale)
        assert stale is not None
        self.assertEqual([item.seq for item in stale.messages], [1])
        cache.put("codex", "k", history, [self._message(2, "again")], self._state(2), 8)
        hit = cache.get("codex", "k", history)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual([item.seq for item in hit.messages], [2])
        self.assertEqual(hit.generation, 8)

    def test_put_prompts_commit_failure_rolls_back_and_recovers(self) -> None:
        """Same post-INSERT commit failure through the prompts projection."""
        history = self._history()
        target = self.directory / "cache.sqlite3"
        cache = self._cache()
        first_rows = [{"seq": 1, "role": "user", "text": "hello"}]
        cache.put_prompts("codex", "k", history, 7, first_rows, upto_seq=1)
        self.assertEqual(cache.get_prompts("codex", "k", history, 7), (first_rows, 1))
        real = cache._local.connection
        self.assertIsNotNone(real)
        assert real is not None
        proxy = _FlakyConnection(real)
        proxy._fail_next_commit = 1
        cache._local.connection = proxy
        try:
            cache.put_prompts(
                "codex", "k", history, 8,
                first_rows + [{"seq": 2, "role": "user", "text": "again"}], upto_seq=2,
            )
        finally:
            cache._local.connection = real
        self.assertFalse(real.in_transaction, "failed commit must not keep a txn")
        self._assert_no_writer_lock(target)
        self.assertEqual(cache.get_prompts("codex", "k", history, 7), (first_rows, 1))
        second_rows = first_rows + [{"seq": 2, "role": "user", "text": "again"}]
        cache.put_prompts("codex", "k", history, 8, second_rows, upto_seq=2)
        self.assertEqual(cache.get_prompts("codex", "k", history, 8), (second_rows, 2))

    def test_schema_repair_failure_rolls_back_and_recovers(self) -> None:
        """A repair that starts a real transaction then fails still rolls back."""
        history = self._history()
        target = self.directory / "cache.sqlite3"
        target.touch()  # existing file, no tables
        cache = self._cache()
        self.assertIsNone(cache.get("codex", "k", history))
        live = cache._local.connection
        self.assertIsNotNone(live)
        assert live is not None
        real_init = transcript_cache.TranscriptCache._init_schema

        def flaky_init(conn) -> None:
            inner = conn._real if isinstance(conn, _FlakyConnection) else conn
            real_init(inner)  # genuine repair, committed ...
            inner.execute(  # ... then genuine new work in a fresh txn ...
                "INSERT OR REPLACE INTO transcript(runtime, session_key, path, dev, ino,"
                " size, mtime_ns, parser_version, generation, reader_state, payload,"
                " accessed_at) VALUES('junk','junk','junk',0,0,0,0,'junk',0,'{}','[]',0.0)"
            )
            raise sqlite3.OperationalError("injected schema failure after txn start")

        with mock.patch.object(
            transcript_cache.TranscriptCache, "_init_schema", side_effect=flaky_init
        ):
            cache.put("codex", "k", history, [self._message()], self._state(), 7)
        self.assertFalse(live.in_transaction, "failed repair must not keep a txn")
        self._assert_no_writer_lock(target)
        helper = sqlite3.connect(str(target), timeout=5.0)
        try:
            junk = helper.execute(
                "SELECT count(*) FROM transcript WHERE runtime='junk'"
            ).fetchone()
        finally:
            helper.close()
        self.assertEqual(junk, (0,), "partial repair write must roll back")
        cache.put("codex", "k", history, [self._message()], self._state(), 7)
        hit = cache.get("codex", "k", history)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual([item.text for item in hit.messages], ["hello"])

        # Prompts leg: a plain repair failure on the other table.
        dropper = sqlite3.connect(str(target), timeout=5.0)
        try:
            dropper.execute("DROP TABLE IF EXISTS session_prompts")
            dropper.commit()
        finally:
            dropper.close()
        with mock.patch.object(
            transcript_cache.TranscriptCache,
            "_init_schema",
            side_effect=sqlite3.OperationalError("injected repair failure"),
        ):
            cache.put_prompts(
                "codex", "k", history, 7,
                [{"seq": 1, "role": "user", "text": "hello"}], upto_seq=1,
            )
        self.assertFalse(live.in_transaction, "failed repair must not keep a txn")
        self._assert_no_writer_lock(target)
        self.assertIsNone(cache.get_prompts("codex", "k", history, 7))
        cache.put_prompts(
            "codex", "k", history, 7,
            [{"seq": 1, "role": "user", "text": "hello"}], upto_seq=1,
        )
        self.assertEqual(
            cache.get_prompts("codex", "k", history, 7),
            ([{"seq": 1, "role": "user", "text": "hello"}], 1),
        )

    def test_put_retry_commit_failure_rolls_back_and_recovers(self) -> None:
        """Repair succeeds but the retry commit fails: still no txn or lock."""
        history = self._history()
        target = self.directory / "cache.sqlite3"
        target.touch()  # schema-less: first INSERT misses, repair runs, retry follows
        cache = self._cache()
        self.assertIsNone(cache.get("codex", "k", history))
        real = cache._local.connection
        self.assertIsNotNone(real)
        assert real is not None
        proxy = _FlakyConnection(real)
        cache._local.connection = proxy
        real_init = transcript_cache.TranscriptCache._init_schema

        def init_then_arm(conn) -> None:
            inner = conn._real if isinstance(conn, _FlakyConnection) else conn
            real_init(inner)
            conn._fail_next_commit = 1  # only the retry commit raises

        try:
            with mock.patch.object(
                transcript_cache.TranscriptCache, "_init_schema", side_effect=init_then_arm
            ):
                cache.put("codex", "k", history, [self._message()], self._state(), 7)
        finally:
            cache._local.connection = real
        self.assertFalse(real.in_transaction, "failed retry commit must not keep a txn")
        self._assert_no_writer_lock(target)
        self.assertIsNone(cache.get("codex", "k", history))
        cache.put("codex", "k", history, [self._message()], self._state(), 7)
        hit = cache.get("codex", "k", history)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual([item.text for item in hit.messages], ["hello"])
        self.assertEqual(hit.generation, 7)

    def test_put_prompts_retry_execute_failure_rolls_back_and_recovers(self) -> None:
        """Repair succeeds but the retry execute fails: still no txn or lock."""
        history = self._history()
        target = self.directory / "cache.sqlite3"
        target.touch()  # schema-less: first INSERT misses, repair runs, retry follows
        cache = self._cache()
        self.assertIsNone(cache.get_prompts("codex", "k", history, 7))
        real = cache._local.connection
        self.assertIsNotNone(real)
        assert real is not None
        proxy = _FlakyConnection(real)
        cache._local.connection = proxy
        real_init = transcript_cache.TranscriptCache._init_schema

        def init_then_arm(conn) -> None:
            inner = conn._real if isinstance(conn, _FlakyConnection) else conn
            real_init(inner)
            conn._fail_next_execute = 1  # only the retry execute raises

        try:
            with mock.patch.object(
                transcript_cache.TranscriptCache, "_init_schema", side_effect=init_then_arm
            ):
                cache.put_prompts(
                    "codex", "k", history, 7,
                    [{"seq": 1, "role": "user", "text": "hello"}], upto_seq=1,
                )
        finally:
            cache._local.connection = real
        self.assertFalse(real.in_transaction, "failed retry execute must not keep a txn")
        self._assert_no_writer_lock(target)
        self.assertIsNone(cache.get_prompts("codex", "k", history, 7))
        cache.put_prompts(
            "codex", "k", history, 7,
            [{"seq": 1, "role": "user", "text": "hello"}], upto_seq=1,
        )
        self.assertEqual(
            cache.get_prompts("codex", "k", history, 7),
            ([{"seq": 1, "role": "user", "text": "hello"}], 1),
        )


if __name__ == "__main__":
    unittest.main()
