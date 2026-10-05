"""Same-machine / LAN connections must not drop (owner requirement 2026-10-05).

The host log showed 99 reconnects in 30 minutes from one Mac. Causes covered
here: host frames handed to the socket out of counter order, relay data
channels hopping lanes, the per-request client-instance id, and the LAN
channel-open limiter refusing reconnects. See `docs/REMOTE_KNOWLEDGE_BASE.md`
"Same-machine and LAN connections never drop".
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

from corral.remote import crypto, protocol, ratelimit
from corral.remote.config import RemoteState
from corral.remote.service import RemoteService
from corral.remote.transport import channel as channel_mod
from corral.remote.transport import local as local_mod
from corral.remote.transport import relay as relay_mod


def _counter(frame: bytes) -> int:
    return int.from_bytes(frame[:8], "big")


class _TempStateMixin:
    def _use_temp_state(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"CORRAL_CACHE_DIR": tmp.name, "CORRAL_STATE_DIR": tmp.name}
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class HostFrameOrderTests(_TempStateMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._use_temp_state()

    def test_concurrent_sends_reach_the_writer_in_counter_order(self):
        """A reply and pushed events race on different threads in production."""
        written: list[bytes] = []
        channel = channel_mod.HostChannel(
            RemoteService(),
            crypto.generate_private_key_bytes(),
            b"\x01" * 16,
            lambda _frame_type, payload: written.append(payload),
        )
        channel._secure = crypto.SecureChannel(b"\x00" * 32, b"\x01" * 32, b"\x02" * 32)
        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, old_interval)

        def blast() -> None:
            for index in range(300):
                channel._send_message({"t": "evt", "n": index})

        threads = [threading.Thread(target=blast) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        counters = [_counter(frame) for frame in written]
        self.assertEqual(counters, list(range(len(counters))))


class ClientInstanceTests(unittest.TestCase):
    def test_instance_id_is_read_from_the_confirming_request(self):
        self.assertEqual(channel_mod._client_instance({"ci": " mac-1 "}), "mac-1")

    def test_missing_or_malformed_instance_id_is_legacy(self):
        self.assertEqual(channel_mod._client_instance({}), "")
        self.assertEqual(channel_mod._client_instance({"ci": 7}), "")
        self.assertEqual(channel_mod._client_instance({"ci": "x" * 65}), "")


class LocalRateLimitTests(unittest.TestCase):
    def test_loopback_is_recognised(self):
        for address in ("127.0.0.1", "::1", "127.0.0.2"):
            self.assertTrue(local_mod._is_loopback(address), address)
        for address in ("192.0.2.15", "198.51.100.4", "", "relay"):
            self.assertFalse(local_mod._is_loopback(address), address)

    def test_one_peer_reconnect_storm_does_not_lock_out_another(self):
        limiter = ratelimit.SlidingWindowLimiter(
            allow=ratelimit.LOCAL_CHANNEL_OPENS.allow, window=60.0
        )
        with mock.patch.object(ratelimit, "LOCAL_CHANNEL_OPENS", limiter):
            while limiter.allow_request("192.0.2.20"):
                pass
            self.assertTrue(limiter.allow_request("192.0.2.15"))

    def test_reconnect_storm_fits_the_per_peer_budget(self):
        """Two app instances x 3 local hints x 2 planes, reconnecting 4 times a minute."""
        self.assertGreaterEqual(ratelimit.LOCAL_CHANNEL_OPENS.allow, 2 * 3 * 2 * 4)


class _FakeLoop:
    def __init__(self) -> None:
        self.scheduled: list = []

    def call_soon_threadsafe(self, callback, *args) -> None:
        self.scheduled.append((callback, args))

    def call_later(self, _delay, _callback, *_args) -> None:
        return None


class RelayLanePinningTests(_TempStateMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._use_temp_state()
        self.client = relay_mod.RelayClient(
            RemoteService(),
            RemoteState(host_id="pin-test", host_name="t"),
            crypto.generate_private_key_bytes(),
        )
        self.loop = _FakeLoop()
        self.client._loop = self.loop  # type: ignore[assignment]
        self.sent: list[tuple[object, bytes]] = []

        def fake_send(coro, _loop):
            frame = coro.cr_frame.f_locals["frame"]
            socket = coro.cr_frame.f_locals["socket"]
            coro.close()
            self.sent.append((socket, frame))

        patcher = mock.patch.object(relay_mod.asyncio, "run_coroutine_threadsafe", fake_send)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.main = object()
        self.bulk = object()
        self.client._socket = self.main
        self.cid = b"\x07" * 16
        channel = self.client._open_channel(self.cid)
        assert channel is not None
        channel._secure = object()
        channel._confirmed = True
        channel._owns_logical_connection = False  # data plane

    def _attach_bulk(self) -> None:
        self.client._bulk_socket = self.bulk
        self.client._bulk_attached.set()

    def test_data_channel_stays_on_its_first_lane_when_bulk_attaches(self):
        self.client._write(protocol.FRAME_DATA, self.cid, b"a")
        self._attach_bulk()
        self.client._write(protocol.FRAME_DATA, self.cid, b"b")
        self.assertEqual([socket for socket, _ in self.sent], [self.main, self.main])

    def test_lost_lane_closes_the_channel_instead_of_reordering(self):
        self._attach_bulk()
        self.client._write(protocol.FRAME_DATA, self.cid, b"a")
        self.client._bulk_socket = None
        self.client._bulk_attached.clear()
        self.client._write(protocol.FRAME_DATA, self.cid, b"b")
        self.assertEqual([socket for socket, _ in self.sent], [self.bulk])
        self.assertEqual(len(self.loop.scheduled), 1)
        callback, args = self.loop.scheduled[0]
        callback(*args)
        self.assertNotIn(self.cid, self.client._channels)


class HostStartupTests(_TempStateMixin, unittest.TestCase):
    """Transports open before the first scan; data requests wait for it."""

    def setUp(self) -> None:
        self._use_temp_state()

    def test_hub_is_ready_unless_the_daemon_marks_it_starting(self):
        from corral.remote.sessions import SessionHub

        hub = SessionHub()
        self.assertTrue(hub.wait_ready(0))
        hub.mark_starting()
        self.assertFalse(hub.wait_ready(0))

    def test_data_request_during_startup_is_retryable_not_half_loaded(self):
        from corral.remote import service as service_mod
        from corral.remote.service import Connection

        class StartingHub:
            def wait_ready(self, timeout):
                return False

        service = RemoteService(StartingHub())  # type: ignore[arg-type]
        sent: list[dict] = []
        connection = Connection("aa" * 32, sent.append)
        connection.paired = True
        with mock.patch.object(service, "is_known_device", return_value=True), \
                mock.patch.object(service_mod, "_HUB_READY_WAIT", 0):
            service.handle(connection, protocol.request(1, protocol.M_SESSIONS_LIST, {}))
        self.assertFalse(sent[0]["ok"])
        self.assertEqual(sent[0]["e"]["code"], protocol.E_UNAVAILABLE)

    def test_daemon_opens_transports_before_the_first_scan(self):
        import inspect

        from corral.remote import daemon

        source = inspect.getsource(daemon.RemoteDaemon.run)
        self.assertLess(source.index("self.local.run(stop)"), source.index("self.hub.start"))
        self.assertLess(source.index("self.relay.run(stop)"), source.index("self.hub.start"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
