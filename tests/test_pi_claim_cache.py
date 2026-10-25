"""Focused acceptance for the Pi claim exact-source parse memo."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from corral import pi_identity


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _claim(
    instance_id: str,
    session_id: str,
    pid: int = 101,
    *,
    updated: datetime | None = None,
    state: str = "active",
    sequence: int = 1,
    phase: str | None = None,
) -> dict:
    stamp = updated or datetime.now(timezone.utc)
    claim = {
        "protocolVersion": 1,
        "extensionVersion": pi_identity.EXTENSION_VERSION,
        "instanceId": instance_id,
        "pid": pid,
        "instanceNonce": "n" * 16,
        "state": state,
        "sessionId": session_id,
        "sessionFile": None,
        "cwd": "/tmp/project",
        "parentSession": None,
        "reason": "startup",
        "updatedAt": _iso(stamp),
        "sequence": sequence,
    }
    if phase is not None:
        claim["agentPhase"] = phase
        claim["agentPhaseEvent"] = "agent_start"
        claim["agentPhaseAt"] = _iso(stamp)
    return claim


class ClaimMemoTests(unittest.TestCase):
    def setUp(self) -> None:
        pi_identity._CLAIM_MEMO.clear()

    def tearDown(self) -> None:
        pi_identity._CLAIM_MEMO.clear()

    def _write(self, directory: Path, name: str, claim: dict) -> Path:
        path = directory / name
        path.write_text(json.dumps(claim), encoding="utf-8")
        return path

    def test_unchanged_reads_skip_open_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            self._write(directory, "a.json", _claim("inst-a", "sess-a", 11))
            self._write(directory, "b.json", _claim("inst-b", "sess-b", 12))
            first = pi_identity.read_claims(td)
            self.assertEqual({c["sessionId"] for c in first}, {"sess-a", "sess-b"})

            with (
                mock.patch("builtins.open", wraps=open) as opened,
                mock.patch.object(pi_identity.json, "loads", wraps=json.loads) as loads,
            ):
                second = pi_identity.read_claims(td)

            self.assertEqual(opened.call_count, 0)
            self.assertEqual(loads.call_count, 0)
            self.assertEqual(second, first)

    def test_atomic_replacement_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            path = self._write(directory, "inst.json", _claim("inst", "sess-old", 11))
            before_inode = path.stat().st_ino
            self.assertEqual(pi_identity.read_claims(td)[0]["sessionId"], "sess-old")

            replacement = path.with_name(".replacement.json")
            replacement.write_text(
                json.dumps(_claim("inst", "sess-new", 11)), encoding="utf-8"
            )
            os.replace(replacement, path)
            self.assertNotEqual(path.stat().st_ino, before_inode)

            self.assertEqual(pi_identity.read_claims(td)[0]["sessionId"], "sess-new")

    def test_same_length_in_place_rewrite_is_detected_even_with_restored_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            old_claim = _claim(
                "inst", "sess-old", 11, updated=datetime(2026, 1, 1, tzinfo=timezone.utc), sequence=1
            )
            new_claim = _claim(
                "inst", "sess-new", 11, updated=datetime(2026, 1, 1, tzinfo=timezone.utc), sequence=1
            )
            old_raw = json.dumps(old_claim)
            new_raw = json.dumps(new_claim)
            self.assertEqual(len(old_raw), len(new_raw))

            path = self._write(directory, "inst.json", old_claim)
            original = path.stat()
            self.assertEqual(pi_identity.read_claims(td)[0]["sessionId"], "sess-old")

            path.write_text(new_raw, encoding="utf-8")
            os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
            rewritten = path.stat()
            self.assertEqual(rewritten.st_ino, original.st_ino)
            self.assertEqual(rewritten.st_size, original.st_size)
            self.assertEqual(rewritten.st_mtime_ns, original.st_mtime_ns)
            self.assertNotEqual(rewritten.st_ctime_ns, original.st_ctime_ns)

            self.assertEqual(pi_identity.read_claims(td)[0]["sessionId"], "sess-new")

    def test_corrupt_file_recovery_is_not_masked_by_a_failed_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            path = self._write(directory, "inst.json", _claim("inst", "sess-old", 11))
            self.assertEqual(pi_identity.read_claims(td)[0]["sessionId"], "sess-old")

            path.write_text('{"protocolVersion": 1,', encoding="utf-8")
            self.assertEqual(pi_identity.read_claims(td), [])

            path.write_text(
                json.dumps(_claim("inst", "sess-fixed", 11)), encoding="utf-8"
            )
            self.assertEqual(pi_identity.read_claims(td)[0]["sessionId"], "sess-fixed")

    def test_removal_and_reappearance_reparse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            path = self._write(directory, "inst.json", _claim("inst", "sess-old", 11))
            self.assertEqual(len(pi_identity.read_claims(td)), 1)

            path.unlink()
            self.assertEqual(pi_identity.read_claims(td), [])
            self.assertEqual(len(pi_identity._CLAIM_MEMO), 0)

            self._write(directory, "inst.json", _claim("inst", "sess-back", 11))
            with mock.patch.object(pi_identity.json, "loads", wraps=json.loads) as loads:
                claims = pi_identity.read_claims(td)
            self.assertEqual(claims[0]["sessionId"], "sess-back")
            self.assertGreaterEqual(loads.call_count, 1)

    def test_different_roots_never_share_entries(self) -> None:
        with (
            tempfile.TemporaryDirectory() as first_root,
            tempfile.TemporaryDirectory() as second_root,
        ):
            first_dir = pi_identity.claims_dir(first_root)
            second_dir = pi_identity.claims_dir(second_root)
            first_dir.mkdir(parents=True)
            second_dir.mkdir(parents=True)
            self._write(first_dir, "same.json", _claim("inst", "sess-first", 11))
            self._write(second_dir, "same.json", _claim("inst", "sess-second", 12))

            self.assertEqual(
                pi_identity.read_claims(first_root)[0]["sessionId"], "sess-first"
            )
            self.assertEqual(
                pi_identity.read_claims(second_root)[0]["sessionId"], "sess-second"
            )
            # Reading the second root must not replace the first root's entry.
            self.assertEqual(
                pi_identity.read_claims(first_root)[0]["sessionId"], "sess-first"
            )

    def test_returned_mutation_cannot_poison_later_reads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            claim = _claim("inst", "sess-original", 11)
            claim["nested"] = {"items": [1, 2]}
            self._write(directory, "inst.json", claim)
            first = pi_identity.read_claims(td)
            first[0]["sessionId"] = "mutated"
            first[0]["nested"]["items"].append(3)

            second = pi_identity.read_claims(td)
            self.assertEqual(second[0]["sessionId"], "sess-original")
            self.assertEqual(second[0]["nested"], {"items": [1, 2]})

    def test_size_bounded_lru_eviction(self) -> None:
        memo = pi_identity._ClaimMemo(max_bytes=1_700)
        key_a = ("/tmp/root", 1, 1, "a.json")
        key_b = ("/tmp/root", 1, 1, "b.json")
        key_c = ("/tmp/root", 1, 1, "c.json")
        stamp = (1, 2, 3, 4, 5)
        memo.put(key_a, stamp, {"id": "a"}, 800)
        memo.put(key_b, stamp, {"id": "b"}, 800)
        self.assertIsNotNone(memo.get(key_a, stamp))  # promote A past B
        memo.put(key_c, stamp, {"id": "c"}, 800)

        self.assertLessEqual(memo.total_bytes, 1_700)
        self.assertEqual(len(memo), 2)
        self.assertIsNotNone(memo.get(key_a, stamp))
        self.assertIsNone(memo.get(key_b, stamp))
        self.assertIsNotNone(memo.get(key_c, stamp))

    def test_concurrent_calls_return_consistent_claims(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            expected = set()
            for index in range(8):
                session_id = f"sess-{index}"
                expected.add(session_id)
                self._write(
                    directory,
                    f"inst-{index}.json",
                    _claim(f"inst-{index}", session_id, 100 + index),
                )
            pi_identity.read_claims(td)

            def read() -> set[str]:
                return {claim["sessionId"] for claim in pi_identity.read_claims(td)}

            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: read(), range(64)))
            self.assertTrue(all(result == expected for result in results))

    def test_expiry_is_reevaluated_while_source_stamp_is_unchanged(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            path = self._write(
                directory,
                "inst.json",
                _claim("inst", "sess-live", 11, updated=now, phase="working"),
            )
            stamp_before = path.stat()
            self.assertIn("sess-live", pi_identity.live_agent_phases(td, now=now))

            later = now + timedelta(seconds=pi_identity.CLAIM_TTL_SECONDS + 1)
            with mock.patch.object(pi_identity.json, "loads", wraps=json.loads) as loads:
                phases = pi_identity.live_agent_phases(td, now=later)

            self.assertNotIn("sess-live", phases)
            self.assertEqual(loads.call_count, 0)  # unchanged source reused
            stamp_after = path.stat()
            self.assertEqual(
                (
                    stamp_after.st_dev,
                    stamp_after.st_ino,
                    stamp_after.st_size,
                    stamp_after.st_mtime_ns,
                    stamp_after.st_ctime_ns,
                ),
                (
                    stamp_before.st_dev,
                    stamp_before.st_ino,
                    stamp_before.st_size,
                    stamp_before.st_mtime_ns,
                    stamp_before.st_ctime_ns,
                ),
            )

    def test_liveness_verdict_is_requested_on_every_observation(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            self._write(
                directory,
                "inst.json",
                _claim("inst", "sess", 11, updated=now, phase="working"),
            )
            pi_identity.read_claims(td)

            with mock.patch.object(
                pi_identity, "claim_is_live", side_effect=[True, False]
            ) as live:
                first = pi_identity.live_agent_phases(td, now=now)
                second = pi_identity.live_agent_phases(td, now=now)
            self.assertEqual(live.call_count, 2)
            self.assertIn("sess", first)
            self.assertNotIn("sess", second)

    def test_torn_read_is_returned_but_not_memoized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            self._write(directory, "inst.json", _claim("inst", "sess", 11))
            first = os.stat(directory / "inst.json")
            second = SimpleNamespace(
                st_dev=first.st_dev,
                st_ino=first.st_ino,
                st_size=first.st_size,
                st_mtime_ns=first.st_mtime_ns,
                st_ctime_ns=first.st_ctime_ns + 1,
            )
            with mock.patch.object(pi_identity.os, "fstat", side_effect=[first, second]):
                claims = pi_identity.read_claims(td)
            self.assertEqual(claims[0]["sessionId"], "sess")
            self.assertEqual(len(pi_identity._CLAIM_MEMO), 0)


if __name__ == "__main__":
    unittest.main()


class StaleClaimPruneTests(unittest.TestCase):
    """Design §7.1 rule 8: only old, non-live, dead/shutdown claims are removed."""

    def _write(self, directory: Path, name: str, claim: dict, *, age: float) -> Path:
        path = directory / name
        path.write_text(json.dumps(claim), encoding="utf-8")
        stamp = datetime.now(timezone.utc).timestamp() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_prunes_only_old_dead_or_shutdown_claims(self) -> None:
        day = 24 * 3600
        old = datetime.now(timezone.utc) - timedelta(days=3)
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            me, gone = os.getpid(), 9_999_999

            def write(name, *, pid, state="active", updated=old, age=3 * day):
                claim = _claim(name, f"s-{name}", pid=pid, updated=updated, state=state)
                return self._write(directory, f"{name}.json", claim, age=age)

            shutdown = write("a", pid=me, state="shutdown")
            dead = write("b", pid=gone)
            alive_pid = write("c", pid=me)
            recent = write("d", pid=gone, state="shutdown", age=3600)
            live = write("e", pid=me, updated=datetime.now(timezone.utc))
            removed = pi_identity.prune_stale_claims(td)
            self.assertEqual(removed, 2)
            self.assertFalse(shutdown.exists())
            self.assertFalse(dead.exists())
            for kept in (alive_pid, recent, live):
                self.assertTrue(kept.exists(), kept.name)

    def test_periodic_prune_is_off_under_test_isolation(self) -> None:
        with mock.patch.dict(os.environ, {"CORRAL_ISOLATE_MANAGED_HOSTS": "1"}), \
                mock.patch.object(pi_identity, "prune_stale_claims") as prune:
            self.assertEqual(pi_identity.prune_stale_claims_periodically(), 0)
        prune.assert_not_called()
