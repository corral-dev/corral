"""corral.remote.service：方法路由、配对准入与订阅记账。

这里用一个假的会话中枢，把「谁能调什么」和「断线后后台还在不在白抓帧」这两件
真正容易出错的事单独拎出来验证，不去碰真实的 tmux 与助手历史文件。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

from corral.remote import config as remote_config
from corral.remote import crypto, protocol, ratelimit
from corral.remote.service import Connection, RemoteService
from corral.remote.sessions import ActionError


class FakeHub:
    """只记录被调了什么，不做任何真事。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.watching_sessions = 0
        self.watching_screens: dict[str, int] = {}
        self._on_event = None

    def _record(self, name, *args):
        self.calls.append((name, args))

    def runtimes(self):
        return [{"id": "codex", "name": "Codex", "available": True}]

    def list_sessions(self, query: str = "", limit: int = 0):
        self._record("list_sessions", query, limit)
        return [{"key": "codex:abc", "title": "示例会话"}]

    def watch_sessions(self):
        self.watching_sessions += 1

    def unwatch_sessions(self):
        self.watching_sessions -= 1

    def watch_screen(self, key: str):
        self.watching_screens[key] = self.watching_screens.get(key, 0) + 1
        return {"cols": 80, "rows": 24, "full": True, "status": "first", "lines": []}

    def resync_screen(self, key: str):
        self._record("resync_screen", key)
        return {"cols": 80, "rows": 24, "full": True, "status": "resync", "lines": []}

    def unwatch_screen(self, key: str):
        self.watching_screens[key] = self.watching_screens.get(key, 0) - 1

    def watch_conversation(
        self, key: str, *, limit: int = 80, after_seq=None, generation=None
    ):
        self.watching_conversations = getattr(self, "watching_conversations", {})
        self.watching_conversations[key] = self.watching_conversations.get(key, 0) + 1
        self._record("watch_conversation", key, limit, after_seq, generation)
        return {
            "version": 1,
            "kind": "snapshot",
            "messages": [{"seq": 1, "role": "user", "text": "你好"}],
            "oldest_seq": 1,
            "newest_seq": 1,
            "has_more": False,
            "resume": "tail",
            "generation": 1,
        }

    def conversation_snapshot(self, key: str):
        self._record("conversation_snapshot", key)
        return [{"seq": 1, "role": "user", "text": "你好"}, {"seq": 2, "role": "assistant", "text": "在"}]

    def conversation_page(self, key: str, *, limit: int = 80, before_seq: int | None = None):
        self._record("conversation_snapshot", key)
        return {
            "version": 1,
            "kind": "snapshot",
            "messages": [
                {"seq": 1, "role": "user", "text": "你好"},
                {"seq": 2, "role": "assistant", "text": "在"},
            ],
            "oldest_seq": 1,
            "newest_seq": 2,
            "has_more": False,
        }

    def message_page(self, key: str, *, limit: int = 80, before_seq: int | None = None):
        return self.conversation_page(key, limit=limit, before_seq=before_seq)

    def unwatch_conversation(self, key: str):
        self.watching_conversations = getattr(self, "watching_conversations", {})
        self.watching_conversations[key] = self.watching_conversations.get(key, 0) - 1

    def send_text(self, key: str, text: str, submit: bool):
        self._record("send_text", key, text, submit)

    def send_keys(self, key: str, keys):
        self._record("send_keys", key, tuple(keys))

    def send_image(self, key: str, image_bytes: bytes) -> str:
        self._record("send_image", key, len(image_bytes))
        return "/tmp/paste.jpg"

    def projects(self):
        return [
            {
                "cwd": "/tmp/demo",
                "path": "/tmp/demo",
                "label": "demo",
                "name": "demo",
                "count": 1,
                "mtime": 1.0,
            }
        ]

    def delete_session(self, key: str):
        raise ActionError(protocol.E_NOT_FOUND, "会话不在了")


class RemoteServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cache = os.environ.get("CORRAL_CACHE_DIR")
        os.environ["CORRAL_CACHE_DIR"] = self._tmp.name
        self.addCleanup(self._restore_cache)

        ratelimit.PAIR_ATTEMPTS.reset()
        ratelimit.PAIR_ATTEMPTS_HOURLY.reset()
        ratelimit.INPUT_ACTIONS.reset()
        ratelimit.SESSION_CREATE.reset()
        ratelimit.PUSH_REGISTER.reset()
        self.hub = FakeHub()
        self.service = RemoteService(self.hub)  # type: ignore[arg-type]
        self.sent: list[dict] = []
        # Fixture LAN stub: M_HELLO fills local_hints via hostname resolution,
        # which stalls without timeout on some CI networks. Hint content is
        # never asserted here; dedicated LAN coverage lives in test_remote_lan.
        self._lan_hints = mock.patch(
            "corral.remote.lan.local_hints", return_value=[]
        )
        self._lan_hints.start()
        self.addCleanup(self._lan_hints.stop)

    def _restore_cache(self) -> None:
        if self._old_cache is None:
            os.environ.pop("CORRAL_CACHE_DIR", None)
        else:
            os.environ["CORRAL_CACHE_DIR"] = self._old_cache

    def _connect(self, public_key: str = "aa" * 32) -> Connection:
        connection = Connection(public_key, self.sent.append)
        self.service.attach(connection)
        return connection

    def _call(self, connection: Connection, method: str, params: dict | None = None) -> dict:
        self.sent.clear()
        self.service.handle(connection, protocol.request(1, method, params or {}))
        self.assertEqual(len(self.sent), 1, f"{method} 应当恰好回一条消息")
        return self.sent[0]

    # -- 准入 ---------------------------------------------------------------

    def test_unpaired_device_cannot_read_sessions(self):
        connection = self._connect()
        reply = self._call(connection, protocol.M_SESSIONS_LIST)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["e"]["code"], protocol.E_UNAUTHORIZED)
        self.assertEqual(self.hub.calls, [], "没配对就不该碰到会话数据")

    def test_unpaired_device_cannot_send_input(self):
        connection = self._connect()
        reply = self._call(connection, protocol.M_INPUT_TEXT, {"key": "codex:abc", "text": "rm -rf"})
        self.assertFalse(reply["ok"])
        self.assertEqual(self.hub.calls, [])

    def test_hello_works_before_pairing_but_hides_capabilities(self):
        connection = self._connect()
        reply = self._call(connection, protocol.M_HELLO, {"name": "iPhone"})
        self.assertTrue(reply["ok"])
        self.assertFalse(reply["d"]["paired"])
        self.assertEqual(reply["d"]["runtimes"], [], "没配对不该看到装了哪些助手")
        # 未配对也要带中继开关现状，方便手机补全 Host 记录。开源默认关中继。
        self.assertEqual(reply["d"]["relay_url"], self.service.state.relay_url)
        self.assertEqual(reply["d"]["relay_enabled"], self.service.state.relay_enabled)
        self.assertFalse(reply["d"]["relay_enabled"])
        self.assertTrue(reply["d"]["local_enabled"])

    def test_hello_omits_relay_url_when_relay_disabled(self):
        self.service.state.relay_enabled = False
        connection = self._connect()
        reply = self._call(connection, protocol.M_HELLO, {"name": "iPhone"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["relay_url"], "")
        self.assertFalse(reply["d"]["relay_enabled"])
        self.assertTrue(reply["d"]["local_enabled"])

    def test_pairing_with_the_right_code_unlocks_everything(self):
        code = self.service.begin_pairing()
        connection = self._connect()
        reply = self._call(connection, protocol.M_PAIR, {"code": code, "name": "我的 iPhone"})
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(connection.paired)
        listing = self._call(connection, protocol.M_SESSIONS_LIST)
        self.assertTrue(listing["ok"])
        self.assertEqual(listing["d"]["sessions"][0]["key"], "codex:abc")

    def test_sessions_list_rejects_non_integer_limit(self):
        code = self.service.begin_pairing()
        connection = self._connect()
        self.assertTrue(self._call(connection, protocol.M_PAIR, {"code": code})["ok"])
        reply = self._call(connection, protocol.M_SESSIONS_LIST, {"limit": "很多"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)

    def test_pairing_code_is_single_use(self):
        code = self.service.begin_pairing()
        first = self._connect("aa" * 32)
        self.assertTrue(self._call(first, protocol.M_PAIR, {"code": code})["ok"])
        second = self._connect("bb" * 32)
        reply = self._call(second, protocol.M_PAIR, {"code": code})
        self.assertFalse(reply["ok"], "配对码用过一次就该作废")

    def test_concurrent_scans_of_the_same_code_admit_only_one_phone(self):
        code = self.service.begin_pairing()
        barrier = threading.Barrier(2)
        outcomes: list[bool] = []
        lock = threading.Lock()

        def attempt(device_key: str) -> None:
            connection = Connection(device_key, lambda _msg: None)
            self.service.attach(connection)
            barrier.wait()
            self.service.handle(
                connection, protocol.request(1, protocol.M_PAIR, {"code": code})
            )
            with lock:
                outcomes.append(connection.paired)

        workers = [
            threading.Thread(target=attempt, args=("aa" * 32,)),
            threading.Thread(target=attempt, args=("bb" * 32,)),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(sum(1 for ok in outcomes if ok), 1)

    def test_wrong_pairing_code_is_rejected(self):
        self.service.begin_pairing()
        connection = self._connect()
        reply = self._call(connection, protocol.M_PAIR, {"code": "WRONGWRONGWRONG1"})
        self.assertFalse(reply["ok"])
        self.assertFalse(connection.paired)

    def test_pairing_without_an_open_window_is_rejected(self):
        connection = self._connect()
        reply = self._call(connection, protocol.M_PAIR, {"code": "ANYTHINGATALL123"})
        self.assertFalse(reply["ok"])

    def test_known_device_is_admitted_without_a_pairing_window(self):
        code = self.service.begin_pairing()
        first = self._connect()
        self._call(first, protocol.M_PAIR, {"code": code})
        self.assertFalse(self.service.pairing_open())
        self.assertTrue(self.service.accepts("aa" * 32))
        self.assertFalse(self.service.accepts("cc" * 32), "陌生设备在窗口关闭后不该被放行")

    def test_reconnecting_paired_device_is_paired_from_the_start(self):
        code = self.service.begin_pairing()
        self._call(self._connect(), protocol.M_PAIR, {"code": code})
        again = self._connect()
        self.assertTrue(again.paired)

    # -- 订阅记账 -----------------------------------------------------------

    def _paired(self) -> Connection:
        code = self.service.begin_pairing()
        connection = self._connect()
        self._call(connection, protocol.M_PAIR, {"code": code})
        return connection

    def test_duplicate_watch_does_not_double_count(self):
        connection = self._paired()
        self._call(connection, protocol.M_SESSIONS_WATCH)
        self._call(connection, protocol.M_SESSIONS_WATCH)
        self.assertEqual(self.hub.watching_sessions, 1)

    def test_duplicate_screen_watch_resyncs_without_double_count(self):
        """聊天状态条与终端页各调一次 watch：订阅只记一次，第二次必须仍有整帧。"""
        connection = self._paired()
        first = self._call(connection, protocol.M_SCREEN_WATCH, {"key": "codex:abc"})
        second = self._call(connection, protocol.M_SCREEN_WATCH, {"key": "codex:abc"})
        self.assertEqual(self.hub.watching_screens["codex:abc"], 1)
        self.assertEqual(first["d"]["frame"]["status"], "first")
        self.assertEqual(second["d"]["frame"]["status"], "resync")
        self.assertIn(("resync_screen", ("codex:abc",)), self.hub.calls)

    def test_duplicate_session_watch_returns_snapshot_without_double_count(self):
        """同连接第二次 session.watch 不得空历史，且不得再加一层订阅计数。"""
        connection = self._paired()
        first = self._call(connection, protocol.M_SESSION_WATCH, {"key": "codex:abc"})
        second = self._call(connection, protocol.M_SESSION_WATCH, {"key": "codex:abc"})
        self.assertEqual(self.hub.watching_conversations["codex:abc"], 1)
        self.assertEqual(len(first["d"]["messages"]), 1)
        self.assertEqual(len(second["d"]["messages"]), 2)
        self.assertIn(("conversation_snapshot", ("codex:abc",)), self.hub.calls)

    def test_session_watch_forwards_resume_cursor(self):
        """重连带来的 after_seq / generation 必须原样交给会话中枢。"""
        connection = self._paired()
        self._call(
            connection,
            protocol.M_SESSION_WATCH,
            {"key": "codex:abc", "after_seq": 12, "generation": 3},
        )
        self.assertIn(
            ("watch_conversation", ("codex:abc", 80, 12, 3)),
            self.hub.calls,
        )

    def test_disconnect_releases_every_subscription(self):
        """断线不退订会让后台一直抓帧，是最典型的「电脑莫名发烫」来源。"""
        connection = self._paired()
        self._call(connection, protocol.M_SESSIONS_WATCH)
        self._call(connection, protocol.M_SCREEN_WATCH, {"key": "codex:abc"})
        self.service.detach(connection)
        self.assertEqual(self.hub.watching_sessions, 0)
        self.assertEqual(self.hub.watching_screens["codex:abc"], 0)

    def test_events_only_reach_subscribers(self):
        watcher = self._paired()
        bystander = self._connect("bb" * 32)
        self._call(watcher, protocol.M_SESSIONS_WATCH)
        self.sent.clear()
        self.service._dispatch_event(protocol.CH_SESSIONS, {"sessions": []})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["c"], protocol.CH_SESSIONS)
        self.service.detach(bystander)

    def test_closed_connection_stops_receiving_events(self):
        connection = self._paired()
        self._call(connection, protocol.M_SESSIONS_WATCH)
        connection.closed = True
        self.sent.clear()
        self.service._dispatch_event(protocol.CH_SESSIONS, {"sessions": []})
        self.assertEqual(self.sent, [])

    # -- 错误处理 -----------------------------------------------------------

    def test_missing_session_key_is_a_usage_error(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_SCREEN_WATCH, {})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.watching_screens, {})

    def test_empty_key_list_is_rejected_before_touching_tmux(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_INPUT_KEYS, {"key": "codex:abc", "keys": []})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.calls, [])

    def test_hub_errors_are_passed_through_verbatim(self):
        connection = self._paired()
        reply = self._call(
            connection, protocol.M_SESSION_DELETE, {"key": "codex:abc", "confirm": True}
        )
        self.assertEqual(reply["e"]["code"], protocol.E_NOT_FOUND)
        self.assertEqual(reply["e"]["message"], "会话不在了")

    def test_destructive_actions_require_confirm(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_SESSION_DELETE, {"key": "codex:abc"})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        reply = self._call(connection, protocol.M_SESSION_STOP, {"key": "codex:abc"})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)

    def test_input_keys_rejects_tmux_injection(self):
        connection = self._paired()
        reply = self._call(
            connection,
            protocol.M_INPUT_KEYS,
            {"key": "codex:abc", "keys": ["Enter\nrun-shell evil"]},
        )
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.calls, [])

    def test_unpair_on_disk_revokes_live_connection(self):
        """另一进程 unpair 后，常驻服务必须以磁盘为准踢掉在线连接。"""
        connection = self._paired()
        device = remote_config.find_device(self.service.state, connection.device_public_key)
        self.assertIsNotNone(device)
        # 模拟 CLI unpair：只改磁盘
        other = remote_config.load_state()
        self.assertTrue(remote_config.remove_device(other, device.id))
        # 下次请求应被拒并踢下线
        reply = self._call(connection, protocol.M_SESSIONS_LIST)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["e"]["code"], protocol.E_UNAUTHORIZED)
        self.assertFalse(connection.paired)

    def test_touch_device_does_not_resurrect_unpaired_device(self):
        code = self.service.begin_pairing()
        connection = self._connect()
        self._call(connection, protocol.M_PAIR, {"code": code})
        device = remote_config.find_device(self.service.state, connection.device_public_key)
        # CLI 解绑
        disk = remote_config.load_state()
        remote_config.remove_device(disk, device.id)
        # 陈旧服务内存里仍以为有设备；touch 不得把解绑写回去
        remote_config.touch_device(self.service.state, connection.device_public_key)
        reloaded = remote_config.load_state()
        self.assertIsNone(remote_config.find_device(reloaded, connection.device_public_key))

    def test_readonly_pairing_blocks_input(self):
        code = self.service.begin_pairing(mode="readonly")
        connection = self._connect()
        reply = self._call(connection, protocol.M_PAIR, {"code": code, "name": "只读机"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["access"], "readonly")
        blocked = self._call(
            connection, protocol.M_INPUT_TEXT, {"key": "codex:abc", "text": "hi"}
        )
        self.assertEqual(blocked["e"]["code"], protocol.E_UNAUTHORIZED)
        listing = self._call(connection, protocol.M_SESSIONS_LIST)
        self.assertTrue(listing["ok"])

    def test_unknown_method_is_reported_not_crashed(self):
        connection = self._paired()
        reply = self._call(connection, "session.selfDestruct")
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)

    def test_non_request_messages_are_ignored(self):
        connection = self._paired()
        self.sent.clear()
        self.service.handle(connection, protocol.event("sessions", {}))
        self.assertEqual(self.sent, [])

    def test_image_with_broken_base64_is_rejected(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_INPUT_IMAGE, {"key": "codex:abc", "data": "not-base64!!"})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)

    def test_image_with_empty_payload_is_rejected(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_INPUT_IMAGE, {"key": "codex:abc", "data": ""})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.calls, [])

    def test_whitespace_only_keys_are_rejected(self):
        connection = self._paired()
        reply = self._call(
            connection, protocol.M_INPUT_KEYS, {"key": "codex:abc", "keys": ["  ", "\t"]}
        )
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.calls, [])

    def test_screen_resize_is_rejected_without_touching_desktop(self):
        """手机误发 screen.resize 也绝不能改桌面窗口尺寸。"""
        connection = self._paired()
        reply = self._call(
            connection,
            protocol.M_SCREEN_RESIZE,
            {"key": "codex:abc", "cols": 40, "rows": 20},
        )
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.calls, [])

    def test_projects_list_includes_ios_path_name_fields(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_PROJECTS_LIST)
        self.assertTrue(reply["ok"])
        project = reply["d"]["projects"][0]
        self.assertEqual(project["path"], "/tmp/demo")
        self.assertEqual(project["name"], "demo")
        self.assertEqual(project["cwd"], "/tmp/demo")
        self.assertEqual(project["label"], "demo")

    def test_session_new_requires_runtime(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_SESSION_NEW, {"cwd": "/tmp"})
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)

    def test_oversized_success_payload_returns_error_instead_of_dropping(self):
        """回包体积超限必须把错误回给手机，不能让通道静默断开后让客户端空等到超时。"""
        connection = self._paired()
        sent: list[dict] = []
        blows = {"n": 0}

        def flaky_send(message: dict) -> None:
            blows["n"] += 1
            if blows["n"] == 1:
                raise protocol.ProtocolError("消息未压缩体积过大")
            sent.append(message)

        connection.send = flaky_send
        self.service.handle(connection, protocol.request(9, protocol.M_SESSIONS_LIST, {}))
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0].get("ok"))
        self.assertEqual(sent[0]["e"]["code"], protocol.E_INTERNAL)

    # -- 控制/数据双连接 ---------------------------------------------------

    def test_hello_without_want_data_plane_has_planes_but_no_token(self):
        """旧客户端不带 want_data_plane：能看见 planes 能力，但不发绑定令牌。"""
        connection = self._connect()
        reply = self._call(connection, protocol.M_HELLO, {"name": "iPhone"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["capabilities"]["planes"], ["control", "data"])
        self.assertNotIn("data_bind", reply["d"])

    def test_hello_advertises_completion_notify(self) -> None:
        connection = self._connect()
        reply = self._call(connection, protocol.M_HELLO, {"name": "iPhone"})
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["d"]["capabilities"]["completion_notify"])

    def test_push_register_stores_notify_prefs(self) -> None:
        connection = self._paired()
        reply = self._call(
            connection,
            protocol.M_PUSH_REGISTER,
            {
                "token": "b" * 64,
                "env": "sandbox",
                "notify_completed": False,
                "notify_aborted": True,
            },
        )
        self.assertTrue(reply["ok"])
        device = remote_config.find_device(self.service.state, connection.device_public_key)
        assert device is not None
        self.assertFalse(device.notify_completed)
        self.assertTrue(device.notify_aborted)

    def test_push_register_without_prefs_keeps_defaults(self) -> None:
        connection = self._paired()
        reply = self._call(
            connection,
            protocol.M_PUSH_REGISTER,
            {"token": "b" * 64, "env": "sandbox"},
        )
        self.assertTrue(reply["ok"])
        device = remote_config.find_device(self.service.state, connection.device_public_key)
        assert device is not None
        self.assertTrue(device.notify_completed)
        self.assertTrue(device.notify_aborted)

    def test_hello_with_want_data_plane_issues_bind_token(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_HELLO, {"name": "iPhone", "want_data_plane": True})
        self.assertTrue(reply["ok"])
        token = reply["d"]["data_bind"]
        self.assertTrue(token)
        self.assertEqual(reply["d"]["capabilities"]["planes"], ["control", "data"])
        data_sent: list[dict] = []
        attached = self.service.attach_data_plane(
            token, connection.device_public_key, data_sent.append, lambda: None, channel=object()
        )
        self.assertIs(attached, connection)
        self.assertIsNotNone(connection.data_send)

    def test_wrong_data_bind_does_not_kick_control_plane(self):
        connection = self._paired()
        self._call(connection, protocol.M_HELLO, {"name": "iPhone", "want_data_plane": True})
        closed = []
        connection.close_hook = lambda: closed.append("control")
        result = self.service.attach_data_plane(
            "not-a-real-token",
            connection.device_public_key,
            lambda _m: None,
            lambda: None,
            channel=object(),
        )
        self.assertIsNone(result)
        self.assertFalse(connection.closed)
        self.assertIn(connection, self.service._connections)
        self.assertEqual(closed, [])
        listing = self._call(connection, protocol.M_SESSIONS_LIST)
        self.assertTrue(listing["ok"])

    def test_data_bind_wrong_device_key_does_not_kick_control(self):
        connection = self._paired()
        reply = self._call(connection, protocol.M_HELLO, {"name": "iPhone", "want_data_plane": True})
        token = reply["d"]["data_bind"]
        result = self.service.attach_data_plane(
            token, "bb" * 32, lambda _m: None, lambda: None, channel=object()
        )
        self.assertIsNone(result)
        self.assertFalse(connection.closed)
        # 令牌未消费，本机公钥仍能附着。
        attached = self.service.attach_data_plane(
            token, connection.device_public_key, lambda _m: None, lambda: None, channel=object()
        )
        self.assertIs(attached, connection)

    def test_data_bind_is_single_use(self):
        connection = self._paired()
        token = self._call(connection, protocol.M_HELLO, {"name": "iPhone", "want_data_plane": True})["d"][
            "data_bind"
        ]
        first = self.service.attach_data_plane(
            token, connection.device_public_key, lambda _m: None, lambda: None, channel=object()
        )
        second = self.service.attach_data_plane(
            token, connection.device_public_key, lambda _m: None, lambda: None, channel=object()
        )
        self.assertIs(first, connection)
        self.assertIsNone(second)
        self.assertFalse(connection.closed)

    def test_expired_data_bind_is_rejected(self):
        connection = self._paired()
        token = self._call(connection, protocol.M_HELLO, {"name": "iPhone", "want_data_plane": True})["d"][
            "data_bind"
        ]
        self.service._data_binds[token].expires_at = 0
        self.assertIsNone(
            self.service.attach_data_plane(
                token, connection.device_public_key, lambda _m: None, lambda: None, channel=object()
            )
        )
        self.assertFalse(connection.closed)

    def test_large_responses_go_to_data_plane_when_attached(self):
        connection = self._paired()
        data_sent: list[dict] = []
        connection.data_send = data_sent.append
        self.sent.clear()
        self.service.handle(connection, protocol.request(1, protocol.M_SESSION_MESSAGES, {"key": "codex:abc"}))
        self.assertEqual(self.sent, [])
        self.assertEqual(len(data_sent), 1)
        self.assertTrue(data_sent[0]["ok"])

        data_sent.clear()
        self.service.handle(connection, protocol.request(2, protocol.M_SESSIONS_LIST, {}))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(data_sent, [])

    def test_screen_events_go_data_plane_conversation_events_stay_control(self):
        connection = self._paired()
        self._call(connection, protocol.M_SESSIONS_WATCH)
        self._call(connection, protocol.M_SESSION_WATCH, {"key": "codex:abc"})
        self._call(connection, protocol.M_SCREEN_WATCH, {"key": "codex:abc"})
        data_sent: list[dict] = []
        connection.data_send = data_sent.append
        self.sent.clear()
        self.service._dispatch_event(protocol.CH_SESSIONS, {"sessions": []})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(data_sent, [])
        self.sent.clear()
        self.service._dispatch_event(protocol.session_channel("codex:abc"), {"messages": []})
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(data_sent, [])
        self.sent.clear()
        self.service._dispatch_event(protocol.screen_channel("codex:abc"), {"full": True})
        self.assertEqual(self.sent, [])
        self.assertEqual(len(data_sent), 1)
        self.assertEqual(data_sent[0]["c"], protocol.screen_channel("codex:abc"))

    def test_data_plane_disconnect_keeps_control_and_allows_rebind(self):
        connection = self._paired()
        token = self._call(connection, protocol.M_HELLO, {"name": "iPhone", "want_data_plane": True})["d"][
            "data_bind"
        ]
        marker = object()
        self.service.attach_data_plane(
            token, connection.device_public_key, lambda _m: None, lambda: None, channel=marker
        )
        self.assertIsNotNone(connection.data_send)
        self.service.detach_data_plane(connection, marker)
        self.assertIsNone(connection.data_send)
        self.assertFalse(connection.closed)
        again = self._call(connection, protocol.M_HELLO, {"name": "iPhone", "want_data_plane": True})
        self.assertIn("data_bind", again["d"])
        self.assertNotEqual(again["d"]["data_bind"], token)

    def test_control_detach_closes_data_plane(self):
        connection = self._paired()
        closed = []
        token = self._call(connection, protocol.M_HELLO, {"name": "iPhone", "want_data_plane": True})["d"][
            "data_bind"
        ]
        self.service.attach_data_plane(
            token,
            connection.device_public_key,
            lambda _m: None,
            lambda: closed.append("data"),
            channel=object(),
        )
        self.service.detach(connection)
        self.assertEqual(closed, ["data"])
        self.assertTrue(connection.closed)

    def test_rpc_on_either_plane_is_accepted(self):
        """半升级时开在「错误平面」上的调用仍要处理，不能拒收卡死。"""
        connection = self._paired()
        data_sent: list[dict] = []
        inbound_data = []

        def data_reply(message):
            inbound_data.append(message)

        connection.data_send = data_sent.append
        # 从数据面进来的短 RPC：原路返回数据面，不拒。
        self.service.handle(
            connection,
            protocol.request(4, protocol.M_SESSIONS_LIST, {}),
            reply=data_reply,
        )
        self.assertEqual(len(inbound_data), 1)
        self.assertTrue(inbound_data[0]["ok"])

    # -- 同一设备只保留一条控制面 -----------------------------------------

    def test_new_control_plane_supersedes_stale_one_from_same_device(self):
        """手机重连/换网留下的旧控制面必须被新控制面取代并关闭，不能永远算在线。"""
        old = self._paired()
        closed: list[str] = []
        old.close_hook = lambda: closed.append("old")
        token = self._call(old, protocol.M_HELLO, {"name": "iPhone", "want_data_plane": True})["d"][
            "data_bind"
        ]
        self.service.attach_data_plane(
            token, old.device_public_key, lambda _m: None, lambda: closed.append("old-data"), channel=object()
        )

        new = Connection(old.device_public_key, self.sent.append)
        self.service.attach(new)

        self.assertTrue(old.closed)
        self.assertNotIn(old, self.service._connections)
        self.assertIn(new, self.service._connections)
        self.assertEqual(sorted(closed), ["old", "old-data"])
        online = self.service.online_devices()
        self.assertEqual(len(online), 1)
        self.assertEqual(online[0]["public_key"], new.device_public_key[:16] + "…")
        # 新控制面照常可用。
        listing = self._call(new, protocol.M_SESSIONS_LIST)
        self.assertTrue(listing["ok"])

    def _instance(self, instance_id: str, public_key: str = "aa" * 32) -> Connection:
        connection = Connection(public_key, self.sent.append)
        connection.instance_id = instance_id
        self.service.attach(connection)
        return connection

    def test_two_app_instances_of_one_device_coexist(self):
        """Installed Mac app + Debug build share a key; they must not evict each other."""
        installed = self._instance("installed")
        debug = self._instance("debug")
        # Each reconnecting again (as the old ping-pong did) still leaves both online.
        installed_again = self._instance("installed")
        debug_again = self._instance("debug")
        self.assertTrue(installed.closed)
        self.assertTrue(debug.closed)
        self.assertFalse(installed_again.closed)
        self.assertFalse(debug_again.closed)
        self.assertEqual(
            {c for c in self.service._connections}, {installed_again, debug_again}
        )

    def test_reconnect_of_same_instance_supersedes_its_stale_socket(self):
        stale = self._instance("phone")
        fresh = self._instance("phone")
        self.assertTrue(stale.closed)
        self.assertIn(fresh, self.service._connections)

    def test_legacy_client_without_instance_keeps_one_plane_per_key(self):
        legacy = self._connect()
        modern = self._instance("mac")
        self.assertTrue(legacy.closed, "an id-less predecessor is still replaced")
        legacy_again = self._connect()
        self.assertTrue(modern.closed, "an id-less newcomer still replaces everything")
        self.assertEqual(self.service._connections, {legacy_again})

    def test_per_device_control_planes_are_capped_oldest_first(self):
        planes = [self._instance(f"i{index}") for index in range(5)]
        self.assertTrue(planes[0].closed)
        self.assertEqual(self.service._connections, set(planes[1:]))

    def test_other_devices_are_not_touched_when_one_reconnects(self):
        first = self._connect("aa" * 32)
        second = self._connect("bb" * 32)
        replacement = Connection("aa" * 32, self.sent.append)
        self.service.attach(replacement)
        self.assertTrue(first.closed)
        self.assertFalse(second.closed)
        self.assertIn(second, self.service._connections)
        self.assertIn(replacement, self.service._connections)

    def test_detach_twice_only_reports_once(self):
        connection = self._connect()
        self.service.detach(connection)
        # 第二次 detach（例如 close_hook 回调再进来）不应抛错，也不重复计数。
        self.service.detach(connection)
        self.assertNotIn(connection, self.service._connections)

    # -- Slice0 服务端分段计时 -------------------------------------------------

    def test_successful_rpc_records_duration_plane_and_req_id(self):
        """成功 RPC 在 audit 留一条带耗时/平面/req_id 的条目，不记会话 key 与参数。"""
        connection = self._paired()
        self._call(connection, protocol.M_SESSIONS_WATCH, {})
        audit = self.service.recent_audit(10)
        timed = [e for e in audit if e["method"] == protocol.M_SESSIONS_WATCH]
        self.assertEqual(len(timed), 1)
        entry = timed[0]
        self.assertIn("duration_ms", entry)
        self.assertGreaterEqual(entry["duration_ms"], 0)
        self.assertEqual(entry["plane"], "control")
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["req_id"], 1)
        self.assertNotIn("key", entry)
        self.assertNotIn("params", entry)

    def test_failed_rpc_does_not_leave_timing_entry(self):
        """鉴权/参数失败不记耗时条目，由 remote_method_failed 覆盖。"""
        connection = self._connect()  # 未配对
        before = len(self.service.recent_audit(100))
        self._call(connection, protocol.M_SESSIONS_LIST, {})
        self.assertEqual(len(self.service.recent_audit(100)), before)

    def test_data_plane_response_is_marked_data(self):
        """附着数据面后，大响应的 audit 平面记 data。"""
        connection = self._paired()
        data_sent: list[dict] = []
        connection.data_send = data_sent.append
        self.sent.clear()
        self.service.handle(
            connection,
            protocol.request(1, protocol.M_SESSION_MESSAGES, {"key": "codex:abc"}),
        )
        self.assertEqual(self.sent, [])
        self.assertEqual(len(data_sent), 1)
        audit = self.service.recent_audit(10)
        timed = [e for e in audit if e["method"] == protocol.M_SESSION_MESSAGES]
        self.assertEqual(len(timed), 1)
        self.assertEqual(timed[0]["plane"], "data")


