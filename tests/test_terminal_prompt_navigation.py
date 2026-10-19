"""Navigation identity, admission and real tmux coordinate regressions."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from corral import embed
from corral.models import ConversationMessage
from corral.remote.service import Connection, RemoteService
from corral.remote.sessions import ActionError, SessionHub
from corral.ui import terminal_prompt_navigation as nav


class QueryIdentityTests(unittest.TestCase):
    def test_normal_composer_never_scrolls_for_bottom_labels_in_content(self):
        for runtime, composer in [("codex", "›"), ("claude", "❯")]:
            for content in ["Back to bottom", "Jump to bottom", "Back to bottom and Jump to bottom"]:
                with (
                    self.subTest(runtime=runtime, content=content),
                    mock.patch.object(embed, "send_key") as keys,
                    mock.patch.object(nav, "_screen", side_effect=AssertionError("ordinary send entered navigation")),
                ):
                    screen = f"Assistant quoted {content}\n{composer} draft also quotes {content}\n? for shortcuts"
                    self.assertFalse(nav.restore_composer("fixture", runtime, screen=screen))
                    keys.assert_not_called()

    def test_legacy_search_recovery_ignores_bottom_labels_remaining_in_messages(self):
        normal = "Assistant: Back to bottom and Jump to bottom\n› draft\n? for shortcuts"
        trapped = "› draft\nFind: quoted\nNo matches · full transcript · esc cancel"
        with (
            mock.patch.object(nav, "_screen", return_value=normal) as capture,
            mock.patch.object(embed, "send_key") as keys,
        ):
            self.assertTrue(nav.restore_composer("fixture", "codex", screen=trapped))
        keys.assert_called_once_with("fixture", "Escape")
        capture.assert_called_once()

    def test_find_without_matches_still_owns_the_composer(self):
        self.assertTrue(nav._codex_find("› draft\nFind: 文字不对\nNo matches · full transcript · esc cancel\n"))

    def test_quoted_search_chrome_in_a_draft_never_triggers_escape(self):
        screens = {
            "codex": "Find: quoted\nenter accept · full transcript · esc cancel\n› composer draft\n? for shortcuts",
            "claude": "Showing detailed transcript (ctrl+o)\n❯ composer draft\nshortcuts",
        }
        with mock.patch.object(embed, "send_key") as keys:
            for runtime, screen in screens.items():
                self.assertFalse(nav.restore_composer("fixture", runtime, screen=screen))
            keys.assert_not_called()

    def test_repeated_short_prompt_keeps_its_selected_occurrence(self):
        first = ConversationMessage("user", "继续")
        quote = ConversationMessage("assistant", "you said 继续")
        target = ConversationMessage("user", "继续")
        resolved = nav.prompt_target([first, quote, target], target)
        self.assertEqual((resolved.query, resolved.occurrence, resolved.total), ("继续", 2, 3))
        self.assertEqual(resolved.anchor, "you said 继续")

    def test_repeated_prompt_and_assistant_quotation_are_ambiguous(self):
        target = ConversationMessage("user", "continue")
        for other in [ConversationMessage("user", "continue"), ConversationMessage("assistant", "you said continue")]:
            with self.assertRaisesRegex(nav.PromptNavigationError, "ambiguous"):
                nav.prompt_query([target, other], target)

    def test_unique_suffix_disambiguates_shared_opening(self):
        target = ConversationMessage("user", "a shared opening sentence followed by unique 编号一")
        other = ConversationMessage("user", "a shared opening sentence followed by unique 编号二")
        query = nav.prompt_query([target, other], target)
        self.assertIn(query, target.text)
        self.assertNotIn(query, other.text)
        self.assertLessEqual(nav.text_width(query), 24)

    def test_native_anchor_never_joins_source_lines(self):
        first = ConversationMessage("user", "继续")
        answer = ConversationMessage("assistant", "Unique reply heading\nShared response body")
        target = ConversationMessage("user", "继续")
        resolved = nav.prompt_target([first, answer, target], target)
        self.assertEqual(resolved.anchor, "Unique reply heading")

    def test_overlapping_occurrences_cannot_masquerade_as_unique(self):
        target = ConversationMessage("user", "a" * 25)
        with self.assertRaisesRegex(nav.PromptNavigationError, "ambiguous"):
            nav.prompt_query([target], target)

    def test_terminal_control_bytes_never_become_search_input(self):
        target = ConversationMessage("user", "hello\x1b[2J")
        with self.assertRaises(nav.PromptNavigationError):
            nav.prompt_query([target], target)

    def test_unrecognized_custom_search_does_not_type_query_or_escape(self):
        with (
            mock.patch.object(nav, "_screen", return_value="composer draft"),
            mock.patch.object(nav, "_await_screen", side_effect=nav.PromptNavigationError("prompt_unavailable")),
            mock.patch.object(embed, "send_key") as keys,
            mock.patch.object(embed, "send_literal") as literal,
        ):
            with self.assertRaises(nav.PromptNavigationError):
                nav.navigate_native("fixture", "codex", "private query")
        keys.assert_not_called()
        literal.assert_not_called()

    def test_input_transaction_prevents_query_and_typing_interleaving(self):
        entered = threading.Event()
        sent = threading.Event()

        def send():
            entered.set()
            with (
                mock.patch.object(embed, "_active_channel", return_value=None),
                mock.patch.object(embed, "_send", return_value=True),
            ):
                embed.send_bytes("fixture-lock", b"draft")
            sent.set()

        with embed.input_transaction("fixture-lock"):
            thread = threading.Thread(target=send)
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(sent.wait(0.02))
        thread.join(1)
        self.assertTrue(sent.is_set())

    def test_user_input_cancels_navigation_before_waiting_for_its_lock(self):
        entered, cancelled, sent = threading.Event(), threading.Event(), threading.Event()

        def navigate():
            with embed.navigation_transaction("fixture-reading") as current:
                entered.set()
                deadline = time.monotonic() + 2
                while current() and time.monotonic() < deadline:
                    time.sleep(0.005)
                if not current():
                    cancelled.set()

        thread = threading.Thread(target=navigate)
        thread.start()
        self.assertTrue(entered.wait(1))
        with (mock.patch.object(embed, "_active_channel", return_value=None),
              mock.patch.object(embed, "_send", side_effect=lambda *_: sent.set() or True)):
            embed.send_bytes("fixture-reading", b"actual user input")
        thread.join(2)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(sent.is_set())
        self.assertFalse(thread.is_alive())

    def test_superseded_request_never_sends_navigation_keys(self):
        with (
            mock.patch.object(nav, "_screen", return_value="old output\n› composer draft\nmodel status"),
            mock.patch.object(nav, "_await_screen"),
            mock.patch.object(embed, "send_key") as keys,
            mock.patch.object(embed, "send_literal") as literal,
        ):
            states = iter([True, False])
            with self.assertRaisesRegex(nav.PromptNavigationError, "stale"):
                nav.navigate_native("fixture", "codex", "query", is_current=lambda: next(states))
        literal.assert_not_called()
        keys.assert_not_called()

    def test_high_level_send_restores_legacy_codex_find_before_typing(self):
        trapped = "old output\n› unsent draft\nFind: 文字不对\nenter accept · full transcript · esc cancel"
        normal = "latest output\n› unsent draft\nmodel status"
        with (mock.patch.object(nav, "_screen", return_value=normal),
              mock.patch.object(embed, "send_key") as keys,
              mock.patch.object(embed, "send_literal") as literal):
            self.assertTrue(nav.restore_composer("fixture", "codex", screen=trapped))
        keys.assert_called_once_with("fixture", "Escape")
        literal.assert_not_called()

    def test_cancelled_wait_stops_before_another_capture(self):
        with mock.patch.object(nav, "_screen") as capture:
            with self.assertRaisesRegex(nav.PromptNavigationError, "stale"):
                nav._await_screen("fixture", lambda _: True, is_current=lambda: False)
        capture.assert_not_called()


class HostIdentityTests(unittest.TestCase):
    def setUp(self):
        self.target = SimpleNamespace(seq=7, role="user", text="unique target prompt")
        self.transcript = SimpleNamespace(messages=[self.target], generation=9,
                                          reader=SimpleNamespace(has_earlier=lambda: False))
        self.hub = object.__new__(SessionHub)
        self.hub._lock = threading.RLock()
        self.hub.require_session = lambda key: {"source": "codex"}
        self.hub._hosted_name = lambda key: "fixture"
        self.hub.user_prompts = lambda key: None
        self.hub._ensure_transcript = lambda session: self.transcript

    def test_stale_generation_never_searches_a_different_sequence(self):
        with mock.patch.object(nav, "navigate_native") as move:
            with self.assertRaisesRegex(ActionError, "history|历史"):
                self.hub.terminal_prompt("codex:a", 7, 8, native=True)
        move.assert_not_called()

    def test_native_ordinal_path_does_not_require_a_unique_text_query(self):
        with (mock.patch.object(nav, "prompt_target", side_effect=nav.PromptNavigationError("prompt_ambiguous")),
              mock.patch.object(nav, "navigate_native") as move):
            result = self.hub.terminal_prompt("codex:a", 7, 9, native=True)
        self.assertTrue(result["native"])
        self.assertIs(move.call_args.kwargs["selected"], self.target)
        self.assertIsNone(move.call_args.kwargs["target"])

    def test_known_working_remote_session_disables_escape_navigation(self):
        self.hub.require_session = lambda key: {"source": "codex", "attention_kind": "working"}
        with mock.patch.object(nav, "navigate_native") as move:
            self.hub.terminal_prompt("codex:a", 7, 9, native=True)
        self.assertFalse(move.call_args.kwargs["allow_escape"])

    def test_unhosted_selection_never_resumes(self):
        self.hub._hosted_name = lambda key: ""
        with self.assertRaises(ActionError):
            self.hub.terminal_prompt("codex:a", 7, 9, native=True)

    def test_readonly_native_admission_precedes_hub(self):
        service = object.__new__(RemoteService)
        service.hub = mock.Mock()
        connection = Connection("ab" * 32, lambda value: None)
        connection.access = "readonly"
        with self.assertRaises(ActionError):
            service._terminal_prompt(connection, {"key": "codex:a", "seq": 7, "generation": 9, "native": True})
        service.hub.terminal_prompt.assert_not_called()
        service._terminal_prompt(connection, {"key": "codex:a", "seq": 7, "generation": 9})
        service.hub.terminal_prompt.assert_called_once_with("codex:a", 7, 9, native=False)

    def test_later_selection_and_detach_retire_native_input(self):
        service = object.__new__(RemoteService)
        service.hub = mock.Mock()
        service._unsubscribe = lambda *_: False
        connection = Connection("ab" * 32, lambda value: None)
        params = {"key": "codex:a", "seq": 7, "generation": 9, "navigation": "first"}
        service._terminal_prompt(connection, params)
        first_current = service.hub.terminal_prompt.call_args.kwargs["is_current"]
        self.assertTrue(first_current())
        service._terminal_prompt(connection, {**params, "navigation": "second"})
        self.assertFalse(first_current())
        with self.assertRaises(ActionError):
            service._terminal_prompt(connection, {**params, "native": True})
        current = service.hub.terminal_prompt.call_args.kwargs["is_current"]
        self.assertTrue(current())
        service._terminal_detach(connection, {"key": "codex:a"})
        self.assertFalse(current())

    def test_resolve_does_not_send_input(self):
        with (
            mock.patch.object(nav, "buffer_offset", return_value=42),
            mock.patch.object(nav, "navigate_native") as move,
        ):
            result = self.hub.terminal_prompt("codex:a", 7, 9)
        self.assertEqual(result["offset"], 42)
        move.assert_not_called()

    def test_resolve_fallback_returns_native_path_when_text_query_unavailable(self):
        with (mock.patch.object(nav, "prompt_target",
                                side_effect=nav.PromptNavigationError("prompt_unavailable")),
              mock.patch.object(nav, "buffer_offset") as resolve,
              mock.patch.object(nav, "navigate_native") as move):
            result = self.hub.terminal_prompt("codex:a", 7, 9)
        self.assertFalse(result["native"])
        self.assertIsNone(result["offset"])
        self.assertEqual(result["query"], "unique target prompt")
        self.assertEqual((result["occurrence"], result["total"]), (0, 1))
        resolve.assert_not_called()
        move.assert_not_called()

    def test_resolve_empty_text_still_unavailable(self):
        self.target.text = "   "
        with mock.patch.object(nav, "prompt_target",
                               side_effect=nav.PromptNavigationError("prompt_unavailable")):
            with self.assertRaises(ActionError):
                self.hub.terminal_prompt("codex:a", 7, 9)

    def test_incomplete_history_never_claims_query_is_unique(self):
        self.transcript.reader.has_earlier = lambda: True
        with mock.patch.object(nav, "buffer_offset") as resolve:
            with self.assertRaises(ActionError):
                self.hub.terminal_prompt("codex:a", 7, 9)
        resolve.assert_not_called()


@unittest.skipUnless(shutil.which("tmux"), "tmux required")
class RetainedBufferTests(unittest.TestCase):
    def setUp(self):
        self.argv = ["tmux", "-L", "corral-prompt-test-" + uuid.uuid4().hex[:10], "-f", os.devnull]
        self.addCleanup(lambda: subprocess.run([*self.argv, "kill-server"], capture_output=True))
        patch = mock.patch.object(nav, "tmux_argv_for_session", return_value=self.argv)
        patch.start()
        self.addCleanup(patch.stop)
        subprocess.run(
            [*self.argv, "new-session", "-d", "-s", "fixture", "-x", "40", "-y", "10", "stty -echo; cat"],
            check=True,
            capture_output=True,
        )
        time.sleep(0.1)

    def test_actual_scrollback_offset_and_cjk_wrap(self):
        content = "\n".join(
            [f"padding row {i}" for i in range(20)]
            + ["中文目标提问需要保留每一个字包括边界宽字符测试这是一段完整话"]
            + [f"tail {i}" for i in range(25)]
        )
        subprocess.run([*self.argv, "send-keys", "-t", "fixture", "-l", content], check=True)
        deadline = time.monotonic() + 3
        offset = None
        while time.monotonic() < deadline:
            offset = nav.buffer_offset("fixture", "边界宽字符测试")
            if offset is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(offset)
        self.assertGreater(offset, 0)
        capture = subprocess.check_output(
            [*self.argv, "capture-pane", "-p", "-t", "fixture", "-S", str(-offset), "-E", str(9 - offset)], text=True
        )
        self.assertIn("边界宽字符测试", capture.replace("\n", ""))
        # Owner rule: the matched prompt row is the viewport's first row.
        self.assertIn("边界", capture.splitlines()[0])

    def test_missing_or_repeated_text_is_not_acknowledged(self):
        subprocess.run([*self.argv, "send-keys", "-t", "fixture", "-l", "duplicate\nduplicate\n"], check=True)
        time.sleep(0.1)
        self.assertIsNone(nav.buffer_offset("fixture", "missing"))
        self.assertIsNone(nav.buffer_offset("fixture", "duplicate"))

    def test_repeated_buffer_target_requires_complete_occurrence_evidence(self):
        content = "\n".join(["first 继续", *[f"line {i}" for i in range(20)], "second 继续", "tail\n"])
        subprocess.run([*self.argv, "send-keys", "-t", "fixture", "-l", content], check=True)
        deadline = time.monotonic() + 3
        offsets = (None, None)
        while time.monotonic() < deadline:
            offsets = tuple(nav.buffer_offset("fixture", "继续", occurrence=i, total=2) for i in range(2))
            if all(value is not None for value in offsets):
                break
            time.sleep(0.02)
        self.assertTrue(all(value is not None for value in offsets))
        self.assertGreater(offsets[0], offsets[1])
        self.assertIsNone(nav.buffer_offset("fixture", "继续", occurrence=1, total=3))


class NativePromptDispatchTests(unittest.TestCase):
    def test_optional_native_wait_never_occupies_connection_reader_or_normal_input_budget(self):
        service = RemoteService(mock.Mock())
        replies = []
        connection = Connection("ab" * 32, replies.append)
        release = threading.Event()
        entered = threading.Condition()
        count = 0

        def invoke(_connection, method, _params):
            nonlocal count
            if method == "terminal.prompt":
                with entered:
                    count += 1
                    entered.notify_all()
                if not release.wait(3):
                    raise AssertionError("Native fixture was never released")
            return {"ok": True}

        with mock.patch.object(service, "_invoke", side_effect=invoke):
            try:
                for request_id in range(1, 5):
                    service.handle(connection, {"t": "req", "id": request_id, "m": "terminal.prompt",
                                                "p": {"native": True}})
                with entered:
                    self.assertTrue(entered.wait_for(lambda: count == 4, timeout=1))
                self.assertEqual(replies, [])
                service.handle(connection, {"t": "req", "id": 5, "m": "terminal.prompt", "p": {"native": True}})
                self.assertFalse(replies[-1]["ok"])
                self.assertEqual(replies[-1]["id"], 5)
                # All navigation workers are blocked, but ordinary input on
                # this same connection still completes before they finish.
                service.handle(connection, {"t": "req", "id": 6, "m": "terminal.input", "p": {}})
                self.assertEqual(replies[-1]["id"], 6)
                self.assertTrue(replies[-1]["ok"])
                self.assertTrue(service._input_slots.acquire(blocking=False))
                service._input_slots.release()
            finally:
                release.set()
                deadline = time.monotonic() + 2
                while len(replies) < 6 and time.monotonic() < deadline:
                    time.sleep(.01)
        self.assertEqual(sorted(reply["id"] for reply in replies), [1, 2, 3, 4, 5, 6])


class ScrollLandingFallbackTests(unittest.TestCase):
    def test_header_click_that_jumps_elsewhere_resumes_the_search(self):
        # Observed on compacted Claude sessions: a header click can land away
        # from the prompt; the search must continue rather than give up.
        screens = iter(["lost frame\n› composer"])
        calls = []

        def land(screen, own):
            calls.append(screen)
            return next(screens, None)

        with mock.patch.object(nav, "_screen", return_value="start\n› composer"), \
                mock.patch.object(embed, "send_key"):
            nav._scroll_search(nav._SCROLL_PROFILES["codex"], "fixture", [], [0], 0, lambda: True,
                               lambda before: before, lambda _s, _n: 0, lambda _s, _o: None, land,
                               lambda _s: False, lambda: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], "lost frame\n› composer")
