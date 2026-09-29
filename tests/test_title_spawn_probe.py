"""`_spawn_title_daemon` must not fork an interpreter that will lose the title lock."""

from __future__ import annotations

import fcntl
import os
import tempfile
import unittest
from unittest import mock

from corral import cli as corral_cli


class TitleSpawnProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lock_path = os.path.join(tmp.name, "titles.lock")
        patcher = mock.patch.object(corral_cli, "_TITLE_LOCK_FILE", self.lock_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _hold_exclusive(self) -> int:
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)
        return fd

    def test_held_lock_skips_the_fork(self) -> None:
        self._hold_exclusive()
        self.assertTrue(corral_cli._title_daemon_running())
        with mock.patch.object(corral_cli.subprocess, "Popen") as popen:
            corral_cli._spawn_title_daemon(50)
        popen.assert_not_called()

    def test_free_lock_still_spawns_the_daemon(self) -> None:
        open(self.lock_path, "w").close()
        self.assertFalse(corral_cli._title_daemon_running())
        with mock.patch.object(corral_cli.subprocess, "Popen") as popen:
            corral_cli._spawn_title_daemon(50)
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-3:], ["--generate-titles", "--limit", "50"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_missing_lock_file_spawns_so_the_daemon_can_create_it(self) -> None:
        self.assertFalse(os.path.exists(self.lock_path))
        self.assertFalse(corral_cli._title_daemon_running())
        with mock.patch.object(corral_cli.subprocess, "Popen") as popen:
            corral_cli._spawn_title_daemon(20)
        popen.assert_called_once()
        self.assertFalse(os.path.exists(self.lock_path))  # the probe never creates it

    def test_probe_releases_its_shared_lock(self) -> None:
        open(self.lock_path, "w").close()
        self.assertFalse(corral_cli._title_daemon_running())
        # A real daemon must be able to take the exclusive lock right after a probe.
        fd = os.open(self.lock_path, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_probe_errors_read_as_not_running(self) -> None:
        with mock.patch.object(corral_cli.os, "open", side_effect=PermissionError):
            self.assertFalse(corral_cli._title_daemon_running())

    def test_daemon_keeps_its_own_exclusive_guard(self) -> None:
        # Losing the race between probe and spawn must still exit quietly.
        self._hold_exclusive()
        registry = mock.Mock()
        corral_cli._run_title_daemon(registry, limit=20)
        registry.scan_all.assert_not_called()


if __name__ == "__main__":
    unittest.main()
