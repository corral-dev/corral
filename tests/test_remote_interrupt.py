"""session.interrupt: stop a running turn with the runtime's own interrupt key.

Covers routing, pairing access, capability, per-runtime key presses sent as
separate writes, and the never-resume rule for ended or vanished panes.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from corral import embed, split_layout
from corral.remote import protocol, ratelimit
from corral.remote.service import _CONFIRM_METHODS, _READONLY_METHODS, Connection, RemoteService
from corral.remote.sessions import ActionError, SessionHub


class InterruptFakeHub:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def resolve_session_key(self, key: str) -> str:
        return key

    def runtimes(self):
        return [{"id": "claude", "name": "Claude", "available": True}]

    def interrupt_session(self, key: str) -> None:
        self.calls.append(("interrupt_session", key))


class ServiceInterruptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        for bucket in (ratelimit.PAIR_ATTEMPTS, ratelimit.PAIR_ATTEMPTS_HOURLY, ratelimit.INPUT_ACTIONS):
            bucket.reset()
        self.hub = InterruptFakeHub()
        self.service = RemoteService(self.hub)  # type: ignore[arg-type]
        self.sent: list[dict] = []

    def _pair(self, **kwargs) -> Connection:
        code = self.service.begin_pairing(**kwargs)
        connection = Connection("aa" * 32, self.sent.append)
        self.service.attach(connection)
        self.service.handle(connection, protocol.request(1, protocol.M_PAIR, {"code": code}))
        return connection

    def _call(self, connection: Connection, method: str, params: dict | None = None) -> dict:
        self.sent.clear()
        self.service.handle(connection, protocol.request(2, method, params or {}))
        self.assertEqual(len(self.sent), 1)
        return self.sent[0]

    def test_interrupt_routes_without_confirmation(self) -> None:
        self.assertNotIn(protocol.M_SESSION_INTERRUPT, _CONFIRM_METHODS)
        reply = self._call(self._pair(), protocol.M_SESSION_INTERRUPT, {"key": "claude:s1"})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["d"], {"ok": True})
        self.assertEqual(self.hub.calls, [("interrupt_session", "claude:s1")])

    def test_readonly_pairing_cannot_interrupt(self) -> None:
        self.assertNotIn(protocol.M_SESSION_INTERRUPT, _READONLY_METHODS)
        reply = self._call(self._pair(mode="readonly"), protocol.M_SESSION_INTERRUPT, {"key": "claude:s1"})
        self.assertEqual(reply["e"]["code"], protocol.E_UNAUTHORIZED)
        self.assertEqual(self.hub.calls, [])

    def test_missing_key_is_usage_error(self) -> None:
        reply = self._call(self._pair(), protocol.M_SESSION_INTERRUPT, {})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.calls, [])

    def test_hello_advertises_interrupt(self) -> None:
        reply = self._call(self._pair(), protocol.M_HELLO, {"name": "Phone"})
        self.assertTrue(reply["d"]["capabilities"][protocol.CAPABILITY_SESSION_INTERRUPT])


class HubInterruptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self._tmp.name})
        env.start()
        self.addCleanup(env.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.hub = SessionHub(scan_limit=10)
        self.addCleanup(self.hub.stop)
        self.session = {"source": "claude", "id": "s1", "keepalive_name": "corral-claude-s1"}
        for name, value in (
            ("find_session", None),
            ("session_key_migrations", {}),
        ):
            patcher = mock.patch.object(self.hub.store, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.hub.store.find_session.side_effect = lambda _key: self.session
        self.results: list[embed.InjectionResult] = []
        self.presses: list[tuple] = []

        def _send(name, *keys):
            self.presses.append((name, *keys))
            return self.results.pop(0) if self.results else embed.InjectionResult(True)

        send = mock.patch.object(embed, "send_key_detailed", side_effect=_send)
        send.start()
        self.addCleanup(send.stop)
        sleep = mock.patch("corral.remote.sessions.time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        resume = mock.patch.object(self.hub, "resume_session")
        self.resume = resume.start()
        self.addCleanup(resume.stop)

    def _interrupt(self, source: str) -> list[tuple]:
        self.session = {"source": source, "id": "s1", "keepalive_name": f"corral-{source}-s1"}
        self.presses.clear()
        self.hub.interrupt_session(f"{source}:s1")
        return self.presses

    def test_single_escape_runtimes(self) -> None:
        for source in ("claude", "codex", "pi"):
            with self.subTest(source=source):
                self.assertEqual(self._interrupt(source), [(f"corral-{source}-s1", "Escape")])

    def test_opencode_needs_two_separate_escapes(self) -> None:
        self.sleep.reset_mock()
        self.assertEqual(
            self._interrupt("opencode"),
            [("corral-opencode-s1", "Escape"), ("corral-opencode-s1", "Escape")],
        )
        self.sleep.assert_called_once_with(0.3)

    def test_cursor_uses_ctrl_c(self) -> None:
        self.assertEqual(self._interrupt("cursor"), [("corral-cursor-s1", "C-c")])

    def test_unhosted_session_is_not_resumed(self) -> None:
        self.session = {"source": "claude", "id": "s1"}
        with self.assertRaises(ActionError) as caught:
            self.hub.interrupt_session("claude:s1")
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertEqual(self.presses, [])
        self.resume.assert_not_called()

    def test_gone_pane_fails_without_recovery(self) -> None:
        self.results = [embed.InjectionResult(False, "pane_gone")]
        with self.assertRaises(ActionError) as caught:
            self.hub.interrupt_session("claude:s1")
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertEqual(len(self.presses), 1)
        self.resume.assert_not_called()

    def test_uncertain_press_reports_uncertainty(self) -> None:
        self.results = [embed.InjectionResult(False, "tmux_busy", uncertain=True)]
        with self.assertRaises(ActionError) as caught:
            self.hub.interrupt_session("claude:s1")
        self.assertIn("may not have reached", caught.exception.message)

    def test_second_press_failure_is_uncertain(self) -> None:
        self.session = {"source": "opencode", "id": "s1", "keepalive_name": "corral-opencode-s1"}
        self.results = [embed.InjectionResult(True), embed.InjectionResult(False, "pane_gone")]
        with self.assertRaises(ActionError) as caught:
            self.hub.interrupt_session("opencode:s1")
        self.assertIn("may not have reached", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
