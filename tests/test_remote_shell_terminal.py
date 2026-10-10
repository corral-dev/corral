"""Project shells: login shells in a project folder, streamed like agent panes."""

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

from corral import embed, keepalive, liveness
from corral.legacy_names import SOCKET_NAME, is_managed_session, socket_for_session
from corral.remote import protocol, ratelimit
from corral.remote import shell_terminal as st
from corral.remote.service import Connection, RemoteService


class NamingTests(unittest.TestCase):
    def test_keys_map_to_prefixed_names_and_reject_anything_else(self) -> None:
        self.assertEqual(st.name_for_key("shell:0123abcd"), "corralsh-0123abcd")
        self.assertEqual(st.key_for_name("corralsh-0123abcd"), "shell:0123abcd")
        for bad in ("shell:", "shell:../etc", "shell:0123ABCD", "codex:0123abcd", "shell:0123abcd;x"):
            self.assertEqual(st.name_for_key(bad), "", bad)

    def test_shells_live_on_the_keepalive_server_but_outside_every_agent_path(self) -> None:
        self.assertEqual(socket_for_session("corralsh-0123abcd"), SOCKET_NAME)
        self.assertFalse(is_managed_session("corralsh-0123abcd"))
        self.assertIsNone(liveness._parse_managed_session_name("corralsh-0123abcd"))


