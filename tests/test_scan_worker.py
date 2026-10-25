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

    def _write_heartbeat(self, pid: int, age_seconds: float = 0.0, *, cohort: object = "current") -> None:
        from corral.cache import provider_cohort

        path = self.cache / self.scan_worker.HEARTBEAT_FILENAME
        payload: dict[str, object] = {"pid": pid, "updated_at": time.time() - age_seconds}
        if cohort == "current":
            payload["cohort"] = provider_cohort()
        elif isinstance(cohort, str):
            payload["cohort"] = cohort
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

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

    def test_inactive_with_cohortless_legacy_heartbeat(self) -> None:
        # Pre-cohort workers carry no contract stamp: never reuse them.
        self._write_heartbeat(os.getpid(), cohort=None)
        self.assertFalse(self.scan_worker.is_active())

    def test_inactive_with_obsolete_provider_cohort(self) -> None:
        self._write_heartbeat(os.getpid(), cohort="2026-09-29.3+sesskit-0.2.3")
        self.assertFalse(self.scan_worker.is_active())

    def test_write_heartbeat_stamps_current_cohort(self) -> None:
        from corral.cache import provider_cohort

        self.scan_worker._write_heartbeat()
        path = self.cache / self.scan_worker.HEARTBEAT_FILENAME
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload.get("cohort"), provider_cohort())

    # -- worker passes --

    def test_provider_drift_detects_installed_upgrade(self) -> None:
        with mock.patch(
            "corral.cache.sesskit_provider_version", return_value="0.2.17",
        ), mock.patch.object(
            self.scan_worker, "_installed_provider_version", return_value="0.2.18",
        ), mock.patch(
            "corral.updater.install_replaced", return_value=False,
        ):
            self.assertTrue(self.scan_worker.provider_drift())

    def test_provider_drift_false_when_provider_matches(self) -> None:
        with mock.patch(
            "corral.cache.sesskit_provider_version", return_value="0.2.17",
        ), mock.patch.object(
            self.scan_worker, "_installed_provider_version", return_value="0.2.17",
        ), mock.patch(
            "corral.updater.install_replaced", return_value=False,
        ):
            self.assertFalse(self.scan_worker.provider_drift())

    def test_provider_drift_detects_replaced_corral_install(self) -> None:
        with mock.patch(
            "corral.cache.sesskit_provider_version", return_value="0.2.17",
        ), mock.patch.object(
            self.scan_worker, "_installed_provider_version", return_value="0.2.17",
        ), mock.patch(
            "corral.updater.install_replaced", return_value=True,
        ):
            self.assertTrue(self.scan_worker.provider_drift())

    def test_run_loop_exits_when_provider_drifted(self) -> None:
        registry = _StubRegistry({"claude": []})
        with mock.patch.object(
            self.scan_worker, "provider_drift", return_value=True,
        ):
            code = self.scan_worker.run_loop(
                limit=50, interval=0.01, parent_pid=os.getppid(),
                max_passes=100, registry=registry,
            )
        self.assertEqual(code, 0)
        self.assertEqual(registry.calls, [])

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

    def test_obsolete_worker_pid_returns_stale_live_worker(self) -> None:
        self._write_heartbeat(424242, cohort="2026-09-29.3+sesskit-0.2.3")
        with mock.patch.object(
            self.scan_worker, "_pid_alive", return_value=True,
        ), mock.patch.object(
            self.scan_worker, "_is_scan_worker_process", return_value=True,
        ):
            self.assertEqual(self.scan_worker.obsolete_worker_pid(), 424242)

    def test_obsolete_worker_pid_ignores_current_cohort(self) -> None:
        self._write_heartbeat(424242)
        with mock.patch.object(
            self.scan_worker, "_pid_alive", return_value=True,
        ), mock.patch.object(
            self.scan_worker, "_is_scan_worker_process", return_value=True,
        ):
            self.assertIsNone(self.scan_worker.obsolete_worker_pid())

    def test_obsolete_worker_pid_requires_worker_command_line(self) -> None:
        self._write_heartbeat(424242, cohort="2026-09-29.3+sesskit-0.2.3")
        with mock.patch.object(
            self.scan_worker, "_pid_alive", return_value=True,
        ), mock.patch.object(
            self.scan_worker, "_is_scan_worker_process", return_value=False,
        ):
            self.assertIsNone(self.scan_worker.obsolete_worker_pid())

    def test_reap_obsolete_worker_terminates_verified_pid(self) -> None:
        import signal

        self._write_heartbeat(424242, cohort="2026-09-29.3+sesskit-0.2.3")
        alive = {"value": True}

        def fake_kill(_pid: int, _sig: int) -> None:
            alive["value"] = False

        with mock.patch.object(
            self.scan_worker,
            "_pid_alive",
            side_effect=lambda _pid: alive["value"],
        ), mock.patch.object(
            self.scan_worker, "_is_scan_worker_process", return_value=True,
        ), mock.patch.object(self.scan_worker.os, "kill", side_effect=fake_kill) as kill:
            pid = self.scan_worker.reap_obsolete_worker(timeout=0.2)
        self.assertEqual(pid, 424242)
        kill.assert_called_once_with(424242, signal.SIGTERM)

    def test_reap_skips_current_worker_without_signalling(self) -> None:
        self._write_heartbeat(424242)
        with mock.patch.object(
            self.scan_worker, "_pid_alive", return_value=True,
        ), mock.patch.object(
            self.scan_worker, "_is_scan_worker_process", return_value=True,
        ), mock.patch.object(self.scan_worker.os, "kill") as kill:
            self.assertIsNone(self.scan_worker.reap_obsolete_worker(timeout=0.01))
        kill.assert_not_called()

    def test_ensure_reaps_obsolete_worker_then_spawns(self) -> None:
        with mock.patch.object(
            self.scan_worker, "is_active", return_value=False,
        ), mock.patch.object(
            self.scan_worker, "reap_obsolete_worker",
        ) as reap, mock.patch.object(
            subprocess, "Popen", return_value=mock.sentinel.proc,
        ) as popen:
            proc = self.scan_worker.ensure_scan_worker(50)
        self.assertIs(proc, mock.sentinel.proc)
        reap.assert_called_once_with()
        self.assertEqual(popen.call_count, 1)

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

    def test_drifted_consumer_never_reaps_or_spawns(self) -> None:
        self._write_heartbeat(424242, cohort="newer-cohort")
        with mock.patch.object(self.scan_worker, "provider_drift", return_value=True), \
                mock.patch.object(self.scan_worker.os, "kill") as kill, \
                mock.patch.object(subprocess, "Popen") as popen:
            self.assertIsNone(self.scan_worker.obsolete_worker_pid())
            self.assertIsNone(self.scan_worker.ensure_scan_worker(50))
        # A signal-0 liveness query is read-only; no termination signal.
        self.assertTrue(all(call.args[1] == 0 for call in kill.call_args_list))
        popen.assert_not_called()

    def test_process_check_requires_exact_python_module_invocation(self) -> None:
        for command, expected in [
            ("/opt/bin/python3.12 -m corral.scan_worker --limit 200", True),
            ("/opt/Python -m corral.scan_worker --parent-pid 42", True),
            ("python -m corral.scan_worker_other", False),
            ("python -c 'print(\"corral.scan_worker\")'", False),
            ("sh -c 'python -m corral.scan_worker'", False),
            ("echo corral.scan_worker", False),
            ("", False),
            ("python -m 'corral.scan_worker", False),
        ]:
            with self.subTest(command=command), mock.patch.object(
                self.scan_worker, "_process_command", return_value=command,
            ):
                self.assertEqual(self.scan_worker._is_scan_worker_process(42), expected)

    def test_reap_does_not_signal_replaced_heartbeat(self) -> None:
        self._write_heartbeat(424242, cohort="old")
        def replaced(_pid):
            self._write_heartbeat(424243)
            return True
        with mock.patch.object(self.scan_worker, "_pid_alive", return_value=True), \
                mock.patch.object(self.scan_worker, "_is_scan_worker_process", side_effect=replaced), \
                mock.patch.object(self.scan_worker.os, "kill") as kill:
            self.assertIsNone(self.scan_worker.reap_obsolete_worker())
        kill.assert_not_called()

    def test_ensure_does_not_spawn_behind_held_singleton(self) -> None:
        lock = self.scan_worker._acquire_singleton_lock()
        self.assertIsNotNone(lock)
        self.addCleanup(lock.close)
        with mock.patch.object(subprocess, "Popen") as popen:
            self.assertIsNone(self.scan_worker.ensure_scan_worker(50))
        popen.assert_not_called()

    def test_reap_uses_released_flock_instead_of_zombie_pid(self) -> None:
        with mock.patch.object(self.scan_worker, "obsolete_worker_pid", return_value=424242), \
                mock.patch.object(self.scan_worker, "_pid_alive", return_value=True), \
                mock.patch.object(self.scan_worker.os, "kill"), \
                mock.patch.object(self.scan_worker.time, "sleep") as sleep:
            self.assertEqual(self.scan_worker.reap_obsolete_worker(), 424242)
        sleep.assert_not_called()

    def test_spawned_child_is_waited_without_blocking_caller(self) -> None:
        import threading

        collected = threading.Event()
        child = mock.Mock()
        child.wait.side_effect = lambda: collected.set()
        self.scan_worker._reap_child_later(child)
        self.assertTrue(collected.wait(2))
        child.wait.assert_called_once_with()

    def test_editable_or_shadowed_source_beats_stale_metadata(self) -> None:
        source = self.cache / "__init__.py"
        source.write_text('__version__ = "0.2.17"\n', encoding="utf-8")
        dist = mock.Mock(version="0.2.16")
        dist.locate_file.return_value = self.cache / "site-packages/sesskit/__init__.py"
        with mock.patch("importlib.metadata.distribution", return_value=dist), \
                mock.patch.dict(sys.modules, {"sesskit": mock.Mock(__file__=str(source))}):
            self.assertEqual(self.scan_worker._installed_provider_version(), "0.2.17")
            source.write_text('__version__ = "0.2.18"\n', encoding="utf-8")
            self.assertEqual(self.scan_worker._installed_provider_version(), "0.2.18")

    def test_missing_distribution_version_is_unknown_not_drift(self) -> None:
        from importlib.metadata import PackageNotFoundError

        with mock.patch("importlib.metadata.distribution", side_effect=PackageNotFoundError), \
                mock.patch("corral.updater.install_replaced", return_value=False):
            self.assertEqual(self.scan_worker._installed_provider_version(), "")
            self.assertFalse(self.scan_worker.provider_drift())

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
        env["HOME"] = str(self.cache)
        env["XDG_DATA_HOME"] = str(self.cache / "data")
        env["XDG_CONFIG_HOME"] = str(self.cache / "config")
        env["CODEX_HOME"] = str(self.cache / "codex")
        env["CLAUDE_CONFIG_DIR"] = str(self.cache / "claude")
        env["PI_CODING_AGENT_DIR"] = str(self.cache / "pi")
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
        index = self.scan_index.index_path()
        self.assertTrue(index.exists())
        with open(index, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload.get("version"), 1)
        self.assertGreaterEqual(int(payload.get("limit") or 0), 200)


