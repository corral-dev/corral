"""Bounded, ordered transport writers: real write paths against fake sinks.

Evidence provenance: every case here is a deterministic in-process fixture. It
drives the shipped `RelayClient._write`, `LocalServer._handle` and `HostChannel`
entry points (real encryption, real lane writer) against fake sockets, so it
proves admission, ordering, reset and cleanup semantics. It does not measure a
production socket or the live daemon's footprint.

Background: before the ordered lane writer, both transports scheduled one
`run_coroutine_threadsafe(send(...))` task per frame and ignored the future. A
stalled destination therefore retained an unbounded number of send tasks
(measured: 64/256/512 blocked 64 KiB sends retained 4.2/17.0/33.9 MiB and grew
linearly). Socket flow control cannot limit tasks that were already scheduled.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import tracemalloc
import unittest
from unittest import mock

from corral.remote import crypto, protocol
from corral.remote.config import RemoteState
from corral.remote.service import Connection, RemoteService
from corral.remote.transport import local as local_mod
from corral.remote.transport import relay as relay_mod
from corral.remote.transport import writer as writer_mod
from corral.remote.transport.channel import HostChannel

_FRAME_HEADER = 2 + protocol.CHANNEL_ID_LEN


def _counter(frame: bytes) -> int:
    return int.from_bytes(frame[_FRAME_HEADER : _FRAME_HEADER + 8], "big")


def _channel_of(frame: bytes) -> bytes:
    return frame[2 : 2 + protocol.CHANNEL_ID_LEN]


def _lane_tasks() -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if not task.done() and "LaneWriter._pump" in repr(task.get_coro())
    ]


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


class _Sink:
    """Fake physical socket: records frames, can stall, fail and count overlap."""

    def __init__(self, *, stall: bool = False, yield_each: bool = False) -> None:
        self.frames: list[bytes] = []
        self.started = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.fail_from: int | None = None
        self.closed = 0
        self._yield_each = yield_each
        self._release = asyncio.Event()
        if not stall:
            self._release.set()

    def stall(self) -> None:
        self._release.clear()

    def release(self) -> None:
        self._release.set()

    async def send(self, frame: bytes) -> None:
        self.started += 1
        index = self.started
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.fail_from is not None and index >= self.fail_from:
                raise ConnectionResetError("fixture sink failed")
            await self._release.wait()
            if self._yield_each:
                await asyncio.sleep(0)
            self.frames.append(frame)
        finally:
            self.in_flight -= 1

    async def close(self) -> None:
        self.closed += 1


class _FakeLocalSocket:
    """Minimal LAN websocket: feeds inbound frames and records outbound ones."""

    def __init__(self, sink: _Sink) -> None:
        self.remote_address = ("127.0.0.1", 43437)
        self._sink = sink
        self._inbound: asyncio.Queue = asyncio.Queue()
        self.close_calls = 0

    def feed(self, raw: bytes) -> None:
        self._inbound.put_nowait(raw)

    async def send(self, frame: bytes) -> None:
        await self._sink.send(frame)

    async def close(self) -> None:
        self.close_calls += 1
        self._inbound.put_nowait(None)

    async def __aiter__(self):
        while True:
            raw = await self._inbound.get()
            if raw is None:
                return
            yield raw


class _IsolatedTransportTest(unittest.IsolatedAsyncioTestCase):
    """Isolated product state plus captured observations, no shared directories."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ,
            {"CORRAL_CACHE_DIR": self._tmp.name, "CORRAL_STATE_DIR": self._tmp.name},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events: list[tuple[str, dict]] = []
        event_patcher = mock.patch.object(
            writer_mod.observe,
            "event",
            lambda name, **fields: self.events.append((name, fields)),
        )
        event_patcher.start()
        self.addCleanup(event_patcher.stop)

    def _relay(self) -> relay_mod.RelayClient:
        client = relay_mod.RelayClient(
            RemoteService(),
            RemoteState(host_id=f"memory-{crypto.random_id(4)}", host_name="fixture"),
            crypto.generate_private_key_bytes(),
        )
        client._loop = asyncio.get_running_loop()
        return client

    def _channel(self, client, channel_id: bytes, *, data_plane: bool = False) -> HostChannel:
        channel = client._open_channel(channel_id)
        assert channel is not None
        channel._secure = crypto.SecureChannel(b"\x01" * 32, b"\x02" * 32, b"\x03" * 32)
        channel._confirmed = True
        if data_plane:
            channel._owns_logical_connection = False
        return channel

    def _events(self, name: str) -> list[dict]:
        return [fields for event, fields in self.events if event == name]

    async def _tick(self, times: int = 5) -> None:
        for _ in range(times):
            await asyncio.sleep(0)

    async def _drain_lane_tasks(self) -> None:
        assert await _wait_for(lambda: not _lane_tasks(), timeout=5.0), _lane_tasks()


