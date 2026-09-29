"""Keepalive server started as an app-class launchd job (macOS)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import keepalive, tmux_server
from corral.legacy_names import SOCKET_NAME


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "CORRAL_ISOLATE_MANAGED_HOSTS"}
    env.pop("CORRAL_KEEPALIVE_LAUNCHD", None)
    return env


class AppliesTests(unittest.TestCase):
    def test_only_product_socket_on_macos(self) -> None:
        with mock.patch.dict(os.environ, _clean_env(), clear=True), \
                mock.patch.object(tmux_server.sys, "platform", "darwin"), \
                mock.patch.object(tmux_server.shutil, "which", return_value="/bin/launchctl"):
            self.assertTrue(tmux_server.applies(SOCKET_NAME))
            self.assertFalse(tmux_server.applies("corral-test-socket"))
        with mock.patch.object(tmux_server.sys, "platform", "linux"):
            self.assertFalse(tmux_server.applies(SOCKET_NAME))

    def test_isolated_tests_and_opt_out_never_touch_launchd(self) -> None:
        for extra in ({"CORRAL_ISOLATE_MANAGED_HOSTS": "1"}, {"CORRAL_KEEPALIVE_LAUNCHD": "0"}):
            env = {**_clean_env(), **extra}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(tmux_server.sys, "platform", "darwin"):
                self.assertFalse(tmux_server.applies(SOCKET_NAME), extra)


class PlistTests(unittest.TestCase):
    def test_plist_is_interactive_foreground_and_holds_no_secrets(self) -> None:
        env = {"PATH": "/usr/bin", "HOME": "/Users/x", "TMUX_TMPDIR": "/tmp",
               "OPENAI_API_KEY": "sk-secret", "HTTPS_PROXY": "http://u:p@h"}
        payload = tmux_server.plist_payload(SOCKET_NAME, "/c.conf", env, "/bin/tmux")
        self.assertEqual(payload["ProcessType"], "Interactive")
        self.assertFalse(payload["KeepAlive"])
        self.assertEqual(
            payload["ProgramArguments"], ["/bin/tmux", "-D", "-L", SOCKET_NAME, "-f", "/c.conf"],
        )
        self.assertEqual(
            payload["EnvironmentVariables"],
            {"PATH": "/usr/bin", "HOME": "/Users/x", "TMUX_TMPDIR": "/tmp"},
        )
        self.assertEqual(payload["Label"], f"com.x0c.corral.keepalive.{SOCKET_NAME}")

    def test_seed_commands_carry_full_env_but_skip_terminal_state(self) -> None:
        args = tmux_server.seed_commands({"A": "1", "TMUX": "x", "TERM": "xterm", "B": "two words"})
        self.assertEqual(
            args, ["set-environment", "-g", "A", "1", ";", "set-environment", "-g", "B", "two words"],
        )
        self.assertEqual(tmux_server.seed_commands({"TMUX": "x"}), [])


class EnsureServerTests(unittest.TestCase):
    def test_running_server_is_left_alone(self) -> None:
        with mock.patch.object(tmux_server, "applies", return_value=True), \
                mock.patch.object(tmux_server, "server_running", return_value=True), \
                mock.patch.object(tmux_server, "_bootstrap") as boot:
            self.assertFalse(tmux_server.ensure_server(SOCKET_NAME, "/c", {}))
        boot.assert_not_called()

    def test_starts_job_then_seeds_env(self) -> None:
        running = iter([False, False, True])
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(tmux_server, "cache_dir", return_value=Path(td)), \
                mock.patch.object(tmux_server, "applies", return_value=True), \
                mock.patch.object(tmux_server, "server_running", side_effect=lambda *_: next(running)), \
                mock.patch.object(tmux_server, "_bootstrap", return_value=True) as boot, \
                mock.patch.object(tmux_server, "_seed_environment") as seed:
            self.assertTrue(tmux_server.ensure_server(SOCKET_NAME, "/c", {"A": "1"}))
        boot.assert_called_once_with(SOCKET_NAME, "/c", {"A": "1"})
        seed.assert_called_once_with(SOCKET_NAME, {"A": "1"})

    def test_failed_bootstrap_falls_back_silently(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(tmux_server, "cache_dir", return_value=Path(td)), \
                mock.patch.object(tmux_server, "applies", return_value=True), \
                mock.patch.object(tmux_server, "server_running", return_value=False), \
                mock.patch.object(tmux_server, "_bootstrap", return_value=False), \
                mock.patch.object(tmux_server, "_seed_environment") as seed:
            self.assertFalse(tmux_server.ensure_server(SOCKET_NAME, "/c", {}))
        seed.assert_not_called()

    def test_never_raises(self) -> None:
        with mock.patch.object(tmux_server, "applies", side_effect=RuntimeError("boom")):
            self.assertFalse(tmux_server.ensure_server(SOCKET_NAME, "/c", {}))

    def test_host_paths_start_server_before_new_session(self) -> None:
        with mock.patch.object(tmux_server, "ensure_server") as ensure:
            keepalive.ensure_server()
        ensure.assert_called_once()
        self.assertEqual(ensure.call_args.args[0], SOCKET_NAME)


class ReportTests(unittest.TestCase):
    def test_clamped_when_priority_below_app_class(self) -> None:
        with mock.patch.object(tmux_server, "server_pid", return_value=42), \
                mock.patch.object(tmux_server, "scheduling_priority", return_value=20), \
                mock.patch.object(tmux_server, "_launchctl", return_value=False), \
                mock.patch.object(tmux_server.sys, "platform", "darwin"):
            report = tmux_server.server_report(SOCKET_NAME, {})
        self.assertEqual(report, {
            "running": True, "pid": 42, "priority": 20, "interactive_job": False, "clamped": True,
        })

    def test_not_running(self) -> None:
        with mock.patch.object(tmux_server, "server_pid", return_value=None):
            self.assertEqual(tmux_server.server_report(SOCKET_NAME, {}), {"running": False})


if __name__ == "__main__":
    unittest.main()
