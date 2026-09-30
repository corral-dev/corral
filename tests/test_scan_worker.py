"""Off-process history scanner (perf-C): singleton worker + store fallback."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class _StubRegistry:
    """Minimal scan_all double recording how it was called."""

    def __init__(self, sessions: dict | None = None):
        self.sessions = sessions or {}
        self.calls: list[dict] = []
        self.last_scan_cache_hit_all = False
        self.last_scan_shared = False

    @property
    def ids(self) -> tuple:
        return tuple(self.sessions.keys())

    def get(self, runtime_id: str):
        raise KeyError(runtime_id)

    def scan_all(self, limit, keep_ids_by_runtime=None, *, prefer_shared=True):
        from corral import scan_index

        self.calls.append(
            {"limit": limit, "prefer_shared": prefer_shared,
             "keep_ids": keep_ids_by_runtime}
        )
        if prefer_shared:
            shared = scan_index.try_consume(limit, keep_ids_by_runtime)
            if shared is not None:
                self.last_scan_shared = True
                self.last_scan_cache_hit_all = True
                return shared
        self.last_scan_shared = False
        self.last_scan_cache_hit_all = False
        import copy

        result = copy.deepcopy(self.sessions)
        scan_index.publish(
            result, limit=limit, keep_ids_by_runtime=keep_ids_by_runtime,
        )
        return result


class ScanWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cache = Path(self._tmpdir.name)
        # Real index file in a temp dir; worker enabled despite the suite's
        # CORRAL_ISOLATE_MANAGED_HOSTS=1 (which must keep disabling workers
        # outside this module).
        self.patches = [
            mock.patch("corral.scan_index.cache_dir", return_value=self.cache),
            mock.patch("corral.scan_index._shared_index_enabled", return_value=True),
            mock.patch("corral.scan_worker._worker_enabled", return_value=True),
            mock.patch("corral.cache.cache_dir", return_value=self.cache),
            # Real sidebar memory holds hundreds of stale pins; pin to {}
            # so publish/consume round-trips are deterministic here.
            mock.patch(
                "corral.scan_worker._remembered_keep_ids", return_value={},
            ),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        from corral import scan_index, scan_worker

        self.scan_index = scan_index
        self.scan_worker = scan_worker
        scan_index._LAST_PUBLISH.update(
            {"at": 0.0, "path": "", "limit": 0, "keys": None}
        )
        self.addCleanup(
            scan_index._LAST_PUBLISH.update,
            {"at": 0.0, "path": "", "limit": 0, "keys": None},
        )
        scan_worker._LAST_SPAWN_MONO = 0.0

    def _session(self, runtime: str, sid: str, mtime: float) -> dict:
        return {
            "source": runtime,
            "id": sid,
            "short_id": sid,
            "file_mtime": mtime,
            "mtime": mtime,
            "cwd": "/tmp",
            "live": False,
        }

    def _write_heartbeat(self, pid: int, age_seconds: float = 0.0) -> None:
        path = self.cache / self.scan_worker.HEARTBEAT_FILENAME
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {"pid": pid, "updated_at": time.time() - age_seconds}, handle,
            )

    # -- heartbeat verdict --

    def test_inactive_without_heartbeat(self) -> None:
        self.assertFalse(self.scan_worker.is_active())

    def test_inactive_with_stale_heartbeat(self) -> None:
        self._write_heartbeat(os.getpid(), age_seconds=3600.0)
        self.assertFalse(self.scan_worker.is_active())

    def test_inactive_with_dead_pid(self) -> None:
        self._write_heartbeat(2**31 - 1)  # implausible pid, fresh timestamp
        self.assertFalse(self.scan_worker.is_active())

    def test_active_with_fresh_heartbeat_of_live_pid(self) -> None:
        self._write_heartbeat(os.getpid())
        self.assertTrue(self.scan_worker.is_active())

    # -- worker passes --

    def test_run_once_publishes_consumable_index(self) -> None:
        registry = _StubRegistry(
            {"claude": [self._session("claude", "a", 30)]}
        )
        self.assertTrue(self.scan_worker.run_once(limit=50, registry=registry))
        self.assertEqual(registry.calls[0]["limit"], 200)  # covers remote too
        self.assertTrue(registry.calls[0]["prefer_shared"])
        got = self.scan_index.try_consume(50, {})
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual([item["id"] for item in got["claude"]], ["a"])

    def test_run_once_prefers_shared_when_daemon_published(self) -> None:
        self.scan_index.publish(
            {"claude": [self._session("claude", "remote", 30)]}, limit=200,
        )
        registry = _StubRegistry(
            {"claude": [self._session("claude", "local", 10)]}
        )
        self.assertTrue(self.scan_worker.run_once(limit=50, registry=registry))
        # Consumed the daemon's publish instead of parsing locally.
        self.assertTrue(registry.last_scan_shared)

    def test_run_loop_exits_when_parent_dead(self) -> None:
        registry = _StubRegistry({"claude": []})
        code = self.scan_worker.run_loop(
            limit=50, interval=0.01, parent_pid=2**31 - 1,
            max_passes=100, registry=registry,
        )
        self.assertEqual(code, 0)
        self.assertEqual(registry.calls, [])

    def test_run_loop_singleton_second_worker_exits(self) -> None:
        lock = self.scan_worker._acquire_singleton_lock()
        self.assertIsNotNone(lock)
        try:
            registry = _StubRegistry({"claude": []})
            code = self.scan_worker.run_loop(
                limit=50, interval=0.01, parent_pid=os.getppid(),
                max_passes=100, registry=registry,
            )
            self.assertEqual(code, 0)
            self.assertEqual(registry.calls, [])
        finally:
            lock.close()

    def test_run_loop_bounded_passes_publish(self) -> None:
        registry = _StubRegistry(
            {"claude": [self._session("claude", "a", 30)]}
        )
        code = self.scan_worker.run_loop(
            limit=50, interval=0.01, parent_pid=os.getppid(),
            max_passes=2, registry=registry,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(registry.calls), 2)
        self.assertIsNotNone(self.scan_index.try_consume(50, {}))

    # -- ensure --

    def test_ensure_reuses_live_worker_without_spawning(self) -> None:
        self._write_heartbeat(os.getpid())
        with mock.patch.object(
            subprocess, "Popen", side_effect=AssertionError("must not spawn"),
        ):
            self.assertIsNone(self.scan_worker.ensure_scan_worker(50))

    def test_ensure_spawns_when_stale_then_rate_limits(self) -> None:
        with mock.patch.object(subprocess, "Popen", return_value=mock.sentinel.proc) as popen:
            proc = self.scan_worker.ensure_scan_worker(50)
            self.assertIs(proc, mock.sentinel.proc)
            argv = popen.call_args[0][0]
            self.assertEqual(
                argv[:3], [sys.executable, "-m", "corral.scan_worker"],
            )
            self.assertIn("200", argv)  # floor covers the remote daemon
            # Second call inside the respawn window: no new process.
            self.assertIsNone(self.scan_worker.ensure_scan_worker(50))
            self.assertEqual(popen.call_count, 1)

    def test_worker_limit_floors_at_remote_depth(self) -> None:
        self.assertEqual(self.scan_worker.worker_limit(50), 200)
        self.assertEqual(self.scan_worker.worker_limit(300), 300)

    # -- store integration --

    def test_store_skips_forced_local_scan_with_live_worker(self) -> None:
        import corral

        self._write_heartbeat(os.getpid())
        registry = _StubRegistry(
            {"claude": [self._session("claude", "a", 30)]}
        )
        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "list_managed_hosts", return_value=[],
        ), mock.patch.object(
            self.scan_worker, "ensure_scan_worker", return_value=None,
        ) as ensure:
            store = corral.SessionStore(limit=50, registry=registry)
            store._remembered_scan_ids = lambda: {}
            store.load()
            ensure.reset_mock()
            # First refresh would force a local scan; the live worker excuses it.
            store._last_local_scan_at = (
                time.monotonic() - store._FULL_MERGE_INTERVAL - 1
            )
            before_scans = len(registry.calls)
            store.refresh()
            self.assertTrue(registry.last_scan_shared)
            # No extra local parse happened on the forced-local tick.
            local_parses = [
                call for call in registry.calls[before_scans:]
                if not call["prefer_shared"]
            ]
            self.assertEqual(local_parses, [])
            ensure.assert_not_called()

    def test_store_falls_back_to_local_scan_without_worker(self) -> None:
        import corral

        registry = _StubRegistry(
            {"claude": [self._session("claude", "a", 30)]}
        )
        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "list_managed_hosts", return_value=[],
        ), mock.patch.object(
            self.scan_worker, "ensure_scan_worker", return_value=None,
        ) as ensure:
            store = corral.SessionStore(limit=50, registry=registry)
            store._remembered_scan_ids = lambda: {}
            store.load()
            ensure.reset_mock()
            store._last_local_scan_at = (
                time.monotonic() - store._FULL_MERGE_INTERVAL - 1
            )
            store.refresh()
            self.assertFalse(registry.last_scan_shared)
            ensure.assert_called_once_with(50)

    # -- real entry point --

    def test_scan_worker_once_subprocess(self) -> None:
        env = dict(os.environ)
        env["CORRAL_CACHE_DIR"] = str(self.cache)
        # Suite env sets CORRAL_ISOLATE_MANAGED_HOSTS=1; the worker must run
        # with it off (as in production).
        env.pop("CORRAL_ISOLATE_MANAGED_HOSTS", None)
        env.pop("PICKUP_ISOLATE_MANAGED_HOSTS", None)
        env.pop("SC_ISOLATE_MANAGED_HOSTS", None)
        proc = subprocess.run(
            [sys.executable, "-m", "corral.scan_worker", "--once", "--limit", "50"],
            capture_output=True, text=True, timeout=180, env=env,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-2000:])
        index = self.cache / "scan-index.json"
        self.assertTrue(index.exists())
        with open(index, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload.get("version"), 1)
        self.assertGreaterEqual(int(payload.get("limit") or 0), 200)


if __name__ == "__main__":
    unittest.main()
