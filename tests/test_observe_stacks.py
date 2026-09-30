"""按需栈转储（observe.install_stack_dumps）：SIGUSR1 → stacks.log。

只给自己进程发信号，不碰真实保活 socket 与用户缓存目录。
"""

from __future__ import annotations

import os
import signal
import tempfile
import time
import unittest
from unittest import mock


@unittest.skipUnless(hasattr(signal, "SIGUSR1"), "needs SIGUSR1")
class StackDumpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        from corral import observe

        self.observe = observe
        self.stacks_log = os.path.join(self._tmpdir.name, "stacks.log")
        self._patchers = [
            mock.patch.object(observe, "STACKS_LOG", self.stacks_log),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)
        observe.reset_for_tests()

    def tearDown(self) -> None:
        self.observe.reset_for_tests()

    def _dump_once(self) -> None:
        os.kill(os.getpid(), signal.SIGUSR1)
        # 信号处理器跑在主线程下一次 bytecode 切换时；轮询等文件落盘。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if os.path.isfile(self.stacks_log) and os.path.getsize(self.stacks_log) > 0:
                return
            time.sleep(0.01)
        self.fail("stacks.log was not written after SIGUSR1")

    def _read(self) -> str:
        with open(self.stacks_log, encoding="utf-8") as fh:
            return fh.read()

    def test_signal_appends_header_and_all_thread_stacks(self) -> None:
        self.assertTrue(self.observe.install_stack_dumps())
        self._dump_once()
        body = self._read()
        self.assertIn(f"pid={os.getpid()}", body)
        self.assertIn("thread", body.lower())
        self.assertIn("File ", body)

    def test_second_dump_appends(self) -> None:
        self.assertTrue(self.observe.install_stack_dumps())
        self._dump_once()
        first_size = os.path.getsize(self.stacks_log)
        os.kill(os.getpid(), signal.SIGUSR1)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if os.path.getsize(self.stacks_log) > first_size:
                break
            time.sleep(0.01)
        self.assertGreater(os.path.getsize(self.stacks_log), first_size)
        self.assertEqual(self._read().count(f"pid={os.getpid()}"), 2)

    def test_install_is_idempotent(self) -> None:
        self.assertTrue(self.observe.install_stack_dumps())
        self.assertTrue(self.observe.install_stack_dumps())
        self._dump_once()
        self.assertEqual(self._read().count(f"pid={os.getpid()}"), 1)

    def test_oversized_log_rotates_one_generation(self) -> None:
        self.assertTrue(self.observe.install_stack_dumps())
        with mock.patch.object(self.observe, "_STACKS_LOG_MAX_BYTES", 100):
            with open(self.stacks_log, "w", encoding="utf-8") as fh:
                fh.write("x" * 200)
            self._dump_once()
        rotated = self.stacks_log + ".1"
        self.assertTrue(os.path.isfile(rotated))
        with open(rotated, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "x" * 200)
        body = self._read()
        self.assertIn(f"pid={os.getpid()}", body)

    def test_reset_restores_previous_handler(self) -> None:
        previous = signal.getsignal(signal.SIGUSR1)
        self.assertTrue(self.observe.install_stack_dumps())
        self.observe.reset_for_tests()
        self.assertEqual(signal.getsignal(signal.SIGUSR1), previous)


if __name__ == "__main__":
    unittest.main()
