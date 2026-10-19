"""Identity and input-safety regressions found during independent review."""
import os
import tempfile
import time
import unittest
from unittest import mock

from corral import embed
from corral import terminal_navigation_state as state
from corral.models import ConversationMessage
from corral.ui import terminal_prompt_navigation as nav


class VisiblePromptIdentityTests(unittest.TestCase):
    def setUp(self):
        self.messages = [ConversationMessage("user", "继续"),
                         ConversationMessage("assistant", "first unique answer"),
                         ConversationMessage("user", "middle question"),
                         ConversationMessage("user", "继续"),
                         ConversationMessage("assistant", "latest unique answer")]
        self.prompts = [0, 2, 3]

    def test_repeated_visible_text_alone_cannot_prove_old_ordinal(self):
        screen = "› 继续\n\n• answer still rendering\n\n› \n\n? for shortcuts"
        self.assertIsNone(nav._codex_prompt_row(screen, self.messages, self.prompts, 0))
        self.assertIsNone(nav._codex_prompt_row(screen, self.messages, self.prompts, 2))

    def test_visible_reply_disambiguates_repeated_prompt(self):
        screen = "› 继续\n\n• latest unique answer\n\n› \n\n? for shortcuts"
        self.assertIsNone(nav._codex_prompt_row(screen, self.messages, self.prompts, 0))
        self.assertEqual(nav._codex_prompt_row(screen, self.messages, self.prompts, 2), 0)

    def test_later_unique_prompt_disambiguates_repeated_prompt(self):
        screen = "› 继续\n\n› middle question\n\n› \n\n? for shortcuts"
        self.assertEqual(nav._codex_prompt_row(screen, self.messages, self.prompts, 0), 0)
        self.assertIsNone(nav._codex_prompt_row(screen, self.messages, self.prompts, 2))


class StyledComposerTests(unittest.TestCase):
    def test_idle_interruption_notice_is_not_a_running_status(self):
        styled = ("reply\n\n■ Conversation interrupted - use /feedback if something went wrong\n\n\n"
                  "\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m\n\n"
                  "GPT-6.1-Sol high · fixture\n? for shortcuts")
        self.assertTrue(nav._codex_backtrack_ready(styled))

    def test_real_running_status_still_blocks_escape(self):
        styled = ("• Working (12s • esc to interrupt)\n\n"
                  "› \x1b[2mAsk Codex to do anything\x1b[0m\n\n"
                  "GPT-6.1-Sol high · fixture\n? for shortcuts")
        self.assertFalse(nav._codex_backtrack_ready(styled))


    def test_rgb_mode_parameter_two_does_not_make_draft_dim(self):
        self.assertFalse(nav._visible_all_dim("\x1b[38;2;99;99;99mdraft\x1b[0m"))

    def test_zero_rgb_channel_does_not_cancel_dim_placeholder(self):
        self.assertTrue(nav._visible_all_dim("\x1b[2;38;2;99;0;99mplaceholder\x1b[0m"))

    def test_indexed_color_two_does_not_make_draft_dim(self):
        self.assertFalse(nav._visible_all_dim("\x1b[38;5;2mdraft\x1b[0m"))

    def test_plain_and_intensity_reset_text_are_not_dim(self):
        self.assertFalse(nav._visible_all_dim("plain draft"))
        self.assertFalse(nav._visible_all_dim("\x1b[2mplaceholder\x1b[22mdraft"))


class HeaderlessScrollTests(unittest.TestCase):
    def test_tool_quote_of_native_banner_is_not_the_beginning(self):
        screen = "• Ran fixture\n  └ native banner: >_ OpenAI Codex (v0.163.0)\n› draft"
        self.assertFalse(nav._scroll_at_top(nav._SCROLL_PROFILES["codex"], screen))
        screen = "\n  >_ OpenAI Codex (v0.163.0)\n  fixture work\n› first question\n› draft"
        self.assertTrue(nav._scroll_at_top(nav._SCROLL_PROFILES["codex"], screen))

    def test_long_headerless_tool_region_keeps_exponential_progress(self):
        frame = [0]
        keys = []
        def screen():
            return f"frame {frame[0]}\n› composer"

        def settle(_before):
            frame[0] += 1
            return screen()

        def owner(_screen, _near):
            return 0 if frame[0] >= 6 else None

        with mock.patch.object(nav, "_screen", side_effect=lambda _name: screen()), \
                mock.patch.object(embed, "send_key", side_effect=lambda _name, *values: keys.append(values)):
            land = mock.Mock()
            nav._scroll_search(nav._SCROLL_PROFILES["codex"], "fixture", [], [0], 0, lambda: True,
                               settle, owner, lambda _s, _o: None, land, lambda _s: False, lambda: None)
        land.assert_called_once()
        self.assertGreaterEqual(max(len(values) for values in keys), 8)


class NavigationLeaseTests(unittest.TestCase):
    def test_long_operation_still_holds_frames_and_accepts_cancellation_after_ten_seconds(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": root}):
            with embed.navigation_transaction("long-navigation-review", timeout=15) as current:
                later = time.time() + 11
                with mock.patch.object(state.time, "time", return_value=later):
                    self.assertTrue(state.frame_state("long-navigation-review").active)
                    state.cancel("long-navigation-review")
                self.assertFalse(current())
            self.assertFalse(state.frame_state("long-navigation-review").active)


if __name__ == "__main__":
    unittest.main()
