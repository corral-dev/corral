"""session.restart 主机侧：桌面高级操作重启的远程入口。

覆盖：路由/鉴权/能力宣告、运行中重启换进程保身份、已结束退回原生恢复、
占位/shell/dormant 拒绝、预检失败保原进程、起新失败不伪装成功、
同会话并发串行、回执携带人类原因与规范目标键、占位转正列表版本契约。
只读 TUI/iOS 不动；本文件新建，不改旧测试。
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest import mock

from corral import embed, split_layout
from corral.models import LaunchPlan
from corral.remote import protocol, ratelimit
from corral.remote.command_receipts import STATUS_REJECTED, STATUS_UNKNOWN
from corral.remote.service import (
    _CONFIRM_METHODS,
    _READONLY_METHODS,
    Connection,
    RemoteService,
)
from corral.remote.sessions import PartialInjectionError, SessionHub
from corral.runtime import LaunchError


def _running_session(**overrides) -> dict:
    session = {
        "source": "claude",
        "id": "sess-restart-1",
        "short_id": "sess-restart-1",
        "cwd": "/tmp/proj",
        "cwd_display": "/tmp/proj",
        "mtime": 1_700_000_000.0,
        "display_time": "01-01 12:00",
        "size_kb": 3.0,
        "status_tag": "running",
        "live": True,
        "keepalive_name": "corral-claude-abc12345",
        "fallback_title": "重启验收会话",
        "attention_kind": "working",
        "last_user_msg": "在吗",
        "last_agent_msg": "在，你说。",
        "path": "/tmp/hist-restart.jsonl",
    }
    session.update(overrides)
    return session


class RestartFakeHub:
    """service 层路由替身：只记录，不做真事。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.migrations: dict[str, str] = {}

    def runtimes(self):
        return [{"id": "claude", "name": "Claude", "available": True}]

    def resolve_session_key(self, key: str) -> str:
        return self.migrations.get(key, key)

    def restart_session(self, key: str):
        self.calls.append(("restart_session", key))
        return {"key": key, "title": "已重启"}


class ServiceRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cache = os.environ.get("CORRAL_CACHE_DIR")
        os.environ["CORRAL_CACHE_DIR"] = self._tmp.name
        self.addCleanup(self._restore_cache)
        ratelimit.PAIR_ATTEMPTS.reset()
        ratelimit.PAIR_ATTEMPTS_HOURLY.reset()
        ratelimit.INPUT_ACTIONS.reset()
        ratelimit.TERMINAL_TYPING.reset()
        ratelimit.SESSION_CREATE.reset()
        ratelimit.PUSH_REGISTER.reset()
        self.hub = RestartFakeHub()
        self.service = RemoteService(self.hub)  # type: ignore[arg-type]
        self.sent: list[dict] = []

    def _restore_cache(self) -> None:
        if self._old_cache is None:
            os.environ.pop("CORRAL_CACHE_DIR", None)
        else:
            os.environ["CORRAL_CACHE_DIR"] = self._old_cache

    def _connect(self, public_key: str = "aa" * 32) -> Connection:
        connection = Connection(public_key, self.sent.append)
        self.service.attach(connection)
        return connection

    def _pair(self, connection: Connection | None = None, **kwargs) -> Connection:
        code = self.service.begin_pairing(**kwargs)
        connection = connection or self._connect()
        self.service.handle(connection, protocol.request(1, protocol.M_PAIR, {"code": code}))
        return connection

    def _call(self, connection: Connection, method: str, params: dict | None = None) -> dict:
        self.sent.clear()
        self.service.handle(connection, protocol.request(2, method, params or {}))
        self.assertEqual(len(self.sent), 1)
        return self.sent[0]

    def test_restart_routes_and_returns_session(self) -> None:
        connection = self._pair()
        reply = self._call(connection, protocol.M_SESSION_RESTART, {"key": "claude:sess-1"})
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["d"]["session"]["title"], "已重启")
        self.assertIn(("restart_session", "claude:sess-1"), self.hub.calls)

    def test_restart_unpaired_rejected(self) -> None:
        connection = self._connect()
        reply = self._call(connection, protocol.M_SESSION_RESTART, {"key": "claude:sess-1"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["e"]["code"], protocol.E_UNAUTHORIZED)
        self.assertEqual(self.hub.calls, [])

    def test_restart_readonly_rejected(self) -> None:
        connection = self._pair(mode="readonly")
        self.assertEqual(connection.access, "readonly")
        # 只读配对不在允许表里：服务端拒绝，手机侧隐藏菜单。
        self.assertNotIn(protocol.M_SESSION_RESTART, _READONLY_METHODS)
        reply = self._call(connection, protocol.M_SESSION_RESTART, {"key": "claude:sess-1"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["e"]["code"], protocol.E_UNAUTHORIZED)
        self.assertEqual(self.hub.calls, [])

    def test_restart_missing_key_usage(self) -> None:
        connection = self._pair()
        reply = self._call(connection, protocol.M_SESSION_RESTART, {})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)
        self.assertEqual(self.hub.calls, [])

    def test_restart_needs_no_confirm(self) -> None:
        # 桌面裁定：菜单选择即确认。confirm 门只拦 stop/delete。
        self.assertNotIn(protocol.M_SESSION_RESTART, _CONFIRM_METHODS)
        connection = self._pair()
        reply = self._call(connection, protocol.M_SESSION_RESTART, {"key": "claude:sess-1"})
        self.assertTrue(reply["ok"], reply)

    def test_hello_advertises_session_restart(self) -> None:
        connection = self._pair()
        reply = self._call(connection, protocol.M_HELLO, {"name": "Phone"})
        self.assertTrue(reply["d"]["capabilities"][protocol.CAPABILITY_SESSION_RESTART])


class ReceiptCauseFakeHub(RestartFakeHub):
    def __init__(self) -> None:
        super().__init__()
        self.fail_with: Exception | None = None

    def send_text(self, key: str, text: str, submit: bool):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append(("send_text", key, text, submit))


class ReceiptCauseTests(unittest.TestCase):
    """回执必须带人类原因：之前只有 reason 码，message 留在服务端日志里。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cache = os.environ.get("CORRAL_CACHE_DIR")
        os.environ["CORRAL_CACHE_DIR"] = self._tmp.name
        self.addCleanup(self._restore_cache)
        ratelimit.PAIR_ATTEMPTS.reset()
        ratelimit.PAIR_ATTEMPTS_HOURLY.reset()
        ratelimit.INPUT_ACTIONS.reset()
        ratelimit.TERMINAL_TYPING.reset()
        ratelimit.SESSION_CREATE.reset()
        ratelimit.PUSH_REGISTER.reset()
        self.hub = ReceiptCauseFakeHub()
        self.service = RemoteService(self.hub)  # type: ignore[arg-type]
        self.sent: list[dict] = []

    def _restore_cache(self) -> None:
        if self._old_cache is None:
            os.environ.pop("CORRAL_CACHE_DIR", None)
        else:
            os.environ["CORRAL_CACHE_DIR"] = self._old_cache

    def _pair_with_receipts(self, public_key: str = "cc" * 32) -> Connection:
        code = self.service.begin_pairing()
        connection = Connection(public_key, self.sent.append)
        self.service.attach(connection)
        self.service.handle(connection, protocol.request(1, protocol.M_PAIR, {"code": code}))
        self.sent.clear()
        self.service.handle(
            connection,
            protocol.request(1, protocol.M_HELLO, {"name": "Phone", "want_command_receipts": True}),
        )
        self.assertTrue(connection.command_receipts)
        self.sent.clear()
        return connection

    def _call(self, connection: Connection, method: str, params: dict | None = None) -> dict:
        self.sent.clear()
        self.service.handle(connection, protocol.request(1, method, params or {}))
        self.assertEqual(len(self.sent), 1)
        return self.sent[0]

    def test_rejected_receipt_carries_host_message(self) -> None:
        from corral.remote.sessions import ActionError

        connection = self._pair_with_receipts()
        self.hub.fail_with = ActionError(protocol.E_UNAVAILABLE, "pane gone")
        reply = self._call(
            connection,
            protocol.M_INPUT_TEXT,
            {"key": "codex:abc", "text": "hi", "submit": True, "command_id": "cmd-msg-1"},
        )
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["status"], STATUS_REJECTED)
        self.assertEqual(reply["d"]["reason"], protocol.E_UNAVAILABLE)
        # 人类原因走既定的 detail 字段并带 message 别名，而不是只留在服务端日志里。
        self.assertEqual(reply["d"]["detail"], "pane gone")
        self.assertEqual(reply["d"]["message"], "pane gone")
        status = self._call(connection, protocol.M_COMMAND_STATUS, {"command_id": "cmd-msg-1"})
        self.assertEqual(status["d"]["status"], STATUS_REJECTED)
        self.assertEqual(status["d"]["detail"], "pane gone")
        self.assertEqual(status["d"]["message"], "pane gone")

    def test_unknown_receipt_carries_partial_message(self) -> None:
        connection = self._pair_with_receipts()
        self.hub.fail_with = PartialInjectionError("partial injection")
        reply = self._call(
            connection,
            protocol.M_INPUT_TEXT,
            {"key": "codex:abc", "text": "hi", "submit": True, "command_id": "cmd-msg-2"},
        )
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["status"], STATUS_UNKNOWN)
        # unknown 保持不确定：只给人类原因，不转 rejected、不谎报送达。
        self.assertTrue(reply["d"]["detail"])
        self.assertEqual(reply["d"]["message"], reply["d"]["detail"])

    def test_receipt_records_canonical_target_key(self) -> None:
        # 手机手里是退役占位旧键：回执必须记正式键，重试不得换目标。
        self.hub.migrations["codex:old"] = "codex:new"
        connection = self._pair_with_receipts()
        reply = self._call(
            connection,
            protocol.M_INPUT_TEXT,
            {"key": "codex:old", "text": "hi", "submit": True, "command_id": "cmd-canon-1"},
        )
        self.assertTrue(reply["ok"])
        stored = self.service.receipts.get(connection.device_public_key, "cmd-canon-1")
        assert stored is not None
        self.assertEqual(stored.target_key, "codex:new")


class HubRestartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.hub = SessionHub(scan_limit=10)
        self.hub.layout_db = split_layout.SidebarLayoutDB()
        self.addCleanup(self.hub.stop)
        self.session = _running_session()
        self.key = "claude:sess-restart-1"
        self.find = mock.patch.object(
            self.hub.store, "find_session", side_effect=lambda k: self.session if k == self.key else None
        )
        self.find.start()
        self.addCleanup(self.find.stop)
        self.migrations = mock.patch.object(
            self.hub.store, "session_key_migrations", return_value={}
        )
        self.migrations.start()
        self.addCleanup(self.migrations.stop)
        self.get_title = mock.patch.object(
            self.hub.store, "get_title", return_value="重启验收会话"
        )
        self.get_title.start()
        self.addCleanup(self.get_title.stop)
        self.plan = LaunchPlan(argv=("assistant", "--resume"), cwd="/tmp/proj")
        self.build_plan = mock.patch.object(
            self.hub.registry, "build_launch_plan", return_value=self.plan
        )
        self.build_plan.start()
        self.addCleanup(self.build_plan.stop)
        self.available = mock.patch.object(embed, "available", return_value=True)
        self.available.start()
        self.addCleanup(self.available.stop)
        self.host_patcher = mock.patch.object(embed, "host_session", return_value="corral-claude-abc12345")
        self.host = self.host_patcher.start()
        self.addCleanup(self.host_patcher.stop)
        self.close = mock.patch.object(embed, "close_channel", return_value=None)
        self.close.start()
        self.addCleanup(self.close.stop)
        self.forget = mock.patch.object(embed, "forget_alive", return_value=None)
        self.forget.start()
        self.addCleanup(self.forget.stop)
        self.alive = mock.patch.object(embed, "is_alive", return_value=False)
        self.alive.start()
        self.addCleanup(self.alive.stop)
        self.size = mock.patch.object(embed, "pane_size", return_value=(100, 30))
        self.size.start()
        self.addCleanup(self.size.stop)
        self.kill_patcher = mock.patch("corral.remote.sessions.keepalive.kill", return_value=True)
        self.kill_mock = self.kill_patcher.start()
        self.addCleanup(self.kill_patcher.stop)
        self.marked: list[tuple] = []

        def _mark(key, name):
            self.marked.append((key, name))
            if name:
                self.session["keepalive_name"] = name
            else:
                self.session.pop("keepalive_name", None)
            return self.session

        self.mark = mock.patch.object(self.hub.store, "mark_hosted", side_effect=_mark)
        self.mark.start()
        self.addCleanup(self.mark.stop)

    def test_running_restart_replaces_process_retains_identity(self) -> None:
        old_name = self.session["keepalive_name"]
        result = self.hub.restart_session(self.key)
        # 杀的是旧进程，起的是同一 ident（会话 id），落盘历史不动。
        self.kill_mock.assert_called_once_with(old_name)
        self.host.assert_called_once()
        _plan, runtime_id, ident, width, height = self.host.call_args[0]
        self.assertEqual(ident, "sess-restart-1")
        self.assertEqual(runtime_id, "claude")
        # 尺寸沿用旧 pane 真实几何（100x30），而不是默认托管尺寸。
        self.assertEqual((width, height), (100, 30))
        self.assertEqual(self.marked, [(self.key, "corral-claude-abc12345")])
        # 返回同一会话摘要：键/标题/历史路径保留，不是复制也不是接力。
        self.assertEqual(result["key"], self.key)
        self.assertEqual(result["title"], "重启验收会话")

    def test_still_running_old_pane_is_rejected_not_reused(self) -> None:
        from corral.remote.sessions import ActionError

        with mock.patch.object(embed, "is_alive", return_value=True):
            with self.assertRaises(ActionError) as ctx:
                self.hub.restart_session(self.key)
        self.assertEqual(ctx.exception.code, "unavailable")
        # 旧 pane 还活着：不得把同名复用回来的旧进程当成“已重启”返回。
        self.host.assert_not_called()
        self.assertEqual(self.marked, [])
        self.assertEqual(self.session["keepalive_name"], "corral-claude-abc12345")

    def test_ended_session_falls_back_to_native_resume(self) -> None:
        self.session.pop("keepalive_name", None)
        with mock.patch.object(
            self.hub, "resume_session", return_value={"key": self.key}
        ) as resume:
            result = self.hub.restart_session(self.key)
        resume.assert_called_once_with(self.key)
        self.kill_mock.assert_not_called()
        self.host.assert_not_called()
        self.assertEqual(result["key"], self.key)

    def test_provisional_shell_dormant_rejected(self) -> None:
        from corral.remote.sessions import ActionError

        provisional = _running_session(provisional=True)
        for bad in (
            provisional,
            _running_session(source="shell", id="sh-1"),
            _running_session(source="kimi", id="k-1"),
        ):
            with self.subTest(source=bad["source"], provisional=bad.get("provisional")):
                with mock.patch.object(
                    self.hub.store, "find_session", return_value=bad
                ):
                    with self.assertRaises(ActionError) as ctx:
                        self.hub.restart_session("x:y")
                    self.assertEqual(ctx.exception.code, "usage_error")
        self.kill_mock.assert_not_called()
        self.host.assert_not_called()

    def test_preflight_failure_keeps_original_running(self) -> None:
        from corral.remote.sessions import ActionError

        with mock.patch.object(
            self.hub.registry,
            "build_launch_plan",
            side_effect=LaunchError("no resume"),
        ):
            with self.assertRaises(ActionError) as ctx:
                self.hub.restart_session(self.key)
        self.assertEqual(ctx.exception.code, "unavailable")
        # 计划都生成不出来：原进程必须原样保留，标记不动。
        self.kill_mock.assert_not_called()
        self.host.assert_not_called()
        self.assertEqual(self.marked, [])
        self.assertEqual(self.session["keepalive_name"], "corral-claude-abc12345")

    def test_host_failure_reports_without_faking_success(self) -> None:
        from corral.remote.sessions import ActionError

        with mock.patch.object(
            embed, "host_session", side_effect=embed.EmbedError("spawn blew up")
        ):
            with self.assertRaises(ActionError) as ctx:
                self.hub.restart_session(self.key)
        self.assertEqual(ctx.exception.code, "unavailable")
        # 失败不碰托管标记：不伪装成功，下一轮扫描按真实存活纠正。
        self.assertEqual(self.marked, [])

    def test_tmux_missing_unavailable(self) -> None:
        from corral.remote.sessions import ActionError

        with mock.patch.object(embed, "available", return_value=False):
            with self.assertRaises(ActionError) as ctx:
                self.hub.restart_session(self.key)
        self.assertEqual(ctx.exception.code, "unavailable")
        self.kill_mock.assert_not_called()

    def test_concurrent_restarts_are_serialized(self) -> None:
        current = {"n": 0, "peak": 0}
        peak_lock = threading.Lock()

        def _host(plan, runtime_id, ident, width, height):
            with peak_lock:
                current["n"] += 1
                current["peak"] = max(current["peak"], current["n"])
            import time as _time

            _time.sleep(0.05)
            with peak_lock:
                current["n"] -= 1
            return "corral-claude-abc12345"

        with mock.patch.object(embed, "host_session", side_effect=_host):
            errors: list[BaseException] = []

            def _run() -> None:
                try:
                    self.hub.restart_session(self.key)
                except BaseException as exc:  # noqa: BLE001 — surface thread failures
                    errors.append(exc)

            threads = [threading.Thread(target=_run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
        # 同名重启互斥：任何时刻最多一个在杀旧起新，避免并行复用未死透旧名。
        self.assertEqual(current["peak"], 1)


class RetirementContractTests(unittest.TestCase):
    """占位转正必须让手机拿到正式元数据：版本指纹变化 + 旧键可解。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.hub = SessionHub(scan_limit=10)
        self.hub.layout_db = split_layout.SidebarLayoutDB()
        self.addCleanup(self.hub.stop)

    def test_refresh_marks_changed_and_snapshot_carries_formal(self) -> None:
        """真实合并路径：scan→退役→refresh 报 changed→快照带正式卡。

        只比 `_window_version` 不够：这里断言 `store.refresh()` 本身返回
        True（刷新循环推全量 `sessions` 事件的触发条件），以及快照里正式键
        带标题/首尾摘录、占位旧键消失。
        """
        import corral.store as store_module
        from corral.models import session_key

        provisional = self.hub.store.register_hosted_session(
            runtime_id="codex",
            keepalive_name="corral-codex-d5db5d4a",
            title="Codex · 新会话",
            cwd="/tmp/proj",
            ident="d5db5d4a",
        )
        old_key = session_key(provisional)
        formal = _running_session(
            source="codex",
            sid="01a0fc1b-5ebd-74f1-a2db-d7b308bf1702",
            short_id="01a0fc1b",
            title="",
            last_user="在吗",
            last_agent="在，你说。",
        )
        formal["keepalive_name"] = "corral-codex-d5db5d4a"
        formal["fallback_title"] = "无法给 Codex 发送消息的原因"
        with (
            mock.patch.object(self.hub.registry, "scan_all", return_value={"codex": [formal]}),
            mock.patch.object(store_module.liveness, "annotate"),
            mock.patch.object(store_module.liveness, "list_managed_hosts", return_value=[]),
            mock.patch.object(store_module.liveness, "is_alive", return_value=True),
        ):
            changed = self.hub.store.refresh()
        self.assertTrue(changed)
        snapshot = self.hub.list_snapshot()
        self.assertFalse(snapshot["unchanged"])
        keys = [item["key"] for item in snapshot["sessions"]]
        self.assertIn(session_key(formal), keys)
        self.assertNotIn(old_key, keys)
        card = next(item for item in snapshot["sessions"] if item["key"] == session_key(formal))
        self.assertEqual(card["title"], "无法给 Codex 发送消息的原因")
        self.assertEqual(card["last_user"], "在吗")
        self.assertEqual(card["last_agent"], "在，你说。")

    def test_retired_snapshot_reaches_list_subscriber(self) -> None:
        """订阅者事件证据：刷新循环推的那包快照原样送达看列表的手机。"""
        import corral.store as store_module
        from corral.models import session_key
        from corral.remote import protocol
        from corral.remote.service import Connection, RemoteService

        provisional = self.hub.store.register_hosted_session(
            runtime_id="codex",
            keepalive_name="corral-codex-95110b57",
            title="Codex · 新会话",
            cwd="/tmp/proj",
            ident="95110b57",
        )
        old_key = session_key(provisional)
        formal = _running_session(
            source="codex",
            sid="01a0fc1d-b245-7862-b2f4-bd21d5564951",
            short_id="01a0fc1d",
            title="",
            last_user="在吗",
            last_agent="在，你说。",
        )
        formal["keepalive_name"] = "corral-codex-95110b57"
        formal["fallback_title"] = "确认在线状态"
        with (
            mock.patch.object(self.hub.registry, "scan_all", return_value={"codex": [formal]}),
            mock.patch.object(store_module.liveness, "annotate"),
            mock.patch.object(store_module.liveness, "list_managed_hosts", return_value=[]),
            mock.patch.object(store_module.liveness, "is_alive", return_value=True),
        ):
            self.assertTrue(self.hub.store.refresh())
        service = RemoteService(self.hub)
        try:
            sent: list[dict] = []
            connection = Connection("dd" * 32, sent.append)
            service.attach(connection)
            self.hub.watch_sessions()
            service._subscribe(connection, protocol.CH_SESSIONS)
            # 与 `_refresh_loop` 逐字相同的推送：changed/title/marker 任一成立即推全量。
            service._dispatch_event(protocol.CH_SESSIONS, self.hub.list_snapshot())
            self.assertEqual(len(sent), 1)
            event = sent[0]
            self.assertEqual(event["c"], protocol.CH_SESSIONS)
            keys = [item["key"] for item in event["d"]["sessions"]]
            self.assertIn(session_key(formal), keys)
            self.assertNotIn(old_key, keys)
        finally:
            service.detach(connection)

    def test_retirement_changes_list_version_and_resolves_old_key(self) -> None:
        from corral.models import session_key

        provisional = self.hub.store.register_hosted_session(
            runtime_id="codex",
            keepalive_name="corral-codex-d5db5d4a",
            title="Codex · 新会话",
            cwd="/tmp/proj",
            ident="d5db5d4a",
        )
        old_key = session_key(provisional)
        formal = _running_session(
            source="codex",
            sid="01a0fc1b-5ebd-74f1-a2db-d7b308bf1702",
            short_id="01a0fc1b",
            title="",
            last_user="在吗",
            last_agent="在，你说。",
        )
        formal["keepalive_name"] = "corral-codex-d5db5d4a"
        formal["fallback_title"] = "无法给 Codex 发送消息的原因"
        layout = self.hub._layout()
        with mock.patch.object(self.hub.store, "get_title", side_effect=lambda s: s.get("fallback_title") or ""):
            before = self.hub._window_version([provisional], layout)
            after = self.hub._window_version([formal], layout)
        # 版本必须翻：否则 since_version 命中 unchanged，手机永远拿不到正式卡。
        self.assertNotEqual(before, after)
        # 转正：同一托管名被正式会话认领，占位退役并记录迁移。
        with self.hub.store.lock:
            migrations = self.hub.store._reconcile_provisional_sessions({"codex": [formal]})
        self.assertTrue(migrations)
        self.assertEqual(self.hub.store.session_key_migrations().get(old_key), session_key(formal))
        # 旧键读写走正式会话：打开的详情与输入不断流。
        self.assertEqual(self.hub.resolve_session_key(old_key), session_key(formal))
        self.assertIs(self.hub.require_session(old_key), formal)


if __name__ == "__main__":
    unittest.main()