class RelayLaneWriterTests(_IsolatedTransportTest):
    async def test_stalled_relay_sink_stays_within_one_lane_budget(self):
        """512 stalled 64 KiB frames used to retain 33.9 MiB; the lane caps them."""
        client = self._relay()
        sink = _Sink(stall=True)
        client._socket = sink
        payload = b"x" * 65536
        tracing = tracemalloc.is_tracing()
        if not tracing:
            tracemalloc.start()
        try:
            for index in range(512):
                # ZERO_CHANNEL frames belong to no device channel (push traffic),
                # so a refusal cannot desynchronise anybody and the lane budget is
                # the only ceiling in play.
                client._write(protocol.FRAME_PUSH, protocol.ZERO_CHANNEL, payload)
                if index % 32 == 31:
                    await self._tick(2)  # keep the loop responsive while the sink stalls
            await self._tick()
            lane = client._writer_for(sink)
            current, _peak = tracemalloc.get_traced_memory()
        finally:
            if not tracing:
                tracemalloc.stop()

        self.assertEqual(sink.max_in_flight, 1, "one socket send at a time")
        self.assertEqual(sink.started, 1, "a stalled sink only ever has one send in flight")
        self.assertLessEqual(lane.pending_bytes, writer_mod.LANE_MAX_BYTES)
        self.assertLessEqual(lane.pending_frames, writer_mod.LANE_MAX_FRAMES)
        self.assertGreater(lane.pending_frames, 0)
        self.assertGreater(lane.refused_frames, 0)
        self.assertLess(current, writer_mod.LANE_MAX_BYTES + 8 * 1024 * 1024)
        self.assertLessEqual(len(_lane_tasks()), 1, "one drain coroutine per lane")
        refused = self._events("remote_writer_refused")
        self.assertTrue(refused)
        self.assertEqual(refused[0]["reason"], "lane_bytes")
        self.assertEqual(refused[0]["channel"], "")

        admitted = lane.pending_frames
        sink.release()
        await self._drain_lane_tasks()
        self.assertEqual(len(sink.frames), admitted)
        self.assertEqual({len(frame) for frame in sink.frames}, {_FRAME_HEADER + len(payload)})
        self.assertEqual(lane.pending_bytes, 0)
        self.assertEqual(lane.pending_frames, 0)
        self.assertEqual(client._budget.pending_bytes, 0)
        self.assertEqual(client._budget.pending_frames, 0)

    async def test_channel_overflow_resets_only_the_flooding_channel(self):
        client = self._relay()
        sink = _Sink(stall=True)
        client._socket = sink
        flooding, healthy = b"\xa1" * 16, b"\xb2" * 16
        flooding_channel = self._channel(client, flooding)
        healthy_channel = self._channel(client, healthy)

        for index in range(160):  # 10 MiB from one device: exceeds its own share
            flooding_channel._send_message({"t": "evt", "n": 0, "pad": "x" * 65500})
            if index % 16 == 15:
                await self._tick(2)
        for index in range(20):  # another device on the same socket keeps working
            healthy_channel._send_message({"t": "evt", "n": 0, "pad": "x" * 65500})
            if index % 8 == 7:
                await self._tick(2)
        await self._tick()
        lane = client._writer_for(sink)

        self.assertTrue(flooding_channel._closed)
        self.assertNotIn(flooding, client._channels)
        self.assertFalse(healthy_channel._closed)
        self.assertIn(healthy, client._channels)
        self.assertLessEqual(lane.pending_channel_bytes(flooding), writer_mod.CHANNEL_MAX_BYTES)
        self.assertGreater(lane.pending_channel_bytes(healthy), 20 * 65500)
        resets = self._events("remote_writer_channel_reset")
        self.assertTrue(resets)
        self.assertEqual(resets[0]["channel"], flooding.hex()[:8])
        self.assertGreaterEqual(resets[0]["cancelled"], 1)
        self.assertEqual(sink.max_in_flight, 1)

        sink.release()
        await self._drain_lane_tasks()
        # Every healthy frame survives the other device's reset, in order...
        healthy_frames = [
            frame
            for frame in sink.frames
            if _channel_of(frame) == healthy and frame[1] == protocol.FRAME_DATA
        ]
        self.assertEqual(len(healthy_frames), 20)
        self.assertEqual([_counter(frame) for frame in healthy_frames], list(range(20)))
        # ...and the flooding channel's delivered prefix is still monotonic:
        # ciphertext is never dropped from the middle of its counter sequence.
        flooding_counters = [
            _counter(frame)
            for frame in sink.frames
            if _channel_of(frame) == flooding and frame[1] == protocol.FRAME_DATA
        ]
        self.assertEqual(flooding_counters, sorted(flooding_counters))
        self.assertEqual(len(set(flooding_counters)), len(flooding_counters))
        self.assertEqual(lane.pending_bytes, 0)
        self.assertEqual(client._budget.pending_bytes, 0)

    async def test_one_legal_maximum_frame_is_admitted(self):
        client = self._relay()
        sink = _Sink(stall=True)
        client._socket = sink
        first, second = b"\x33" * 16, b"\x34" * 16
        self._channel(client, first)
        self._channel(client, second)
        maximum = b"m" * (writer_mod.MAX_FRAME_BYTES - _FRAME_HEADER)

        client._write(protocol.FRAME_DATA, first, maximum)
        self.assertTrue(await _wait_for(lambda: sink.started == 1))
        lane = client._writer_for(sink)
        self.assertEqual(lane.pending_frames, 1)
        self.assertEqual(lane.pending_bytes, writer_mod.MAX_FRAME_BYTES)

        # A second maximum frame cannot be admitted: the lane budget is one
        # legal maximum frame plus headroom, so that channel is reset instead of
        # having ciphertext silently dropped from the middle of its sequence.
        client._write(protocol.FRAME_DATA, second, maximum)
        await self._tick()
        self.assertEqual(lane.refused_frames, 1)
        self.assertEqual(self._events("remote_writer_refused")[-1]["reason"], "lane_bytes")
        self.assertNotIn(second, client._channels)
        self.assertIn(first, client._channels)

        sink.release()
        await self._drain_lane_tasks()
        self.assertEqual(protocol.decode_frame(sink.frames[0])[0], protocol.FRAME_DATA)
        self.assertEqual(len(sink.frames[0]), writer_mod.MAX_FRAME_BYTES)
        self.assertIn(
            protocol.FRAME_DEVICE_CLOSE,
            [protocol.decode_frame(frame)[0] for frame in sink.frames],
        )
        self.assertEqual(lane.pending_bytes, 0)

    async def test_send_failure_resets_lane_and_completes_pending_once(self):
        client = self._relay()
        sink = _Sink(stall=False)
        sink.fail_from = 2  # first send succeeds, the second one breaks the socket
        client._socket = sink
        channel_id = b"\x44" * 16
        channel = self._channel(client, channel_id)
        lane = client._writer_for(sink)
        for _ in range(5):  # one succeeds, one fails, three stay queued
            client._write(protocol.FRAME_DATA, channel_id, b"f" * 1024)
        await self._tick()
        self.assertTrue(await _wait_for(lambda: lane.closed))
        await self._drain_lane_tasks()

        self.assertEqual(lane.reason, "send_failed")
        self.assertEqual(lane.pending_bytes, 0)
        self.assertEqual(lane.pending_frames, 0)
        self.assertEqual(lane.cancelled_outcomes, 3)
        self.assertEqual(client._budget.pending_bytes, 0)
        self.assertNotIn(channel_id, client._channels)
        self.assertTrue(channel._closed)
        self.assertEqual(len(self._events("remote_writer_send_failed")), 1)
        self.assertEqual(len(self._events("remote_writer_reset")), 1)
        self.assertEqual(self._events("remote_writer_reset")[0]["cancelled"], 3)
        self.assertEqual(sink.closed, 1, "the failed lane closes its own socket")
        self.assertEqual(_lane_tasks(), [])

    async def test_close_with_queued_work_cancels_once_and_reconnects(self):
        client = self._relay()
        old, new = _Sink(stall=True), _Sink(stall=False)
        client._socket = old
        for _ in range(5):
            client._write(protocol.FRAME_PUSH, protocol.ZERO_CHANNEL, b"q" * 4096)
        await self._tick()
        old_lane = client._writer_for(old)
        self.assertEqual(old_lane.pending_frames, 5)

        client._close_writer(old)  # reconnect teardown
        self.assertTrue(old_lane.closed)
        self.assertEqual(old_lane.cancelled_outcomes, 4)
        self.assertEqual(old_lane.pending_frames, 1, "the in-flight frame is never cancelled")

        client._socket = new
        client._write(protocol.FRAME_PUSH, protocol.ZERO_CHANNEL, b"r" * 4096)
        self.assertTrue(await _wait_for(lambda: len(new.frames) == 1))
        self.assertNotIn(old, client._writers)
        self.assertIn(new, client._writers)

        old.release()
        await self._drain_lane_tasks()
        self.assertEqual(old_lane.pending_bytes, 0)
        self.assertEqual(old_lane.pending_frames, 0)
        self.assertEqual(len(old.frames), 1)
        self.assertEqual(client._budget.pending_bytes, 0)

    async def test_single_writer_preserves_counter_order_across_threads(self):
        client = self._relay()
        sink = _Sink(stall=False, yield_each=True)
        client._socket = sink
        channel_id = b"\x55" * 16
        channel = self._channel(client, channel_id)
        messages = 150
        threads = 8
        client._budget.max_frames = 4096
        with (
            mock.patch.object(writer_mod, "LANE_MAX_FRAMES", 4096),
            mock.patch.object(writer_mod, "CHANNEL_MAX_FRAMES", 4096),
        ):
            def blast() -> None:
                for _index in range(messages):
                    channel._send_message({"t": "evt", "n": 0})

            await asyncio.gather(*(asyncio.to_thread(blast) for _ in range(threads)))
            expected = threads * messages
            self.assertTrue(await _wait_for(lambda: len(sink.frames) == expected))
        self.assertEqual(sink.max_in_flight, 1)
        self.assertEqual([_counter(frame) for frame in sink.frames], list(range(expected)))
        self.assertEqual(client._budget.pending_bytes, 0)

    async def test_bulk_lane_failure_closes_only_its_pinned_channels(self):
        client = self._relay()
        primary, bulk = _Sink(stall=False), _Sink(stall=False)
        client._socket = primary
        client._bulk_socket = bulk
        client._bulk_attached.set()
        data_id, control_id = b"\x66" * 16, b"\x67" * 16
        data_channel = self._channel(client, data_id, data_plane=True)
        control_channel = self._channel(client, control_id)
        client._write(protocol.FRAME_DATA, data_id, b"d")
        client._write(protocol.FRAME_DATA, control_id, b"c")
        await self._tick()
        self.assertEqual(client._lanes[data_id], bulk)
        self.assertEqual(client._lanes[control_id], primary)

        bulk.fail_from = bulk.started + 1
        client._write(protocol.FRAME_DATA, data_id, b"e")
        self.assertTrue(await _wait_for(lambda: data_channel._closed))
        await self._drain_lane_tasks()
        self.assertFalse(control_channel._closed)
        self.assertIn(control_id, client._channels)
        self.assertTrue(await _wait_for(lambda: client._bulk_socket is None))
        self.assertGreaterEqual(bulk.closed, 1)
        self.assertNotIn(bulk, client._writers)
        # The control plane kept working through the bulk outage, and the relay
        # was told to detach only the failed device's data plane.
        kinds = [protocol.decode_frame(frame) for frame in primary.frames]
        self.assertEqual(kinds[0][0], protocol.FRAME_DATA)
        self.assertEqual(kinds[0][1], control_id)
        self.assertIn((protocol.FRAME_DEVICE_CLOSE, data_id), [(kind, cid) for kind, cid, _ in kinds])
        self.assertEqual(len(self._events("remote_writer_reset")), 1)
        self.assertEqual(_lane_tasks(), [])

    async def test_aggregate_budget_is_shared_by_lanes(self):
        client = self._relay()
        first, second = _Sink(stall=True), _Sink(stall=True)
        client._budget.max_bytes = 200 * 1024
        client._budget.max_frames = 10
        payload = b"z" * 32 * 1024
        client._socket = first
        for _ in range(5):
            client._write(protocol.FRAME_PUSH, protocol.ZERO_CHANNEL, payload)
        client._socket = second
        for _ in range(5):
            client._write(protocol.FRAME_PUSH, protocol.ZERO_CHANNEL, payload)
        await self._tick()

        first_lane = client._writer_for(first)
        second_lane = client._writer_for(second)
        self.assertEqual(first_lane.pending_frames, 5)
        self.assertLess(second_lane.pending_frames, 5, "aggregate ceiling stops the second lane")
        self.assertIn("aggregate_budget", [fields["reason"] for fields in self._events("remote_writer_refused")])
        self.assertLessEqual(client._budget.pending_bytes, client._budget.max_bytes)
        first.release()
        second.release()
        await self._drain_lane_tasks()
        self.assertEqual(client._budget.pending_bytes, 0)


