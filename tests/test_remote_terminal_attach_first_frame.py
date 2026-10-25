"""`terminal.attach` must acknowledge the subscription, not the first screen.

Measured on this host (2026-10-25, lab tmux pane with 4000 history rows):
attach returns in 26 ms while the first `term:` snapshot arrives at 57 ms; when
the capture is delayed by 400 ms attach stays at 29 ms and the snapshot moves to
476 ms. The first screen therefore already arrives asynchronously on the
existing snapshot path (`TerminalStream` worker, `term:<session>` channel) and
`terminal.attach` only votes the size and returns it. These tests lock that
contract down instead of re-implementing it.

The remaining race is detach/re-attach while a capture is still in flight: the
stopped stream must not publish that late snapshot into the newer attachment's
`term:` subscription.
"""

from __future__ import annotations

import base64
import os
import queue
import tempfile
import threading
import time
import unittest
from unittest import mock

from corral import embed, split_layout
from corral.remote import protocol
from corral.remote import terminal_stream as ts
from corral.remote.service import Connection, RemoteService, _terminal_viewer
from corral.remote.sessions import SessionHub

_STATE = "100|30|4|7|1|0|1|0|1|0|0|1|0|0|1|0|29|bar|1"


def _hosted_session(name: str) -> dict:
    return {
        "source": "claude",
        "id": "attach-first-frame",
        "short_id": "attach-first-frame",
        "cwd": "/tmp",
        "cwd_display": "/tmp",
        "mtime": time.time(),
        "display_time": "01-01 12:00",
        "size_kb": 3.0,
        "status_tag": "running",
        "live": True,
        "keepalive_name": name,
        "fallback_title": "attach",
        "attention_kind": "working",
        "last_user_msg": "",
        "last_agent_msg": "",
        "path": "/tmp/attach-first-frame.jsonl",
    }


class _FakeControlChannel:
    """Control-channel stand-in: no tmux, ordered capture never used."""

    def __init__(self) -> None:
        self.dead = False
        self.on_data = None
        self.commands: list[tuple] = []

    def request(self, *args, timeout=0):
        return ["100|30"]

    def request_ordered(self, *args, timeout=0):
        raise AssertionError("capture is patched in these tests")

    def command(self, *args):
        self.commands.append(args)
        return True


