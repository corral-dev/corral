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
        keys.assert_called_once_with("fixture", "F3")
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

    def test_superseded_request_does_not_type_into_recognized_search(self):
        with (
            mock.patch.object(nav, "_screen", return_value="Find: full transcript esc cancel"),
            mock.patch.object(nav, "_await_screen"),
            mock.patch.object(embed, "send_key") as keys,
            mock.patch.object(embed, "send_literal") as literal,
        ):
            # Current at admission; superseded while the search opens.
            states = iter([True, False])
            with self.assertRaisesRegex(nav.PromptNavigationError, "stale"):
                with mock.patch.object(nav, "_screen", side_effect=["composer", "Find: esc cancel"]):
                    nav.navigate_native("fixture", "codex", "query", is_current=lambda: next(states))
        literal.assert_not_called()
        self.assertEqual(keys.call_args_list, [mock.call("fixture", "F3"), mock.call("fixture", "Escape")])


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

    def test_resolve_does_not_send_input(self):
        with (
            mock.patch.object(nav, "buffer_offset", return_value=42),
            mock.patch.object(nav, "navigate_native") as move,
        ):
            result = self.hub.terminal_prompt("codex:a", 7, 9)
        self.assertEqual(result["offset"], 42)
        move.assert_not_called()

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

    def test_missing_or_repeated_text_is_not_acknowledged(self):
        subprocess.run([*self.argv, "send-keys", "-t", "fixture", "-l", "duplicate\nduplicate\n"], check=True)
        time.sleep(0.1)
        self.assertIsNone(nav.buffer_offset("fixture", "missing"))
        self.assertIsNone(nav.buffer_offset("fixture", "duplicate"))
