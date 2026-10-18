"""Actual Textual hit routing; no real user sessions or desktop pointer."""
from __future__ import annotations

import asyncio
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from textual.app import App
from textual.screen import Screen

from corral.models import ConversationMessage
from corral.ui.controllers.hud_controller import HudControllerMixin
from corral.ui.session_hud import HudData, SessionHud, prompt_identity, summarize_user_messages


class PromptApp(App):
    def get_css_variables(self):
        return {**super().get_css_variables(), "pane-active-background": "#31475E"}

    def __init__(self):
        super().__init__()
        self.selected = []
        self.toggles = 0
        self.identities = []

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
        self.identities.append((event.message_index, event.token))


class PromptHitTests(unittest.IsolatedAsyncioTestCase):
    async def test_pressed_fill_covers_wrapped_entry_and_clears_after_release(self):
        app = PromptApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            hud = app.hud
            before = hud.render()
            await pilot.mouse_down(hud, offset=(3, 3))
            await pilot.pause()
            self.assertEqual(hud._pressed_entry, 1)
            self.assertNotEqual(hud.render().spans, before.spans)
            self.assertIsNone(app.focused)
            await pilot.mouse_up(hud, offset=(3, 3))
            await pilot.pause(0.3)
            self.assertIsNone(hud._pressed_entry)
            self.assertTrue(hud.expanded)

    async def test_identical_rows_preserve_distinct_message_identity(self):
        messages = [ConversationMessage("user", "继续"), ConversationMessage("assistant", "reply"),
                    ConversationMessage("user", "继续")]
        app = PromptApp()
        async with app.run_test(size=(100, 30)) as pilot:
            app.hud.update_data(summarize_user_messages(messages), expanded=True)
            await pilot.pause()
            await pilot.click(app.hud, offset=(3, 1))
            await pilot.click(app.hud, offset=(3, 2))
            await pilot.pause()
            self.assertEqual(app.selected, ["继续", "继续"])
            self.assertEqual(app.identities, [(0, prompt_identity(messages, 0)), (2, prompt_identity(messages, 2))])
            self.assertNotEqual(app.identities[0][1], app.identities[1][1])

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

        self.store = SimpleNamespace(find_session=find_session, hosted_name_for=lambda key: None)

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


class HostedJumpApp(App, HudControllerMixin):
    """Freshly hosted session: the scan snapshot has no keepalive_name yet."""

    embed_ok = False

    def __init__(self):
        super().__init__()
        self.pane = SimpleNamespace(
            session_name="corral-claude-x",
            history_offset=0,
            _uses_detail_window=lambda: False,
            _request_immediate_capture=lambda: None,
        )
        self.cell = SimpleNamespace(
            spec=SimpleNamespace(session_key="claude:x"),
            embed_pane=lambda: self.pane,
        )
        session = {"source": "claude", "id": "x"}
        self.store = SimpleNamespace(
            find_session=lambda key: session,
            get_conversation=lambda session: [ConversationMessage("user", "定位到这条消息的顶部")],
            hosted_name_for=lambda key: "corral-claude-x",
            peek_conversation=lambda *args, **kwargs: None,
        )


class HostedJumpTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_or_broken_buffer_never_drives_native_input_or_crashes_ui(self):
        from textual.worker import WorkerState

        for outcome in [None, RuntimeError("capture failed")]:
            app = HostedJumpApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with (mock.patch("corral.ui.terminal_prompt_navigation.buffer_offset", **kwargs) as capture,
                      mock.patch("corral.ui.terminal_prompt_navigation.navigate_native") as native):
                    app.jump_to_terminal_prompt("claude:x", "定位到这条消息的顶部", app.cell)
                    deadline = time.monotonic() + 5
                    while not capture.called and time.monotonic() < deadline:
                        await asyncio.sleep(0.02)
                    await pilot.pause()
                    self.assertTrue(capture.called)
                    native.assert_not_called()
                    self.assertFalse(any(worker.state == WorkerState.ERROR for worker in app.workers))
                    self.assertEqual(app.pane.history_offset, 0)
                    await pilot.press("a", "enter")

    async def test_hosted_map_supplies_name_before_liveness_annotation(self):
        app = HostedJumpApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            with mock.patch("corral.ui.terminal_prompt_navigation.buffer_offset", return_value=23) as resolve:
                app.jump_to_terminal_prompt("claude:x", "定位到这条消息的顶部", app.cell)
                deadline = time.monotonic() + 10
                while app.pane.history_offset == 0 and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
            self.assertEqual(app.pane.history_offset, 23)
            resolve.assert_called_once_with(
                "corral-claude-x", "定位到这条消息的顶部", occurrence=0, total=1,
            )


class PreviewScreen(Screen, HudControllerMixin):
    def __init__(self, messages):
        super().__init__()
        self.messages = messages
        self.store = SimpleNamespace(find_session=lambda key: {"source": "codex"},
                                     peek_conversation=lambda *args, **kwargs: self.messages)

    def compose(self):
        from rich.console import Group

        from corral.display import _preview_blocks
        from corral.ui.split_pane_area import PaneCell, PaneSpec

        self.cell = PaneCell(PaneSpec("fixture", cell_id="preview"), title="History preview",
                             on_close=lambda spec: None, on_focus_list=lambda: None,
                             osc_report=None, detail_renderer=lambda: Group(
                                 *_preview_blocks(self.messages, "Codex", self.cell.size.width)))
        yield self.cell

    def on_mount(self):
        self.cell.query_one(SessionHud).update_data(summarize_user_messages(self.messages), expanded=True)


class PreviewApp(App):
    def get_css_variables(self):
        return {**super().get_css_variables(), "pane-active-background": "#31475E"}

    def __init__(self, messages):
        super().__init__()
        self.preview = PreviewScreen(messages)

    def on_mount(self):
        self.push_screen(self.preview)


class PreviewNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_card_click_jumps_each_duplicate_without_host_or_collapse(self):
        messages = [ConversationMessage("user", "继续"),
                    ConversationMessage("assistant", "First answer\n\n" + "long paragraph " * 400),
                    ConversationMessage("user", "继续"),
                    ConversationMessage("assistant", "Second answer\n\n" + "last paragraph " * 400)]
        app = PreviewApp(messages)
        with mock.patch("corral.ui.terminal_prompt_navigation.navigate_native") as native:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                cell = app.preview.cell
                pane = cell.embed_pane()
                hud = cell.query_one(SessionHud)
                await pilot.click(hud, offset=(3, 1))
                await pilot.pause()
                first = pane.detail_offset
                self.assertFalse(pane._detail_stick_bottom)
                visible = "\n".join(pane.render_line(y).text for y in range(pane.size.height))
                self.assertIn("First answer", visible)
                await pilot.click(hud, offset=(3, 2))
                await pilot.pause()
                self.assertGreater(pane.detail_offset, first)
                visible = "\n".join(pane.render_line(y).text for y in range(pane.size.height))
                self.assertIn("Second answer", visible)
                self.assertNotIn("First answer", visible)
                # Owner rule: the chosen prompt's header is the first visible row.
                from corral.i18n import t
                self.assertEqual(pane.render_line(0).text.strip(), t("preview.you"))
                self.assertTrue(hud.expanded)
                self.assertIsNone(pane.session_name)
                old_offset = pane.detail_offset
                app.preview.jump_to_terminal_prompt("fixture", "继续", cell, 0, "stale-token")
                self.assertEqual(pane.detail_offset, old_offset)
                native.assert_not_called()
