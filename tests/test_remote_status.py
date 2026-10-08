"""remote status：中继真实在线状态要进快照与人读输出。"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from corral.i18n import t
from corral.remote import account as remote_account
from corral.remote import cli as remote_cli
from corral.remote import config as remote_config


class RemoteConfigEmptyDirTests(unittest.TestCase):
    def test_load_state_on_empty_dir_does_not_deadlock(self) -> None:
        # load_state 持锁时会再写 identity.key / host.key；普通 Lock 会在空目录死锁。
        previous = os.environ.get("CORRAL_CACHE_DIR")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["CORRAL_CACHE_DIR"] = tmp
            try:
                state = remote_config.load_state()
            finally:
                if previous is None:
                    os.environ.pop("CORRAL_CACHE_DIR", None)
                else:
                    os.environ["CORRAL_CACHE_DIR"] = previous
        self.assertTrue(state.host_id)
        self.assertEqual(len(state.host_id), 26)


class RemoteStatusRelayOnlineTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        isolation = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": fixture.name})
        isolation.start()
        self.addCleanup(isolation.stop)
        # Fixture LAN stub (same boundary as the service/receipt suites):
        # status without snapshot hints resolves the hostname. Relay-online
        # assertions never inspect hint content; LAN coverage is test_remote_lan.
        self._lan_hints = mock.patch(
            "corral.remote.lan.local_hints", return_value=[]
        )
        self._lan_hints.start()
        self.addCleanup(self._lan_hints.stop)

    def test_login_on_single_tenant_relay_does_not_request_device_code(self) -> None:
        with mock.patch.object(remote_account, "_request") as request:
            ok, message = remote_account.login("wss://relay.example.com")

        self.assertTrue(ok)
        self.assertEqual(message, t("remote.login.not_needed"))
        request.assert_not_called()

    def test_start_automatically_installs_missing_components(self) -> None:
        installed = []

        def missing() -> list[str]:
            return [] if installed else ["cryptography", "websockets", "segno"]

        def install(*_args, **_kwargs):
            installed.append(True)
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(remote_cli, "_missing_dependencies", side_effect=missing),
            mock.patch.object(remote_cli.updater, "detect_channel", return_value="pip"),
            mock.patch.object(remote_cli.subprocess, "run", side_effect=install) as run,
        ):
            self.assertEqual(remote_cli._ensure_dependencies(), "")

        command = run.call_args.args[0]
        self.assertEqual(
            command[:6],
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input"],
        )
        self.assertEqual(command[6:], ["cryptography", "websockets", "segno"])

    def test_start_uses_pipx_injection_for_pipx_install(self) -> None:
        installed = []

        def missing() -> list[str]:
            return [] if installed else ["cryptography", "websockets", "segno"]

        def install(*_args, **_kwargs):
            installed.append(True)
            return mock.Mock(returncode=0)

        with (
            mock.patch.object(remote_cli, "_missing_dependencies", side_effect=missing),
            mock.patch.object(remote_cli.updater, "detect_channel", return_value="pipx"),
            mock.patch.object(remote_cli.subprocess, "run", side_effect=install) as run,
        ):
            self.assertEqual(remote_cli._ensure_dependencies(), "")

        self.assertEqual(
            run.call_args.args[0],
            ["pipx", "inject", "corral", "cryptography", "websockets", "segno"],
        )

    def test_start_reports_when_automatic_install_does_not_fix_environment(self) -> None:
        with (
            mock.patch.object(remote_cli, "_missing_dependencies", return_value=["cryptography"]),
            mock.patch.object(remote_cli.updater, "detect_channel", return_value="pip"),
            mock.patch.object(remote_cli.subprocess, "run", return_value=mock.Mock(returncode=1)),
        ):
            self.assertEqual(remote_cli._ensure_dependencies(), t("remote.deps.auto_install_failed"))

    def test_on_is_idempotent_when_already_running(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1", host_name="suzhou", relay_url="", relay_enabled=False,
            local_enabled=False, local_port=8737, devices=[]
        )
        args = mock.Mock(
            relay_url=None, insecure_relay=False, no_relay=False, no_local=False,
            port=None, force=False, foreground=False, quiet=False, json=False,
        )
        with (
            mock.patch.object(remote_cli, "_ensure_dependencies", return_value=""),
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state"),
            mock.patch.object(remote_config, "read_pid", return_value=12345),
            mock.patch.object(remote_cli, "_spawn_background_daemon") as spawn,
            mock.patch.object(remote_config, "write_pairing") as write_pairing,
            mock.patch.object(remote_cli.remote_autostart, "enable", return_value="") as enable,
            mock.patch.object(remote_cli.remote_autostart, "is_installed", return_value=True),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(remote_cli._cmd_on(args), 0)

        spawn.assert_not_called()
        write_pairing.assert_not_called()
        enable.assert_called_once()
        self.assertTrue(state.wanted)
        self.assertIn(t("remote.on.already", pid=12345), buf.getvalue())
        self.assertIn(t("remote.on.remembered"), buf.getvalue())

    def test_on_spawns_background_daemon_without_pairing(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1",
            host_name="suzhou",
            relay_url="",
            relay_enabled=False,
            local_enabled=False,
            local_port=8737,
            devices=[],
        )
        args = mock.Mock(
            relay_url=None,
            insecure_relay=False,
            no_relay=False,
            no_local=False,
            port=None,
            force=False,
            foreground=False,
            quiet=False,
            json=False,
        )
        child = mock.Mock()
        child.poll.return_value = None
        with (
            mock.patch.object(remote_cli, "_ensure_dependencies", return_value=""),
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state"),
            mock.patch.object(remote_config, "read_pid", side_effect=[None, None, 4242]),
            mock.patch.object(remote_cli, "_spawn_background_daemon", return_value=child) as spawn,
            mock.patch.object(remote_cli, "_print_pairing") as print_pairing,
            mock.patch.object(remote_cli, "_run_daemon_foreground") as foreground,
            mock.patch.object(remote_cli.remote_autostart, "enable", return_value="no launchd"),
            mock.patch.object(remote_cli.remote_autostart, "is_installed", return_value=False),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(remote_cli._cmd_on(args), 0)

        spawn.assert_called_once()
        foreground.assert_not_called()
        print_pairing.assert_not_called()
        self.assertTrue(state.wanted)
        self.assertIn(t("remote.on.ready", name="suzhou", pid=4242), buf.getvalue())
        self.assertIn(t("remote.on.pair_hint"), buf.getvalue())
        self.assertIn(t("remote.on.remembered"), buf.getvalue())

    def test_start_alias_matches_on(self) -> None:
        self.assertIs(remote_cli._cmd_start, remote_cli._cmd_on)
        self.assertIs(remote_cli._cmd_stop, remote_cli._cmd_off)

    def test_off_is_idempotent_when_already_off(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1", host_name="suzhou", relay_url="", relay_enabled=False,
            local_enabled=False, local_port=8737, wanted=False, devices=[],
        )
        args = mock.Mock(json=False, quiet=False)
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state"),
            mock.patch.object(remote_config, "read_pid", return_value=None),
            mock.patch.object(remote_cli.remote_autostart, "disable", return_value="") as disable,
            mock.patch.object(remote_cli.remote_autostart, "is_installed", return_value=False),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(remote_cli._cmd_off(args), 0)
        disable.assert_called_once()
        self.assertFalse(state.wanted)
        self.assertIn(t("remote.off.already"), buf.getvalue())

    def test_off_disarms_autostart_before_stopping(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1", host_name="suzhou", relay_url="", relay_enabled=False,
            local_enabled=False, local_port=8737, wanted=True, devices=[],
        )
        args = mock.Mock(json=False, quiet=False)
        order: list[str] = []

        def disable() -> str:
            order.append("disable")
            return ""

        def kill(pid: int, sig: int) -> None:
            if sig == 0:
                raise OSError("gone")
            order.append("kill")

        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state"),
            mock.patch.object(remote_config, "read_pid", return_value=777),
            mock.patch.object(remote_config, "clear_pid"),
            mock.patch.object(remote_cli.remote_autostart, "disable", side_effect=disable),
            mock.patch.object(remote_cli.remote_autostart, "is_installed", return_value=False),
            mock.patch.object(remote_cli.os, "kill", side_effect=kill),
        ):
            self.assertEqual(remote_cli._cmd_off(args), 0)
        self.assertEqual(order, ["disable", "kill"])
        self.assertFalse(state.wanted)

    def test_pair_still_prints_qr_when_service_is_on(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1", host_name="suzhou", relay_url="", relay_enabled=False,
            local_enabled=False, local_port=8737, devices=[]
        )
        args = mock.Mock(readonly=False, json=False)
        with (
            mock.patch.object(remote_cli, "_ensure_dependencies", return_value=""),
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state") as save,
            mock.patch.object(remote_cli.remote_autostart, "enable", return_value="") as enable,
            mock.patch.object(remote_config, "load_or_create_identity", return_value=object()),
            mock.patch.object(remote_config, "write_pairing") as write_pairing,
            mock.patch.object(remote_config, "read_pid", return_value=99),
            mock.patch.object(remote_cli.crypto, "public_key_bytes", return_value=b"public-key"),
            mock.patch.object(remote_cli.crypto, "new_pairing_code", return_value="新配对码"),
            mock.patch.object(remote_cli, "_print_pairing") as print_pairing,
        ):
            self.assertEqual(remote_cli._cmd_pair(args), 0)

        write_pairing.assert_called_once_with("新配对码", remote_cli._PAIRING_TTL, mode="full")
        save.assert_called_once_with(state)
        enable.assert_called_once_with(restart_existing=False)
        print_pairing.assert_called_once_with(
            state, "新配对码", b"public-key", 8737, mode="full"
        )

    def test_status_text_shows_relay_online_from_snapshot(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1",
            host_name="suzhou",
            relay_url="wss://relay.example.com",
            relay_enabled=True,
            local_enabled=True,
            local_port=8737,
            devices=[],
        )
        snapshot = {
            "updated_at": 1_700_000_000.0,
            "online": [],
            "recent": [],
            "relay_online": True,
            "relay_connected_at": 1_700_000_100.0,
            "relay_error": "",
        }
        args = mock.Mock(json=False)
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "read_pid", return_value=12345),
            mock.patch.object(remote_config, "read_pairing", return_value=None),
            mock.patch.object(remote_config, "read_status_snapshot", return_value=snapshot),
            mock.patch.object(remote_cli, "_check_dependencies", return_value=None),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_status(args)
        self.assertEqual(code, 0)
        text = buf.getvalue()
        since = t(
            "remote.status.relay_since",
            time=time.strftime("%H:%M:%S", time.localtime(1_700_000_100.0)),
        )
        self.assertIn(
            t(
                "remote.status.relay_online",
                label="wss://relay.example.com",
                since=since,
            ),
            text,
        )
        self.assertIn("relay.example.com", text)

    def test_status_json_includes_relay_online_fields(self) -> None:
        state = remote_config.RemoteState(
            host_id="h1",
            host_name="suzhou",
            relay_url="wss://relay.example.com",
            relay_enabled=True,
            local_enabled=True,
            local_port=8737,
            devices=[],
        )
        snapshot = {
            "relay_online": False,
            "relay_connected_at": None,
            "relay_error": "连接被重置",
            "online": [],
            "recent": [],
        }
        args = mock.Mock(json=True)
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "read_pid", return_value=99),
            mock.patch.object(remote_config, "read_pairing", return_value=None),
            mock.patch.object(remote_config, "read_status_snapshot", return_value=snapshot),
            mock.patch.object(remote_cli, "_check_dependencies", return_value=None),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_status(args)
        self.assertEqual(code, 0)
        self.assertIn('"relay_online": false', buf.getvalue())
        self.assertIn("连接被重置", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
