import sys
import unittest
from unittest import mock

from corral import bootstrap, shim


class BootstrapShimTests(unittest.TestCase):
    def test_interactive_startup_does_not_auto_install_shim(self):
        with (
            mock.patch.object(sys, "argv", ["corral"]),
            mock.patch.object(sys.stdin, "isatty", return_value=True),
            mock.patch.object(sys.stdout, "isatty", return_value=True),
            mock.patch.object(shim, "install") as install,
            mock.patch.object(bootstrap, "_migrate_pi_history") as migrate,
            mock.patch("corral.cli.main") as cli_main,
        ):
            bootstrap.main()

        install.assert_not_called()
        migrate.assert_called_once_with()
        cli_main.assert_called_once_with()

    def test_non_interactive_startup_never_writes_shell_configuration(self):
        with (
            mock.patch.object(sys, "argv", ["corral"]),
            mock.patch.object(sys.stdin, "isatty", return_value=False),
            mock.patch.object(sys.stdout, "isatty", return_value=False),
            mock.patch.object(shim, "install") as install,
            mock.patch.object(bootstrap, "_migrate_pi_history") as migrate,
            mock.patch("corral.cli.main") as cli_main,
        ):
            bootstrap.main()

        install.assert_not_called()
        migrate.assert_not_called()
        cli_main.assert_called_once_with()