class _ShellHub:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def terminal_attach(self, key, viewer, cols, rows, *, vote):
        self.calls.append(("attach", key, vote))
        return {"cols": cols, "rows": rows}

    def terminal_input(self, key, data, viewer=""):
        self.calls.append(("input", key, data, viewer))

    def terminal_detach(self, key, viewer):
        self.calls.append(("detach", key))


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        from test_remote_service import FakeHub

        class Hub(_ShellHub, FakeHub):
            def __init__(self) -> None:
                FakeHub.__init__(self)
                _ShellHub.__init__(self)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (
            mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": tmp.name}),
            mock.patch("corral.remote.lan.local_hints", return_value=[]),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        ratelimit.TERMINAL_TYPING.reset()
        ratelimit.INPUT_ACTIONS.reset()
        self.hub = Hub()
        self.service = RemoteService(self.hub)  # type: ignore[arg-type]
        self.sent: list[dict] = []

    def _paired(self, mode: str = "full") -> Connection:
        code = self.service.begin_pairing(mode=mode)
        connection = Connection("cd" * 32, self.sent.append)
        self.service.attach(connection)
        self._call(connection, protocol.M_PAIR, {"code": code, "name": "Mac"})
        return connection

    def _call(self, connection: Connection, method: str, params: dict | None = None) -> dict:
        self.sent.clear()
        self.service.handle(connection, protocol.request(1, method, params or {}))
        return self.sent[-1]

    def test_capability_is_advertised(self) -> None:
        hello = self._call(self._paired(), protocol.M_HELLO, {"name": "Mac"})
        self.assertTrue(hello["d"]["capabilities"][protocol.CAPABILITY_PROJECT_SHELL])

    def test_readonly_pairing_cannot_list_open_or_watch_a_shell(self) -> None:
        connection = self._paired(mode="readonly")
        for method, params in (
            (protocol.M_SHELL_LIST, {}),
            (protocol.M_SHELL_OPEN, {"cwd": "/tmp"}),
            (protocol.M_TERMINAL_ATTACH, {"key": "shell:0123abcd", "cols": 80, "rows": 24}),
        ):
            self.assertEqual(self._call(connection, method, params)["e"]["code"], protocol.E_UNAUTHORIZED, method)
        self.assertEqual(self.hub.calls, [])

    def test_shell_keys_never_reach_session_layout_pin_or_image_methods(self) -> None:
        connection = self._paired()
        for method, params in (
            (protocol.M_LAYOUT_SET_GROUP, {"project": "/tmp", "keys": ["codex:a", "shell:0123abcd"]}),
            (protocol.M_LAYOUT_REMOVE, {"key": "shell:0123abcd"}),
            (protocol.M_LAYOUT_PIN, {"key": "shell:0123abcd"}),
            (protocol.M_SESSION_PIN, {"key": "shell:0123abcd"}),
            (protocol.M_SESSION_MARK_READ, {"key": "shell:0123abcd"}),
            (protocol.M_INPUT_IMAGE, {"key": "shell:0123abcd", "data": "AA=="}),
            (protocol.M_INPUT_KEYS, {"key": "shell:0123abcd", "keys": ["Enter"]}),
        ):
            self.assertEqual(self._call(connection, method, params)["e"]["code"], protocol.E_USAGE, method)
        self.assertEqual(self.hub.calls, [])

    def test_missing_folder_is_not_found(self) -> None:
        reply = self._call(self._paired(), protocol.M_SHELL_OPEN, {"cwd": "/no/such/folder/for/corral"})
        self.assertEqual(reply["e"]["code"], protocol.E_NOT_FOUND)

    def test_typing_identifies_the_viewer(self) -> None:
        connection = self._paired()
        data = base64.b64encode(b"ls\r").decode()
        self._call(connection, protocol.M_TERMINAL_INPUT, {"key": "shell:0123abcd", "data": data})
        call = self.hub.calls[-1]
        self.assertEqual(call[:3], ("input", "shell:0123abcd", b"ls\r"))
        self.assertTrue(call[3].startswith("remote:"))


@unittest.skipUnless(shutil.which("tmux"), "needs real tmux")
class RealTmuxShellTests(unittest.TestCase):
    SOCKET = "corral-test-shell"

    def setUp(self) -> None:
        argv = ("tmux", "-L", self.SOCKET)
        subprocess.run([*argv, "kill-server"], capture_output=True)
        self.addCleanup(subprocess.run, [*argv, "kill-server"], capture_output=True)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = os.path.realpath(tmp.name)
        for patch in (
            mock.patch.object(keepalive, "_BASE_ARGV", argv),
            mock.patch.object(keepalive, "tmux_argv", lambda name=None: argv),
            mock.patch.object(keepalive, "tmux_argv_for_session", lambda name: argv),
            mock.patch.object(keepalive, "ensure_server", lambda: None),
            mock.patch.object(keepalive, "ensure_config_file", lambda: "/dev/null"),
            mock.patch.object(st, "login_shell", lambda: "/bin/sh"),
            mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": tmp.name}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        embed._manual_window_size_sockets.discard(SOCKET_NAME)
        self.addCleanup(embed.close_channel)
        self.events: list[dict] = []
        self.lock = threading.Lock()

    def _emit(self, payload: dict) -> None:
        with self.lock:
            self.events.append(payload)

    def _wait(self, predicate, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                found = [e for e in self.events if predicate(e)]
            if found:
                return found[-1]
            time.sleep(0.05)
        self.fail(f"no matching event in {timeout}s: {[e['kind'] for e in self.events]}")

    def test_open_list_stream_size_and_close(self) -> None:
        shell = st.open_shell(self.folder, 100, 30)
        self.assertTrue(shell["key"].startswith("shell:"))
        self.assertEqual(shell["project"], self.folder)
        self.assertEqual([s["key"] for s in st.list_shells()], [shell["key"]])
        name = st.name_for_key(shell["key"])
        self.assertEqual(embed.pane_size(name), (100, 30))

        stream = st.ShellTerminalStream(shell["key"], name, self._emit,
                                        lambda: name if st.alive(name) else "")
        self.addCleanup(stream.stop)
        stream.start()
        self._wait(lambda e: e["kind"] == "snapshot")
        self.assertTrue(stream.send_input(b"pwd\r"))
        self._wait(lambda e: e["kind"] == "output" and self.folder.encode() in base64.b64decode(e["data"]))

        # Latest active viewer decides the grid, not the widest.
        stream.vote("remote:mac:1", 120, 40)
        stream.vote("remote:phone:1", 60, 20)
        self._wait(lambda e: e["kind"] == "snapshot" and e["cols"] == 60 and e["rows"] == 20)
        stream.activate("remote:mac:1")
        self._wait(lambda e: e["kind"] == "snapshot" and e["cols"] == 120 and e["rows"] == 40)
        stream.withdraw("remote:mac:1")
        self._wait(lambda e: e["kind"] == "snapshot" and e["cols"] == 60)

        self.assertTrue(st.close_shell(shell["key"]))
        self._wait(lambda e: e["kind"] == "ended", timeout=4.0)
        self.assertEqual(st.list_shells(), [])

    def test_folder_names_with_tabs_and_newlines_stay_listed(self) -> None:
        odd = os.path.join(self.folder, "a\tb\nc")
        os.mkdir(odd)
        shell = st.open_shell(odd, 80, 24)
        listed = {s["key"]: s for s in st.list_shells()}
        self.assertEqual(listed[shell["key"]]["project"], odd)
        st.close_shell(shell["key"])

    def test_folder_metadata_survives_literal_escapes_and_control_bytes(self) -> None:
        for leaf in ("literal%09%1F\\037", "a|b\x1fc\x1ed\x7f", "中文🦉\u0085folder", "a\r\x07\x1bz"):
            with self.subTest(leaf=leaf):
                folder = os.path.join(self.folder, leaf)
                os.mkdir(folder)
                shell = st.open_shell(folder, 80, 24)
                try:
                    listed = {s["key"]: s for s in st.list_shells()}
                    self.assertEqual(listed[shell["key"]]["project"], folder)
                    self.assertEqual(listed[shell["key"]]["cwd"], folder)
                    self.assertTrue(st.alive(st.name_for_key(shell["key"])))
                finally:
                    st.close_shell(shell["key"])

    def test_exit_ends_the_shell(self) -> None:
        shell = st.open_shell(self.folder, 80, 24)
        name = st.name_for_key(shell["key"])
        subprocess.run([*keepalive._BASE_ARGV, "send-keys", "-t", name, "exit", "Enter"], check=True)
        deadline = time.monotonic() + 3
        while st.alive(name) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(st.alive(name))


if __name__ == "__main__":
    unittest.main()
