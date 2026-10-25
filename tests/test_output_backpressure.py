"""Paint backpressure preserves ordered output without blocking the UI loop."""

from __future__ import annotations

import json
import os
import queue
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from textual._compositor import CompositorUpdate
from textual.app import App

from corral.ui.app import CorralApp
from corral.ui.terminal_theme import TerminalThemeLinuxDriver, query_runtime_theme


@unittest.skipIf(TerminalThemeLinuxDriver is None, "Unix terminal driver")
class OutputBackpressureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.driver = object.__new__(TerminalThemeLinuxDriver)
        self.output = queue.Queue(maxsize=30)
        self.driver._writer_thread = SimpleNamespace(_queue=self.output)
        self.app = CorralApp(object(), embed_ok=False)
        self.app._driver = self.driver

    def fill(self, count: int = 10) -> None:
        for index in range(count):
            self.output.put_nowait(f"ordered-{index}")

    def test_paint_stops_before_control_queue_is_full(self) -> None:
        self.fill(9)
        self.assertFalse(self.app.output_backpressured)
        self.output.put_nowait("tenth")
        self.assertTrue(self.app.output_backpressured)
        self.assertFalse(self.output.full())

    def test_many_paints_coalesce_into_one_recovery_timer(self) -> None:
        self.fill()
        frame = mock.Mock(spec=CompositorUpdate)
        with (
            mock.patch.object(self.app, "set_timer", return_value=mock.Mock()) as timer,
            mock.patch.object(self.app, "post_display_hook") as displayed,
            mock.patch.object(App, "_display") as native,
        ):
            for _ in range(100):
                self.app._display(mock.sentinel.screen, frame)
        timer.assert_called_once_with(0.05, self.app._resume_output)
        native.assert_not_called()
        self.assertEqual(displayed.call_count, 100)
        self.assertEqual(self.output.qsize(), 10)
        self.assertEqual([self.output.get_nowait() for _ in range(10)],
                         [f"ordered-{index}" for index in range(10)])

    def test_recovery_waits_then_repaints_latest_screen(self) -> None:
        self.fill()
        with (
            mock.patch.object(self.app, "set_timer", return_value=mock.Mock()) as timer,
            mock.patch.object(self.app, "_force_full_repaint") as repaint,
        ):
            self.app._resume_output()
            timer.assert_called_once()
            repaint.assert_not_called()
            while not self.output.empty():
                self.output.get_nowait()
            self.app._resume_output()
            repaint.assert_called_once_with()
            self.assertIsNone(self.app._output_repaint_timer)

    def test_no_delta_slips_in_ahead_of_pending_full_repaint(self) -> None:
        self.app._output_repaint_timer = mock.Mock()
        with mock.patch.object(App, "_display") as native:
            self.app._display(mock.sentinel.screen, mock.Mock(spec=CompositorUpdate))
        native.assert_not_called()

    def test_normal_paint_and_other_renderables_keep_native_path(self) -> None:
        frame = mock.Mock(spec=CompositorUpdate)
        with mock.patch.object(App, "_display") as native:
            self.app._display(mock.sentinel.screen, frame)
            native.assert_called_once_with(mock.sentinel.screen, frame)
        self.fill()
        with mock.patch.object(App, "_display") as native:
            self.app._display(mock.sentinel.screen, mock.sentinel.nonpaint)
            native.assert_called_once_with(mock.sentinel.screen, mock.sentinel.nonpaint)

    def test_theme_poll_never_waits_for_a_saturated_writer(self) -> None:
        self.fill(30)
        with mock.patch.object(self.driver, "write", side_effect=AssertionError("blocking write")):
            query_runtime_theme(self.driver)
        self.assertEqual(self.output.qsize(), 30)

    def test_theme_query_uses_same_ordered_writer(self) -> None:
        self.fill(2)
        query_runtime_theme(self.driver)
        self.assertEqual(self.output.get_nowait(), "ordered-0")
        self.assertEqual(self.output.get_nowait(), "ordered-1")
        self.assertIn("\x1b]11;?\x07", self.output.get_nowait())

    def test_shutdown_cancels_pending_repaint(self) -> None:
        timer = mock.Mock()
        self.app._output_repaint_timer = timer
        self.app.on_unmount()
        timer.stop.assert_called_once_with()
        self.assertIsNone(self.app._output_repaint_timer)


