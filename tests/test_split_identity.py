"""Split composition must outlive scan snapshots and placeholder promotion."""

import asyncio
import tempfile
import unittest
from unittest import mock

from corral.models import session_key
from corral.runtime.registry import RuntimeRegistry
from corral.store import SessionStore
from corral.ui.split_pane_area import SplitPaneArea


class SplitIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patch = mock.patch.dict("os.environ", {"CORRAL_CACHE_DIR": temporary.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.store = SessionStore(20, registry=RuntimeRegistry((mock.Mock(id="claude"),)))
        self.claude = self.session("claude", "placeholder", "corral-claude-placeholder")
        self.codex = self.session("codex", "first", "corral-codex-first")
        self.store.sessions = {"claude": [self.claude], "codex": [self.codex]}
        self.area = SplitPaneArea(
            self.store, on_runtime_pick=lambda _: None,
            on_pane_close=lambda _: None, on_focus_list=lambda: None,
        )
        self.area._schedule_mount = mock.Mock()
        self.area._cells = mock.Mock(return_value=[])
        self.area._cell_for_spec = mock.Mock(return_value=None)
        self.area.show_hosted_group(
            "/fixture", [(s, s["keepalive_name"], None) for s in (self.codex, self.claude)],
            focus_key=session_key(self.claude),
        )

    @staticmethod
    def session(runtime, ident, host):
        return {"source": runtime, "id": ident, "cwd": "/fixture", "keepalive_name": host}

    def add_codex(self, ident="second"):
        session = self.session("codex", ident, f"corral-codex-{ident}")
        self.area.add_hosted_pane(session, session["keepalive_name"], None)

    def test_add_resolves_promotion_before_sidebar_rebuild(self):
        real = dict(self.claude, id="native")
        self.store.sessions["claude"] = [real]
        self.store._session_key_migrations["claude:placeholder"] = "claude:native"
        self.add_codex()
        self.assertEqual(self.area.ordered_session_keys(), ["codex:first", "claude:native", "codex:second"])
        self.assertEqual(self.area.focus_key, "claude:native")

    def test_exact_host_resolves_without_migration_record(self):
        real = dict(self.claude, id="native")
        self.store.sessions["claude"] = [real]
        self.add_codex()
        self.assertIn("claude:native", self.area.ordered_session_keys())

    def test_missing_scan_row_and_back_to_back_additions_keep_binding(self):
        self.store.sessions["claude"] = []
        self.add_codex()
        self.add_codex("third")
        self.assertEqual(
            self.area.ordered_session_keys(),
            ["codex:first", "claude:placeholder", "codex:second", "codex:third"],
        )
        self.assertIs(self.area.pane_specs()[1].session, self.claude)

    def test_ambiguous_host_never_redirects_existing_pane(self):
        self.store.sessions["claude"] = [dict(self.claude, id="a"), dict(self.claude, id="b")]
        self.add_codex()
        self.assertEqual(self.area.pane_specs()[1].session_key, "claude:placeholder")

    def test_same_project_and_different_host_never_redirect_pane(self):
        unrelated = dict(self.claude, id="other", keepalive_name="corral-claude-other")
        self.store.sessions["claude"] = [unrelated]
        self.add_codex()
        self.assertIs(self.area.pane_specs()[1].session, self.claude)

    def test_recorded_migration_chain_wins_over_old_snapshot(self):
        self.store._session_key_migrations.update({
            "claude:placeholder": "claude:middle", "claude:middle": "claude:native",
        })
        real = dict(self.claude, id="native")
        self.store.sessions["claude"] = [self.claude, real]
        self.add_codex()
        self.assertEqual(self.area.pane_specs()[1].session_key, "claude:native")

    def test_identity_reconciliation_migrates_pending_focus(self):
        self.area._focus_intent_key = "claude:placeholder"
        self.area._input_claim_key = "claude:placeholder"
        self.area.reconcile_session_keys({self.claude["keepalive_name"]: "claude:native"})
        self.assertEqual(self.area.focus_key, "claude:native")
        self.assertEqual(self.area._focus_intent_key, "claude:native")
        self.assertEqual(self.area._input_claim_key, "claude:native")

    def test_scan_gap_does_not_reverse_an_already_migrated_key(self):
        self.area.reconcile_session_keys({self.claude["keepalive_name"]: "claude:native"})
        self.store.sessions["claude"] = []
        self.add_codex()
        self.assertEqual(self.area.pane_specs()[1].session_key, "claude:native")


class RebuildLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_during_await_does_not_query_removed_header(self):
        from corral.ui.main_screen import MainScreen

        screen = mock.Mock(is_mounted=True, embed_ok=False)
        screen._rebuild_lock = asyncio.Lock()
        screen.app.is_running = True
        view = screen.query_one.return_value
        view._displayed_selected_key.return_value = None

        async def shutdown(**kwargs):
            screen.app.is_running = False

        view.rebuild = mock.AsyncMock(side_effect=shutdown)
        await MainScreen._rebuild_list(screen)
        screen._update_header.assert_not_called()
        view.refresh_active_card.assert_not_called()

    async def test_queued_rebuild_after_shutdown_does_not_query_widgets(self):
        from corral.ui.main_screen import MainScreen

        screen = mock.Mock(is_mounted=False)
        screen._rebuild_lock = asyncio.Lock()
        await MainScreen._rebuild_list(screen)
        screen.query_one.assert_not_called()

