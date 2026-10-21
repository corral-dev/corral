"""Remote host restarts onto a replaced install instead of serving from a deleted one."""

from __future__ import annotations

import asyncio
import types
import unittest
from unittest import mock

from corral.remote import cli as remote_cli
from corral.remote import daemon


def _host(ready: bool) -> types.SimpleNamespace:
    return types.SimpleNamespace(restart_requested=False, _new_install_ready=lambda: ready)


async def _watch(host: types.SimpleNamespace, readings: list[bool], ticks: int) -> asyncio.Event:
    stop = asyncio.Event()
    # Never exhaust: Python 3.10 hangs when a thread's StopIteration reaches a Future.
    pending = iter(readings)
    with (
        mock.patch.object(daemon, "_INSTALL_CHECK_INTERVAL", 0.001),
        mock.patch.object(daemon.updater, "install_replaced", side_effect=lambda: next(pending, False)),
    ):
        task = asyncio.create_task(daemon.RemoteDaemon._install_watch_loop(host, stop))
        for _ in range(ticks):
            if stop.is_set():
                break
            await asyncio.sleep(0.005)
        stop.set()
        await task
    return stop


class InstallWatchTests(unittest.TestCase):
    def test_two_consecutive_replaced_readings_request_restart(self) -> None:
        host = _host(ready=True)
        asyncio.run(_watch(host, [True, True], ticks=40))
        self.assertTrue(host.restart_requested)

    def test_single_transient_reading_does_not_restart(self) -> None:
        host = _host(ready=True)
        asyncio.run(_watch(host, [True, False, True, False], ticks=20))
        self.assertFalse(host.restart_requested)

    def test_unimportable_new_install_does_not_restart(self) -> None:
        host = _host(ready=False)
        asyncio.run(_watch(host, [True] * 10, ticks=20))
        self.assertFalse(host.restart_requested)


class ReexecTests(unittest.TestCase):
    def test_foreground_reexecs_stable_serve_argv_after_upgrade(self) -> None:
        fake = mock.Mock(restart_requested=True)
        fake.run = mock.AsyncMock()
        argv = ["/opt/homebrew/opt/corral/libexec/bin/python", "-m", "corral", "remote", "_serve"]
        with (
            mock.patch.object(daemon, "RemoteDaemon", return_value=fake),
            mock.patch("corral.observe.install_stack_dumps"),
            mock.patch.object(remote_cli.remote_autostart, "serve_argv", return_value=argv),
            mock.patch.object(remote_cli.os, "execv") as execv,
        ):
            remote_cli._run_daemon_foreground(mock.Mock())
        execv.assert_called_once_with(argv[0], argv)


class OutdatedHostTests(unittest.TestCase):
    def _run(self, host_version: str | None, *, allowed: bool = True, wanted: bool = True) -> tuple[bool, mock.Mock]:
        snapshot = {} if host_version is None else {"version": host_version}
        with (
            mock.patch.object(remote_cli.remote_autostart, "autostart_allowed", return_value=allowed),
            mock.patch.object(remote_cli.remote_config, "read_pid", return_value=4242),
            mock.patch.object(remote_cli.remote_config, "load_state", return_value=mock.Mock(wanted=wanted)),
            mock.patch.object(remote_cli.remote_config, "read_status_snapshot", return_value=snapshot),
            mock.patch.object(remote_cli.remote_config, "clear_pid"),
            mock.patch.object(remote_cli.remote_autostart, "disable"),
            mock.patch.object(remote_cli.remote_autostart, "enable", return_value=""),
            mock.patch.object(remote_cli, "_stop_pid") as stop,
            mock.patch.object(remote_cli, "_start_background", return_value=(4243, "")),
            mock.patch("corral.__version__", "0.24.296"),
        ):
            return remote_cli.restart_if_outdated(), stop

    def test_older_or_unversioned_host_is_restarted(self) -> None:
        for version in (None, "0.24.295"):
            restarted, stop = self._run(version)
            self.assertTrue(restarted, version)
            stop.assert_called_once_with(4242)

    def test_current_newer_or_isolated_host_is_left_alone(self) -> None:
        for kwargs in ({"host_version": "0.24.296"}, {"host_version": "0.24.297"},
                       {"host_version": None, "allowed": False}, {"host_version": None, "wanted": False}):
            restarted, stop = self._run(**kwargs)
            self.assertFalse(restarted, kwargs)
            stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
