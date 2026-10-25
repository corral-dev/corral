"""Per-session transcript locks: parallel sessions, single-flight, safe paging.

The remote host used to parse every session behind one process-wide lock
(``SessionHub._transcript_io``). Opening a 65 MB Codex history froze every
other session and every control-plane read for the duration of the parse.
These tests pin the replacement contract with deterministic fake readers:
no large fixtures, no real parsing, no reliance on machine speed.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from corral.remote import richmsg, sessions, transcript_cache


class _History:
    """Shared backing store for the fake readers of one session."""

    def __init__(self, path: str | Path, messages: list[richmsg.RichMessage], window: int = 5) -> None:
        self.path = Path(path)
        self.messages = list(messages)
        self.path.write_text("".join(self._line(item) for item in self.messages), encoding="utf-8")
        self.window = window
        self.delay = 0.0
        self.parse_calls = 0
        self.poll_calls = 0
        self.earlier_calls = 0
        self.broken = False
        self.guard = threading.Lock()

    @staticmethod
    def _line(message: richmsg.RichMessage) -> str:
        return f"{message.seq}\t{message.role}\t{message.text}\n"

    def append(self, message: richmsg.RichMessage) -> None:
        """One new history line: the file signature must move with the message."""
        self.messages.append(message)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(self._line(message))

    def touch(self) -> None:
        """Make the file signature move without changing what is parsed."""
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("\n")

    def count_parse(self) -> None:
        with self.guard:
            self.parse_calls += 1

    def count_earlier(self) -> None:
        with self.guard:
            self.earlier_calls += 1

    def count_poll(self) -> None:
        with self.guard:
            self.poll_calls += 1


class _FakeReader:
    """Reader-compatible stub: only the surface ``SessionHub`` uses."""

    def __init__(self, session: dict, history: _History, barrier: threading.Barrier | None = None) -> None:
        self.session = dict(session)
        self.path = str(session.get("path") or "")
        self.runtime_id = str(session.get("source") or "")
        self.history = history
        self.barrier = barrier
        self.pos = len(history.messages)
        self.consumed = len(history.messages)
        self.seq = 0
        self.spans: list[tuple[float, float]] = []

    def _enter(self) -> None:
        if self.barrier is not None:
            try:
                self.barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                self.history.broken = True
        if self.history.delay:
            time.sleep(self.history.delay)

    def read_all(self, limit: int | None = None) -> list[richmsg.RichMessage]:
        started = time.monotonic()
        self.history.count_parse()
        try:
            self._enter()
            window = max(1, int(limit or self.history.window))
            start = max(0, len(self.history.messages) - window)
            self.pos = start
            self.consumed = len(self.history.messages)
            self.seq = self.history.messages[-1].seq if self.history.messages else 0
            return list(self.history.messages[start:])
        finally:
            self.spans.append((started, time.monotonic()))

    def poll(self) -> list[richmsg.RichMessage]:
        self.history.count_poll()
        new = list(self.history.messages[self.consumed:])
        self.consumed = len(self.history.messages)
        if new:
            self.seq = new[-1].seq
        return new

    def take_replacement(self) -> list[richmsg.RichMessage] | None:
        return None

    def has_earlier(self) -> bool:
        return self.pos > 0

    def read_earlier(self, limit: int, *, before_seq: int) -> list[richmsg.RichMessage]:
        self.history.count_earlier()
        delay = self.history.delay
        if delay:
            time.sleep(delay)
        start = max(0, self.pos - max(1, int(limit)))
        chunk = list(self.history.messages[start:self.pos])
        self.pos = start
        return chunk

    def export_state(self) -> dict:
        return {"pos": self.pos, "consumed": self.consumed, "seq": self.seq}

    def restore_state(self, state: dict, messages: list[richmsg.RichMessage]) -> None:
        self.pos = int(state.get("pos") or 0)
        self.consumed = int(state.get("consumed") or len(messages))
        self.seq = int(state.get("seq") or 0)


class _Store:
    """Minimal ``SessionStore`` stand-in for the hub's lookup helpers."""

    def __init__(self, items: list[dict]) -> None:
        self.by_key = {sessions.session_key(item): item for item in items}

    def find_session(self, key: str) -> dict | None:
        return self.by_key.get(key)

    def session_key_migrations(self) -> dict:
        return {}

    def all_sessions(self) -> list[dict]:
        return list(self.by_key.values())


