"""Real Textual input paths against an isolated, delayed history catalog."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest import mock

from test_search_catalog import CatalogFixture
from textual.widgets import Input, TextArea

from corral.i18n import set_lang
from corral.ui.app import CorralApp
from corral.ui.search_modal import FullTextSearchModal
from corral.ui.session_list import SessionListView


async def until(predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("timed out waiting for search state")
        await asyncio.sleep(0.01)


class SearchCatalogUITests(CatalogFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        set_lang("en")
        patch = mock.patch("corral.ui.main_screen._SEARCH_INDEX_WARM_DELAY", 3600)
        patch.start()
        self.addCleanup(patch.stop)
        # This fixture owns only search workers. The unrelated perpetual
        # state-probe loop can still be writing its SQLite cache after Textual
        # cancels its thread, racing TemporaryDirectory removal. Its real
        # lifecycle is covered by test_ui.MainScreenWorkerLifecycleTests.
        refresh_patch = mock.patch("corral.ui.main_screen.MainScreen._background_refresh_worker")
        refresh_patch.start()
        self.addCleanup(refresh_patch.stop)

    async def test_exact_title_beyond_recent_window_and_clear(self):
        store = self.store()
        app = CorralApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            field = app.screen.query_one("#project-search", Input)
            view = app.screen.query_one(SessionListView)
            self.assertEqual(len(view.visible_sessions()), 50)
            field.value = "排查OpenCode会话消息丢失"
            await until(lambda: store.search_catalog.ready and not app.screen._history_search_pending)
            await pilot.pause()
            self.assertEqual([row["id"] for row in view.visible_sessions()], ["history-81"])
            self.assertIn("opencode:history-81", view._current_row_identities())
            calls = self.runtime.scan_sessions.call_count
            field.value = "History 90"
            await pilot.pause(delay=0.2)
            self.assertEqual(self.runtime.scan_sessions.call_count, calls)
            field.value = ""
            await pilot.pause()
            self.assertEqual(len(view.visible_sessions()), 50)
            self.assertEqual(len(store.all_sessions()), 50)
            self.runtime.load_conversation.assert_not_called()

    async def test_filter_enter_targets_result_not_new_session(self):
        store = self.store()
        app = CorralApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            field = app.screen.query_one("#project-search", Input)
            field.focus()
            field.value = "排查OpenCode会话消息丢失"
            await until(lambda: store.search_catalog.ready and not app.screen._history_search_pending)
            await pilot.press("enter")
            await pilot.pause()
            view = app.screen.query_one(SessionListView)
            self.assertFalse(view.is_new_session_selected())
            self.assertEqual(view.selected_session()["id"], "history-81")
            self.runtime.build_resume_plan.assert_not_called()

    async def test_typing_and_clear_during_slow_discovery_do_not_restore_old_query(self):
        store = self.store()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def scan(limit):
            started.set()
            if not release.wait(6):
                raise TimeoutError("fixture release")
            return self.rows[:limit]

        self.runtime.scan_sessions.side_effect = scan
        app = CorralApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            field = app.screen.query_one("#project-search", Input)
            field.value = "排查OpenCode会话消息丢失"
            await until(started.is_set)
            self.assertTrue(app.screen._history_search_pending)
            field.value = "History 90"
            await pilot.pause()
            field.value = ""
            await pilot.pause()
            self.assertFalse(app.screen._history_search_pending)
            self.assertEqual(len(app.screen.query_one(SessionListView).visible_sessions()), 50)
            release.set()
            await until(lambda: not app.screen._history_search_running)
            self.assertEqual(field.value, "")
            self.assertEqual(app.screen.nav.project_query, "")
            self.assertEqual(len(app.screen.query_one(SessionListView).visible_sessions()), 50)
            self.assertEqual(self.runtime.scan_sessions.call_count, 3)

    async def test_rapid_query_edits_coalesce_filter_application(self):
        store = self.store()
        store.prepare_search()
        app = CorralApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(delay=0.2)
            view = app.screen.query_one(SessionListView)
            field = app.screen.query_one("#project-search", Input)
            # Clock/layout refreshes may legitimately rebuild the list too;
            # measure the input-owned application, not unrelated UI work.
            with mock.patch.object(app.screen, "_apply_sidebar_filter",
                                   wraps=app.screen._apply_sidebar_filter) as apply_filter:
                for text in ("H", "Hi", "Hist", "History", "History 90"):
                    field.value = text
                await until(lambda: app.screen.nav.project_query == "History 90"
                            and app.screen._sidebar_filter_timer is None)
                await pilot.pause()
                self.assertEqual(apply_filter.await_count, 1)
            self.assertIn("history-90", [row["id"] for row in view.visible_sessions()])
            self.assertIn("opencode:history-90", view._current_row_identities())

    async def test_early_match_is_usable_while_deeper_history_is_preparing(self):
        store = self.store()
        deeper, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def scan(limit):
            if limit > 128:
                deeper.set()
                if not release.wait(6):
                    raise TimeoutError("fixture release")
            return self.rows[:limit]

        self.runtime.scan_sessions.side_effect = scan
        app = CorralApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            field = app.screen.query_one("#project-search", Input)
            field.value = "排查OpenCode会话消息丢失"
            await until(deeper.is_set)
            view = app.screen.query_one(SessionListView)
            self.assertEqual([row["id"] for row in view.visible_sessions()], ["history-81"])
            self.assertTrue(app.screen._history_search_pending)
            self.assertFalse(store.search_catalog.ready)
            release.set()
            await until(lambda: not app.screen._history_search_running)
            await pilot.pause()
            self.assertEqual([row["id"] for row in view.visible_sessions()], ["history-81"])

    async def test_failed_discovery_is_visible_and_next_edit_retries(self):
        store = self.store()
        self.runtime.scan_sessions.side_effect = OSError("unreadable history")
        app = CorralApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            field = app.screen.query_one("#project-search", Input)
            field.value = "missing"
            await until(lambda: app.screen._history_search_failed)
            self.assertFalse(store.search_catalog.ready)
            self.assertEqual(len(store.all_sessions()), 50)
            self.runtime.scan_sessions.side_effect = lambda limit: self.rows[:limit]
            field.value = "排查OpenCode会话消息丢失"
            await until(lambda: store.search_catalog.ready and not app.screen._history_search_pending)
            await pilot.pause()
            self.assertEqual([row["id"] for row in app.screen.query_one(SessionListView).visible_sessions()],
                             ["history-81"])

    async def test_warm_fulltext_queries_do_not_wait_for_incremental_refresh(self):
        from corral.search import ConversationIndex

        store = self.store()
        store.prepare_search()
        index = ConversationIndex()
        index.refresh(store, store.search_sessions())
        app = CorralApp(store, embed_ok=False)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def prepare():
            started.set()
            if not release.wait(6):
                raise TimeoutError("fixture release")

        async with app.run_test(size=(100, 30)) as pilot:
            with mock.patch.object(store, "prepare_search", side_effect=prepare):
                modal = FullTextSearchModal(store, index, "archived body needle")
                app.push_screen(modal)
                await until(started.is_set)
                self.assertFalse(modal._indexing)
                self.assertEqual([match.key for match in modal._matches], ["opencode:history-81"])
                release.set()
                await pilot.pause()
                await pilot.press("escape")

    async def test_fulltext_preparation_failure_stays_open_and_can_retry(self):
        store = self.store()
        self.runtime.scan_sessions.side_effect = OSError("unreadable history")
        app = CorralApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("ctrl+f")
            await until(lambda: isinstance(app.screen, FullTextSearchModal))
            modal = app.screen
            await until(lambda: modal._index_failed)
            self.assertFalse(modal._indexing)
            self.runtime.scan_sessions.side_effect = lambda limit: self.rows[:limit]
            modal.query_one("#search-query", TextArea).load_text("archived body needle")
            await until(lambda: not modal._indexing and len(modal._matches) == 1)
            self.assertFalse(modal._index_failed)
            self.assertEqual(modal._matches[0].key, "opencode:history-81")

    async def test_fulltext_search_opens_and_retains_out_of_window_match(self):
        store = self.store()
        app = CorralApp(store, embed_ok=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("ctrl+f")
            await until(lambda: isinstance(app.screen, FullTextSearchModal))
            modal = app.screen
            await until(lambda: not modal._indexing)
            self.assertFalse(modal._index_failed)
            modal.query_one("#search-query", TextArea).load_text("archived body needle")
            await until(lambda: len(modal._matches) == 1)
            await pilot.pause()
            self.assertEqual(modal._matches[0].key, "opencode:history-81")
            self.assertEqual(len(store.all_sessions()), 50)
            await pilot.press("enter")
            await until(lambda: not isinstance(app.screen, FullTextSearchModal))
            await pilot.pause()
            self.assertEqual(app.screen.query_one(SessionListView).selected_session()["id"], "history-81")
            self.assertEqual(len(store.all_sessions()), 51)
