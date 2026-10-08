"""`corral remote` 的 Agent 友好契约：envelope 形状、退出码分层、--dry-run 只读性。"""

from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest import mock

from corral.remote import cli as remote_cli
from corral.remote import config as remote_config


def _state(**overrides):
    base = dict(host_id="h1", host_name="dev", relay_url="", relay_enabled=False,
                local_enabled=False, local_port=8737, devices=[])
    base.update(overrides)
    return remote_config.RemoteState(**base)


def _on_args(**overrides):
    base = dict(relay_url=None, insecure_relay=False, no_relay=False, no_local=False,
                port=None, force=False, foreground=False, quiet=True, json=True,
                dry_run=False)
    base.update(overrides)
    return argparse.Namespace(**base)


class RemoteEnvelopeTests(unittest.TestCase):
    def test_failure_envelope_carries_code_hint_next_and_meta(self) -> None:
        text = remote_cli._envelope(False, code="not_found", message="没有",
                                    hint="去看 devices",
                                    next_commands=["corral remote devices --json"])
        payload = json.loads(text)
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["data"])
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertEqual(payload["error"]["message"], "没有")
        self.assertEqual(payload["error"]["hint"], "去看 devices")
        self.assertEqual(payload["error"]["next_commands"], ["corral remote devices --json"])
        self.assertEqual(payload["meta"]["version"], remote_cli.REMOTE_API_VERSION)

    def test_success_envelope_keeps_shape(self) -> None:
        payload = json.loads(remote_cli._envelope(True, {"a": 1}))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"], {"a": 1})
        self.assertIsNone(payload["error"])
        self.assertIn("version", payload["meta"])

    def test_unpair_missing_device_is_not_found_exit_3(self) -> None:
        state = _state()
        args = argparse.Namespace(device_id="ghost", json=True, dry_run=False)
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "find_device_by_id", return_value=None),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_unpair(args)
        self.assertEqual(code, remote_cli.EXIT_NOT_FOUND)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "not_found")
        self.assertTrue(payload["error"]["next_commands"])

    def test_rename_blank_is_usage_error_with_code(self) -> None:
        state = _state()
        args = argparse.Namespace(name="   ", clear=False, json=True, dry_run=False)
        with mock.patch.object(remote_config, "load_state", return_value=state):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_rename(args)
        self.assertEqual(code, remote_cli.EXIT_USAGE)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["error"]["code"], "usage_error")

    def test_bin_returns_usable_identity(self) -> None:
        self.assertTrue(remote_cli._bin())


