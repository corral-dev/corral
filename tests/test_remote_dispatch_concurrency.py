"""Slow read-only RPCs must not hold the serial encrypted channel.

A Mac/iPhone client with many sessions pipelines reads (`session.get`,
`session.messages`, `session.prompts`, `terminal.peek`) with keystrokes,
mark-read and `terminal.attach`. Before this, `HostChannel._on_data` ran every
handler on the channel's reader thread, so one cold multi-MB parse delayed the
next frame by up to seconds (measured: 67/942 RPCs >= 1 s, max 20.2 s on the
client; `session.prompts` 607-983 ms followed by an 83 ms `terminal.attach`).
`RemoteService.dispatch` now moves a fixed whitelist of read-only methods to
bounded per-connection workers; everything ordering sensitive stays inline.

These tests drive the real `HostChannel` queue, decryption and encrypted
response writer, so the concurrency, ordering and saturation are the shipped
ones, not a mock of the dispatcher.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from corral.remote import crypto, protocol, ratelimit
from corral.remote import service as service_module
from corral.remote.service import Connection, RemoteService
from corral.remote.transport.channel import HostChannel

# A following frame must not wait for the blocked read. The bound is generous
# for a loaded machine but far below the 10 s the fake read can stay blocked.
_FOLLOWING_FRAME_BOUND = 1.0


class _DispatchHub:
    """Only the methods these tests route; slow reads block on an event."""

    def __init__(self) -> None:
        self.slow_started = threading.Event()
        self.release_slow = threading.Event()
        self.active_reads = 0
        self.max_active_reads = 0
        self.attach_calls: list[tuple] = []
        self.inputs: list[bytes] = []
        self.sent_text: list[tuple] = []
        self._lock = threading.Lock()

    # -- slow read (session.get is whitelisted) --------------------------
    def session_detail(self, key: str) -> dict:
        with self._lock:
            self.active_reads += 1
            self.max_active_reads = max(self.max_active_reads, self.active_reads)
        try:
            self.slow_started.set()
            self.release_slow.wait(10.0)
            return {"key": key, "title": "slow read"}
        finally:
            with self._lock:
                self.active_reads -= 1

    # -- frames that must stay on the reader thread ----------------------
    def terminal_attach(self, key, viewer, cols, rows, *, vote, active=True):
        self.attach_calls.append((key, cols, rows, vote))
        return {"cols": cols, "rows": rows}

    def terminal_input(self, key, data, viewer="", *, passive=False):
        self.inputs.append(data)

    def send_text(self, key, text, submit):
        self.sent_text.append((key, text, submit))


class _ChannelFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "CORRAL_CACHE_DIR": tmp.name,
            "CORRAL_STATE_DIR": tmp.name,
            "CORRAL_ISOLATE_MANAGED_HOSTS": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        ratelimit.TERMINAL_TYPING.reset()
        ratelimit.INPUT_ACTIONS.reset()
        self.addCleanup(ratelimit.TERMINAL_TYPING.reset)
        self.addCleanup(ratelimit.INPUT_ACTIONS.reset)
        self.hub = _DispatchHub()
        self.service = RemoteService(self.hub)  # type: ignore[arg-type]
        for target, value in (
            ("is_known_device", True),
            ("device_access", "full"),
        ):
            patcher = mock.patch.object(self.service, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.messages: list[dict] = []
        self.changed = threading.Condition()
        self.connection = Connection("aa" * 32, self._record)
        self.connection.paired = True
        # Real serial HostChannel queue, decryption and encrypted writer.
        self.client_secure = crypto.SecureChannel(b"a" * 32, b"b" * 32, b"c" * 32)
        self.channel = HostChannel(
            self.service, crypto.generate_private_key_bytes(), b"x" * 16, self._wire_reply,
        )
        self.channel._secure = crypto.SecureChannel(b"b" * 32, b"a" * 32, b"c" * 32)
        self.channel._confirmed = True
        self.channel._connection = self.connection
        self.channel._owns_logical_connection = False
        self.addCleanup(self._stop_channel)
        self.addCleanup(self.hub.release_slow.set)

    def _record(self, message: dict) -> None:
        with self.changed:
            self.messages.append(message)
            self.changed.notify_all()

    def _wire_reply(self, frame_type: int, payload: bytes) -> None:
        self.assertEqual(frame_type, protocol.FRAME_DATA)
        self._record(protocol.loads(self.client_secure.decrypt(payload)))

    def _send(self, req_id: int, method: str, params: dict | None = None) -> None:
        message = protocol.request(req_id, method, params or {})
        self.channel.submit(protocol.FRAME_DATA, self.client_secure.encrypt(protocol.dumps(message)))

    def _response(self, req_id: int, timeout: float = 5.0) -> dict:
        def match() -> dict | None:
            for message in self.messages:
                if message.get("t") == "res" and message.get("id") == req_id:
                    return message
            return None

        with self.changed:
            if not self.changed.wait_for(lambda: match() is not None, timeout):
                self.fail(f"no response for request {req_id} within {timeout}s: {self.messages}")
            message = match()
        assert message is not None
        return message

    def _wait_for(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition not reached in time")

    def _response_ids(self) -> list[int]:
        with self.changed:
            return [m["id"] for m in self.messages if m.get("t") == "res"]

    def _stop_channel(self) -> None:
        self.channel.close()
        if self.channel._worker is not None:
            self.channel._worker.join(timeout=2)
            self.assertFalse(self.channel._worker.is_alive())


class HeadOfLineTests(_ChannelFixture):
    def test_slow_read_does_not_delay_following_terminal_frames(self) -> None:
        self._send(1, protocol.M_SESSION_GET, {"key": "codex:slow"})
        self.assertTrue(self.hub.slow_started.wait(2.0), "slow read never started")

        started = time.perf_counter()
        self._send(2, protocol.M_TERMINAL_ATTACH, {"key": "codex:live", "cols": 100, "rows": 30})
        self._send(3, protocol.M_INPUT_TEXT, {"key": "codex:live", "text": "keep typing"})
        attach = self._response(2, timeout=_FOLLOWING_FRAME_BOUND)
        typed = self._response(3, timeout=_FOLLOWING_FRAME_BOUND)
        elapsed = time.perf_counter() - started

        self.assertTrue(attach["ok"], attach)
        self.assertTrue(typed["ok"], typed)
        self.assertLess(elapsed, _FOLLOWING_FRAME_BOUND)
        self.assertEqual(self.hub.attach_calls, [("codex:live", 100, 30, True)])
        self.assertEqual(self.hub.sent_text, [("codex:live", "keep typing", True)])
        # The slow read is still running: it was genuinely concurrent, not a
        # fake finished handler.
        self.assertNotIn(1, self._response_ids())

        self.hub.release_slow.set()
        slow = self._response(1)
        self.assertTrue(slow["ok"], slow)
        self.assertEqual(slow["d"]["key"], "codex:slow")

    def test_saturation_is_retryable_unavailable_not_an_unbounded_queue(self) -> None:
        bound = service_module._MAX_CONCURRENT_READS
        sent = bound + 1
        for req_id in range(1, sent + 1):
            self._send(req_id, protocol.M_SESSION_GET, {"key": f"codex:{req_id}"})
        self._wait_for(lambda: self.hub.max_active_reads == bound, timeout=10.0)

        started = time.perf_counter()
        rejected = self._response(sent, timeout=3.0)
        self.assertLess(time.perf_counter() - started, 3.0)
        self.assertFalse(rejected["ok"], rejected)
        self.assertEqual(rejected["e"]["code"], protocol.E_UNAVAILABLE)
        self.assertLessEqual(self.hub.max_active_reads, bound)
        self.assertEqual(self.hub.active_reads, bound)
        # Exactly one response came back before any slot was released: the
        # excess request was refused, never queued behind the running reads.
        self.assertEqual(self._response_ids(), [sent])

        self.hub.release_slow.set()
        for req_id in range(1, sent):
            self.assertTrue(self._response(req_id)["ok"])
        self.assertLessEqual(self.hub.max_active_reads, bound)

    def test_two_terminal_inputs_are_applied_in_arrival_order_behind_a_slow_read(self) -> None:
        self._send(1, protocol.M_SESSION_GET, {"key": "codex:slow"})
        self.assertTrue(self.hub.slow_started.wait(2.0), "slow read never started")

        self._send(2, protocol.M_TERMINAL_INPUT, {
            "key": "codex:live", "data": "YQ==",  # b"a"
        })
        self._send(3, protocol.M_TERMINAL_INPUT, {
            "key": "codex:live", "data": "Yg==",  # b"b"
        })
        self.assertTrue(self._response(2)["ok"])
        self.assertTrue(self._response(3)["ok"])
        self.assertEqual(self.hub.inputs, [b"a", b"b"])
        # Both receipts were written by the single reader thread in order.
        ids = [req for req in self._response_ids() if req in (2, 3)]
        self.assertEqual(ids, [2, 3])

        self.hub.release_slow.set()
        self.assertTrue(self._response(1)["ok"])


class WhitelistTests(unittest.TestCase):
    def test_only_slow_read_only_methods_are_offloaded(self) -> None:
        self.assertEqual(
            service_module._CONCURRENT_READ_METHODS,
            frozenset({
                protocol.M_SESSION_GET,
                protocol.M_SESSION_MESSAGES,
                protocol.M_SESSION_TOOL_DETAIL,
                protocol.M_SESSION_PROMPTS,
                protocol.M_SESSION_USER_PROMPTS,
                protocol.M_MEDIA_IMAGE,
                protocol.M_SEARCH,
                protocol.M_SEARCH_FULLTEXT,
                protocol.M_PROJECTS_LIST,
                protocol.M_RUNTIMES_LIST,
                protocol.M_TERMINAL_PEEK,
            }),
        )
        # Ordering-sensitive frames must never move off the reader thread.
        for method in (
            protocol.M_INPUT_TEXT,
            protocol.M_INPUT_KEYS,
            protocol.M_COMMAND_STATUS,
            protocol.M_TERMINAL_INPUT,
            protocol.M_TERMINAL_ATTACH,
            protocol.M_TERMINAL_RESIZE,
            protocol.M_TERMINAL_DETACH,
            protocol.M_TERMINAL_THEME,
            protocol.M_TERMINAL_PROMPT,
            protocol.M_SESSION_WATCH,
            protocol.M_SESSION_UNWATCH,
            protocol.M_SESSION_MARK_READ,
            protocol.M_LAYOUT_WATCH,
            protocol.M_SESSION_NEW,
            protocol.M_SESSION_RESUME,
            protocol.M_SESSION_RESTART,
            protocol.M_SESSION_HANDOFF,
            protocol.M_SESSION_COPY,
            protocol.M_SESSION_STOP,
            protocol.M_SESSION_DELETE,
            protocol.M_SESSION_PIN,
            protocol.M_PUSH_REGISTER,
            protocol.M_SHELL_OPEN,
            protocol.M_SHELL_CLOSE,
        ):
            self.assertNotIn(method, service_module._CONCURRENT_READ_METHODS, method)


if __name__ == "__main__":
    unittest.main()
