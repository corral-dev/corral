"""Cross-process shared scan index."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from corral import scan_index


class ScanIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cache = Path(self._tmpdir.name)
        self.patches = [
            mock.patch("corral.scan_index.cache_dir", return_value=self.cache),
            mock.patch("corral.scan_index.cache_enabled", return_value=True),
            mock.patch("corral.scan_index._shared_index_enabled", return_value=True),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        scan_index._LAST_PUBLISH.update({"at": 0.0, "path": "", "limit": 0, "keys": None})
        self.addCleanup(
            scan_index._LAST_PUBLISH.update,
            {"at": 0.0, "path": "", "limit": 0, "keys": None},
        )
        scan_index._LAST_MISS.update({"at": 0.0, "reason": "none", "detail": {}})
        self.addCleanup(
            scan_index._LAST_MISS.update,
            {"at": 0.0, "reason": "none", "detail": {}},
        )
        scan_index._LAST_MISS_EVENT.update({"at": 0.0, "reason": ""})
        self.addCleanup(
            scan_index._LAST_MISS_EVENT.update, {"at": 0.0, "reason": ""},
        )

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

    def test_publish_and_consume_covering_index(self) -> None:
        scanned = {
            "claude": [
                self._session("claude", "a", 30),
                self._session("claude", "b", 20),
                self._session("claude", "c", 10),
            ],
            "codex": [self._session("codex", "x", 5)],
        }
        scan_index.publish(scanned, limit=50, keep_ids_by_runtime={"claude": {"c"}})
        got = scan_index.try_consume(2, {"claude": {"c"}})
        self.assertIsNotNone(got)
        assert got is not None
        claude_ids = [item["id"] for item in got["claude"]]
        self.assertEqual(claude_ids[:2], ["a", "b"])
        self.assertIn("c", claude_ids)
        self.assertEqual([item["id"] for item in got["codex"]], ["x"])

    def test_miss_when_publisher_limit_too_small(self) -> None:
        scan_index.publish(
            {"claude": [self._session("claude", "a", 1)]},
            limit=10,
        )
        self.assertIsNone(scan_index.try_consume(50))

    def test_miss_when_keep_id_absent(self) -> None:
        scan_index.publish(
            {"claude": [self._session("claude", "a", 1)]},
            limit=50,
        )
        self.assertIsNone(scan_index.try_consume(50, {"claude": {"missing"}}))

    def test_stale_keep_ids_excused_when_publisher_could_not_cover(self) -> None:
        # Publisher scanned with a keep set containing long-gone sessions;
        # a consumer with the same sidebar memory must still hit.
        scanned = {"claude": [self._session("claude", "a", 1)]}
        scan_index.publish(
            scanned, limit=50, keep_ids_by_runtime={"claude": {"a", "gone-1", "gone-2"}},
        )
        got = scan_index.try_consume(50, {"claude": {"a", "gone-1", "gone-2"}})
        self.assertIsNotNone(got)

    def test_new_pin_not_seen_by_publisher_still_forces_miss(self) -> None:
        scanned = {"claude": [self._session("claude", "a", 1)]}
        scan_index.publish(
            scanned, limit=50, keep_ids_by_runtime={"claude": {"a"}},
        )
        self.assertIsNone(scan_index.try_consume(50, {"claude": {"a", "fresh-pin"}}))

    def test_publish_throttle_skips_identical_republish(self) -> None:
        scanned = {"claude": [self._session("claude", "a", 1)]}
        scan_index.publish(scanned, limit=50)
        first = json.loads(scan_index.index_path().read_text(encoding="utf-8"))
        scan_index.publish(
            {"claude": [dict(self._session("claude", "a", 1))]}, limit=50,
        )
        second = json.loads(scan_index.index_path().read_text(encoding="utf-8"))
        self.assertEqual(first["published_at"], second["published_at"])
        scan_index.publish(
            {"claude": [self._session("claude", "a", 1), self._session("claude", "b", 2)]},
            limit=50,
        )
        third = json.loads(scan_index.index_path().read_text(encoding="utf-8"))
        self.assertGreaterEqual(third["published_at"], second["published_at"])
        self.assertEqual(len(third["sessions"]["claude"]), 2)

    def test_publish_cleans_stale_tmps(self) -> None:
        stale = self.cache / "scan-index.json.tmp.424242"
        stale.write_text("{}", encoding="utf-8")
        old = time.time() - 600
        os.utime(stale, (old, old))
        fresh = self.cache / "scan-index.json.tmp.424243"
        fresh.write_text("{}", encoding="utf-8")
        scan_index.publish({"claude": [self._session("claude", "a", 1)]}, limit=50)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_miss_when_stale(self) -> None:
        scan_index.publish(
            {"claude": [self._session("claude", "a", 1)]},
            limit=50,
        )
        path = scan_index.index_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["published_at"] = time.time() - 100
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertIsNone(scan_index.try_consume(50, max_age=12.0))

    def test_disabled_cache_skips(self) -> None:
        with mock.patch("corral.scan_index._shared_index_enabled", return_value=False):
            scan_index.publish(
                {"claude": [self._session("claude", "a", 1)]},
                limit=50,
            )
            self.assertIsNone(scan_index.try_consume(50))
            self.assertFalse(scan_index.index_path().exists())

    def test_miss_reason_recorded_without_session_ids(self) -> None:
        scan_index.publish(
            {"claude": [self._session("claude", "a", 1)]},
            limit=10,
        )
        self.assertIsNone(scan_index.try_consume(50))
        miss = scan_index.last_miss()
        self.assertEqual(miss["reason"], "limit")
        self.assertEqual(miss["detail"]["published_limit"], 10)
        self.assertEqual(miss["detail"]["need_limit"], 50)
        scan_index.publish(
            {"claude": [self._session("claude", "b", 2)]},
            limit=50,
        )
        self.assertIsNone(scan_index.try_consume(50, {"claude": {"missing"}}))
        miss = scan_index.last_miss()
        self.assertEqual(miss["reason"], "keep_ids")
        self.assertEqual(miss["detail"]["runtime"], "claude")
        self.assertEqual(miss["detail"]["missing_count"], 1)
        self.assertIsNone(scan_index.try_consume(50, max_age=-1))
        self.assertEqual(scan_index.last_miss()["reason"], "ttl")

    def test_registry_reports_consume_miss_reason(self) -> None:
        from corral.runtime.registry import RuntimeRegistry

        runtime = mock.Mock(id="claude", display_name="Claude")
        runtime.scan_signature.return_value = None
        runtime.scan_sessions.return_value = []
        registry = RuntimeRegistry((runtime,))
        scan_index.publish({"claude": []}, limit=10)
        registry.scan_all(50)
        self.assertFalse(registry.last_scan_shared)
        self.assertEqual(registry.last_scan_shared_miss, "limit")

    def test_narrow_publish_defers_to_fresh_wide_index(self) -> None:
        wide = {"claude": [self._session("claude", "a", 1)]}
        scan_index.publish(wide, limit=200)
        before = scan_index.index_path().read_text(encoding="utf-8")
        scan_index.publish(
            {"claude": [self._session("claude", "b", 2)]}, limit=50,
        )
        # Narrower publish must not clobber the fresh wide index deep
        # consumers (daemon/worker at 200) depend on.
        self.assertEqual(scan_index.index_path().read_text(encoding="utf-8"), before)

    def test_narrow_publish_proceeds_when_cover_stale_or_narrow(self) -> None:
        scan_index.publish(
            {"claude": [self._session("claude", "a", 1)]}, limit=50,
        )
        path = scan_index.index_path()
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["published_at"] = time.time() - 100
        path.write_text(json.dumps(payload), encoding="utf-8")
        scan_index.publish(
            {"claude": [self._session("claude", "b", 2)]}, limit=50,
        )
        fresh = json.loads(path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(fresh["published_at"], payload["published_at"])
        self.assertEqual(
            [item["id"] for item in fresh["sessions"]["claude"]], ["b"],
        )

    def test_miss_event_throttled_per_reason(self) -> None:
        with mock.patch("corral.observe.event") as emitted, mock.patch(
            "corral.scan_index.getenv", return_value=""
        ):
            self.assertIsNone(scan_index.try_consume(50))
            self.assertIsNone(scan_index.try_consume(50))
            self.assertEqual(emitted.call_count, 1)
            self.assertEqual(emitted.call_args[0][0], "scan_index_miss")
            scan_index.publish(
                {"claude": [self._session("claude", "a", 1)]},
                limit=10,
            )
            self.assertIsNone(scan_index.try_consume(50))
            self.assertEqual(emitted.call_count, 2)

    def test_isolate_managed_hosts_disables_shared_index(self) -> None:
        for patch in self.patches:
            patch.stop()
        with (
            mock.patch("corral.scan_index.cache_dir", return_value=self.cache),
            mock.patch("corral.scan_index.cache_enabled", return_value=True),
            mock.patch.dict(os.environ, {"CORRAL_ISOLATE_MANAGED_HOSTS": "1"}, clear=False),
        ):
            self.assertFalse(scan_index._shared_index_enabled())
            scan_index.publish(
                {"claude": [self._session("claude", "a", 1)]},
                limit=50,
            )
            self.assertIsNone(scan_index.try_consume(50))
            self.assertFalse(scan_index.index_path().exists())


class RegistrySharedIndexTests(unittest.TestCase):
    def test_scan_all_reuses_shared_index(self) -> None:
        from corral.runtime.registry import RuntimeRegistry

        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.scan_signature.return_value = "sig-1"
        runtime.scan_sessions.return_value = [
            {"source": "claude", "id": "local", "file_mtime": 1, "mtime": 1},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            with (
                mock.patch("corral.scan_index.cache_dir", return_value=cache),
                mock.patch("corral.scan_index.cache_enabled", return_value=True),
                mock.patch("corral.scan_index._shared_index_enabled", return_value=True),
                mock.patch("corral.cache.scan_period") as period,
            ):
                period.return_value.__enter__ = mock.Mock(return_value=None)
                period.return_value.__exit__ = mock.Mock(return_value=False)
                scan_index.publish(
                    {
                        "claude": [
                            {
                                "source": "claude",
                                "id": "shared",
                                "file_mtime": 9,
                                "mtime": 9,
                            },
                        ],
                    },
                    limit=50,
                )
                registry = RuntimeRegistry((runtime,))
                result = registry.scan_all(50, prefer_shared=True)
                self.assertTrue(registry.last_scan_shared)
                self.assertEqual(result["claude"][0]["id"], "shared")
                runtime.scan_sessions.assert_not_called()

                result = registry.scan_all(50, prefer_shared=False)
                self.assertFalse(registry.last_scan_shared)
                self.assertEqual(result["claude"][0]["id"], "local")
                runtime.scan_sessions.assert_called_once()


if __name__ == "__main__":
    unittest.main()
