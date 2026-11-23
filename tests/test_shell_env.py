"""Unit tests for corral.shell_env login PATH probing and directory discovery."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import shell_env


class ShellEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        shell_env.probe_login_shell_path.cache_clear()

    def tearDown(self) -> None:
        shell_env.probe_login_shell_path.cache_clear()

    def test_parse_sentinel_path_ignores_startup_noise_and_picks_last(self) -> None:
        noisy = (
            "Welcome to zsh\n"
            "__CORRAL_PATH_BEGIN__/old/path__CORRAL_PATH_END__\n"
            "loading nvm...\n"
            "__CORRAL_PATH_BEGIN__/custom/bin:/opt/homebrew/bin__CORRAL_PATH_END__\n"
            "done\n"
        )
        self.assertEqual(
            shell_env.parse_sentinel_path(noisy),
            "/custom/bin:/opt/homebrew/bin",
        )
        self.assertIsNone(shell_env.parse_sentinel_path("no markers here"))
        self.assertIsNone(shell_env.parse_sentinel_path("__CORRAL_PATH_BEGIN__   __CORRAL_PATH_END__"))

    def test_probe_command_handles_fish_and_posix_shells(self) -> None:
        self.assertIn("(string join : $PATH)", shell_env.probe_command("/opt/homebrew/bin/fish"))
        self.assertIn('"$PATH"', shell_env.probe_command("/bin/zsh"))

    def test_probe_falls_back_from_interactive_timeout_to_login_shell(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, **_kwargs):
            calls.append(argv)
            if "-i" in argv:
                raise subprocess.TimeoutExpired(argv, 3.0)
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout="__CORRAL_PATH_BEGIN__/home/.local/share/nodejs/current-v22/bin:/usr/bin__CORRAL_PATH_END__",
                stderr="",
            )

        with (
            mock.patch.dict(os.environ, {"CORRAL_SKIP_SHELL_PATH_PROBE": ""}, clear=False),
            mock.patch.object(shell_env.subprocess, "run", side_effect=fake_run),
        ):
            resolved = shell_env.probe_login_shell_path("/bin/zsh")

        self.assertEqual(resolved, "/home/.local/share/nodejs/current-v22/bin:/usr/bin")
        self.assertEqual(len(calls), 2)
        self.assertIn("-i", calls[0])
        self.assertNotIn("-i", calls[1])

    def test_probe_respects_skip_env(self) -> None:
        with (
            mock.patch.dict(os.environ, {"CORRAL_SKIP_SHELL_PATH_PROBE": "1"}, clear=False),
            mock.patch.object(shell_env.subprocess, "run") as run_mock,
        ):
            self.assertEqual(shell_env.probe_login_shell_path("/bin/zsh"), "")
        run_mock.assert_not_called()

    def test_discover_common_bin_dirs_finds_nodejs_nvm_bun_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            node_v22 = home / ".local" / "share" / "nodejs" / "current-v22" / "bin"
            node_v20 = home / ".local" / "share" / "nodejs" / "current-v20" / "bin"
            nvm_v22 = home / ".nvm" / "versions" / "node" / "v22.11.0" / "bin"
            bun_bin = home / ".bun" / "bin"
            claude_bin = home / ".claude" / "local" / "bin"
            for path in (node_v22, node_v20, nvm_v22, bun_bin, claude_bin):
                path.mkdir(parents=True)

            dirs = shell_env.discover_common_bin_dirs(home)
            self.assertIn(str(node_v22), dirs)
            self.assertIn(str(node_v20), dirs)
            self.assertLess(dirs.index(str(node_v22)), dirs.index(str(node_v20)))
            self.assertIn(str(nvm_v22), dirs)
            self.assertIn(str(bun_bin), dirs)
            self.assertIn(str(claude_bin), dirs)
            self.assertNotIn(str(home / ".volta" / "bin"), dirs)
            self.assertIn("/opt/homebrew/bin", dirs)

    def test_resolve_user_path_precedence_and_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.object(shell_env, "probe_login_shell_path", return_value="/shell/bin:/opt/homebrew/bin"):
                # Minimal GUI PATH: probed shell & preserved unit paths take precedence.
                minimal_resolved = shell_env.resolve_user_path(
                    current_path="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                    preserved_path="/preserved/bin:/opt/homebrew/bin",
                    home=home,
                    include_shell=True,
                )
                parts = minimal_resolved.split(":")
                self.assertEqual(parts[:3], ["/shell/bin", "/opt/homebrew/bin", "/preserved/bin"])
                self.assertEqual(len(parts), len(set(parts)))

                # Rich terminal PATH: keep current PATH first.
                rich_resolved = shell_env.resolve_user_path(
                    current_path="/custom/dev/bin:/usr/bin:/bin",
                    preserved_path="/preserved/bin",
                    home=home,
                    include_shell=True,
                )
                self.assertTrue(
                    rich_resolved.startswith(
                        "/custom/dev/bin:/usr/bin:/bin:/shell/bin:/opt/homebrew/bin:/preserved/bin"
                    )
                )


if __name__ == "__main__":
    unittest.main()
