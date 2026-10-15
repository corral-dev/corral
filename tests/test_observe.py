"""corral.observe.py：结构化事件日志（~/.cache/corral/events.log）。"""

from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import time
import unittest
from unittest import mock


class ObserveTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cache_dir = self._tmpdir.name
        from corral import observe

        self.observe = observe
        self._events_log = os.path.join(self.cache_dir, "events.log")
        self._embed_error_log = os.path.join(self.cache_dir, "embed-error.log")
        self._stacks_log = os.path.join(self.cache_dir, "stacks.log")
        self._patchers = [
            mock.patch.object(observe, "CACHE_DIR", self.cache_dir),
            mock.patch.object(observe, "EVENTS_LOG", self._events_log),
            mock.patch.object(observe, "EMBED_ERROR_LOG", self._embed_error_log),
            mock.patch.object(observe, "STACKS_LOG", self._stacks_log),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        observe.reset_for_tests()

    def _lines(self) -> list[dict]:
        if not os.path.isfile(self._events_log):
            return []
        out = []
        with open(self._events_log, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def test_event_writes_json_line_with_ts_and_name(self) -> None:
        self.observe.init(debug=False)
        self.observe.event("scan_done", duration_ms=12, runtime_count=2)
        rows = self._lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "scan_done")
        self.assertEqual(rows[0]["duration_ms"], 12)
        self.assertEqual(rows[0]["runtime_count"], 2)
        self.assertEqual(rows[0]["pid"], os.getpid())
        self.assertIn("ts", rows[0])

    def test_debug_silent_unless_debug_enabled(self) -> None:
        self.observe.init(debug=False)
        self.observe.debug("osc_probe", ok=True)
        self.assertEqual(self._lines(), [])
        self.observe.init(debug=True)
        self.observe.debug("osc_probe", ok=True)
        rows = self._lines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "osc_probe")
        self.assertEqual(rows[0]["level"], "debug")

    def test_redacts_sensitive_fields(self) -> None:
        self.observe.init(debug=False)
        self.observe.event(
            "x",
            text="secret dialogue",
            prompt="do not log",
            messages=[{"role": "user"}],
            runtime="claude",
        )
        row = self._lines()[0]
        self.assertEqual(row["text"], "<redacted>")
        self.assertEqual(row["prompt"], "<redacted>")
        self.assertEqual(row["messages"], "<redacted>")
        self.assertEqual(row["runtime"], "claude")

    def test_rotates_events_without_discarding_previous_context(self) -> None:
        self.observe.init(debug=False)
        with open(self._events_log, "w", encoding="utf-8") as fh:
            fh.write('{"name":"before_rotation"}\n')
            fh.write("x" * (256 * 1024 - 100))
        self.observe.event("after_rotation", ok=True)
        self.assertLessEqual(os.path.getsize(self._events_log), 256 * 1024 + 512)
        rows = self._lines()
        self.assertEqual([row["name"] for row in rows], ["after_rotation"])
        with open(self._events_log + ".1", encoding="utf-8") as fh:
            self.assertIn("before_rotation", fh.read())

    def test_stall_records_live_stack_once_and_recovery_duration(self) -> None:
        callbacks: queue.Queue = queue.Queue()

        class Loop:
            def call_soon_threadsafe(self, callback, *args) -> None:  # noqa: ANN001
                callbacks.put((callback, args))

        watchdog = self.observe.TuiStallWatchdog(Loop(), interval=0.01, threshold=0.08)
        watchdog.start()
        try:
            callback, args = callbacks.get(timeout=2)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if os.path.isfile(self._stacks_log):
                    break
                time.sleep(0.01)
            self.assertTrue(os.path.isfile(self._stacks_log))
            with open(self._stacks_log, encoding="utf-8") as fh:
                stack_body = fh.read()
            self.assertIn("reason=tui_stall", stack_body)
            time.sleep(0.05)
            callback(*args)
            rows = self._lines()
            stalls = [row for row in rows if row["name"] == "tui_stall"]
            recovered = [row for row in rows if row["name"] == "tui_stall_recovered"]
            self.assertEqual(len(stalls), 1)
            self.assertEqual(len(recovered), 1)
            self.assertEqual(stalls[0]["stall_id"], recovered[0]["stall_id"])
            self.assertIn(f"stall_id={stalls[0]['stall_id']}", stack_body)
            self.assertGreaterEqual(recovered[0]["duration_ms"], 80)
            self.assertTrue(stalls[0]["stack_captured"])
        finally:
            watchdog.stop()

    def test_recovery_is_recorded_when_watchdog_could_not_sample(self) -> None:
        callbacks: queue.Queue = queue.Queue()

        class Loop:
            def call_soon_threadsafe(self, callback, *args) -> None:  # noqa: ANN001
                callbacks.put((callback, args))

        watchdog = self.observe.TuiStallWatchdog(Loop(), interval=0.2, threshold=0.05)
        watchdog.start()
        try:
            callback, args = callbacks.get(timeout=2)
            time.sleep(0.07)
            self.assertTrue(callbacks.empty())  # one outstanding probe, no queue buildup
            callback(*args)
            rows = self._lines()
            self.assertEqual([row["name"] for row in rows], ["tui_stall_recovered"])
            self.assertFalse(rows[0]["stack_attempted"])
        finally:
            watchdog.stop()

    def test_timed_records_duration_ms(self) -> None:
        self.observe.init(debug=False)
        with self.observe.timed("list_rebuild", mode="full"):
            time.sleep(0.01)
        row = self._lines()[0]
        self.assertEqual(row["name"], "list_rebuild")
        self.assertEqual(row["mode"], "full")
        self.assertGreaterEqual(row["duration_ms"], 5)

    def test_log_exception_writes_events_and_embed_error(self) -> None:
        self.observe.init(debug=False)
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            self.observe.log_exception("抓帧", exc)
        rows = self._lines()
        self.assertEqual(rows[0]["name"], "error")
        self.assertEqual(rows[0]["where"], "抓帧")
        self.assertEqual(rows[0]["exc_type"], "RuntimeError")
        self.assertNotIn("traceback", rows[0])
        with open(self._embed_error_log, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("抓帧", body)
        self.assertIn("RuntimeError", body)
        self.assertIn("boom", body)
        self.assertIn("Traceback", body)

    def test_read_last_error_returns_none_when_missing(self) -> None:
        self.assertIsNone(self.observe.read_last_error())

    def test_read_last_error_returns_most_recent_entry(self) -> None:
        self.observe.init(debug=False)
        try:
            raise RuntimeError("first")
        except RuntimeError as exc:
            self.observe.log_exception("早期", exc)
        try:
            raise NameError("name '_project_groups' is not defined")
        except NameError as exc:
            self.observe.log_exception("TUI 未捕获异常", exc)
        last = self.observe.read_last_error()
        self.assertIsNotNone(last)
        assert last is not None
        self.assertEqual(last["where"], "TUI 未捕获异常")
        self.assertEqual(last["exc_type"], "NameError")
        self.assertIn("_project_groups", last["exc_msg"])
        self.assertIn("Traceback", last["traceback"])
        self.assertIn("ts", last)

    def test_error_log_rotation_preserves_last_error(self) -> None:
        with mock.patch.object(self.observe, "_MAX_LOG_BYTES", 500):
            try:
                raise RuntimeError("before rotation")
            except RuntimeError as exc:
                self.observe.log_exception("scan", exc)
            try:
                raise RuntimeError("after rotation")
            except RuntimeError as exc:
                self.observe.log_exception("capture", exc)
        self.assertTrue(os.path.isfile(self._embed_error_log + ".1"))
        last = self.observe.read_last_error()
        self.assertIsNotNone(last)
        self.assertIn("after rotation", last["exc_msg"])

    def test_crash_hooks_log_uncaught_exception(self) -> None:
        self.observe.init(debug=False)
        self.observe.install_crash_hooks()
        try:
            raise ValueError("fatal flash")
        except ValueError:
            # 模拟 sys.excepthook：把当前异常交给 hook，而不是真的退出进程。
            exc_type, exc, tb = sys.exc_info()
            assert exc_type is not None and exc is not None
            sys.excepthook(exc_type, exc, tb)
        last = self.observe.read_last_error()
        self.assertIsNotNone(last)
        assert last is not None
        self.assertEqual(last["where"], "进程未捕获异常")
        self.assertEqual(last["exc_type"], "ValueError")
        self.assertIn("fatal flash", last["exc_msg"])

    def test_log_exception_unwraps_worker_failed_style_wrapper(self) -> None:
        """Textual WorkerFailed 只带短消息；必须展开 .error 写出内层完整栈。"""
        self.observe.init(debug=False)

        def _boom() -> None:
            raise NameError("name '_project_groups' is not defined")

        try:
            _boom()
        except NameError as inner:
            class WorkerFailed(Exception):
                def __init__(self, error: BaseException) -> None:
                    self.error = error
                    super().__init__(f"Worker raised exception: {error!r}")

            wrapped = WorkerFailed(inner)
            self.observe.log_exception("TUI 未捕获异常", wrapped)

        rows = self._lines()
        self.assertEqual(rows[0]["exc_type"], "NameError")
        self.assertIn("_project_groups", rows[0]["exc_msg"])
        self.assertEqual(rows[0]["via"], "WorkerFailed")
        last = self.observe.read_last_error()
        self.assertIsNotNone(last)
        assert last is not None
        self.assertEqual(last["exc_type"], "NameError")
        self.assertIn("_project_groups", last["exc_msg"])
        self.assertIn("Traceback", last["traceback"])
        self.assertIn("_boom", last["traceback"])
        self.assertIn("via WorkerFailed", last["traceback"])

    def test_log_exception_unwraps_cause_chain(self) -> None:
        self.observe.init(debug=False)
        try:
            try:
                raise NameError("name '_project_groups' is not defined")
            except NameError as inner:
                raise RuntimeError("wrapper") from inner
        except RuntimeError as wrapped:
            self.observe.log_exception("TUI 未捕获异常", wrapped)
        rows = self._lines()
        self.assertEqual(rows[0]["exc_type"], "NameError")
        self.assertEqual(rows[0]["via"], "RuntimeError")
        last = self.observe.read_last_error()
        assert last is not None
        self.assertEqual(last["exc_type"], "NameError")
        self.assertIn("via RuntimeError", last["traceback"])


class CorralEmbedErrorBridgeTests(unittest.TestCase):
    """corral._log_embed_error 必须转调 observe（双写 events + embed-error）。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        cache = self._tmpdir.name
        import corral
        from corral import observe

        self.observe = observe
        self.corral = corral
        self.events = os.path.join(cache, "events.log")
        self.embed_err = os.path.join(cache, "embed-error.log")
        for p in (
            mock.patch.object(observe, "CACHE_DIR", cache),
            mock.patch.object(observe, "EVENTS_LOG", self.events),
            mock.patch.object(observe, "EMBED_ERROR_LOG", self.embed_err),
            mock.patch.object(corral.titles, "CACHE_FILE", os.path.join(cache, "corral.titles.json")),
        ):
            p.start()
            self.addCleanup(p.stop)
        observe.reset_for_tests()
        observe.init(debug=False)

    def test_log_embed_error_writes_events_and_traceback_file(self) -> None:
        try:
            raise RuntimeError("x")
        except RuntimeError as exc:
            self.corral._log_embed_error("抓帧", exc)
        with open(self.events, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(rows[0]["name"], "error")
        self.assertEqual(rows[0]["where"], "抓帧")
        with open(self.embed_err, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("抓帧", body)
        self.assertIn("Traceback", body)


class InstrumentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        cache = self._tmpdir.name
        from corral import observe

        self.observe = observe
        self.events = os.path.join(cache, "events.log")
        for p in (
            mock.patch.object(observe, "CACHE_DIR", cache),
            mock.patch.object(observe, "EVENTS_LOG", self.events),
            mock.patch.object(observe, "EMBED_ERROR_LOG", os.path.join(cache, "embed-error.log")),
        ):
            p.start()
            self.addCleanup(p.stop)
        observe.reset_for_tests()
        observe.init(debug=False)

    def test_scan_all_event_from_store_refresh(self) -> None:
        import corral

        runtime = mock.Mock(id="claude", display_name="Claude")
        runtime.scan_signature.return_value = None
        runtime.scan_sessions.return_value = []
        registry = corral.RuntimeRegistry((runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}), mock.patch.object(
            corral.liveness, "annotate"
        ):
            store = corral.SessionStore(limit=5, registry=registry)
            store.load()
            store.refresh()
        with open(self.events, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        scan_rows = [r for r in rows if r.get("name") == "scan_all"]
        self.assertGreaterEqual(len(scan_rows), 2)
        self.assertIn("duration_ms", scan_rows[0])
        self.assertEqual(scan_rows[0]["session_count"], 0)


if __name__ == "__main__":
    unittest.main()
