"""Desktop raw terminal stream: snapshot + ordered output, widest-viewer sizing."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from corral import embed
from corral.remote import protocol, ratelimit
from corral.remote import terminal_stream as ts
from corral.remote.service import _READONLY_METHODS, Connection, RemoteService

_STATE = "120|30|4|7|1|0|1|0|1|0|0|1|0|0|1|0|29|bar|1"


class PureHelperTests(unittest.TestCase):
    def test_control_output_unescape_keeps_utf8_and_restores_controls(self) -> None:
        raw = b"\\033[1m\xe4\xb8\xad\\134x\\015\\012"
        self.assertEqual(embed.unescape_control_output(raw), b"\x1b[1m\xe4\xb8\xad\\x\r\n")
        self.assertEqual(embed.unescape_control_output(b"plain"), b"plain")

    def test_pane_state_modes_restore_cursor_keys_mouse_and_shape(self) -> None:
        state = ts.PaneState(_STATE)
        self.assertEqual((state.cols, state.rows, state.cursor_x, state.cursor_y), (120, 30, 4, 7))
        modes = state.modes().decode()
        self.assertIn("\x1b[8;5H", modes)
        self.assertIn("\x1b[?1h", modes)  # application cursor keys
        self.assertIn("\x1b[?1000h", modes)
        self.assertIn("\x1b[?1006h", modes)
        self.assertIn("\x1b[5 q", modes)  # blinking bar
        self.assertNotIn("r", modes.split("H")[0])  # full-screen region is not re-sent

    def test_theme_report_is_osc_11_then_10_and_needs_a_background(self) -> None:
        self.assertEqual(
            ts.theme_report("#FFFFFF", "#1f2328"),
            b"\x1b]11;rgb:ffff/ffff/ffff\x07\x1b]10;rgb:1f1f/2323/2828\x07",
        )
        self.assertEqual(ts.theme_report("14171f"), b"\x1b]11;rgb:1414/1717/1f1f\x07")
        self.assertIsNone(ts.theme_report("", "#ffffff"))
        self.assertIsNone(ts.theme_report("white"))

    def test_snapshot_joins_lines_and_enters_alternate_screen_when_on(self) -> None:
        state = ts.PaneState(_STATE.replace("|1|0|1|0|1|", "|1|1|1|0|1|", 1))
        data = ts.build_snapshot(state, ["main 1", "main 2"], ["alt"]).decode()
        self.assertIn("main 1\x1b[0m\r\nmain 2", data)
        self.assertLess(data.index("main 2"), data.index("\x1b[?1049h"))
        self.assertLess(data.index("\x1b[?1049h"), data.index("alt"))


class _FakeChannel:
    """Ordered responses whose barrier the test controls."""

    def __init__(self) -> None:
        self.dead = False
        self.on_data = None
        self.output_seq = 0
        self.capture_lines = ["hello"]

    def request_ordered(self, *args, timeout=0):
        if args[0] == "display-message":
            return [_STATE], self.output_seq
        return list(self.capture_lines), self.output_seq

    def request(self, *args, timeout=0):
        return ["120|30"]


class BarrierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: list[dict] = []
        self.channel = _FakeChannel()
        self.stream = ts.TerminalStream("claude:a", "pane-a", self.events.append, lambda: "pane-a")
        self.stream._channel = self.channel
        patch = mock.patch.object(embed, "active_channel", return_value=self.channel)
        patch.start()
        self.addCleanup(patch.stop)

    def _feed(self, seq: int, data: bytes) -> None:
        self.channel.output_seq = seq
        self.stream._on_data("%1", data, seq)

    def test_output_already_in_the_capture_is_not_sent_again(self) -> None:
        self._feed(1, b"before")
        self._feed(2, b"also-before")
        self.channel.output_seq = 2
        self.stream._snapshot()
        self._feed(3, b"after")
        self.stream._flush()
        kinds = [(e["kind"], e["seq"]) for e in self.events]
        self.assertEqual(kinds, [("snapshot", 1), ("output", 2)])
        self.assertEqual(base64.b64decode(self.events[1]["data"]), b"after")
        self.assertEqual((self.events[0]["cols"], self.events[0]["rows"]), (120, 30))

    def test_sequence_numbers_are_consecutive_across_kinds(self) -> None:
        self.stream._snapshot()
        for seq in range(1, 4):
            self._feed(seq, b"x%d" % seq)
            self.stream._flush()
        self.stream._snapshot()
        self.assertEqual([e["seq"] for e in self.events], [1, 2, 3, 4, 5])

    def test_large_burst_is_split_into_bounded_events(self) -> None:
        self.stream._snapshot()
        self._feed(1, b"a" * (ts._MAX_EVENT_BYTES * 2 + 10))
        self.stream._flush()
        outputs = [e for e in self.events if e["kind"] == "output"]
        self.assertEqual(len(outputs), 3)
        self.assertEqual(sum(len(base64.b64decode(e["data"])) for e in outputs), ts._MAX_EVENT_BYTES * 2 + 10)

    def test_unhosted_session_ends_the_stream(self) -> None:
        stream = ts.TerminalStream("claude:b", "gone", self.events.append, lambda: "")
        with mock.patch.object(embed, "active_channel", return_value=None):
            stream._snapshot()
        self.assertEqual(self.events[-1]["kind"], "ended")


class _TerminalHub:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def terminal_attach(self, key, viewer, cols, rows, *, vote):
        self.calls.append(("attach", key, cols, rows, vote))
        return {"cols": cols, "rows": rows}

    def terminal_resize(self, key, viewer, cols, rows, *, vote):
        self.calls.append(("resize", key, cols, rows, vote))
        return {"cols": cols, "rows": rows}

    def terminal_resync(self, key):
        self.calls.append(("resync", key))

    def terminal_theme(self, key, report):
        self.calls.append(("theme", key, report))

    def terminal_input(self, key, data):
        self.calls.append(("input", key, data))

    def terminal_detach(self, key, viewer):
        self.calls.append(("detach", key))


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        from test_remote_service import FakeHub

        class Hub(_TerminalHub, FakeHub):
            def __init__(self) -> None:
                FakeHub.__init__(self)
                _TerminalHub.__init__(self)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        lan = mock.patch("corral.remote.lan.local_hints", return_value=[])
        lan.start()
        self.addCleanup(lan.stop)
        ratelimit.TERMINAL_TYPING.reset()
        self.hub = Hub()
        self.service = RemoteService(self.hub)  # type: ignore[arg-type]
        self.sent: list[dict] = []

    def _paired(self, mode: str = "full") -> Connection:
        code = self.service.begin_pairing(mode=mode)
        connection = Connection("ab" * 32, self.sent.append)
        self.service.attach(connection)
        self._call(connection, protocol.M_PAIR, {"code": code, "name": "Mac"})
        return connection

    def _call(self, connection: Connection, method: str, params: dict | None = None) -> dict:
        self.sent.clear()
        self.service.handle(connection, protocol.request(1, method, params or {}))
        return self.sent[-1]

    def test_capability_is_advertised(self) -> None:
        connection = self._paired()
        hello = self._call(connection, protocol.M_HELLO, {"name": "Mac"})
        self.assertTrue(hello["d"]["capabilities"][protocol.CAPABILITY_TERMINAL_STREAM])

    def test_attach_votes_and_reattach_resyncs(self) -> None:
        connection = self._paired()
        reply = self._call(connection, protocol.M_TERMINAL_ATTACH, {"key": "codex:a", "cols": 132, "rows": 40})
        self.assertTrue(reply["ok"])
        self.assertIn(("attach", "codex:a", 132, 40, True), self.hub.calls)
        self._call(connection, protocol.M_TERMINAL_ATTACH, {"key": "codex:a", "cols": 100, "rows": 40})
        self.assertIn(("resize", "codex:a", 100, 40, True), self.hub.calls)
        self.assertIn(("resync", "codex:a"), self.hub.calls)

    def test_readonly_device_watches_without_a_size_vote_and_cannot_type(self) -> None:
        connection = self._paired(mode="readonly")
        self._call(connection, protocol.M_TERMINAL_ATTACH, {"key": "codex:a", "cols": 132, "rows": 40})
        self.assertIn(("attach", "codex:a", 132, 40, False), self.hub.calls)
        blocked = self._call(connection, protocol.M_TERMINAL_INPUT, {"key": "codex:a", "data": "aGk="})
        self.assertEqual(blocked["e"]["code"], protocol.E_UNAUTHORIZED)
        self.assertNotIn(protocol.M_TERMINAL_INPUT, _READONLY_METHODS)

    def test_attach_and_theme_report_the_viewer_colours(self) -> None:
        connection = self._paired()
        light = ts.theme_report("#ffffff", "#1f2328")
        self._call(connection, protocol.M_TERMINAL_ATTACH,
                   {"key": "codex:a", "cols": 80, "rows": 24, "background": "#ffffff", "foreground": "#1f2328"})
        self.assertIn(("theme", "codex:a", light), self.hub.calls)
        reply = self._call(connection, protocol.M_TERMINAL_THEME, {"key": "codex:a", "background": "#14171f"})
        self.assertTrue(reply["ok"])
        self.assertIn(("theme", "codex:a", ts.theme_report("#14171f")), self.hub.calls)
        bad = self._call(connection, protocol.M_TERMINAL_THEME, {"key": "codex:a", "background": "dark"})
        self.assertEqual(bad["e"]["code"], protocol.E_USAGE)

    def test_readonly_device_does_not_report_colours(self) -> None:
        connection = self._paired(mode="readonly")
        self._call(connection, protocol.M_TERMINAL_ATTACH,
                   {"key": "codex:a", "cols": 80, "rows": 24, "background": "#ffffff"})
        self.assertFalse([c for c in self.hub.calls if c[0] == "theme"])
        blocked = self._call(connection, protocol.M_TERMINAL_THEME, {"key": "codex:a", "background": "#ffffff"})
        self.assertEqual(blocked["e"]["code"], protocol.E_UNAUTHORIZED)

    def test_bad_size_is_a_usage_error(self) -> None:
        connection = self._paired()
        reply = self._call(connection, protocol.M_TERMINAL_ATTACH, {"key": "codex:a", "cols": 0, "rows": 40})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)

    def test_input_is_raw_bytes(self) -> None:
        connection = self._paired()
        data = base64.b64encode(b"\x1b[A\x03").decode()
        self.assertTrue(self._call(connection, protocol.M_TERMINAL_INPUT, {"key": "codex:a", "data": data})["ok"])
        self.assertIn(("input", "codex:a", b"\x1b[A\x03"), self.hub.calls)

    def test_disconnect_withdraws_the_viewer(self) -> None:
        connection = self._paired()
        self._call(connection, protocol.M_TERMINAL_ATTACH, {"key": "codex:a", "cols": 80, "rows": 24})
        self.service.detach(connection)
        self.assertIn(("detach", "codex:a"), self.hub.calls)

    def test_stream_events_use_the_data_plane(self) -> None:
        connection = self._paired()
        data_frames: list[dict] = []
        connection.data_send = data_frames.append
        self._call(connection, protocol.M_TERMINAL_ATTACH, {"key": "codex:a", "cols": 80, "rows": 24})
        self.service._dispatch_event("term:codex:a", {"kind": "output", "seq": 1, "data": ""})
        self.assertEqual(data_frames[-1]["c"], "term:codex:a")


@unittest.skipUnless(shutil.which("tmux"), "needs real tmux")
class RealTmuxStreamTests(unittest.TestCase):
    SOCKET = "corral-test-stream"
    SESSION = "stream-it"

    def setUp(self) -> None:
        subprocess.run(["tmux", "-L", self.SOCKET, "kill-server"], capture_output=True)
        subprocess.run(
            ["tmux", "-L", self.SOCKET, "-f", "/dev/null", "new-session", "-d", "-s", self.SESSION,
             "-x", "80", "-y", "24", "/bin/sh"],
            check=True, capture_output=True,
        )
        self.addCleanup(subprocess.run, ["tmux", "-L", self.SOCKET, "kill-server"], capture_output=True)
        for target, value in (
            ("_BASE_ARGV", ("tmux", "-L", self.SOCKET)),
            ("tmux_argv", lambda name=None: ("tmux", "-L", self.SOCKET)),
        ):
            patch = mock.patch.object(embed.keepalive, target, value)
            patch.start()
            self.addCleanup(patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(embed.close_channel)
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.stream = ts.TerminalStream("claude:it", self.SESSION, self._emit, lambda: self.SESSION)
        self.addCleanup(self.stream.stop)
        time.sleep(0.3)

    def _emit(self, payload: dict) -> None:
        with self.lock:
            self.events.append(payload)

    def _wait(self, predicate, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                found = [e for e in self.events if predicate(e)]
            if found:
                return found[-1]
            time.sleep(0.05)
        self.fail(f"no matching event in {timeout}s: {[e['kind'] for e in self.events]}")

    def _stream_text(self) -> bytes:
        with self.lock:
            return b"".join(base64.b64decode(e.get("data", "")) for e in self.events)

    def test_snapshot_then_live_output_in_order(self) -> None:
        subprocess.run(["tmux", "-L", self.SOCKET, "send-keys", "-t", self.SESSION,
                        "printf 'mark-%s\\n' before", "Enter"], check=True)
        time.sleep(0.4)
        self.stream.start()
        snapshot = self._wait(lambda e: e["kind"] == "snapshot")
        self.assertEqual((snapshot["cols"], snapshot["rows"]), (80, 24))
        self.assertIn(b"mark-before", base64.b64decode(snapshot["data"]))
        self.assertTrue(self.stream.send_input(b"printf 'live-%s\\n' after\r"))
        self._wait(lambda e: e["kind"] == "output" and b"live-after" in base64.b64decode(e["data"]))
        with self.lock:
            seqs = [e["seq"] for e in self.events]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        # Raw stream: the shell echo carries real escape/control bytes, not tmux key names.
        self.assertIn(b"\r\n", self._stream_text())

    def test_desktop_vote_resizes_pane_and_resnapshots(self) -> None:
        self.stream.start()
        self._wait(lambda e: e["kind"] == "snapshot")
        effective = self.stream.vote("remote:mac:1", 132, 40)
        self.assertEqual(effective, (132, 40))
        snapshot = self._wait(lambda e: e["kind"] == "snapshot" and e["cols"] == 132)
        self.assertEqual(snapshot["rows"], 40)
        self.assertEqual(embed.pane_size(self.SESSION), (132, 40))
        # A wider TUI viewer wins; the Mac crops instead of narrowing the pane.
        embed.desired_host_size(self.SESSION, "tui:1", 160, 30)
        self.assertEqual(self.stream.vote("remote:mac:1", 132, 40), (160, 30))
        self.stream.withdraw("remote:mac:1")

    @unittest.skipUnless(embed.supports_theme_report(), "needs tmux >= 3.5")
    def test_reported_colours_answer_the_agents_background_query(self) -> None:
        self.stream.start()
        self._wait(lambda e: e["kind"] == "snapshot")
        self.stream.set_theme(ts.theme_report("#ffffff", "#1f2328"))
        time.sleep(0.4)
        probe = os.path.join(tempfile.mkdtemp(), "probe.py")
        with open(probe, "w") as handle:
            handle.write(
                "import os, select, sys, termios, tty\n"
                "fd = sys.stdin.fileno(); old = termios.tcgetattr(fd); tty.setraw(fd)\n"
                "os.write(1, b'\\x1b]11;?\\x07'); buf = b''\n"
                "while select.select([fd], [], [], 2)[0]:\n"
                "    buf += os.read(fd, 64)\n"
                "    if b'\\x07' in buf or b'\\x1b\\\\' in buf: break\n"
                "termios.tcsetattr(fd, termios.TCSADRAIN, old)\n"
                "print('BG=' + buf.decode('ascii', 'replace').split('rgb:')[-1][:14])\n"
            )
        subprocess.run(["tmux", "-L", self.SOCKET, "send-keys", "-t", self.SESSION,
                        f"python3 {probe}", "Enter"], check=True)
        self._wait(lambda e: e["kind"] == "output" and b"BG=ffff/ffff/ffff" in base64.b64decode(e["data"]))

    def test_alternate_screen_snapshot_keeps_both_screens(self) -> None:
        subprocess.run(["tmux", "-L", self.SOCKET, "send-keys", "-t", self.SESSION,
                        "echo main-line; printf '\\033[?1049h\\033[Halt-line'", "Enter"], check=True)
        time.sleep(0.5)
        self.stream.start()
        data = base64.b64decode(self._wait(lambda e: e["kind"] == "snapshot")["data"])
        self.assertIn(b"main-line", data[: data.index(b"\x1b[?1049h")])
        self.assertIn(b"alt-line", data[data.index(b"\x1b[?1049h"):])


if __name__ == "__main__":
    unittest.main()