class _FakeTime:
    """Deterministic clock for run_loop: sleeps advance instantly."""

    def __init__(self, start: float = 0.0):
        self.now = float(start)

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


class _NullWatcher:
    """FS-event stub: quiet by default; subclass or hook to inject events."""

    def wait(self, timeout=None):  # noqa: ANN001, ANN202
        return False

    def clear(self) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 1.0) -> None:  # noqa: ARG002
        pass


class _ChurnWatcher(_NullWatcher):
    """FS-event stub that always reports writes (sustained agent churn)."""

    def wait(self, timeout=None):  # noqa: ANN001, ANN202
        return True


class _TimedWatcher(_NullWatcher):
    """FS-event stub that fires only before a fake-clock cutoff (churn stops)."""

    def __init__(self, clock, until: float):
        self._clock = clock
        self._until = float(until)

    def wait(self, timeout=None):  # noqa: ANN001, ANN202
        return self._clock() < self._until


class _AlwaysParseRegistry:
    """Stub that always locally parses (simulates TTL-miss churn passes)."""

    def __init__(self, buckets_fn):
        self._buckets_fn = buckets_fn
        self.parse_at: list[float] = []
        self.last_scan_shared = False
        self._clock = time.monotonic

    def scan_all(self, limit, keep_ids_by_runtime=None, *, prefer_shared=True):
        import copy

        from corral import scan_index

        self.parse_at.append(self._clock())
        result = copy.deepcopy(self._buckets_fn())
        scan_index.publish(
            result, limit=limit, keep_ids_by_runtime=keep_ids_by_runtime,
        )
        self.last_scan_shared = False
        return result


