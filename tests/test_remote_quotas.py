"""Tests for Corral's strictly read-only quota RPCs and QuotaHub."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import unittest
from unittest import mock

from corral.remote import protocol
from corral.remote.quotas import QuotaHub, fetch_subswap_quotas
from corral.remote.service import _HANDLERS, _READONLY_METHODS, Connection, RemoteService


class _MinimalHub:
    def __init__(self) -> None:
        self._on_event = None

    def runtimes(self) -> list[dict]:
        return []

    def watch_sessions(self) -> None:
        pass

    def unwatch_sessions(self) -> None:
        pass

    def unwatch_layout(self) -> None:
        pass

    def unwatch_screen(self, _key: str) -> None:
        pass

    def terminal_detach(self, _key: str, _viewer: str) -> None:
        pass

    def unwatch_conversation(self, _key: str) -> None:
        pass


def _sample_snapshot(used: int = 20) -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-11-26T09:10:00Z",
        "daemon_running": True,
        "stale_after_ms": 300000,
        "auto_swap": {"enabled": True, "threshold": 0.99},
        "warn_pct": 90.0,
        "exhausted_pct": 100.0,
        "provider_order": ["claude"],
        "accounts": [
            {
                "id": "acc-1",
                "provider": "claude",
                "provider_display_name": "Claude",
                "display_label": "user@example.com",
                "active": True,
                "billing": "subscription",
                "manual_only": False,
                "fetch_state": "ready",
                "cached_at": "2026-11-26T09:09:50Z",
                "quotas": [
                    {
                        "provider": "claude",
                        "account_id": "acc-1",
                        "window": "five_hour",
                        "used": used,
                        "limit": 100,
                        "reset_at": "2026-11-26T14:00:00Z",
                        "status": "ok",
                    }
                ],
            }
        ],
    }


def _send_and_collect(service: RemoteService, conn: Connection, method: str, params: dict | None = None) -> dict:
    replies: list[dict] = []
    msg = {"t": "req", "id": 1, "m": method, "p": params or {}}
    service.handle(conn, msg, reply=replies.append)
    assert replies
    return replies[-1]


class QuotaFetchTests(unittest.TestCase):
    def test_missing_subswap_binary_returns_not_installed(self) -> None:
        with mock.patch("corral.remote.quotas.find_subswap_binary", return_value=None):
            res = fetch_subswap_quotas(refresh=True)
        self.assertFalse(res["available"])
        self.assertEqual(res["reason"], "subswap_not_installed")

    def test_old_cli_without_refresh_falls_back_to_plain_status(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, **_kwargs):
            calls.append(list(args))
            if "--refresh" in args:
                return subprocess.CompletedProcess(
                    args, 2, stdout="", stderr="error: unexpected argument '--refresh' found"
                )
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps(_sample_snapshot(35)), stderr=""
            )

        with (
            mock.patch("corral.remote.quotas.find_subswap_binary", return_value="/usr/local/bin/subswap"),
            mock.patch("corral.remote.quotas.subprocess.run", side_effect=fake_run),
        ):
            res = fetch_subswap_quotas(refresh=True)

        self.assertTrue(res["available"])
        self.assertEqual(res["snapshot"]["accounts"][0]["quotas"][0]["used"], 35)
        self.assertEqual(len(calls), 2)
        self.assertIn("--refresh", calls[0])
        self.assertNotIn("--refresh", calls[1])


class QuotaHubAndServiceTests(unittest.TestCase):
    def test_readonly_capability_and_no_write_rpc(self) -> None:
        events: list[tuple[str, dict]] = []
        hub = QuotaHub(
            poll_interval=60.0,
            fetcher=lambda _refresh: {
                "available": True,
                "snapshot": _sample_snapshot(22),
                "fetched_at": time.time(),
            },
        )
        service = RemoteService(_MinimalHub(), quotas=hub)  # type: ignore[arg-type]
        try:
            conn = Connection("pk-readonly", lambda msg: events.append(("reply", msg)))
            conn.paired = True
            conn.access = "readonly"

            with (
                mock.patch.object(service, "is_known_device", return_value=True),
                mock.patch.object(service, "device_access", return_value="readonly"),
            ):
                hello = _send_and_collect(service, conn, protocol.M_HELLO, {"name": "iPhone"})
                self.assertTrue(hello["ok"], hello)
                self.assertTrue(hello["d"]["capabilities"][protocol.CAPABILITY_QUOTAS])

                for m in (protocol.M_QUOTAS_SNAPSHOT, protocol.M_QUOTAS_WATCH, protocol.M_QUOTAS_UNWATCH):
                    self.assertIn(m, _READONLY_METHODS)
                snap = _send_and_collect(service, conn, protocol.M_QUOTAS_SNAPSHOT, {"refresh": False})
                self.assertTrue(snap["ok"], snap)
                self.assertEqual(snap["d"]["snapshot"]["accounts"][0]["quotas"][0]["used"], 22)

                watched = _send_and_collect(service, conn, protocol.M_QUOTAS_WATCH, {})
                self.assertTrue(watched["ok"], watched)
                self.assertTrue(watched["d"]["available"])
                unwatched = _send_and_collect(service, conn, protocol.M_QUOTAS_UNWATCH, {})
                self.assertTrue(unwatched["ok"], unwatched)

            for key in _HANDLERS:
                self.assertNotIn("swap", key.lower())
        finally:
            service.stop()

    def test_snapshot_coalescing_and_watch_push_lifecycle(self) -> None:
        events: list[tuple[str, dict]] = []
        values = iter([20, 20, 45])
        gate = threading.Event()
        calls = 0

        def fetcher(refresh: bool) -> dict:
            nonlocal calls
            calls += 1
            if calls == 1 and refresh:
                gate.wait(timeout=1.0)
            used = next(values, 45)
            return {"available": True, "snapshot": _sample_snapshot(used), "fetched_at": time.time()}

        hub = QuotaHub(
            on_event=lambda ch, data: events.append((ch, data)),
            poll_interval=0.06,
            fetcher=fetcher,
        )
        try:
            # Concurrent snapshot requests coalesce onto a single fetcher run.
            results: list[dict] = []
            t1 = threading.Thread(target=lambda: results.append(hub.snapshot(refresh=True)))
            t2 = threading.Thread(target=lambda: results.append(hub.snapshot(refresh=False)))
            t1.start()
            time.sleep(0.02)
            t2.start()
            gate.set()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)
            self.assertEqual(calls, 1)
            self.assertEqual(len(results), 2)

            # Watch returns cached, skips unchanged poll, then emits when snapshot changes.
            initial = hub.watch()
            self.assertTrue(initial["available"])
            deadline = time.monotonic() + 1.5
            while not events and time.monotonic() < deadline:
                time.sleep(0.03)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][0], protocol.CH_QUOTAS)
            self.assertEqual(events[0][1]["snapshot"]["accounts"][0]["quotas"][0]["used"], 45)

            hub.unwatch()
            count_after_unwatch = calls
            time.sleep(0.18)
            # Polling stops cleanly once watchers drop to zero.
            self.assertEqual(calls, count_after_unwatch)
        finally:
            hub.stop()
