"""Native recovery must not treat a stale hosted name as a live assistant."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from corral import embed, keepalive, split_layout
from corral.models import LaunchPlan
from corral.remote import protocol, ratelimit
from corral.remote.service import Connection, RemoteService
from corral.remote.sessions import ActionError, SessionHub


class _ResumeFixture:
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="corral-resume-")
        self.addCleanup(temporary.cleanup)
        patch = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": temporary.name})
        patch.start()
        self.addCleanup(patch.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.events: list[tuple[str, dict]] = []
        self.hub = SessionHub(on_event=lambda channel, data: self.events.append((channel, data)))
        self.addCleanup(self.hub.stop)
        self.session = {
            "source": "claude", "id": "resume-fixture", "cwd": temporary.name,
            "live": False, "keepalive_name": "corral-claude-expired",
            "mtime": time.time(), "fallback_title": "Resume acceptance",
        }
        self.key = "claude:resume-fixture"
        self.hub.store.sessions = {"claude": [self.session]}
        self.hub.store.mark_hosted(self.key, self.session["keepalive_name"])
        self.runtime = mock.Mock(id="claude")
        self.runtime.build_resume_plan.return_value = LaunchPlan(argv=("sh",), cwd=temporary.name)
        patch = mock.patch.object(self.hub, "_runtime_of", return_value=self.runtime)
        patch.start()
        self.addCleanup(patch.stop)


class NativeResumeTests(_ResumeFixture, unittest.TestCase):
    def test_dead_binding_is_cleared_and_same_history_resumed(self) -> None:
        with mock.patch.object(embed, "available", return_value=True), \
             mock.patch.object(embed, "pane_liveness", return_value="dead"), \
             mock.patch.object(embed, "close_channel") as close, \
             mock.patch.object(embed, "forget_alive") as forget, \
             mock.patch.object(embed, "host_session", return_value="corral-claude-fresh") as launch:
            result = self.hub.resume_session(self.key)
        launch.assert_called_once()
        close.assert_called_once_with("corral-claude-expired")
        forget.assert_called_once_with("corral-claude-expired")
        self.assertEqual(result["key"], self.key)
        self.assertEqual(self.session["id"], "resume-fixture")
        self.assertEqual(self.hub.store.hosted[self.key], "corral-claude-fresh")

    def test_live_binding_is_reused_even_if_scan_says_ended(self) -> None:
        with mock.patch.object(embed, "available", return_value=True), \
             mock.patch.object(embed, "pane_liveness", return_value="alive"), \
             mock.patch.object(embed, "host_session") as launch:
            self.assertEqual(self.hub.resume_session(self.key)["key"], self.key)
        launch.assert_not_called()
        self.runtime.build_resume_plan.assert_not_called()

    def test_unknown_binding_is_rejected_without_spawning_or_clearing(self) -> None:
        with mock.patch.object(embed, "available", return_value=True), \
             mock.patch.object(embed, "pane_liveness", return_value="unknown"), \
             mock.patch.object(embed, "host_session") as launch:
            with self.assertRaises(ActionError) as error:
                self.hub.resume_session(self.key)
        self.assertEqual(error.exception.code, "unavailable")
        launch.assert_not_called()
        self.assertEqual(self.hub.store.hosted[self.key], "corral-claude-expired")

    def test_start_failure_does_not_restore_dead_binding(self) -> None:
        with mock.patch.object(embed, "available", return_value=True), \
             mock.patch.object(embed, "pane_liveness", return_value="dead"), \
             mock.patch.object(embed, "close_channel"), \
             mock.patch.object(embed, "forget_alive"), \
             mock.patch.object(embed, "host_session", side_effect=embed.EmbedError("fixture failure")):
            with self.assertRaises(ActionError):
                self.hub.resume_session(self.key)
        self.assertNotIn(self.key, self.hub.store.hosted)
        self.assertNotIn("keepalive_name", self.session)

    def test_simultaneous_resumes_create_only_one_process(self) -> None:
        entered, release = threading.Event(), threading.Event()
        results: list[dict] = []

        def launch(*_args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("resume launch did not release")
            return "corral-claude-fresh"

        def state(name):
            return "alive" if name == "corral-claude-fresh" else "dead"

        with mock.patch.object(embed, "available", return_value=True), \
             mock.patch.object(embed, "pane_liveness", side_effect=state), \
             mock.patch.object(embed, "close_channel"), \
             mock.patch.object(embed, "forget_alive"), \
             mock.patch.object(embed, "host_session", side_effect=launch) as host:
            threads = [threading.Thread(target=lambda: results.append(self.hub.resume_session(self.key)))
                       for _ in range(2)]
            try:
                threads[0].start()
                self.assertTrue(entered.wait(3))
                threads[1].start()
            finally:
                release.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(3)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(results), 2)
            host.assert_called_once()

    def test_input_recovery_can_resume_under_the_shared_lock(self) -> None:
        with mock.patch.object(embed, "available", return_value=True), \
             mock.patch.object(embed, "pane_liveness", return_value="dead"), \
             mock.patch.object(embed, "forget_alive"), \
             mock.patch.object(embed, "host_session", return_value="corral-claude-fresh"):
            result = self.hub._recover_dead_pane_binding(self.key, "corral-claude-expired", cause="pane_gone")
        self.assertEqual(result, ("corral-claude-fresh", True))


@unittest.skipUnless(shutil.which("tmux"), "tmux required")
class RealResumeTests(_ResumeFixture, unittest.TestCase):
    def test_mac_resume_dispatch_starts_real_pane_and_streams_output(self) -> None:
        socket = "corral-resume-test-" + uuid.uuid4().hex[:12]
        command = ["tmux", "-L", socket]
        # Keep a sentinel pane so a missing target has an authoritative error.
        subprocess.run([*command, "new-session", "-d", "-s", "sentinel", "sleep 60"], check=True)
        self.addCleanup(lambda: subprocess.run([*command, "kill-server"], capture_output=True))
        with mock.patch.object(keepalive, "tmux_argv", side_effect=lambda *args: command), \
             mock.patch.object(embed, "socket_for_session", return_value=socket), \
             mock.patch.object(keepalive, "ensure_server"), \
             mock.patch.object(keepalive, "reap_pressure"):
            service = RemoteService(self.hub)
            sent: list[dict] = []
            connection = Connection("ab" * 32, sent.append)
            service.attach(connection)
            ratelimit.PAIR_ATTEMPTS.reset()
            ratelimit.PAIR_ATTEMPTS_HOURLY.reset()
            ratelimit.SESSION_CREATE.reset()
            ratelimit.TERMINAL_TYPING.reset()
            ratelimit.INPUT_ACTIONS.reset()
            code = service.begin_pairing()
            service.handle(connection, protocol.request(1, protocol.M_PAIR, {"code": code}))
            self.assertTrue(sent[-1]["ok"], sent[-1])
            service.handle(connection, protocol.request(2, protocol.M_SESSION_RESUME, {"key": self.key}))
            self.assertTrue(sent[-1]["ok"], sent[-1])
            name = self.session["keepalive_name"]
            self.assertNotEqual(name, "corral-claude-expired")
            self.assertEqual(embed.pane_liveness(name), "alive")
            service.handle(connection, protocol.request(3, protocol.M_SESSION_RESUME, {"key": self.key}))
            self.assertTrue(sent[-1]["ok"], sent[-1])
            self.runtime.build_resume_plan.assert_called_once()
            service.handle(connection, protocol.request(
                4, protocol.M_TERMINAL_ATTACH, {"key": self.key, "cols": 80, "rows": 24}
            ))
            self.assertTrue(any(reply.get("id") == 4 and reply.get("ok") for reply in sent), sent)
            service.handle(connection, protocol.request(5, protocol.M_TERMINAL_INPUT, {
                "key": self.key, "data": base64.b64encode(b"echo RESUME_REAL_OK\r").decode(),
            }))
            self.assertTrue(any(reply.get("id") == 5 and reply.get("ok") for reply in sent), sent)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                data = b"".join(base64.b64decode(reply["d"]["data"]) for reply in list(sent)
                                if reply.get("t") == "evt" and "data" in reply.get("d", {}))
                screen = subprocess.run([*command, "capture-pane", "-p", "-t", name],
                                        capture_output=True, text=True, check=True).stdout
                if b"RESUME_REAL_OK" in data and "RESUME_REAL_OK" in screen.splitlines():
                    break
                time.sleep(0.05)
            else:
                self.fail("resumed pane did not stream output")
            service.handle(connection, protocol.request(6, protocol.M_TERMINAL_DETACH, {"key": self.key}))
            embed.close_channel(name)
            # iPhone's ended-chat send must recover this same dead binding too.
            keepalive.kill(name)
            service.handle(connection, protocol.request(7, protocol.M_INPUT_TEXT, {
                "key": self.key, "text": "echo PHONE_RESUME_OK", "submit": True,
            }))
            self.assertTrue(sent[-1]["ok"], sent[-1])
            name = self.session["keepalive_name"]
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                screen = subprocess.run([*command, "capture-pane", "-p", "-t", name],
                                        capture_output=True, text=True, check=True).stdout
                if "PHONE_RESUME_OK" in screen.splitlines():
                    break
                time.sleep(0.05)
            else:
                self.fail("phone recovery did not submit to the resumed pane")
            # Its explicit Restart route must still replace a live process.
            old_pid = subprocess.check_output([*command, "display-message", "-p", "-t", name,
                                               "#{pane_pid}"]).strip()
            with mock.patch.object(self.hub.registry, "build_launch_plan",
                                   return_value=self.runtime.build_resume_plan.return_value):
                service.handle(connection, protocol.request(8, protocol.M_SESSION_RESTART, {"key": self.key}))
            self.assertTrue(sent[-1]["ok"], sent[-1])
            name = self.session["keepalive_name"]
            new_pid = subprocess.check_output([*command, "display-message", "-p", "-t", name,
                                               "#{pane_pid}"]).strip()
            self.assertNotEqual(new_pid, old_pid)


if __name__ == "__main__":
    unittest.main()
