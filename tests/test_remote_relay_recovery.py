"""Real WebSocket outages must recover one lane without sacrificing control."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import websockets

from corral.remote import crypto, protocol
from corral.remote.config import RemoteState
from corral.remote.service import RemoteService
from corral.remote.transport import relay


class RelayRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.events = []
        patches = [
            mock.patch.dict(os.environ, CORRAL_CACHE_DIR=tmp.name, CORRAL_STATE_DIR=tmp.name),
            mock.patch.object(relay.observe, "event", lambda name, **fields: self.events.append((name, fields))),
            mock.patch.object(relay.remote_config, "clear_host_prev_key"),
            mock.patch.object(relay.RelayClient, "_headers", return_value={}),
            mock.patch.object(relay.RelayClient, "_attach_headers", return_value={}),
            mock.patch.object(relay, "_INITIAL_BACKOFF", 0.03),
            mock.patch.object(relay, "_MAX_BACKOFF", 0.06),
            mock.patch.object(relay, "_ACK_TIMEOUT", 0.15),
            mock.patch.object(relay, "_CLOSE_TIMEOUT", 0.12),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.primary = []
        self.bulk = []
        self.primary_frames = []
        self.reject_bulk = 0
        self.silent_primary = False
        self.silent_bulk = 0
        self.server = await websockets.serve(
            self.handle, "127.0.0.1", 0, subprotocols=[protocol.SUBPROTOCOL],
            process_request=self.process_request, ping_interval=None,
        )
        port = self.server.sockets[0].getsockname()[1]
        self.client = relay.RelayClient(
            RemoteService(), RemoteState(host_id="fixture", host_name="fixture", relay_url=f"ws://127.0.0.1:{port}"),
            crypto.generate_private_key_bytes(),
        )
        self.stop = asyncio.Event()
        self.task = None

    async def process_request(self, connection, request):
        if request.path.endswith("/attach") and self.reject_bulk:
            self.reject_bulk -= 1
            return connection.respond(503, "fixture unavailable")

    async def handle(self, socket):
        bulk = socket.request.path.endswith("/attach")
        (self.bulk if bulk else self.primary).append(socket)
        if (bulk and self.silent_bulk) or (not bulk and self.silent_primary):
            self.silent_bulk = max(0, self.silent_bulk - 1)
            await socket.wait_closed()
            return
        body = json.dumps({"generation": 7, "node": "fixture", "capabilities": ["host_lane_attach"]}).encode()
        await socket.send(protocol.encode_frame(
            protocol.FRAME_LANE_ATTACHED if bulk else protocol.FRAME_REGISTERED,
            protocol.ZERO_CHANNEL, b"" if bulk else body,
        ))
        try:
            async for raw in socket:
                if not bulk:
                    self.primary_frames.append(protocol.decode_frame(raw))
        except websockets.exceptions.ConnectionClosed:
            pass  # Deliberate heartbeat/connection failures belong to the fixture.

    async def asyncTearDown(self):
        for socket in self.primary + self.bulk:
            socket.transport.resume_reading()
        self.stop.set()
        if self.task is not None:
            await asyncio.wait_for(self.task, 3)
        self.server.close()
        await self.server.wait_closed()

    async def until(self, predicate, timeout=3):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(poll(), timeout)

    async def start(self):
        self.task = asyncio.create_task(self.client.run(self.stop))
        await self.until(lambda: self.client.bulk_lane_attached)

    async def test_bulk_loss_retries_without_new_primary_and_releases_only_lost_pins(self):
        await self.start()
        cid, control = b"\x01" * 16, b"\x02" * 16
        for key in (cid, control):
            self.assertIsNotNone(self.client._open_channel(key))
        self.client._lanes[cid] = self.client._bulk_socket
        self.client._lanes[control] = self.client._socket
        await self.bulk[0].close()
        await self.until(lambda: len(self.bulk) == 2 and self.client.bulk_lane_attached)
        self.assertEqual(len(self.primary), 1)
        self.assertTrue(self.client.is_connected)
        self.assertNotIn(cid, self.client._channels)
        self.assertIn(control, self.client._channels)
        await self.until(lambda: any(kind == protocol.FRAME_DEVICE_CLOSE and key == cid
                                     for kind, key, _ in self.primary_frames))

    async def test_bulk_open_failure_retries_and_records_opening_stage(self):
        self.reject_bulk = 1
        await self.start()
        failures = [fields for name, fields in self.events if name == "remote_relay_bulk_disconnected"]
        self.assertEqual(failures[0]["stage"], "opening")
        self.assertEqual(failures[0]["error_type"], "InvalidStatus")
        self.assertEqual(len(self.primary), 1)

    async def test_missing_bulk_ack_retries_without_claiming_attached(self):
        self.silent_bulk = 1
        self.task = asyncio.create_task(self.client.run(self.stop))
        await self.until(lambda: len(self.bulk) == 1)
        self.assertFalse(self.client.bulk_lane_attached)
        await self.until(lambda: len(self.bulk) == 2 and self.client.bulk_lane_attached)
        failures = [f for n, f in self.events if n == "remote_relay_bulk_disconnected"]
        self.assertEqual(failures[0]["stage"], "registering")
        self.assertEqual(failures[0]["error_type"], "TimeoutError")
        self.assertEqual(len(self.primary), 1)

    async def test_unregistered_socket_is_not_online_and_stop_cleans_receivers(self):
        self.silent_primary = True
        self.task = asyncio.create_task(self.client.run(self.stop))
        await self.until(lambda: len(self.primary) == 1)
        self.assertFalse(self.client.is_connected)
        await self.until(lambda: any(n == "remote_relay_retry_scheduled" for n, _ in self.events))
        self.assertFalse(self.client.is_connected)
        self.stop.set()
        await self.task
        await asyncio.sleep(0)
        self.assertFalse(any("Connection.recv" in repr(t.get_coro()) for t in asyncio.all_tasks()))

    async def test_primary_loss_reconnects_and_replaces_bulk(self):
        await self.start()
        await self.primary[0].close()
        await self.until(lambda: len(self.primary) == 2 and len(self.bulk) == 2 and self.client.bulk_lane_attached)
        self.assertTrue(self.client.is_connected)
        await self.until(lambda: self.bulk[0].close_code is not None)

    async def test_system_resume_rebuilds_stale_sockets_without_waiting_for_heartbeat(self):
        gap = [0.0]
        sampled = set()

        def clock():
            sampled.add(asyncio.current_task())
            return time.monotonic() + gap[0]

        with mock.patch.object(relay, "_suspend_time", clock), \
                mock.patch.object(relay, "_RESUME_CHECK_INTERVAL", 0.01):
            await self.start()
            await self.until(lambda: len(sampled) == 2)
            gap[0] = 30.0
            await self.until(lambda: len(self.primary) == 2 and len(self.bulk) >= 2 and self.client.bulk_lane_attached)
            failure = next(f for n, f in self.events
                           if n == "remote_relay_disconnected" and f.get("failure_kind") == "system_resume")
            self.assertAlmostEqual(failure["suspended_ms"], 30000, delta=50)
            self.assertTrue(self.client.is_connected)

    async def test_blackholed_bulk_has_bounded_close_wait_and_heartbeat_evidence(self):
        with mock.patch.object(relay, "_PING_INTERVAL", 0.05), mock.patch.object(relay, "_PING_TIMEOUT", 0.1):
            await self.start()
            self.bulk[0].transport.pause_reading()
            started = time.monotonic()
            await self.until(lambda: len(self.bulk) == 2 and self.client.bulk_lane_attached, timeout=2)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(len(self.primary), 1)
            failure = next(f for n, f in self.events if n == "remote_relay_bulk_disconnected")
            self.assertEqual(failure["stage"], "active")
            self.assertEqual(failure["failure_kind"], "heartbeat_timeout")
            self.assertEqual(failure["sent_code"], 1011)
            self.assertIn("last_frame_age_ms", failure)
            ready = [f for n, f in self.events if n == "remote_relay_ready" and f["lane"] == "bulk"]
            self.assertEqual([f["attempt"] for f in ready], [1, 2])
            self.assertTrue(all("open_ms" in f and "ready_ms" in f for f in ready))


if __name__ == "__main__":
    unittest.main()
