from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral.codex_identity import live_claims
from corral.runtime.host_extension import corral_host_extension
from corral.runtime.sesskit_bridge import call_scan


class CallScanForwardingTests(unittest.TestCase):
    def test_host_forwarded_only_when_accepted(self) -> None:
        seen: dict = {}

        def new_scan(*, limit: int, host=None):
            seen["host"] = host
            return ["new"]

        def old_scan(*, limit: int):
            return ["old"]

        host = corral_host_extension()
        self.assertEqual(call_scan(new_scan, limit=7, host=host), ["new"])
        self.assertIs(seen["host"], host)
        self.assertEqual(call_scan(old_scan, limit=7, host=host), ["old"])

    def test_codex_style_legacy_provider_still_forwarded(self) -> None:
        seen: dict = {}

        def legacy_codex_scan(*, limit: int, host_claim_provider=None):
            seen["provider"] = host_claim_provider
            return host_claim_provider("/synthetic/codex/sessions")

        with mock.patch("corral.codex_identity.live_claims", return_value={"t": 1}) as provider:
            from corral.codex_identity import live_claims as claims_fn

            result = call_scan(
                legacy_codex_scan, limit=10, host_claim_provider=claims_fn
            )
        provider.assert_called_once_with("/synthetic/codex/sessions")
        self.assertEqual(result, {"t": 1})
        self.assertIs(seen["provider"], provider)

    def test_none_extras_are_skipped(self) -> None:
        def scan(*, limit: int, host=None):
            return host

        self.assertIsNone(call_scan(scan, limit=3, host=None))

    def test_unknown_kwargs_are_dropped(self) -> None:
        def scan(*, limit: int):
            return [limit]

        self.assertEqual(call_scan(scan, limit=5, host=object(), bogus=1), [5])

    def test_uninspectable_scanner_falls_back_to_limit_only(self) -> None:
        class UninspectableScanner:
            @property
            def __signature__(self):
                raise ValueError("synthetic signature failure")

            def __call__(self, **kwargs):
                return kwargs

        self.assertEqual(call_scan(UninspectableScanner(), limit=9), {"limit": 9})

    def test_extension_carries_corral_claim_providers(self) -> None:
        host = corral_host_extension()
        self.assertIsNotNone(host)
        self.assertTrue(callable(host.codex_claim_provider))
        self.assertTrue(callable(host.pi_claims_provider))
        self.assertTrue(host.title_prompt_marker)
        self.assertIn("oc-manager-", host.ephemeral_prefixes)


class CodexClaimProviderTests(unittest.TestCase):
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
