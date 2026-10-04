"""局域网直连修复（CLI/开发机侧）：lan 过滤 / 有效端口 / 配对 l= / hello / mDNS 降级。"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout
from unittest import mock

from corral.remote import cli as remote_cli
from corral.remote import config as remote_config
from corral.remote import lan as lan_module
from corral.remote import pairing, protocol, ratelimit
from corral.remote.lan import DEFAULT_LOCAL_PORT, effective_local_port, lan_addresses
from corral.remote.mdns import MdnsAdvertiser, service_name
from corral.remote.mdns import available as mdns_available
from corral.remote.service import Connection, RemoteService


class _FakeSocketModule:
    """替代 lan.socket：探针返回脏地址，hostname 返回混合地址。"""

    AF_INET = 2
    SOCK_DGRAM = 2

    def __init__(self, probe_ip: str, host_addrs: list[str]) -> None:
        self._probe_ip = probe_ip
        self._host_addrs = list(host_addrs)

    def socket(self, *args, **kwargs):
        probe_ip = self._probe_ip

        class _Probe:
            def connect(self, _addr) -> None:
                pass

            def getsockname(self):
                return (probe_ip, 51234)

            def close(self) -> None:
                pass

        return _Probe()

    def gethostname(self) -> str:
        return "macbook-pro.local"

    def getaddrinfo(self, _host, _port, *args, **kwargs):
        return [(2, 1, 6, "", (addr, 0)) for addr in self._host_addrs]


class _HelloHub:
    """hello 只需要 runtimes；其余一律不碰。"""

    _on_event = None

    def runtimes(self):
        return []


class LanAddressTests(unittest.TestCase):
    def test_vpn_and_fake_ip_probe_results_are_dropped(self):
        """UDP 探针在有透明代理时返回 198.18.0.1：绝不能写进配对二维码。"""
        fake = _FakeSocketModule(
            "198.18.0.1",
            ["127.0.0.1", "192.168.110.11", "198.18.0.1", "169.254.5.6"],
        )
        with mock.patch.object(lan_module, "socket", fake):
            addresses = lan_addresses()
        self.assertEqual(addresses, ["192.168.110.11"])

    def test_real_lan_sorts_before_overlay_networks(self):
        """真局域网在前；Tailscale 这类私有组网保留在后，不断连。"""
        fake = _FakeSocketModule("100.96.0.2", ["192.168.1.5"])
        with mock.patch.object(lan_module, "socket", fake):
            addresses = lan_addresses()
        self.assertEqual(addresses, ["192.168.1.5", "100.96.0.2"])

    def test_effective_local_port(self):
        zero = remote_config.RemoteState(host_id="h", host_name="dev", local_port=0)
        self.assertEqual(effective_local_port(zero), 8737)
        self.assertEqual(effective_local_port(zero), DEFAULT_LOCAL_PORT)
        custom = remote_config.RemoteState(host_id="h", host_name="dev", local_port=9999)
        self.assertEqual(effective_local_port(custom), 9999)


class PairingPayloadTests(unittest.TestCase):
    def _state(self, **overrides) -> remote_config.RemoteState:
        base = {
            "host_id": "h1",
            "host_name": "dev",
            "relay_url": "",
            "relay_enabled": False,
            "local_enabled": True,
            "local_port": 0,
            "devices": [],
        }
        base.update(overrides)
        return remote_config.RemoteState(**base)

    def test_build_payload_includes_l_when_port_is_zero(self):
        """remote.json 的 local_port 恒为 0：配对仍要带 l=，不能丢局域网地址。"""
        state = self._state()
        with mock.patch(
            "corral.remote.pairing.local_hints", return_value=["192.168.1.5:8737"]
        ):
            url = pairing.build_payload(state, "CODE123", b"\x01" * 32, 0)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query.get("l"), ["192.168.1.5:8737"])

    def test_build_payload_omits_l_when_local_disabled(self):
        state = self._state(local_enabled=False)
        with mock.patch(
            "corral.remote.pairing.local_hints", return_value=["192.168.1.5:8737"]
        ):
            url = pairing.build_payload(state, "CODE123", b"\x01" * 32, 0)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertNotIn("l", query)

    def test_as_json_reports_effective_port(self):
        state = self._state()
        with mock.patch(
            "corral.remote.pairing.local_hints", return_value=["192.168.1.5:8737"]
        ):
            payload = json.loads(pairing.as_json(state, "CODE123", b"\x01" * 32, 0))
        self.assertEqual(payload["local_port"], 8737)
        self.assertIn("l=192.168.1.5%3A8737", payload["url"])


class HelloLocalHintsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cache = os.environ.get("CORRAL_CACHE_DIR")
        os.environ["CORRAL_CACHE_DIR"] = self._tmp.name
        self.addCleanup(self._restore_cache)
        for limiter in (
            ratelimit.PAIR_ATTEMPTS,
            ratelimit.PAIR_ATTEMPTS_HOURLY,
            ratelimit.INPUT_ACTIONS,
            ratelimit.TERMINAL_TYPING,
            ratelimit.SESSION_CREATE,
            ratelimit.PUSH_REGISTER,
        ):
            limiter.reset()
        self.service = RemoteService(_HelloHub())  # type: ignore[arg-type]
        self.sent: list[dict] = []

    def _restore_cache(self) -> None:
        if self._old_cache is None:
            os.environ.pop("CORRAL_CACHE_DIR", None)
        else:
            os.environ["CORRAL_CACHE_DIR"] = self._old_cache

    def _call(self, connection: Connection, method: str, params: dict | None = None) -> dict:
        self.sent.clear()
        self.service.handle(connection, protocol.request(1, method, params or {}))
        self.assertEqual(len(self.sent), 1, f"{method} 应当恰好回一条消息")
        return self.sent[0]

    def test_hello_carries_local_hints_before_pairing(self):
        """未配对也返回：手机配对后 IP 变化靠 hello 自愈，不必重扫。"""
        connection = Connection("aa" * 32, self.sent.append)
        self.service.attach(connection)
        with mock.patch(
            "corral.remote.lan.local_hints", return_value=["192.168.1.5:8737"]
        ):
            reply = self._call(connection, protocol.M_HELLO, {"name": "iPhone"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["local_hints"], ["192.168.1.5:8737"])

    def test_hello_omits_local_hints_when_local_disabled(self):
        self.service.state.local_enabled = False
        connection = Connection("aa" * 32, self.sent.append)
        self.service.attach(connection)
        reply = self._call(connection, protocol.M_HELLO, {"name": "iPhone"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["local_hints"], [])


class MdnsDegradationTests(unittest.TestCase):
    def test_service_name_carries_short_host_id(self):
        self.assertEqual(service_name("abcdef1234567890"), "Corral-abcdef12")

    def test_start_without_zeroconf_never_blocks_daemon(self):
        """没装 zeroconf：start() 静默跳过、不读端口、不抛异常。"""

        def _must_not_run() -> int:
            raise AssertionError("缺依赖时不得读端口")

        advertiser = MdnsAdvertiser("abcdef1234567890", "dev", _must_not_run)
        if mdns_available():
            self.skipTest("zeroconf 已安装，走不了可选依赖缺失路径")
        self.assertFalse(advertiser.start())
        self.assertFalse(advertiser.start(), "跳过路径也必须幂等")
        advertiser.stop()
        self.assertFalse(advertiser.advertising)


class StatusLocalHintsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cache = os.environ.get("CORRAL_CACHE_DIR")
        os.environ["CORRAL_CACHE_DIR"] = self._tmp.name
        self.addCleanup(self._restore_cache)

    def _restore_cache(self) -> None:
        if self._old_cache is None:
            os.environ.pop("CORRAL_CACHE_DIR", None)
        else:
            os.environ["CORRAL_CACHE_DIR"] = self._old_cache

    def _run_status(self, as_json: bool):
        state = remote_config.RemoteState(
            host_id="h1",
            host_name="dev",
            relay_url="",
            relay_enabled=False,
            local_enabled=True,
            local_port=0,
            devices=[],
        )
        args = mock.Mock(json=as_json)
        with (
            mock.patch.object(remote_config, "load_state", return_value=state),
            mock.patch.object(remote_config, "read_pid", return_value=None),
            mock.patch.object(remote_config, "read_pairing", return_value=None),
            mock.patch.object(remote_config, "read_status_snapshot", return_value=None),
            mock.patch.object(remote_cli, "_check_dependencies", return_value=""),
            mock.patch.object(remote_config, "load_account", return_value={}),
            mock.patch(
                "corral.remote.lan.local_hints", return_value=["192.168.1.5:8737"]
            ),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = remote_cli._cmd_status(args)
        return code, buf.getvalue()

    def test_status_json_reports_effective_port_and_hints(self):
        code, text = self._run_status(as_json=True)
        self.assertEqual(code, 0)
        data = json.loads(text)["data"]
        self.assertEqual(data["local_port"], 8737)
        self.assertEqual(data["local_hints"], ["192.168.1.5:8737"])

    def test_status_text_shows_first_hint(self):
        code, text = self._run_status(as_json=False)
        self.assertEqual(code, 0)
        self.assertIn("192.168.1.5:8737", text)


if __name__ == "__main__":
    unittest.main()