class ChurnBackoffMathTests(unittest.TestCase):
    def test_gap_ramps_from_floor_to_cap(self) -> None:
        from corral import scan_worker

        self.assertEqual(scan_worker._parse_gap_seconds(0, 2.0, False), 2.0)
        self.assertEqual(scan_worker._parse_gap_seconds(1, 2.0, False), 4.0)
        self.assertEqual(scan_worker._parse_gap_seconds(2, 2.0, False), 8.0)
        self.assertEqual(scan_worker._parse_gap_seconds(3, 2.0, False), 16.0)
        self.assertEqual(
            scan_worker._parse_gap_seconds(4, 2.0, False),
            scan_worker.CHURN_BACKOFF_MAX_SECONDS,
        )
        self.assertEqual(
            scan_worker._parse_gap_seconds(99, 2.0, False),
            scan_worker.CHURN_BACKOFF_MAX_SECONDS,
        )

    def test_gap_cap_rises_under_pressure(self) -> None:
        from corral import scan_worker

        self.assertEqual(
            scan_worker._parse_gap_seconds(99, 2.0, True),
            scan_worker.CHURN_BACKOFF_PRESSURED_MAX_SECONDS,
        )
        self.assertGreater(
            scan_worker.CHURN_BACKOFF_PRESSURED_MAX_SECONDS,
            scan_worker.CHURN_BACKOFF_MAX_SECONDS,
        )

    def test_fingerprint_ignores_message_churn(self) -> None:
        from corral import scan_worker

        before = {"opencode": [{"id": "a", "mtime": 10, "title": "x"}]}
        churned = {"opencode": [{"id": "a", "mtime": 99, "title": "y"}]}
        arrived = {"opencode": [
            {"id": "a", "mtime": 99, "title": "y"},
            {"id": "b", "mtime": 100, "title": "z"},
        ]}
        fp = scan_worker._bucket_fingerprint(before)
        self.assertEqual(fp, scan_worker._bucket_fingerprint(churned))
        self.assertNotEqual(fp, scan_worker._bucket_fingerprint(arrived))


