"""corral.remote.sessions：会话载荷字段与置顶/搜索/删除后布局一致性。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import split_layout
from corral.embed import Cell, InjectionResult
from corral.remote import sessions as remote_sessions
from corral.remote.screen import ScreenEncoder
from corral.remote.sessions import SessionHub


def _session(
    *,
    source: str = "claude",
    sid: object = "abc123",
    short_id: object = "abc123",
    title: str = "示例会话",
    cwd: str = "/tmp/proj",
    mtime: object = 1_700_000_000.0,
    attention: str = "none",
    last_user: str = "你好",
    last_agent: str = "好的",
) -> dict:
    return {
        "source": source,
        "id": sid,
        "short_id": short_id,
        "cwd": cwd,
        "cwd_display": cwd,
        "mtime": mtime,
        "display_time": "01-01 12:00",
        "size_kb": 1.5,
        "status_tag": "ended",
        "live": False,
        "keepalive_name": None,
        "fallback_title": title,
        "attention_kind": attention,
        "last_user_msg": last_user,
        "last_agent_msg": last_agent,
        "path": "/tmp/hist.jsonl",
    }


class SessionHubPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"CORRAL_CACHE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.hub = SessionHub(scan_limit=10)
        self.hub.layout_db = split_layout.SidebarLayoutDB()

    def tearDown(self) -> None:
        self.hub.stop()

    def test_projects_with_unknown_directory_can_cross_wire(self) -> None:
        from corral.projects import project_entries
        from corral.remote import protocol

        entries = project_entries(
            {"claude": [_session(cwd=""), _session(cwd="/workspace/example")]},
            scan_filesystem=False,
        )
        with mock.patch.object(self.hub.store, "projects", return_value=entries):
            payload = self.hub.projects()
        decoded = protocol.loads(protocol.dumps(protocol.response(1, {"projects": payload})))
        projects = decoded["d"]["projects"]
        self.assertEqual(len(projects), 2)
        unknown = next(item for item in projects if not item["path"])
        self.assertIsInstance(unknown["name"], str)
        self.assertTrue(unknown["name"])
        self.assertEqual(unknown["name"], unknown["label"])
        self.assertEqual(unknown["cwd"], "")
        known = next(item for item in projects if item["path"])
        self.assertEqual(known["name"], "example")

    def test_session_payload_coerces_id_and_mtime_for_ios_decoder(self) -> None:
        """手机端 id/short_id 按 String、mtime 按 Double 解码；类型错会整表空白。"""
        session = _session(sid=42, short_id=42, mtime="1700000000")
        with mock.patch.object(self.hub.store, "get_title", return_value="标题"):
            payload = self.hub.session_payload(session, None)
        self.assertEqual(payload["id"], "42")
        self.assertEqual(payload["short_id"], "42")
        self.assertEqual(payload["mtime"], 1_700_000_000.0)
        self.assertIsInstance(payload["pinned"], bool)
        self.assertFalse(payload["pinned"])
        self.assertEqual(payload["attention"], "none")

    def test_list_marker_matches_tui_active_marker(self) -> None:
        """Phone dots come from the TUI rule, never from a merely live process."""
        import time as _time

        now = _time.time()
        idle_live = _session(sid="idle", attention="none", mtime=now - 3600)
        idle_live["live"] = True
        idle_live["keepalive_name"] = "pane-idle"
        recent = _session(sid="recent", attention="none", mtime=now - 5)
        recent["live"] = True
        recent["keepalive_name"] = "pane-recent"
        external = _session(sid="external", attention="none", mtime=now - 5)
        external["live"] = True
        working = _session(sid="working", attention="working", mtime=now - 3600)
        with mock.patch.object(self.hub.store, "get_title", return_value="t"):
            markers = {
                item["id"]: self.hub.session_payload(item, None)["marker"]
                for item in (idle_live, recent, external, working)
            }
        self.assertEqual(
            markers,
            {"idle": "", "recent": "recent", "external": "", "working": "working"},
        )

    def test_marker_expiry_is_detected_without_history_change(self) -> None:
        import time as _time

        session = _session(sid="m", attention="none", mtime=_time.time() - 5)
        session["keepalive_name"] = "pane-m"
        self.hub.store.sessions = {"claude": [session]}
        self.assertTrue(self.hub._detect_marker_changes())
        self.assertFalse(self.hub._detect_marker_changes())
        session["mtime"] = _time.time() - 3600
        self.assertTrue(self.hub._detect_marker_changes())

    def test_open_detail_receives_marker_without_list_subscription(self) -> None:
        import time as _time

        from sesskit.titles import STATUS_ABORTED

        events = []
        self.hub._on_event = lambda channel, data: events.append((channel, data))
        session = _session(sid="quota", attention="none", mtime=_time.time())
        session.update(live=True, keepalive_name="pane-quota")
        self.hub.store.sessions = {"claude": [session]}
        # Canonical key differs after a provisional session is retired.
        watch = remote_sessions._ConversationWatch("claude:placeholder", mock.Mock())
        watch.canonical_key = "claude:quota"
        watch.watchers = 1
        self.hub._conversations[watch.key] = watch
        self.assertEqual(self.hub._sessions_watchers, 0)
        self.hub._detect_marker_changes()
        self.assertEqual(events[-1][1]["summary"]["marker"], "recent")
        events.clear()
        session["status_tag"] = STATUS_ABORTED
        self.assertTrue(self.hub._detect_marker_changes())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "session:claude:placeholder")
        self.assertEqual(events[0][1]["kind"], "metadata")
        self.assertEqual(events[0][1]["summary"]["marker"], "")
        self.assertEqual(events[0][1]["summary"]["attention"], "none")
        self.assertTrue(events[0][1]["summary"]["live"])
        events.clear()
        self.assertFalse(self.hub._detect_marker_changes())
        self.assertEqual(events, [])
        session["attention_kind"] = "working"
        self.hub._detect_marker_changes()
        self.assertEqual(events[-1][1]["summary"]["marker"], "working")

    def test_recent_expiry_updates_open_detail_metadata(self) -> None:
        import time as _time

        events = []
        self.hub._on_event = lambda channel, data: events.append((channel, data))
        session = _session(sid="recent", mtime=_time.time())
        session["keepalive_name"] = "pane-recent"
        self.hub.store.sessions = {"claude": [session]}
        watch = remote_sessions._ConversationWatch("claude:recent", mock.Mock())
        watch.watchers = 1
        self.hub._conversations[watch.key] = watch
        self.hub._detect_marker_changes()
        events.clear()
        session["mtime"] = _time.time() - 3600
        self.hub._detect_marker_changes()
        self.assertEqual(events[-1][1]["summary"]["marker"], "")

    def test_unchanged_capture_skips_screen_parsing_and_encoding(self) -> None:
        session = _session(sid="live")
        session["keepalive_name"] = "pane-live"
        watch = remote_sessions._ScreenWatch("claude:live", ScreenEncoder())
        state = (0, 0, True, False, False, 0, 80, 24)
        grid = [[Cell(ch="o"), Cell(ch="k")]]
        with (
            mock.patch.object(self.hub.store, "find_session", return_value=session),
            mock.patch.object(remote_sessions.embed, "pane_state", return_value=state),
            mock.patch.object(remote_sessions.embed, "capture", return_value="ok"),
            mock.patch.object(remote_sessions.embed, "parse_screen", return_value=grid) as parse_screen,
        ):
            self.assertIsNotNone(self.hub._capture_frame(watch))
            self.assertIsNone(self.hub._capture_frame(watch))
        parse_screen.assert_called_once_with("ok", 80, 24)

    def test_session_payload_carries_no_group_for_phone(self) -> None:
        """移动端没有分组概念：载荷不再带 group，桌面整组置顶也不得带进来。"""
        layout = split_layout.SplitLayoutStore()
        layout.set_group("/tmp/proj", ["claude:a", "codex:b"])
        layout.toggle_group_pin(layout.get_group("claude:a").group_id)
        session = _session(sid="a")
        with mock.patch.object(self.hub.store, "get_title", return_value="A"):
            payload = self.hub.session_payload(session, layout)
        self.assertNotIn("group", payload)
        self.assertFalse(payload["pinned"])

    def test_toggle_pin_independent_session_returns_true_then_false(self) -> None:
        self.assertTrue(self.hub.toggle_pin("claude:solo"))
        layout = self.hub.layout_db.read()
        self.assertIn("claude:solo", layout.pinned_session_keys)
        self.assertFalse(self.hub.toggle_pin("claude:solo"))
        layout = self.hub.layout_db.read()
        self.assertNotIn("claude:solo", layout.pinned_session_keys)

    def test_toggle_pin_group_member_pins_only_that_session(self) -> None:
        """移动端 pin 一条只钉那一条：桌面分屏组、整组置顶都不参与。"""
        self.hub.layout_db.set_group("/tmp/proj", ["claude:a", "codex:b"])
        self.assertTrue(self.hub.toggle_pin("claude:a"))
        # 移动端读必须走不提升的快照：默认 read() 的 normalize 会把成员独立钉
        # 提升成整组置顶（桌面侧栏展示用），不能拿它断言手机置顶状态。
        layout = self.hub.layout_db.read_with_promote(skip_promote=True)
        gid = layout.get_group("claude:a").group_id
        self.assertIn("claude:a", layout.pinned_session_keys)
        self.assertNotIn(gid, layout.pinned_group_ids)
        self.assertNotIn("codex:b", layout.pinned_session_keys)
        # 手机列表只靠独立置顶归入置顶区
        session = _session(sid="a")
        with mock.patch.object(self.hub.store, "get_title", return_value="A"):
            payload = self.hub.session_payload(session, layout)
        self.assertTrue(payload["pinned"])

    def test_toggle_pin_twice_unpins_group_member_session(self) -> None:
        """组内会话 pin 两次回到未置顶，不得触碰桌面整组置顶。"""
        self.hub.layout_db.set_group("/tmp/proj", ["claude:a", "codex:b"])
        self.assertTrue(self.hub.toggle_pin("claude:a"))
        self.assertFalse(self.hub.toggle_pin("claude:a"))
        layout = self.hub.layout_db.read_with_promote(skip_promote=True)
        gid = layout.get_group("claude:a").group_id
        self.assertNotIn("claude:a", layout.pinned_session_keys)
        self.assertNotIn(gid, layout.pinned_group_ids)

    def test_desktop_write_does_not_persist_phone_pin_as_group_pin(self) -> None:
        """手机 pin 只是独立钉：别的桌面写操作不得把它固化成显式组钉。"""
        self.hub.layout_db.set_group("/tmp/proj", ["claude:a", "codex:b"])
        self.assertTrue(self.hub.toggle_pin("claude:a"))
        gid = self.hub.layout_db.read().get_group("claude:a").group_id
        self.hub.layout_db.set_collapsed(gid, True)
        self.assertFalse(self.hub.toggle_pin("claude:a"))
        layout = self.hub.layout_db.read()
        self.assertNotIn(gid, layout.pinned_group_ids)
        self.assertNotIn("claude:a", layout.pinned_session_keys)

    def test_desktop_explicit_group_pin_survives_phone_pin_cycle(self) -> None:
        """桌面显式钉整组：手机 pin/unpin 同组另一条不得清掉它。"""
        self.hub.layout_db.set_group("/tmp/proj", ["claude:a", "codex:b"])
        gid = self.hub.layout_db.read().get_group("claude:a").group_id
        snapshot = self.hub.layout_db.toggle_group_pin(gid)
        self.assertIn(gid, snapshot.pinned_group_ids)
        self.assertTrue(self.hub.toggle_pin("claude:a"))
        phone = self.hub.layout_db.read_with_promote(skip_promote=True)
        self.assertIn("claude:a", phone.pinned_session_keys)
        self.assertFalse(self.hub.toggle_pin("claude:a"))
        phone = self.hub.layout_db.read_with_promote(skip_promote=True)
        self.assertNotIn("claude:a", phone.pinned_session_keys)
        layout = self.hub.layout_db.read()
        self.assertIn(gid, layout.pinned_group_ids)

    def test_desktop_group_pin_does_not_leak_into_phone_list(self) -> None:
        """桌面显式钉整组：手机列表仍按独立钉判定，不进置顶区。"""
        self.hub.layout_db.set_group("/tmp/proj", ["claude:a", "codex:b"])
        gid = self.hub.layout_db.read().get_group("claude:a").group_id
        snapshot = self.hub.layout_db.toggle_group_pin(gid)
        self.assertIn(gid, snapshot.pinned_group_ids)
        sessions = [_session(sid="a"), _session(sid="b", source="codex")]
        with (
            mock.patch.object(self.hub.store, "all_sessions", return_value=sessions),
            mock.patch.object(
                self.hub.store, "get_title", side_effect=lambda item: item["fallback_title"]
            ),
        ):
            listed = self.hub.list_sessions()
        by_key = {item["key"]: item for item in listed}
        self.assertFalse(by_key["claude:a"]["pinned"])
        self.assertFalse(by_key["codex:b"]["pinned"])
        self.assertNotIn("group", by_key["claude:a"])
        phone_layout = self.hub.layout_db.read_with_promote(skip_promote=True)
        self.assertFalse(remote_sessions._session_is_priority(sessions[0], phone_layout))

    def test_list_search_ignores_desktop_group_name(self) -> None:
        sessions = [
            _session(sid="a", title="alpha"),
            _session(source="codex", sid="b", title="beta"),
        ]
        with (
            mock.patch.object(self.hub.store, "all_sessions", return_value=sessions),
            mock.patch.object(self.hub.store, "get_title", side_effect=lambda s: s["fallback_title"]),
        ):
            limited = self.hub.list_sessions(limit=1)
            self.assertEqual(len(limited), 1)
            miss = self.hub.list_sessions(query="zzz-no-match")
            self.assertEqual(miss, [])

    def test_default_list_keeps_waiting_and_caps_idle_history(self) -> None:
        """手机首包不能把几百条闲置历史整表塞出去，但等待中/置顶必须留下。"""
        sessions = [
            _session(sid=f"idle{index}", title=f"闲置 {index}", mtime=1_700_000_000 + index)
            for index in range(120)
        ]
        sessions.append(_session(sid="wait", title="等你回答", attention="waiting", mtime=1))
        sessions.append(_session(sid="pin", title="置顶", mtime=2))
        self.hub.layout_db.toggle_session_pin("claude:pin")
        with (
            mock.patch.object(self.hub.store, "all_sessions", return_value=sessions),
            mock.patch.object(
                self.hub.store, "get_title", side_effect=lambda item: item["fallback_title"]
            ),
        ):
            listed = self.hub.list_sessions()
            searched = self.hub.list_sessions(query="等你回答")
        keys = {item["key"] for item in listed}
        self.assertIn("claude:wait", keys)
        self.assertIn("claude:pin", keys)
        self.assertEqual(len(listed), remote_sessions._PHONE_LIST_LIMIT)
        self.assertEqual([item["key"] for item in searched], ["claude:wait"])

    def test_default_list_builds_payloads_only_for_the_window(self) -> None:
        """截窗必须发生在组摘要之前，不能先为几百条闲置会话做完整打包。"""
        sessions = [
            _session(sid=f"idle{index}", title=f"闲置 {index}", mtime=1_700_000_000 + index)
            for index in range(120)
        ]
        calls = {"n": 0}
        real = SessionHub.session_payload

        def wrapped(hub, session, layout=None):
            calls["n"] += 1
            return real(hub, session, layout)

        with (
            mock.patch.object(self.hub.store, "all_sessions", return_value=sessions),
            mock.patch.object(
                self.hub.store, "get_title", side_effect=lambda item: item["fallback_title"]
            ),
            mock.patch.object(SessionHub, "session_payload", wrapped),
        ):
            listed = self.hub.list_sessions()
        self.assertEqual(len(listed), remote_sessions._PHONE_LIST_LIMIT)
        self.assertEqual(calls["n"], remote_sessions._PHONE_LIST_LIMIT)

    def test_list_snapshot_skips_sessions_when_version_matches(self) -> None:
        sessions = [_session(sid="a", title="alpha"), _session(sid="b", title="beta")]
        calls = {"n": 0}
        real = SessionHub.session_payload

        def wrapped(hub, session, layout=None):
            calls["n"] += 1
            return real(hub, session, layout)

        with (
            mock.patch.object(self.hub.store, "all_sessions", return_value=sessions),
            mock.patch.object(
                self.hub.store, "get_title", side_effect=lambda item: item["fallback_title"]
            ),
        ):
            first = self.hub.list_snapshot()
            payload_calls_after_first = 0
            with mock.patch.object(SessionHub, "session_payload", wrapped):
                again = self.hub.list_snapshot(since_version=str(first["version"]))
                payload_calls_after_first = calls["n"]
        self.assertFalse(first["unchanged"])
        self.assertEqual(len(first["sessions"]), 2)
        self.assertTrue(again["unchanged"])
        self.assertNotIn("sessions", again)
        self.assertEqual(again["version"], first["version"])
        self.assertEqual(payload_calls_after_first, 0)

    def test_delete_session_removes_layout_membership(self) -> None:
        self.hub.layout_db.set_group("/tmp/proj", ["claude:a", "codex:b"])
        self.hub.layout_db.toggle_session_pin("claude:solo")
        session = _session(sid="a")
        runtime = mock.Mock()
        with (
            mock.patch.object(self.hub, "require_session", return_value=session),
            mock.patch.object(self.hub, "_runtime_of", return_value=runtime),
            mock.patch.object(self.hub.store, "mark_deleted"),
            mock.patch.object(self.hub.store, "abort_delete"),
        ):
            self.hub.delete_session("claude:a")
        runtime.delete_session.assert_called_once_with(session)
        layout = self.hub.layout_db.read()
        # 只剩一个成员时应解散组
        self.assertIsNone(layout.get_group("codex:b"))
        self.assertEqual(layout.groups, {})

    def test_second_conversation_watcher_still_gets_history(self) -> None:
        """第二路 session.watch 也必须拿到首屏窗口，不能因为共享订阅计数变成空列表。"""
        path = Path(self._tmp.name) / "claude.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "你好"}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            first = self.hub.watch_conversation("claude:a")
            second = self.hub.watch_conversation("claude:a")
        self.assertEqual(len(first["messages"]), 1)
        self.assertEqual(first["messages"][0]["text"], "你好")
        self.assertEqual(len(second["messages"]), 1)
        self.assertEqual(second["messages"][0]["text"], "你好")
        self.assertEqual(first["has_more"], False)
        self.hub.unwatch_conversation("claude:a")
        self.hub.unwatch_conversation("claude:a")

    def test_message_page_is_bounded_and_supports_before_cursor(self) -> None:
        items = [
            remote_sessions.richmsg.RichMessage(index, "assistant", f"消息 {index}")
            for index in range(1, 7)
        ]
        page = remote_sessions._message_page(items, limit=3)
        self.assertEqual([item["seq"] for item in page["messages"]], [4, 5, 6])
        self.assertTrue(page["has_more"])
        self.assertEqual(page["oldest_seq"], 4)
        self.assertEqual(page["from"], 4)
        self.assertEqual(page["to"], 6)
        self.assertEqual(page["total"], 6)
        self.assertEqual(page["generation"], 1)

        earlier = remote_sessions._message_page(items, limit=3, before_seq=4)
        self.assertEqual([item["seq"] for item in earlier["messages"]], [1, 2, 3])
        self.assertFalse(earlier["has_more"])

    def test_tool_detail_returns_bodies_omitted_from_wire(self) -> None:
        path = Path(self._tmp.name) / "claude-tools.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "assistant",
                            "uuid": "a1",
                            "message": {
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": "改完了"},
                                    {
                                        "type": "tool_use",
                                        "id": "toolu_1",
                                        "name": "Bash",
                                        "input": {"command": "echo hello"},
                                    },
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "user",
                            "uuid": "u1",
                            "message": {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "toolu_1",
                                        "content": "hello\n",
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        session = _session(sid="tools")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            page = self.hub.message_page("claude:tools")
            wire_tools = page["messages"][0].get("tools") or []
            self.assertTrue(wire_tools)
            self.assertNotIn("output", wire_tools[0])
            seq = page["messages"][0]["seq"]
            detail = self.hub.tool_detail("claude:tools", seq=seq)
        self.assertGreaterEqual(detail["total"], 1)
        self.assertIn("hello", detail["tools"][0].get("output", ""))

    def test_opening_session_parses_history_once(self) -> None:
        path = Path(self._tmp.name) / "claude.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "你好"}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        session = _session(sid="a")
        session["path"] = str(path)
        reads = {"all": 0}
        original = remote_sessions.richmsg.RichReader.read_all

        def counting(reader, *args, **kwargs):
            reads["all"] += 1
            return original(reader, *args, **kwargs)

        with mock.patch.object(self.hub, "require_session", return_value=session):
            with mock.patch.object(
                remote_sessions.richmsg.RichReader, "read_all", counting
            ):
                first = self.hub.watch_conversation("claude:a")
                page = self.hub.message_page("claude:a")
                prompts = self.hub.prompts("claude:a")
        self.assertEqual(reads["all"], 1)
        self.assertEqual(len(first["messages"]), 1)
        self.assertEqual(len(page["messages"]), 1)
        self.assertEqual(first["generation"], page["generation"])
        self.assertEqual(prompts, [])

    def test_disk_cache_avoids_full_reread_on_new_hub(self) -> None:
        path = Path(self._tmp.name) / "claude.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "缓存命中"}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            self.hub.watch_conversation("claude:a")

        hub2 = SessionHub(scan_limit=10)
        self.addCleanup(hub2.stop)
        reads = {"all": 0}
        original = remote_sessions.richmsg.RichReader.read_all

        def counting(reader, *args, **kwargs):
            reads["all"] += 1
            return original(reader, *args, **kwargs)

        with mock.patch.object(hub2, "require_session", return_value=session):
            with mock.patch.object(
                remote_sessions.richmsg.RichReader, "read_all", counting
            ):
                page = hub2.message_page("claude:a")
        self.assertEqual(reads["all"], 0)
        self.assertEqual(page["messages"][0]["text"], "缓存命中")

    def test_watch_without_after_seq_returns_tail(self) -> None:
        """旧手机不传 after_seq，仍拿当前尾部窗口。"""
        path = Path(self._tmp.name) / "claude.jsonl"
        _write_assistant_jsonl(path, ["一", "二", "三", "四", "五"])
        session = _session(sid="a")
        session["path"] = str(path)
        session.update(attention_kind="working", live=True)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            page = self.hub.watch_conversation("claude:a", limit=3)
        self.assertEqual(page["resume"], "tail")
        self.assertEqual((page["attention"], page["live"]), ("working", True))
        self.assertEqual([item["seq"] for item in page["messages"]], [3, 4, 5])
        self.assertEqual(page["kind"], "snapshot")
        self.hub.unwatch_conversation("claude:a")

    def test_watch_same_generation_replays_only_the_gap(self) -> None:
        """generation 相同且缺口还在规范化缓存里时，只返回 after_seq 之后的更新。"""
        path = Path(self._tmp.name) / "claude.jsonl"
        _write_assistant_jsonl(path, ["一", "二", "三", "四", "五"])
        session = _session(sid="a")
        session["path"] = str(path)
        session.update(attention_kind="working", live=True)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            first = self.hub.watch_conversation("claude:a")
            self.hub.unwatch_conversation("claude:a")
            gap = self.hub.watch_conversation(
                "claude:a",
                after_seq=3,
                generation=first["generation"],
            )
        self.assertEqual(first["resume"], "tail")
        self.assertEqual(gap["resume"], "replay")
        self.assertEqual((gap["attention"], gap["live"]), ("working", True))
        self.assertEqual([item["seq"] for item in gap["messages"]], [4, 5])
        self.assertEqual(gap["generation"], first["generation"])
        self.hub.unwatch_conversation("claude:a")

    def test_watch_generation_mismatch_returns_tail(self) -> None:
        """手机记下的 generation 对不上时，退回当前尾部，不得假装回放。"""
        path = Path(self._tmp.name) / "claude.jsonl"
        _write_assistant_jsonl(path, ["一", "二", "三"])
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            first = self.hub.watch_conversation("claude:a")
            self.hub.unwatch_conversation("claude:a")
            page = self.hub.watch_conversation(
                "claude:a",
                after_seq=1,
                generation=first["generation"] + 9,
            )
        self.assertEqual(page["resume"], "tail")
        self.assertEqual(len(page["messages"]), 3)
        self.hub.unwatch_conversation("claude:a")

    def test_empty_replay_is_not_a_clear(self) -> None:
        """已追上时 replay 的 messages 为空，表示没有新缺口，不是让手机清空。"""
        path = Path(self._tmp.name) / "claude.jsonl"
        _write_assistant_jsonl(path, ["一", "二"])
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            first = self.hub.watch_conversation("claude:a")
            self.hub.unwatch_conversation("claude:a")
            caught_up = self.hub.watch_conversation(
                "claude:a",
                after_seq=first["newest_seq"],
                generation=first["generation"],
            )
        self.assertEqual(caught_up["resume"], "replay")
        self.assertEqual(caught_up["messages"], [])
        self.assertEqual(first["messages"][0]["text"], "一")
        self.hub.unwatch_conversation("claude:a")

    def test_delta_buffer_overflow_falls_back_to_tail(self) -> None:
        """有界缓冲溢出后，旧序号不可回放，改为当前尾部。"""
        path = Path(self._tmp.name) / "claude.jsonl"
        _write_assistant_jsonl(path, ["一", "二", "三"])
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            first = self.hub.watch_conversation("claude:a")
            watch = self.hub._conversations["claude:a"]
            watch.deltas = remote_sessions._DeltaBuffer(maxlen=3)
            watch.deltas.append(
                [
                    remote_sessions.richmsg.RichMessage(index, "assistant", f"增量 {index}")
                    for index in range(1, 6)
                ]
            )
            page = self.hub.watch_conversation(
                "claude:a",
                after_seq=1,
                generation=first["generation"],
            )
        self.assertEqual(page["resume"], "tail")
        self.assertEqual(len(page["messages"]), 3)
        self.hub.unwatch_conversation("claude:a")
        self.hub.unwatch_conversation("claude:a")

    def test_large_history_opens_tail_and_pages_earlier_without_full_parse(self) -> None:
        # P1 新契约：SessKit 冷启动做一次完整解释（不再按字节切块），Corral
        # 只返回尾部窗口并用稳定全局序号翻页；无变更 poll 只 stat 文件。
        path = Path(self._tmp.name) / "claude.jsonl"
        total = 4000
        _write_assistant_jsonl(path, [f"尾部消息-{index}" for index in range(total)])
        session = _session(sid="a")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            page = self.hub.watch_conversation("claude:a")
            reader = self.hub._transcripts["claude:a"].reader
            self.assertTrue(page["has_more"])
            self.assertEqual(page["messages"][-1]["text"], f"尾部消息-{total - 1}")
            self.assertEqual(len(page["messages"]), 80)
            self.assertEqual(page["messages"][0]["text"], f"尾部消息-{total - 80}")

            earlier = self.hub.message_page("claude:a", before_seq=page["oldest_seq"])
            self.assertEqual(len(earlier["messages"]), 80)
            self.assertEqual(earlier["messages"][-1]["text"], f"尾部消息-{total - 81}")
            self.assertLess(earlier["messages"][-1]["seq"], page["oldest_seq"])

            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "uuid": "new",
                            "message": {
                                "role": "assistant",
                                "content": [{"type": "text", "text": "追加一条"}],
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            added = reader.poll()
        self.assertEqual([item.text for item in added], ["追加一条"])
        self.hub.unwatch_conversation("claude:a")

    def test_user_prompts_cover_whole_session_beyond_loaded_window(self) -> None:
        """Your prompts lists every human prompt, not just the paged-in tail."""
        path = Path(self._tmp.name) / "claude-prompts.jsonl"
        lines = []
        for index in range(600):
            lines.append({
                "type": "user",
                "uuid": f"q{index}",
                "message": {"role": "user", "content": f"提问-{index}"},
            })
            lines.append({
                "type": "assistant",
                "uuid": f"a{index}",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "回答"}],
                },
            })
        lines.append({
            "type": "user",
            "uuid": "long",
            "message": {"role": "user", "content": "长" * 2000},
        })
        path.write_text(
            "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
            encoding="utf-8",
        )
        session = _session(sid="p")
        session["path"] = str(path)
        with mock.patch.object(self.hub, "require_session", return_value=session):
            page = self.hub.watch_conversation("claude:p")
            self.assertTrue(page["has_more"])
            result = self.hub.user_prompts("claude:p")
            texts = [row["text"] for row in result["prompts"]]
            self.assertEqual(texts[0], "提问-0")
            self.assertEqual(texts[599], "提问-599")
            self.assertEqual(result["total"], 601)
            self.assertTrue(texts[-1].endswith("…"))
            self.assertLessEqual(len(texts[-1]), remote_sessions.USER_PROMPT_TEXT_LIMIT + 1)
            seqs = [row["seq"] for row in result["prompts"]]
            self.assertEqual(seqs, sorted(seqs))
            # Seqs share the message-page space: paging earlier reaches the first prompt.
            earliest = self.hub.message_page("claude:p", before_seq=seqs[0] + 1, limit=1)
            self.assertEqual(earliest["messages"][0]["seq"], seqs[0])
            self.assertEqual(earliest["messages"][0]["text"], "提问-0")
        self.hub.unwatch_conversation("claude:p")

    def test_require_session_follows_placeholder_key_migration(self) -> None:
        """占位卡转正后，手机仍拿着旧键也必须能找到会话，不能报已经不在列表里。"""
        real = _session(sid="real-id", title="正式会话")
        self.hub.store.sessions = {"claude": [real]}
        self.hub.store._session_key_migrations["claude:placeholder"] = "claude:real-id"
        found = self.hub.require_session("claude:placeholder")
        self.assertEqual(found["id"], "real-id")
        with self.assertRaises(remote_sessions.ActionError) as raised:
            self.hub.require_session("claude:missing")
        self.assertEqual(raised.exception.code, "not_found")

    def test_resolve_restart_alias_via_keepalive_name(self) -> None:
        """重启丢迁移表后，旧临时键经精确托管名反查到正式会话。"""
        native = _session(
            source="codex",
            sid="01a0f2c1-eac5-7292-acb8-ce6a5ecd1443",
            short_id="01a0f2c1",
            title="正式会话",
        )
        native["keepalive_name"] = "corral-codex-bbe7b248"
        native["live"] = True
        self.hub.store.sessions = {"codex": [native]}
        self.hub.store._session_key_migrations = {}
        resolved = self.hub.resolve_session_key("codex:bbe7b248")
        self.assertEqual(resolved, "codex:01a0f2c1-eac5-7292-acb8-ce6a5ecd1443")
        found = self.hub.require_session("codex:bbe7b248")
        self.assertEqual(found["id"], "01a0f2c1-eac5-7292-acb8-ce6a5ecd1443")

    def test_resolve_restart_alias_unknown_keeps_not_found(self) -> None:
        """托管名对不上时仍报 not_found，禁止 cwd/标题兜底。"""
        native = _session(source="codex", sid="some-other-id", short_id="some-oth")
        native["keepalive_name"] = "corral-codex-aaaaaaaa"
        native["cwd"] = "/Users/geraltgraham/Codes/Corral"
        self.hub.store.sessions = {"codex": [native]}
        self.hub.store._session_key_migrations = {}
        self.assertEqual(self.hub.resolve_session_key("codex:bbe7b248"), "codex:bbe7b248")
        with self.assertRaises(remote_sessions.ActionError) as raised:
            self.hub.require_session("codex:bbe7b248")
        self.assertEqual(raised.exception.code, "not_found")

    def test_resolve_native_prefix_single_and_ambiguous(self) -> None:
        """原生 id 前缀一对一才认领；多条共享前缀时不串台。"""
        solo = _session(
            source="codex",
            sid="01a0f2c1-eac5-7292-acb8-ce6a5ecd1443",
            short_id="01a0f2c1",
        )
        self.hub.store.sessions = {"codex": [solo]}
        self.hub.store._session_key_migrations = {}
        self.assertEqual(
            self.hub.resolve_session_key("codex:01a0f2c1"),
            "codex:01a0f2c1-eac5-7292-acb8-ce6a5ecd1443",
        )
        first = _session(source="codex", sid="01a0f11d-8af0-1111-1111-111111111111", short_id="01a0f11d")
        second = _session(source="codex", sid="01a0f11d-6feb-2222-2222-222222222222", short_id="01a0f11d")
        self.hub.store.sessions = {"codex": [first, second]}
        self.assertEqual(self.hub.resolve_session_key("codex:01a0f11d"), "codex:01a0f11d")

    def test_conversation_watch_rebinding_keeps_phone_channel(self) -> None:
        """转正后实时订阅仍走手机原来的通道，但读取正式历史。"""
        old_path = Path(self._tmp.name) / "old.jsonl"
        old_path.write_text("", encoding="utf-8")
        new_path = Path(self._tmp.name) / "new.jsonl"
        _write_assistant_jsonl(new_path, ["转正后的回复"])
        placeholder = _session(sid="placeholder")
        placeholder["path"] = str(old_path)
        real = _session(sid="real-id")
        real["path"] = str(new_path)
        self.hub.store.sessions = {"claude": [placeholder]}
        page = self.hub.watch_conversation("claude:placeholder")
        self.assertEqual(page["messages"], [])
        self.hub.store.sessions = {"claude": [real]}
        self.hub.store._session_key_migrations["claude:placeholder"] = "claude:real-id"
        self.hub._follow_key_migrations()
        found = self.hub.require_session("claude:placeholder")
        self.assertEqual(found["id"], "real-id")
        watch = self.hub._conversations["claude:placeholder"]
        self.assertEqual(watch.key, "claude:placeholder")
        self.assertEqual(watch.canonical_key, "claude:real-id")
        cached = self.hub._transcripts["claude:real-id"]
        self.assertEqual([item.text for item in cached.messages], ["转正后的回复"])
        with new_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "later",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "后来追加"}],
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        added = watch.reader.poll()
        self.assertEqual([item.text for item in added], ["后来追加"])
        self.hub.unwatch_conversation("claude:placeholder")

    def test_prompts_incremental_poll_publishes_watch_events(self) -> None:
        """watch 之后 prompts/_ensure_transcript 增量读必须推给正在看的通道。"""
        events: list[tuple[str, dict]] = []
        self.hub._on_event = lambda channel, data: events.append((channel, data))
        path = Path(self._tmp.name) / "claude.jsonl"
        _write_assistant_jsonl(path, ["第一句"])
        session = _session(sid="a")
        session["path"] = str(path)
        self.hub.store.sessions = {"claude": [session]}
        page = self.hub.watch_conversation("claude:a")
        self.assertEqual(page["messages"][-1]["text"], "第一句")
        events.clear()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u-new",
                        "message": {"role": "user", "content": "用户新提问"},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "a-new",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "助手新回复"}],
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        self.hub.prompts("claude:a")
        texts = _delta_role_texts(events)
        self.assertEqual({channel for channel, _payload in events}, {"session:claude:a"})
        self.assertIn(("user", "用户新提问"), texts)
        self.assertIn(("assistant", "助手新回复"), texts)
        self.hub.unwatch_conversation("claude:a")

    def test_rebinding_prompts_publish_to_phone_channel(self) -> None:
        """占位卡转正后，增量仍推到手机原来的通道。"""
        events: list[tuple[str, dict]] = []
        self.hub._on_event = lambda channel, data: events.append((channel, data))
        old_path = Path(self._tmp.name) / "old-live.jsonl"
        old_path.write_text("", encoding="utf-8")
        new_path = Path(self._tmp.name) / "new-live.jsonl"
        _write_assistant_jsonl(new_path, ["转正后的回复"])
        placeholder = _session(sid="placeholder")
        placeholder["path"] = str(old_path)
        real = _session(sid="real-id")
        real["path"] = str(new_path)
        self.hub.store.sessions = {"claude": [placeholder]}
        self.hub.watch_conversation("claude:placeholder")
        self.hub.store.sessions = {"claude": [real]}
        self.hub.store._session_key_migrations["claude:placeholder"] = "claude:real-id"
        self.hub._follow_key_migrations()
        events.clear()
        with new_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "later-live",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "后来追加"}],
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        self.hub.prompts("claude:placeholder")
        self.assertEqual({channel for channel, _payload in events}, {"session:claude:placeholder"})
        self.assertIn(("assistant", "后来追加"), _delta_role_texts(events))
        self.hub.unwatch_conversation("claude:placeholder")

    def test_send_text_echoes_user_text_to_phone_channel(self) -> None:
        events: list[tuple[str, dict]] = []
        self.hub._on_event = lambda channel, data: events.append((channel, data))
        session = _session(sid="a")
        session["keepalive_name"] = "pane-a"
        self.hub.store.sessions = {"claude": [session]}
        with (
            mock.patch.object(
                remote_sessions.embed, "paste_detailed", return_value=InjectionResult(True)
            ) as paste,
            mock.patch.object(
                remote_sessions.embed, "send_key_detailed", return_value=InjectionResult(True)
            ) as send_key,
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            self.hub.send_text("claude:a", "你好手机")
            paste.assert_called_once_with("pane-a", "你好手机")
            send_key.assert_called_once_with("pane-a", "Enter")
            self.assertEqual(
                events,
                [
                    (
                        "session:claude:a",
                        {
                            "version": 1,
                            "kind": "attention",
                            "session": "claude:a",
                            "attention": "working",
                            "provisional": True,
                            "live": True,
                        },
                    ),
                    (
                        "session:claude:a",
                        {
                            "version": 1,
                            "kind": "echo",
                            "session": "claude:a",
                            "role": "user",
                            "text": "你好手机",
                        },
                    )
                ],
            )
            events.clear()
            self.hub.send_text("claude:a", "")
        self.assertEqual(events, [])

    def test_send_text_resumes_when_session_not_hosted(self) -> None:
        session = _session(sid="ended")
        self.hub.store.sessions = {"claude": [session]}

        def _fake_resume(key: str) -> dict:
            found = self.hub.store.find_session("claude:ended")
            self.assertIsNotNone(found)
            assert found is not None
            found["keepalive_name"] = "pane-resumed"
            return {"key": key}

        with (
            mock.patch.object(self.hub, "resume_session", side_effect=_fake_resume) as resume,
            mock.patch.object(
                remote_sessions.embed, "paste_detailed", return_value=InjectionResult(True)
            ) as paste,
            mock.patch.object(
                remote_sessions.embed, "send_key_detailed", return_value=InjectionResult(True)
            ) as send_key,
            mock.patch.object(remote_sessions.time, "sleep"),
            mock.patch.object(self.hub, "_wait_pane_settled") as settle,
        ):
            self.hub.send_text("claude:ended", "快点动手实现")
            resume.assert_called_once_with("claude:ended")
            settle.assert_called_once_with("pane-resumed")
            paste.assert_called_once_with("pane-resumed", "快点动手实现")
            send_key.assert_called_once_with("pane-resumed", "Enter")

    def test_send_text_to_hosted_session_does_not_wait_for_settle(self) -> None:
        session = _session(sid="live")
        session["keepalive_name"] = "pane-live"
        self.hub.store.sessions = {"claude": [session]}
        with (
            mock.patch.object(
                remote_sessions.embed, "paste_detailed", return_value=InjectionResult(True)
            ),
            mock.patch.object(
                remote_sessions.embed, "send_key_detailed", return_value=InjectionResult(True)
            ),
            mock.patch.object(remote_sessions.time, "sleep"),
            mock.patch.object(self.hub, "_wait_pane_settled") as settle,
        ):
            self.hub.send_text("claude:live", "继续")
        settle.assert_not_called()

    def test_wait_pane_settled_returns_after_quiet_window(self) -> None:
        frames = ["", "Starting ⠋", "Starting ⠙"] + ["› ready"] * 10
        captured: list[str] = []

        def _capture(*_args: object) -> str:
            captured.append(frames[len(captured)])
            return captured[-1]

        # Two clock reads before the loop, then one per poll, 0.3 s apart.
        clock = iter([0.0, 0.0] + [0.3 * n for n in range(1, 20)])
        with (
            mock.patch.object(remote_sessions.embed, "capture", side_effect=_capture),
            mock.patch.object(remote_sessions.time, "monotonic", side_effect=lambda: next(clock)),
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            self.hub._wait_pane_settled("pane", quiet=1.0, timeout=20.0)
        # Returned on the first frame after "› ready" stayed unchanged for >= 1 s,
        # never on the empty or spinning frames.
        self.assertEqual(captured[-1], "› ready")
        self.assertLess(len(captured), len(frames))

    def test_wait_pane_settled_gives_up_at_deadline_without_raising(self) -> None:
        counter = iter(range(1000))
        with (
            mock.patch.object(
                remote_sessions.embed, "capture", side_effect=lambda *_: f"spin {next(counter)}"
            ),
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            self.hub._wait_pane_settled("pane", quiet=1.0, timeout=0.0)

    def test_send_text_recovers_dead_binding_on_same_conversation(self) -> None:
        """Stale binding + failed paste + proven-dead pane → clear, native-resume
        the SAME conversation once, retry the paste on the new pane only."""
        session = _session(sid="stale")
        session["keepalive_name"] = "pane-dead"
        self.hub.store.sessions = {"claude": [session]}

        def _fake_resume(key: str) -> dict:
            self.assertEqual(key, "claude:stale")
            found = self.hub.store.find_session("claude:stale")
            assert found is not None
            self.assertNotIn("keepalive_name", found)
            found["keepalive_name"] = "pane-new"
            return {"key": key}

        pastes: list[tuple[str, str]] = []

        def _fake_paste(name: str, text: str):
            pastes.append((name, text))
            if name == "pane-dead":
                return InjectionResult(False, "pane_gone", False)
            return InjectionResult(True)

        with (
            mock.patch.object(self.hub, "resume_session", side_effect=_fake_resume) as resume,
            mock.patch.object(remote_sessions.embed, "pane_liveness", return_value="dead"),
            mock.patch.object(
                remote_sessions.embed, "paste_detailed", side_effect=_fake_paste
            ),
            mock.patch.object(
                remote_sessions.embed, "send_key_detailed", return_value=InjectionResult(True)
            ) as send_key,
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            self.hub.send_text("claude:stale", "继续")
            resume.assert_called_once_with("claude:stale")
            self.assertEqual(
                pastes, [("pane-dead", "继续"), ("pane-new", "继续")]
            )
            send_key.assert_called_once_with("pane-new", "Enter")
            current = self.hub.store.find_session("claude:stale")
            assert current is not None
            self.assertEqual(current.get("keepalive_name"), "pane-new")

    def test_send_text_transient_when_pane_still_alive(self) -> None:
        """Paste fails but the pane is alive → one safe retry on the same live
        binding (certain no-effect failure), then plain transient, no resume,
        nothing restarted, no duplicate process (a busy pane is never resumed)."""
        session = _session(sid="busy")
        session["keepalive_name"] = "pane-busy"
        self.hub.store.sessions = {"claude": [session]}
        with (
            mock.patch.object(self.hub, "resume_session") as resume,
            mock.patch.object(remote_sessions.embed, "pane_liveness", return_value="alive"),
            mock.patch.object(
                remote_sessions.embed,
                "paste_detailed",
                return_value=InjectionResult(False, "tmux_busy", False),
            ) as paste,
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            with self.assertRaises(remote_sessions.ActionError) as ctx:
                self.hub.send_text("claude:busy", "继续")
            self.assertEqual(ctx.exception.code, "unavailable")
            message = str(ctx.exception.message)
            self.assertIn("did not respond in time", message)
            self.assertIn("nothing was restarted", message)
            for raw in ("tmux_busy", "pane_gone", "tmux_error", "uncertain"):
                self.assertNotIn(raw, message)
            resume.assert_not_called()
            self.assertEqual(paste.call_count, 2, "one safe retry on the same live pane")
            paste.assert_called_with("pane-busy", "继续")
            current = self.hub.store.find_session("claude:busy")
            assert current is not None
            self.assertEqual(current.get("keepalive_name"), "pane-busy")

    def test_send_text_resume_failure_propagates_own_cause(self) -> None:
        """Dead binding whose same-conversation resume fails → the resume's own
        cause reaches the receipt, never a blind second paste."""
        session = _session(sid="noresume")
        session["keepalive_name"] = "pane-dead"
        self.hub.store.sessions = {"claude": [session]}

        def _boom(_key: str) -> dict:
            raise remote_sessions.ActionError("unavailable", "恢复失败：xxx")

        with (
            mock.patch.object(self.hub, "resume_session", side_effect=_boom),
            mock.patch.object(remote_sessions.embed, "pane_liveness", return_value="dead"),
            mock.patch.object(
                remote_sessions.embed,
                "paste_detailed",
                return_value=InjectionResult(False, "pane_gone", False),
            ) as paste,
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            with self.assertRaises(remote_sessions.ActionError) as ctx:
                self.hub.send_text("claude:noresume", "继续")
            self.assertIn("恢复失败", str(ctx.exception.message))
            paste.assert_called_once_with("pane-dead", "继续")

    def test_send_text_uncertain_paste_is_partial_without_resume_or_retry(self) -> None:
        """A paste that may have delivered (timeout) → unknown receipt, never a
        rejection, never a resume, never a second paste."""
        session = _session(sid="unc")
        session["keepalive_name"] = "pane-unc"
        self.hub.store.sessions = {"claude": [session]}
        with (
            mock.patch.object(self.hub, "resume_session") as resume,
            mock.patch.object(
                remote_sessions.embed,
                "paste_detailed",
                return_value=InjectionResult(False, "uncertain", True),
            ) as paste,
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            with self.assertRaises(remote_sessions.PartialInjectionError):
                self.hub.send_text("claude:unc", "继续")
            resume.assert_not_called()
            paste.assert_called_once_with("pane-unc", "继续")

    def test_send_text_unknown_liveness_never_resumes(self) -> None:
        """Two unknowns (timeout-shaped) are not proof of death: transient, no resume."""
        session = _session(sid="twouk")
        session["keepalive_name"] = "pane-twouk"
        self.hub.store.sessions = {"claude": [session]}
        with (
            mock.patch.object(self.hub, "resume_session") as resume,
            mock.patch.object(
                remote_sessions.embed, "pane_liveness", return_value="unknown"
            ),
            mock.patch.object(
                remote_sessions.embed,
                "paste_detailed",
                return_value=InjectionResult(False, "pane_gone", False),
            ),
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            with self.assertRaises(remote_sessions.ActionError) as ctx:
                self.hub.send_text("claude:twouk", "继续")
            self.assertEqual(ctx.exception.code, "unavailable")
            resume.assert_not_called()
            current = self.hub.store.find_session("claude:twouk")
            assert current is not None
            self.assertEqual(current.get("keepalive_name"), "pane-twouk")

    def test_send_turn_with_pasted_images_never_resumes_on_text_failure(self) -> None:
        """Image paths already in the pane → text failure is partial, no resume,
        no retry (retrying would replay an image-less turn)."""
        session = _session(source="cursor", sid="imgturn", attention="none")
        session["keepalive_name"] = "pane-img"
        self.hub.store.sessions = {"cursor": [session]}
        with (
            mock.patch.object(self.hub, "resume_session") as resume,
            mock.patch.object(remote_sessions.embed, "pane_liveness", return_value="alive"),
            mock.patch.object(remote_sessions.embed, "capture", return_value="→ ready"),
            mock.patch.object(
                remote_sessions.embed,
                "save_image_and_paste_path",
                return_value="/tmp/paste-1.png",
            ),
            mock.patch.object(
                remote_sessions.embed,
                "paste_detailed",
                return_value=InjectionResult(False, "pane_gone", False),
            ) as paste,
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            with self.assertRaises(remote_sessions.PartialInjectionError):
                self.hub.send_turn("cursor:imgturn", "看图说话", images=[b"png-bytes"])
            resume.assert_not_called()
            paste.assert_called_once_with("pane-img", "看图说话")

    def test_concurrent_recoveries_resume_once_and_share_new_binding(self) -> None:
        """Two racing recoveries on one key: a single resume, both retry on it."""
        import threading as _threading

        session = _session(sid="shared")
        session["keepalive_name"] = "pane-dead"
        self.hub.store.sessions = {"claude": [session]}
        resumes: list[str] = []

        def _fake_resume(key: str) -> dict:
            resumes.append(key)
            import time as _time

            _time.sleep(0.05)
            found = self.hub.store.find_session("claude:shared")
            assert found is not None
            found["keepalive_name"] = "pane-new"
            return {"key": key}

        lock = _threading.Lock()
        pastes: list[tuple[str, str]] = []
        first_attempts = _threading.Barrier(2)
        paste_calls = {"n": 0}

        def _fake_paste(name: str, text: str):
            with lock:
                pastes.append((name, text))
                paste_calls["n"] += 1
                first_round = paste_calls["n"] <= 2
            if first_round:
                # Both threads attempt on the stale binding before either may
                # recover, so the stranded-duplicate race is actually exercised.
                first_attempts.wait(timeout=10)
            if name == "pane-dead":
                return InjectionResult(False, "pane_gone", False)
            return InjectionResult(True)

        def _liveness(name: str) -> str:
            return "dead" if name == "pane-dead" else "alive"

        errors: list[BaseException] = []

        def _send() -> None:
            try:
                self.hub.send_text("claude:shared", "继续")
            except BaseException as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        with (
            mock.patch.object(self.hub, "resume_session", side_effect=_fake_resume),
            mock.patch.object(remote_sessions.embed, "pane_liveness", side_effect=_liveness),
            mock.patch.object(remote_sessions.embed, "paste_detailed", side_effect=_fake_paste),
            mock.patch.object(
                remote_sessions.embed, "send_key_detailed", return_value=InjectionResult(True)
            ),
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            first = _threading.Thread(target=_send)
            second = _threading.Thread(target=_send)
            first.start()
            second.start()
            first.join()
            second.join()
        self.assertEqual(errors, [])
        self.assertEqual(resumes, ["claude:shared"])
        self.assertEqual(
            sorted(pastes),
            [("pane-dead", "继续"), ("pane-dead", "继续"), ("pane-new", "继续"), ("pane-new", "继续")],
        )

    def test_post_resume_failure_says_restarted_not_safe_retry(self) -> None:
        """After a resume, a second failure must not claim nothing restarted."""
        session = _session(sid="again")
        session["keepalive_name"] = "pane-dead"
        self.hub.store.sessions = {"claude": [session]}

        def _fake_resume(key: str) -> dict:
            found = self.hub.store.find_session("claude:again")
            assert found is not None
            found["keepalive_name"] = "pane-new"
            return {"key": key}

        calls = {"n": 0}

        def _fake_paste(name: str, text: str):
            calls["n"] += 1
            return InjectionResult(False, "tmux_error", False)

        with (
            mock.patch.object(self.hub, "resume_session", side_effect=_fake_resume),
            mock.patch.object(remote_sessions.embed, "pane_liveness", return_value="dead"),
            mock.patch.object(remote_sessions.embed, "paste_detailed", side_effect=_fake_paste),
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            with self.assertRaises(remote_sessions.ActionError) as ctx:
                self.hub.send_text("claude:again", "继续")
            message = str(ctx.exception.message)
            self.assertIn("was restarted", message)
            self.assertNotIn("nothing was restarted", message)
            for raw in ("pane_gone", "tmux_busy", "tmux_error", "uncertain", "tmux_unavailable"):
                self.assertNotIn(raw, message)
            self.assertEqual(calls["n"], 2)

    def test_send_text_cursor_promotes_with_second_enter(self) -> None:
        """Phone Cursor submits must steer: paste + Enter + empty Enter."""
        session = _session(source="cursor", sid="c1", attention="working")
        session["keepalive_name"] = "pane-cursor"
        self.hub.store.sessions = {"cursor": [session]}
        with (
            mock.patch.object(
                remote_sessions.embed, "paste_detailed", return_value=InjectionResult(True)
            ) as paste,
            mock.patch.object(
                remote_sessions.embed, "send_key_detailed", return_value=InjectionResult(True)
            ) as send_key,
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            self.hub.send_text("cursor:c1", "改方向")
            paste.assert_called_once_with("pane-cursor", "改方向")
            self.assertEqual(
                send_key.call_args_list,
                [
                    mock.call("pane-cursor", "Enter"),
                    mock.call("pane-cursor", "Enter"),
                ],
            )

    def test_send_text_cursor_waiting_skips_steer_promote(self) -> None:
        session = _session(source="cursor", sid="c2", attention="waiting")
        session["keepalive_name"] = "pane-cursor"
        self.hub.store.sessions = {"cursor": [session]}
        with (
            mock.patch.object(
                remote_sessions.embed, "paste_detailed", return_value=InjectionResult(True)
            ),
            mock.patch.object(
                remote_sessions.embed, "send_key_detailed", return_value=InjectionResult(True)
            ) as send_key,
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            self.hub.send_text("cursor:c2", "选 A")
            send_key.assert_called_once_with("pane-cursor", "Enter")

    def test_send_turn_waits_pastes_images_text_and_submits(self) -> None:
        session = _session(source="cursor", sid="turn1", attention="none")
        session["keepalive_name"] = "pane-turn"
        self.hub.store.sessions = {"cursor": [session]}
        captures = iter(["booting", "ready → idle", "working → Add a follow-up"])

        def _capture(_name: str, _scroll: int = 0, _rows: int = 0):
            return next(captures, "working → Add a follow-up")

        with (
            mock.patch.object(remote_sessions.embed, "capture", side_effect=_capture),
            mock.patch.object(
                remote_sessions.embed,
                "save_image_and_paste_path",
                return_value="/tmp/paste-1.png",
            ) as save_img,
            mock.patch.object(
                remote_sessions.embed, "paste_detailed", return_value=InjectionResult(True)
            ) as paste,
            mock.patch.object(remote_sessions.embed, "pane_liveness", return_value="alive"),
            mock.patch.object(
                remote_sessions.embed, "send_key_detailed", return_value=InjectionResult(True)
            ) as send_key,
            mock.patch.object(remote_sessions.time, "sleep"),
            mock.patch.object(
                remote_sessions,
                "_composer_still_holds",
                return_value=False,
            ),
        ):
            paths = self.hub.send_turn(
                "cursor:turn1",
                "改成叫Item888",
                images=[b"png-bytes"],
            )
            self.assertEqual(paths, ["/tmp/paste-1.png"])
            save_img.assert_called_once_with("pane-turn", b"png-bytes")
            paste.assert_called_once_with("pane-turn", "改成叫Item888")
            self.assertEqual(
                send_key.call_args_list,
                [
                    mock.call("pane-turn", "Enter"),
                    mock.call("pane-turn", "Enter"),
                ],
            )

    def test_send_turn_retries_enter_while_prompt_stuck_in_composer(self) -> None:
        session = _session(source="cursor", sid="stuck", attention="none")
        session["keepalive_name"] = "pane-stuck"
        self.hub.store.sessions = {"cursor": [session]}
        hold_checks = [True, False]

        with (
            mock.patch.object(
                remote_sessions.embed, "capture", return_value="→ ready"
            ),
            mock.patch.object(
                remote_sessions.embed, "paste_detailed", return_value=InjectionResult(True)
            ),
            mock.patch.object(remote_sessions.embed, "pane_liveness", return_value="alive"),
            mock.patch.object(
                remote_sessions.embed, "send_key_detailed", return_value=InjectionResult(True)
            ) as send_key,
            mock.patch.object(remote_sessions.time, "sleep"),
            mock.patch.object(
                remote_sessions,
                "_composer_still_holds",
                side_effect=lambda _plain, _needle: hold_checks.pop(0),
            ),
        ):
            self.hub.send_turn("cursor:stuck", "改成叫Item888", images=None)
            # initial submit (2 Enters for Cursor) + one retry submit (2 more)
            self.assertEqual(send_key.call_count, 4)

    def test_phone_steer_promote_helper(self) -> None:
        self.assertTrue(
            remote_sessions._phone_steer_promote(
                {"source": "cursor", "attention_kind": "working"}
            )
        )
        self.assertTrue(
            remote_sessions._phone_steer_promote(
                {"source": "cursor", "attention_kind": "none"}
            )
        )
        self.assertFalse(
            remote_sessions._phone_steer_promote(
                {"source": "cursor", "attention_kind": "waiting"}
            )
        )
        self.assertFalse(
            remote_sessions._phone_steer_promote(
                {"source": "claude", "attention_kind": "working"}
            )
        )

    def test_attention_change_publishes_to_conversation_watch(self) -> None:
        events: list[tuple[str, dict]] = []
        self.hub._on_event = lambda channel, data: events.append((channel, data))
        path = Path(self._tmp.name) / "claude.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(sid="a", attention="working")
        session["path"] = str(path)
        self.hub.store.sessions = {"claude": [session]}
        self.hub.watch_conversation("claude:a")
        self.hub._snapshot_attention()
        events.clear()
        session["attention_kind"] = "waiting"
        self.hub._detect_attention_changes()
        attention = [
            (channel, payload)
            for channel, payload in events
            if payload.get("kind") == "attention"
        ]
        self.assertEqual(len(attention), 1)
        self.assertEqual(attention[0][0], "session:claude:a")
        self.assertEqual(
            attention[0][1],
            {
                "version": 1,
                "kind": "attention",
                "session": "claude:a",
                "attention": "waiting",
                "live": False,
            },
        )
        self.hub.unwatch_conversation("claude:a")

    def test_live_change_publishes_metadata_to_conversation_watch(self) -> None:
        events: list[tuple[str, dict]] = []
        self.hub._on_event = lambda channel, data: events.append((channel, data))
        path = Path(self._tmp.name) / "cursor.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(source="cursor", sid="fold", attention="working")
        session["path"] = str(path)
        session["live"] = False
        self.hub.store.sessions = {"cursor": [session]}
        self.hub.watch_conversation("cursor:fold")
        self.hub._snapshot_attention()
        self.hub._snapshot_live()
        events.clear()
        session["live"] = True
        self.hub._detect_live_changes()
        metadata = [
            (channel, payload)
            for channel, payload in events
            if payload.get("kind") == "metadata"
        ]
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0][0], "session:cursor:fold")
        self.assertTrue(metadata[0][1]["summary"]["live"])
        self.assertEqual(metadata[0][1]["summary"]["attention"], "working")
        self.hub.unwatch_conversation("cursor:fold")

    def test_conversation_poll_interval_tightens_when_working(self) -> None:
        path = Path(self._tmp.name) / "claude.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(sid="a", attention="working")
        session["path"] = str(path)
        self.hub.store.sessions = {"claude": [session]}
        self.hub.watch_conversation("claude:a")
        self.assertEqual(
            self.hub._conversation_poll_interval(),
            remote_sessions._CONVERSATION_ACTIVE_INTERVAL,
        )
        session["attention_kind"] = "none"
        self.assertEqual(
            self.hub._conversation_poll_interval(),
            remote_sessions._CONVERSATION_INTERVAL,
        )
        session["attention_kind"] = "waiting"
        self.assertEqual(
            self.hub._conversation_poll_interval(),
            remote_sessions._CONVERSATION_ACTIVE_INTERVAL,
        )
        self.hub.unwatch_conversation("claude:a")

    def test_paging_earlier_does_not_publish_deltas(self) -> None:
        events: list[tuple[str, dict]] = []
        self.hub._on_event = lambda channel, data: events.append((channel, data))
        path = Path(self._tmp.name) / "claude.jsonl"
        _write_assistant_jsonl(path, [f"消息-{index}" for index in range(90)])
        session = _session(sid="a")
        session["path"] = str(path)
        self.hub.store.sessions = {"claude": [session]}
        page = self.hub.watch_conversation("claude:a")
        events.clear()
        earlier = self.hub.message_page("claude:a", before_seq=page["oldest_seq"])
        self.assertGreater(len(earlier["messages"]), 0)
        self.assertEqual(_delta_role_texts(events), [])
        self.hub.unwatch_conversation("claude:a")

    def test_stop_and_delete_mutate_canonical_key(self) -> None:
        """手机仍拿旧键时，停止/删除必须改正式会话，不能写到已经不存在的占位卡。"""
        real = _session(sid="real-id")
        real["keepalive_name"] = "corral-claude-real"
        self.hub.store.sessions = {"claude": [real]}
        self.hub.store.hosted["claude:real-id"] = "corral-claude-real"
        self.hub.store._session_key_migrations["claude:placeholder"] = "claude:real-id"
        with mock.patch("corral.remote.sessions.keepalive.kill", return_value=True) as mocked:
            self.hub.stop_session("claude:placeholder")
        mocked.assert_called_once_with("corral-claude-real")
        self.assertNotIn("claude:real-id", self.hub.store.hosted)
        stopped = self.hub.store.find_session("claude:real-id")
        self.assertIsNotNone(stopped)
        self.assertFalse(stopped["live"])

        real = _session(sid="real-id")
        self.hub.store.sessions = {"claude": [real]}
        self.hub.store._deleted.clear()
        self.hub.store._session_key_migrations["claude:placeholder"] = "claude:real-id"
        runtime = mock.Mock()
        with mock.patch.object(self.hub, "_runtime_of", return_value=runtime):
            self.hub.delete_session("claude:placeholder")
        runtime.delete_session.assert_called_once()
        self.assertIn("claude:real-id", self.hub.store._deleted)
        self.assertIn("claude:placeholder", self.hub.store._deleted)
        self.assertIsNone(self.hub.store.find_session("claude:real-id"))


def _delta_role_texts(events: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for _channel, payload in events:
        if payload.get("kind") != "delta":
            continue
        for message in payload.get("messages") or []:
            found.append((str(message.get("role") or ""), str(message.get("text") or "")))
    return found


def _write_assistant_jsonl(path: Path, texts: list[str]) -> None:
    lines = []
    for index, text in enumerate(texts, start=1):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": f"u{index}",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                },
                ensure_ascii=False,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
