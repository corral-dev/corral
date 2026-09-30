"""Regressions for deferred work crossing live-pane binding boundaries."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from textual import events
from textual.app import App, ComposeResult
from textual.geometry import Size

from corral.ui.embed_pane import EmbedPane


class DeferredResizeOwnershipTests(unittest.TestCase):
    def test_rebind_does_not_resize_new_session_with_previous_layout(self) -> None:
        pane = EmbedPane()
        pane.session_name = "corral-review-old"
        timer = mock.Mock()
        with (
            mock.patch.object(pane, "set_timer", return_value=timer),
            mock.patch.object(pane, "_update_app_cursor"),
            mock.patch("corral.embed.open_channel", return_value=None),
            mock.patch("corral.embed.desired_host_size", side_effect=lambda _n, _v, w, h: (w, h)),
            mock.patch("corral.embed.resize") as resize,
        ):
            pane._on_resize(events.Resize(Size(80, 24), Size(80, 24)))
            pane.focus_session("corral-review-new", target_size=(140, 30))
            pane._apply_pending_tmux_resize()

        self.assertEqual(resize.call_args_list, [mock.call("corral-review-new", 140, 30)])
        timer.stop.assert_called_once()

    def test_parking_then_reusing_pane_cancels_deferred_resize(self) -> None:
        pane = EmbedPane()
        pane.session_name = "corral-review-old"
        timer = mock.Mock()
        with (
            mock.patch.object(pane, "set_timer", return_value=timer),
            mock.patch.object(pane, "_update_app_cursor"),
            mock.patch("corral.embed.close_channel"),
            mock.patch("corral.embed.open_channel", return_value=None),
            mock.patch("corral.embed.resize") as resize,
        ):
            pane._on_resize(events.Resize(Size(80, 24), Size(80, 24)))
            pane.clear()
            pane.focus_session("corral-review-new", resize_immediately=False)
            pane._apply_pending_tmux_resize()

        resize.assert_not_called()
        timer.stop.assert_called_once()


class CaptureBindingOwnershipTests(unittest.TestCase):
    def _run_one_capture(self, *, switch_during_state: bool) -> tuple[EmbedPane, mock.Mock, mock.Mock]:
        pane = EmbedPane()
        pane.session_name = "corral-review-old"
        fake_app = SimpleNamespace(call_from_thread=mock.Mock())

        def switch_binding() -> None:
            pane._capture_generation += 1
            pane.session_name = "corral-review-new"
            pane._tmux_pane_size = (140, 30)
            pane._stop.set()

        def capture(*_args):
            if not switch_during_state:
                switch_binding()
            return "OLD FRAME"

        def state(*_args):
            switch_binding()
            return (0, 0, True, False, False, 0, 80, 24)

        with (
            mock.patch.object(EmbedPane, "app", new_callable=mock.PropertyMock, return_value=fake_app),
            mock.patch("corral.schedprio.boost_ui_worker"),
            mock.patch("corral.embed.active_channel", return_value=None),
            mock.patch("corral.embed.capture", side_effect=capture),
            mock.patch("corral.embed.pane_state", side_effect=state) as pane_state,
            mock.patch("corral.embed.parse_screen_rows", return_value=[]) as parse,
            mock.patch.object(pane, "_capture_size", return_value=(80, 24)),
            mock.patch.object(pane, "_heal_host_size_if_needed") as heal,
            mock.patch.object(pane._poke, "wait"),
        ):
            pane._capture_loop()

        self.assertEqual(pane._tmux_pane_size, (140, 30))
        parse.assert_not_called()
        fake_app.call_from_thread.assert_not_called()
        return pane, pane_state, heal

    def test_switch_during_capture_discards_before_state_query_and_parse(self) -> None:
        _pane, pane_state, heal = self._run_one_capture(switch_during_state=False)
        pane_state.assert_not_called()
        heal.assert_not_called()

    def test_switch_during_state_query_preserves_new_size_and_viewer(self) -> None:
        _pane, pane_state, heal = self._run_one_capture(switch_during_state=True)
        pane_state.assert_called_once()
        heal.assert_not_called()

    def test_unmount_invalidates_queued_frame_and_dead_callbacks(self) -> None:
        pane = EmbedPane()
        pane.session_name = "corral-review-old"
        generation = pane._capture_generation
        with (
            mock.patch.object(pane, "_set_real_cursor"),
            mock.patch("corral.embed.close_channel"),
        ):
            pane.on_unmount()

        self.assertFalse(pane._capture_is_current(generation, "corral-review-old"))
        with mock.patch.object(pane, "_sync_strips") as sync:
            pane._apply_capture(generation, "corral-review-old", [], None, None)
            pane._apply_dead(generation, "corral-review-old")
        sync.assert_not_called()
        self.assertFalse(pane.dead)

    def test_switch_during_viewer_registration_does_not_resize_previous_host(self) -> None:
        pane = EmbedPane()
        pane.session_name = "corral-review-old"
        generation = pane._capture_generation

        def register(*_args):
            pane._capture_generation += 1
            pane.session_name = "corral-review-new"
            pane._claimed_session = "corral-review-new"
            return (140, 30)

        with (
            mock.patch.object(pane, "_expected_host_size", return_value=(140, 30)),
            mock.patch("corral.embed.desired_host_size", side_effect=register),
            mock.patch("corral.embed.release_host_view") as release,
            mock.patch("corral.embed.resize") as resize,
        ):
            pane._heal_host_size_if_needed("corral-review-old", (80, 24), generation=generation)

        resize.assert_not_called()
        release.assert_called_once_with("corral-review-old", pane._viewer_id)
        self.assertEqual(pane._claimed_session, "corral-review-new")


class _PaneApp(App):
    def compose(self) -> ComposeResult:
        yield EmbedPane(id="review-pane")


@unittest.skipUnless(shutil.which("tmux"), "real terminal regression requires tmux")
class RealTerminalBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebound_pane_keeps_new_host_width_and_screen(self) -> None:
        """Use a disposable tmux socket and real capture/control paths."""
        socket = f"corral-tui-review-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        base = ("tmux", "-L", socket, "-f", "/dev/null")
        names = (f"review-old-{uuid.uuid4().hex[:8]}", f"review-new-{uuid.uuid4().hex[:8]}")
        with tempfile.TemporaryDirectory(prefix="corral-tui-review-") as fixture_root:
            try:
                for name, marker in zip(names, ("REVIEW_OLD_FRAME", "REVIEW_NEW_FRAME"), strict=True):
                    command = shlex.join((
                        sys.executable, "-u", "-c",
                        f"import sys; print({marker!r}, flush=True); sys.stdin.read()",
                    ))
                    subprocess.run(
                        (*base, "new-session", "-d", "-s", name, "-x", "140", "-y", "30", command),
                        check=True, capture_output=True, timeout=4,
                    )
                with (
                    mock.patch.object(embed.keepalive, "tmux_argv", return_value=base),
                    mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": fixture_root}),
                ):
                    app = _PaneApp()
                    async with app.run_test(size=(140, 30)) as pilot:
                        pane = app.query_one("#review-pane", EmbedPane)
                        pane.focus_session(names[0], target_size=(140, 30))
                        for _ in range(40):
                            if "REVIEW_OLD_FRAME" in pane.render().plain:
                                break
                            await pilot.pause(0.05)
                        self.assertIn("REVIEW_OLD_FRAME", pane.render().plain)
                        pane._on_resize(events.Resize(Size(80, 24), Size(80, 24)))
                        pane.focus_session(names[1], target_size=(140, 30))
                        await pilot.pause(0.4)
                        for _ in range(40):
                            if "REVIEW_NEW_FRAME" in pane.render().plain:
                                break
                            await pilot.pause(0.05)
                        self.assertIn("REVIEW_NEW_FRAME", pane.render().plain)
                        self.assertNotIn("REVIEW_OLD_FRAME", pane.render().plain)
                        self.assertEqual(embed.pane_size(names[1]), (140, 30))
                        screenshot = app.save_screenshot(path=fixture_root, filename="rebound-pane.svg")
                        self.assertTrue(Path(screenshot).is_file())
                    for name in names:
                        embed.close_channel(name)
            finally:
                subprocess.run(
                    (*base, "kill-server"), capture_output=True, timeout=4,
                )
from corral import embed
