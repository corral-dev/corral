"""Latest navigation covers the agent viewport, cancellation and write admission."""
from __future__ import annotations

import threading
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from corral import embed
from corral.remote import protocol
from corral.remote.service import Connection, RemoteService
from corral.remote.sessions import ActionError, SessionHub
from corral.ui import terminal_prompt_navigation as nav


@contextmanager
def ownership(*args, **kwargs):
    yield lambda: True


class NativeLatestTests(unittest.TestCase):
    def test_fullscreen_keys_never_type_submit_or_interrupt(self):
        for runtime, key in [("codex", "C-End"), ("claude", "C-End"), ("pi", "C-End"),
                             ("opencode", "C-M-g")]:
            with (self.subTest(runtime=runtime),
                  mock.patch.object(embed, "cancel_navigation"),
                  mock.patch.object(embed, "navigation_transaction", ownership),
                  mock.patch.object(nav, "_tmux", return_value="1"),
                  mock.patch.object(nav, "_screen", side_effect=["old\ncomposer draft", "latest\ncomposer draft"]),
                  mock.patch.object(embed, "send_key") as send,
                  mock.patch.object(embed, "send_literal") as literal):
                nav.return_to_latest("fixture", runtime)
                send.assert_called_once_with("fixture", key)
                literal.assert_not_called()

    def test_normal_screen_and_shell_never_receive_editor_shortcuts(self):
        with (mock.patch.object(embed, "cancel_navigation"),
              mock.patch.object(embed, "navigation_transaction", ownership),
              mock.patch.object(nav, "_tmux", return_value="0"),
              mock.patch.object(embed, "send_key") as send):
            for runtime in ["codex", "claude", "pi", "opencode", "cursor"]:
                nav.return_to_latest("fixture", runtime)
            send.assert_not_called()

    def test_stale_and_missing_panes_never_send(self):
        with (mock.patch.object(embed, "cancel_navigation"),
              mock.patch.object(embed, "navigation_transaction", ownership),
              mock.patch.object(nav, "_tmux", return_value=""),
              mock.patch.object(embed, "send_key") as send):
            for current in [lambda: False, lambda: True]:
                with self.assertRaises(nav.PromptNavigationError):
                    nav.return_to_latest("fixture", "codex", is_current=current)
            send.assert_not_called()

    def test_quoted_navigation_chrome_does_not_trigger_escape(self):
        before = "Find: quoted\nNo matches · full transcript · esc cancel\n› draft\nstatus"
        with (mock.patch.object(embed, "cancel_navigation"),
              mock.patch.object(embed, "navigation_transaction", ownership),
              mock.patch.object(nav, "_tmux", return_value="1"),
              mock.patch.object(nav, "_screen", side_effect=[before, "latest\n› draft"]),
              mock.patch.object(embed, "send_key") as send):
            nav.return_to_latest("fixture", "codex")
            send.assert_called_once_with("fixture", "C-End")

    def test_latest_cancels_an_inflight_prompt_before_waiting_for_input(self):
        entered, release = threading.Event(), threading.Event()
        stale = []
        def prompt():
            with embed.navigation_transaction("latest-race-fixture") as current:
                entered.set()
                release.wait(2)
                stale.append(not current())
        with mock.patch("corral.terminal_navigation_state._path") as path:
            # Use a real disposable lock/state directory, not the user's state.
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as directory:
                path.side_effect = lambda name, suffix: Path(directory) / (name + suffix)
                worker = threading.Thread(target=prompt)
                worker.start()
                self.assertTrue(entered.wait(2))
                embed.cancel_navigation("latest-race-fixture")
                release.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(stale, [True])


class LatestHostTests(unittest.TestCase):
    def setUp(self):
        self.hub = object.__new__(SessionHub)
        self.hub._lock = threading.RLock()
        self.hub._terminals = {"codex:a": (mock.Mock(), set())}
        self.hub.require_session = lambda key: {"source": "codex"}
        self.hub._hosted_name = lambda key: "fixture"

    def test_latest_uses_existing_identity_and_refreshes_stream(self):
        with mock.patch.object(nav, "return_to_latest") as latest:
            self.assertEqual(self.hub.terminal_latest("codex:a"), {"ok": True})
        self.assertEqual(latest.call_args.args, ("fixture", "codex"))
        self.assertTrue(latest.call_args.kwargs["is_current"]())
        self.hub._terminals["codex:a"][0].request_snapshot.assert_called_once()
        self.hub._hosted_name = lambda key: "replacement"
        self.assertFalse(latest.call_args.kwargs["is_current"]())

    def test_unhosted_latest_does_not_resume_or_navigate(self):
        self.hub._hosted_name = lambda key: ""
        with mock.patch.object(nav, "return_to_latest") as latest:
            with self.assertRaises(ActionError):
                self.hub.terminal_latest("codex:a")
        latest.assert_not_called()

    def test_stale_result_never_requests_snapshot(self):
        with mock.patch.object(nav, "return_to_latest", side_effect=nav.PromptNavigationError("prompt_stale")):
            with self.assertRaises(ActionError) as error:
                self.hub.terminal_latest("codex:a")
        self.assertEqual(error.exception.code, "stale")
        self.hub._terminals["codex:a"][0].request_snapshot.assert_not_called()

    def test_write_admission_and_late_prompt_retirement(self):
        service = object.__new__(RemoteService)
        service.hub = mock.Mock()
        connection = Connection("ab" * 32, lambda value: None)
        connection.access = "readonly"
        params = {"key": "codex:a", "navigation": "latest"}
        with self.assertRaises(ActionError):
            service._terminal_latest(connection, params)
        service.hub.terminal_latest.assert_not_called()
        self.assertNotIn(protocol.M_TERMINAL_LATEST, __import__(
            "corral.remote.service", fromlist=["_READONLY_METHODS"])._READONLY_METHODS)
        connection.access = "full"
        connection.terminal_prompt_requests["codex:a"] = "latest"
        service._terminal_latest(connection, params)
        current = service.hub.terminal_latest.call_args.kwargs["is_current"]
        self.assertTrue(current())
        with self.assertRaises(ActionError):
            service._terminal_prompt(connection, {"key": "codex:a", "navigation": "old-prompt",
                                                  "seq": 1, "generation": 9, "native": True})
        connection.terminal_prompt_requests["other"] = "other-request"
        self.assertTrue(current())
        connection.closed = True
        self.assertFalse(current())


class LatestUITests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_latest_controller_returns_local_and_native_without_collapsing(self):
        from textual.app import App

        from corral.ui.controllers.hud_controller import HudControllerMixin

        class Fixture(App, HudControllerMixin):
            def _sync_hud(self):
                pass
        app = Fixture()
        pane = SimpleNamespace(session_name="fixture", history_offset=0,
                               return_to_latest=mock.Mock(), _uses_detail_window=lambda: False,
                               _request_immediate_capture=mock.Mock())
        cell = SimpleNamespace(spec=SimpleNamespace(session_key="codex:a"), embed_pane=lambda: pane)
        app.store = SimpleNamespace(find_session=lambda key: {"source": "codex"})
        async with app.run_test() as pilot:
            with (mock.patch.object(embed, "cancel_navigation"),
                  mock.patch.object(nav, "return_to_latest") as latest):
                app.jump_to_latest("codex:a", cell)
                await app.workers.wait_for_complete()
                await pilot.pause()
                pane.return_to_latest.assert_called_once()
                latest.assert_called_once()
                self.assertEqual(pane.history_offset, 0)
                pane._request_immediate_capture.assert_called_once()