class LocalTransportWriteTests(_IsolatedTransportTest):
    async def _start_local(self, *, stall: bool):
        service = RemoteService()
        service.begin_pairing()
        host_private = crypto.generate_private_key_bytes()
        state = RemoteState(host_id=f"local-{crypto.random_id(4)}", host_name="fixture")
        server = local_mod.LocalServer(service, state, host_private)
        sink = _Sink(stall=stall)
        sock = _FakeLocalSocket(sink)
        task = asyncio.create_task(server._handle(sock))
        return server, sink, sock, task, host_private

    async def _complete_handshake(self, sock: _FakeLocalSocket, sink: _Sink, host_private: bytes):
        client_handshake = crypto.Handshake(crypto.generate_private_key_bytes())
        sock.feed(
            protocol.encode_frame(
                protocol.FRAME_HELLO,
                protocol.ZERO_CHANNEL,
                client_handshake.static_public + client_handshake.ephemeral_public,
            )
        )
        self.assertTrue(await _wait_for(lambda: len(sink.frames) >= 2))
        channel_id = protocol.decode_frame(sink.frames[0])[1]
        client_secure = client_handshake.complete(
            crypto.public_key_bytes(host_private), protocol.decode_frame(sink.frames[1])[2]
        )
        sock.feed(
            protocol.encode_frame(
                protocol.FRAME_DATA,
                channel_id,
                client_secure.encrypt(
                    protocol.dumps(protocol.request(1, protocol.M_HELLO, {"ci": "fixture"}))
                ),
            )
        )
        return channel_id, client_secure

    async def test_local_frames_are_ordered_through_one_stalled_lane(self):
        server, sink, sock, task, host_private = await self._start_local(stall=True)
        try:
            self.assertTrue(await _wait_for(lambda: sink.started == 1))
            self.assertEqual(sink.max_in_flight, 1)
            self.assertEqual(sink.frames, [], "the stalled sink has not accepted a frame")

            client_handshake = crypto.Handshake(crypto.generate_private_key_bytes())
            sock.feed(
                protocol.encode_frame(
                    protocol.FRAME_HELLO,
                    protocol.ZERO_CHANNEL,
                    client_handshake.static_public + client_handshake.ephemeral_public,
                )
            )
            # The HELLO reply queues behind the frame the sink is holding and
            # must not start a second overlapping socket send.
            self.assertTrue(await _wait_for(lambda: sink.started == 1 and server._channels == 1))
            self.assertEqual(sink.max_in_flight, 1)
            self.assertLessEqual(len(_lane_tasks()), 2)

            sink.release()
            self.assertTrue(await _wait_for(lambda: len(sink.frames) >= 2))
            kinds = [protocol.decode_frame(frame)[0] for frame in sink.frames[:2]]
            self.assertEqual(kinds, [protocol.FRAME_DEVICE_OPEN, protocol.FRAME_HELLO])

            channel_id = protocol.decode_frame(sink.frames[0])[1]
            client_secure = client_handshake.complete(
                crypto.public_key_bytes(host_private), protocol.decode_frame(sink.frames[1])[2]
            )
            sock.feed(
                protocol.encode_frame(
                    protocol.FRAME_DATA,
                    channel_id,
                    client_secure.encrypt(
                        protocol.dumps(protocol.request(1, protocol.M_HELLO, {"ci": "fixture"}))
                    ),
                )
            )
            self.assertTrue(await _wait_for(lambda: len(sink.frames) >= 3))
            self.assertEqual(protocol.decode_frame(sink.frames[2])[0], protocol.FRAME_DATA)
            self.assertEqual(server._channels, 1)
        finally:
            await sock.close()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.assertTrue(await _wait_for(lambda: server._channels == 0))

    async def test_local_overflow_closes_only_that_connection(self):
        with (
            mock.patch.object(writer_mod, "CHANNEL_MAX_BYTES", 7000),
            mock.patch.object(writer_mod, "LANE_MAX_BYTES", 9000),
        ):
            server, sink, sock, task, host_private = await self._start_local(stall=True)
            try:
                sink.release()
                _channel_id, _client_secure = await self._complete_handshake(sock, sink, host_private)
                self.assertTrue(await _wait_for(lambda: len(server.service._connections) == 1))
                connection = next(iter(server.service._connections))

                sink.stall()
                for index in range(3):
                    connection.send({"t": "evt", "c": "term:x", "d": {"i": index, "pad": "p" * 3000}})
                self.assertTrue(await _wait_for(lambda: bool(self._events("remote_writer_channel_reset"))))
                self.assertEqual(self._events("remote_writer_channel_reset")[0]["reason"], "channel_bytes")
                self.assertTrue(await _wait_for(lambda: sock.close_calls >= 1))
                self.assertTrue(await _wait_for(lambda: not server.service._connections))
                self.assertTrue(await _wait_for(lambda: server._channels == 0))
            finally:
                sink.release()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


class HostChannelAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ,
            {"CORRAL_CACHE_DIR": self._tmp.name, "CORRAL_STATE_DIR": self._tmp.name},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events: list[tuple[str, dict]] = []
        event_patcher = mock.patch.object(
            writer_mod.observe,
            "event",
            lambda name, **fields: self.events.append((name, fields)),
        )
        event_patcher.start()
        self.addCleanup(event_patcher.stop)
        self.service = RemoteService()
        self.transport_closed: list[str] = []

    def _channel(self, *, queue_size: int = 1, queue_bytes: int = 8 * 1024 * 1024) -> HostChannel:
        return HostChannel(
            self.service,
            crypto.generate_private_key_bytes(),
            b"\x77" * 16,
            lambda _frame_type, _payload: None,
            queue_size=queue_size,
            queue_bytes=queue_bytes,
            close_transport=lambda: self.transport_closed.append("plane"),
        )

    def _events(self, name: str) -> list[dict]:
        return [fields for event, fields in self.events if event == name]

    def test_data_plane_overflow_closes_only_that_plane(self):
        control = Connection("aa" * 32, lambda _message: None)
        self.service.attach(control)
        control.close_hook = lambda: self.transport_closed.append("control")
        channel = self._channel()
        channel._ensure_worker = lambda: None
        channel._owns_logical_connection = False
        channel._confirmed = True
        channel._connection = control
        control.data_send = channel._send_message
        control.data_channel = channel
        channel._queue.put_nowait((protocol.FRAME_DATA, b"fill"))

        channel.submit(protocol.FRAME_DATA, b"overflow")

        self.assertTrue(channel._closed)
        self.assertFalse(control.closed)
        self.assertIn(control, self.service._connections)
        self.assertIsNone(control.data_send)
        self.assertEqual(self.transport_closed, ["plane"])
        self.assertEqual(len(self._events("remote_channel_overflow")), 1)
        self.assertEqual(self._events("remote_channel_overflow")[0]["plane"], "data")
        self.assertEqual(self._events("remote_channel_overflow")[0]["reason"], "frames")
        control.close_hook = None
        self.service.detach(control)

    def test_control_plane_overflow_closes_itself(self):
        control = Connection("bb" * 32, lambda _message: None)
        self.service.attach(control)
        channel = self._channel()
        channel._ensure_worker = lambda: None
        channel._owns_logical_connection = True
        channel._confirmed = True
        channel._connection = control
        control.close_hook = channel.close
        channel._queue.put_nowait((protocol.FRAME_DATA, b"fill"))

        channel.submit(protocol.FRAME_DATA, b"overflow")

        self.assertTrue(channel._closed)
        self.assertTrue(control.closed)
        self.assertNotIn(control, self.service._connections)
        self.assertEqual(self._events("remote_channel_overflow")[0]["plane"], "control")

    def test_admission_counts_in_flight_bytes(self):
        channel = self._channel(queue_size=8, queue_bytes=1500)
        started = threading.Event()
        release = threading.Event()

        def blocked(_frame_type, _payload):
            started.set()
            release.wait(5.0)

        channel._on_frame = blocked  # type: ignore[method-assign]
        try:
            channel.submit(protocol.FRAME_DATA, b"a" * 1000)
            self.assertTrue(started.wait(2.0), "the worker must be inside the frame")
            self.assertEqual(channel.queued_bytes, 1000, "in-flight bytes stay counted")
            channel.submit(protocol.FRAME_DATA, b"b" * 400)
            self.assertEqual(channel.queued_bytes, 1400)
            channel.submit(protocol.FRAME_DATA, b"c" * 400)  # 1800 > 1500
            self.assertTrue(channel._closed)
            self.assertIn("bytes", [event["reason"] for event in self._events("remote_channel_overflow")])
        finally:
            release.set()
            channel.close()

    def test_closing_a_full_queue_stops_the_worker(self):
        channel = self._channel(queue_size=2, queue_bytes=1 << 20)
        started = threading.Event()
        release = threading.Event()

        def blocked(_frame_type, _payload):
            started.set()
            release.wait(5.0)

        channel._on_frame = blocked  # type: ignore[method-assign]
        try:
            channel.submit(protocol.FRAME_DATA, b"a" * 100)
            self.assertTrue(started.wait(2.0))
            channel.submit(protocol.FRAME_DATA, b"b" * 100)
            channel.submit(protocol.FRAME_DATA, b"c" * 100)
            self.assertEqual(channel._queue.qsize(), 2)
            channel.close()
            self.assertEqual(channel._queue.qsize(), 2, "the stop marker needs room")
            release.set()
            channel._worker.join(3.0)
            self.assertFalse(channel._worker.is_alive(), "a closed channel must stop its worker")
            self.assertTrue(channel._queue.empty())
        finally:
            release.set()
            channel.close()

    def test_byte_budget_admits_one_legal_maximum_frame(self):
        channel = self._channel(queue_size=8, queue_bytes=writer_mod.MAX_FRAME_BYTES)
        channel._ensure_worker = lambda: None
        channel.submit(protocol.FRAME_DATA, b"x" * writer_mod.MAX_FRAME_BYTES)
        self.assertFalse(channel._closed)
        self.assertEqual(channel.queued_bytes, writer_mod.MAX_FRAME_BYTES)
        channel.submit(protocol.FRAME_DATA, b"y")
        self.assertTrue(channel._closed)
        self.assertEqual(self._events("remote_channel_overflow")[0]["reason"], "bytes")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