class RemoteDryRunTests(unittest.TestCase):
    def test_on_dry_run_writes_nothing(self) -> None:
        state = _state()
        args = _on_args(dry_run=True)
        with (
            mock.patch.object(remote_cli, "_missing_dependencies", return_value=[]),
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state") as save,
            mock.patch.object(remote_config, "read_pid", return_value=None),
            mock.patch.object(remote_cli, "_spawn_background_daemon") as spawn,
            mock.patch.object(remote_cli.remote_autostart, "enable") as enable,
            mock.patch.object(remote_cli.remote_autostart, "is_installed", return_value=False),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_on(args)
        self.assertEqual(code, 0)
        save.assert_not_called()
        spawn.assert_not_called()
        enable.assert_not_called()
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["data"]["dry_run"])
        self.assertFalse(payload["data"]["changed"])
        self.assertEqual(payload["data"]["would"], "start")

    def test_on_dry_run_reports_already_running(self) -> None:
        state = _state()
        args = _on_args(dry_run=True)
        with (
            mock.patch.object(remote_cli, "_missing_dependencies", return_value=[]),
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state") as save,
            mock.patch.object(remote_config, "read_pid", return_value=4242),
            mock.patch.object(remote_cli.remote_autostart, "is_installed", return_value=True),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_on(args)
        self.assertEqual(code, 0)
        save.assert_not_called()
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["data"]["would"], "already_running")
        self.assertEqual(payload["data"]["pid"], 4242)

    def test_off_dry_run_kills_nothing(self) -> None:
        state = _state(wanted=True)
        args = argparse.Namespace(json=True, quiet=True, dry_run=True)
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state") as save,
            mock.patch.object(remote_config, "read_pid", return_value=777),
            mock.patch.object(remote_cli.remote_autostart, "disable") as disable,
            mock.patch.object(remote_cli.remote_autostart, "is_installed", return_value=False),
            mock.patch.object(remote_cli.os, "kill") as kill,
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_off(args)
        self.assertEqual(code, 0)
        save.assert_not_called()
        disable.assert_not_called()
        kill.assert_not_called()
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["data"]["dry_run"])
        self.assertFalse(payload["data"]["changed"])
        self.assertEqual(payload["data"]["would"], "stop")

    def test_pair_dry_run_mints_nothing(self) -> None:
        state = _state()
        args = argparse.Namespace(readonly=False, json=True, dry_run=True)
        with (
            mock.patch.object(remote_cli, "_check_dependencies", return_value=""),
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "load_or_create_identity") as ident,
            mock.patch.object(remote_config, "write_pairing") as write_pairing,
            mock.patch.object(remote_config, "read_pid", return_value=None),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_pair(args)
        self.assertEqual(code, 0)
        ident.assert_not_called()
        write_pairing.assert_not_called()
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["data"]["dry_run"])
        self.assertNotIn("code", payload["data"])  # 演练不给假码
        self.assertTrue(payload["data"]["would_start_service"])

    def _run_pair(self, state, *, pid_reads, spawn_ok=True, readonly=False, relay_error=None):
        args = argparse.Namespace(readonly=readonly, json=True, dry_run=False)
        with (
            mock.patch.object(remote_cli, "_ensure_dependencies", return_value=""),
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state") as save,
            mock.patch.object(remote_config, "load_or_create_identity", return_value=b"k"),
            mock.patch.object(remote_cli.crypto, "public_key_bytes", return_value=b"\x01" * 32),
            mock.patch.object(remote_config, "write_pairing") as write_pairing,
            mock.patch.object(remote_config, "read_pid", side_effect=pid_reads),
            mock.patch.object(remote_cli, "_wait_until_running",
                              side_effect=[None, 5151 if spawn_ok else None]),
            mock.patch.object(remote_cli, "_spawn_background_daemon") as spawn,
            mock.patch.object(remote_cli.remote_autostart, "enable", return_value=None) as enable,
            mock.patch.object(remote_cli, "_register_with_relay", return_value=relay_error),
        ):
            spawn.return_value.poll.return_value = None if spawn_ok else 1
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_pair(args)
        return code, json.loads(buf.getvalue()), save, write_pairing, spawn, enable

    def test_pair_turns_remote_on_when_off(self) -> None:
        state = _state(wanted=False)
        code, payload, save, write_pairing, spawn, enable = self._run_pair(state, pid_reads=[None])
        self.assertEqual(code, 0)
        self.assertTrue(state.wanted)
        save.assert_called()
        enable.assert_called_once()
        spawn.assert_called_once()
        write_pairing.assert_called_once()
        self.assertTrue(payload["data"]["service_started"])
        self.assertEqual(payload["data"]["service_pid"], 5151)
        self.assertIn("code", payload["data"])

    def test_pair_keeps_running_service(self) -> None:
        state = _state(wanted=True)
        code, payload, _save, write_pairing, spawn, enable = self._run_pair(state, pid_reads=[4242])
        self.assertEqual(code, 0)
        spawn.assert_not_called()
        enable.assert_called_once_with(restart_existing=False)
        write_pairing.assert_called_once()
        self.assertFalse(payload["data"]["service_started"])
        self.assertEqual(payload["data"]["service_pid"], 4242)

    def test_pair_mints_no_code_when_service_fails(self) -> None:
        state = _state(wanted=False)
        code, payload, _save, write_pairing, _spawn, _enable = self._run_pair(
            state, pid_reads=[None], spawn_ok=False)
        self.assertNotEqual(code, 0)
        self.assertFalse(payload["ok"])
        write_pairing.assert_not_called()

    def test_pair_mints_no_code_when_relay_registration_fails(self) -> None:
        code, payload, _save, write_pairing, spawn, enable = self._run_pair(
            _state(relay_enabled=True, relay_url="wss://relay.example.com"),
            pid_reads=[None], relay_error="registration failed")
        self.assertNotEqual(code, 0)
        self.assertEqual(payload["error"]["code"], "account_error")
        write_pairing.assert_not_called()
        spawn.assert_not_called()
        enable.assert_not_called()

    def test_readonly_pair_starts_service_with_restricted_pairing(self) -> None:
        code, payload, _save, write_pairing, spawn, _enable = self._run_pair(
            _state(), pid_reads=[None], readonly=True)
        self.assertEqual(code, 0)
        spawn.assert_called_once()
        self.assertEqual(write_pairing.call_args.kwargs["mode"], "readonly")
        self.assertEqual(payload["data"]["access"], "readonly")

    def test_unpair_dry_run_removes_nothing(self) -> None:
        from types import SimpleNamespace

        state = _state()
        device = SimpleNamespace(id="d1", name="iPhone")
        args = argparse.Namespace(device_id="d1", json=True, dry_run=True)
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "find_device_by_id", return_value=device),
            mock.patch.object(remote_config, "remove_device") as remove,
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_unpair(args)
        self.assertEqual(code, 0)
        remove.assert_not_called()
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["data"]["removed"])
        self.assertTrue(payload["data"]["dry_run"])

    def test_rotate_dry_run_keeps_key(self) -> None:
        state = _state()
        args = argparse.Namespace(json=True, dry_run=True)
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "rotate_host_key") as rotate,
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_rotate_key(args)
        self.assertEqual(code, 0)
        rotate.assert_not_called()
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["data"]["rotated"])
        self.assertTrue(payload["data"]["dry_run"])

    def test_rename_dry_run_saves_nothing(self) -> None:
        state = _state(host_name="old")
        args = argparse.Namespace(name="new", clear=False, json=True, dry_run=True)
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state") as save,
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_rename(args)
        self.assertEqual(code, 0)
        save.assert_not_called()
        self.assertEqual(state.host_name, "old")
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["data"]["host_name"], "new")
        self.assertTrue(payload["data"]["dry_run"])

    def test_rename_reports_changed_flag(self) -> None:
        state = _state(host_name="same")
        args = argparse.Namespace(name="same", clear=False, json=True, dry_run=False)
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "save_state"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_rename(args)
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(buf.getvalue())["data"]["changed"])

    def test_parser_wires_dry_run_for_write_commands(self) -> None:
        parser = remote_cli.build_parser()
        for argv in (["on", "--dry-run"], ["off", "--dry-run"], ["pair", "--dry-run"],
                     ["unpair", "d1", "--dry-run"], ["rotate-key", "--dry-run"],
                     ["rename", "--dry-run"], ["rename", "x", "--dry-run"]):
            self.assertTrue(parser.parse_args(argv).dry_run, argv)
        # 只读命令没有 --dry-run（鉴权引导与查询免 rehearsal）
        for argv in (["status"], ["devices"], ["login"], ["logout"], ["whoami"]):
            self.assertFalse(hasattr(parser.parse_args(argv), "dry_run"), argv)


if __name__ == "__main__":
    unittest.main()
