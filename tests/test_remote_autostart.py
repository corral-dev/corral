"""Remote switch autostart (LaunchAgent / systemd) unit tests."""

from __future__ import annotations

import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral.remote import autostart


def _ok() -> mock.Mock:
    return mock.Mock(returncode=0, stdout="", stderr="")


def _fail() -> mock.Mock:
    return mock.Mock(returncode=1, stdout="", stderr="unloaded")


def _fast_run(args: list[str]) -> mock.Mock:
    """Healthy launchd: unload already visible, everything else succeeds."""
    if args[1] == "print":
        return _fail()
    return _ok()


class RemoteAutostartTests(unittest.TestCase):
    def test_autostart_skipped_under_cache_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": tmp}, clear=False):
                self.assertFalse(autostart.autostart_allowed())
                self.assertEqual(autostart.enable(), "")
                self.assertFalse(autostart.is_installed())

    def test_darwin_plist_contains_serve_argv_and_keepalive(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS only")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plist_path = home / "Library" / "LaunchAgents" / "com.x0c.corral.remote.plist"
            with (
                mock.patch.object(autostart, "autostart_allowed", return_value=True),
                mock.patch.object(autostart, "_home", return_value=home),
                mock.patch.object(autostart, "_darwin_run", side_effect=_fast_run),
            ):
                self.assertEqual(autostart.enable(), "")
            self.assertTrue(plist_path.is_file())
            with plist_path.open("rb") as fh:
                payload = plistlib.load(fh)
            self.assertEqual(payload["Label"], "com.x0c.corral.remote")
            self.assertTrue(payload["RunAtLoad"])
            self.assertTrue(payload["KeepAlive"])
            self.assertEqual(payload["ProcessType"], "Interactive")
            self.assertEqual(payload["ProgramArguments"][:3], [sys.executable, "-m", "corral"])
            self.assertEqual(payload["ProgramArguments"][-2:], ["remote", "_serve"])

    def test_stale_plist_without_process_type_reports_not_installed(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS only")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plist_path = home / "Library" / "LaunchAgents" / "com.x0c.corral.remote.plist"
            plist_path.parent.mkdir(parents=True)
            with plist_path.open("wb") as fh:
                plistlib.dump({"Label": "com.x0c.corral.remote", "KeepAlive": True}, fh)
            with (
                mock.patch.object(autostart, "autostart_allowed", return_value=True),
                mock.patch.object(autostart, "_home", return_value=home),
            ):
                self.assertTrue(plist_path.is_file())
                self.assertFalse(autostart.is_installed())

    def test_fresh_plist_reports_installed(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS only")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with (
                mock.patch.object(autostart, "autostart_allowed", return_value=True),
                mock.patch.object(autostart, "_home", return_value=home),
                mock.patch.object(autostart, "_darwin_run", side_effect=_fast_run),
            ):
                self.assertEqual(autostart.enable(), "")
                self.assertTrue(autostart.is_installed())

    def test_unreadable_plist_keeps_old_answer(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS only")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plist_path = home / "Library" / "LaunchAgents" / "com.x0c.corral.remote.plist"
            plist_path.parent.mkdir(parents=True)
            plist_path.write_bytes(b"not a plist")
            with (
                mock.patch.object(autostart, "autostart_allowed", return_value=True),
                mock.patch.object(autostart, "_home", return_value=home),
            ):
                self.assertTrue(autostart.is_installed())

    def test_enable_retries_bootstrap_once_after_bootout_teardown(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS only")
        bootstraps: list[str] = []

        def fake_run(args: list[str]) -> mock.Mock:
            if args[1] == "print":
                return _fail()
            if args[1] == "bootstrap":
                bootstraps.append(args[2])
                return _fail() if len(bootstraps) == 1 else _ok()
            return _ok()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with (
                mock.patch.object(autostart, "autostart_allowed", return_value=True),
                mock.patch.object(autostart, "_home", return_value=home),
                mock.patch.object(autostart, "_darwin_run", side_effect=fake_run),
                mock.patch("time.sleep") as asleep,
            ):
                self.assertEqual(autostart.enable(), "")
            self.assertEqual(len(bootstraps), 2)
            asleep.assert_called_once_with(2.0)

    def test_enable_falls_back_to_load_when_bootstrap_keeps_failing(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS only")
        seen: list[str] = []

        def fake_run(args: list[str]) -> mock.Mock:
            seen.append(args[1])
            if args[1] in {"print", "bootstrap"}:
                return _fail()
            return _ok()

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with (
                mock.patch.object(autostart, "autostart_allowed", return_value=True),
                mock.patch.object(autostart, "_home", return_value=home),
                mock.patch.object(autostart, "_darwin_run", side_effect=fake_run),
                mock.patch("time.sleep"),
            ):
                self.assertEqual(autostart.enable(), "")
            self.assertIn("load", seen)

    def test_darwin_disable_removes_plist(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS only")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            plist_path = home / "Library" / "LaunchAgents" / "com.x0c.corral.remote.plist"
            plist_path.parent.mkdir(parents=True)
            plist_path.write_bytes(b"placeholder")
            with (
                mock.patch.object(autostart, "autostart_allowed", return_value=True),
                mock.patch.object(autostart, "_home", return_value=home),
                mock.patch.object(autostart, "_darwin_run", side_effect=_fast_run),
            ):
                self.assertEqual(autostart.disable(), "")
            self.assertFalse(plist_path.exists())


if __name__ == "__main__":
    unittest.main()
