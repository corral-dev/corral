from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral.codex_identity import live_claims
from corral.runtime.sesskit_bridge import call_scan


class SessKitHostClaimsTests(unittest.TestCase):
    def test_codex_scan_receives_corral_claim_provider(self) -> None:
        seen: list[object] = []

        def scan_sessions(*, limit: int, host_claim_provider=None):
            seen.append(host_claim_provider)
            return host_claim_provider("/synthetic/codex/sessions")

        scan_sessions.__module__ = "sesskit.parsers.codex"

        with mock.patch("corral.codex_identity.live_claims", return_value={"thread": 919}) as provider:
            result = call_scan(scan_sessions, limit=17)

        provider.assert_called_once_with("/synthetic/codex/sessions")
        self.assertIs(seen[0], provider)
        self.assertEqual(result, {"thread": 919})

    def test_bridge_injects_provider_for_synthetic_claim_fixture(self) -> None:
        thread_id = "019efe42-6d51-7fb3-ad48-112a8eefaa01"
        stale_id = "019efe42-6d51-7fb3-ad48-112a8eefaa02"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claim_dir = root / "claims"
            claim_dir.mkdir()
            sessions_dir = root / "sessions"
            sessions_dir.mkdir()
            claims = {
                thread_id: os.getpid(),
                stale_id: 987654321,
            }
            for claimed_id in claims:
                rollout = sessions_dir / f"rollout-2026-07-16T10-00-00-{claimed_id}.jsonl"
                rollout.touch()
                (claim_dir / f"{claimed_id}.json").write_text(
                    json.dumps(
                        {
                            "thread_id": claimed_id,
                            "rollout_path": str(rollout),
                            "pid": claims[claimed_id],
                        }
                    ),
                    encoding="utf-8",
                )

            def scan_sessions(*, limit: int, host_claim_provider=None):
                self.assertEqual(limit, 10)
                return host_claim_provider(str(sessions_dir))

            scan_sessions.__module__ = "sesskit.parsers.codex"

            def process_probe(pid: int, _signal: int) -> None:
                if pid == 987654321:
                    raise ProcessLookupError(pid)

            with (
                mock.patch("corral.codex_identity.CLAIM_DIR", claim_dir),
                mock.patch("corral.codex_identity.os.kill", side_effect=process_probe),
            ):
                result = call_scan(scan_sessions, limit=10)

        self.assertEqual(result, {thread_id: os.getpid()})

    def test_non_codex_scanner_does_not_receive_corral_claim_provider(self) -> None:
        seen: list[object] = []

        def scan_sessions(*, limit: int, host_claim_provider=None):
            seen.append(host_claim_provider)
            return [limit]

        with mock.patch("corral.codex_identity.live_claims") as provider:
            result = call_scan(scan_sessions, limit=11)

        provider.assert_not_called()
        self.assertEqual(seen, [None])
        self.assertEqual(result, [11])

    def test_older_codex_scanner_without_provider_is_left_unchanged(self) -> None:
        def scan_sessions(*, limit: int):
            return [limit]

        scan_sessions.__module__ = "sesskit.parsers.codex"

        with mock.patch("corral.codex_identity.live_claims") as provider:
            result = call_scan(scan_sessions, limit=8)

        provider.assert_not_called()
        self.assertEqual(result, [8])

    def test_uninspectable_scanner_falls_back_to_limit_only(self) -> None:
        class UninspectableScanner:
            @property
            def __signature__(self):
                raise ValueError("synthetic signature failure")

            def __call__(self, **kwargs):
                return kwargs

        self.assertEqual(call_scan(UninspectableScanner(), limit=9), {"limit": 9})

    def test_corral_provider_discards_invalid_and_stale_claims(self) -> None:
        valid_id = "019efe42-6d51-7fb3-ad48-112a8eefaa01"
        stale_id = "019efe42-6d51-7fb3-ad48-112a8eefaa02"
        invalid_id = "not-a-session-id"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claim_dir = root / "claims"
            claim_dir.mkdir()
            sessions_dir = root / "sessions"
            sessions_dir.mkdir()
            valid_path = sessions_dir / f"rollout-2026-07-16T10-00-00-{valid_id}.jsonl"
            valid_path.touch()
            stale_path = sessions_dir / f"rollout-2026-07-16T10-00-00-{stale_id}.jsonl"
            stale_path.touch()

            claims = {
                "valid.json": {
                    "thread_id": valid_id,
                    "rollout_path": str(valid_path),
                    "pid": os.getpid(),
                },
                "stale.json": {
                    "thread_id": stale_id,
                    "rollout_path": str(stale_path),
                    "pid": 987654321,
                },
                "invalid.json": {
                    "thread_id": invalid_id,
                    "rollout_path": str(valid_path),
                    "pid": os.getpid(),
                },
                "outside.json": {
                    "thread_id": valid_id,
                    "rollout_path": str(root / "elsewhere" / f"{valid_id}.jsonl"),
                    "pid": os.getpid(),
                },
            }
            for name, payload in claims.items():
                (claim_dir / name).write_text(json.dumps(payload), encoding="utf-8")

            def process_probe(pid: int, _signal: int) -> None:
                if pid == 987654321:
                    raise ProcessLookupError(pid)

            with (
                mock.patch("corral.codex_identity.CLAIM_DIR", claim_dir),
                mock.patch("corral.codex_identity.os.kill", side_effect=process_probe),
            ):
                result = live_claims(str(sessions_dir))

        self.assertEqual(result, {valid_id: os.getpid()})


if __name__ == "__main__":
    unittest.main()
