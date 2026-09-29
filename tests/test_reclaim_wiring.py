"""Call sites of the silent reclaim tick (TUI refresh worker + remote hub loop).

`corral.reclaim` owns the throttle, the protections and the audit event; these
tests only pin how the two background loops call it: from a worker thread, with
a copied snapshot, never breaking the loop, and never from startup/host paths.
"""

from __future__ import annotations

import builtins
import contextlib
import inspect
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import corral
from corral import i18n, keepalive
from corral import split_layout as _split_layout
from corral.remote import sessions as remote_sessions
from corral.remote.sessions import SessionHub
from corral.ui.app import CorralApp
from corral.ui.main_screen import MainScreen

i18n.set_lang("en")

_SRC = Path(corral.__file__).resolve().parent


@contextlib.contextmanager
def fake_reclaim(*, enabled: bool = True, side_effect: Exception | None = None):
    """Install a stand-in `corral.reclaim`; yields (module, calls)."""
    calls: list[tuple] = []
    module = types.ModuleType("corral.reclaim")
    module.enabled = lambda: enabled

    def maybe_reclaim(sessions_provider=None, *, now=None):
        calls.append((sessions_provider, threading.current_thread()))
        if side_effect is not None:
            raise side_effect
        return []

    module.maybe_reclaim = maybe_reclaim
    with (
        mock.patch.dict(sys.modules, {"corral.reclaim": module}),
        mock.patch.object(corral, "reclaim", module, create=True),
    ):
        yield module, calls


@contextlib.contextmanager
def reclaim_import_fails():
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "corral" and fromlist and "reclaim" in fromlist:
            raise ImportError("reclaim unavailable")
        return real_import(name, globals, locals, fromlist, level)

    with mock.patch("builtins.__import__", side_effect=guarded):
        yield


def _store_stub(*, loaded: bool = True, sessions=None) -> SimpleNamespace:
    rows = sessions if sessions is not None else [{"source": "claude", "id": "a1"}]
    return SimpleNamespace(loaded=loaded, all_sessions=lambda: rows)


class _HookContract:
    """Shared expectations; subclasses bind `call_hook(store)`."""

    def call_hook(self, store) -> None:
        raise NotImplementedError

    def test_passes_a_copied_snapshot_provider(self) -> None:
        session = {"source": "claude", "id": "a1", "keepalive_name": "corral-claude-a1"}
        with fake_reclaim() as (_module, calls):
            self.call_hook(_store_stub(sessions=[session]))
        self.assertEqual(len(calls), 1)
        provider = calls[0][0]
        snapshot = provider()
        self.assertEqual(snapshot, [session])
        self.assertIsNot(snapshot[0], session)  # mutating the copy must not touch the store

    def test_disabled_does_not_call(self) -> None:
        with fake_reclaim(enabled=False) as (_module, calls):
            self.call_hook(_store_stub())
        self.assertEqual(calls, [])

    def test_waits_for_a_real_scan(self) -> None:
        # Hydrated snapshot data has no host annotations yet.
        with fake_reclaim() as (_module, calls):
            self.call_hook(_store_stub(loaded=False))
        self.assertEqual(calls, [])

    def test_swallows_reclaim_failures(self) -> None:
        with fake_reclaim(side_effect=RuntimeError("boom")) as (_module, calls):
            self.call_hook(_store_stub())
        self.assertEqual(len(calls), 1)

    def test_swallows_import_failure(self) -> None:
        with reclaim_import_fails():
            self.call_hook(_store_stub())


class TuiReclaimHookTests(_HookContract, unittest.TestCase):
    def call_hook(self, store) -> None:
        MainScreen._reclaim_inactive_hosts(SimpleNamespace(store=store))


class HubReclaimHookTests(_HookContract, unittest.TestCase):
    def call_hook(self, store) -> None:
        SessionHub._reclaim_inactive_hosts(SimpleNamespace(store=store))


class NoReclaimOnStartupOrHostPathsTests(unittest.TestCase):
    """The contract forbids reclaiming at TUI startup or while creating a session."""

    def test_startup_and_host_paths_never_call_the_reclaimer(self) -> None:
        self.assertNotIn("maybe_reclaim", inspect.getsource(keepalive.reap))
        for relative in ("ui/controllers/host_controller.py", "embed.py", "cli.py"):
            text = (_SRC / relative).read_text(encoding="utf-8")
            self.assertNotIn("maybe_reclaim", text, relative)


class RemoteHubLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": tmp.name}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        _split_layout.reset_default_layout_db()
        self.addCleanup(_split_layout.reset_default_layout_db)
        self.hub = SessionHub(scan_limit=10)
        self.addCleanup(self.hub.stop)

    def test_refresh_loop_ticks_reclaim_after_a_real_scan_on_its_own_thread(self) -> None:
        seen = threading.Event()
        threads: list[threading.Thread] = []

        def hook() -> None:
            threads.append(threading.current_thread())
            seen.set()

        hub = self.hub
        hub._history_watcher = None  # no FS watcher: every slice becomes a scan
        with (
            mock.patch("corral.schedprio.demote_background"),
            mock.patch.object(remote_sessions, "_TITLE_POLL_SLICE", 0.01),
            mock.patch.object(hub.store, "refresh", return_value=False) as refresh,
            mock.patch.object(hub, "_reclaim_inactive_hosts", side_effect=hook),
            mock.patch.object(hub, "_follow_key_migrations"),
            mock.patch.object(hub, "_detect_attention_changes"),
            mock.patch.object(hub, "_detect_live_changes"),
            mock.patch.object(hub, "_detect_status_changes"),
        ):
            worker = threading.Thread(target=hub._refresh_loop, daemon=True)
            worker.start()
            try:
                self.assertTrue(seen.wait(10.0), "reclaim tick never ran")
            finally:
                hub._stop.set()
                worker.join(5.0)
        self.assertGreaterEqual(refresh.call_count, 1)
        self.assertIsNot(threads[0], threading.main_thread())


class TuiRefreshWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_worker_ticks_reclaim_off_the_main_thread(self) -> None:
        tmp = tempfile.mkdtemp(prefix="corral-test-reclaim-wiring-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": tmp}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        _split_layout.reset_default_layout_db()
        self.addCleanup(_split_layout.reset_default_layout_db)

        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.is_available.return_value = True
        runtime.scan_sessions.return_value = [
            {
                "source": "claude", "id": "s0", "short_id": "s0", "mtime": time.time(),
                "size_bytes": 1, "size_kb": 1, "native_title": None,
                "fallback_title": "s0", "cwd": "/tmp", "live": False,
            }
        ]
        registry = corral.RuntimeRegistry((runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()
        store.refresh = mock.Mock(return_value=False)
        app = CorralApp(store, embed_ok=False)

        with (
            fake_reclaim() as (_module, calls),
            mock.patch("corral.ui.main_screen.REFRESH_MIN_GAP", 0.01),
            mock.patch("corral.ui.main_screen.REFRESH_RECONCILE", 0.02),
            mock.patch("corral.ui.main_screen.REFRESH_RECONCILE_FALLBACK", 0.02),
        ):
            async with app.run_test(size=(100, 30)) as pilot:
                for _ in range(500):
                    if calls:
                        break
                    await pilot.pause(delay=0.02)
                await pilot.press("escape")
                await pilot.pause()

        self.assertTrue(calls, "refresh worker never ticked the reclaimer")
        self.assertIsNot(calls[0][1], threading.main_thread())


if __name__ == "__main__":
    unittest.main()
