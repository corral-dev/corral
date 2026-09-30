"""Focused static-preview regressions with disposable state and synthetic history."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import corral
from corral import i18n, split_layout
from corral.ui.app import CorralApp
from corral.ui.main_screen import MainScreen


class PreviewRefreshReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = tempfile.TemporaryDirectory(prefix="corral-preview-review-")
        self.addCleanup(self.state.cleanup)
        self.env = mock.patch.dict("os.environ", {
            "CORRAL_CACHE_DIR": self.state.name,
            "CORRAL_CACHE": "0",
            "CORRAL_ISOLATE_MANAGED_HOSTS": "1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        i18n.set_lang("en")
        # Keep the production widgets and worker path, without unrelated scans,
        # polling, update checks, or HUD readers warming the same synthetic data.
        self.mount = mock.patch.object(MainScreen, "on_mount", lambda self: None)
        self.mount.start()
        self.addCleanup(self.mount.stop)

    def _store(self):
        history = Path(self.state.name) / "synthetic-history.jsonl"
        history.write_text("{}\n", encoding="utf-8")
        session = {
            "source": "claude", "id": "preview", "short_id": "preview",
            "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
            "fallback_title": "Synthetic preview", "cwd": "/tmp", "live": False,
            "path": str(history),
        }
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.is_available.return_value = True
        registry = corral.RuntimeRegistry((runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=5, registry=registry)
        store.sessions["claude"] = [session]
        store.loaded = True
        return store, runtime, session

    async def test_async_history_refreshes_focused_preview_without_moving_focus(self) -> None:
        store, runtime, session = self._store()
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def load(_session):
            started.set()
            if not release.wait(5):
                raise TimeoutError("Synthetic history reader was not released")
            return [corral.ConversationMessage("assistant", "FOCUSED_PREVIEW_READY")]

        runtime.load_conversation.side_effect = load
        app = CorralApp(store, embed_ok=True)
        async with app.run_test(size=(120, 24)) as pilot:
            screen = app.screen
            area = screen._split_area()
            area.show_single_preview(session, screen._detail_renderer_for(session))
            await pilot.pause()
            pane = area.cells()[0].embed_pane()
            self.assertIn("Loading conversation", pane.render().plain)
            app.screen.set_focus(pane)
            await pilot.pause()
            self.assertTrue(pane.has_focus)
            screen._warm_conversation(session, screen._preview_gen)
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIn("FOCUSED_PREVIEW_READY", pane.render().plain)
            self.assertTrue(pane.has_focus)

    async def test_focused_preview_refresh_keeps_manual_scroll_position(self) -> None:
        store, runtime, session = self._store()
        messages = [
            corral.ConversationMessage("assistant", f"Synthetic history row {index}")
            for index in range(30)
        ]
        runtime.load_conversation.return_value = messages
        store.get_conversation(session)
        app = CorralApp(store, embed_ok=True)
        async with app.run_test(size=(120, 24)) as pilot:
            screen = app.screen
            area = screen._split_area()
            area.show_single_preview(session, screen._detail_renderer_for(session))
            await pilot.pause()
            pane = area.cells()[0].embed_pane()
            screen.set_focus(pane)
            await pilot.pause()
            self.assertGreater(pane._detail_max_offset(), 0)
            self.assertTrue(pane.scroll_detail_home())
            self.assertEqual(pane.detail_offset, 0)
            runtime.load_conversation.return_value = [
                corral.ConversationMessage("assistant", "UPDATED_HISTORY_AT_TOP"),
                *messages,
            ]
            session["mtime"] += 1
            Path(session["path"]).write_text("{}\n{}\n", encoding="utf-8")
            screen._warm_conversation(session, screen._preview_gen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIn("UPDATED_HISTORY_AT_TOP", pane.render().plain)
            self.assertEqual(pane.detail_offset, 0)
            self.assertTrue(pane.has_focus)

    async def test_stale_history_result_does_not_refresh_replaced_preview(self) -> None:
        store, _runtime, session = self._store()
        app = CorralApp(store, embed_ok=True)
        async with app.run_test(size=(120, 24)) as pilot:
            screen = app.screen
            area = screen._split_area()
            area.show_single_preview(session, screen._detail_renderer_for(session))
            await pilot.pause()
            stale_gen = screen._preview_gen
            screen._preview_gen += 1
            with mock.patch.object(area, "invalidate_visible_previews") as invalidate:
                screen._refresh_preview_detail("claude:preview", stale_gen)
            invalidate.assert_not_called()