def _session(directory: Path, name: str) -> dict:
    return {
        "source": "codex",
        "id": name,
        "short_id": name,
        "path": str(directory / f"{name}.jsonl"),
        "cwd": str(directory),
    }


def _messages(count: int, *, users: int = 0) -> list[richmsg.RichMessage]:
    out = []
    for index in range(1, count + 1):
        role = "user" if index % 3 == 1 and users else "assistant"
        out.append(richmsg.RichMessage(index, role, f"message {index}"))
    return out


class _HubCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def hub(self, session_list: list[dict], histories: dict[str, _History], *, barrier=None):
        hub = object.__new__(sessions.SessionHub)
        hub._lock = threading.Lock()
        hub._transcripts = {}
        hub._conversations = {}
        hub._transcript_locks = sessions._KeyedLocks()
        hub._transcript_cache = transcript_cache.TranscriptCache(self.directory / "cache.sqlite3")
        self.addCleanup(hub._transcript_cache.close)
        hub._on_event = lambda *_args, **_kwargs: None
        hub.store = _Store(session_list)
        hub._new_reader = lambda session: _FakeReader(
            session, histories[str(session["id"])], barrier
        )
        return hub


class ParallelSessionParsingTests(_HubCase):
    def test_two_sessions_parse_at_the_same_time(self) -> None:
        first, second = _session(self.directory, "first"), _session(self.directory, "second")
        first_history = _History(first["path"], _messages(20))
        second_history = _History(second["path"], _messages(20))
        first_history.delay = second_history.delay = 0.25
        barrier = threading.Barrier(2)
        hub = self.hub([first, second], {"first": first_history, "second": second_history},
                       barrier=barrier)

        errors: list[BaseException] = []

        def load(session: dict) -> None:
            try:
                hub._ensure_transcript(session)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        started = time.monotonic()
        threads = [threading.Thread(target=load, args=(session,)) for session in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
        elapsed = time.monotonic() - started

        self.assertEqual(errors, [])
        self.assertFalse(first_history.broken, "parses were serialized: one thread never entered")
        self.assertFalse(second_history.broken)
        self.assertEqual(first_history.parse_calls, 1)
        self.assertEqual(second_history.parse_calls, 1)
        # Each reader records its own read window; with a shared process-wide
        # lock they cannot overlap and the total is the sum of both parses.
        windows = [reader.spans for reader in (
            hub._transcripts["codex:first"].reader,
            hub._transcripts["codex:second"].reader,
        )]
        (a_start, a_end), = windows[0]
        (b_start, b_end), = windows[1]
        self.assertTrue(a_start < b_end and b_start < a_end, f"no overlap: {windows}")
        summed = (a_end - a_start) + (b_end - b_start)
        self.assertLess(elapsed, summed * 0.9, f"{elapsed:.3f}s for {summed:.3f}s of parsing")


class SingleFlightTests(_HubCase):
    def test_concurrent_open_of_one_session_parses_once(self) -> None:
        session = _session(self.directory, "shared")
        history = _History(session["path"], _messages(20))
        history.delay = 0.2
        hub = self.hub([session], {"shared": history})

        start = threading.Barrier(8)
        results: list[object] = []
        errors: list[BaseException] = []

        def load() -> None:
            try:
                start.wait(timeout=10)
                results.append(hub._ensure_transcript(session))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=load) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)

        self.assertEqual(errors, [])
        self.assertEqual(history.parse_calls, 1, "each waiter must reuse the first parse")
        self.assertEqual(len(results), 8)
        self.assertEqual(len({id(item) for item in results}), 1)
        self.assertEqual(hub._transcript_locks.tracked(), 0)


