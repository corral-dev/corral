"""Byte-weighted remote transcript retention and global read admission.

The remote host used to cap in-memory transcripts by entry count only (48).
A single 60 MB native history projected into typed messages, full tool bodies
and a materialized reader generation could keep far more than its wire page;
after the phone stopped watching, that heavyweight state stayed resident.
These tests pin the replacement policy:

* inactive transcripts are evicted by real recency and retained weight;
* watched conversations are protected, and ending the last watch releases
  reconstructible heavyweight reader state without touching cursors/seqs;
* an evicted transcript still reconstructs exact history, backpages, tool
  details, appends and resets through SessKit and the persisted cache;
* concurrent same-key loads parse once, different keys stay independent;
* history/image/search reads share one process-wide admission budget while
  input/control frames stay immediate.

All fixtures are synthetic JSONL in an isolated cache/state directory; no real
history or product cache is read or written.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from corral.remote import protocol, ratelimit, richmsg
from corral.remote import service as service_module
from corral.remote import sessions as remote_sessions
from corral.remote.service import Connection, RemoteService
from corral.remote.sessions import SessionHub


def _write_claude(path: Path, texts: list[str]) -> None:
    lines = []
    for index, text in enumerate(texts, start=1):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": f"a{index}",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _append_claude(path: Path, text: str, *, index: int) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": f"a{index}",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def _session(session_id: str, path: Path) -> dict:
    return {
        "source": "claude",
        "id": session_id,
        "short_id": session_id,
        "cwd": "/tmp/project",
        "cwd_display": "/tmp/project",
        "mtime": 1_700_000_000.0,
        "display_time": "01-01 12:00",
        "size_kb": 1.0,
        "status_tag": "ended",
        "live": False,
        "keepalive_name": None,
        "fallback_title": session_id,
        "attention_kind": "none",
        "last_user_msg": "",
        "last_agent_msg": "",
        "path": str(path),
    }


class _HubFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        env = mock.patch.dict(
            os.environ,
            {
                "CORRAL_CACHE": "1",
                "CORRAL_CACHE_DIR": str(root / "cache"),
                "CORRAL_STATE_DIR": str(root / "state"),
                "CORRAL_ISOLATE_MANAGED_HOSTS": "1",
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        self.sessions: dict[str, dict] = {}
        self.hub = SessionHub(scan_limit=10)
        self.addCleanup(self.hub.stop)
        patcher = mock.patch.object(
            self.hub,
            "require_session",
            side_effect=lambda key: self.sessions[key],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _add(self, session_id: str, texts: list[str]) -> dict:
        path = Path(self._tmp.name) / f"{session_id}.jsonl"
        _write_claude(path, texts)
        session = _session(session_id, path)
        self.sessions[f"claude:{session_id}"] = session
        return session

    def _keys(self) -> set[str]:
        return set(self.hub._transcripts)


class CountAndRecencyTests(_HubFixture):
    def test_count_budget_evicts_least_recently_used_inactive_entry(self) -> None:
        for name in ("one", "two", "three", "four"):
            self._add(name, [f"{name}-1", f"{name}-2"])
        with (
            mock.patch.object(remote_sessions, "_MAX_IN_MEMORY_TRANSCRIPTS", 2),
            mock.patch.object(
                remote_sessions, "_MAX_IN_MEMORY_TRANSCRIPT_BYTES", 1_000_000_000
            ),
            mock.patch.object(remote_sessions, "_IDLE_READER_RELEASE_BYTES", 10**12),
        ):
            self.hub.message_page("claude:one")
            self.hub.message_page("claude:two")
            self.hub.message_page("claude:three")
            self.assertEqual(self._keys(), {"claude:two", "claude:three"})

            # Genuine recency, not insertion order: touching `two` before the
            # next admission means `three` is now the oldest inactive entry.
            self.hub.message_page("claude:two")
            self.hub.message_page("claude:four")
            self.assertEqual(self._keys(), {"claude:two", "claude:four"})

    def test_byte_budget_evicts_oldest_and_keeps_single_oversized_entry(self) -> None:
        self._add("big-one", ["x" * 40_000])
        self._add("big-two", ["y" * 40_000])
        with (
            mock.patch.object(remote_sessions, "_MAX_IN_MEMORY_TRANSCRIPTS", 48),
            mock.patch.object(remote_sessions, "_MAX_IN_MEMORY_TRANSCRIPT_BYTES", 20_000),
            mock.patch.object(remote_sessions, "_IDLE_READER_RELEASE_BYTES", 10**12),
        ):
            self.hub.message_page("claude:big-one")
            # One oversized entry must remain readable; it is not immediately
            # ejected and reparsed on every page request.
            self.assertIn("claude:big-one", self._keys())
            self.assertGreater(
                self.hub._transcripts["claude:big-one"].bytes_estimate, 20_000
            )

            self.hub.message_page("claude:big-two")
            self.assertEqual(self._keys(), {"claude:big-two"})

    def test_estimate_includes_full_tool_bodies_and_reader_generation(self) -> None:
        message = richmsg.RichMessage(
            1,
            "assistant",
            "done",
            tools=[
                richmsg.ToolCall(
                    call_id="tool-1",
                    name="Bash",
                    kind="shell",
                    summary="run",
                    detail="d" * 4_000,
                    output="o" * 2_000,
                )
            ],
        )
        tool_bytes = richmsg.estimate_message_bytes(message)
        self.assertGreaterEqual(tool_bytes, 6_000)
        self.assertGreater(
            tool_bytes,
            richmsg.estimate_message_bytes(
                richmsg.RichMessage(1, "assistant", "done")
            ),
        )

        reader = richmsg.RichReader({})
        reader._cc_full = [
            richmsg.RichMessage(2, "assistant", "extra " * 100),
        ]
        reader._cc_fps = {2: ("assistant", "extra " * 100, ())}
        wire = mock.Mock()
        wire.event_total = 500
        reader._cc_reader = wire
        retained = richmsg.estimate_reader_bytes(reader, [message])
        # The extra projected message, its fingerprint and the coarse native
        # event materialization are all part of the policy estimate.
        self.assertGreater(retained, 500 * 1024)
        reader.session = {"size_bytes": 10 * 1024 * 1024, "source": "pi"}
        self.assertGreaterEqual(
            richmsg.estimate_reader_bytes(reader, [message]), 10 * 1024 * 1024
        )


class WatchProtectionTests(_HubFixture):
    def test_watched_entry_survives_budget_and_unwatch_releases_it(self) -> None:
        for name in ("watched", "filler", "later"):
            self._add(name, [f"{name}-1"])
        with (
            mock.patch.object(remote_sessions, "_MAX_IN_MEMORY_TRANSCRIPTS", 1),
            mock.patch.object(
                remote_sessions, "_MAX_IN_MEMORY_TRANSCRIPT_BYTES", 1_000_000_000
            ),
            mock.patch.object(remote_sessions, "_IDLE_READER_RELEASE_BYTES", 10**12),
        ):
            self.hub.watch_conversation("claude:watched")
            self.hub.watch_conversation("claude:watched")
            self.hub.message_page("claude:filler")
            # Count is over budget, but the only unprotected entry is the one
            # just admitted; watched work is never evicted to satisfy a cache.
            self.assertEqual(
                self._keys(), {"claude:watched", "claude:filler"}
            )

            self.hub.unwatch_conversation("claude:watched")
            self.assertEqual(
                self.hub._conversations["claude:watched"].watchers, 1
            )
            self.hub.unwatch_conversation("claude:watched")
            self.hub.message_page("claude:later")
            # Once the last watch ends the entry is eligible again; `watched`
            # is older than the new admission, so it is the released one.
            self.assertNotIn("claude:watched", self._keys())
            self.assertIn("claude:later", self._keys())

    def test_watch_readmits_transcript_evicted_before_subscription(self) -> None:
        self._add("race", ["race-1"])
        original = self.hub._ensure_transcript

        def ensure_then_evict(session):
            transcript = original(session)
            with self.hub._lock:
                self.hub._transcripts.pop(transcript.key, None)
            return transcript

        with mock.patch.object(
            self.hub, "_ensure_transcript", side_effect=ensure_then_evict
        ):
            page = self.hub.watch_conversation("claude:race")
        self.assertEqual([item["text"] for item in page["messages"]], ["race-1"])
        self.assertIn("claude:race", self._keys())
        watch = self.hub._conversations["claude:race"]
        self.assertIs(watch.reader, self.hub._transcripts["claude:race"].reader)
        self.hub.unwatch_conversation("claude:race")

    def test_unwatch_skips_release_while_a_page_holds_the_key(self) -> None:
        self._add("busy", [f"message {index} " + "z" * 200 for index in range(1, 301)])
        with mock.patch.object(remote_sessions, "_IDLE_READER_RELEASE_BYTES", 0):
            self.hub.watch_conversation("claude:busy")
            transcript = self.hub._transcripts["claude:busy"]
            parser_started = threading.Event()
            release_parser = threading.Event()

            def hold_key() -> None:
                with self.hub._transcript_locks.hold("claude:busy"):
                    parser_started.set()
                    release_parser.wait(5.0)

            worker = threading.Thread(target=hold_key)
            worker.start()
            self.assertTrue(parser_started.wait(2.0))
            started = time.perf_counter()
            self.hub.unwatch_conversation("claude:busy")
            elapsed = time.perf_counter() - started
            release_parser.set()
            worker.join(5.0)

            self.assertLess(elapsed, 0.5)
            self.assertTrue(transcript.reader.has_cached_reader_state())
            # Once the page finishes, the next idle-release opportunity frees it.
            self.hub._release_idle_reader("claude:busy")
            self.assertFalse(transcript.reader.has_cached_reader_state())

    def test_unwatch_releases_heavy_reader_state_without_moving_cursor(self) -> None:
        self._add("heavy", [f"message {index} " + "z" * 200 for index in range(1, 301)])
        with mock.patch.object(remote_sessions, "_IDLE_READER_RELEASE_BYTES", 0):
            self.hub.watch_conversation("claude:heavy")
            transcript = self.hub._transcripts["claude:heavy"]
            reader = transcript.reader
            before_state = reader.export_state()
            before_messages = [(item.seq, item.text) for item in transcript.messages]
            before_bytes = transcript.bytes_estimate
            self.assertTrue(reader.has_cached_reader_state())

            self.hub.unwatch_conversation("claude:heavy")
            self.assertFalse(reader.has_cached_reader_state())
            self.assertEqual(reader.export_state(), before_state)
            self.assertEqual(
                [(item.seq, item.text) for item in transcript.messages],
                before_messages,
            )
            self.assertLess(transcript.bytes_estimate, before_bytes)

            # The released reader still serves the cached window and rebuilds
            # complete history for a backward page.
            tail = self.hub.message_page("claude:heavy")
            self.assertEqual(len(tail["messages"]), 80)
            earlier = self.hub.message_page(
                "claude:heavy", before_seq=tail["oldest_seq"]
            )
            self.assertEqual(len(earlier["messages"]), 80)
            self.assertLess(earlier["messages"][-1]["seq"], tail["oldest_seq"])


class ReconstructionTests(_HubFixture):
    def test_huge_history_reconstructs_after_eviction_and_exact_cache_hit(self) -> None:
        total = 600
        self._add("huge", [f"tail-{index}" for index in range(total)])
        self._add("other", ["other-1"])
        with mock.patch.object(remote_sessions, "_MAX_IN_MEMORY_TRANSCRIPTS", 1):
            first = self.hub.message_page("claude:huge")
            first_state = self.hub._transcripts["claude:huge"].reader.export_state()
            self.assertEqual([item["text"] for item in first["messages"]][-1], f"tail-{total - 1}")
            self.assertEqual(len(first["messages"]), 80)
            self.assertTrue(first["has_more"])
            self.hub.message_page("claude:other")
            self.assertNotIn("claude:huge", self._keys())

            hub2 = SessionHub(scan_limit=10)
            self.addCleanup(hub2.stop)
            with mock.patch.object(hub2, "require_session", side_effect=self.sessions.__getitem__):
                original = richmsg.RichReader.read_all
                reads = {"count": 0}

                def counting(reader, *args, **kwargs):
                    reads["count"] += 1
                    return original(reader, *args, **kwargs)

                with mock.patch.object(richmsg.RichReader, "read_all", counting):
                    restored = hub2.message_page("claude:huge")
                    restored_state = hub2._transcripts["claude:huge"].reader.export_state()
                    earlier = hub2.message_page(
                        "claude:huge", before_seq=restored["oldest_seq"]
                    )
                self.assertEqual(reads["count"], 0)
                self.assertEqual(restored["generation"], first["generation"])
                self.assertEqual(restored["messages"], first["messages"])
                self.assertEqual(restored_state, first_state)
                self.assertEqual(len(earlier["messages"]), 80)
                self.assertLess(earlier["messages"][-1]["seq"], restored["oldest_seq"])

    def test_huge_history_reconstructs_without_persisted_cache(self) -> None:
        total = 400
        self._add("nocache", [f"row-{index}" for index in range(total)])
        self._add("other", ["other-1"])
        with mock.patch.object(remote_sessions, "_MAX_IN_MEMORY_TRANSCRIPTS", 1):
            first = self.hub.message_page("claude:nocache")
            self.hub.message_page("claude:other")
            self.assertNotIn("claude:nocache", self._keys())
            with mock.patch.dict(os.environ, {"CORRAL_CACHE": "0"}):
                rebuilt = self.hub.message_page("claude:nocache")
                earlier = self.hub.message_page(
                    "claude:nocache", before_seq=rebuilt["oldest_seq"]
                )
        self.assertEqual([item["text"] for item in rebuilt["messages"]], [
            item["text"] for item in first["messages"]
        ])
        self.assertEqual(
            [item["seq"] for item in rebuilt["messages"]],
            [item["seq"] for item in first["messages"]],
        )
        self.assertEqual(len(earlier["messages"]), 80)
        self.assertLess(earlier["messages"][-1]["seq"], rebuilt["oldest_seq"])

    def test_append_after_eviction_delivers_new_tail_without_duplicates(self) -> None:
        self._add("append", [f"m-{index}" for index in range(1, 201)])
        self._add("other", ["other-1"])
        with mock.patch.object(remote_sessions, "_MAX_IN_MEMORY_TRANSCRIPTS", 1):
            first = self.hub.message_page("claude:append")
            self.hub.message_page("claude:other")
            self.assertNotIn("claude:append", self._keys())

            path = Path(self.sessions["claude:append"]["path"])
            _append_claude(path, "appended", index=201)
            page = self.hub.message_page("claude:append")
            texts = [item["text"] for item in page["messages"]]
            self.assertEqual(texts[-1], "appended")
            self.assertEqual(page["generation"], first["generation"])
            self.assertEqual(len(page["messages"]), 80)
            self.assertEqual(len(set(item["seq"] for item in page["messages"])), 80)

    def test_tool_detail_and_pending_result_survive_eviction(self) -> None:
        path = Path(self._tmp.name) / "tool.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "changing a file"},
                            {
                                "type": "tool_use",
                                "id": "toolu_1",
                                "name": "Bash",
                                "input": {"command": "echo hello"},
                            },
                        ],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.sessions["claude:tool"] = _session("tool", path)
        self._add("other", ["other-1"])
        with mock.patch.object(remote_sessions, "_MAX_IN_MEMORY_TRANSCRIPTS", 1):
            page = self.hub.message_page("claude:tool")
            seq = page["messages"][0]["seq"]
            running = self.hub.tool_detail("claude:tool", seq=seq)
            self.assertIn("echo hello", running["tools"][0].get("detail", ""))

            self.hub.message_page("claude:other")
            self.assertNotIn("claude:tool", self._keys())

            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "user",
                            "uuid": "u1",
                            "message": {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "toolu_1",
                                        "content": "hello\n",
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            reloaded = self.hub.message_page("claude:tool")
            self.assertEqual([item["seq"] for item in reloaded["messages"]][0], seq)
            detail = self.hub.tool_detail("claude:tool", seq=seq)
            self.assertTrue(detail["tools"][0]["has_detail"])
            self.assertIn("hello", detail["tools"][0].get("output", ""))

    def test_pending_question_projection_survives_eviction(self) -> None:
        path = Path(self._tmp.name) / "question.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "question-1",
                                "name": "AskUserQuestion",
                                "input": {
                                    "questions": [
                                        {
                                            "question": "Which target?",
                                            "header": "Target",
                                            "options": [
                                                {"label": "staging"},
                                                {"label": "production"},
                                            ],
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.sessions["claude:question"] = _session("question", path)
        self._add("other", ["other-1"])
        with mock.patch.object(remote_sessions, "_MAX_IN_MEMORY_TRANSCRIPTS", 1):
            prompts = self.hub.prompts("claude:question")
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["options"], ["staging", "production"])
            self.hub.message_page("claude:other")
            self.assertNotIn("claude:question", self._keys())
            reloaded = self.hub.prompts("claude:question")
        self.assertEqual(len(reloaded), 1)
        self.assertEqual(reloaded[0]["request_id"], prompts[0]["request_id"])
        self.assertEqual(reloaded[0]["options"], ["staging", "production"])

    def test_history_reset_after_eviction_replaces_generation(self) -> None:
        session = self._add("reset", ["old-1", "old-2", "old-3"])
        self._add("other", ["other-1"])
        with mock.patch.object(remote_sessions, "_MAX_IN_MEMORY_TRANSCRIPTS", 1):
            first = self.hub.message_page("claude:reset")
            self.hub.message_page("claude:other")
            self.assertNotIn("claude:reset", self._keys())

            _write_claude(Path(session["path"]), ["new-1"])
            page = self.hub.message_page("claude:reset")
            self.assertEqual([item["text"] for item in page["messages"]], ["new-1"])
            self.assertNotEqual(page["generation"], first["generation"])


class ConcurrencyTests(_HubFixture):
    def test_same_key_concurrent_loads_parse_once(self) -> None:
        self._add("same", ["only-1", "only-2"])
        started = threading.Event()
        release = threading.Event()
        calls = {"count": 0}
        count_lock = threading.Lock()
        original = richmsg.RichReader.read_all

        def slow(reader, *args, **kwargs):
            with count_lock:
                calls["count"] += 1
            started.set()
            release.wait(5.0)
            return original(reader, *args, **kwargs)

        results: list[dict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def load() -> None:
            try:
                page = self.hub.message_page("claude:same")
                with lock:
                    results.append(page)
            except BaseException as exc:  # pragma: no cover - reported below
                with lock:
                    errors.append(exc)

        with mock.patch.object(richmsg.RichReader, "read_all", slow):
            threads = [threading.Thread(target=load) for _ in range(2)]
            for thread in threads:
                thread.start()
            self.assertTrue(started.wait(2.0))
            release.set()
            for thread in threads:
                thread.join(5.0)
        self.assertFalse(errors)
        self.assertEqual(calls["count"], 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["messages"], results[1]["messages"])

    def test_different_keys_load_in_parallel(self) -> None:
        self._add("left", ["left-1"])
        self._add("right", ["right-1"])
        barrier = threading.Barrier(2, timeout=5.0)
        original = richmsg.RichReader.read_all

        def rendezvous(reader, *args, **kwargs):
            barrier.wait()
            return original(reader, *args, **kwargs)

        errors: list[BaseException] = []

        def load(key: str) -> None:
            try:
                self.hub.message_page(key)
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

        with mock.patch.object(richmsg.RichReader, "read_all", rendezvous):
            threads = [
                threading.Thread(target=load, args=(key,))
                for key in ("claude:left", "claude:right")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10.0)
        self.assertFalse(errors)
        self.assertEqual(
            [item["text"] for item in self.hub.message_page("claude:left")["messages"]],
            ["left-1"],
        )
        self.assertEqual(
            [item["text"] for item in self.hub.message_page("claude:right")["messages"]],
            ["right-1"],
        )


class _BlockingHub:
    """Minimal hub for the shipped dispatch gate; history reads block."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.max_active = 0
        self.sent_text: list[tuple[str, str, bool]] = []
        self._lock = threading.Lock()

    def message_page(self, key: str, *, limit: int = 80, before_seq=None) -> dict:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.started.set()
            self.release.wait(5.0)
            return {
                "messages": [],
                "oldest_seq": 0,
                "newest_seq": 0,
                "generation": 1,
                "has_more": False,
            }
        finally:
            with self._lock:
                self.active -= 1

    def send_text(self, key: str, text: str, submit: bool) -> None:
        self.sent_text.append((key, text, submit))


class GlobalReadAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(
            os.environ,
            {
                "CORRAL_CACHE_DIR": tmp.name,
                "CORRAL_STATE_DIR": tmp.name,
                "CORRAL_ISOLATE_MANAGED_HOSTS": "1",
            },
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)
        ratelimit.INPUT_ACTIONS.reset()
        self.addCleanup(ratelimit.INPUT_ACTIONS.reset)
        self.addCleanup(ratelimit.TERMINAL_TYPING.reset)
        self.hub = _BlockingHub()
        with mock.patch.object(service_module, "_MAX_GLOBAL_HISTORY_READS", 2):
            self.service = RemoteService(self.hub)  # type: ignore[arg-type]
        for target, value in (("is_known_device", True), ("device_access", "full")):
            patcher = mock.patch.object(self.service, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.hub.release.set)
        self.responses: list[dict] = []
        self.lock = threading.Lock()

    def _connection(self) -> Connection:
        connection = Connection("aa" * 32, self._record)
        connection.paired = True
        return connection

    def _record(self, message: dict) -> None:
        with self.lock:
            self.responses.append(message)

    def _send(self, connection: Connection, req_id: int, method: str, params: dict) -> None:
        self.service.dispatch(connection, protocol.request(req_id, method, params))

    def _wait_response(self, req_id: int, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                for message in self.responses:
                    if message.get("t") == "res" and message.get("id") == req_id:
                        return message
            time.sleep(0.01)
        self.fail(f"no response for {req_id}: {self.responses}")

    def _wait_started(self, count: int, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.hub._lock:
                if self.hub.active >= count:
                    return
            time.sleep(0.01)
        self.fail("history reads did not start")

    def test_global_saturation_is_retryable_and_control_stays_immediate(self) -> None:
        first = self._connection()
        second = self._connection()
        self._send(first, 1, protocol.M_SESSION_MESSAGES, {"key": "claude:one"})
        self._send(second, 2, protocol.M_SESSION_MESSAGES, {"key": "claude:two"})
        self._wait_started(2)

        started = time.perf_counter()
        self._send(first, 3, protocol.M_SESSION_MESSAGES, {"key": "claude:three"})
        rejected = self._wait_response(3, timeout=1.0)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertFalse(rejected["ok"], rejected)
        self.assertEqual(rejected["e"]["code"], protocol.E_UNAVAILABLE)

        # Input/control is not in the history gate and must not wait for the
        # two blocked parses.
        started = time.perf_counter()
        self._send(first, 4, protocol.M_INPUT_TEXT, {"key": "claude:one", "text": "hi"})
        typed = self._wait_response(4, timeout=1.0)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertTrue(typed["ok"], typed)
        self.assertEqual(self.hub.sent_text, [("claude:one", "hi", True)])
        with self.lock:
            self.assertNotIn(1, [m.get("id") for m in self.responses if m.get("t") == "res"])
            self.assertNotIn(2, [m.get("id") for m in self.responses if m.get("t") == "res"])
        self.assertEqual(self.hub.max_active, 2)

        self.hub.release.set()
        self.assertTrue(self._wait_response(1)["ok"])
        self.assertTrue(self._wait_response(2)["ok"])

        # Slots were released, not leaked: a further history read proceeds.
        self._send(first, 5, protocol.M_SESSION_MESSAGES, {"key": "claude:five"})
        self.assertTrue(self._wait_response(5)["ok"])


if __name__ == "__main__":
    unittest.main()