class PairingWindowTests(unittest.TestCase):
    """配对窗口存在文件里，所以要单独确认过期与清理的行为。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = os.environ.get("CORRAL_CACHE_DIR")
        os.environ["CORRAL_CACHE_DIR"] = self._tmp.name
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._old is None:
            os.environ.pop("CORRAL_CACHE_DIR", None)
        else:
            os.environ["CORRAL_CACHE_DIR"] = self._old

    def test_expired_window_is_treated_as_closed(self):
        remote_config.write_pairing(crypto.new_pairing_code(), ttl=-1)
        self.assertIsNone(remote_config.read_pairing())

    def test_reading_an_expired_window_cleans_up_the_file(self):
        remote_config.write_pairing(crypto.new_pairing_code(), ttl=-1)
        remote_config.read_pairing()
        self.assertFalse(remote_config.pairing_path().exists())

    def test_identity_key_is_stable_and_private(self):
        first = remote_config.load_or_create_identity()
        self.assertEqual(first, remote_config.load_or_create_identity())
        mode = remote_config.identity_key_path().stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, "私钥文件权限必须是 0600")

    def test_devices_survive_a_restart(self):
        state = remote_config.load_state()
        remote_config.add_device(
            state,
            remote_config.PairedDevice(
                id="d1", name="iPhone", public_key="aa" * 32, paired_at=1.0
            ),
        )
        reloaded = remote_config.load_state()
        self.assertIsNotNone(remote_config.find_device(reloaded, "aa" * 32))
        self.assertEqual(reloaded.host_id, state.host_id)

    def test_wrong_code_does_not_close_the_window(self):
        code = crypto.new_pairing_code()
        remote_config.write_pairing(code, ttl=60)
        mode, reason = remote_config.consume_pairing("WRONGWRONGWRONG1")
        self.assertIsNone(mode)
        self.assertEqual(reason, "wrong")
        self.assertIsNotNone(remote_config.read_pairing())

    def test_consume_pairing_is_atomic_across_threads(self):
        code = crypto.new_pairing_code()
        remote_config.write_pairing(code, ttl=60, mode="readonly")
        barrier = threading.Barrier(2)
        results: list[tuple[str | None, str | None]] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            result = remote_config.consume_pairing(code)
            with lock:
                results.append(result)

        workers = [threading.Thread(target=worker) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        oks = [item for item in results if item[1] is None]
        self.assertEqual(len(oks), 1)
        self.assertEqual(oks[0][0], "readonly")
        self.assertEqual(sum(1 for item in results if item[1] == "expired"), 1)

    def test_rotate_host_key_keeps_the_key_still_registered_on_the_relay(self):
        first = remote_config.load_or_create_host_key()
        state = remote_config.load_state()
        remote_config.rotate_host_key(state)
        self.assertEqual(remote_config.load_host_prev_key(), first)
        remote_config.rotate_host_key(state)
        self.assertEqual(
            remote_config.load_host_prev_key(),
            first,
            "连续轮换尚未连上中继时，prev 必须仍是中继上那把",
        )
        self.assertNotEqual(remote_config.host_key_path().read_bytes(), first)

    def test_relay_headers_sign_prev_auth_with_the_same_nonce(self):
        from corral.remote.transport.relay import RelayClient

        first = remote_config.load_or_create_host_key()
        state = remote_config.load_state()
        remote_config.rotate_host_key(state)
        client = RelayClient(None, state, first)  # type: ignore[arg-type]
        headers = client._headers()
        self.assertIn("X-Corral-Prev-Auth", headers)
        auth_parts = headers["X-Corral-Auth"].split(".")
        prev_parts = headers["X-Corral-Prev-Auth"].split(".")
        self.assertEqual(auth_parts[1:4], prev_parts[1:4])
        remote_config.clear_host_prev_key()
        self.assertNotIn("X-Corral-Prev-Auth", client._headers())

    def test_relay_registered_payload_enables_host_lane_attach(self):
        from corral.remote.transport.relay import RelayClient

        state = remote_config.load_state()
        client = RelayClient(None, state, b"\x00" * 32)  # type: ignore[arg-type]
        called: list[bool] = []
        payload = json.dumps(
            {"generation": 3, "node": "node-a", "capabilities": ["host_lane_attach"]}
        ).encode("utf-8")
        client._on_registered(payload, lambda: called.append(True))
        self.assertEqual(client._registration_generation, 3)
        self.assertEqual(client._registration_node, "node-a")
        self.assertTrue(client._host_lane_attach)
        self.assertEqual(called, [True])
        client._on_registered(b"", None)
        self.assertFalse(client._host_lane_attach)


if __name__ == "__main__":
    unittest.main()