class AttachFirstFrameTests(unittest.TestCase):
    PANE = "pane-attach-first-frame"
    KEY = "claude:attach-first-frame"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "CORRAL_CACHE_DIR": tmp.name,
            "CORRAL_STATE_DIR": tmp.name,
            "XDG_CONFIG_HOME": tmp.name,
            "CORRAL_ISOLATE_MANAGED_HOSTS": "1",
        })
        env.start()
        self.addCleanup(env.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.hub = SessionHub(scan_limit=10)
        self.session = _hosted_session(self.PANE)
        for target, value in (
            ("find_session", mock.Mock(side_effect=lambda key: self.session if key == self.KEY else None)),
            ("session_key_migrations", mock.Mock(return_value={})),
        ):
            patcher = mock.patch.object(self.hub.store, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = RemoteService(self.hub)
        for target, value in (("is_known_device", True), ("device_access", "full")):
            patcher = mock.patch.object(self.service, target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Never touch a real tmux server or the shared viewer registry from a test.
        self.channels: dict[str, _FakeControlChannel] = {}

        def open_channel(name, on_data=None):
            channel = _FakeControlChannel()
            channel.on_data = on_data
            self.channels[name] = channel
            return channel

        for target, value in (
            ("open_channel", open_channel),
            ("active_channel", lambda name: self.channels.get(name)),
            ("close_channel", lambda *args, **kwargs: None),
            ("resize", lambda *args, **kwargs: None),
            ("pane_size", lambda name: (100, 30)),
            ("desired_host_size", lambda name, viewer, cols, rows, used_at=0.0: (cols, rows)),
        ):
            patcher = mock.patch.object(embed, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.messages: list[dict] = []
        self.changed = threading.Condition()
        self.connection = Connection("ab" * 32, self._record)
        self.connection.paired = True
        self.captures: queue.Queue = queue.Queue()
        self.capture_count = 0
        self.capture_lock = threading.Lock()

        def delayed_capture(_stream, _channel):
            with self.capture_lock:
                self.capture_count += 1
                marker = f"capture-{self.capture_count}"
            gate = threading.Event()
            self.captures.put((marker, gate))
            gate.wait(10.0)
            return ts.PaneState(_STATE), [f"screen-{marker}"], None, 0

        capturer = mock.patch.object(ts.TerminalStream, "_capture", delayed_capture)
        capturer.start()
        self.addCleanup(capturer.stop)
        self.addCleanup(self._release_all_captures)
        # Stop the hub while the tmux/embed patches are still active, then drop
        # any capture a late tick started.
        self.addCleanup(self.hub.stop)

    def _release_all_captures(self) -> None:
        while True:
            try:
                _index, gate = self.captures.get_nowait()
            except queue.Empty:
                return
            gate.set()

    def _record(self, message: dict) -> None:
        with self.changed:
            self.messages.append(message)
            self.changed.notify_all()

    def _call(self, req_id: int, method: str, params: dict) -> dict:
        self.service.dispatch(self.connection, protocol.request(req_id, method, params))
        return self._response(req_id)

    def _response(self, req_id: int, timeout: float = 2.0) -> dict:
        def match() -> dict | None:
            for message in self.messages:
                if message.get("t") == "res" and message.get("id") == req_id:
                    return message
            return None

        with self.changed:
            if not self.changed.wait_for(lambda: match() is not None, timeout):
                self.fail(f"no response for {req_id}: {self.messages}")
            reply = match()
        assert reply is not None
        return reply

    def _next_capture(self, timeout: float = 5.0) -> tuple[str, threading.Event]:
        try:
            return self.captures.get(timeout=timeout)
        except queue.Empty:
            self.fail("the stream never started its capture")

    def _snapshots(self, key: str) -> list[dict]:
        with self.changed:
            return [
                message["d"]
                for message in self.messages
                if message.get("t") == "evt" and message.get("c") == f"term:{key}"
                and message.get("d", {}).get("kind") == "snapshot"
            ]

    def _wait_snapshot(self, key: str, timeout: float = 3.0) -> dict:
        def match() -> dict | None:
            found = self._snapshots(key)
            return found[-1] if found else None

        with self.changed:
            if not self.changed.wait_for(lambda: match() is not None, timeout):
                self.fail(f"no snapshot on term:{key}: {self.messages}")
            snapshot = match()
        assert snapshot is not None
        return snapshot

    def test_attach_returns_the_effective_size_while_the_capture_is_pending(self) -> None:
        started = time.perf_counter()
        reply = self._call(1, protocol.M_TERMINAL_ATTACH, {
            "key": self.KEY, "cols": 100, "rows": 30,
        })
        attach_seconds = time.perf_counter() - started
        _marker, gate = self._next_capture()

        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["d"], {"cols": 100, "rows": 30})
        self.assertFalse(gate.is_set(), "the snapshot must still be pending")
        self.assertLess(attach_seconds, 1.0)
        self.assertEqual(self._snapshots(self.KEY), [])

        gate.set()
        snapshot = self._wait_snapshot(self.KEY)
        self.assertEqual((snapshot["cols"], snapshot["rows"]), (100, 30))
        self.assertIn(b"screen-capture-1", base64.b64decode(snapshot["data"]))

    def test_detach_and_resize_racing_an_in_flight_capture_keep_the_newer_attachment(self) -> None:
        first = self._call(1, protocol.M_TERMINAL_ATTACH, {
            "key": self.KEY, "cols": 100, "rows": 30,
        })
        self.assertTrue(first["ok"], first)
        old_stream, _viewers = self.hub._terminals[self.KEY]
        old_marker, old_gate = self._next_capture()

        self.assertTrue(self._call(2, protocol.M_TERMINAL_DETACH, {"key": self.KEY})["ok"])
        self.assertNotIn(f"term:{self.KEY}", self.connection.channels)
        self.assertNotIn(self.KEY, self.hub._terminals)

        second = self._call(3, protocol.M_TERMINAL_ATTACH, {
            "key": self.KEY, "cols": 110, "rows": 32,
        })
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["d"], {"cols": 110, "rows": 32})
        new_marker, new_gate = self._next_capture()
        self.assertNotEqual(old_marker, new_marker)

        resized = self._call(4, protocol.M_TERMINAL_RESIZE, {
            "key": self.KEY, "cols": 120, "rows": 40,
        })
        self.assertTrue(resized["ok"], resized)
        self.assertEqual(resized["d"], {"cols": 120, "rows": 40})
        _stream, viewers = self.hub._terminals[self.KEY]
        self.assertEqual(viewers, {_terminal_viewer(self.connection)})

        # The first stream was stopped while its capture was still in flight. Its
        # late snapshot must not be published into the newer subscription.
        old_gate.set()
        old_stream._thread.join(timeout=2.0)
        self.assertFalse(old_stream._thread.is_alive(), "the stopped stream never finished")
        self.assertEqual(self._snapshots(self.KEY), [])

        new_gate.set()
        snapshot = self._wait_snapshot(self.KEY)
        self.assertIn(b"screen-capture-2", base64.b64decode(snapshot["data"]))
        _stream, viewers = self.hub._terminals[self.KEY]
        self.assertIn(_terminal_viewer(self.connection), viewers)

    def test_stopped_stream_does_not_publish_after_stop(self) -> None:
        stream = ts.TerminalStream(self.KEY, self.PANE, self.messages.append, lambda: self.PANE)
        stream.start()
        _marker, gate = self._next_capture()
        stream.stop()
        gate.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual([m.get("kind") for m in self.messages], [])
        self.assertTrue(stream._stop.is_set())


if __name__ == "__main__":
    unittest.main()
