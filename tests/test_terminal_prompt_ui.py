"""Actual Textual hit routing; no real user sessions or desktop pointer."""
from __future__ import annotations

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace

from textual.app import App

from corral.ui.controllers.hud_controller import HudControllerMixin
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


class SilentFailureApp(App, HudControllerMixin):
    """Owner requirement: a failed prompt jump shows nothing, not even a toast."""

    def __init__(self):
        super().__init__()
        self.notified: list = []
        self.worker_ran = threading.Event()
        store = SimpleNamespace(session={"keepalive_name": ""})

        def find_session(key):
            self.worker_ran.set()
            return store.session

        self.store = SimpleNamespace(find_session=find_session)

    def notify(self, message, *args, **kwargs):  # type: ignore[override]
        self.notified.append(message)


class SilentFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_jump_never_notifies(self):
        app = SilentFailureApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            cell = SimpleNamespace(spec=SimpleNamespace(session_key="gone"))
            app.jump_to_terminal_prompt("gone", "继续", cell)
            # The worker must actually run; an unstarted worker would vacuously pass.
            deadline = time.monotonic() + 10
            while not app.worker_ran.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            self.assertTrue(app.worker_ran.is_set())
            # Failure path has no sleeps; a grace window catches late toasts.
            await asyncio.sleep(1.0)
            await pilot.pause()
            self.assertEqual(app.notified, [])
