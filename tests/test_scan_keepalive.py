"""Independent keep-alive during a blocked parse; isolated completed snapshots."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from corral import scan_index, scan_worker


class ScanKeepAliveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        for patch in (
            mock.patch("corral.cache.cache_dir", return_value=self.path),
            mock.patch("corral.scan_index.cache_dir", return_value=self.path),
            mock.patch("corral.scan_index._shared_index_enabled", return_value=True),
            mock.patch("corral.scan_worker._worker_enabled", return_value=True),
            mock.patch("corral.scan_worker.provider_drift", return_value=False),
            mock.patch("corral.scan_worker._remembered_keep_ids", return_value={}),
            mock.patch("corral.observe.event"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        scan_index._LAST_PUBLISH.update(at=0.0, path="", keys=None, keep_ids=None)
        self.addCleanup(scan_index._LAST_PUBLISH.clear)
        self.buckets = {"claude": [{"id": "completed", "mtime": 1}]}

    def helper(self, buckets=None, stamp=0.0):
        return scan_worker._ScanKeepAlive(
            self.buckets if buckets is None else buckets,
            limit=200, keep_ids={}, published_wall=stamp, parent_pid=os.getppid(),
        )

    def test_tick_adopts_newer_foreign_snapshot(self) -> None:
        stamp = scan_index.publish(self.buckets, limit=200)
        helper = self.helper(stamp=stamp)
        foreign = {"claude": [{"id": "foreign", "mtime": 2}]}
        scan_index.publish(foreign, limit=200)
        helper._tick()
        self.assertEqual(helper.buckets, foreign)
        self.assertEqual(scan_index.try_consume(50), foreign)

    def test_noncovering_foreign_snapshot_is_not_overwritten(self) -> None:
        stamp = scan_index.publish(self.buckets, limit=200)
        helper = self.helper(stamp=stamp)
        helper.keep_ids = {"claude": {"new-pin"}}
        foreign = {"claude": [{"id": "foreign", "mtime": 2}]}
        scan_index.publish(foreign, limit=200)
        with mock.patch.object(scan_worker, "_republish") as republish:
            helper._tick()
        republish.assert_not_called()
        self.assertEqual(scan_index.try_consume(50), foreign)

    def test_first_parse_does_not_invent_a_snapshot(self) -> None:
        helper = self.helper()
        helper.buckets = None
        with mock.patch.object(scan_worker, "_republish") as republish:
            helper._tick()
        republish.assert_not_called()
        self.assertFalse(scan_index.index_path().exists())

    def test_drift_stops_keepalive_before_heartbeat_or_republish(self) -> None:
        helper = self.helper()
        helper.last_heartbeat -= scan_worker.HEARTBEAT_MIN_INTERVAL_SECONDS
        with mock.patch.object(scan_worker, "provider_drift", return_value=True), \
                mock.patch.object(scan_worker, "_write_heartbeat") as heartbeat, \
                mock.patch.object(scan_worker, "_republish") as republish:
            helper._tick()
        self.assertTrue(helper.stop.is_set())
        heartbeat.assert_not_called()
        republish.assert_not_called()

    def test_keepalive_stops_and_joins_on_exception(self) -> None:
        helper = self.helper()
        with self.assertRaisesRegex(RuntimeError, "fixture"), helper:
            raise RuntimeError("fixture")
        self.assertFalse(helper.thread.is_alive())
        self.assertTrue(helper.stop.is_set())

    def test_real_loop_keeps_snapshot_and_heartbeat_fresh_while_scan_blocked(self) -> None:
        scan_index.publish(self.buckets, limit=200)
        started, release, refreshed = (threading.Event() for _ in range(3))
        published = []
        codes = []
        original = scan_worker._republish

        def record(*args, **kwargs):
            result = original(*args, **kwargs)
            if result is not None:
                published.append(result)
                if len(published) >= 3:
                    refreshed.set()
            return result

        def blocking_scan(**kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("fixture did not release scan")
            return self.buckets, False

        with mock.patch.object(scan_worker, "HEARTBEAT_MIN_INTERVAL_SECONDS", 0.03), \
                mock.patch.object(scan_worker, "REPUBLISH_GAP_SECONDS", 0.04), \
                mock.patch.object(scan_index, "PUBLISH_MIN_INTERVAL_SECONDS", 0.01), \
                mock.patch.object(scan_worker, "_scan_local", side_effect=blocking_scan), \
                mock.patch.object(scan_worker, "_republish", side_effect=record):
            thread = threading.Thread(target=lambda: codes.append(scan_worker.run_loop(
                limit=200, interval=0.01, parent_pid=os.getppid(), max_passes=2,
                registry=mock.Mock(), watcher=mock.Mock(arrival_seq=0),
            )))
            thread.start()
            try:
                self.assertTrue(started.wait(2), "production loop never started parse")
                self.assertTrue(refreshed.wait(3), "no independent keep-alive progress")
                self.assertFalse(release.is_set())
                self.assertTrue(scan_worker.is_active(max_age=0.12))
                self.assertIsNotNone(scan_index.try_consume(50, max_age=0.12))
                self.assertGreaterEqual(len(published), 3)
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(codes, [0])
        self.assertFalse(any(t.name == "corral-scan-keepalive" for t in threading.enumerate()))


if __name__ == "__main__":
    unittest.main()
