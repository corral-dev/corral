"""corral.cli._restart_process：客户端自动更新重启后用新代码 re-exec 自身。

os.execv 会立即替换当前进程、不会返回，测试里必须 mock 掉，否则会杀死测试进程。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock

from corral import cli, updater


class RestartProcessTests(unittest.TestCase):
    def test_reexecs_via_path_corral_when_available(self) -> None:
        # brew 升级后 PATH 上的 corral 软链已指向新版本，优先用它重启
        with mock.patch.object(cli.sys, "argv", ["corral", "--limit", "5"]), \
             mock.patch("shutil.which", return_value="/opt/homebrew/bin/corral"), \
             mock.patch.object(cli.os, "execv") as execv:
            cli._restart_process()
        execv.assert_called_once_with(
            "/opt/homebrew/bin/corral", ["/opt/homebrew/bin/corral", "--limit", "5"],
        )

    def test_falls_back_to_interpreter_module_when_no_path_binary(self) -> None:
        with mock.patch.object(cli.sys, "argv", ["corral"]), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch.object(cli.os, "execv") as execv:
            cli._restart_process()
        execv.assert_called_once_with(sys.executable, [sys.executable, "-m", "corral"])


class FinishSelfUpdateTests(unittest.TestCase):
    def test_successful_update_restarts_only_after_installation_returns(self) -> None:
        request = updater.RestartRequest("9.9.9", "brew")
        order: list[str] = []

        def run_update(latest, channel, spec=None):
            self.assertEqual((latest, channel), ("9.9.9", "brew"))
            order.append("更新")
            return True, "ok"

        with mock.patch.object(cli.updater, "run_update", side_effect=run_update), \
             mock.patch.object(cli, "_restart_process", side_effect=lambda: order.append("重启")), \
             mock.patch.object(cli.observe, "event"):
            self.assertTrue(cli._finish_self_update(request))
        self.assertEqual(order, ["更新", "重启"])

    def test_failed_update_does_not_restart(self) -> None:
        request = updater.RestartRequest("9.9.9", "pip")
        with mock.patch.object(cli.updater, "run_update", return_value=(False, "失败原因")), \
             mock.patch.object(cli, "_restart_process") as restart, \
             mock.patch.object(cli.observe, "event"), \
             mock.patch.object(cli.observe, "debug"):
            self.assertFalse(cli._finish_self_update(request))
        restart.assert_not_called()


if __name__ == "__main__":
    unittest.main()