class WorkerBackoffLoopTests(unittest.TestCase):
    """End-to-end cadence through run_loop with a fake clock (no real sleeps)."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cache = Path(self._tmpdir.name)
        self.fake = _FakeTime(start=time.time())
        self.patches = [
            mock.patch("corral.scan_index.cache_dir", return_value=self.cache),
            mock.patch("corral.scan_index._shared_index_enabled", return_value=True),
            mock.patch("corral.scan_worker._worker_enabled", return_value=True),
            mock.patch("corral.cache.cache_dir", return_value=self.cache),
            mock.patch(
                "corral.scan_worker._remembered_keep_ids", return_value={},
            ),
            mock.patch("corral.scan_worker._memory_pressured", return_value=False),
            mock.patch("corral.scan_worker.provider_drift", return_value=False),
            mock.patch("corral.scan_worker.time", self.fake),
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

    def _session(self, sid: str, mtime: float) -> dict:
        return {
            "source": "opencode", "id": sid, "short_id": sid,
            "file_mtime": mtime, "mtime": mtime, "cwd": "/tmp", "live": False,
        }

    def _run(self, registry, max_passes: int, watcher=None) -> int:
        return self.scan_worker.run_loop(
            limit=50, interval=2.0, parent_pid=os.getppid(),
            max_passes=max_passes, registry=registry,
            watcher=watcher if watcher is not None else _ChurnWatcher(),
        )

    def test_sustained_churn_backs_off_parses(self) -> None:
        buckets = {"opencode": [self._session("a", 30)]}
        registry = _AlwaysParseRegistry(lambda: buckets)
        registry._clock = self.fake.monotonic
        code = self._run(registry, max_passes=35)  # ~68 fake seconds
        self.assertEqual(code, 0)
        # Old fixed poll would parse on most of the 35 passes; backoff keeps
        # full parses near the TTL-cycle count while still scanning sometimes.
        self.assertGreaterEqual(len(registry.parse_at), 2)
        self.assertLessEqual(len(registry.parse_at), 8)
        got = self.scan_index.try_consume(50, {})
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual([item["id"] for item in got["opencode"]], ["a"])

    def test_arrival_resets_to_floor(self) -> None:
        t0 = self.fake.now

        def buckets():
            items = [self._session("a", 30)]
            if self.fake.now >= t0 + 10.0:
                items.append(self._session("b", self.fake.now))
            return {"opencode": items}

        registry = _AlwaysParseRegistry(buckets)
        registry._clock = self.fake.monotonic
        code = self._run(registry, max_passes=14)
        self.assertEqual(code, 0)
        got = self.scan_index.try_consume(50, {})
        self.assertIsNotNone(got)
        assert got is not None
        self.assertIn("b", [item["id"] for item in got["opencode"]])
        # A parse must have picked the arrival up promptly (gap cap is 24 s;
        # mid-ramp it must be far sooner).
        arrival_parses = [at for at in registry.parse_at if at >= t0 + 10.0]
        self.assertTrue(arrival_parses)
        self.assertLessEqual(arrival_parses[0] - (t0 + 10.0), 12.0)

    def test_keep_alive_republishes_without_overwriting_fresher(self) -> None:
        from corral import scan_index

        republished = []
        real_republish = self.scan_worker._republish

        def counting(scanned, *, limit, keep_ids, expected_published_at=None):
            republished.append(self.fake.now)
            return real_republish(
                scanned, limit=limit, keep_ids=keep_ids,
                expected_published_at=expected_published_at,
            )

        buckets = {"opencode": [self._session("a", 30)]}
        registry = _AlwaysParseRegistry(lambda: buckets)
        registry._clock = self.fake.monotonic
        with mock.patch.object(
            self.scan_worker, "_republish", side_effect=counting,
        ):
            code = self._run(registry, max_passes=12)
        self.assertEqual(code, 0)
        # Backed-off passes keep-alive republish on schedule (gap 8 s).
        self.assertTrue(republished)
        # The republished payload still carries the worker's buckets.
        payload_path = self.scan_index.index_path()
        with open(payload_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(
            [item["id"] for item in payload["sessions"]["opencode"]], ["a"],
        )
        got = scan_index.try_consume(50, {})
        self.assertIsNotNone(got)

    def test_fresher_foreign_publish_is_adopted_not_overwritten(self) -> None:
        from corral import scan_index

        buckets = {"opencode": [self._session("a", 30)]}
        registry = _AlwaysParseRegistry(lambda: buckets)
        registry._clock = self.fake.monotonic

        class _Injector(_NullWatcher):
            def __init__(self, test):
                self.test = test
                self.injected = False

            def wait(self, timeout=None):
                if not self.injected and len(registry.parse_at) >= 2:
                    # A fresher daemon publish (same content shape, newer).
                    scan_index.publish(
                        {"opencode": [self.test._session("a", 31)]},
                        limit=200, keep_ids_by_runtime={},
                    )
                    self.injected = True
                return False

        code = self.scan_worker.run_loop(
            limit=50, interval=2.0, parent_pid=os.getppid(),
            max_passes=6, registry=registry, watcher=_Injector(self),
        )
        self.assertEqual(code, 0)
        self.assertTrue(registry.parse_at)  # still parses on schedule
        got = scan_index.try_consume(50, {})
        self.assertIsNotNone(got)

    def test_quiet_returns_to_floor(self) -> None:
        buckets = {"opencode": [self._session("a", 30)]}
        registry = _AlwaysParseRegistry(lambda: buckets)
        registry._clock = self.fake.monotonic
        t0 = self.fake.now
        watcher = _TimedWatcher(self.fake.monotonic, t0 + 30.0)
        code = self.scan_worker.run_loop(
            limit=50, interval=2.0, parent_pid=os.getppid(),
            max_passes=44, registry=registry, watcher=watcher,
        )
        self.assertEqual(code, 0)
        rel = [at - t0 for at in registry.parse_at]
        # Backoff engaged during churn: sparse parses mid-run.
        mid = [at for at in rel if 20.0 < at < 60.0]
        self.assertLessEqual(len(mid), 3)
        # After ~30 s quiet the floor cadence resumes (~2 s spacing).
        late = [at for at in rel if at >= 60.0]
        self.assertGreaterEqual(len(late), 5)
        gaps = [b - a for a, b in zip(late, late[1:], strict=False)]
        self.assertTrue(gaps)
        self.assertLessEqual(max(gaps), 3.0)


if __name__ == "__main__":
    unittest.main()
