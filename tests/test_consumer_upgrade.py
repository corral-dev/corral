"""Provider-upgrade consumer acceptance: cohort-gated derived data.

SessKit 0.2.4 changed Claude completion identity. Older derived session
metadata, shared snapshots, and worker heartbeats must be rejected before the
remote's cold baseline — only the new contract is consumed, and history must
never re-notify.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sesskit import titles as sesskit_titles

from corral.cache import PerformanceCache, file_signature, provider_cohort
from corral.remote import crypto
from corral.remote.config import PairedDevice, RemoteState
from corral.remote.push import PushNotifier
from corral.remote.sessions import SessionHub

_HAS_CRYPTO = crypto.available()
_SKIP = "未安装 remote 附加组件（pip install '.[remote]'）"

OLD_PARSER_BASE = "2026-09-29.3"
OLD_PROVIDER_VERSION = "0.2.3"
OLD_HOST_TAG = "('host', 'corral', True, None, (), ())"


def _old_version(tag: str = "") -> str:
    return f"{OLD_PARSER_BASE}+sesskit-{OLD_PROVIDER_VERSION}{tag}"


class CacheCohortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cache.sqlite3"
        self.cache = PerformanceCache(self.path)
        self.env = mock.patch.dict(os.environ, {"CORRAL_CACHE": "1"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        PerformanceCache._last_stale_session_purge = 0.0
        self.addCleanup(setattr, PerformanceCache, "_last_stale_session_purge", 0.0)

    def _history(self, name: str = "s.jsonl") -> Path:
        path = Path(self.temp.name) / name
        path.write_text('{"type": "user"}\n', encoding="utf-8")
        return path

    def _seed_row(self, runtime: str, path: Path, version: str, payload: dict) -> None:
        signature = file_signature(str(path))
        assert signature is not None
        with self.cache._connect() as conn:
            assert conn is not None
            conn.execute(
                "INSERT OR REPLACE INTO session_meta "
                "(runtime,path,dev,ino,size,mtime_ns,parser_version,payload,accessed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    runtime, str(path), *signature, version,
                    json.dumps(payload), time.time(),
                ),
            )
            conn.commit()

    def test_cohort_carries_provider_version(self) -> None:
        cohort = provider_cohort()
        self.assertTrue(cohort.startswith(OLD_PARSER_BASE + "+sesskit-"))
        self.assertIn("sesskit-", cohort)

    def test_old_provider_row_is_a_miss(self) -> None:
        """Warm 0.2.3 row under a 0.2.4 consumer: miss, never stale semantics."""
        history = self._history()
        self._seed_row(
            "claude", history, _old_version(OLD_HOST_TAG),
            {"id": "s", "completion_id": "stale-id-from-0-2-3"},
        )
        self.assertIsNone(self.cache.get_session("claude", str(history), OLD_HOST_TAG))
        # And a fresh native parse disagrees with the stale row (new contract).
        from sesskit.parsers import claude

        user = {"type": "user", "uuid": "u1", "timestamp": "2026-09-30T20:00:00Z",
                "message": {"content": "probe"}}
        final = {"type": "assistant", "uuid": "a-1", "timestamp": "2026-09-30T20:01:00Z",
                 "message": {"content": [{"type": "text", "text": "done"}],
                             "stop_reason": "end_turn"}}
        history.write_text(
            "\n".join(json.dumps(row) for row in (user, final)) + "\n",
            encoding="utf-8",
        )
        fresh = claude._build_session_info(str(history), "probe")
        assert fresh is not None
        self.assertEqual(fresh["status_tag"], sesskit_titles.STATUS_DONE)
        self.assertTrue(fresh.get("completion_id"))
        self.assertNotEqual(fresh.get("completion_id"), "stale-id-from-0-2-3")

    def test_current_host_tagged_row_hits_and_survives_prune(self) -> None:
        """Host-tagged rows are live entries: hits now, kept by cold-flush purge."""
        from sesskit.parsers.common import HostExtension, host_cache_tag

        host = HostExtension(name="corral")
        tag = host_cache_tag(host)
        self.assertTrue(tag)
        history = self._history()
        self.cache.put_session("claude", str(history), {"id": "s"}, tag)
        self.cache.flush_pending()
        self.assertIsNotNone(self.cache.get_session("claude", str(history), tag))
        # Cold flush runs the hourly-bounded purge; fresh rows must survive it.
        PerformanceCache._last_stale_session_purge = 0.0
        self.cache.prune_stale_sessions()
        with self.cache._connect() as conn:
            assert conn is not None
            count = conn.execute("SELECT count(*) FROM session_meta").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertIsNotNone(self.cache.get_session("claude", str(history), tag))

    def test_prune_drops_old_provider_rows(self) -> None:
        live = self._history("live.jsonl")
        self.cache.put_session("claude", str(live), {"id": "live"})
        self.cache.flush_pending()
        stale = self._history("stale.jsonl")
        self._seed_row("claude", stale, _old_version(OLD_HOST_TAG), {"id": "stale"})
        PerformanceCache._last_stale_session_purge = 0.0
        removed = self.cache.prune_stale_sessions()
        self.assertGreaterEqual(removed, 1)
        with self.cache._connect() as conn:
            assert conn is not None
            remaining = {
                row[0] for row in conn.execute("SELECT path FROM session_meta").fetchall()
            }
        self.assertEqual(remaining, {str(live)})


class IndexCohortTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cache = Path(self._tmpdir.name)
        self.patches = [
            mock.patch("corral.scan_index.cache_dir", return_value=self.cache),
            mock.patch("corral.scan_index._shared_index_enabled", return_value=True),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        from corral import scan_index

        self.scan_index = scan_index
        scan_index._LAST_PUBLISH.update(
            {"at": 0.0, "path": "", "limit": 0, "keys": None}
        )
        self.addCleanup(
            scan_index._LAST_PUBLISH.update,
            {"at": 0.0, "path": "", "limit": 0, "keys": None},
        )

    def _session(self, runtime: str, sid: str, mtime: float) -> dict:
        return {
            "source": runtime, "id": sid, "short_id": sid,
            "file_mtime": mtime, "mtime": mtime, "cwd": "/tmp", "live": False,
        }

    def _read_payload(self) -> dict:
        with open(self.cache / "scan-index.json", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_payload(self, payload: dict) -> None:
        with open(self.cache / "scan-index.json", "w", encoding="utf-8") as handle:
            json.dump(payload, handle)

    def test_current_cohort_roundtrips(self) -> None:
        self.scan_index.publish({"claude": [self._session("claude", "a", 1)]}, limit=50)
        payload = self._read_payload()
        self.assertEqual(payload.get("provider_cohort"), provider_cohort())
        self.assertIsNotNone(self.scan_index.try_consume(50))

    def test_legacy_snapshot_without_cohort_rejected(self) -> None:
        self.scan_index.publish({"claude": [self._session("claude", "a", 1)]}, limit=50)
        payload = self._read_payload()
        del payload["provider_cohort"]
        self._write_payload(payload)
        self.assertIsNone(self.scan_index.try_consume(50))
        self.assertIsNone(self.scan_index.published_meta())

    def test_obsolete_cohort_snapshot_rejected(self) -> None:
        self.scan_index.publish({"claude": [self._session("claude", "a", 1)]}, limit=50)
        payload = self._read_payload()
        payload["provider_cohort"] = _old_version()
        self._write_payload(payload)
        self.assertIsNone(self.scan_index.try_consume(50))
        self.assertIsNone(self.scan_index.published_meta())


@unittest.skipUnless(_HAS_CRYPTO, _SKIP)
class HubUpgradeTests(unittest.TestCase):
    """Warm old cache → new consumer: restart adds zero, new round adds one.

    Real SessionHub._detect_status_changes + real PushNotifier with an
    isolated capture sender; sessions come from real SessKit 0.2.4 parses of
    one Claude fixture whose native history never changes identity underneath.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"CORRAL_CACHE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        import corral.split_layout as split_layout

        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.device_private = crypto.generate_private_key_bytes()
        self.host_private = crypto.generate_private_key_bytes()
        self.sent: list = []
        state = RemoteState(
            host_id="host",
            host_name="Mac",
            devices=[
                PairedDevice(
                    id="d1",
                    name="iPhone",
                    public_key=crypto.public_key_bytes(self.device_private).hex(),
                    paired_at=1.0,
                    push_token="a" * 64,
                    push_env="sandbox",
                )
            ],
        )
        self.notifier = PushNotifier(
            state,
            self.host_private,
            sender=self._capture,
            sent_path=Path(self._tmp.name) / "push-sent.json",
            pending_path=Path(self._tmp.name) / "push-pending.json",
        )
        self.hub = SessionHub(scan_limit=10)
        self.hub.layout_db = split_layout.SidebarLayoutDB()
        self.hub.set_status_hook(self.notifier.on_status_change)

    def tearDown(self) -> None:
        self.hub.stop()

    def _capture(self, token: str, env: str, payload: bytes, push_id: str = "") -> None:
        self.sent.append((token, env, payload, push_id))

    def _fixture(self) -> Path:
        path = Path(self._tmp.name) / "upgrade.jsonl"
        rows = [
            {"type": "user", "uuid": "u1", "timestamp": "2026-09-30T20:00:00Z",
             "message": {"content": "upgrade probe"}},
            {"type": "assistant", "uuid": "a-1", "timestamp": "2026-09-30T20:01:00Z",
             "message": {"content": [{"type": "text", "text": "done"}],
                         "stop_reason": "end_turn"}},
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    def _parse(self, path: Path) -> dict:
        from sesskit.parsers import claude

        info = claude._build_session_info(str(path), "probe")
        assert info is not None
        self.assertEqual(info["status_tag"], sesskit_titles.STATUS_DONE)
        self.assertTrue(info.get("completion_id"))
        # An old completion: stale mtime defeats the first-sight freshness rule.
        info["mtime"] = time.time() - 86400.0
        return info

    def test_restart_baseline_and_rescan_add_zero(self) -> None:
        path = self._fixture()
        session = self._parse(path)
        first_id = session["completion_id"]
        self.hub.store.sessions = {"claude": [session]}
        # Cold baseline on old history: silent.
        self.hub._detect_status_changes()
        self.assertEqual(len(self.sent), 0)
        # Metadata-append rescan, same terminal event and id: still silent.
        self.hub._detect_status_changes()
        self.assertEqual(len(self.sent), 0)
        # True restart: fresh hub + reloaded ledgers, same snapshot: zero added.
        self.hub.stop()
        hub2 = SessionHub(scan_limit=10)
        self.addCleanup(hub2.stop)
        import corral.split_layout as split_layout

        hub2.layout_db = split_layout.SidebarLayoutDB()
        notifier2 = PushNotifier(
            self.notifier.state,
            self.host_private,
            sender=self._capture,
            sent_path=Path(self._tmp.name) / "push-sent.json",
            pending_path=Path(self._tmp.name) / "push-pending.json",
        )
        hub2.set_status_hook(notifier2.on_status_change)
        hub2.store.sessions = {"claude": [session]}
        hub2._detect_status_changes()
        self.assertEqual(len(self.sent), 0)
        self.assertEqual(session["completion_id"], first_id)

    def test_new_native_final_event_adds_one(self) -> None:
        path = self._fixture()
        session = self._parse(path)
        self.hub.store.sessions = {"claude": [session]}
        self.hub._detect_status_changes()
        self.assertEqual(len(self.sent), 0)
        # Genuine new round, identical final text, distinct native event id.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"type": "user", "uuid": "u2",
                            "timestamp": "2026-09-30T21:00:00Z",
                            "message": {"content": "again"}}) + "\n"
            )
            handle.write(
                json.dumps({"type": "assistant", "uuid": "a-2",
                            "timestamp": "2026-09-30T21:01:00Z",
                            "message": {"content": [{"type": "text", "text": "done"}],
                                        "stop_reason": "end_turn"}}) + "\n"
            )
        second = self._parse(path)
        self.assertNotEqual(second["completion_id"], session["completion_id"])
        second["mtime"] = time.time()
        self.hub.store.sessions = {"claude": [second]}
        self.hub._detect_status_changes()
        self.assertEqual(len(self.sent), 1)
