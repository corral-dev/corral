"""Search breadth, single-flight metadata preparation and recent-window isolation."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from corral import split_layout
from corral.display import _filter_sessions_by_query
from corral.models import ConversationMessage, session_key
from corral.runtime.registry import RuntimeRegistry
from corral.search_catalog import SearchCatalog, SearchCatalogError
from corral.store import SessionStore
from corral.titles import TitleState


def session(index: int, title: str | None = None) -> dict:
    return {
        "source": "opencode", "id": f"history-{index}", "short_id": f"h{index}",
        "fallback_title": title or f"History {index}", "native_title": None,
        "cwd": "/tmp", "cwd_display": "/tmp", "path": "",
        "mtime": time.time() - index * 10, "file_mtime": 10, "size_bytes": 10,
        "size_kb": 1, "live": False, "status_tag": "", "attention_kind": "none",
    }


def runtime(rows):
    result = mock.Mock()
    result.id = "opencode"
    result.display_name = "OpenCode"
    result.is_available.return_value = True
    result.scan_signature.return_value = "unchanged"
    result.scan_sessions.side_effect = lambda limit: rows[:limit]
    result.load_conversation.side_effect = lambda item: [
        ConversationMessage("user", "archived body needle" if item["id"] == "history-81" else "hello"),
    ]
    return result


class CatalogFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.dict(os.environ, {
            "CORRAL_CACHE_DIR": tmp.name, "CORRAL_ISOLATE_MANAGED_HOSTS": "1",
            "CORRAL_QUICK_START_PATH": os.path.join(tmp.name, "recent.json"),
        })
        patch.start()
        self.addCleanup(patch.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.rows = [session(i) for i in range(300)]
        self.rows[81]["fallback_title"] = "排查OpenCode会话消息丢失"
        self.runtime = runtime(self.rows)
        self.registry = RuntimeRegistry([self.runtime])

    def store(self):
        store = SessionStore(50, registry=self.registry, title_state=TitleState(cache={}))
        store.sessions = {"opencode": self.rows[:50]}
        store._order = [session_key(row) for row in self.rows[:50]]
        store.display_titles = {session_key(row): row["fallback_title"] for row in self.rows[:50]}
        store.loaded = True
        store._load_event.set()
        return store


class SearchCatalogTests(CatalogFixture):
    def test_expands_without_fixed_depth_and_never_loads_bodies(self):
        catalog = SearchCatalog(self.registry)
        catalog.refresh({})
        self.assertEqual([call.args[0] for call in self.runtime.scan_sessions.call_args_list],
                         [128, 256, 512])
        self.assertEqual(len(catalog.sessions()), 300)
        self.assertTrue(catalog.ready)
        self.runtime.load_conversation.assert_not_called()
        self.runtime.scan_signature.assert_not_called()

    def test_repeated_queries_reuse_catalog_and_unchanged_signatures(self):
        catalog = SearchCatalog(self.registry)
        catalog.refresh({})
        calls = self.runtime.scan_sessions.call_count
        catalog.refresh({})
        self.assertEqual(self.runtime.scan_sessions.call_count, calls)
        # First reconciliation establishes a native signature without delaying
        # the first search. Later unchanged reconciliations reuse that record.
        catalog._prepared_at -= 60
        catalog.refresh({})
        self.assertEqual(self.runtime.scan_sessions.call_count, calls + 1)
        catalog._prepared_at -= 60
        catalog.refresh({})
        self.assertEqual(self.runtime.scan_sessions.call_count, calls + 1)

    def test_changed_signature_reconciles_arrivals_and_removals(self):
        catalog = SearchCatalog(self.registry)
        catalog.refresh({})
        self.rows.pop()
        self.runtime.scan_signature.return_value = "changed"
        catalog._prepared_at -= 60
        catalog.refresh({})
        self.assertEqual(len(catalog.sessions()), 299)
        self.assertIsNone(catalog.find("opencode:history-299"))

    def test_failure_keeps_good_history_and_can_retry(self):
        catalog = SearchCatalog(self.registry)
        catalog.refresh({})
        catalog._prepared_at -= 60
        self.runtime.scan_signature.return_value = "changed"
        self.runtime.scan_sessions.side_effect = OSError("temporarily unreadable")
        with self.assertRaises(SearchCatalogError):
            catalog.refresh({})
        self.assertTrue(catalog.ready)
        self.assertTrue(catalog.failed)
        self.assertFalse(catalog.fresh)
        self.assertEqual(len(catalog.sessions()), 300)
        self.runtime.scan_sessions.side_effect = lambda limit: self.rows[:limit]
        catalog.refresh({})
        self.assertTrue(catalog.ready)

    def test_concurrent_preparation_is_single_flight(self):
        started, release = threading.Event(), threading.Event()

        def scan(limit):
            started.set()
            if not release.wait(5):
                raise TimeoutError("fixture release")
            return self.rows[:limit]

        self.runtime.scan_sessions.side_effect = scan
        catalog = SearchCatalog(self.registry)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(catalog.refresh, {})
            self.assertTrue(started.wait(5))
            second = pool.submit(catalog.refresh, {})
            release.set()
            first.result(5)
            second.result(5)
        self.assertEqual(self.runtime.scan_sessions.call_count, 3)

    def test_shared_wider_window_yields_match_before_native_discovery(self):
        from corral import scan_index

        catalog = SearchCatalog(self.registry)
        observed = []
        with (mock.patch.object(scan_index, "published_meta", return_value=(time.time(), 200)),
              mock.patch.object(scan_index, "try_consume", return_value={"opencode": [self.rows[81]]}) as consume):
            catalog.refresh({}, progress=lambda: observed.append(
                (catalog.ready, catalog.find("opencode:history-81") is not None)))
        consume.assert_called_once_with(200)
        self.assertEqual(observed[0], (False, True))
        self.assertTrue(catalog.ready)
        self.assertEqual(len(catalog.sessions()), 300)

    def test_dormant_runtime_is_never_scanned(self):
        dormant = mock.Mock(id="kimi")
        catalog = SearchCatalog(RuntimeRegistry([self.runtime, dormant]))
        catalog.refresh({})
        dormant.scan_sessions.assert_not_called()

    def test_store_search_does_not_expand_normal_list_or_preview_cache(self):
        store = self.store()
        store.prepare_search()
        self.assertEqual(len(store.all_sessions()), 50)
        self.assertEqual(len(store.search_sessions()), 300)
        self.assertEqual(len(store.conversations), 0)
        self.rows[0]["live"] = True
        self.assertTrue(store.search_sessions()[0]["live"])
        self.runtime.load_conversation.assert_not_called()
        matched = _filter_sessions_by_query(store.search_sessions(), "排查OpenCode会话消息丢失",
                                           titles=store.snapshot())
        self.assertEqual([row["id"] for row in matched], ["history-81"])

    def test_open_promotes_only_chosen_result_and_retains_it_on_rescan(self):
        store = self.store()
        store.prepare_search()
        store.reveal_search_session("opencode:history-81")
        store.reveal_search_session("opencode:history-81")
        self.assertEqual(len(store.all_sessions()), 51)
        self.assertIn("history-81", store._remembered_scan_ids()["opencode"])
        self.assertIsNotNone(store.find_session("opencode:history-81"))

    def test_deleted_result_cannot_be_resurrected(self):
        store = self.store()
        store.prepare_search()
        store._deleted.add("opencode:history-81")
        self.assertIsNone(store.find_session("opencode:history-81"))
        store.reveal_search_session("opencode:history-81")
        self.assertNotIn("history-81", [row["id"] for row in store.search_sessions()])
        self.assertEqual(len(store.all_sessions()), 50)

    def test_native_title_is_searchable_alongside_generated_title(self):
        row = session(1, "Generated title")
        row["native_title"] = "Original upstream issue"
        self.assertEqual(_filter_sessions_by_query([row], "upstream issue"), [row])
        from corral.search import ConversationIndex

        self.assertEqual(ConversationIndex().search([row], "upstream issue").total, 1)

    def test_warm_remote_queries_keep_last_snapshot_and_never_wait_for_refresh(self):
        from corral.remote.sessions import SessionHub

        hub = SessionHub(scan_limit=50)
        self.addCleanup(hub.stop)
        hub.store = self.store()
        self.assertEqual(hub.fulltext_search("archived body needle")["total"], 1)
        hub.store.search_catalog._prepared_at -= 60
        self.runtime.scan_signature.return_value = "changed"
        self.runtime.scan_sessions.side_effect = OSError("temporary failure")
        with self.assertRaises(SearchCatalogError):
            hub.store.prepare_search()
        hub._threads = [mock.Mock()]
        with mock.patch.object(hub.store, "prepare_search",
                               side_effect=AssertionError("foreground catalog wait")):
            self.assertEqual(hub.list_snapshot(query="排查OpenCode会话消息丢失")["total"], 1)
            self.assertEqual(hub.fulltext_search("archived body needle")["total"], 1)
        self.assertTrue(hub._search_refresh.is_set())

    def test_remote_initial_preparation_is_not_a_complete_empty_result(self):
        from corral.remote.sessions import ActionError, SessionHub

        hub = SessionHub(scan_limit=50)
        self.addCleanup(hub.stop)
        hub.store = self.store()
        hub._threads = [mock.Mock()]
        with self.assertRaises(ActionError):
            hub.list_snapshot(query="not-yet-indexed")
        with self.assertRaises(ActionError):
            hub.fulltext_search("not-yet-indexed")
        self.runtime.scan_sessions.assert_not_called()

    def test_remote_title_and_body_search_share_expanded_catalog(self):
        from corral.remote.sessions import SessionHub

        hub = SessionHub(scan_limit=50)
        self.addCleanup(hub.stop)
        hub.store = self.store()
        result = hub.list_snapshot(query="排查OpenCode会话消息丢失")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["sessions"][0]["id"], "history-81")
        self.runtime.load_conversation.assert_not_called()
        result = hub.fulltext_search("archived body needle")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["matches"][0]["key"], "opencode:history-81")
        self.assertEqual(len(hub.store.all_sessions()), 50)
        self.assertIsNotNone(hub.store.find_session("opencode:history-81"))
        self.assertEqual(len(hub.store.conversations), 0)