@unittest.skipIf(os.name == "nt", "Unix PTY")
class RealTerminalBackpressureTests(unittest.TestCase):
    def test_blocked_terminal_keeps_input_live_and_recovers_final_screen(self) -> None:
        import pty

        source = r'''
import json, os, sys
from pathlib import Path
from textual.widgets import Static
from corral.ui.app import CorralApp

class Fixture(CorralApp):
    def __init__(self):
        super().__init__(object(), embed_ok=False)
        self.tick = 0
        self.accepted = False
    def compose(self):
        yield Static("INITIAL", id="output")
    def on_mount(self, event):
        event.prevent_default()
        self.set_interval(0.01, self.update_fixture)
    def update_fixture(self):
        self.tick += 1
        text = "FINAL_BACKPRESSURE_SCREEN" if self.accepted else (str(self.tick % 10) * 110 + "\n") * 36
        self.query_one("#output", Static).update(text)
        path = Path(sys.argv[1])
        pending = path.with_suffix(".tmp")
        pending.write_text(json.dumps({"tick": self.tick, "accepted": self.accepted,
            "pressure": self.output_backpressured, "driver": type(self._driver).__name__,
            "queued": self._driver._writer_thread._queue.qsize()}))
        pending.replace(path)
    def _handle_exception(self, error):
        import traceback
        Path(sys.argv[1]).with_suffix(".error").write_text("".join(traceback.format_exception(error)))
        super()._handle_exception(error)
    def on_key(self, event):
        if event.key == "x": self.accepted = True
        if event.key == "q": self.exit()
Fixture().run(size=(120, 40))
'''
        with tempfile.TemporaryDirectory(prefix="corral-output-pty-") as directory:
            root = Path(directory)
            fixture = root / "fixture.py"
            fixture.write_text(source)
            status = root / "status.json"
            master, slave = pty.openpty()
            process = None
            output = bytearray()
            try:
                env = os.environ | {"TERM": "xterm-256color", "CORRAL_CACHE": "0",
                                    "CORRAL_ISOLATE_MANAGED_HOSTS": "1", "CORRAL_CACHE_DIR": str(root / "cache")}
                env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).resolve().parents[1] / "src"),
                                                    os.environ.get("PYTHONPATH", "")])
                process = subprocess.Popen([sys.executable, str(fixture), str(status)],
                    stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
                os.close(slave)
                slave = -1

                def read_status():
                    try:
                        return json.loads(status.read_text())
                    except (OSError, ValueError):
                        return {}

                def drain(timeout=0.01):
                    if select.select([master], [], [], timeout)[0]:
                        try:
                            output.extend(os.read(master, 65536))
                        except OSError:
                            pass

                deadline = time.monotonic() + 10
                while not read_status() and time.monotonic() < deadline:
                    drain()
                self.assertTrue(read_status(), output.decode(errors="replace")[-2000:])
                # Stop consuming the actual PTY until its output backs up.
                while not read_status().get("pressure") and time.monotonic() < deadline:
                    time.sleep(0.01)
                before = read_status()
                self.assertTrue(before.get("pressure"),
                    f"Fixture never filled the terminal writer: {before}, exit={process.poll()}, "
                    f"output={output[-1500:]!r}, error="
                    f"{status.with_suffix('.error').read_text() if status.with_suffix('.error').exists() else ''}")
                os.write(master, b"x")
                deadline = time.monotonic() + 2
                while not read_status().get("accepted") and time.monotonic() < deadline:
                    time.sleep(0.01)
                accepted = read_status()
                self.assertTrue(accepted.get("accepted"), "Terminal output blocked input handling")
                self.assertGreater(accepted["tick"], before["tick"])
                deadline = time.monotonic() + 5
                while b"FINAL_BACKPRESSURE_SCREEN" not in output and time.monotonic() < deadline:
                    drain()
                self.assertIn(b"FINAL_BACKPRESSURE_SCREEN", output)
                os.write(master, b"q")
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    drain()
                self.assertEqual(process.poll(), 0, output.decode(errors="replace")[-2000:])
            finally:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                if slave >= 0:
                    os.close(slave)
                os.close(master)


if __name__ == "__main__":
    unittest.main()
