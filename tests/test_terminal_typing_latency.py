"""Guard the production raw-input path used by real Pi timing inspections.

These are routing assertions, not keyboard-to-screen latency measurements.
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from corral import embed
from corral.remote import terminal_stream as ts
from corral.remote.sessions import SessionHub


class TerminalTypingRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = ts.TerminalStream("pi:typing", "typing-pane", lambda _: None, lambda: "typing-pane")
        self.hub = object.__new__(SessionHub)
        self.hub._lock = threading.Lock()
        self.hub._terminals = {"pi:typing": (self.stream, {"remote:viewer"})}

    def test_keyboard_bytes_reach_the_owned_pane_unchanged(self) -> None:
        with mock.patch.object(embed, "send_bytes", return_value=True) as send, \
                mock.patch.object(self.stream, "activate") as activate:
            for data in (b"x", "中文".encode(), b"\x7f", b"\x1b[D"):
                with self.subTest(data=data):
                    self.hub.terminal_input("pi:typing", data, "remote:viewer")
                    activate.assert_called_with("remote:viewer")
                    send.assert_called_with("typing-pane", data, passive=False)

    def test_focus_reports_are_not_typing_or_a_new_size_claim(self) -> None:
        with mock.patch.object(embed, "send_bytes", return_value=True) as send, \
                mock.patch.object(self.stream, "activate") as activate:
            for data in (b"\x1b[I", b"\x1b[O", b"\x1b[I\x1b[O"):
                with self.subTest(data=data):
                    self.hub.terminal_input("pi:typing", data, "remote:viewer")
                    send.assert_called_with("typing-pane", data, passive=True)
            activate.assert_not_called()

    def test_passive_parser_replies_do_not_claim_the_viewer(self) -> None:
        with mock.patch.object(embed, "send_bytes", return_value=True) as send, \
                mock.patch.object(self.stream, "activate") as activate:
            self.hub.terminal_input("pi:typing", b"\x1b[1;1R", "remote:viewer", passive=True)
            send.assert_called_once_with("typing-pane", b"\x1b[1;1R", passive=True)
            activate.assert_not_called()

    def test_stream_sender_does_not_transform_the_native_input(self) -> None:
        with mock.patch.object(embed, "send_bytes", return_value=True) as send:
            data = "Pi typing 中文".encode()
            self.assertTrue(self.stream.send_input(data))
            send.assert_called_once_with("typing-pane", data)