class CacheHydrationTests(_HubCase):
    """A cached transcript is reused without turning a cold open into a parse."""

    def cold(self, session, history):
        return self.hub([session], {str(session["id"]): history})

    def test_unchanged_file_is_not_polled_again(self) -> None:
        session = _session(self.directory, "cached")
        history = _History(session["path"], _messages(300), window=10)
        hub = self.cold(session, history)
        hub._ensure_transcript(session)
        self.assertEqual(history.parse_calls, 1)
        hub._transcript_cache.close()

        restarted = self.cold(session, history)
        transcript = restarted._ensure_transcript(session)
        self.assertEqual(history.parse_calls, 1, "cold open must not re-parse a cached file")
        self.assertEqual(
            history.poll_calls, 0,
            "an exact cache hit must not poll: SessKit readers would re-materialise",
        )
        self.assertEqual(
            [item.seq for item in transcript.messages], list(range(221, 301))
        )

    def test_device_number_change_is_not_a_cache_miss(self) -> None:
        # macOS reports another st_dev for the same inode across boots; the
        # rows written before that change must stay usable.
        import sqlite3

        session = _session(self.directory, "remounted")
        history = _History(session["path"], _messages(120), window=10)
        hub = self.cold(session, history)
        hub._ensure_transcript(session)
        self.assertEqual(history.parse_calls, 1)
        hub._transcript_cache.close()
        with sqlite3.connect(str(self.directory / "cache.sqlite3")) as conn:
            conn.execute("UPDATE transcript SET dev = dev + 1")
            conn.commit()

        restarted = self.cold(session, history)
        transcript = restarted._ensure_transcript(session)
        self.assertEqual(history.parse_calls, 1)
        self.assertEqual(history.poll_calls, 0)
        self.assertEqual(len(transcript.messages), 80)

    def test_grown_file_resumes_from_the_cached_cursor(self) -> None:
        session = _session(self.directory, "grown")
        history = _History(session["path"], _messages(120), window=10)
        hub = self.cold(session, history)
        hub._ensure_transcript(session)
        hub._transcript_cache.close()

        history.append(richmsg.RichMessage(121, "assistant", "message 121"))
        restarted = self.cold(session, history)
        transcript = restarted._ensure_transcript(session)

        self.assertEqual(history.parse_calls, 1, "growth must resume, not re-parse")
        self.assertEqual(history.poll_calls, 1)
        self.assertEqual(transcript.messages[-1].seq, 121)
        self.assertEqual([item.seq for item in transcript.messages], list(range(41, 122)))


class ChunkedPagingTests(_HubCase):
    def test_backfill_and_prompt_projection_keep_one_consistent_list(self) -> None:
        session = _session(self.directory, "paged")
        history = _History(session["path"], _messages(60, users=1), window=10)
        hub = self.hub([session], {"paged": history})
        key = sessions.session_key(session)
        hub._ensure_transcript(session)

        failures: list[str] = []
        stop = threading.Event()

        def page_back() -> None:
            while not stop.is_set():
                transcript = hub._ensure_transcript(session)
                oldest = min((item.seq for item in transcript.messages), default=1)
                response = hub.message_page(key, limit=10, before_seq=oldest + 1)
                if not transcript.reader.has_earlier():
                    return
                if not response["messages"]:
                    failures.append("empty page while history remained")

        def read_prompts() -> None:
            while not stop.is_set():
                response = hub.user_prompts(key)
                seqs = [row["seq"] for row in response["prompts"]]
                if seqs != sorted(seqs) or len(seqs) != len(set(seqs)):
                    failures.append(f"prompt rows out of order: {seqs}")
                    return
                if response["total"] != len(response["prompts"]):
                    failures.append("total disagrees with rows")
                    return
                if not hub._ensure_transcript(session).reader.has_earlier():
                    return

        threads = [threading.Thread(target=page_back), threading.Thread(target=read_prompts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        stop.set()

        self.assertEqual(failures, [])
        transcript = hub._ensure_transcript(session)
        seqs = [item.seq for item in transcript.messages]
        self.assertEqual(seqs, list(range(1, 61)), "backfill duplicated or dropped messages")
        self.assertFalse(transcript.reader.has_earlier())
        prompts = hub.user_prompts(key)
        self.assertEqual([row["seq"] for row in prompts["prompts"]],
                         [item.seq for item in transcript.messages if item.role == "user"])
        self.assertEqual(prompts["total"], len(prompts["prompts"]))


if __name__ == "__main__":
    unittest.main()
