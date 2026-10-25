"""``session.userPrompts`` projection cache.

The host used to page a whole history back to its first message on every cold
open just to list "Your prompts". The projection now lives beside the cached
transcript, so a repeat or cold open answers from that row. These tests use the
deterministic fake readers from ``test_remote_transcript_concurrency`` (no real
history parsing, no machine-speed assumptions beyond a relative comparison).
"""
from __future__ import annotations

import sqlite3
import time
import unittest
from unittest import mock

from test_remote_transcript_concurrency import _History, _HubCase, _messages, _session

from corral.remote import sessions


def _prompt_texts(response: dict) -> list[str]:
    return [row["text"] for row in response["prompts"]]


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


if __name__ == "__main__":
    unittest.main()
