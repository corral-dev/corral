"""Initial-index preparation must not hold the serial encrypted RPC channel."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from corral.remote import crypto, protocol
from corral.remote import service as service_module
from corral.remote.service import Connection, RemoteService
from corral.remote.transport.channel import HostChannel


class _StartingHub:
    def __init__(self):
        self.ready = threading.Event()
        self.waiting = threading.Event()
        self.list_snapshot = mock.Mock(return_value={"sessions": [{"key": "claude:fixture"}]})
        self.runtimes = mock.Mock(return_value=[{"id": "claude", "available": True}])

    def wait_ready(self, timeout):
        if timeout:
            self.waiting.set()
        return self.ready.wait(timeout)


class StartupQueryLatencyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "CORRAL_CACHE_DIR": tmp.name, "CORRAL_STATE_DIR": tmp.name,
            "CORRAL_ISOLATE_MANAGED_HOSTS": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        self.hub = _StartingHub()
        self.service = RemoteService(self.hub)
        self.known = mock.patch.object(self.service, "is_known_device", return_value=True)
        self.known_mock = self.known.start()
        self.addCleanup(self.known.stop)
        access = mock.patch.object(self.service, "device_access", return_value="readonly")
        access.start()
        self.addCleanup(access.stop)
        self.responses = {}
        self.changed = threading.Condition()
        self.connection = Connection("aa" * 32, self._record)
        self.connection.paired = True
        # Exercise the real serial HostChannel queue, decryption, dispatcher and
        # encrypted response writer. Handshake has its own independent coverage.
        self.client_secure = crypto.SecureChannel(b"a" * 32, b"b" * 32, b"c" * 32)
        self.channel = HostChannel(
            self.service, crypto.generate_private_key_bytes(), b"x" * 16, self._wire_reply,
        )
        self.channel._secure = crypto.SecureChannel(b"b" * 32, b"a" * 32, b"c" * 32)
        self.channel._confirmed = True
        self.channel._connection = self.connection
        self.channel._owns_logical_connection = False
        self.addCleanup(self._stop_channel)
        self.addCleanup(self.hub.ready.set)

    def _record(self, message):
        with self.changed:
            self.responses[message["id"]] = message
            self.changed.notify_all()

    def _wire_reply(self, frame_type, payload):
        self.assertEqual(frame_type, protocol.FRAME_DATA)
        self._record(protocol.loads(self.client_secure.decrypt(payload)))

    def _send(self, req_id, method):
        message = protocol.request(req_id, method, {})
        self.channel.submit(protocol.FRAME_DATA, self.client_secure.encrypt(protocol.dumps(message)))

    def _response(self, req_id, timeout=2.0):
        with self.changed:
            self.assertTrue(self.changed.wait_for(lambda: req_id in self.responses, timeout))
            return self.responses[req_id]

    def _stop_channel(self):
        self.channel.close()
        if self.channel._worker is not None:
            self.channel._worker.join(timeout=2)
            self.assertFalse(self.channel._worker.is_alive())

    def test_two_startup_queries_do_not_hold_following_runtime_rpc(self):
        started = time.perf_counter()
        self._send(1, protocol.M_SESSIONS_LIST)
        self.assertTrue(self.hub.waiting.wait(2))
        self._send(2, protocol.M_PROJECTS_LIST)
        self._send(3, protocol.M_RUNTIMES_LIST)
        self.assertTrue(self._response(3)["ok"])
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertFalse(self.hub.ready.is_set())
        self.hub.list_snapshot.assert_not_called()
        self.hub.projects = mock.Mock(return_value=[])
        self.hub.ready.set()
        self.assertTrue(self._response(1)["ok"])
        self.assertTrue(self._response(2)["ok"])
        self.assertEqual(self.responses[1]["d"]["sessions"], [{"key": "claude:fixture"}])

    def test_startup_timeout_is_unavailable_not_partial_success(self):
        with mock.patch.object(service_module, "_HUB_READY_WAIT", 0.05):
            self._send(1, protocol.M_SESSIONS_LIST)
            response = self._response(1)
        self.assertFalse(response["ok"])
        self.assertEqual(response["e"]["code"], protocol.E_UNAVAILABLE)
        self.hub.list_snapshot.assert_not_called()

    def test_waiter_admission_is_bounded_without_queueing_later_frames(self):
        count = service_module._MAX_STARTUP_QUERIES
        for req_id in range(1, count + 2):
            self._send(req_id, protocol.M_SESSIONS_LIST)
        rejected = self._response(count + 1)
        self.assertEqual(rejected["e"]["code"], protocol.E_UNAVAILABLE)
        self.assertEqual(len(self.responses), 1)
        self._send(count + 2, protocol.M_RUNTIMES_LIST)
        self.assertTrue(self._response(count + 2)["ok"])
        self.hub.ready.set()
        for req_id in range(1, count + 1):
            self.assertTrue(self._response(req_id)["ok"])
        # All worker slots are returned after dispatch, not merely admission.
        acquired = []
        deadline = time.monotonic() + 2
        while len(acquired) < count and time.monotonic() < deadline:
            if self.service._startup_slots.acquire(timeout=0.05):
                acquired.append(True)
        for _ in acquired:
            self.service._startup_slots.release()
        self.assertEqual(len(acquired), count)

    def test_waiter_rechecks_revocation_before_reading_index(self):
        self._send(1, protocol.M_SESSIONS_LIST)
        self.assertTrue(self.hub.waiting.wait(2))
        self.known_mock.return_value = False
        with mock.patch.object(self.service, "_kick"):
            self.hub.ready.set()
            response = self._response(1)
        self.assertEqual(response["e"]["code"], protocol.E_UNAUTHORIZED)
        self.hub.list_snapshot.assert_not_called()

    def test_unready_write_is_rejected_without_starting_a_waiter_or_handler(self):
        self._send(1, protocol.M_SESSION_NEW)
        self.assertEqual(self._response(1)["e"]["code"], protocol.E_UNAVAILABLE)
        self.assertFalse(self.hub.waiting.is_set())
        self._send(2, protocol.M_RUNTIMES_LIST)
        self.assertTrue(self._response(2)["ok"])

    def test_native_prompt_keeps_its_separate_admission_after_startup(self):
        message = protocol.request(1, protocol.M_TERMINAL_PROMPT, {"native": True})
        with mock.patch.object(self.service._prompt_slots, "acquire", return_value=False):
            self.service.handle(self.connection, message)
            self.assertTrue(self.hub.waiting.wait(2))
            self.hub.ready.set()
            response = self._response(1)
        self.assertEqual(response["e"]["code"], protocol.E_UNAVAILABLE)

    def test_ready_rpc_reads_previous_catalog_during_a_full_scan(self):
        from corral.remote.sessions import SessionHub
        from corral.runtime.claude import ClaudeRuntime
        from corral.runtime.registry import RuntimeRegistry
        from corral.store import SessionStore

        runtime = ClaudeRuntime()
        hub = SessionHub()
        self.addCleanup(hub._transcript_cache.close)
        hub.store = SessionStore(limit=5, registry=RuntimeRegistry((runtime,)))
        hub.registry = hub.store.registry
        self.service.hub = hub
        session = {
            "source": "claude", "id": "fixture", "short_id": "fixture", "path": "",
            "cwd": "", "mtime": time.time(), "live": False,
            "fallback_title": "Synthetic scan fixture", "first_user_msg": "fixture",
        }
        scanning = threading.Event()
        release = threading.Event()
        failures = []

        def blocked_scan(*_args, **_kwargs):
            scanning.set()
            if not release.wait(5):
                raise TimeoutError("fixture scan was not released")
            return [dict(session)]

        def refresh():
            try:
                hub.store.refresh()
            except Exception as exc:
                failures.append(exc)

        with mock.patch.object(runtime, "scan_signature", return_value=None), \
                mock.patch.object(runtime, "scan_sessions", return_value=[session]) as scan:
            hub.store.load()
            self.assertIsNone(hub.store.get_load_error())
            scan.side_effect = blocked_scan
            thread = threading.Thread(target=refresh)
            thread.start()
            try:
                self.assertTrue(scanning.wait(2))
                started = time.perf_counter()
                self._send(1, protocol.M_SESSIONS_LIST)
                self.assertTrue(self._response(1)["ok"])
                self.assertLess(time.perf_counter() - started, 2.0)
                self.assertTrue(thread.is_alive())
                self.assertEqual(scan.call_count, 2)  # load + one refresh, never an RPC scan
                self.assertEqual(self.responses[1]["d"]["sessions"][0]["key"], "claude:fixture")
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])

    def test_thread_start_failure_releases_admission(self):
        with mock.patch.object(service_module.threading, "Thread", side_effect=RuntimeError):
            self.service.handle(self.connection, protocol.request(1, protocol.M_SESSIONS_LIST, {}))
        self.assertEqual(self._response(1)["e"]["code"], protocol.E_UNAVAILABLE)
        count = service_module._MAX_STARTUP_QUERIES
        acquired = [self.service._startup_slots.acquire(blocking=False) for _ in range(count)]
        for ok in acquired:
            if ok:
                self.service._startup_slots.release()
        self.assertEqual(acquired, [True] * count)


if __name__ == "__main__":
    unittest.main()
