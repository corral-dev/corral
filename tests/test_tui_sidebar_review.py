"""Focused sidebar regressions using synthetic data and the real Textual DOM."""

from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from corral.ui.app import CorralApp
from corral.ui.nav import NavState
from corral.ui.session_list import SessionListView


def _store(count: int = 20):
    sessions = [
        {
            "source": "claude",
            "id": f"review-{i}",
            "short_id": f"review-{i}",
            "mtime": time.time(),
            "cwd": "/tmp/sidebar-review",
            "fallback_title": f"Synthetic session {i}",
            "live": False,
        }
        for i in range(count)
    ]
    registry = mock.Mock()
    registry.get.return_value = SimpleNamespace(id="claude", display_name="Claude")
    return SimpleNamespace(
        all_sessions=lambda: sessions,
        snapshot=lambda: {},
        registry=registry,
        find_session=lambda key: next(
            (s for s in sessions if key == f"claude:{s['id']}"), None
        ),
    )


class _SidebarApp(CorralApp):
    def __init__(self, store):
        super().__init__(store, embed_ok=False)
        self.sidebar = SessionListView(store, NavState(source="claude"))

    def compose(self):
        yield self.sidebar

    async def on_mount(self, event):
        event.prevent_default()
        await self.sidebar.rebuild()


async def _wait_until(predicate):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for synthetic sidebar rows")


class SidebarChunkSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_rebuild_keeps_selected_row_beyond_first_chunk(self):
        app = _SidebarApp(_store())
        with mock.patch("corral.ui.session_list._MOUNT_CHUNK", 5):
            async with app.run_test(size=(70, 25)) as pilot:
                sidebar = app.sidebar
                await _wait_until(lambda: len(sidebar._session_items()) == 20)
                self.assertTrue(sidebar.select_session_key("claude:review-17"))
                await sidebar.clear()
                await sidebar.rebuild(select_key="claude:review-17")
                self.assertIsNotNone(sidebar.selected_session())
                self.assertEqual(sidebar.selected_session()["id"], "review-17")
                await pilot.pause(delay=0.3)
                self.assertEqual(sidebar.selected_session()["id"], "review-17")
                highlighted = sidebar._scroll_list.highlighted_child
                self.assertIsNotNone(highlighted)
                self.assertEqual(highlighted.children[0].session["id"], "review-17")

    async def test_stale_tail_waiter_does_not_discard_new_rebuild_tail(self):
        app = _SidebarApp(_store())
        async with app.run_test(size=(70, 25)) as pilot:
            await pilot.pause()
            sidebar = app.sidebar
            await sidebar._rebuild_lock.acquire()
            try:
                sidebar._tail_token = sidebar._rebuild_seq
                old_token = sidebar._tail_token
                waiter = asyncio.create_task(sidebar._mount_tail_batch())
                await asyncio.sleep(0)
                self.assertFalse(waiter.done())
                # A full rebuild holding this same lock registers its new tail.
                sidebar._rebuild_seq += 1
                new_tail = [mock.Mock()]
                sidebar._tail_items = new_tail
                sidebar._tail_token = sidebar._rebuild_seq
                self.assertNotEqual(sidebar._tail_token, old_token)
            finally:
                sidebar._rebuild_lock.release()
            await waiter
            self.assertEqual(sidebar._tail_items, new_tail)
            sidebar._tail_items = []

    async def test_old_timer_does_not_consume_new_rebuild_tail(self):
        app = _SidebarApp(_store())
        async with app.run_test(size=(70, 25)) as pilot:
            await pilot.pause()
            sidebar = app.sidebar
            rows = sidebar._sidebar_rows()
            with mock.patch.object(sidebar, "set_timer") as schedule:
                sidebar._begin_tail_mount([], rows, sidebar._rebuild_seq)
                stale_callback = schedule.call_args.args[1]
                sidebar._rebuild_seq += 1
                new_tail = [sidebar._item_for_row(rows[0], {})]
                sidebar._begin_tail_mount(new_tail, rows, sidebar._rebuild_seq)
                mounted_before = len(sidebar.list_children)
                await stale_callback()
                self.assertEqual(sidebar._tail_items, new_tail)
                self.assertEqual(len(sidebar.list_children), mounted_before)
                sidebar._tail_items = []

    async def test_initial_rebuild_retains_chunked_mounting(self):
        app = _SidebarApp(_store())
        with mock.patch("corral.ui.session_list._MOUNT_CHUNK", 5):
            async with app.run_test(size=(70, 25)):
                sidebar = app.sidebar
                await _wait_until(lambda: len(sidebar._session_items()) == 20)
                await sidebar.clear()
                await sidebar.rebuild(keep_selection=False)
                self.assertEqual(len(sidebar._session_items()), 5)
                self.assertEqual(len(sidebar._tail_items), 15)
                await _wait_until(lambda: len(sidebar._session_items()) == 20)
                self.assertEqual(len(sidebar._session_items()), 20)
                self.assertFalse(sidebar._tail_items)
