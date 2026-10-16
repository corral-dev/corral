"""Actual Textual hit routing; no real user sessions or desktop pointer."""
from __future__ import annotations

import unittest

from textual.app import App

from corral.ui.session_hud import HudData, SessionHud


class PromptApp(App):
    def get_css_variables(self):
        return {**super().get_css_variables(), "pane-active-background": "#31475E"}

    def __init__(self):
        super().__init__()
        self.selected = []
        self.toggles = 0

    def compose(self):
        self.hud = SessionHud(self.toggle)
        self.hud.update_data(HudData(3, (("12:00", "first prompt"),
                                       ("12:01", "long 中文提问 " * 12),
                                       ("12:02", "last prompt"))), expanded=True)
        yield self.hud

    def toggle(self):
        self.toggles += 1
        self.hud.update_data(self.hud.data, expanded=not self.hud.expanded)

    def on_session_hud_prompt_selected(self, event):
        self.selected.append(event.body)


class PromptHitTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapped_rows_select_one_prompt_header_and_footer_only_toggle(self):
        app = PromptApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            hud = app.hud
            self.assertTrue(hud.expanded)
            await pilot.click(hud, offset=(3, 1))
            await pilot.pause()
            self.assertEqual(app.selected, ["first prompt"])
            self.assertEqual(app.toggles, 0)
            await pilot.click(hud, offset=(3, 3))
            await pilot.pause()
            self.assertEqual(app.selected[-1], "long 中文提问 " * 12)
            await pilot.click(hud, offset=(3, hud.size.height - 1))
            await pilot.pause()
            self.assertFalse(hud.expanded)
            self.assertEqual(app.toggles, 1)
            await pilot.click(hud, offset=(3, 1))
            await pilot.pause()
            self.assertTrue(hud.expanded)
            self.assertEqual(app.toggles, 2)

    async def test_scrolled_hit_uses_visible_window(self):
        app = PromptApp()
        async with app.run_test(size=(100, 12)) as pilot:
            await pilot.pause()
            hud = app.hud
            data = HudData(30, tuple(("12:00", f"prompt {i}") for i in range(30)))
            hud.update_data(data, expanded=True)
            await pilot.pause()
            self.assertGreater(hud._scroll, 0)
            first_visible = hud._scroll
            await pilot.click(hud, offset=(3, 1))
            await pilot.pause()
            self.assertEqual(app.selected, [f"prompt {first_visible}"])
