"""Composer rules on pane captures from the installed runtimes (2026-10-18)."""

from __future__ import annotations

import unittest

from corral.remote import sessions as remote_sessions
from corral.runtime import default_registry

RULE = "─" * 80

CLAUDE_POPUP = "\n".join([
    "  ❯ /recap        Generate a one-line session recap now",
    "    /rename       Rename the current conversation",
    RULE,
    "❯ check @src/app/ma",
    RULE,
    "  Opus 5.5 high · ctx 3% used",
    "  ⏵⏵ auto mode on (shift+tab to cycle)",
])
CLAUDE_SUBMITTED = "\n".join([
    "❯ check @src/app/main.py",
    "⏺ ok",
    RULE,
    "❯ ",
    RULE,
    "  Opus 5.5 high · ctx 3% used",
])
CODEX_POPUP = "\n".join([
    "• ok",
    "› /review  review my current changes and find issues",
    "  /rename  rename the current thread",
    "› Reply ok. Look at src/app/main.py",
    "  second paragraph",
    "",
    "  last paragraph tail",
    "",
    "  GPT-6.1-Sol xhigh · 5h 59% left · Context 2% used",
    "",
])
CODEX_SUBMITTED = "\n".join([
    "› Reply ok. Look at src/app/main.py",
    "• Working (2s • esc to interrupt)",
    "› Ask Codex to do anything",
    "",
    "  GPT-6.1-Sol xhigh · 5h 72% left · Context 2% used",
])
OPENCODE_POPUP = "\n".join([
    "› /tmp is a dir, reply ok",
    "Build · Nemotron · 2.3s",
    "",
    "┃ Reply ok. Look at @src/app/ma",
    "┃",
    "",
    "  @src/app/main.py",
])
PI_PASTE = "\n".join([
    " Run /mcp to fix.",
    RULE,
    "  [paste #1 +40 lines]",
    RULE,
    "corral-sendlab ⎇ master │ 0 turns",
])


class ComposerRegionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = default_registry()

    def region(self, runtime: str, screen: str) -> str | None:
        return self.registry.get(runtime).composer_region(screen)

    def test_claude_box_excludes_popup_and_footer(self) -> None:
        self.assertEqual(self.region("claude", CLAUDE_POPUP), "❯ check @src/app/ma")
        self.assertEqual(self.region("claude", CLAUDE_SUBMITTED), "❯ ")

    def test_codex_bottom_chevron_excludes_popup_rows_and_footer(self) -> None:
        region = self.region("codex", CODEX_POPUP)
        self.assertTrue(region.startswith("› Reply ok."))
        self.assertIn("last paragraph tail", region)
        self.assertNotIn("GPT-6.1", region)
        self.assertNotIn("/review", region)

    def test_opencode_bottom_bar_run_excludes_popup_below(self) -> None:
        self.assertEqual(
            self.region("opencode", OPENCODE_POPUP), "┃ Reply ok. Look at @src/app/ma\n┃"
        )

    def test_pi_box_between_last_rules(self) -> None:
        self.assertEqual(self.region("pi", PI_PASTE), "  [paste #1 +40 lines]")

    def test_cursor_follow_up_placeholder_is_empty_composer(self) -> None:
        self.assertEqual(self.region("cursor", "working → Add a follow-up"), "")
        self.assertEqual(self.region("cursor", "ready → fix it"), "→ fix it")

    def test_unrecognized_screen_is_unknown(self) -> None:
        for runtime in ("claude", "codex", "opencode", "pi", "cursor"):
            with self.subTest(runtime=runtime):
                self.assertIsNone(self.region(runtime, "booting"))


class ComposerHoldsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = default_registry()

    def holds(self, runtime: str, screen: str, text: str) -> bool | None:
        return remote_sessions._composer_holds(self.registry.get(runtime), screen, text)

    def test_tail_matches_across_wrapping_and_rewritten_mentions(self) -> None:
        self.assertTrue(self.holds("codex", CODEX_POPUP, "Reply ok. Look at @src/app/main.py"))
        wrapped = f"{RULE}\n❯ please look at the long\n  sentence tail\n{RULE}"
        self.assertTrue(self.holds("claude", wrapped, "please look at the long sentence tail"))

    def test_submitted_prompt_in_history_does_not_count(self) -> None:
        self.assertFalse(self.holds("claude", CLAUDE_SUBMITTED, "check @src/app/main.py"))
        self.assertFalse(self.holds("codex", CODEX_SUBMITTED, "Reply ok. Look at @src/app/main.py"))

    def test_paste_placeholder_counts_as_held(self) -> None:
        self.assertTrue(self.holds("pi", PI_PASTE, "line\n" * 40))
        claude = f"{RULE}\n❯ [Pasted text #2 +39 lines]\n{RULE}"
        self.assertTrue(self.holds("claude", claude, "line\n" * 40))
        codex = "› [Pasted Content 1469 chars]\n\n  GPT-6.1"
        self.assertTrue(self.holds("codex", codex, "x" * 1469))

    def test_unknown_without_rule_or_letters(self) -> None:
        self.assertIsNone(remote_sessions._composer_holds(None, CLAUDE_POPUP, "check"))
        self.assertIsNone(self.holds("claude", CLAUDE_POPUP, "?!"))
        self.assertIsNone(self.holds("claude", "booting", "check"))


if __name__ == "__main__":
    unittest.main()
