"""`corral flex` launches agentflex: installed binary first, else npx, else a clear error."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

from corral import flex_cmd


class FlexCommandTests(unittest.TestCase):
    def test_prefers_installed_binary_and_passes_arguments(self) -> None:
        with mock.patch("shutil.which", side_effect=lambda n: "/usr/local/bin/agentflex" if n == "agentflex" else None):
            self.assertEqual(flex_cmd.command(["--month"]), ["/usr/local/bin/agentflex", "--month"])

    def test_falls_back_to_npx_latest(self) -> None:
        with mock.patch("shutil.which", side_effect=lambda n: "/opt/bin/npx" if n == "npx" else None):
            self.assertEqual(flex_cmd.command(["--png", "w.png"]),
                             ["/opt/bin/npx", "--yes", "agentflex@latest", "--png", "w.png"])

    def test_reports_missing_route(self) -> None:
        err = io.StringIO()
        with mock.patch("shutil.which", return_value=None), redirect_stderr(err):
            self.assertEqual(flex_cmd.main([]), 1)
        self.assertIn("agentflex", err.getvalue())

    def test_launch_failure_is_reported(self) -> None:
        err = io.StringIO()
        with mock.patch.object(flex_cmd, "command", return_value=["/nonexistent/agentflex"]), \
                mock.patch("os.execv", side_effect=OSError(2, "No such file")), \
                mock.patch("subprocess.call", side_effect=OSError(2, "No such file")), redirect_stderr(err):
            self.assertEqual(flex_cmd.main([]), 1)
        self.assertIn("agentflex", err.getvalue())


if __name__ == "__main__":
    unittest.main()
