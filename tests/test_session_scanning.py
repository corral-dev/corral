from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from corral import i18n
from corral.i18n import t

i18n.set_lang("en")

import corral
from corral import agent_api, titles
from corral.models import ConversationMessage, Handoff, LaunchPlan, format_message_time
from corral.runtime.base import BaseRuntime
from corral.runtime.claude import ClaudeRuntime
from corral.runtime.codex import CodexRuntime
from corral.runtime.host_extension import corral_host_extension
from corral.scan import claude as scan_claude
from corral.scan import codex as scan_codex
from corral.scan import kimi as scan_kimi
from corral.scan import opencode as scan_opencode
from corral.scan import pi as scan_pi


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _make_opencode_db(path: Path, sessions: list[dict], messages: list[dict] = (), parts: list[dict] = ()) -> None:
    """按 opencode.db 真实 schema（session/message/part 三表）建最小 SQLite fixture。

    sessions: 每项含 id/directory/title/time_created/time_updated，可选 parent_id/time_archived。
    messages: 每项含 id/session_id/time_created/data（dict，会被 json.dumps）。
    parts: 每项含 id/message_id/session_id/time_created/data（dict）。
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE session (id text PRIMARY KEY, project_id text, parent_id text, "
            "slug text, directory text, title text, version text, "
            "time_created integer, time_updated integer, time_archived integer)"
        )
        conn.execute(
            "CREATE TABLE message (id text PRIMARY KEY, session_id text, "
            "time_created integer, time_updated integer, data text)"
        )
        conn.execute(
            "CREATE TABLE part (id text PRIMARY KEY, message_id text, session_id text, "
            "time_created integer, time_updated integer, data text)"
        )
        for s in sessions:
            conn.execute(
                "INSERT INTO session (id, project_id, parent_id, slug, directory, title, "
                "version, time_created, time_updated, time_archived) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    s["id"], s.get("project_id", "global"), s.get("parent_id"), s.get("slug", "x"),
                    s.get("directory", ""), s.get("title"), s.get("version", "1.0.0"),
                    s.get("time_created", 0), s.get("time_updated", 0), s.get("time_archived"),
                ),
            )
        for m in messages:
            conn.execute(
                "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?)",
                (m["id"], m["session_id"], m["time_created"], m.get("time_updated", m["time_created"]),
                 json.dumps(m["data"], ensure_ascii=False)),
            )
        for p in parts:
            conn.execute(
                "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?,?,?,?,?,?)",
                (p["id"], p["message_id"], p["session_id"], p["time_created"],
                 p.get("time_updated", p["time_created"]), json.dumps(p["data"], ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()


class TimezoneMixin:
    @classmethod
    def setUpClass(cls) -> None:
        cls._old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Shanghai"
        if hasattr(time, "tzset"):
            time.tzset()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls._old_tz
        if hasattr(time, "tzset"):
            time.tzset()


class ClaudeScanTests(TimezoneMixin, unittest.TestCase):
    def test_extract_text_keeps_command_args_as_user_intent(self) -> None:
        content = (
            "<command-name>/plan</command-name>\n"
            "<command-message>plan</command-message>\n"
            "<command-args>@openconductor 页面输入框要支持文件上传和选择图片</command-args>"
        )

        self.assertEqual(
            scan_claude._extract_text(content),
            "@openconductor 页面输入框要支持文件上传和选择图片",
        )

    def test_extract_text_does_not_crash_when_part_text_is_json_null(self) -> None:
        # part.get("text", "") 的默认值只在 key 缺失时生效；key 存在但值为
        # JSON null 时曾直接 AttributeError（.strip() on None）。
        content = [{"type": "text", "text": None}, {"type": "text", "text": "真实文本"}]
        self.assertEqual(scan_claude._extract_text(content), "真实文本")

    def test_scan_signature_ignores_parent_dir_mtime_and_sees_jsonl_append(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            projects = Path(td) / "projects"
            proj = projects / "demo"
            proj.mkdir(parents=True)
            jsonl = proj / "sess.jsonl"
            jsonl.write_text("{}\n", encoding="utf-8")
            with mock.patch.object(scan_claude, "PROJECTS_DIR", str(projects)), mock.patch.object(
                scan_claude, "SESSIONS_DIR", str(Path(td) / "sessions")
            ):
                first = scan_claude.scan_signature()
                os.utime(proj, None)
                after_dir = scan_claude.scan_signature()
                jsonl.write_text("{}\n{}\n", encoding="utf-8")
                after_file = scan_claude.scan_signature()
        self.assertEqual(first, after_dir)
        self.assertNotEqual(first, after_file)

    def test_entry_time_does_not_crash_when_snapshot_is_json_null(self) -> None:
        self.assertIsNone(scan_claude._entry_time({"snapshot": None}))

    def test_build_session_info_does_not_crash_when_tail_assistant_text_is_json_null(self) -> None:
        # _build_session_info 的尾部循环里同样按 part.get("text", "").strip() 过滤
        # assistant 文本 part，text 为 JSON null 时曾直接 AttributeError。
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abc.jsonl"
            _write_jsonl(
                path,
                [
                    {"type": "user", "message": {"content": "问题"}, "cwd": td,
                     "timestamp": "2026-07-01T10:00:00Z"},
                    {"type": "assistant",
                     "message": {"content": [{"type": "text", "text": None},
                                              {"type": "text", "text": "真实答复"}]},
                     "timestamp": "2026-07-01T10:00:05Z"},
                ],
            )
            info = scan_claude._build_session_info(str(path), "proj")

        self.assertEqual(info["last_agent_msg"], "真实答复")

    def test_title_uses_last_prompt_and_time_falls_back_to_event_time_when_mtime_stale(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abc.jsonl"
            rows = [
                {"type": "mode", "mode": "normal", "sessionId": "abc"},
                {
                    "type": "user",
                    "message": {"content": "第一条问题"},
                    "timestamp": "2026-06-22T08:11:26.000Z",
                    "cwd": "/tmp/demo",
                    "sessionId": "abc",
                },
            ]
            rows.extend({"type": "attachment", "sessionId": "abc"} for _ in range(8))
            rows.extend(
                [
                    {"type": "ai-title", "aiTitle": "分析代码索引更新机制", "sessionId": "abc"},
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "完成"}]},
                        "timestamp": "2026-06-22T08:41:55.000Z",
                        "sessionId": "abc",
                    },
                    {"type": "last-prompt", "lastPrompt": "最后一次问题", "sessionId": "abc"},
                ]
            )
            _write_jsonl(path, rows)
            # 文件 mtime 远远新于会话内最后一条真实事件（如驻留会话被重新打开后
            # 追加了 last-prompt/ai-title 等无时间戳元数据），应判定 mtime 不可信。
            os.utime(path, (1893456000, 1893456000))

            info = scan_claude._build_session_info(str(path), "demo")

            self.assertEqual(info["native_title"], "分析代码索引更新机制")
            self.assertEqual(info["fallback_title"], "最后一次问题")
            self.assertEqual(info["display_time"], "06-22 16:41")
            self.assertEqual(info["mtime"], info["event_time"])
            self.assertEqual(info["time_source"], "event_time_stale_mtime")
            self.assertEqual(format_message_time(info["file_mtime"]), "01-01 08:00")

    def test_scan_sessions_sorts_by_effective_time(self) -> None:
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_claude.PROJECTS_DIR = td
                project = Path(td) / "proj"
                cwd = Path(td) / "demo"
                cwd.mkdir()
                old_path = project / "old.jsonl"
                new_path = project / "new.jsonl"
                _write_jsonl(
                    old_path,
                    [
                        {
                            "type": "user",
                            "message": {"content": "旧会话"},
                            "timestamp": "2026-06-01T00:00:00.000Z",
                            "cwd": str(cwd),
                        }
                    ],
                )
                _write_jsonl(
                    new_path,
                    [
                        {
                            "type": "user",
                            "message": {"content": "新会话"},
                            "timestamp": "2026-06-25T00:00:00.000Z",
                            "cwd": str(cwd),
                        }
                    ],
                )
                # old.jsonl 的 mtime 被顶到远未来（模拟被重新打开导致的假新），
                # 应回退到其真实事件时间 2026-06-01；new.jsonl 的 mtime 落在
                # 2000 年、比自己的事件时间 2026-06-25 还旧，不满足"mtime 比
                # 事件时间新"的回退条件，保持 file_mtime 不变。修正后 old.jsonl
                # 的有效时间（2026-06-01）仍晚于 new.jsonl 的有效时间（2000），
                # 排序结果与改动前巧合一致，但含义已从"纯按文件 mtime 排序"
                # 变为"按修正后的有效时间排序"。
                os.utime(old_path, (1893456000, 1893456000))
                os.utime(new_path, (946684800, 946684800))

                sessions = scan_claude.scan_sessions(limit=2)

                self.assertEqual([s["fallback_title"] for s in sessions], ["旧会话", "新会话"])
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_scan_sessions_falls_back_to_event_time_for_bulk_touched_sessions(self) -> None:
        """Syncthing/复制等批量元数据刷新会让一批历史会话同一时刻被 touch；

        每个会话各自的 mtime 都远新于自己内部最后一条真实事件，单会话 gap
        规则应逐个回退到 event_time，不需要额外的"同分钟桶聚簇"判污染。
        """

        def _iso(ts: float) -> str:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_claude.PROJECTS_DIR = td
                project = Path(td) / "proj"
                cwd = Path(td) / "demo"
                cwd.mkdir()
                bulk_mtime = int(time.time() // 60) * 60
                for i in range(6):
                    path = project / f"bulk-{i}.jsonl"
                    _write_jsonl(
                        path,
                        [
                            {
                                "type": "user",
                                "message": {"content": f"批量旧会话 {i}"},
                                "timestamp": _iso(bulk_mtime - 10 * 86400),
                                "cwd": str(cwd),
                            }
                        ],
                    )
                    os.utime(path, (bulk_mtime, bulk_mtime))
                real_path = project / "real.jsonl"
                _write_jsonl(
                    real_path,
                    [
                        {
                            "type": "user",
                            "message": {"content": "真实新会话"},
                            "timestamp": _iso(bulk_mtime - 60),
                            "cwd": str(cwd),
                        }
                    ],
                )
                os.utime(real_path, (bulk_mtime - 60, bulk_mtime - 60))

                sessions = scan_claude.scan_sessions(limit=7)

                self.assertEqual(sessions[0]["fallback_title"], "真实新会话")
                self.assertEqual(sessions[0]["time_source"], "file_mtime")
                self.assertTrue(all(s["time_source"] == "event_time_stale_mtime" for s in sessions[1:]))
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_scan_sessions_recovers_true_time_for_stale_resumed_session(self) -> None:
        """复现真实故障：会话内容 9 天前就结束，但驻留进程重新打开它时追加了

        没有时间戳的 last-prompt/ai-title/mode/permission-mode 元数据，把
        文件 mtime 顶到"现在"。列表时间列必须显示真实的 9 天前，而不是"刚刚"。
        """
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_claude.PROJECTS_DIR = td
                project = Path(td) / "proj"
                cwd = Path(td) / "demo"
                cwd.mkdir()
                now = time.time()
                stale_event = now - 9 * 86400
                path = project / "resumed.jsonl"
                _write_jsonl(
                    path,
                    [
                        {
                            "type": "user",
                            "message": {"content": "9天前的真实问题"},
                            "timestamp": datetime.fromtimestamp(stale_event, tz=timezone.utc).strftime(
                                "%Y-%m-%dT%H:%M:%S.000Z"
                            ),
                            "cwd": str(cwd),
                        },
                        {"type": "last-prompt", "lastPrompt": "9天前的真实问题"},
                        {"type": "ai-title", "aiTitle": "旧任务"},
                        {"type": "mode", "mode": "normal"},
                        {"type": "permission-mode", "mode": "default"},
                    ],
                )
                os.utime(path, (now, now))  # 驻留会话被重新打开，文件 mtime 顶到现在

                sessions = scan_claude.scan_sessions(limit=1)

                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0]["time_source"], "event_time_stale_mtime")
                self.assertAlmostEqual(sessions[0]["mtime"], stale_event, delta=2)
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_scan_sessions_filters_and_sorts_before_applying_limit(self) -> None:
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_claude.PROJECTS_DIR = td
                old_path = Path(td) / "old_proj" / "old.jsonl"
                new_path = Path(td) / "new_proj" / "new.jsonl"
                _write_jsonl(
                    old_path,
                    [{"type": "user", "message": {"content": "旧会话"}, "timestamp": "2026-06-01T00:00:00.000Z"}],
                )
                _write_jsonl(
                    new_path,
                    [{"type": "user", "message": {"content": "新会话"}, "timestamp": "2026-06-01T00:00:00.000Z"}],
                )
                os.utime(old_path, (946684800, 946684800))
                os.utime(new_path, (1893456000, 1893456000))

                real_listdir = os.listdir

                def ordered_listdir(path: str) -> list[str]:
                    if path == td:
                        return ["old_proj", "new_proj"]
                    return real_listdir(path)

                with mock.patch.object(scan_claude.os, "listdir", side_effect=ordered_listdir):
                    sessions = scan_claude.scan_sessions(limit=1)

                self.assertEqual([s["fallback_title"] for s in sessions], ["新会话"])
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_claude_without_cached_title_triggers_background_generation(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "mtime": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "兜底标题",
        }

        title, needs_gen = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "兜底标题")
        self.assertTrue(needs_gen)

    def test_claude_fallback_rejects_continue_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abc.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "message": {"content": "帮我分析会话标题选择策略"},
                        "timestamp": "2026-06-22T08:11:26.000Z",
                        "cwd": "/tmp/demo",
                    },
                    {"type": "last-prompt", "lastPrompt": "继续", "sessionId": "abc"},
                ],
            )

            info = scan_claude._build_session_info(str(path), "demo")

            self.assertEqual(info["fallback_title"], "帮我分析会话标题选择策略")

    def test_claude_fallback_rejects_test_challenge_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abc.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "message": {
                            "content": (
                                "<command-name>/plan</command-name>\n"
                                "<command-args>@openconductor 页面输入框要支持文件上传和选择图片</command-args>"
                            )
                        },
                        "timestamp": "2026-06-22T08:11:26.000Z",
                        "cwd": "/tmp/demo",
                    },
                    {"type": "last-prompt", "lastPrompt": "你测试了吗 完整端到端测试", "sessionId": "abc"},
                ],
            )

            info = scan_claude._build_session_info(str(path), "demo")

            self.assertEqual(info["fallback_title"], "页面输入框要支持文件上传和选择图片")

    def test_claude_fallback_uses_agent_summary_when_user_prompts_are_low_value(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abc.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "message": {"content": "[Request interrupted by user]"},
                        "timestamp": "2026-06-22T08:11:26.000Z",
                        "cwd": "/tmp/demo",
                    },
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "端到端三步全通：上传、发送、回显均通过。"}]},
                        "timestamp": "2026-06-22T08:12:26.000Z",
                    },
                    {"type": "last-prompt", "lastPrompt": "快点啊", "sessionId": "abc"},
                ],
            )

            info = scan_claude._build_session_info(str(path), "demo")

            self.assertEqual(info["fallback_title"], "端到端三步全通：上传、发送、回显均通过。")

    def test_claude_native_slug_is_only_temporary_fallback(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "mtime": 1,
            "size_kb": 1,
            "native_title": "account-model-runtime-decoupling",
            "fallback_title": "继续",
        }

        title, needs_gen = titles.resolve_initial_title(session, {})

        self.assertEqual(title, t("session.title.pending"))
        self.assertTrue(needs_gen)

    def test_claude_native_slug_does_not_override_meaningful_fallback(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "mtime": 1,
            "size_kb": 1,
            "native_title": "fix-doc-init-product-first",
            "fallback_title": "@doc-init/ @doc-update/ @doc-audit/ 关于现在的文档体系从产品视角优先",
        }

        title, needs_gen = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "关于现在的文档体系从产品视角优先")
        self.assertTrue(needs_gen)

    def test_doc_command_fallback_is_human_readable_while_generation_is_pending(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "mtime": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "/doc-init @openconductor",
        }

        i18n.set_lang("en")
        title, needs_gen = titles.resolve_initial_title(session, {})
        self.assertEqual(title, "openconductor Init docs")
        self.assertTrue(needs_gen)

        i18n.set_lang("zh")
        title, needs_gen = titles.resolve_initial_title(session, {})
        self.assertEqual(title, "openconductor 文档初始化")
        self.assertTrue(needs_gen)
        i18n.set_lang("en")

    def test_generated_title_cache_wins_for_claude(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "mtime": 1,
            "size_kb": 1,
            "native_title": "fix-doc-init-product-first",
            "fallback_title": "/doc-init @openconductor",
        }

        title, needs_gen = titles.resolve_initial_title(
            session,
            {"abc": {"fp": "v3:1", "title": "文档体系产品化重构"}},
        )

        self.assertEqual(title, "文档体系产品化重构")
        self.assertFalse(needs_gen)

    def test_low_value_claude_session_is_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "abc.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "message": {"content": "..."},
                        "timestamp": "2026-06-22T08:11:26.000Z",
                        "cwd": "/tmp/demo",
                    },
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": '{"019efe42-6d51-7fb3-ad48-112a8eefa02b": "修复会话时间显示与标题提取"}',
                                }
                            ]
                        },
                        "timestamp": "2026-06-22T08:12:26.000Z",
                    },
                ],
            )

            info = scan_claude._build_session_info(str(path), "demo")

            self.assertEqual(info["fallback_title"], "(仅本地命令)")

    def test_scan_filters_teammate_subagent_sessions(self) -> None:
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_claude.PROJECTS_DIR = td
                project = Path(td) / "proj"
                project.mkdir()
                cwd = str(project / "workspace")
                Path(cwd).mkdir()
                _write_jsonl(
                    project / "parent-session.jsonl",
                    [
                        {"type": "user", "message": {"content": "顶层用户问题"}, "cwd": cwd},
                        {"type": "assistant", "message": {"content": [{"type": "text", "text": "好的"}]}},
                    ],
                )
                _write_jsonl(
                    project / "teammate-session.jsonl",
                    [
                        {"type": "ai-title", "aiTitle": "reviewer"},
                        {"type": "agent-name", "agentName": "reviewer"},
                        {"type": "user", "message": {"content": "请审查这段代码"}, "cwd": cwd},
                        {"type": "assistant", "message": {"content": [{"type": "text", "text": "审查完成"}]}},
                    ],
                )
                sessions = scan_claude.scan_sessions(limit=10)
                self.assertEqual([s["id"] for s in sessions], ["parent-session"])
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_scan_filters_teammate_session_dispatched_by_team_lead(self) -> None:
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_claude.PROJECTS_DIR = td
                project = Path(td) / "proj"
                project.mkdir()
                cwd = str(project / "workspace")
                Path(cwd).mkdir()
                _write_jsonl(
                    project / "parent-session.jsonl",
                    [
                        {"type": "user", "message": {"content": "顶层用户问题"}, "cwd": cwd},
                        {"type": "assistant", "message": {"content": [{"type": "text", "text": "好的"}]}},
                    ],
                )
                _write_jsonl(
                    project / "team-lead-dispatched-session.jsonl",
                    [
                        {
                            "type": "user",
                            "message": {
                                "content": (
                                    '<teammate-message teammate_id="team-lead">\\n'
                                    "请审查这段代码\\n"
                                    "</teammate-message>"
                                )
                            },
                            "cwd": cwd,
                        },
                        {"type": "assistant", "message": {"content": [{"type": "text", "text": "审查完成"}]}},
                    ],
                )
                sessions = scan_claude.scan_sessions(limit=10)
                self.assertEqual([s["id"] for s in sessions], ["parent-session"])
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_scan_keeps_team_lead_with_team_name_on_messages(self) -> None:
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_claude.PROJECTS_DIR = td
                project = Path(td) / "proj"
                project.mkdir()
                cwd = str(project / "workspace")
                Path(cwd).mkdir()
                _write_jsonl(
                    project / "team-lead-session.jsonl",
                    [
                        {"type": "user", "message": {"content": "启动 agent team"}, "cwd": cwd, "teamName": "audit"},
                        {"type": "assistant", "message": {"content": [{"type": "text", "text": "已创建团队"}]}},
                    ],
                )
                sessions = scan_claude.scan_sessions(limit=10)
                self.assertEqual([s["id"] for s in sessions], ["team-lead-session"])
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_scan_keeps_parent_session_after_claude_writes_agent_name(self) -> None:
        """Claude 2.1+ 会在顶层会话写出自己的显示名，不能因此把真会话滤掉。"""
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_claude.PROJECTS_DIR = td
                project = Path(td) / "proj"
                project.mkdir()
                cwd = str(project / "workspace")
                Path(cwd).mkdir()
                _write_jsonl(
                    project / "named-parent.jsonl",
                    [
                        {"type": "mode", "mode": "normal"},
                        {"type": "permission-mode", "permissionMode": "bypassPermissions"},
                        {"type": "user", "message": {"content": "修一下账户余量显示"}, "cwd": cwd},
                        {"type": "assistant", "message": {"content": [{"type": "text", "text": "开始查"}]}},
                        {"type": "ai-title", "aiTitle": "调查账户余量未显示"},
                        {"type": "ai-title", "aiTitle": "cursor-account-disappearing-fix"},
                        {"type": "agent-name", "agentName": "cursor-account-disappearing-fix"},
                    ],
                )
                _write_jsonl(
                    project / "teammate-session.jsonl",
                    [
                        {"type": "ai-title", "aiTitle": "reviewer"},
                        {"type": "agent-name", "agentName": "reviewer"},
                        {"type": "user", "message": {"content": "请审查这段代码"}, "cwd": cwd},
                    ],
                )
                sessions = scan_claude.scan_sessions(limit=10)
                self.assertEqual([s["id"] for s in sessions], ["named-parent"])
                self.assertEqual(sessions[0]["native_title"], "调查账户余量未显示")
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

    def test_low_value_cached_title_is_ignored(self) -> None:
        session = {
            "source": "codex",
            "id": "abc",
            "mtime": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "真实问题标题",
        }

        title, needs_gen = titles.resolve_initial_title(session, {"abc": {"fp": "v3:1", "title": "继续"}})

        self.assertEqual(title, "真实问题标题")
        self.assertTrue(needs_gen)

    def test_low_value_only_session_does_not_trigger_generation(self) -> None:
        session = {
            "source": "opencode",
            "id": "only-greeting",
            "mtime": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "在吗",
        }

        title, needs_gen = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "在吗")
        self.assertFalse(needs_gen)

    def test_cached_title_survives_file_mtime_change(self) -> None:
        old_session = {
            "source": "claude",
            "id": "abc",
            "mtime": 100,
            "size_bytes": 1234,
            "size_kb": 1.2,
            "native_title": "unstable-title-slug",
            "fallback_title": "真实会话意图",
        }
        new_session = dict(old_session, mtime=200)
        cache = {"abc": {"fp": titles._fingerprint(old_session), "title": "稳定生成标题"}}

        title, needs_gen = titles.resolve_initial_title(new_session, cache)

        self.assertEqual(title, "稳定生成标题")
        self.assertFalse(needs_gen)

    def test_cached_title_is_not_regenerated_when_session_grows(self) -> None:
        session = {
            "source": "codex",
            "id": "abc",
            "mtime": 200,
            "size_bytes": 2345,
            "size_kb": 2.3,
            "native_title": None,
            "fallback_title": "这是一条很长很长的用户原文，不应该因为会话文件继续增长就重新显示在列表里",
        }
        cache = {"abc": {"fp": "v3:1234", "title": "稳定生成标题"}}

        title, needs_gen = titles.resolve_initial_title(session, cache)

        self.assertEqual(title, "稳定生成标题")
        self.assertFalse(needs_gen)

    def test_refresh_titles_does_not_retry_each_session_when_batch_fails(self) -> None:
        sessions = [
            {"id": "a", "mtime": 1, "size_kb": 1, "fallback_title": "标题A"},
            {"id": "b", "mtime": 1, "size_kb": 1, "fallback_title": "标题B"},
        ]
        cache = {}

        with mock.patch.object(titles, "generate_titles_batch", return_value={}) as mocked:
            with mock.patch.object(titles, "save_cache", return_value=None) as save_mock:
                result = titles.refresh_titles(sessions, cache, generator=mock.Mock())

        self.assertEqual(result, {})
        # One persist per batch; batch size lives in titles._BATCH_SIZE. Chunks for
        # distinct sessions never poison each other: with no healthy candidate left,
        # each chunk best-efforts the least-recently-failed one once. Cross-run
        # retry storms are still prevented by the persisted failure marks below.
        expected_batches = (len(sessions) + titles._BATCH_SIZE - 1) // titles._BATCH_SIZE
        self.assertEqual(mocked.call_count, expected_batches)
        self.assertEqual(save_mock.call_count, expected_batches)
        for session in sessions:
            entry = cache[titles.session_key(session)]
            self.assertEqual(entry["generation_state"], "failed")
            self.assertEqual(entry["generation_version"], titles.TITLE_CACHE_VERSION)
            self.assertIn("failed_at", entry)
            self.assertFalse(titles.resolve_initial_title(session, cache)[1])

        expired_cache = {
            titles.session_key(sessions[0]): {
                **cache[titles.session_key(sessions[0])],
                "generation_version": titles.TITLE_CACHE_VERSION - 1,
            }
        }
        self.assertTrue(titles.resolve_initial_title(sessions[0], expired_cache)[1])

    def test_refresh_titles_without_available_generator_writes_finished_state(self) -> None:
        sessions = [
            {"id": "offline", "source": "claude", "mtime": 1, "size_kb": 1,
             "fallback_title": "整理离线安装流程"},
        ]
        cache = {}

        with (
            mock.patch.object(titles.titlegen, "available_generators", return_value=()),
            mock.patch.object(titles, "save_cache") as save_mock,
        ):
            result = titles.refresh_titles(sessions, cache)

        self.assertEqual(result, {})
        save_mock.assert_called_once_with(cache)
        entry = cache["claude:offline"]
        self.assertEqual(entry["generation_state"], "failed")
        self.assertEqual(entry["generation_version"], titles.TITLE_CACHE_VERSION)
        with mock.patch.object(titles.titlegen, "gateway_key", return_value=None):
            self.assertEqual(titles.resolve_initial_title(sessions[0], cache), ("整理离线安装流程", False))

    def test_refresh_titles_partial_result_marks_missing_and_low_value_items_finished(self) -> None:
        sessions = [
            {"id": "ok", "source": "claude", "mtime": 1, "size_kb": 1,
             "fallback_title": "修复登录报错"},
            {"id": "missing", "source": "claude", "mtime": 1, "size_kb": 1,
             "fallback_title": "补充支付测试"},
            {"id": "low", "source": "claude", "mtime": 1, "size_kb": 1,
             "fallback_title": "整理部署流程"},
        ]
        cache = {}
        raw = {"claude:ok": "修复登录报错", "claude:low": "继续"}

        with (
            mock.patch.object(titles, "generate_titles_batch", return_value=raw),
            mock.patch.object(titles, "save_cache"),
        ):
            result = titles.refresh_titles(sessions, cache, generator=mock.Mock())

        self.assertEqual(result, {"claude:ok": "修复登录报错"})
        self.assertNotIn("generation_state", cache["claude:ok"])
        for key in ("claude:missing", "claude:low"):
            self.assertEqual(cache[key]["generation_state"], "failed")
        self.assertFalse(titles.resolve_initial_title(sessions[1], cache)[1])
        self.assertFalse(titles.resolve_initial_title(sessions[2], cache)[1])

    def test_refresh_titles_probes_bad_preferred_generator_only_once(self) -> None:
        sessions = [
            {"id": f"s{i}", "source": "claude", "mtime": 1, "size_kb": 1,
             "fallback_title": f"处理标题任务{i}"}
            for i in range(titles._BATCH_SIZE * 3)
        ]
        failed = mock.Mock()
        failed.id = "claude"
        fallback = mock.Mock()
        fallback.id = "codex"
        calls = {"claude": 0, "codex": 0}

        def fake_batch(chunk, generator, timeout=90):
            calls[generator.id] += 1
            if generator is failed:
                return {}
            return {titles.session_key(s): f"生成{s['id']}" for s in chunk}

        with (
            mock.patch.object(titles.titlegen, "available_generators", return_value=(failed, fallback)),
            mock.patch.object(titles, "generate_titles_batch", side_effect=fake_batch),
            mock.patch.object(titles, "save_cache"),
        ):
            result = titles.refresh_titles(sessions, {})

        self.assertEqual(len(result), len(sessions))
        self.assertEqual(calls["claude"], 1)
        self.assertEqual(calls["codex"], 3)

    def test_refresh_titles_saves_cache_per_batch(self) -> None:
        # 三批（_BATCH_SIZE 条/批）并行完成后，仍应每批落盘而非最后一次性写。
        sessions = [
            {"id": f"s{i}", "source": "claude", "mtime": 1, "size_kb": 1, "fallback_title": f"标题{i}"}
            for i in range(titles._BATCH_SIZE * 3)
        ]

        def fake_batch(chunk, model="haiku", timeout=90):
            return {titles.session_key(s): f"生成{s['id']}" for s in chunk}

        with mock.patch.object(titles, "generate_titles_batch", side_effect=fake_batch):
            with mock.patch.object(titles, "save_cache", return_value=None) as save_mock:
                result = titles.refresh_titles(sessions, {}, generator=mock.Mock())

        self.assertEqual(len(result), titles._BATCH_SIZE * 3)
        self.assertEqual(save_mock.call_count, 3)

    def test_refresh_titles_runs_max_batches_in_parallel(self) -> None:
        sessions = [
            {"id": f"s{i}", "source": "claude", "mtime": 1, "size_kb": 1, "fallback_title": f"标题{i}"}
            for i in range(titles._BATCH_SIZE * (titles._MAX_PARALLEL_BATCHES + 1))
        ]
        lock = threading.Lock()
        started = threading.Event()
        release = threading.Event()
        state = {"calls": 0, "active": 0, "max_active": 0}
        outcome = {}

        def fake_batch(chunk, generator, timeout=90):
            with lock:
                state["calls"] += 1
                # 第一批是串行健康探测，必须先正常完成；后面的批次才并发。
                if state["calls"] == 1:
                    return {titles.session_key(s): f"生成{s['id']}" for s in chunk}
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                if state["active"] == titles._MAX_PARALLEL_BATCHES:
                    started.set()
            if not release.wait(timeout=2):
                raise AssertionError("并行批次未在预期时间内释放")
            with lock:
                state["active"] -= 1
            return {titles.session_key(s): f"生成{s['id']}" for s in chunk}

        def run_refresh():
            try:
                outcome["result"] = titles.refresh_titles(sessions, {}, generator=mock.Mock())
            except BaseException as exc:
                outcome["error"] = exc

        with (
            mock.patch.object(titles, "generate_titles_batch", side_effect=fake_batch),
            mock.patch.object(titles, "save_cache", return_value=None),
        ):
            runner = threading.Thread(target=run_refresh)
            runner.start()
            try:
                self.assertTrue(started.wait(timeout=1))
                with lock:
                    self.assertEqual(state["active"], titles._MAX_PARALLEL_BATCHES)
                    self.assertEqual(state["max_active"], titles._MAX_PARALLEL_BATCHES)
            finally:
                release.set()
            runner.join(timeout=2)

        self.assertFalse(runner.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(len(outcome["result"]), len(sessions))
        self.assertEqual(state["calls"], titles._MAX_PARALLEL_BATCHES + 1)

    def test_title_daemon_drains_additional_pending_rounds(self) -> None:
        """持锁期间生成后再扫一轮，避免只扫一次就退出漏掉新会话。"""
        from corral import cli as corral_cli

        first = {
            "source": "claude",
            "id": "first",
            "mtime": 1,
            "size_kb": 1,
            "fallback_title": "第一轮待生成",
        }
        second = {
            "source": "claude",
            "id": "second",
            "mtime": 1,
            "size_kb": 1,
            "fallback_title": "第二轮才出现",
        }
        runtime = mock.Mock()
        runtime.id = "claude"
        scans = [{"claude": [first]}, {"claude": [first, second]}, {"claude": [first, second]}]
        runtime.scan_sessions.side_effect = lambda limit=None: scans.pop(0)["claude"]
        registry = corral.RuntimeRegistry((runtime,))
        refresh_calls: list[list[str]] = []
        shared_cache: dict = {}

        def fake_load_cache() -> dict:
            return dict(shared_cache)

        def fake_refresh(pending, cache, generator=None):
            refresh_calls.append([s["id"] for s in pending])
            for session in pending:
                key = titles.session_key(session)
                entry = {
                    "fp": titles._fingerprint(session),
                    "title": f"标题-{session['id']}",
                }
                cache[key] = entry
                shared_cache[key] = entry
            return {titles.session_key(s): f"标题-{s['id']}" for s in pending}

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(titles, "CACHE_DIR", td),
            mock.patch.object(corral_cli, "_TITLE_LOCK_FILE", os.path.join(td, "titles.lock")),
            mock.patch.object(titles, "load_cache", side_effect=fake_load_cache),
            mock.patch.object(titles, "refresh_titles", side_effect=fake_refresh),
            mock.patch.object(corral_cli, "_TITLE_DRAIN_MAX_ROUNDS", 3),
        ):
            with mock.patch.object(registry, "scan_all", side_effect=scans):
                corral_cli._run_title_daemon(registry, limit=20)

        self.assertEqual(refresh_calls, [["first"], ["second"]])

    def test_scan_sessions_memoizes_cwd_isdir_and_peek_skips_noise_and_dead_cwd(self) -> None:
        # 首屏 ≤1s 的回归防退化用例：不依赖真实数据。构造大量会话共享极少数
        # cwd，断言 (1) 内容只保留真实会话且排序正确；(2) os.path.isdir 按 cwd
        # 记忆化，不随会话条数线性增长；(3) 廉价预探提前跳过噪音/死 cwd 会话，
        # 不必等整文件解析完才丢弃。
        old_projects_dir = scan_claude.PROJECTS_DIR
        try:
            with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as workspace:
                scan_claude.PROJECTS_DIR = td
                project = Path(td) / "proj"
                # cwd 路径特意放在 PROJECTS_DIR 之外的独立目录树，避免被目录遍历
                # 循环自身的 os.path.isdir(proj_base) 检查提前访问，干扰调用计数。
                real_cwd = Path(workspace) / "real_cwd"
                real_cwd.mkdir()
                dead_cwd = str(Path(workspace) / "dead_cwd_does_not_exist")

                base_mtime = 1_800_000_000
                file_index = 0

                def write_session(content: str, cwd: str, minute_offset: int) -> Path:
                    nonlocal file_index
                    file_index += 1
                    path = project / f"s{file_index}.jsonl"
                    _write_jsonl(
                        path,
                        [
                            {
                                "type": "user",
                                "message": {"content": content},
                                "timestamp": "2026-06-22T08:11:26.000Z",
                                "cwd": cwd,
                            }
                        ],
                    )
                    mtime = base_mtime + minute_offset * 120  # 分钟桶两两不同，避免污染检测分支
                    os.utime(path, (mtime, mtime))
                    return path

                for i in range(5):
                    write_session(f"真实问题 {i}", str(real_cwd), minute_offset=i)
                noise_paths = [
                    write_session(f"{titles.PROMPT_MARKER} 摘录 {i}", str(real_cwd), minute_offset=10 + i)
                    for i in range(4)
                ]
                dead_cwd_paths = [
                    write_session(f"真实问题但目录已删 {i}", dead_cwd, minute_offset=20 + i)
                    for i in range(3)
                ]
                for i in range(2):
                    write_session_empty_path = project / f"empty{i}.jsonl"
                    _write_jsonl(
                        write_session_empty_path,
                        [{"type": "mode", "mode": "normal"}],
                    )
                    mtime = base_mtime + (30 + i) * 120
                    os.utime(write_session_empty_path, (mtime, mtime))

                real_isdir = scan_claude.os.path.isdir
                isdir_calls: list[str] = []

                def counting_isdir(path: str) -> bool:
                    isdir_calls.append(path)
                    return real_isdir(path)

                real_build = scan_claude._build_session_info
                build_calls: list[str] = []

                def counting_build(fpath: str, proj: str, host=None):
                    build_calls.append(fpath)
                    return real_build(fpath, proj, host)

                with (
                    mock.patch.object(scan_claude.os.path, "isdir", side_effect=counting_isdir),
                    mock.patch.object(scan_claude, "_build_session_info", side_effect=counting_build),
                ):
                    sessions = scan_claude.scan_sessions(limit=10, host=corral_host_extension())
        finally:
            scan_claude.PROJECTS_DIR = old_projects_dir

        # 1) 内容正确：只剩 5 条真实会话，噪音/死 cwd/空会话全部被过滤，按 mtime 降序。
        self.assertEqual(len(sessions), 5)
        self.assertEqual(
            [s["fallback_title"] for s in sessions],
            [f"真实问题 {i}" for i in range(4, -1, -1)],
        )

        # 2) 判活去重生效：5 个真实会话 + 4 个噪音会话都引用同一个 real_cwd，
        #    但真正落到 os.path.isdir(real_cwd) 的调用只有 1 次（记忆化）；
        #    3 个死 cwd 会话同理只触发 1 次。
        self.assertEqual(isdir_calls.count(str(real_cwd)), 1)
        self.assertEqual(isdir_calls.count(dead_cwd), 1)

        # 3) 预探跳噪音生效：噪音（4）和死 cwd（3）会话的完整解析被提前拦截，
        #    整文件解析只发生在 5 个真实会话 + 2 个空会话（peek 探测不到首条
        #    用户消息，只能落回完整解析兜底）身上，一共 7 次，而不是 5+4+3+2=14。
        self.assertEqual(len(build_calls), 7)
        for path in noise_paths + dead_cwd_paths:
            self.assertNotIn(str(path), build_calls)

    def test_live_session_ids_matches_alive_pid_and_skips_dead_pid(self) -> None:
        # 状态列判活：只有 pid 文件里的进程真的还存活才算 live。
        with tempfile.TemporaryDirectory() as td:
            alive_pid = os.getpid()  # 当前测试进程本身，保证存活
            dead_pid = 99999999  # 大概率不存在的 pid
            (Path(td) / f"{alive_pid}.json").write_text(
                json.dumps({"sessionId": "alive-session"}), encoding="utf-8"
            )
            (Path(td) / f"{dead_pid}.json").write_text(
                json.dumps({"sessionId": "dead-session"}), encoding="utf-8"
            )

            old_sessions_dir = scan_claude.SESSIONS_DIR
            scan_claude.SESSIONS_DIR = td
            try:
                live_ids = scan_claude._live_session_ids()
            finally:
                scan_claude.SESSIONS_DIR = old_sessions_dir

        self.assertIn("alive-session", live_ids)
        self.assertNotIn("dead-session", live_ids)
        self.assertEqual(live_ids["alive-session"], alive_pid)  # 判活的同时要能精确回填 pid


class CodexScanTests(TimezoneMixin, unittest.TestCase):
    def test_file_mtime_overrides_event_time_and_tail_keeps_first_line_for_small_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            uuid = "019efe42-6d51-7fb3-ad48-112a8eefa02b"
            path = Path(td) / f"rollout-2026-06-25T18-10-26-{uuid}.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "timestamp": "2026-06-25T10:10:40.522Z",
                        "type": "session_meta",
                        "payload": {
                            "id": uuid,
                            "timestamp": "2026-06-25T10:10:26.837Z",
                            "cwd": "/tmp/demo",
                        },
                    },
                    {
                        "timestamp": "2026-06-25T10:10:50.000Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "修复会话展示"},
                    },
                    {
                        "timestamp": "2026-06-25T10:12:15.876Z",
                        "type": "event_msg",
                        "payload": {"type": "task_complete", "last_agent_message": "已完成"},
                    },
                ],
            )
            os.utime(path, (946684800, 946684800))

            info = scan_codex._build_session_info(str(path), {})

            self.assertIsNotNone(info)
            self.assertEqual(info["display_time"], "01-01 08:00")
            self.assertEqual(format_message_time(info["event_time"]), "06-25 18:12")
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info["first_user_msg"], "修复会话展示")

    def test_build_session_info_does_not_crash_when_payload_is_json_null(self) -> None:
        # entry.get("payload", {}) 的默认值只在 key 缺失时生效；某些事件类型的
        # payload 字段可能是 JSON null（key 存在但值为 null），曾在后续
        # .get(...) 上直接 AttributeError 崩掉整个扫描。
        with tempfile.TemporaryDirectory() as td:
            uuid = "019efe42-6d51-7fb3-ad48-112a8eefa03c"
            path = Path(td) / f"rollout-2026-06-25T18-10-26-{uuid}.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "timestamp": "2026-06-25T10:10:40.522Z",
                        "type": "session_meta",
                        "payload": {"id": uuid, "cwd": "/tmp/demo"},
                    },
                    {"timestamp": "2026-06-25T10:10:45.000Z", "type": "noise_event", "payload": None},
                    {
                        "timestamp": "2026-06-25T10:10:50.000Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "真实用户消息"},
                    },
                ],
            )

            info = scan_codex._build_session_info(str(path), {})

        self.assertIsNotNone(info)
        self.assertEqual(info["first_user_msg"], "真实用户消息")

    def test_build_session_info_reads_modern_response_item_messages(self) -> None:
        """新版 Codex 将真实对话写入 response_item，不得误判为空会话。"""
        with tempfile.TemporaryDirectory() as td:
            uuid = "019efe42-6d51-7fb3-ad48-112a8eefa03d"
            path = Path(td) / f"rollout-2026-06-25T18-10-26-{uuid}.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "timestamp": "2026-06-25T10:10:40.522Z",
                        "type": "session_meta",
                        "payload": {"id": uuid, "cwd": td},
                    },
                    {
                        "timestamp": "2026-06-25T10:10:50.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "修复 Codex 会话标题"}],
                        },
                    },
                    {
                        "timestamp": "2026-06-25T10:11:10.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "正在排查"}],
                        },
                    },
                ],
            )

            info = scan_codex._build_session_info(str(path), {})
            conversation = scan_codex.load_conversation(str(path))

        self.assertIsNotNone(info)
        self.assertEqual(info["first_user_msg"], "修复 Codex 会话标题")
        self.assertEqual(info["fallback_title"], "修复 Codex 会话标题")
        self.assertEqual(info["last_user_msg"], "修复 Codex 会话标题")
        self.assertEqual(info["last_agent_msg"], "正在排查")
        self.assertEqual(
            [(message.role, message.text) for message in conversation],
            [("user", "修复 Codex 会话标题"), ("assistant", "正在排查")],
        )

    def test_entry_time_does_not_crash_when_payload_is_json_null(self) -> None:
        self.assertIsNone(scan_codex._entry_time({"payload": None}))

    def test_recovers_true_time_when_stale_session_file_gets_touched(self) -> None:
        """复现真实故障的 Codex 版本：会话内容 9 天前结束，文件之后被 touch

        （如同步、复制）把 mtime 顶到"现在"；时间列必须回退到真实事件时间。
        """
        with tempfile.TemporaryDirectory() as td:
            uuid = "019efe42-6d51-7fb3-ad48-112a8eefa02c"
            now = time.time()
            stale_event = now - 9 * 86400
            stale_dt = datetime.fromtimestamp(stale_event, tz=timezone.utc)
            path = Path(td) / f"rollout-{stale_dt.strftime('%Y-%m-%dT%H-%M-%S')}-{uuid}.jsonl"
            iso = stale_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            _write_jsonl(
                path,
                [
                    {
                        "timestamp": iso,
                        "type": "session_meta",
                        "payload": {"id": uuid, "timestamp": iso, "cwd": "/tmp/demo"},
                    },
                    {
                        "timestamp": iso,
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "9天前的真实问题"},
                    },
                ],
            )
            os.utime(path, (now, now))

            info = scan_codex._build_session_info(str(path), {})

            self.assertIsNotNone(info)
            self.assertEqual(info["time_source"], "event_time_stale_mtime")
            self.assertAlmostEqual(info["mtime"], stale_event, delta=2)

    def test_scan_sessions_memoizes_cwd_isdir_check(self) -> None:
        # 首屏 ≤1s 回归防退化：多个会话共享同一个 cwd 时，os.path.isdir 只应
        # 被真正调用一次（记忆化），不随会话条数线性增长。
        old_sessions_dir = scan_codex.SESSIONS_DIR
        old_session_index = scan_codex.SESSION_INDEX
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_codex.SESSIONS_DIR = td
                scan_codex.SESSION_INDEX = os.path.join(td, "session_index.jsonl")  # 不存在，_load_index 返回空
                real_cwd = Path(td) / "real_cwd"
                real_cwd.mkdir()

                for i in range(5):
                    uuid = f"019efe42-6d51-7fb3-ad48-112a8eefa0{i:02d}"
                    path = Path(td) / f"rollout-2026-06-25T18-1{i}-26-{uuid}.jsonl"
                    _write_jsonl(
                        path,
                        [
                            {
                                "timestamp": "2026-06-25T10:10:26.837Z",
                                "type": "session_meta",
                                "payload": {"id": uuid, "cwd": str(real_cwd)},
                            },
                            {
                                "timestamp": "2026-06-25T10:10:50.000Z",
                                "type": "event_msg",
                                "payload": {"type": "user_message", "message": f"真实问题 {i}"},
                            },
                        ],
                    )
                    mtime = 1_800_000_000 + i * 120
                    os.utime(path, (mtime, mtime))

                real_isdir = scan_codex.os.path.isdir
                isdir_calls: list[str] = []

                def counting_isdir(path: str) -> bool:
                    isdir_calls.append(path)
                    return real_isdir(path)

                with mock.patch.object(scan_codex.os.path, "isdir", side_effect=counting_isdir):
                    sessions = scan_codex.scan_sessions(limit=10)
        finally:
            scan_codex.SESSIONS_DIR = old_sessions_dir
            scan_codex.SESSION_INDEX = old_session_index

        self.assertEqual(len(sessions), 5)
        self.assertEqual(isdir_calls.count(str(real_cwd)), 1)

    def test_scan_sessions_filters_out_subagent_thread_spawns(self) -> None:
        # 复现真实故障：Codex 多智能体任务会把每个子代理线程写成独立的
        # rollout 文件，且 fork 时继承父会话开头的历史，导致同一段用户消息
        # 在列表里重复出现好几条。session_meta.payload.thread_source
        # == "subagent" 的文件不是用户发起的顶层会话，必须被过滤掉。
        old_sessions_dir = scan_codex.SESSIONS_DIR
        old_session_index = scan_codex.SESSION_INDEX
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_codex.SESSIONS_DIR = td
                scan_codex.SESSION_INDEX = os.path.join(td, "session_index.jsonl")

                root_uuid = "019f4b05-4930-7c31-9920-86c9f7a7570d"
                root_path = Path(td) / f"rollout-2026-07-10T15-54-25-{root_uuid}.jsonl"
                _write_jsonl(
                    root_path,
                    [
                        {
                            "timestamp": "2026-07-10T07:54:25.456Z",
                            "type": "session_meta",
                            "payload": {"id": root_uuid, "cwd": str(Path(td))},
                        },
                        {
                            "timestamp": "2026-07-10T07:54:26.000Z",
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": "原始需求"},
                        },
                    ],
                )
                os.utime(root_path, (1_800_000_000, 1_800_000_000))

                subagent_uuid = "019f4b30-fba4-75d1-abd1-d086ddd1699c"
                subagent_path = Path(td) / f"rollout-2026-07-10T16-42-09-{subagent_uuid}.jsonl"
                _write_jsonl(
                    subagent_path,
                    [
                        {
                            "timestamp": "2026-07-10T08:42:09.349Z",
                            "type": "session_meta",
                            "payload": {
                                "id": subagent_uuid,
                                "forked_from_id": root_uuid,
                                "cwd": str(Path(td)),
                                "thread_source": "subagent",
                                "agent_nickname": "Parfit",
                            },
                        },
                        {
                            "timestamp": "2026-07-10T08:42:09.400Z",
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": "原始需求"},
                        },
                    ],
                )
                os.utime(subagent_path, (1_800_000_120, 1_800_000_120))

                sessions = scan_codex.scan_sessions(limit=10)
            self.assertEqual([s["id"] for s in sessions], [root_uuid])
        finally:
            scan_codex.SESSIONS_DIR = old_sessions_dir
            scan_codex.SESSION_INDEX = old_session_index

    def test_live_session_ids_parses_uuid_from_proc_fd_on_linux(self) -> None:
        # Linux 判活：codex 进程持有自己的 rollout jsonl 文件描述符，遍历
        # /proc/<pid>/fd 逐个 readlink 抽出会话 UUID 即视为存活。
        uuid = "019f2c27-c9b0-7dc3-a600-8678bf0e8dcc"
        rollout_path = (
            f"/home/user/.codex/sessions/2026/07/04/rollout-2026-07-04T16-03-52-{uuid}.jsonl"
        )

        def fake_listdir(path):
            if path == "/proc/47372/fd":
                return ["0", "1", "2", "45"]
            raise AssertionError(f"unexpected listdir: {path}")

        def fake_readlink(path):
            if path == "/proc/47372/fd/45":
                return rollout_path
            raise OSError("not a symlink we care about")

        with mock.patch("corral.scan.codex.live_pid_snapshot", return_value=(47372,)), \
             mock.patch("corral.scan.codex.sys.platform", "linux"), \
             mock.patch("corral.scan.codex.os.listdir", side_effect=fake_listdir), \
             mock.patch("corral.scan.codex.os.readlink", side_effect=fake_readlink):
            live_ids = scan_codex._live_session_ids()

        self.assertEqual(live_ids, {uuid: 47372})  # 判活的同时要能精确回填 pid

    def test_live_session_ids_parses_uuid_from_lsof_on_macos(self) -> None:
        # macOS（无 /proc）判活：一次合并 lsof -Fpn 调用覆盖全部候选 pid，
        # 按 p<pid>/n<name> 字段行重建 pid -> 文件名对应关系。
        uuid = "019f2c27-c9b0-7dc3-a600-8678bf0e8dcc"
        lsof_output = (
            "p47372\n"
            f"n/private/tmp/codex/sessions/2026/07/04/"
            f"rollout-2026-07-04T16-03-52-{uuid}.jsonl\n"
        )

        def fake_check_output(cmd, **kwargs):
            if cmd[0] == "lsof":
                self.assertEqual(cmd, ["lsof", "-n", "-P", "-Fpn", "-p", "47372"])
                return lsof_output.encode()
            raise AssertionError(f"unexpected command: {cmd}")

        with mock.patch("corral.scan.codex.live_pid_snapshot", return_value=(47372,)), \
             mock.patch("corral.scan.codex.subprocess.check_output", side_effect=fake_check_output), \
             mock.patch("corral.scan.codex.sys.platform", "darwin"):
            live_ids = scan_codex._live_session_ids()

        self.assertEqual(live_ids, {uuid: 47372})  # 判活的同时要能精确回填 pid

    def test_live_session_ids_returns_empty_when_pgrep_unavailable(self) -> None:
        # 进程快照为空时不再调用 lsof，静默降级为空集。
        with mock.patch("corral.scan.codex.live_pid_snapshot", return_value=()), \
             mock.patch("corral.scan.codex.subprocess.check_output") as probe:
            live_ids = scan_codex._live_session_ids()

        self.assertEqual(live_ids, {})
        probe.assert_not_called()

    def test_scan_filters_self_generated_title_sessions(self) -> None:
        # 后台标题生成兜底路径若真在 ~/.codex/sessions/ 留下会话
        # (旧版 codex 无 --ephemeral,或用户手动跑过同款 prompt),
        # 必须像 Claude 侧一样按 PROMPT_MARKER 前缀过滤,不进列表。
        old_sessions_dir = scan_codex.SESSIONS_DIR
        old_session_index = scan_codex.SESSION_INDEX
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_codex.SESSIONS_DIR = td
                scan_codex.SESSION_INDEX = os.path.join(td, "session_index.jsonl")
                real_cwd = Path(td) / "real_cwd"
                real_cwd.mkdir()

                specs = [
                    ("019efe42-6d51-7fb3-ad48-112a8eefaa01", "真实的用户问题"),
                    ("019efe42-6d51-7fb3-ad48-112a8eefaa02", f"{titles.PROMPT_MARKER}(JSON 数组…)"),
                ]
                for i, (uuid, first_msg) in enumerate(specs):
                    path = Path(td) / f"rollout-2026-07-16T10-0{i}-00-{uuid}.jsonl"
                    _write_jsonl(
                        path,
                        [
                            {
                                "timestamp": "2026-07-16T02:00:00.000Z",
                                "type": "session_meta",
                                "payload": {"id": uuid, "cwd": str(real_cwd)},
                            },
                            {
                                "timestamp": "2026-07-16T02:00:10.000Z",
                                "type": "event_msg",
                                "payload": {"type": "user_message", "message": first_msg},
                            },
                        ],
                    )
                    mtime = 1_800_000_000 + i * 60
                    os.utime(path, (mtime, mtime))

                sessions = scan_codex.scan_sessions(limit=10, host=corral_host_extension())
        finally:
            scan_codex.SESSIONS_DIR = old_sessions_dir
            scan_codex.SESSION_INDEX = old_session_index

        self.assertEqual([s["first_user_msg"] for s in sessions], ["真实的用户问题"])

    def test_scan_keeps_live_session_before_first_user_message(self) -> None:
        """corral 新建后尚未输入的 Codex 会话也必须在下次进入时重新发现。"""
        old_sessions_dir = scan_codex.SESSIONS_DIR
        old_session_index = scan_codex.SESSION_INDEX
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_codex.SESSIONS_DIR = td
                scan_codex.SESSION_INDEX = os.path.join(td, "session_index.jsonl")
                real_cwd = Path(td) / "real_cwd"
                real_cwd.mkdir()
                live_uuid = "019efe42-6d51-7fb3-ad48-112a8eefaa11"
                ended_uuid = "019efe42-6d51-7fb3-ad48-112a8eefaa12"
                for offset, uuid in enumerate((live_uuid, ended_uuid)):
                    path = Path(td) / f"rollout-2026-07-16T10-0{offset}-00-{uuid}.jsonl"
                    _write_jsonl(
                        path,
                        [{
                            "timestamp": "2026-07-16T02:00:00.000Z",
                            "type": "session_meta",
                            "payload": {"id": uuid, "cwd": str(real_cwd)},
                        }],
                    )
                with mock.patch.object(scan_codex, "_live_session_ids", return_value={live_uuid: 47372}):
                    sessions = scan_codex.scan_sessions(limit=10)
        finally:
            scan_codex.SESSIONS_DIR = old_sessions_dir
            scan_codex.SESSION_INDEX = old_session_index

        self.assertEqual([session["id"] for session in sessions], [live_uuid])
        self.assertTrue(sessions[0]["live"])
        self.assertEqual(sessions[0]["fallback_title"], "Codex 新会话")

    def test_is_ephemeral_agent_cwd(self) -> None:
        from corral.scan.common import is_ephemeral_agent_cwd

        self.assertTrue(is_ephemeral_agent_cwd("/tmp/oc-manager-codex/run-1"))
        self.assertTrue(is_ephemeral_agent_cwd("/var/folders/xx/oc-manager-claude/work"))
        self.assertFalse(is_ephemeral_agent_cwd("/Users/me/Codes/corral"))
        self.assertFalse(is_ephemeral_agent_cwd(""))
        self.assertFalse(is_ephemeral_agent_cwd("/tmp/other-project"))

    def test_ephemeral_marker_hides_workspace_and_descendants(self) -> None:
        """Experiments drop .sesskit-ignore; plain /tmp work must still list."""
        from corral.scan.common import is_ephemeral_agent_cwd

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "butler-e1"
            run = root / "claude-1790653232" / "sub"
            run.mkdir(parents=True)
            self.assertFalse(is_ephemeral_agent_cwd(str(run)))
            (root / ".sesskit-ignore").touch()
            self.assertTrue(is_ephemeral_agent_cwd(str(root)))
            self.assertTrue(is_ephemeral_agent_cwd(str(run)))
            self.assertFalse(is_ephemeral_agent_cwd(td))

    def test_scan_filters_ephemeral_oc_manager_cwd(self) -> None:
        """OpenConductor 管家 /tmp/oc-manager-* 会话不进列表（目录复活也不刷屏）。"""
        old_sessions_dir = scan_codex.SESSIONS_DIR
        old_session_index = scan_codex.SESSION_INDEX
        try:
            with tempfile.TemporaryDirectory() as td:
                scan_codex.SESSIONS_DIR = td
                scan_codex.SESSION_INDEX = os.path.join(td, "session_index.jsonl")
                real_cwd = Path(td) / "real_cwd"
                real_cwd.mkdir()
                oc_cwd = Path(td) / "oc-manager-codex" / "job"
                oc_cwd.mkdir(parents=True)

                specs = [
                    ("019efe42-6d51-7fb3-ad48-112a8eefaa01", str(real_cwd), "真实用户问题"),
                    ("019efe42-6d51-7fb3-ad48-112a8eefaa02", str(oc_cwd), "管家自动任务"),
                ]
                for i, (uuid, cwd, first_msg) in enumerate(specs):
                    path = Path(td) / f"rollout-2026-07-16T10-0{i}-00-{uuid}.jsonl"
                    _write_jsonl(
                        path,
                        [
                            {
                                "timestamp": "2026-07-16T02:00:00.000Z",
                                "type": "session_meta",
                                "payload": {"id": uuid, "cwd": cwd},
                            },
                            {
                                "timestamp": "2026-07-16T02:00:10.000Z",
                                "type": "event_msg",
                                "payload": {"type": "user_message", "message": first_msg},
                            },
                        ],
                    )
                    mtime = 1_800_000_000 + i * 60
                    os.utime(path, (mtime, mtime))

                sessions = scan_codex.scan_sessions(limit=10, host=corral_host_extension())
        finally:
            scan_codex.SESSIONS_DIR = old_sessions_dir
            scan_codex.SESSION_INDEX = old_session_index

        self.assertEqual([s["first_user_msg"] for s in sessions], ["真实用户问题"])


class OpenCodeScanTests(TimezoneMixin, unittest.TestCase):
    """OpenCode 历史存 SQLite（session/message/part 三表），扫描用只读连接直接查询。"""

    def _text_part(self, part_id: str, message_id: str, session_id: str, text, t: int, synthetic: bool = False) -> dict:
        data = {"type": "text", "text": text}
        if synthetic:
            data["synthetic"] = True
        return {"id": part_id, "message_id": message_id, "session_id": session_id, "time_created": t, "data": data}

    def _scan_with_live(
        self,
        db_path,
        *,
        processes: list[tuple[int, str]],
        cmdlines: dict[int, str] | None = None,
        environ: dict[int, dict[str, str]] | None = None,
        starts: dict[int, float | None] | None = None,
        limit: int = 10,
        host=None,
    ) -> list:
        cmdlines = cmdlines or {}
        environ = environ or {}
        starts = starts or {}
        with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
             mock.patch.object(scan_opencode, "live_processes", return_value=processes), \
             mock.patch.object(
                 scan_opencode, "process_command_line",
                 side_effect=lambda pid: cmdlines.get(pid, "opencode --auto"),
             ), \
             mock.patch.object(
                 scan_opencode, "process_environ",
                 side_effect=lambda pid, **kwargs: environ.get(pid, {}),
             ), \
             mock.patch.object(
                 scan_opencode, "process_start_time",
                 side_effect=lambda pid: starts.get(pid),
             ):
            return scan_opencode.scan_sessions(limit=limit, host=host)

    def test_field_mapping_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[{
                    "id": "ses_a", "directory": "/tmp/demo", "title": "修复登录",
                    "time_created": 1_700_000_000_000, "time_updated": 1_700_000_500_000,
                }],
                messages=[
                    {"id": "msg_u1", "session_id": "ses_a", "time_created": 1_700_000_000_000,
                     "data": {"role": "user", "time": {"created": 1_700_000_000_000}}},
                    {"id": "msg_a1", "session_id": "ses_a", "time_created": 1_700_000_500_000,
                     "data": {"role": "assistant", "finish": "stop", "time": {"created": 1_700_000_500_000}}},
                ],
                parts=[
                    self._text_part("p1", "msg_u1", "ses_a", "帮我修复登录报错", 1_700_000_000_000),
                    self._text_part("p2", "msg_a1", "ses_a", "已定位并修复", 1_700_000_500_000),
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                sessions = scan_opencode.scan_sessions(limit=10)

            self.assertEqual(len(sessions), 1)
            info = sessions[0]
            self.assertEqual(info["source"], "opencode")
            self.assertEqual(info["id"], "ses_a")
            self.assertEqual(info["short_id"], "ses_a"[:12])
            self.assertEqual(info["cwd"], "/tmp/demo")
            self.assertEqual(info["native_title"], "修复登录")
            self.assertEqual(info["fallback_title"], "帮我修复登录报错")
            self.assertEqual(info["mtime"], 1_700_000_500_000 / 1000)
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(
                info["size_bytes"],
                len(json.dumps({"type": "text", "text": "帮我修复登录报错"}, ensure_ascii=False))
                + len(json.dumps({"type": "text", "text": "已定位并修复"}, ensure_ascii=False)),
            )
            self.assertEqual(info["path"], str(db_path))

    def test_filters_out_subagent_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_root", "directory": "/tmp/demo", "title": "根会话",
                     "time_created": 0, "time_updated": 100_000},
                    {"id": "ses_sub", "directory": "/tmp/demo", "title": "子代理会话",
                     "parent_id": "ses_root", "time_created": 0, "time_updated": 200_000},
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                sessions = scan_opencode.scan_sessions(limit=10)

            self.assertEqual([s["id"] for s in sessions], ["ses_root"])

    def test_filters_out_archived_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_live", "directory": "/tmp/demo", "title": "存活会话",
                     "time_created": 0, "time_updated": 100_000},
                    {"id": "ses_arch", "directory": "/tmp/demo", "title": "已归档会话",
                     "time_archived": 999_999, "time_created": 0, "time_updated": 200_000},
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                sessions = scan_opencode.scan_sessions(limit=10)

            self.assertEqual([s["id"] for s in sessions], ["ses_live"])

    def test_empty_session_without_title_is_dropped_but_native_title_alone_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_blank", "directory": "/tmp/demo", "title": None,
                     "time_created": 0, "time_updated": 100_000},
                    {"id": "ses_titled_only", "directory": "/tmp/demo", "title": "简单问候",
                     "time_created": 0, "time_updated": 200_000},
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                sessions = scan_opencode.scan_sessions(limit=10)

            self.assertEqual([s["id"] for s in sessions], ["ses_titled_only"])
            self.assertEqual(sessions[0]["fallback_title"], "(无消息)")

    def test_status_tag_all_branches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_pending", "directory": "/tmp/a", "title": "待回复",
                     "time_created": 0, "time_updated": 100_000},
                    {"id": "ses_done", "directory": "/tmp/b", "title": "已完成",
                     "time_created": 0, "time_updated": 200_000},
                    {"id": "ses_aborted", "directory": "/tmp/c", "title": "已中断",
                     "time_created": 0, "time_updated": 300_000},
                    {"id": "ses_none", "directory": "/tmp/d", "title": "无状态",
                     "time_created": 0, "time_updated": 400_000},
                ],
                messages=[
                    {"id": "m_pending", "session_id": "ses_pending", "time_created": 100_000,
                     "data": {"role": "user", "time": {"created": 100_000}}},
                    {"id": "m_done", "session_id": "ses_done", "time_created": 200_000,
                     "data": {"role": "assistant", "finish": "stop", "time": {"created": 200_000}}},
                    {"id": "m_aborted", "session_id": "ses_aborted", "time_created": 300_000,
                     "data": {"role": "assistant", "error": {"message": "连接失败"}, "time": {"created": 300_000}}},
                    {"id": "m_none", "session_id": "ses_none", "time_created": 400_000,
                     "data": {"role": "assistant", "finish": "tool-calls", "time": {"created": 400_000}}},
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                sessions = scan_opencode.scan_sessions(limit=10)

            by_id = {s["id"]: s["status_tag"] for s in sessions}
            self.assertEqual(by_id["ses_pending"], titles.STATUS_PENDING)
            self.assertEqual(by_id["ses_done"], titles.STATUS_DONE)
            self.assertEqual(by_id["ses_aborted"], titles.STATUS_ABORTED)
            self.assertEqual(by_id["ses_none"], titles.STATUS_NONE)

    def test_chinese_native_title_and_preview_survive_intact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[{
                    "id": "ses_cn", "directory": "/tmp/demo", "title": "中文标题：修复终端乱码问题",
                    "time_created": 0, "time_updated": 100_000,
                }],
                messages=[
                    {"id": "m1", "session_id": "ses_cn", "time_created": 0,
                     "data": {"role": "user", "time": {"created": 0}}},
                ],
                parts=[self._text_part("p1", "m1", "ses_cn", "终端显示的中文全是乱码，帮我看看", 0)],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                sessions = scan_opencode.scan_sessions(limit=10)

            self.assertEqual(sessions[0]["native_title"], "中文标题：修复终端乱码问题")
            self.assertEqual(sessions[0]["fallback_title"], "终端显示的中文全是乱码，帮我看看")

    def test_scan_sessions_returns_empty_when_db_missing(self) -> None:
        with mock.patch.object(scan_opencode, "_db_paths", return_value=[]):
            self.assertEqual(scan_opencode.scan_sessions(limit=10), [])

    def test_scan_signature_sorts_live_snapshot_and_detects_process_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            db_path.touch()

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes",
                                   return_value=[(22, "/tmp/b"), (11, "/tmp/a")]):
                first = scan_opencode.scan_signature()
            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes",
                                   return_value=[(11, "/tmp/a"), (22, "/tmp/b")]):
                reordered = scan_opencode.scan_signature()
            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes",
                                   return_value=[(11, "/tmp/a")]):
                changed = scan_opencode.scan_signature()

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)

    def test_scan_sessions_raises_when_all_existing_db_connections_fail(self) -> None:
        with mock.patch.object(scan_opencode, "_db_paths", return_value=["a.db", "b.db"]), \
             mock.patch.object(scan_opencode, "_connect_ro", return_value=None), \
             mock.patch.object(scan_opencode, "live_processes", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "所有 OpenCode 会话数据库均读取失败"):
                scan_opencode.scan_sessions(limit=10)

    def test_scan_sessions_keeps_successful_result_when_another_db_query_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            broken_path = Path(td) / "broken.db"
            broken_path.write_text("不是一个真正的 sqlite 文件", encoding="utf-8")
            healthy_path = Path(td) / "healthy.db"
            _make_opencode_db(
                healthy_path,
                sessions=[{
                    "id": "ses_healthy", "directory": "/tmp/demo", "title": "正常会话",
                    "time_created": 0, "time_updated": 100_000,
                }],
            )

            with mock.patch.object(
                scan_opencode, "_db_paths", return_value=[str(broken_path), str(healthy_path)]
            ), mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                sessions = scan_opencode.scan_sessions(limit=10)

        self.assertEqual([session["id"] for session in sessions], ["ses_healthy"])

    def test_scan_sessions_raises_when_db_is_corrupted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            db_path.write_text("不是一个真正的 sqlite 文件", encoding="utf-8")

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "所有 OpenCode 会话数据库均读取失败"):
                    scan_opencode.scan_sessions(limit=10)

    def test_scan_sessions_raises_when_tables_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            conn = sqlite3.connect(str(db_path))
            conn.close()  # 建一个空库，session/message/part 表都不存在

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "所有 OpenCode 会话数据库均读取失败"):
                    scan_opencode.scan_sessions(limit=10)

    def test_limit_keeps_newest_sessions_in_descending_order(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": f"ses_{i}", "directory": "/tmp/demo", "title": f"会话{i}",
                     "time_created": 0, "time_updated": i * 100_000}
                    for i in range(5)
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                sessions = scan_opencode.scan_sessions(limit=3)

            self.assertEqual([s["id"] for s in sessions], ["ses_4", "ses_3", "ses_2"])

    def test_live_flags_bind_session_flag_even_if_not_newest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_old", "directory": cwd, "title": "旧会话",
                     "time_created": 100_000, "time_updated": 100_000},
                    {"id": "ses_new", "directory": cwd, "title": "新会话",
                     "time_created": 200_000, "time_updated": 200_000},
                ],
            )
            sessions = self._scan_with_live(
                db_path,
                processes=[(4242, cwd)],
                cmdlines={4242: "opencode --auto -s ses_old"},
            )
            by_id = {s["id"]: s for s in sessions}
            self.assertTrue(by_id["ses_old"]["live"])
            self.assertEqual(by_id["ses_old"]["pid"], 4242)
            self.assertFalse(by_id["ses_new"]["live"])

    def test_live_flags_bind_full_corral_session_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            db_path = Path(td) / "opencode.db"
            session_id = "ses_ff7288ed5ffeOZOJxBKicWOzYk"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": session_id, "directory": cwd, "title": "恢复会话",
                     "time_created": 100_000, "time_updated": 100_000},
                    {"id": "ses_newer", "directory": cwd, "title": "更新的会话",
                     "time_created": 200_000, "time_updated": 200_000},
                ],
            )
            sessions = self._scan_with_live(
                db_path,
                processes=[(77, cwd)],
                cmdlines={77: "opencode --auto"},
                environ={77: {"CORRAL_SESSION_ID": session_id}},
                host=corral_host_extension(),
            )
            by_id = {s["id"]: s for s in sessions}
            self.assertTrue(by_id[session_id]["live"])
            self.assertEqual(by_id[session_id]["pid"], 77)
            self.assertFalse(by_id["ses_newer"]["live"])

    def test_live_flags_do_not_bind_short_placeholder_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[{
                    "id": "ses_ff7288ed5ffeOZOJxBKicWOzYk", "directory": cwd, "title": "旧历史",
                    "time_created": 100_000, "time_updated": 100_000,
                }],
            )
            sessions = self._scan_with_live(
                db_path,
                processes=[(88, cwd)],
                cmdlines={88: "opencode --auto"},
                environ={88: {"CORRAL_SESSION_ID": "745659f2"}},
            )
            self.assertFalse(sessions[0]["live"])

    def test_live_flags_same_cwd_two_tuis_bind_each_created_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_first", "directory": cwd, "title": "先开的",
                     "time_created": 1_700_000_100_000, "time_updated": 1_700_000_100_000},
                    {"id": "ses_second", "directory": cwd, "title": "后开的",
                     "time_created": 1_700_000_200_000, "time_updated": 1_700_000_200_000},
                ],
            )
            sessions = self._scan_with_live(
                db_path,
                processes=[(11, cwd), (22, cwd)],
                cmdlines={11: "opencode --auto", 22: "opencode --auto"},
                starts={11: 1_700_000_090.0, 22: 1_700_000_190.0},
            )
            by_id = {s["id"]: s for s in sessions}
            self.assertTrue(by_id["ses_first"]["live"])
            self.assertEqual(by_id["ses_first"]["pid"], 11)
            self.assertTrue(by_id["ses_second"]["live"])
            self.assertEqual(by_id["ses_second"]["pid"], 22)

    def test_live_flags_do_not_bind_new_tui_to_older_cwd_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[{
                    "id": "ses_old", "directory": cwd, "title": "更早的历史",
                    "time_created": 1_700_000_000_000, "time_updated": 1_700_000_500_000,
                }],
            )
            sessions = self._scan_with_live(
                db_path,
                processes=[(99, cwd)],
                cmdlines={99: "opencode --auto"},
                starts={99: 1_700_000_400.0},
            )
            self.assertFalse(sessions[0]["live"])

    def test_live_flags_skip_run_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[{
                    "id": "ses_run", "directory": cwd, "title": "标题生成",
                    "time_created": 1_700_000_100_000, "time_updated": 1_700_000_100_000,
                }],
            )
            sessions = self._scan_with_live(
                db_path,
                processes=[(55, cwd)],
                cmdlines={55: "opencode run --auto -s ses_run 请生成标题"},
                starts={55: 1_700_000_090.0},
            )
            self.assertFalse(sessions[0]["live"])

    def test_live_processes_degrades_when_pgrep_unavailable(self) -> None:
        with mock.patch("corral.scan.common.subprocess.check_output", side_effect=FileNotFoundError()):
            self.assertEqual(scan_opencode.live_processes("opencode"), [])

    def test_etime_seconds_parses_ps_formats(self) -> None:
        from corral.scan.common import _etime_seconds

        self.assertEqual(_etime_seconds("14:36"), 14 * 60 + 36)
        self.assertEqual(_etime_seconds("01:14:36"), 3600 + 14 * 60 + 36)
        self.assertEqual(_etime_seconds("1-01:14:36"), 86400 + 3600 + 14 * 60 + 36)
        self.assertIsNone(_etime_seconds(""))
        self.assertIsNone(_etime_seconds("nope"))

    def test_is_opencode_tui_cmdline(self) -> None:
        self.assertTrue(scan_opencode.is_opencode_tui_cmdline("opencode --auto"))
        self.assertTrue(scan_opencode.is_opencode_tui_cmdline("opencode --auto -s ses_x"))
        self.assertTrue(scan_opencode.is_opencode_tui_cmdline("opencode /tmp/proj"))
        self.assertTrue(scan_opencode.is_opencode_tui_cmdline(
            "opencode --auto --prompt Task: picking up a session from Pi. "
            "Start a new session and run the agent export"
        ))
        self.assertTrue(scan_opencode.is_opencode_tui_cmdline(
            "opencode --prompt=You are picking up a session from Pi"
        ))
        self.assertFalse(scan_opencode.is_opencode_tui_cmdline("opencode run --auto hello"))
        self.assertFalse(scan_opencode.is_opencode_tui_cmdline("opencode serve"))
        self.assertFalse(scan_opencode.is_opencode_tui_cmdline("opencode session list"))
        self.assertFalse(scan_opencode.is_opencode_tui_cmdline(
            "opencode run --prompt picking up a session"
        ))
        self.assertEqual(
            scan_opencode._session_id_from_cmdline("opencode --auto -s ses_real --prompt hello"),
            "ses_real",
        )
        self.assertIsNone(scan_opencode._session_id_from_cmdline(
            "opencode --auto --prompt Task: resume with -s ses_old from Pi"
        ))
        self.assertFalse(scan_opencode._is_continue_cmdline(
            "opencode --auto --prompt please -c continue the work"
        ))
        self.assertTrue(scan_opencode._is_continue_cmdline("opencode --auto -c"))

    def test_live_flags_bind_handoff_prompt_containing_subcommand_words(self) -> None:
        """接力 ``--prompt`` 正文里的 session/agent 不得把仍在跑的 TUI 判成非交互。"""
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[{
                    "id": "ses_handoff", "directory": cwd, "title": "接力会话",
                    "time_created": 1_700_000_100_000, "time_updated": 1_700_000_100_000,
                }],
            )
            sessions = self._scan_with_live(
                db_path,
                processes=[(42, cwd)],
                cmdlines={42: (
                    "opencode --auto --prompt Task: 审查今日代码变动并优化 "
                    "You are picking up a session from Pi. Start a new session "
                    "and continue the work; this is not a native resume."
                )},
                starts={42: 1_700_000_090.0},
            )
            self.assertTrue(sessions[0]["live"])
            self.assertEqual(sessions[0]["pid"], 42)

    def test_live_flags_ignore_session_flag_inside_handoff_prompt(self) -> None:
        """``--prompt`` 正文里的 ``-s ses_旧`` 不得把新建 TUI 错绑到被接力的历史上。"""
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {
                        "id": "ses_old", "directory": cwd, "title": "被接力的原会话",
                        "time_created": 1_700_000_050_000, "time_updated": 1_700_000_050_000,
                    },
                    {
                        "id": "ses_new", "directory": cwd, "title": "接力新建",
                        "time_created": 1_700_000_100_000, "time_updated": 1_700_000_100_000,
                    },
                ],
            )
            sessions = self._scan_with_live(
                db_path,
                processes=[(42, cwd)],
                cmdlines={42: (
                    "opencode --auto --prompt Task: picking up a session. "
                    "Original command was opencode --auto -s ses_old"
                )},
                starts={42: 1_700_000_090.0},
                host=corral_host_extension(),
            )
            by_id = {item["id"]: item for item in sessions}
            self.assertTrue(by_id["ses_new"]["live"])
            self.assertEqual(by_id["ses_new"]["pid"], 42)
            self.assertFalse(by_id["ses_old"]["live"])
        """标题生成落盘时即使 OpenCode 额外包一层引号，也必须过滤。"""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            noise_msg = f'"{titles.PROMPT_MARKER}（JSON 数组…）"'
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_real", "directory": td, "title": "真实会话",
                     "time_created": 0, "time_updated": 200_000},
                    {"id": "ses_noise", "directory": td, "title": "噪音标题",
                     "time_created": 0, "time_updated": 300_000},
                ],
                messages=[
                    {"id": "m_real", "session_id": "ses_real", "time_created": 100_000,
                     "data": {"role": "user", "time": {"created": 100_000}}},
                    {"id": "m_noise", "session_id": "ses_noise", "time_created": 200_000,
                     "data": {"role": "user", "time": {"created": 200_000}}},
                ],
                parts=[
                    self._text_part("p_real", "m_real", "ses_real", "真实的用户问题", 100_000),
                    self._text_part("p_noise", "m_noise", "ses_noise", noise_msg, 200_000),
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                sessions = scan_opencode.scan_sessions(limit=10, host=corral_host_extension())

        self.assertEqual([s["id"] for s in sessions], ["ses_real"])
        self.assertEqual(sessions[0]["first_user_msg"], "真实的用户问题")

    def test_scan_filters_title_sessions_even_when_only_native_title_has_marker(self) -> None:
        """OpenCode 有时把标题生成 JSON 写进原生标题，首条用户消息未必带标记。"""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_real", "directory": td, "title": "真实会话",
                     "time_created": 0, "time_updated": 200_000},
                    {"id": "ses_noise", "directory": td,
                     "title": f"{titles.PROMPT_MARKER}（JSON 数组…）",
                     "time_created": 0, "time_updated": 300_000},
                ],
                messages=[
                    {"id": "m_real", "session_id": "ses_real", "time_created": 100_000,
                     "data": {"role": "user", "time": {"created": 100_000}}},
                    {"id": "m_noise", "session_id": "ses_noise", "time_created": 200_000,
                     "data": {"role": "user", "time": {"created": 200_000}}},
                ],
                parts=[
                    self._text_part("p_real", "m_real", "ses_real", "真实的用户问题", 100_000),
                    self._text_part("p_noise", "m_noise", "ses_noise", "看起来像普通提问", 200_000),
                ],
            )

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                sessions = scan_opencode.scan_sessions(limit=10, host=corral_host_extension())

        self.assertEqual([s["id"] for s in sessions], ["ses_real"])

    def test_scan_overfetches_so_title_noise_does_not_hide_real_sessions(self) -> None:
        """比展示条数更新的标题生成噪音不得把更早的真实会话挤出列表。"""
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            noise_msg = f'"{titles.PROMPT_MARKER}（JSON 数组…）"'
            sessions = [
                {"id": "ses_real", "directory": td, "title": "真实会话",
                 "time_created": 0, "time_updated": 1_000},
            ]
            messages = [
                {"id": "m_real", "session_id": "ses_real", "time_created": 1_000,
                 "data": {"role": "user", "time": {"created": 1_000}}},
            ]
            parts = [self._text_part("p_real", "m_real", "ses_real", "真实的用户问题", 1_000)]
            for i in range(12):
                sid = f"ses_noise_{i}"
                mid = f"m_noise_{i}"
                pid = f"p_noise_{i}"
                ts = 10_000 + i
                sessions.append(
                    {"id": sid, "directory": td, "title": f"噪音{i}",
                     "time_created": 0, "time_updated": ts},
                )
                messages.append(
                    {"id": mid, "session_id": sid, "time_created": ts,
                     "data": {"role": "user", "time": {"created": ts}}},
                )
                parts.append(self._text_part(pid, mid, sid, noise_msg, ts))
            _make_opencode_db(db_path, sessions=sessions, messages=messages, parts=parts)

            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                found = scan_opencode.scan_sessions(limit=1, host=corral_host_extension())

        self.assertEqual([s["id"] for s in found], ["ses_real"])

    def test_load_conversation_merges_parts_filters_roles_and_avoids_none_literal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[{"id": "ses_multi", "directory": "/tmp/demo", "title": "多段消息",
                           "time_created": 0, "time_updated": 300_000}],
                messages=[
                    {"id": "m_user", "session_id": "ses_multi", "time_created": 100_000,
                     "data": {"role": "user", "time": {"created": 100_000}}},
                    {"id": "m_asst", "session_id": "ses_multi", "time_created": 200_000,
                     "data": {"role": "assistant", "finish": "stop", "time": {"created": 200_000}}},
                    {"id": "m_null", "session_id": "ses_multi", "time_created": 300_000,
                     "data": {"role": "assistant", "finish": "stop", "time": {"created": 300_000}}},
                ],
                parts=[
                    self._text_part("p_user", "m_user", "ses_multi", "先看看这个报错", 100_000),
                    # 同一条 assistant 消息的两段 text part（step 之间产生），应合并成一条
                    self._text_part("p_a1", "m_asst", "ses_multi", "第一段结论", 200_000),
                    self._text_part("p_a2", "m_asst", "ses_multi", "第二段结论", 200_001),
                    # 非 text 类型的 part 不应出现在对话里
                    {"id": "p_reason", "message_id": "m_asst", "session_id": "ses_multi",
                     "time_created": 200_002, "data": {"type": "reasoning", "text": "思考过程"}},
                    {"id": "p_tool", "message_id": "m_asst", "session_id": "ses_multi",
                     "time_created": 200_003, "data": {"type": "tool", "tool": "bash"}},
                    # synthetic 注入内容应被过滤
                    self._text_part("p_synthetic", "m_asst", "ses_multi", "系统注入内容", 200_004, synthetic=True),
                    # text 为 JSON null 时不应产出字面量 "None"
                    self._text_part("p_null", "m_null", "ses_multi", None, 300_000),
                ],
            )

            messages = scan_opencode.load_conversation(str(db_path), "ses_multi")

            self.assertEqual([m.role for m in messages], ["user", "assistant"])
            self.assertEqual(messages[0].text, "先看看这个报错")
            self.assertEqual(messages[1].text, "第一段结论\n\n第二段结论")
            self.assertNotIn("None", messages[1].text)
            self.assertEqual(messages[0].timestamp, 100_000 / 1000)
            self.assertEqual(messages[1].timestamp, 200_000 / 1000)
            ts = [m.timestamp for m in messages if m.timestamp is not None]
            self.assertEqual(ts, sorted(ts))


def _make_kimi_session(sessions_dir: Path, workspace: str, session_id: str,
                       state: dict, wire_rows: list[dict]) -> Path:
    """在临时 sessions 目录下落一份 Kimi 会话（state.json + agents/main/wire.jsonl）。"""
    session_dir = sessions_dir / workspace / session_id
    (session_dir / "agents" / "main").mkdir(parents=True, exist_ok=True)
    (session_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    wire = session_dir / "agents" / "main" / "wire.jsonl"
    wire.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in wire_rows) + "\n", encoding="utf-8")
    return session_dir


def _kimi_user_event(text: str, t: int, origin_kind: str = "user") -> dict:
    return {"type": "context.append_message", "time": t,
            "message": {"role": "user", "content": [{"type": "text", "text": text}],
                        "origin": {"kind": origin_kind}}}


def _kimi_assistant_text(text: str, t: int) -> dict:
    return {"type": "context.append_loop_event", "time": t,
            "event": {"type": "content.part", "part": {"type": "text", "text": text}}}


def _kimi_assistant_think(text: str, t: int) -> dict:
    return {"type": "context.append_loop_event", "time": t,
            "event": {"type": "content.part", "part": {"type": "think", "think": text}}}


class KimiScanTests(TimezoneMixin, unittest.TestCase):
    """Kimi Code 历史按「工作区 / 会话」两级目录存放，元数据取 state.json，正文解析 wire.jsonl。"""

    def _scan(self, sessions_dir: Path, **kwargs) -> list[dict]:
        with mock.patch.object(scan_kimi, "SESSIONS_DIR", str(sessions_dir)), \
             mock.patch.object(scan_kimi, "live_processes", return_value=[]):
            return scan_kimi.scan_sessions(**kwargs)

    def _scan_with_live(
        self,
        sessions_dir: Path,
        *,
        processes: list[tuple[int, str]],
        cmdlines: dict[int, str] | None = None,
        environ: dict[int, dict[str, str]] | None = None,
        starts: dict[int, float | None] | None = None,
        limit: int = 10,
        host=None,
    ) -> list:
        cmdlines = cmdlines or {}
        environ = environ or {}
        starts = starts or {}
        with mock.patch.object(scan_kimi, "SESSIONS_DIR", str(sessions_dir)), \
             mock.patch.object(scan_kimi, "live_processes", return_value=processes), \
             mock.patch.object(
                 scan_kimi, "process_command_line",
                 side_effect=lambda pid: cmdlines.get(pid, "kimi -y"),
             ), \
             mock.patch.object(
                 scan_kimi, "process_environ",
                 side_effect=lambda pid, **kwargs: environ.get(pid, {}),
             ), \
             mock.patch.object(
                 scan_kimi, "process_start_time",
                 side_effect=lambda pid: starts.get(pid),
             ):
            return scan_kimi.scan_sessions(limit=limit, host=host)

    def test_scan_signature_tracks_file_stat_not_parent_dir_mtime(self) -> None:
        """祖先目录 touch 不得改变签名；wire 追加必须改变。"""
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_sig",
                state={"title": "签名", "workDir": td, "lastPrompt": "hi",
                       "createdAt": "2026-07-17T08:00:00.000Z", "updatedAt": "2026-07-17T08:05:00.000Z"},
                wire_rows=[_kimi_user_event("hello", 1_784_275_205_000)],
            )
            with mock.patch.object(scan_kimi, "SESSIONS_DIR", str(sessions_dir)), mock.patch.object(
                scan_kimi, "live_pid_snapshot", return_value=()
            ):
                first = scan_kimi.scan_signature()
                os.utime(sessions_dir / "wd_demo", None)
                after_dir = scan_kimi.scan_signature()
                wire = sessions_dir / "wd_demo" / "session_sig" / "agents" / "main" / "wire.jsonl"
                wire.write_text(wire.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
                after_file = scan_kimi.scan_signature()
        self.assertEqual(first, after_dir)
        self.assertNotEqual(first, after_file)

    def test_field_mapping_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_abc",
                state={"title": "修复登录", "workDir": td, "lastPrompt": "在吗",
                       "createdAt": "2026-07-17T08:00:00.000Z", "updatedAt": "2026-07-17T08:05:00.000Z"},
                wire_rows=[
                    {"type": "metadata", "protocol_version": "1.4", "created_at": 1_784_275_200_000},
                    {"type": "config.update", "systemPrompt": "You are Kimi " * 500, "time": 1_784_275_200_000},
                    _kimi_user_event("帮我修复登录报错", 1_784_275_205_000),
                    _kimi_assistant_think("先看一下报错栈", 1_784_275_206_000),
                    _kimi_assistant_text("已定位并修复", 1_784_275_207_000),
                ],
            )
            sessions = self._scan(sessions_dir, limit=10)

            self.assertEqual(len(sessions), 1)
            info = sessions[0]
            self.assertEqual(info["source"], "kimi")
            self.assertEqual(info["id"], "session_abc")
            self.assertEqual(info["short_id"], "abc")
            self.assertEqual(info["cwd"], td)
            self.assertEqual(info["native_title"], "修复登录")
            self.assertEqual(info["fallback_title"], "修复登录")
            self.assertEqual(info["status_tag"], titles.STATUS_DONE)
            self.assertEqual(info["first_user_msg"], "帮我修复登录报错")
            self.assertEqual(info["last_agent_msg"], "已定位并修复")
            self.assertEqual(
                info["path"],
                str(sessions_dir / "wd_demo" / "session_abc" / "agents" / "main" / "wire.jsonl"),
            )
            # updatedAt 权威时间优先于文件 mtime
            self.assertEqual(info["mtime"], scan_kimi._parse_iso("2026-07-17T08:05:00.000Z"))

    def test_pending_status_when_last_event_is_user(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_pending",
                state={"title": "待回复", "workDir": td, "updatedAt": "2026-07-17T08:05:00.000Z"},
                wire_rows=[
                    _kimi_user_event("第一个问题", 1_784_275_205_000),
                    _kimi_assistant_text("这是回答", 1_784_275_206_000),
                    _kimi_user_event("追问一下", 1_784_275_207_000),
                ],
            )
            sessions = self._scan(sessions_dir, limit=10)
            self.assertEqual(sessions[0]["status_tag"], titles.STATUS_PENDING)
            self.assertEqual(sessions[0]["last_user_msg"], "追问一下")

    def test_empty_session_is_dropped_but_title_only_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_blank",
                state={"title": None, "workDir": td, "updatedAt": "2026-07-17T08:01:00.000Z"},
                wire_rows=[{"type": "metadata", "protocol_version": "1.4", "created_at": 1_784_275_200_000}],
            )
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_titled",
                state={"title": "简单问候", "workDir": td, "updatedAt": "2026-07-17T08:02:00.000Z"},
                wire_rows=[{"type": "metadata", "protocol_version": "1.4", "created_at": 1_784_275_200_000}],
            )
            sessions = self._scan(sessions_dir, limit=10)
            self.assertEqual([s["id"] for s in sessions], ["session_titled"])
            self.assertEqual(sessions[0]["fallback_title"], "简单问候")

    def test_blank_title_and_last_prompt_do_not_crash_scan(self) -> None:
        # 真实故障复现：title/lastPrompt 是纯空白字符串（如 " "）而非缺失时，
        # strip() 后为空串，splitlines()[0] 曾抛 IndexError 崩掉整个扫描。
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_blank_title",
                state={"title": " ", "workDir": td, "lastPrompt": "\n",
                       "updatedAt": "2026-07-17T08:01:00.000Z"},
                wire_rows=[
                    _kimi_user_event("真实用户消息", 1_784_275_205_000),
                ],
            )
            sessions = self._scan(sessions_dir, limit=10)
            self.assertEqual([s["id"] for s in sessions], ["session_blank_title"])
            self.assertEqual(sessions[0]["fallback_title"], "真实用户消息")

    def test_dead_cwd_session_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_dead",
                state={"title": "旧任务", "workDir": "/tmp/does-not-exist-kimi-test",
                       "updatedAt": "2026-07-17T08:05:00.000Z"},
                wire_rows=[_kimi_user_event("你好", 1_784_275_205_000)],
            )
            self.assertEqual(self._scan(sessions_dir, limit=10), [])

    def test_scan_filters_self_generated_title_sessions(self) -> None:
        """标题生成 `kimi -p` 落盘会话必须以 PROMPT_MARKER 前缀过滤。"""
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            noise_msg = f"{titles.PROMPT_MARKER}（JSON 数组…）"
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_real",
                state={"title": "真实会话", "workDir": td, "updatedAt": "2026-07-17T08:05:00.000Z"},
                wire_rows=[_kimi_user_event("真实的用户问题", 1_784_275_205_000)],
            )
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_noise_user",
                state={"title": "噪音", "workDir": td, "updatedAt": "2026-07-17T08:06:00.000Z"},
                wire_rows=[_kimi_user_event(noise_msg, 1_784_275_206_000)],
            )
            # 无 wire 用户消息时 fallback 取自 lastPrompt，也应被 fallback 前缀拦住。
            _make_kimi_session(
                sessions_dir, "wd_demo", "session_noise_prompt",
                state={"title": None, "workDir": td, "lastPrompt": noise_msg,
                       "updatedAt": "2026-07-17T08:07:00.000Z"},
                wire_rows=[{"type": "metadata", "protocol_version": "1.4", "created_at": 1_784_275_200_000}],
            )
            sessions = self._scan(sessions_dir, limit=10)

        self.assertEqual([s["id"] for s in sessions], ["session_real"])
        self.assertEqual(sessions[0]["first_user_msg"], "真实的用户问题")

    def test_scan_returns_empty_when_dir_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(self._scan(Path(td) / "nope", limit=10), [])

    def test_load_conversation_filters_think_and_merges_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wire = Path(td) / "wire.jsonl"
            _write_jsonl(wire, [
                {"type": "metadata", "protocol_version": "1.4", "created_at": 1_784_275_200_000},
                {"type": "config.update", "systemPrompt": "system noise", "time": 1_784_275_200_000},
                _kimi_user_event("先看看这个报错", 100_000),
                _kimi_assistant_think("我先想想", 150_000),
                _kimi_assistant_text("第一段结论", 200_000),
                _kimi_assistant_text("第二段结论", 201_000),
                _kimi_user_event("好的继续", 300_000),
                _kimi_assistant_text("已完成", 400_000),
            ])
            messages = scan_kimi.load_conversation(str(wire))

            self.assertEqual([m.role for m in messages], ["user", "assistant", "user", "assistant"])
            self.assertEqual(messages[1].text, "第一段结论\n\n第二段结论")  # 同轮文本合并，思考剔除
            self.assertEqual(messages[0].timestamp, 100_000 / 1000)
            self.assertEqual(messages[1].timestamp, 200_000 / 1000)  # 取该轮首个文本分片时间
            self.assertEqual(messages[3].text, "已完成")

    def test_load_conversation_drops_system_origin_user_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            wire = Path(td) / "wire.jsonl"
            _write_jsonl(wire, [
                _kimi_user_event("真人提问", 100_000),
                _kimi_user_event("系统注入的事件", 150_000, origin_kind="task-notification"),
                _kimi_assistant_text("回答", 200_000),
            ])
            messages = scan_kimi.load_conversation(str(wire))
            self.assertEqual([m.role for m in messages], ["user", "assistant"])
            self.assertEqual(messages[0].text, "真人提问")

    def test_live_flags_bind_session_flag_even_if_not_newest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd", "session_old",
                state={"title": "旧会话", "workDir": cwd,
                       "createdAt": "2023-11-14T22:00:00.000Z",
                       "updatedAt": "2023-11-14T22:00:00.000Z"},
                wire_rows=[_kimi_user_event("旧问题", 1_700_000_000_000)],
            )
            _make_kimi_session(
                sessions_dir, "wd", "session_new",
                state={"title": "新会话", "workDir": cwd,
                       "createdAt": "2023-11-14T22:10:00.000Z",
                       "updatedAt": "2023-11-14T22:10:00.000Z"},
                wire_rows=[_kimi_user_event("新问题", 1_700_000_600_000)],
            )
            sessions = self._scan_with_live(
                sessions_dir,
                processes=[(4242, cwd)],
                cmdlines={4242: "kimi -y -S session_old"},
            )
            by_id = {s["id"]: s for s in sessions}
            self.assertTrue(by_id["session_old"]["live"])
            self.assertEqual(by_id["session_old"]["pid"], 4242)
            self.assertFalse(by_id["session_new"]["live"])

    def test_live_flags_bind_full_corral_session_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            session_id = "session_ff7288ed5ffeOZOJxBKicWOzYk"
            _make_kimi_session(
                sessions_dir, "wd", session_id,
                state={"title": "恢复会话", "workDir": cwd,
                       "createdAt": "2023-11-14T22:00:00.000Z",
                       "updatedAt": "2023-11-14T22:00:00.000Z"},
                wire_rows=[_kimi_user_event("恢复", 1_700_000_000_000)],
            )
            _make_kimi_session(
                sessions_dir, "wd", "session_newer",
                state={"title": "更新的会话", "workDir": cwd,
                       "createdAt": "2023-11-14T22:10:00.000Z",
                       "updatedAt": "2023-11-14T22:10:00.000Z"},
                wire_rows=[_kimi_user_event("更新", 1_700_000_600_000)],
            )
            sessions = self._scan_with_live(
                sessions_dir,
                processes=[(77, cwd)],
                cmdlines={77: "kimi -y"},
                environ={77: {"CORRAL_SESSION_ID": session_id}},
                host=corral_host_extension(),
            )
            by_id = {s["id"]: s for s in sessions}
            self.assertTrue(by_id[session_id]["live"])
            self.assertEqual(by_id[session_id]["pid"], 77)
            self.assertFalse(by_id["session_newer"]["live"])

    def test_live_flags_do_not_bind_short_placeholder_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd", "session_ff7288ed5ffeOZOJxBKicWOzYk",
                state={"title": "旧历史", "workDir": cwd,
                       "createdAt": "2023-11-14T22:00:00.000Z",
                       "updatedAt": "2023-11-14T22:00:00.000Z"},
                wire_rows=[_kimi_user_event("旧问题", 1_700_000_000_000)],
            )
            sessions = self._scan_with_live(
                sessions_dir,
                processes=[(88, cwd)],
                cmdlines={88: "kimi -y"},
                environ={88: {"CORRAL_SESSION_ID": "745659f2"}},
            )
            self.assertFalse(sessions[0]["live"])

    def test_live_flags_same_cwd_two_tuis_bind_each_created_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd", "session_first",
                state={"title": "先开的", "workDir": cwd,
                       "createdAt": "2023-11-14T22:15:00.000Z",
                       "updatedAt": "2023-11-14T22:15:00.000Z"},
                wire_rows=[_kimi_user_event("先开", 1_700_000_100_000)],
            )
            _make_kimi_session(
                sessions_dir, "wd", "session_second",
                state={"title": "后开的", "workDir": cwd,
                       "createdAt": "2023-11-14T22:16:40.000Z",
                       "updatedAt": "2023-11-14T22:16:40.000Z"},
                wire_rows=[_kimi_user_event("后开", 1_700_000_200_000)],
            )
            sessions = self._scan_with_live(
                sessions_dir,
                processes=[(11, cwd), (22, cwd)],
                cmdlines={11: "kimi -y", 22: "kimi -y"},
                starts={11: 1_700_000_090.0, 22: 1_700_000_190.0},
            )
            by_id = {s["id"]: s for s in sessions}
            self.assertTrue(by_id["session_first"]["live"])
            self.assertEqual(by_id["session_first"]["pid"], 11)
            self.assertTrue(by_id["session_second"]["live"])
            self.assertEqual(by_id["session_second"]["pid"], 22)

    def test_live_flags_do_not_bind_new_tui_to_older_cwd_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd", "session_old",
                state={"title": "更早的历史", "workDir": cwd,
                       "createdAt": "2023-11-14T22:13:20.000Z",
                       "updatedAt": "2023-11-14T22:20:00.000Z"},
                wire_rows=[_kimi_user_event("旧问题", 1_700_000_000_000)],
            )
            sessions = self._scan_with_live(
                sessions_dir,
                processes=[(99, cwd)],
                cmdlines={99: "kimi -y"},
                starts={99: 1_700_000_400.0},
            )
            self.assertFalse(sessions[0]["live"])

    def test_live_flags_skip_prompt_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "workdir"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            _make_kimi_session(
                sessions_dir, "wd", "session_run",
                state={"title": "标题生成", "workDir": cwd,
                       "createdAt": "2023-11-14T22:15:00.000Z",
                       "updatedAt": "2023-11-14T22:15:00.000Z"},
                wire_rows=[_kimi_user_event("请生成标题", 1_700_000_100_000)],
            )
            sessions = self._scan_with_live(
                sessions_dir,
                processes=[(55, cwd)],
                cmdlines={55: "kimi -y -p 请生成标题"},
                starts={55: 1_700_000_090.0},
            )
            self.assertFalse(sessions[0]["live"])

    def test_is_kimi_tui_cmdline(self) -> None:
        self.assertTrue(scan_kimi.is_kimi_tui_cmdline("kimi -y"))
        self.assertTrue(scan_kimi.is_kimi_tui_cmdline("kimi -y -S session_x"))
        self.assertTrue(scan_kimi.is_kimi_tui_cmdline("kimi --auto"))
        self.assertTrue(scan_kimi.is_kimi_tui_cmdline("kimi --auto -S session_x"))
        self.assertTrue(scan_kimi.is_kimi_tui_cmdline("kimi --session=session_x"))
        self.assertFalse(scan_kimi.is_kimi_tui_cmdline("kimi -p hello"))
        self.assertFalse(scan_kimi.is_kimi_tui_cmdline("kimi --prompt=hello"))
        self.assertFalse(scan_kimi.is_kimi_tui_cmdline("kimi server"))
        self.assertFalse(scan_kimi.is_kimi_tui_cmdline("kimi web"))


class ConversationPreviewTests(unittest.TestCase):
    def test_claude_conversation_keeps_assistant_text_even_before_tool_calls(self) -> None:
        """真实 JSONL 里一次 assistant 轮次的 thinking/text/tool_use 是各自独立的行，且共享同一个
        stop_reason；文本紧跟着工具调用时该行的 stop_reason 也是 tool_use，不代表这段文本不重要
        （历史上按 stop_reason 过滤把这类文本整段丢了，是本用例要回归防止的 bug）。"""
        entries = [
            {"type": "user", "message": {"content": "第一个问题"}},
            {
                "type": "assistant",
                "message": {"stop_reason": "tool_use", "content": [{"type": "text", "text": "工具调用前的说明"}]},
            },
            {
                "type": "assistant",
                "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "Bash"}]},
            },
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "工具结果"}]}},
            {
                "type": "assistant",
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "第一个最终答复"}]},
            },
            {"type": "user", "isMeta": True, "message": {"content": "内部提醒"}},
            {"type": "user", "message": {"content": "第二个问题"}},
            {
                "type": "assistant",
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "第二个最终答复"}]},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_claude.load_conversation(str(path))

        self.assertEqual(
            [(message.role, message.text) for message in messages],
            [
                ("user", "第一个问题"),
                ("assistant", "工具调用前的说明"),
                ("assistant", "第一个最终答复"),
                ("user", "第二个问题"),
                ("assistant", "第二个最终答复"),
            ],
        )

    def test_claude_conversation_drops_task_notification_system_events(self) -> None:
        """Monitor/task-notification 事件在原始记录里也挂在 user 轮次下（`origin.kind` 是
        "task-notification" 而不是 "human"），价值很低，消息历史只保留 Agent 和真人的对话，
        这类系统事件应整条丢弃，不进入返回结果（不是标个 role 展示出来，是完全不展示）。"""
        entries = [
            {
                "type": "user",
                "origin": {"kind": "human"},
                "message": {"content": "真人提问"},
            },
            {
                "type": "user",
                "origin": {"kind": "task-notification"},
                "message": {"content": "<task-notification>系统事件内容</task-notification>"},
            },
            {
                "type": "user",
                "message": {"content": "/plan 之类无 origin 字段但仍是真人输入"},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_claude.load_conversation(str(path))

        self.assertEqual(
            [(message.role, message.text) for message in messages],
            [
                ("user", "真人提问"),
                ("user", "/plan 之类无 origin 字段但仍是真人输入"),
            ],
        )

    def test_codex_conversation_keeps_commentary_and_removes_task_complete_duplicate(self) -> None:
        entries = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "用户问题"}},
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "commentary", "message": "处理中间状态"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "final_answer", "message": "最终答复"},
            },
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "最终答复"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_codex.load_conversation(str(path))

        self.assertEqual(
            [(message.role, message.text) for message in messages],
            [("user", "用户问题"), ("assistant", "处理中间状态"), ("assistant", "最终答复")],
        )

    def test_codex_conversation_dedupes_response_item_and_event_msg_user(self) -> None:
        """新版 Codex 同一句真人输入会各写一遍 response_item 和 event_msg。

        预览 / Your prompts 必须只留先到的那条，时间戳也跟它走；助手侧已有
        相邻正文去重，用户侧漏了就会成对出现。不相邻的同一句仍是两轮。
        """
        entries = [
            {
                "timestamp": "2026-07-01T10:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "用户问题"}],
                },
            },
            {
                "timestamp": "2026-07-01T10:00:00.100Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "用户问题"},
            },
            {
                "timestamp": "2026-07-01T10:00:03Z",
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "final_answer", "message": "最终答复"},
            },
            {
                "timestamp": "2026-07-01T10:00:10Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "用户问题"}],
                },
            },
            {
                "timestamp": "2026-07-01T10:00:10.100Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "用户问题"},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_codex.load_conversation(str(path))

        self.assertEqual(
            [(message.role, message.text) for message in messages],
            [("user", "用户问题"), ("assistant", "最终答复"), ("user", "用户问题")],
        )
        self.assertEqual(
            messages[0].timestamp,
            scan_codex._parse_timestamp("2026-07-01T10:00:00Z"),
        )
        self.assertEqual(
            messages[2].timestamp,
            scan_codex._parse_timestamp("2026-07-01T10:00:10Z"),
        )

    def test_codex_conversation_ignores_null_message_instead_of_literal_none(self) -> None:
        """payload 里的字段即使有 key，值也可能是 JSON null（如任务无输出就结束的
        task_complete）；`payload.get(key, "")` 只在 key 缺失时才用默认值，key 存在但值为
        null 时会拿到 None，`str(None)` 会变成字面量 "None" 混进正文——回归用例覆盖这个坑。"""
        entries = [
            {"type": "event_msg", "payload": {"type": "user_message", "message": "问题"}},
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "phase": "final_answer", "message": None},
            },
            {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": None}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_codex.load_conversation(str(path))

        self.assertEqual([(message.role, message.text) for message in messages], [("user", "问题")])

    def test_claude_conversation_carries_message_timestamp(self) -> None:
        entries = [
            {"type": "user", "timestamp": "2026-07-01T10:00:00Z", "message": {"content": "第一个问题"}},
            {
                "type": "assistant",
                "timestamp": "2026-07-01T10:00:05Z",
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "第一个答复"}]},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_claude.load_conversation(str(path))

        self.assertEqual([message.timestamp for message in messages],
                          [scan_claude._parse_timestamp("2026-07-01T10:00:00Z"),
                           scan_claude._parse_timestamp("2026-07-01T10:00:05Z")])

    def test_claude_legacy_answer_keeps_timestamp_of_original_entry(self) -> None:
        """stop_reason 为 None 的历史遗留格式答复要等下一条用户消息（或文件末尾）才会被
        flush 进 messages，时间戳必须是这条 assistant 记录自己的时间，不是 flush 发生时的时间。"""
        entries = [
            {"type": "user", "timestamp": "2026-07-01T10:00:00Z", "message": {"content": "问题"}},
            {
                "type": "assistant",
                "timestamp": "2026-07-01T10:00:05Z",
                "message": {"stop_reason": None, "content": [{"type": "text", "text": "遗留格式答复"}]},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_claude.load_conversation(str(path))

        self.assertEqual(messages[-1].role, "assistant")
        self.assertEqual(messages[-1].timestamp, scan_claude._parse_timestamp("2026-07-01T10:00:05Z"))

    def test_load_conversation_does_not_crash_when_assistant_text_part_is_json_null(self) -> None:
        # content 数组里某个 text part 的 text 字段是 JSON null（key 存在但值为 null）时，
        # 曾在过滤条件 part.get("text", "").strip() 上直接 AttributeError 崩掉整段解析。
        entries = [
            {"type": "user", "timestamp": "2026-07-01T10:00:00Z", "message": {"content": "问题"}},
            {
                "type": "assistant",
                "timestamp": "2026-07-01T10:00:05Z",
                "message": {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": None}, {"type": "text", "text": "真实答复"}],
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_claude.load_conversation(str(path))

        self.assertEqual(messages[-1].text, "真实答复")
        self.assertNotIn("None", messages[-1].text)

    def test_codex_conversation_carries_message_timestamp(self) -> None:
        entries = [
            {
                "type": "event_msg",
                "timestamp": "2026-07-01T10:00:00Z",
                "payload": {"type": "user_message", "message": "用户问题"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-01T10:00:03Z",
                "payload": {"type": "agent_message", "phase": "final_answer", "message": "最终答复"},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries), encoding="utf-8")

            messages = scan_codex.load_conversation(str(path))

        self.assertEqual([message.timestamp for message in messages],
                          [scan_codex._parse_timestamp("2026-07-01T10:00:00Z"),
                           scan_codex._parse_timestamp("2026-07-01T10:00:03Z")])


class BackgroundLuminanceTests(unittest.TestCase):
    """OSC 11 背景色应答 -> 浅/深色判定，供 corral 自身界面配色跟随外层终端。"""

    def test_dark_background_detected(self) -> None:
        self.assertFalse(corral._background_is_light(b"\x1b]11;rgb:1e1e/1e1e/2e2e\x07"))

    def test_light_background_detected(self) -> None:
        self.assertTrue(corral._background_is_light(b"\x1b]11;rgb:ffff/ffff/ffff\x07"))

    def test_two_digit_hex_channels_supported(self) -> None:
        self.assertFalse(corral._background_is_light(b"\x1b]11;rgb:1e/1e/2e\x07"))

    def test_picks_osc11_not_osc10_foreground(self) -> None:
        mixed = b"\x1b]10;rgb:cccc/cccc/cccc\x07\x1b]11;rgb:1e1e/1e1e/2e2e\x07"
        self.assertFalse(corral._background_is_light(mixed))

    def test_missing_or_unparsable_report_returns_none(self) -> None:
        self.assertIsNone(corral._background_is_light(None))


class SplitOscReportTests(unittest.TestCase):
    """探测应答拆分：注入托管 pane 前必须把 OSC 11 / OSC 10 拆成两条独立序列。

    tmux 的 refresh-client -r 只解析第一条 OSC 序列，拼接串会让背景色注入整个失效
    （真机：浅色终端下内嵌 agent 全部渲染成深色主题）。
    """

    def test_splits_combined_report(self) -> None:
        combined = b"\x1b]10;rgb:0000/0000/0000\x07\x1b]11;rgb:ffff/ffff/ffff\x07"
        background, foreground = corral.theme._split_osc_report(combined)
        self.assertEqual(background, b"\x1b]11;rgb:ffff/ffff/ffff\x07")
        self.assertEqual(foreground, b"\x1b]10;rgb:0000/0000/0000\x07")

    def test_keeps_last_duplicate_like_luminance_parsing(self) -> None:
        # tmux passthrough 会带来重复应答，真实终端那份在后：与 _background_is_light
        # 取 matches[-1] 的规则必须一致，否则界面与 pane 注入会各用一个颜色
        duplicated = (b"\x1b]11;rgb:0000/0000/0000\x07"
                      b"\x1b]11;rgb:ffff/ffff/ffff\x07")
        background, _ = corral.theme._split_osc_report(duplicated)
        self.assertEqual(background, b"\x1b]11;rgb:ffff/ffff/ffff\x07")

    def test_st_terminated_and_partial_reports(self) -> None:
        background, foreground = corral.theme._split_osc_report(
            b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\")
        self.assertEqual(background, b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\")
        self.assertIsNone(foreground)
        self.assertEqual(corral.theme._split_osc_report(None), (None, None))
        self.assertEqual(corral.theme._split_osc_report(b"rubbish"), (None, None))
        self.assertIsNone(corral._background_is_light(b""))
        self.assertIsNone(corral._background_is_light(b"garbage"))


class TuiLayoutTests(unittest.TestCase):
    def test_session_store_uses_compact_title_before_background_generation(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "short_id": "abc",
            "mtime": 1,
            "size_bytes": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "这是一条很长的兜底标题，启动后不应该直接显示",
        }
        claude_runtime = mock.Mock()
        claude_runtime.id = "claude"
        claude_runtime.display_name = "Claude"
        claude_runtime.scan_sessions.return_value = [session]
        registry = corral.RuntimeRegistry((claude_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        # 启动时无缓存：展示临时兜底标题，并标记为等待后台进程生成（转圈圈）。
        self.assertEqual(store.get_title(session), "这是一条很长的兜底标题")
        self.assertIn(corral.session_key(session), store.generating)

    def test_low_value_session_never_enters_generating_state(self) -> None:
        session = {
            "source": "claude",
            "id": "greeting",
            "short_id": "greeting",
            "mtime": 1,
            "size_bytes": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "在吗",
        }
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = [session]
        registry = corral.RuntimeRegistry((runtime,))

        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        self.assertEqual(store.get_title(session), "在吗")
        self.assertNotIn(corral.session_key(session), store.generating)

    def test_poll_cache_updates_clears_spinner_when_title_arrives(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "short_id": "abc",
            "mtime": 1,
            "size_bytes": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "这是一条很长的兜底标题，启动后不应该直接显示",
        }
        claude_runtime = mock.Mock()
        claude_runtime.id = "claude"
        claude_runtime.display_name = "Claude"
        claude_runtime.scan_sessions.return_value = [session]
        registry = corral.RuntimeRegistry((claude_runtime,))
        key = corral.session_key(session)

        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()
        self.assertIn(key, store.generating)

        # 模拟后台进程把标题写进缓存：轮询应拾取它、刷新展示标题并停掉转圈圈。
        fresh_cache = {key: {"fp": titles._fingerprint(session), "title": "后台生成的标题"}}
        with (
            mock.patch.object(corral.SessionStore, "_cache_file_mtime", return_value=999.0),
            mock.patch.object(corral.titles, "_read_cache", return_value=(fresh_cache, True)),
        ):
            store.poll_cache_updates()

        self.assertEqual(store.get_title(session), "后台生成的标题")
        self.assertNotIn(key, store.generating)
        self.assertTrue(store.dirty.is_set())
        self.assertGreater(store.title_revision, 0)

    def test_failed_title_terminal_state_clears_spinner_and_survives_restart(self) -> None:
        session = {
            "source": "claude",
            "id": "failed",
            "short_id": "failed",
            "mtime": 1,
            "size_bytes": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "排查标题生成卡死",
        }
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = [session]
        registry = corral.RuntimeRegistry((runtime,))
        key = corral.session_key(session)

        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()
        self.assertIn(key, store.generating)

        failed_cache = {
            key: {
                "fp": titles._fingerprint(session),
                "title": "排查标题生成卡死",
                "generation_state": "failed",
                "generation_version": titles.TITLE_CACHE_VERSION,
                "failed_at": time.time(),
            }
        }
        with (
            mock.patch.object(corral.SessionStore, "_cache_file_mtime", return_value=999.0),
            mock.patch.object(corral.titles, "_read_cache", return_value=(failed_cache, True)),
        ):
            store.poll_cache_updates()

        self.assertEqual(store.get_title(session), "排查标题生成卡死")
        self.assertNotIn(key, store.generating)
        self.assertTrue(store.dirty.is_set())

        # 模拟重新启动 corral：冷却期内的失败终态不能再次进入待生成队列。
        with mock.patch.object(corral.titles, "load_cache", return_value=failed_cache):
            restarted = corral.SessionStore(limit=20, registry=registry)
            restarted.load()
        self.assertNotIn(key, restarted.generating)
        self.assertFalse(titles.resolve_initial_title(session, failed_cache)[1])

    def test_failed_title_without_failed_at_can_retry(self) -> None:
        """历史失败条目没有时间戳：视为已到期，允许再入队消化积压。"""
        session = {
            "source": "claude",
            "id": "legacy-fail",
            "mtime": 1,
            "size_kb": 1,
            "fallback_title": "整理失败标题重试",
        }
        cache = {
            "claude:legacy-fail": {
                "fp": "v4:1",
                "title": "整理失败标题重试",
                "generation_state": "failed",
                "generation_version": titles.TITLE_CACHE_VERSION,
            }
        }
        title, needs = titles.resolve_initial_title(session, cache)
        self.assertEqual(title, "整理失败标题重试")
        self.assertTrue(needs)

    def test_failed_title_after_cooldown_can_retry(self) -> None:
        session = {
            "source": "claude",
            "id": "cooled",
            "mtime": 1,
            "size_kb": 1,
            "fallback_title": "冷却后重新生成标题",
        }
        cache = {
            "claude:cooled": {
                "fp": "v4:1",
                "title": "冷却后重新生成标题",
                "generation_state": "failed",
                "generation_version": titles.TITLE_CACHE_VERSION,
                "failed_at": time.time() - titles.FAILED_RETRY_COOLDOWN_SECONDS - 1,
            }
        }
        self.assertTrue(titles.resolve_initial_title(session, cache)[1])

    def test_merge_requests_title_spawn_when_pending(self) -> None:
        """合并扫出待生成会话时会防抖拉起标题后台。"""
        session = {
            "source": "claude",
            "id": "spawn-me",
            "short_id": "spawn",
            "mtime": time.time(),
            "size_bytes": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "新出现的会话需要生成标题",
        }
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = [session]
        registry = corral.RuntimeRegistry((runtime,))
        spawned: list[int] = []

        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store._title_spawn_fn = spawned.append
            store.load()

        self.assertIn(corral.session_key(session), store.generating)
        self.assertEqual(spawned, [20])

        # 防抖：紧接着再请求不应重复拉起。
        store.request_title_generation()
        self.assertEqual(spawned, [20])

    def test_stale_generating_poll_requests_title_spawn(self) -> None:
        session = {
            "source": "claude",
            "id": "stale",
            "short_id": "stale",
            "mtime": 1,
            "size_bytes": 1,
            "size_kb": 1,
            "fallback_title": "缓存未变仍在等待标题",
        }
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = [session]
        registry = corral.RuntimeRegistry((runtime,))
        spawned: list[int] = []

        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store._title_spawn_fn = spawned.append
            store._last_title_spawn_at = 0.0
            store.load()
        spawned.clear()
        store._last_title_spawn_at = 0.0
        store._generating_since = time.time() - corral.store._TITLE_STALE_SPAWN_SECONDS - 1

        with mock.patch.object(
            corral.SessionStore, "_cache_file_mtime", return_value=store._cache_mtime,
        ):
            store.poll_cache_updates()

        self.assertEqual(spawned, [20])

    def test_conversation_is_loaded_lazily_and_cached(self) -> None:
        session = {
            "source": "claude",
            "id": "abc",
            "short_id": "abc",
            "mtime": 1,
            "size_bytes": 1,
            "size_kb": 1,
            "native_title": None,
            "fallback_title": "测试会话",
        }
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = [session]
        runtime.load_conversation.return_value = [corral.ConversationMessage("user", "问题")]
        registry = corral.RuntimeRegistry((runtime,))

        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        runtime.load_conversation.assert_not_called()
        self.assertEqual(store.get_conversation(session), [corral.ConversationMessage("user", "问题")])
        self.assertEqual(store.get_conversation(session), [corral.ConversationMessage("user", "问题")])
        runtime.load_conversation.assert_called_once_with(session)

    def test_conversation_cache_invalidates_when_history_file_mtime_changes(self) -> None:
        """预览实时刷新和"关闭重开还是旧内容"两个诉求的根因是同一处：缓存必须按历史文件
        mtime 失效，而不是按会话键永久缓存到进程退出。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            session = {
                "source": "claude",
                "id": "abc",
                "short_id": "abc",
                "path": str(path),
                "mtime": 1,
                "size_bytes": 1,
                "size_kb": 1,
                "native_title": None,
                "fallback_title": "测试会话",
            }
            runtime = mock.Mock()
            runtime.id = "claude"
            runtime.display_name = "Claude"
            runtime.scan_sessions.return_value = [session]
            runtime.load_conversation.side_effect = [
                [corral.ConversationMessage("assistant", "旧内容")],
                [corral.ConversationMessage("assistant", "新内容")],
            ]
            registry = corral.RuntimeRegistry((runtime,))

            with mock.patch.object(corral.titles, "load_cache", return_value={}):
                store = corral.SessionStore(limit=20, registry=registry)
                store.load()

            self.assertEqual(store.get_conversation(session)[0].text, "旧内容")
            self.assertEqual(store.get_conversation(session)[0].text, "旧内容")
            runtime.load_conversation.assert_called_once_with(session)

            # 模拟历史文件被追加写入：mtime 前进，下一次 get_conversation 必须重读，不能沿用旧缓存。
            future = os.stat(path).st_mtime + 5
            os.utime(path, (future, future))

            self.assertEqual(store.get_conversation(session)[0].text, "新内容")
            self.assertEqual(runtime.load_conversation.call_count, 2)

    def test_conversation_cache_invalidates_when_sqlite_wal_changes(self) -> None:
        """内存预览缓存必须把 path-wal 算进版本，否则 Cursor 最新消息一直吃旧缓存。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.db"
            conn = sqlite3.connect(str(path))
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
            conn.execute("INSERT INTO blobs VALUES ('u1', ?)", (b'{"role":"user","content":"x"}',))
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            session = {
                "source": "claude",
                "id": "abc",
                "short_id": "abc",
                "path": str(path),
                "mtime": 1,
                "size_bytes": 1,
                "size_kb": 1,
                "native_title": None,
                "fallback_title": "测试会话",
            }
            runtime = mock.Mock()
            runtime.id = "claude"
            runtime.display_name = "Claude"
            runtime.scan_sessions.return_value = [session]
            runtime.load_conversation.side_effect = [
                [corral.ConversationMessage("assistant", "旧内容")],
                [corral.ConversationMessage("assistant", "WAL 新内容")],
            ]
            registry = corral.RuntimeRegistry((runtime,))
            with mock.patch.object(corral.titles, "load_cache", return_value={}):
                store = corral.SessionStore(limit=20, registry=registry)
                store.load()

            self.assertEqual(store.get_conversation(session)[0].text, "旧内容")
            # 主库 mtime/size 不动，只往 WAL 写——旧实现会继续命中「旧内容」。
            main_before = (path.stat().st_mtime_ns, path.stat().st_size)
            conn.execute("INSERT INTO blobs VALUES ('u2', ?)", (b'{"role":"user","content":"y"}',))
            conn.commit()
            self.assertEqual((path.stat().st_mtime_ns, path.stat().st_size), main_before)
            self.assertGreater((Path(str(path) + "-wal")).stat().st_size, 0)

            self.assertEqual(store.get_conversation(session)[0].text, "WAL 新内容")
            self.assertEqual(runtime.load_conversation.call_count, 2)
            conn.close()

    def test_shared_db_session_cache_not_invalidated_by_other_sessions(self) -> None:
        """OpenCode 全部会话共用一个 db 文件，别的会话写入会带动文件 mtime；
        该信号不得让本会话预览缓存失效（否则预览频繁只剩表头）。本会话自身
        有更新（mtime 前进）时仍必须重读。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.db"
            path.write_text("", encoding="utf-8")
            session = {
                "source": "opencode", "id": "abc", "short_id": "abc",
                "path": str(path),
                "mtime": 1786121161.995, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "测试会话",
            }
            runtime = mock.Mock()
            runtime.id = "opencode"
            runtime.display_name = "OpenCode"
            runtime.scan_sessions.return_value = [session]
            runtime.load_conversation.side_effect = [
                [corral.ConversationMessage("user", "旧内容")],
                [corral.ConversationMessage("user", "新内容")],
            ]
            registry = corral.RuntimeRegistry((runtime,))
            with mock.patch.object(corral.titles, "load_cache", return_value={}):
                store = corral.SessionStore(limit=20, registry=registry)
                store.load()

            self.assertEqual(store.get_conversation(session)[0].text, "旧内容")
            self.assertEqual(store.get_conversation(session)[0].text, "旧内容")
            runtime.load_conversation.assert_called_once_with(session)

            # 别的会话写入同一 db：文件 mtime 前进，但本会话版本不该因此失效。
            future = os.stat(path).st_mtime + 60
            os.utime(path, (future, future))
            self.assertEqual(store.get_conversation(session)[0].text, "旧内容")
            self.assertEqual(store.peek_conversation(session)[0].text, "旧内容")
            self.assertEqual(runtime.load_conversation.call_count, 1)

            # 本会话自身有更新：mtime 前进，必须重读。
            session["mtime"] = session["mtime"] + 30
            self.assertEqual(store.get_conversation(session)[0].text, "新内容")
            self.assertEqual(runtime.load_conversation.call_count, 2)

            # 会话写入期（活跃中）缓存再次失效：严格模式返回 None，stale_ok
            # 模式保留旧内容，让详情预览不闪「正在读取对话内容…」空态。
            session["mtime"] = session["mtime"] + 60
            self.assertIsNone(store.peek_conversation(session))
            self.assertEqual(store.peek_conversation(session, stale_ok=True)[0].text, "新内容")

    def test_live_session_conversation_is_not_written_to_disk_cache(self) -> None:
        """还在写的会话不落盘：签名每写一次就变，落盘只是白写。

        右上角会话小窗和「在别的窗口跑」的对话都会每隔几秒重读一次运行中会话，真按
        mtime 落盘就变成几秒一次的整份 JSON 写库 + prune（缓存到上限还要删行 +
        checkpoint）。内存缓存照常更新，会话结束后第一次读取再补上落盘。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            session = {
                "source": "claude", "id": "abc", "short_id": "abc", "path": str(path),
                "mtime": 1, "size_bytes": 1, "size_kb": 1,
                "native_title": None, "fallback_title": "测试会话", "live": True,
            }
            runtime = mock.Mock()
            runtime.id = "claude"
            runtime.display_name = "Claude"
            runtime.scan_sessions.return_value = [session]
            runtime.load_conversation.return_value = [
                corral.ConversationMessage("user", "运行中的提问"),
            ]
            registry = corral.RuntimeRegistry((runtime,))
            with mock.patch.object(corral.titles, "load_cache", return_value={}):
                store = corral.SessionStore(limit=20, registry=registry)
                store.load()

            # store 模块里是 `from corral.cache import get_cache`，必须打在 store 上
            with mock.patch("corral.store.get_cache") as get_cache:
                get_cache.return_value.get_conversation.return_value = None
                store.get_conversation(session)
                get_cache.return_value.put_conversation.assert_not_called()

                # 会话结束后同一份历史必须恢复落盘
                store.conversations.clear()
                session["live"] = False
                store.get_conversation(session)
                get_cache.return_value.put_conversation.assert_called_once()

    def test_format_relative_time_thresholds(self) -> None:
        now = 1_000_000.0
        self.assertEqual(corral._format_relative_time(now - 5, now), "just now")
        self.assertEqual(corral._format_relative_time(now + 100, now), "just now")  # 时钟漂移/未来
        self.assertEqual(corral._format_relative_time(now - 179, now), "just now")  # 3 分钟内
        self.assertEqual(corral._format_relative_time(now - 180, now), "3m ago")
        self.assertEqual(corral._format_relative_time(now - 3 * 3600, now), "3h ago")
        # 超过一天退回绝对日期时间（沿用 MM-DD HH:MM）
        old = now - 3 * 86400
        self.assertEqual(
            corral._format_relative_time(old, now),
            corral.datetime.fromtimestamp(old).strftime("%m-%d %H:%M"),
        )

    def test_time_brightness_tier_steps_by_age(self) -> None:
        """侧边栏时间行的亮度分档：半小时 / 三小时 / 一天为界，越旧越暗。"""
        now = 1_000_000.0
        self.assertEqual(corral._time_brightness_tier(now - 5, now), "fresh")
        self.assertEqual(corral._time_brightness_tier(now + 100, now), "fresh")  # 时钟漂移/未来
        self.assertEqual(corral._time_brightness_tier(now - 1799, now), "fresh")
        self.assertEqual(corral._time_brightness_tier(now - 1801, now), "recent")
        self.assertEqual(corral._time_brightness_tier(now - 4 * 3600, now), "today")
        self.assertEqual(corral._time_brightness_tier(now - 3 * 86400, now), "old")
        self.assertEqual(corral._time_brightness_tier(0, now), "old")  # 缺时间戳按最旧

    def test_fit_cell_uses_terminal_display_width(self) -> None:
        self.assertEqual(corral._text_width("标题"), 4)
        self.assertEqual(corral._fit_cell("标题", 6), "标题  ")
        self.assertEqual(corral._fit_cell("标题很长", 5), "标题 ")
        self.assertEqual(corral._text_width(corral._fit_cell("✅完成", 8)), 8)

        mixed = "काe\u0301好🙂"
        self.assertEqual(corral._text_width(mixed), 6)
        self.assertEqual(corral._fit_cell(mixed, 4), "काe\u0301好")

        contextual_emoji = "👨\u200d💻"
        self.assertEqual(corral._text_width(contextual_emoji), 2)
        self.assertEqual(corral._fit_cell(contextual_emoji + "x", 2), contextual_emoji)
        self.assertEqual(corral._wrap_preview_text(contextual_emoji + "x", 2), [contextual_emoji, "x"])

    def test_fit_cell_ellipsis_keeps_display_width(self) -> None:
        fitted = corral._fit_cell("标题很长需要省略", 8, ellipsis=True)
        self.assertTrue(fitted.rstrip().endswith("..."))
        self.assertEqual(corral._text_width(fitted), 8)
        short = corral._fit_cell("短", 8, ellipsis=True)
        self.assertEqual(short.rstrip(), "短")
        self.assertEqual(corral._text_width(short), 8)

    @staticmethod
    def _preview_text(messages, runtime_name, width, **kw):
        """把预览块渲染成纯文本行，供断言使用（与右栏真实渲染同一条路径）。"""
        import io

        from rich.console import Console

        blocks = corral._preview_blocks(messages, runtime_name, width, **kw)
        console = Console(width=width, file=io.StringIO(), color_system=None)
        for block in blocks:
            console.print(block)
        return [line.rstrip() for line in console.file.getvalue().splitlines()]

    def test_preview_renders_messages_as_chronological_chat(self) -> None:
        messages = [
            corral.ConversationMessage("user", "请分析启动速度"),
            corral.ConversationMessage("assistant", "主要耗时来自历史扫描"),
            corral.ConversationMessage("user", "再增加聊天记录预览"),
            corral.ConversationMessage("assistant", "已经完成实现和验证"),
        ]

        lines = self._preview_text(messages, "Codex", 40)

        # 角色抬头独占一行，正文顶格另起一行（不带「角色: 」前缀）
        self.assertEqual(lines[0], "● You")
        self.assertEqual(lines[1], "请分析启动速度")
        self.assertEqual(sum(1 for line in lines if line == "● You"), 2)
        self.assertEqual(sum(1 for line in lines if line == "◆ Codex"), 2)
        self.assertTrue(all(corral._text_width(line) <= 40 for line in lines))

    def test_preview_separates_messages_with_a_rule_not_a_blank_line(self) -> None:
        """消息之间用横线分隔（取后一条消息的角色色），不再是看不出边界的空行。"""
        from rich.text import Text

        messages = [
            corral.ConversationMessage("user", "第一条"),
            corral.ConversationMessage("assistant", "第二条"),
        ]
        blocks = corral._preview_blocks(
            messages, "Codex", 24, assistant_style="bold #D97757",
        )
        rules = [
            b for b in blocks
            if isinstance(b, Text) and set(b.plain) == {"─"}
        ]
        self.assertEqual(len(rules), 1, "两条消息之间恰好一条分隔线，首条之前不画")
        self.assertEqual(corral._text_width(rules[0].plain), 22)
        self.assertEqual(
            str(rules[0].style), "dim #D97757",
            "分隔线取后一条消息的角色色并压暗",
        )

    def test_preview_puts_timestamp_on_the_role_line_only_when_available(self) -> None:
        ts = 1_780_000_000.0
        messages = [
            corral.ConversationMessage("user", "带时间戳的消息", ts),
            corral.ConversationMessage("assistant", "老格式缺时间戳的消息"),
        ]

        lines = self._preview_text(messages, "Claude", 40)

        # 时间戳挂在角色抬头行，不再跟正文抢首行宽度
        self.assertEqual(lines[0], f"● You  · {corral.format_message_time(ts)}")
        self.assertIn("带时间戳的消息", lines)
        self.assertIn("◆ Claude", lines)
        self.assertIn("老格式缺时间戳的消息", lines)

    def test_preview_wraps_body_full_width_without_role_indent(self) -> None:
        """正文顶格折行、吃满整格宽度，续行不再按「角色: 」前缀缩进。"""
        messages = [
            corral.ConversationMessage("user", "这是一条需要折行的很长很长的用户消息内容"),
            corral.ConversationMessage("assistant", "这是一条需要折行的很长很长的助手回复内容"),
        ]
        lines = self._preview_text(messages, "Codex", 24)
        bodies = [
            line for line in lines
            if line and line not in ("● You", "◆ Codex") and set(line) != {"─"}
        ]
        self.assertGreaterEqual(len(bodies), 4, "两条消息都应折成多行")
        self.assertFalse(any(line.startswith(" ") for line in bodies), "正文行不得有缩进")
        self.assertTrue(all(corral._text_width(line) <= 24 for line in lines))

    def test_preview_renders_markdown_structure(self) -> None:
        """正文按 Markdown 排版：标题去掉 #、列表变项目符号、反引号不再露出来。"""
        messages = [
            corral.ConversationMessage(
                "assistant",
                "### 结论\n\n用的是 `getAccountSetting`，**已确认**。\n\n- 第一点\n- 第二点\n",
            ),
        ]
        lines = self._preview_text(messages, "Claude", 60)
        body = "\n".join(lines)
        self.assertIn("结论", body)
        self.assertNotIn("###", body)
        self.assertNotIn("`", body)
        self.assertNotIn("**", body)
        self.assertIn("getAccountSetting", body)
        self.assertTrue(any("•" in line and "第一点" in line for line in lines))

    def test_preview_markdown_never_swallows_angle_bracket_text(self) -> None:
        """回归：CommonMark 默认把 <foo> 当 HTML，Rich 没有 HTML 处理器会整段丢内容。

        助手/用户消息里 `<system-reminder>`、`<Thinking>` 这类文本极常见，真丢过
        整条消息；`_markdown_renderable` 因此关掉了 HTML 解析。
        """
        messages = [
            corral.ConversationMessage(
                "assistant", "<system-reminder>\n这段必须还在\n</system-reminder>",
            ),
            corral.ConversationMessage("user", "报错 <urlopen error> 出现了"),
        ]
        body = "\n".join(self._preview_text(messages, "Claude", 60))
        self.assertIn("这段必须还在", body)
        self.assertIn("system-reminder", body)
        self.assertIn("urlopen error", body)

    def test_preview_ends_with_END_marker_and_blank_guard_lines(self) -> None:
        """预览尾部三行：空行 → 居中结尾标记 → 空行。"""
        messages = [
            corral.ConversationMessage("user", "最后一条"),
        ]
        lines = self._preview_text(messages, "Codex", 40)
        self.assertIn(t("detail.preview_end"), lines[-2])
        self.assertEqual(lines[-1], "", "最后一行应为空行")
        self.assertEqual(lines[-3], "", "结尾标记上一行应为空行")

    def test_all_sessions_keep_stable_order_and_prepend_new_on_refresh(self) -> None:
        """列表展示出来后已有会话位置固定：内容更新（mtime 变新）不再跳到顶上；
        后台重扫只把「最近」新出现的会话按 mtime 倒序插到最前；更旧的 fresh
        （如临时 cwd 复活扫回的几天前会话）追加到末尾。"""
        now = time.time()

        def make_session(session_id: str, mtime: float) -> dict:
            return {
                "source": "claude",
                "id": session_id,
                "short_id": session_id,
                "mtime": mtime,
                "size_bytes": 1,
                "size_kb": 1,
                "native_title": None,
                "fallback_title": f"会话{session_id}",
            }

        first = [
            make_session("a", now - 30),
            make_session("b", now - 60),
            make_session("c", now - 90),
        ]
        # 第二轮：b 有了新消息（mtime 顶到最新），d 是全新且最近的会话
        second = [
            make_session("b", now),
            make_session("d", now - 10),
            make_session("a", now - 30),
            make_session("c", now - 90),
        ]
        claude_runtime = mock.Mock()
        claude_runtime.id = "claude"
        claude_runtime.display_name = "Claude"
        claude_runtime.scan_signature.return_value = None
        claude_runtime.scan_sessions.side_effect = [first, second]
        registry = corral.RuntimeRegistry((claude_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()
            self.assertEqual([s["id"] for s in store.all_sessions()], ["a", "b", "c"])

            changed = store.refresh()

        self.assertTrue(changed)  # 新会话 d 出现，集合确实变了
        # d 插到最前；a/b/c 保持首次展示时的相对位置，b 内容更新但不移动
        self.assertEqual([s["id"] for s in store.all_sessions()], ["d", "a", "b", "c"])
        self.assertEqual(store.all_sessions()[2]["mtime"], now)

    def test_all_sessions_append_resurrected_old_sessions_on_refresh(self) -> None:
        """临时 cwd 复活时扫回的几天前会话不能整批顶到侧边栏最前。"""
        now = time.time()

        def make_session(session_id: str, mtime: float) -> dict:
            return {
                "source": "codex",
                "id": session_id,
                "short_id": session_id,
                "mtime": mtime,
                "size_bytes": 1,
                "size_kb": 1,
                "native_title": None,
                "fallback_title": f"会话{session_id}",
            }

        first = [make_session("keep", now - 60)]
        # old_a / old_b：mtime 远超 _FRESH_PREPEND_MAX_AGE；hot：刚活跃的真新会话
        second = [
            make_session("hot", now - 10),
            make_session("old_a", now - 5 * 86400),
            make_session("old_b", now - 4 * 86400),
            make_session("keep", now - 60),
        ]
        runtime = mock.Mock()
        runtime.id = "codex"
        runtime.display_name = "Codex"
        runtime.scan_signature.return_value = None
        runtime.scan_sessions.side_effect = [first, second]
        registry = corral.RuntimeRegistry((runtime,))
        with (
            mock.patch.object(corral.titles, "load_cache", return_value={}),
            mock.patch.object(corral.liveness, "list_managed_hosts", return_value=[]),
        ):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()
            self.assertEqual([s["id"] for s in store.all_sessions()], ["keep"])
            changed = store.refresh()

        self.assertTrue(changed)
        self.assertEqual(
            [s["id"] for s in store.all_sessions()],
            ["hot", "keep", "old_b", "old_a"],
        )

    def test_mark_hosted_clear_forces_ended_until_scan_confirms_dead(self) -> None:
        """按 q 结束托管后不能闪「运行中」：立刻清 live，重扫仍报 live 时继续压住。"""
        session = {
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": 1.0,
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "t",
            "cwd": "/tmp", "live": True, "pid": 99, "keepalive_name": "corral-claude-s0",
        }
        claude_runtime = mock.Mock()
        claude_runtime.id = "claude"
        claude_runtime.display_name = "Claude"
        claude_runtime.scan_signature.return_value = None
        claude_runtime.scan_sessions.return_value = [dict(session)]
        registry = corral.RuntimeRegistry((claude_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        store.mark_hosted("claude:s0", None)
        current = store.find_session("claude:s0")
        self.assertFalse(current.get("live"))
        self.assertIsNone(current.get("pid"))
        self.assertNotIn("keepalive_name", current)
        self.assertIn("claude:s0", store._force_ended)

        still_live = {
            "source": "claude", "id": "s0", "short_id": "s0", "mtime": 1.0,
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "t",
            "cwd": "/tmp", "live": True, "pid": 99,
        }
        claude_runtime.scan_sessions.return_value = [still_live]
        with mock.patch.object(corral.liveness, "annotate"):
            store.refresh()
        current = store.find_session("claude:s0")
        self.assertFalse(current.get("live"))
        self.assertIn("claude:s0", store._force_ended)

        dead = dict(still_live, live=False, pid=None)
        claude_runtime.scan_sessions.return_value = [dead]
        with mock.patch.object(corral.liveness, "annotate"):
            store.refresh()
        self.assertNotIn("claude:s0", store._force_ended)
        self.assertFalse(store.find_session("claude:s0").get("live"))

    def test_register_hosted_session_survives_refresh_until_real_session_claims(self) -> None:
        """跨运行时接力占位卡：磁盘尚无历史时重扫不能抹掉；真实会话挂上同名托管后退役。"""
        cursor_runtime = mock.Mock()
        cursor_runtime.id = "cursor"
        cursor_runtime.display_name = "Cursor"
        cursor_runtime.scan_signature.return_value = None
        cursor_runtime.scan_sessions.return_value = []
        registry = corral.RuntimeRegistry((cursor_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        provisional = store.register_hosted_session(
            runtime_id="cursor",
            keepalive_name="corral-cursor-abcd1234",
            title="接力自 Claude",
            cwd="/tmp/proj",
        )
        key = corral.session_key(provisional)
        self.assertEqual(provisional.get("keepalive_name"), "corral-cursor-abcd1234")
        self.assertTrue(provisional.get("provisional"))
        self.assertEqual(store.find_session(key)["fallback_title"], "接力自 Claude")

        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "is_alive", return_value=True
        ):
            store.refresh()
        kept = store.find_session(key)
        self.assertIsNotNone(kept)
        self.assertEqual(kept.get("keepalive_name"), "corral-cursor-abcd1234")

        real = {
            "source": "cursor", "id": "real-uuid", "short_id": "realuuid", "mtime": 9.0,
            "size_bytes": 1, "size_kb": 1, "native_title": "真会话", "fallback_title": "真会话",
            "cwd": "/tmp/proj", "live": True, "pid": 42,
            "keepalive_name": "corral-cursor-abcd1234",
        }
        cursor_runtime.scan_sessions.return_value = [real]
        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "is_alive", return_value=True
        ):
            store.refresh()
        self.assertIsNone(store.find_session(key))
        self.assertIsNotNone(store.find_session("cursor:real-uuid"))
        self.assertNotIn(key, store._provisional)

    def test_foreign_tmux_host_is_adopted_as_interactive_provisional(self) -> None:
        """Other-process hosted panes (phone remote) must appear interactive before history.

        Desktop TUI does not share the remote daemon's SessionStore. Without
        adoption, the sidebar stays empty until Cursor/Codex write history, then
        briefly shows a static preview until annotate attaches keepalive.
        """
        cursor_runtime = mock.Mock()
        cursor_runtime.id = "cursor"
        cursor_runtime.display_name = "Cursor"
        cursor_runtime.scan_signature.return_value = None
        cursor_runtime.scan_sessions.return_value = []
        registry = corral.RuntimeRegistry((cursor_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}), mock.patch.object(
            corral.liveness, "list_managed_hosts", return_value=[]
        ), mock.patch.object(corral.liveness, "annotate"):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        foreign = {
            "name": "corral-cursor-deadbeef",
            "runtime_id": "cursor",
            "ident": "deadbeef",
            "cwd": "/tmp/phone-proj",
            "pane_pid": 4242,
        }
        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "list_managed_hosts", return_value=[foreign]
        ), mock.patch.object(corral.liveness, "is_alive", return_value=True):
            changed = store.refresh()
        self.assertTrue(changed)
        card = store.find_session("cursor:deadbeef")
        self.assertIsNotNone(card)
        self.assertTrue(card.get("provisional"))
        self.assertTrue(card.get("live"))
        self.assertEqual(card.get("keepalive_name"), "corral-cursor-deadbeef")
        self.assertEqual(card.get("cwd"), "/tmp/phone-proj")

    def test_foreign_hosted_pane_in_ephemeral_workspace_is_not_adopted(self) -> None:
        """Experiment panes hosted by another process must not become sidebar cards."""
        cursor_runtime = mock.Mock()
        cursor_runtime.id = "claude"
        cursor_runtime.display_name = "Claude"
        cursor_runtime.scan_signature.return_value = None
        cursor_runtime.scan_sessions.return_value = []
        registry = corral.RuntimeRegistry((cursor_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}), mock.patch.object(
            corral.liveness, "list_managed_hosts", return_value=[]
        ), mock.patch.object(corral.liveness, "annotate"):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".sesskit-ignore").touch()
            foreign = {
                "name": "corral-claude-0badf00d",
                "runtime_id": "claude",
                "ident": "0badf00d",
                "cwd": str(Path(td) / "claude-1790653232"),
                "pane_pid": 4242,
            }
            with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
                corral.liveness, "list_managed_hosts", return_value=[foreign]
            ), mock.patch.object(corral.liveness, "is_alive", return_value=True):
                store.refresh()
        self.assertIsNone(store.find_session("claude:0badf00d"))

    def test_foreign_adopted_provisional_retires_onto_real_history(self) -> None:
        """Adopted remote provisional must retire when formal history appears."""
        cursor_runtime = mock.Mock()
        cursor_runtime.id = "cursor"
        cursor_runtime.display_name = "Cursor"
        cursor_runtime.scan_signature.return_value = None
        cursor_runtime.scan_sessions.return_value = []
        registry = corral.RuntimeRegistry((cursor_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}), mock.patch.object(
            corral.liveness, "list_managed_hosts", return_value=[]
        ), mock.patch.object(corral.liveness, "annotate"):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        foreign = {
            "name": "corral-cursor-cafebabe",
            "runtime_id": "cursor",
            "ident": "cafebabe",
            "cwd": "/tmp/phone-proj",
            "pane_pid": 99,
        }
        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "list_managed_hosts", return_value=[foreign]
        ), mock.patch.object(corral.liveness, "is_alive", return_value=True):
            store.refresh()
        self.assertIsNotNone(store.find_session("cursor:cafebabe"))

        real = {
            "source": "cursor", "id": "bbbbbbbb-1111-2222-3333-444444444444",
            "short_id": "bbbbbbbb1111", "mtime": time.time(),
            "size_bytes": 1, "size_kb": 1, "native_title": "手机开的",
            "fallback_title": "手机开的", "cwd": "/tmp/phone-proj",
            "live": True, "pid": 99,
        }
        cursor_runtime.scan_sessions.return_value = [real]
        # annotate intentionally does not attach keepalive (uuid ≠ ident).
        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "list_managed_hosts", return_value=[foreign]
        ), mock.patch.object(corral.liveness, "is_alive", return_value=True):
            store.refresh()
        self.assertIsNone(store.find_session("cursor:cafebabe"))
        real_card = store.find_session("cursor:bbbbbbbb-1111-2222-3333-444444444444")
        self.assertIsNotNone(real_card)
        self.assertEqual(real_card.get("keepalive_name"), "corral-cursor-cafebabe")
        self.assertTrue(real_card.get("live"))

    def test_cursor_fresh_listed_session_retires_provisional_without_duplicate(self) -> None:
        """正式 Cursor 历史先于占位卡进列表时，仍须退役占位，避免侧栏双卡。

        真机/竞态：扫描先看到新 UUID（尚无 keepalive），随后分屏托管才登记占位卡；
        旧逻辑只认「本轮 newcomers」，正式卡已在 ``_order`` 里就认领失败，占位与
        正式卡并存——分屏组里一份、组外再挂一份，下一轮 annotate 才并掉。
        """
        cursor_runtime = mock.Mock()
        cursor_runtime.id = "cursor"
        cursor_runtime.display_name = "Cursor"
        cursor_runtime.scan_signature.return_value = None

        def _sessions(*, include_new: bool):
            rows = [
                {
                    "source": "cursor", "id": "old-uuid", "short_id": "olduuid",
                    "mtime": 1.0, "size_bytes": 1, "size_kb": 1,
                    "native_title": "旧", "fallback_title": "旧",
                    "cwd": "/tmp/proj", "live": False,
                },
            ]
            if include_new:
                rows.append(
                    {
                        "source": "cursor", "id": "early-uuid", "short_id": "earlyuuid",
                        "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
                        "native_title": "新", "fallback_title": "新",
                        "cwd": "/tmp/proj", "live": False, "pid": None,
                    }
                )
            return rows

        cursor_runtime.scan_sessions.side_effect = (
            lambda *a, **k: _sessions(include_new=True)
        )
        registry = corral.RuntimeRegistry((cursor_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "is_alive", return_value=True
        ):
            store.refresh()

        provisional = store.register_hosted_session(
            runtime_id="cursor",
            keepalive_name="corral-cursor-efgh5678",
            title="分屏新建",
            cwd="/tmp/proj",
            ident="efgh5678",
        )
        old_key = corral.session_key(provisional)
        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "is_alive", return_value=True
        ):
            store.refresh()

        self.assertIsNone(store.find_session(old_key))
        claimed = store.find_session("cursor:early-uuid")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.get("keepalive_name"), "corral-cursor-efgh5678")
        self.assertNotIn(old_key, store._provisional)
        self.assertEqual(
            store.session_key_migrations().get(old_key),
            "cursor:early-uuid",
        )
        # 同目录更早的旧卡不得被误认领。
        self.assertIsNone(store.find_session("cursor:old-uuid").get("keepalive_name"))

    def test_pi_unique_newcomer_retires_provisional_instead_of_duplicating(self) -> None:
        """Pi 落盘 uuid 与占位 ident 不同、又没 pid 可 annotate 时，同 cwd 唯一新卡应退役占位。

        真机：分屏里新建 Pi，过一会儿组外冒出真实卡，组里还留着占位卡；或占位
        被清掉后真实卡跑到组外。同目录只有一张活着的占位卡时可以安全认领。
        """
        pi_runtime = mock.Mock()
        pi_runtime.id = "pi"
        pi_runtime.display_name = "Pi"
        pi_runtime.scan_signature.return_value = None
        pi_runtime.scan_sessions.return_value = []
        registry = corral.RuntimeRegistry((pi_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        provisional = store.register_hosted_session(
            runtime_id="pi",
            keepalive_name="corral-pi-abcd1234",
            title="新Pi会话",
            cwd="/tmp/proj",
            ident="abcd1234",
        )
        old_key = corral.session_key(provisional)
        real = {
            "source": "pi", "id": "019ffa0b-6679-7e5e-bfd9-1615e07cf643",
            "short_id": "019ffa0b6679", "mtime": 9.0,
            "size_bytes": 1, "size_kb": 1, "native_title": "真会话",
            "fallback_title": "真会话", "cwd": "/tmp/proj", "live": False, "pid": None,
        }
        pi_runtime.scan_sessions.return_value = [real]
        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "is_alive", return_value=True
        ):
            store.refresh()
        self.assertIsNone(store.find_session(old_key))
        claimed = store.find_session("pi:019ffa0b-6679-7e5e-bfd9-1615e07cf643")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.get("keepalive_name"), "corral-pi-abcd1234")
        self.assertNotIn(old_key, store._provisional)
        self.assertEqual(
            store.hosted.get("pi:019ffa0b-6679-7e5e-bfd9-1615e07cf643"),
            "corral-pi-abcd1234",
        )

    def test_pi_two_newcomers_same_cwd_do_not_cross_claim(self) -> None:
        """同目录两个新建 Pi 同时落盘时不能靠 cwd 猜，否则分屏两格会串台。"""
        pi_runtime = mock.Mock()
        pi_runtime.id = "pi"
        pi_runtime.display_name = "Pi"
        pi_runtime.scan_signature.return_value = None
        pi_runtime.scan_sessions.return_value = []
        registry = corral.RuntimeRegistry((pi_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        first = store.register_hosted_session(
            runtime_id="pi",
            keepalive_name="corral-pi-aaa11111",
            title="Pi A",
            cwd="/tmp/proj",
            ident="aaa11111",
        )
        second = store.register_hosted_session(
            runtime_id="pi",
            keepalive_name="corral-pi-bbb22222",
            title="Pi B",
            cwd="/tmp/proj",
            ident="bbb22222",
        )
        real_a = {
            "source": "pi", "id": "uuid-a", "short_id": "uuid-a", "mtime": 9.0,
            "size_bytes": 1, "size_kb": 1, "native_title": "A", "fallback_title": "A",
            "cwd": "/tmp/proj", "live": False,
        }
        real_b = {
            "source": "pi", "id": "uuid-b", "short_id": "uuid-b", "mtime": 8.0,
            "size_bytes": 1, "size_kb": 1, "native_title": "B", "fallback_title": "B",
            "cwd": "/tmp/proj", "live": False,
        }
        pi_runtime.scan_sessions.return_value = [real_a, real_b]
        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "is_alive", return_value=True
        ):
            store.refresh()
        self.assertIsNotNone(store.find_session(corral.session_key(first)))
        self.assertIsNotNone(store.find_session(corral.session_key(second)))
        self.assertIsNotNone(store.find_session("pi:uuid-a"))
        self.assertIsNotNone(store.find_session("pi:uuid-b"))
        self.assertIsNone(store.find_session("pi:uuid-a").get("keepalive_name"))
        self.assertIsNone(store.find_session("pi:uuid-b").get("keepalive_name"))

    def test_pi_session_dir_newcomers_claim_without_crossing(self) -> None:
        """同 cwd 两个占位卡按 corral-<ident> 目录各领各的真实卡，不再因两条新卡放弃。"""
        from corral.runtime.host_extension import hosted_session_dir

        pi_runtime = mock.Mock()
        pi_runtime.id = "pi"
        pi_runtime.display_name = "Pi"
        pi_runtime.scan_signature.return_value = None
        pi_runtime.scan_sessions.return_value = []
        registry = corral.RuntimeRegistry((pi_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        first = store.register_hosted_session(
            runtime_id="pi",
            keepalive_name="corral-pi-aaa11111",
            title="Pi A",
            cwd="/tmp/proj",
            ident="aaa11111",
        )
        second = store.register_hosted_session(
            runtime_id="pi",
            keepalive_name="corral-pi-bbb22222",
            title="Pi B",
            cwd="/tmp/proj",
            ident="bbb22222",
        )
        path_a = os.path.join(
            hosted_session_dir("/tmp/proj", "aaa11111"),
            "2026-08-19T00-00-00-000Z_uuid-a.jsonl",
        )
        path_b = os.path.join(
            hosted_session_dir("/tmp/proj", "bbb22222"),
            "2026-08-19T00-00-00-000Z_uuid-b.jsonl",
        )
        real_a = {
            "source": "pi", "id": "uuid-a", "short_id": "uuid-a", "mtime": 9.0,
            "size_bytes": 1, "size_kb": 1, "native_title": "A", "fallback_title": "A",
            "cwd": "/tmp/proj", "live": False, "path": path_a,
        }
        real_b = {
            "source": "pi", "id": "uuid-b", "short_id": "uuid-b", "mtime": 8.0,
            "size_bytes": 1, "size_kb": 1, "native_title": "B", "fallback_title": "B",
            "cwd": "/tmp/proj", "live": False, "path": path_b,
        }
        pi_runtime.scan_sessions.return_value = [real_a, real_b]
        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "is_alive", return_value=True
        ):
            store.refresh()
        self.assertIsNone(store.find_session(corral.session_key(first)))
        self.assertIsNone(store.find_session(corral.session_key(second)))
        claimed_a = store.find_session("pi:uuid-a")
        claimed_b = store.find_session("pi:uuid-b")
        self.assertEqual(claimed_a.get("keepalive_name"), "corral-pi-aaa11111")
        self.assertEqual(claimed_b.get("keepalive_name"), "corral-pi-bbb22222")

    def test_pi_same_ident_after_session_id_does_not_duplicate(self) -> None:
        """`--session-id` 让落盘 id 与占位 ident 相同时，只留一张卡。"""
        pi_runtime = mock.Mock()
        pi_runtime.id = "pi"
        pi_runtime.display_name = "Pi"
        pi_runtime.scan_signature.return_value = None
        pi_runtime.scan_sessions.return_value = []
        registry = corral.RuntimeRegistry((pi_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()

        provisional = store.register_hosted_session(
            runtime_id="pi",
            keepalive_name="corral-pi-abcd1234",
            title="新Pi会话",
            cwd="/tmp/proj",
            ident="abcd1234",
        )
        key = corral.session_key(provisional)
        real = {
            "source": "pi", "id": "abcd1234", "short_id": "abcd1234", "mtime": 9.0,
            "size_bytes": 1, "size_kb": 1, "native_title": "真会话",
            "fallback_title": "真会话", "cwd": "/tmp/proj", "live": False,
        }
        pi_runtime.scan_sessions.return_value = [real]
        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "is_alive", return_value=True
        ):
            store.refresh()
        found = store.find_session(key)
        self.assertIsNotNone(found)
        self.assertFalse(found.get("provisional"))
        self.assertEqual(found.get("keepalive_name"), "corral-pi-abcd1234")
        self.assertNotIn(key, store._provisional)
        self.assertEqual(len([s for s in store.all_sessions() if s.get("source") == "pi"]), 1)

    def test_refresh_rebinds_hosted_name_without_is_alive(self) -> None:
        """本进程仍登记 hosted 时，annotate 未命中也必须回填 keepalive。

        切走再切回时 has-session 超时不能把还在跑的会话降成已结束预览。
        """
        session = {
            "source": "claude", "id": "s1", "short_id": "s1", "mtime": 1.0,
            "size_bytes": 1, "size_kb": 1, "native_title": None, "fallback_title": "t",
            "cwd": "/tmp", "live": True, "pid": 42,
        }
        claude_runtime = mock.Mock()
        claude_runtime.id = "claude"
        claude_runtime.display_name = "Claude"
        claude_runtime.scan_signature.return_value = None
        claude_runtime.scan_sessions.return_value = [dict(session)]
        registry = corral.RuntimeRegistry((claude_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()
        store.hosted["claude:s1"] = "corral-claude-s1"
        store.find_session("claude:s1").pop("keepalive_name", None)
        claude_runtime.scan_sessions.return_value = [dict(session)]
        with (
            mock.patch.object(corral.liveness, "annotate"),
            mock.patch("corral.store.liveness.is_alive", return_value=False) as alive,
        ):
            store.refresh()
        alive.assert_not_called()
        self.assertEqual(store.find_session("claude:s1").get("keepalive_name"), "corral-claude-s1")
        self.assertEqual(store.hosted.get("claude:s1"), "corral-claude-s1")

    def test_refresh_keeps_provisional_when_hosted_even_if_is_alive_false(self) -> None:
        """刚开的占位卡：探活假阴性不得在切走期间把卡退役成已结束。"""
        cursor_runtime = mock.Mock()
        cursor_runtime.id = "cursor"
        cursor_runtime.display_name = "Cursor"
        cursor_runtime.scan_signature.return_value = None
        cursor_runtime.scan_sessions.return_value = []
        registry = corral.RuntimeRegistry((cursor_runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()
        provisional = store.register_hosted_session(
            runtime_id="cursor",
            keepalive_name="corral-cursor-abcd1234",
            title="新 Cursor 会话",
            cwd="/tmp/proj",
        )
        key = corral.session_key(provisional)
        with mock.patch.object(corral.liveness, "annotate"), mock.patch.object(
            corral.liveness, "is_alive", return_value=False,
        ):
            store.refresh()
        kept = store.find_session(key)
        self.assertIsNotNone(kept)
        self.assertTrue(kept.get("provisional"))
        self.assertEqual(kept.get("keepalive_name"), "corral-cursor-abcd1234")
        self.assertEqual(store.hosted.get(key), "corral-cursor-abcd1234")


class NavStub:
    """`_new_session_cwd` 只需要 `project_query` 属性；界面层真实用的是
    `ui.nav.NavState`，测试这里用一个最小 stub 避免依赖 ui 包。"""

    def __init__(self, project_query: str = "") -> None:
        self.project_query = project_query


class ProjectSidebarTests(unittest.TestCase):
    def test_normalize_cwd_trailing_slash_and_empty(self) -> None:
        self.assertEqual(corral._normalize_cwd("/a/b/"), "/a/b")
        self.assertEqual(corral._normalize_cwd(""), "")
        self.assertEqual(corral._normalize_cwd(None), "")
        self.assertEqual(corral._normalize_cwd("/"), "")

    def test_project_groups_sorted_by_count_then_latest_mtime(self) -> None:
        sessions_by_source = {
            "claude": [
                {"cwd": "/a/x", "mtime": 10},
                {"cwd": "/a/x", "mtime": 5},
                {"cwd": "/a/y", "mtime": 20},
                {"cwd": "/a/z", "mtime": 1},
            ],
        }
        groups = corral._project_groups(sessions_by_source)
        keys = [g["cwd_key"] for g in groups]

        # 会话数最多的项目排第一；会话数相同的项目按最近会话时间倒序。
        self.assertEqual(keys, ["/a/x", "/a/y", "/a/z"])
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[0]["latest_mtime"], 10)

    def test_project_groups_merges_all_sources(self) -> None:
        sessions_by_source = {
            "claude": [{"cwd": "/a/x", "mtime": 1}],
            "codex": [{"cwd": "/a/x", "mtime": 2}],
        }
        groups = corral._project_groups(sessions_by_source)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[0]["latest_mtime"], 2)

    def test_project_groups_labels_empty_and_root_cwd_as_unknown(self) -> None:
        sessions_by_source = {"claude": [{"cwd": "", "mtime": 1}, {"cwd": "/", "mtime": 2}]}
        groups = corral._project_groups(sessions_by_source)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["cwd_key"], "")
        self.assertEqual(groups[0]["label"], corral.UNKNOWN_PROJECT_LABEL)
        self.assertEqual(groups[0]["count"], 2)

    def test_session_store_projects_resolves_project_groups(self) -> None:
        """SessionStore.projects 合并会话 cwd（并可扫 git 根），不能 NameError 闪退。"""
        from corral import projects as projects_mod
        from corral.store import SessionStore

        runtime = mock.Mock(id="claude", display_name="Claude")
        runtime.scan_signature.return_value = None
        runtime.scan_sessions.return_value = []
        registry = corral.RuntimeRegistry((runtime,))
        with mock.patch.object(corral.titles, "load_cache", return_value={}), mock.patch.object(
            corral.liveness, "annotate"
        ):
            store = SessionStore(limit=5, registry=registry)
        store.sessions = {
            "claude": [{"cwd": "/proj/a", "mtime": 3}, {"cwd": "/proj/b", "mtime": 1}],
        }
        # 跳过真实家目录 git 扫描，只断言会话源聚合。
        with mock.patch.dict(os.environ, {"CORRAL_PROJECT_ROOTS": ""}):
            projects_mod.clear_filesystem_cache()
            groups = store.projects()
        self.assertEqual([g["cwd_key"] for g in groups], ["/proj/a", "/proj/b"])
        self.assertEqual(groups[0]["count"], 1)

    def test_disambiguate_adds_parent_only_on_conflict(self) -> None:
        labels = corral._disambiguate_labels(["/a/x/cli", "/b/y/app"])
        self.assertEqual(labels["/a/x/cli"], "cli")
        self.assertEqual(labels["/b/y/app"], "app")

    def test_disambiguate_climbs_multiple_levels(self) -> None:
        # 两个同名 cli 目录连上一级目录名也相同，需要继续向上爬升才能唯一区分。
        labels = corral._disambiguate_labels(["/a/p/cli", "/b/p/cli"])
        self.assertEqual(labels["/a/p/cli"], "a/p/cli")
        self.assertEqual(labels["/b/p/cli"], "b/p/cli")
        self.assertNotEqual(labels["/a/p/cli"], labels["/b/p/cli"])

    def test_filter_sessions_uses_exact_cwd_not_basename(self) -> None:
        sessions = [
            {"cwd": "/a/x/cli", "mtime": 1},
            {"cwd": "/b/y/cli", "mtime": 2},
        ]
        filtered = corral._filter_sessions(sessions, "/a/x/cli")

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["cwd"], "/a/x/cli")

    def test_filter_sessions_none_key_returns_unfiltered(self) -> None:
        sessions = [{"cwd": "/a/x", "mtime": 1}]
        self.assertEqual(corral._filter_sessions(sessions, None), sessions)

    def test_fuzzy_match_case_insensitive_substring_and_subsequence(self) -> None:
        self.assertTrue(corral._fuzzy_match("proxy", "ProxyAgent"))
        self.assertTrue(corral._fuzzy_match("PROXY", "ProxyAgent"))
        self.assertTrue(corral._fuzzy_match("pxy", "ProxyAgent"))
        self.assertTrue(corral._fuzzy_match("界面", "侧边栏界面打磨"))
        self.assertFalse(corral._fuzzy_match("zzz", "ProxyAgent"))
        self.assertTrue(corral._fuzzy_match("", "anything"))

    def test_filter_sessions_by_query_matches_project_and_title(self) -> None:
        sessions = [
            {"source": "claude", "id": "1", "cwd": "/x/ProxyAgent", "fallback_title": "节点"},
            {"source": "claude", "id": "2", "cwd": "/x/corral", "fallback_title": "界面打磨"},
            {"source": "claude", "id": "3", "cwd": "/x/other", "fallback_title": "无关"},
        ]
        by_project = corral._filter_sessions_by_query(sessions, "pRoXy")
        self.assertEqual([s["id"] for s in by_project], ["1"])
        by_title = corral._filter_sessions_by_query(sessions, "界面")
        self.assertEqual([s["id"] for s in by_title], ["2"])
        self.assertEqual(corral._filter_sessions_by_query(sessions, ""), sessions)

    def test_new_session_cwd_uses_unique_project_from_query(self) -> None:
        store = mock.Mock()
        store.all_sessions.return_value = [
            {"source": "claude", "id": "1", "cwd": "/proj/a", "fallback_title": "a1"},
            {"source": "claude", "id": "2", "cwd": "/proj/a", "fallback_title": "a2"},
            {"source": "claude", "id": "3", "cwd": "/proj/b", "fallback_title": "b1"},
        ]
        store.display_titles = {}
        nav = NavStub(project_query="proj/a")
        session = {"cwd": "/proj/other"}

        self.assertEqual(corral._new_session_cwd(store, nav, session), "/proj/a")

    def test_new_session_cwd_falls_back_to_session_cwd_when_query_ambiguous(self) -> None:
        store = mock.Mock()
        store.all_sessions.return_value = [
            {"source": "claude", "id": "1", "cwd": "/proj/a", "fallback_title": "x"},
            {"source": "claude", "id": "2", "cwd": "/proj/b", "fallback_title": "x"},
        ]
        store.display_titles = {}
        nav = NavStub(project_query="proj")
        session = {"cwd": "/proj/session"}

        self.assertEqual(corral._new_session_cwd(store, nav, session), "/proj/session")

    def test_new_session_cwd_falls_back_to_session_cwd_when_empty_query(self) -> None:
        store = mock.Mock()
        nav = NavStub()
        session = {"cwd": "/proj/session"}

        self.assertEqual(corral._new_session_cwd(store, nav, session), "/proj/session")

    def test_new_session_cwd_none_without_project_or_session(self) -> None:
        store = mock.Mock()
        nav = NavStub()

        self.assertIsNone(corral._new_session_cwd(store, nav, None))


class TmuxRequirementTests(unittest.TestCase):
    def test_supported_version_passes(self) -> None:
        with (
            mock.patch.object(corral.shutil, "which", return_value="/usr/bin/tmux"),
            mock.patch.object(corral.embed, "_tmux_version", return_value=(3, 2)),
        ):
            corral._require_tmux()

    def test_unparseable_version_does_not_block(self) -> None:
        with (
            mock.patch.object(corral.shutil, "which", return_value="/usr/bin/tmux"),
            mock.patch.object(corral.embed, "_tmux_version", return_value=None),
        ):
            corral._require_tmux()

    def test_old_version_reports_required_and_current_versions(self) -> None:
        with (
            mock.patch.object(corral.shutil, "which", return_value="/usr/bin/tmux"),
            mock.patch.object(corral.embed, "_tmux_version", return_value=(3, 1)),
            mock.patch("builtins.print") as print_mock,
            self.assertRaises(SystemExit) as ctx,
        ):
            corral._require_tmux()

        self.assertEqual(ctx.exception.code, 1)
        output = " ".join(str(call) for call in print_mock.call_args_list)
        self.assertIn("3.2", output)
        self.assertIn("3.1", output)


class DirectLaunchTests(unittest.TestCase):
    """`corral claude [参数…]` / `corral codex [参数…]` 直启透传子命令的分发逻辑。

    裸位置参数已改为项目名匹配（见 test_projects.DirectLaunchProjectTests）；
    透传用例的首参必须以 `-` 开头。
    """

    def _registry_returning(self, plan: LaunchPlan) -> mock.Mock:
        registry = mock.Mock()
        # 直启入口会先把第一个词按别名解析成运行时 id；假 registry 原样返回即可
        registry.resolve_id.side_effect = lambda token: token
        registry.build_passthrough_plan.return_value = plan
        return registry

    def test_passes_through_args_and_wraps_with_keepalive_by_default(self) -> None:
        plan = LaunchPlan(("claude", "--dangerously-skip-permissions", "--print", "hi"), None)
        wrapped = LaunchPlan(("tmux", "-L", "corral-keepalive", "new-session", "-A", "-s", "sc-claude-xxxx"), None)
        registry = self._registry_returning(plan)

        with (
            mock.patch.object(corral, "keepalive") as keepalive_mock,
            mock.patch.object(corral, "execute_launch") as execute_launch,
            mock.patch.object(corral, "_require_tmux"),
            mock.patch.object(corral.sys.stdin, "isatty", return_value=False),
            mock.patch.object(corral.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = True
            keepalive_mock.new_session_ident.return_value = "xxxx"
            keepalive_mock.wrap_plan.return_value = wrapped

            corral._dispatch_direct_launch(["claude", "--print", "hi"], registry)

        registry.build_passthrough_plan.assert_called_once_with("claude", ["--print", "hi"])
        keepalive_mock.enabled.assert_called_once_with(False)
        keepalive_mock.wrap_plan.assert_called_once_with(plan, "claude", "xxxx")
        execute_launch.assert_called_once_with(wrapped)

    def test_no_keepalive_prefix_strips_flag_and_skips_wrap(self) -> None:
        plan = LaunchPlan(("codex", "--dangerously-bypass-approvals-and-sandbox", "--resume", "x"), None)
        registry = self._registry_returning(plan)

        with (
            mock.patch.object(corral, "keepalive") as keepalive_mock,
            mock.patch.object(corral, "execute_launch") as execute_launch,
            mock.patch.object(corral, "_require_tmux"),
            mock.patch.object(corral.sys.stdin, "isatty", return_value=False),
            mock.patch.object(corral.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = False

            corral._dispatch_direct_launch(["--no-keepalive", "codex", "--resume", "x"], registry)

        registry.build_passthrough_plan.assert_called_once_with("codex", ["--resume", "x"])
        keepalive_mock.enabled.assert_called_once_with(True)
        keepalive_mock.wrap_plan.assert_not_called()
        execute_launch.assert_called_once_with(plan)

    def test_launch_error_prints_message_and_exits_nonzero(self) -> None:
        plan = LaunchPlan(("claude",), None)
        registry = self._registry_returning(plan)

        with (
            mock.patch.object(corral, "keepalive") as keepalive_mock,
            mock.patch.object(corral, "execute_launch", side_effect=corral.LaunchError("未找到 claude 命令")),
            mock.patch.object(corral, "_require_tmux"),
            mock.patch.object(corral.sys.stdin, "isatty", return_value=False),
            mock.patch.object(corral.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = False

            with self.assertRaises(SystemExit) as ctx:
                corral._dispatch_direct_launch(["claude"], registry)

        self.assertEqual(ctx.exception.code, 1)

    def test_tty_with_embed_enters_tui_with_direct_launch_plan(self) -> None:
        """真实终端 + 内嵌可用：直启进入 TUI 侧边栏模式，启动计划交给 Textual
        主屏托管，不再走 execvp 全屏接管。"""
        plan = LaunchPlan(("claude", "--dangerously-skip-permissions"), None)
        registry = self._registry_returning(plan)

        with (
            mock.patch.object(corral.sys, "stdin") as stdin_mock,
            mock.patch.object(corral.sys, "stdout") as stdout_mock,
            mock.patch.object(corral, "keepalive") as keepalive_mock,
            mock.patch.object(corral.embed, "available", return_value=True),
            mock.patch.object(corral, "SessionStore") as store_cls,
            mock.patch.object(corral, "_spawn_title_daemon"),
            mock.patch.object(corral, "_probe_osc_colours", return_value=None),
            mock.patch.object(corral, "_require_tmux"),
            mock.patch("corral.ui.app.run_app", return_value=None) as run_app,
            mock.patch.object(corral.embed, "close_channel"),
            mock.patch.object(corral, "execute_launch") as execute_launch,
        ):
            stdin_mock.isatty.return_value = True
            stdout_mock.isatty.return_value = True
            keepalive_mock.new_session_ident.return_value = "xxxx"

            corral._dispatch_direct_launch(["claude"], registry)

        store_cls.return_value.load.assert_called_once_with()
        run_app.assert_called_once()
        args = run_app.call_args.args
        self.assertIs(args[0], store_cls.return_value)
        self.assertIs(args[1], True)
        self.assertEqual(args[2], corral._DirectLaunch(plan, "claude", "xxxx"))
        execute_launch.assert_not_called()
        keepalive_mock.wrap_plan.assert_not_called()


class HandoffDigestTests(unittest.TestCase):
    """跨运行时接手提示词里的对话摘录：标题只有十几个字，摘录是目标 agent 的任务锚点。"""

    class _StubRuntime(BaseRuntime):
        id = "claude"
        display_name = "Claude"
        executable = "claude"
        history_reading_hint = "格式提示"

        def __init__(self, messages) -> None:
            self._messages = messages

        def scan_sessions(self, limit, keep_ids=None):
            return []

        def load_conversation(self, session):
            if isinstance(self._messages, Exception):
                raise self._messages
            return self._messages

        def build_resume_plan(self, session):
            raise NotImplementedError

        def build_new_plan(self, handoff):
            raise NotImplementedError

        def build_new_session_plan(self, cwd):
            raise NotImplementedError

    def _export(self, messages, **session_extra):
        with tempfile.NamedTemporaryFile(suffix=".jsonl") as history:
            session = {
                "path": history.name,
                "cwd": "/tmp",
                "status_tag": titles.STATUS_PENDING,
                "first_user_msg": "",
                "last_user_msg": "",
                "last_agent_msg": "",
            }
            session.update(session_extra)
            return self._StubRuntime(messages).export_handoff(session, "标题")

    def test_digest_keeps_first_need_and_last_eight_messages_with_llm_facing_roles(self) -> None:
        # 角色必须标"用户"而不是"你"：摘录是给接手的大模型看的，"你"会被误解为指它自己。
        messages = [ConversationMessage("user", "最初的需求说明")]
        for i in range(10):
            role = "assistant" if i % 2 == 0 else "user"
            messages.append(ConversationMessage(role, f"消息{i}" + ("长" * 500 if i == 9 else "")))

        handoff = self._export(messages)

        lines = handoff.conversation_digest.splitlines()
        self.assertEqual(lines[0], t("handoff.digest.first_need") + "最初的需求说明")
        self.assertEqual(lines[1], t("handoff.digest.recent"))
        self.assertEqual(len(lines), 2 + 8)  # 最近 8 条
        self.assertTrue(
            lines[2].startswith((f"{t('handoff.role.user')}: ", f"{t('handoff.role.assistant')}: "))
        )
        self.assertNotIn("你:", handoff.conversation_digest)
        self.assertTrue(lines[-1].endswith("…"))  # 超长消息被截断

    def test_digest_skips_first_need_when_already_in_recent_window(self) -> None:
        messages = [
            ConversationMessage("user", "问题"),
            ConversationMessage("assistant", "答复"),
        ]
        handoff = self._export(messages)
        self.assertNotIn(t("handoff.digest.first_need"), handoff.conversation_digest)
        self.assertEqual(
            handoff.conversation_digest,
            (
                f"{t('handoff.digest.recent')}\n"
                f"{t('handoff.role.user')}: 问题\n"
                f"{t('handoff.role.assistant')}: 答复"
            ),
        )

    def test_digest_falls_back_to_scan_fields_when_conversation_unavailable(self) -> None:
        # 对话提取失败（异常）时静默降级到扫描层首尾消息，不阻断接力。
        handoff = self._export(
            OSError("boom"),
            first_user_msg="最初需求",
            last_user_msg="最后追问",
            last_agent_msg="最后答复",
        )
        self.assertEqual(
            handoff.conversation_digest,
            (
                f"{t('handoff.digest.first_need')}最初需求\n"
                f"{t('handoff.digest.recent')}\n"
                f"{t('handoff.role.user')}: 最后追问\n"
                f"{t('handoff.role.assistant')}: 最后答复"
            ),
        )

    def test_prompt_without_digest_or_status_matches_legacy_shape(self) -> None:
        handoff = self._export([], status_tag="")
        self.assertEqual(handoff.conversation_digest, "")
        prompt = handoff.render_prompt()
        self.assertNotIn(t("handoff.digest_intro", digest="").rstrip(), prompt)
        self.assertNotIn("会话状态", prompt)
        self.assertIn(t("handoff.read_without_digest"), prompt)

    def test_prompt_with_digest_omits_status_and_keeps_authority_note(self) -> None:
        messages = [
            ConversationMessage("user", "问题"),
            ConversationMessage("assistant", "答复"),
        ]
        prompt = self._export(messages).render_prompt()
        # 接力目的就是接着往下干，绝不能把源会话状态（尤其"已完成"）注入提示词误导接手方。
        self.assertNotIn("会话状态", prompt)
        self.assertIn(t("handoff.digest_intro", digest="").rstrip(), prompt)
        self.assertIn(t("handoff.read_with_digest"), prompt)
        self.assertIn(f"{t('handoff.role.user')}: 问题", prompt)

    def test_build_new_plan_prompt_carries_digest_for_both_runtimes(self) -> None:
        handoff = Handoff(
            source_runtime_id="claude", source_runtime_name="Claude", title="标题",
            history_path="/tmp/h.jsonl", original_cwd="/tmp", history_reading_hint="hint",
            conversation_digest="【最近对话】\n用户: 独特摘录内容",
        )
        for runtime in (ClaudeRuntime(), CodexRuntime()):
            plan = runtime.build_new_plan(handoff)
            self.assertIn("独特摘录内容", plan.argv[-1])


class AgentApiTests(unittest.TestCase):
    def _session(
        self, sid: str, title: str, mtime: int, *, cwd: str = "/tmp/demo", status=titles.STATUS_DONE,
        live: bool = False, pid: int | None = None, last_user_msg: str = "", last_agent_msg: str = "",
    ) -> dict:
        return {
            "source": "claude",
            "id": sid,
            "short_id": sid[:8],
            "cwd": cwd,
            "cwd_display": cwd,
            "mtime": mtime,
            "display_time": "07-01 12:00",
            "size_bytes": 1000,
            "size_kb": 1.0,
            "native_title": None,
            "fallback_title": title,
            "status_tag": status,
            "live": live,
            "pid": pid,
            "first_user_msg": title,
            "last_user_msg": last_user_msg,
            "last_agent_msg": last_agent_msg,
            "path": f"/tmp/{sid}.jsonl",
        }

    def _registry(self, sessions: list[dict], messages: list[ConversationMessage] | None = None):
        runtime = mock.Mock()
        runtime.id = "claude"
        runtime.display_name = "Claude"
        runtime.scan_sessions.return_value = sessions
        runtime.load_conversation.return_value = messages or []
        runtime.build_resume_plan.side_effect = (
            lambda session: LaunchPlan(argv=("claude", "--resume", session["id"]), cwd=session.get("cwd"))
        )
        runtime.export_handoff.side_effect = lambda session, title: Handoff(
            source_runtime_id="claude", source_runtime_name="Claude", title=title,
            history_path=session.get("path") or "", original_cwd=session.get("cwd") or "",
            history_reading_hint="hint",
        )

        class FakeRegistry:
            def __iter__(self):
                return iter([runtime])

            def get(self, runtime_id: str):
                if runtime_id != "claude":
                    raise AssertionError(runtime_id)
                return runtime

        return FakeRegistry(), runtime

    def test_list_top_is_result_limit_and_compact_fields_include_resume(self) -> None:
        # --limit 是扫描深度，--top 才是返回条数上限；compact 默认只保留 Agent 常用字段。
        sessions = [
            self._session("aaa11111", "第一个", 30),
            self._session("bbb22222", "第二个", 20),
            self._session("ccc33333", "第三个", 10),
        ]
        registry, runtime = self._registry(sessions)
        args = mock.Mock(runtime=None, limit=3, top=2, compact=True, status=None, cwd=None, fields=None)

        result = agent_api.cmd_list(args, registry)

        runtime.scan_sessions.assert_called_once_with(3)
        self.assertEqual(result["data"]["count"], 2)
        first = result["data"]["sessions"][0]
        self.assertEqual(first["id"], "aaa11111")
        self.assertEqual(first["resume_command"], "claude --resume aaa11111")
        self.assertTrue(first["resumable"])
        self.assertNotIn("history_path", first)

    def test_search_scores_and_sorts_by_relevance_before_time(self) -> None:
        # 标题命中比分散字段命中权重更高；排序先看相关性，再按更新时间。
        sessions = [
            self._session("oldtitle", "fable 数据修复", 10),
            self._session("newcwdxx", "普通问题", 999, cwd="/tmp/fable-project"),
            self._session("nomatchx", "普通问题", 1000),
        ]
        registry, _ = self._registry(sessions)
        args = mock.Mock(
            keywords=["fable"], deep=False, runtime=None, limit=10, top=2, compact=True, fields=None,
        )

        result = agent_api.cmd_search(args, registry)

        found = result["data"]["sessions"]
        self.assertEqual([item["id"] for item in found], ["oldtitle", "newcwdxx"])
        self.assertGreater(found[0]["score"], found[1]["score"])
        self.assertEqual(found[0]["matched_via"], "quick")
        self.assertEqual(found[0]["matched_fields"], ["title", "fallback_title", "first_user_msg"])
        self.assertIn("resume_command", found[0])

    def test_show_out_writes_full_result_and_stdout_returns_reference(self) -> None:
        session = self._session("show1234", "查看完整会话", 1)
        messages = [
            ConversationMessage("user", "问题一"),
            ConversationMessage("assistant", "答复一"),
            ConversationMessage("user", "问题二"),
        ]
        registry, _ = self._registry([session], messages)
        with tempfile.TemporaryDirectory() as td:
            out_path = os.path.join(td, "show.json")
            args = mock.Mock(session="claude:show1234", limit=10, full=True, messages=None, out=out_path,
                              compact=True, fields=None)

            result = agent_api.cmd_show(args, registry)

            self.assertTrue(result["data"]["messages_omitted"])
            self.assertEqual(result["data"]["output_path"], out_path)
            self.assertNotIn("messages", result["data"])
            saved = json.loads(Path(out_path).read_text(encoding="utf-8"))

        self.assertTrue(saved["ok"])
        self.assertEqual(saved["data"]["message_count_shown"], 3)
        self.assertEqual(saved["data"]["messages"][2]["text"], "问题二")

    def test_session_payload_exposes_live_pid_and_trims_summary(self) -> None:
        # 管家 Agent 场景：list/search 默认要能看出「哪个 CodingAgent 在跑」（live/pid）
        # 和「最近聊了什么」（last_user/last_agent，硬截断精简，不是全文）。
        long_text = "问" * 200
        session = self._session(
            "live1234", "运行中的会话", 1, live=True, pid=12345,
            last_user_msg=long_text, last_agent_msg="好的，正在处理",
        )
        payload = agent_api.session_payload(session, {}, runtime=None)

        self.assertTrue(payload["live"])
        self.assertEqual(payload["pid"], 12345)
        self.assertEqual(payload["last_agent"], "好的，正在处理")
        self.assertTrue(payload["last_user"].endswith("…"))
        self.assertLessEqual(len(payload["last_user"]), agent_api._SUMMARY_TRIM_LEN + 1)

        idle_session = self._session("idle5678", "已结束的会话", 1, live=False)
        idle_payload = agent_api.session_payload(idle_session, {}, runtime=None)
        self.assertFalse(idle_payload["live"])
        self.assertIsNone(idle_payload["pid"])

    def test_session_payload_exposes_keepalive_flag(self) -> None:
        session = self._session("keep1234", "保活中的会话", 1, live=True, pid=12345)
        session["keepalive_name"] = "sc-claude-keep1234"
        payload = agent_api.session_payload(session, {}, runtime=None)
        self.assertTrue(payload["keepalive"])

        no_keepalive_session = self._session("plain1234", "普通会话", 1)
        plain_payload = agent_api.session_payload(no_keepalive_session, {}, runtime=None)
        self.assertFalse(plain_payload["keepalive"])

    def test_session_payload_exposes_attention_kind(self) -> None:
        session = self._session("attn1234", "等回话的会话", 1, live=True, pid=7)
        session["attention_kind"] = "waiting"
        payload = agent_api.session_payload(session, {}, runtime=None)
        self.assertEqual(payload["attention"], "waiting")

        idle = agent_api.session_payload(self._session("idle5678", "普通会话", 1), {}, runtime=None)
        self.assertEqual(idle["attention"], "none")

    def test_list_and_search_annotate_scanned_sessions_before_building_payload(self) -> None:
        # cmd_list/cmd_search 必须先对本次扫描出的会话跑一次 keepalive.annotate，
        # 才能把「是否在后台保活」正确反映进输出字段，不能让调用方自己再查一遍。
        sessions = [self._session("live1234", "在跑的会话", 1, live=True, pid=555)]
        registry, _ = self._registry(sessions)

        def _fake_annotate(scanned_sessions):
            for item in scanned_sessions:
                if item.get("pid") == 555:
                    item["keepalive_name"] = "sc-claude-live1234"

        with mock.patch.object(agent_api.liveness, "annotate", side_effect=_fake_annotate) as mocked:
            list_args = mock.Mock(runtime=None, limit=10, top=None, compact=True, status=None, cwd=None,
                                   fields=None, live=None)
            list_result = agent_api.cmd_list(list_args, registry)
            self.assertTrue(mocked.called)
            self.assertTrue(list_result["data"]["sessions"][0]["keepalive"])

        with mock.patch.object(agent_api.liveness, "annotate", side_effect=_fake_annotate):
            search_args = mock.Mock(keywords=["在跑"], deep=False, runtime=None, limit=10, top=None,
                                     compact=True, fields=None)
            search_result = agent_api.cmd_search(search_args, registry)
            self.assertTrue(search_result["data"]["sessions"][0]["keepalive"])

    def test_list_live_filter_keeps_only_running_sessions(self) -> None:
        sessions = [
            self._session("run11111", "跑着的", 20, live=True, pid=111),
            self._session("done2222", "结束的", 10, live=False),
        ]
        registry, _ = self._registry(sessions)
        args = mock.Mock(runtime=None, limit=10, top=None, compact=True, status=None, cwd=None, fields=None, live=True)

        result = agent_api.cmd_list(args, registry)

        self.assertEqual([s["id"] for s in result["data"]["sessions"]], ["run11111"])

    def test_list_keepalive_filter_keeps_only_hosted_sessions(self) -> None:
        sessions = [
            self._session("run11111", "托管的", 20, live=True, pid=111),
            self._session("run22222", "外面跑的", 15, live=True, pid=222),
        ]
        registry, _ = self._registry(sessions)

        def _fake_annotate(scanned_sessions):
            for item in scanned_sessions:
                if item.get("pid") == 111:
                    item["keepalive_name"] = "corral-claude-run11111"

        with mock.patch.object(agent_api.liveness, "annotate", side_effect=_fake_annotate):
            args = mock.Mock(
                runtime=None, limit=10, top=None, compact=True, status=None, cwd=None,
                fields=None, live=True, keepalive=True,
            )
            result = agent_api.cmd_list(args, registry)

        self.assertEqual([s["id"] for s in result["data"]["sessions"]], ["run11111"])
        self.assertTrue(result["data"]["sessions"][0]["keepalive"])
        self.assertEqual(result["data"]["sessions"][0]["attention"], "none")

    def test_list_without_live_flag_still_returns_all_sessions(self) -> None:
        # 回归：mock.Mock() 未显式设置的属性会自动生成一个真值 Mock，--live 判断必须
        # 用 `is True` 而不是单纯 truthy，否则老调用方（未传 live 参数）会被误过滤。
        sessions = [
            self._session("run11111", "跑着的", 20, live=True, pid=111),
            self._session("done2222", "结束的", 10, live=False),
        ]
        registry, _ = self._registry(sessions)
        args = mock.Mock(runtime=None, limit=10, top=None, compact=True, status=None, cwd=None, fields=None)

        result = agent_api.cmd_list(args, registry)

        self.assertEqual(len(result["data"]["sessions"]), 2)

    def test_search_live_filter_keeps_only_running_sessions(self) -> None:
        sessions = [
            self._session("run11111", "fable 跑着的", 20, live=True, pid=111),
            self._session("done2222", "fable 结束的", 10, live=False),
        ]
        registry, _ = self._registry(sessions)
        args = mock.Mock(
            keywords=["fable"], deep=False, runtime=None, limit=10, top=None, compact=True, fields=None, live=True,
        )

        result = agent_api.cmd_search(args, registry)

        self.assertEqual([s["id"] for s in result["data"]["sessions"]], ["run11111"])

    def test_context_reports_live_and_pid(self) -> None:
        session = self._session("ctxsess1", "上下文会话", 1, live=True, pid=999)
        registry, _ = self._registry([session])
        args = mock.Mock(session="claude:ctxsess1", limit=10)

        result = agent_api.cmd_context(args, registry)

        self.assertTrue(result["data"]["live"])
        self.assertEqual(result["data"]["pid"], 999)

    def test_describe_list_and_search_document_live_flag_and_new_fields(self) -> None:
        # corral describe 的输出与实现同源，改完命令必须同步这里，防止参数/字段说明漂移。
        args = mock.Mock(target="list")
        result = agent_api.cmd_describe(args, registry=None)
        list_flags = [flag for arg in result["data"]["args"] for flag in arg["flags"]]
        self.assertIn("--live", list_flags)
        self.assertIn("--keepalive", list_flags)
        self.assertIn("live", result["data"]["fields"])
        self.assertIn("keepalive", result["data"]["fields"])
        self.assertIn("attention", result["data"]["fields"])
        self.assertIn("pid", result["data"]["fields"])
        self.assertIn("last_user", result["data"]["fields"])
        self.assertIn("last_agent", result["data"]["fields"])

        args = mock.Mock(target="search")
        result = agent_api.cmd_describe(args, registry=None)
        search_flags = [flag for arg in result["data"]["args"] for flag in arg["flags"]]
        self.assertIn("--live", search_flags)
        self.assertIn("--keepalive", search_flags)


class CursorScanTests(unittest.TestCase):
    """Cursor CLI：~/.cursor/chats/<ws>/<chatId>/meta.json + prompt_history + store.db。"""

    def _chat(
        self,
        root: Path,
        ws: str,
        chat_id: str,
        *,
        title: str,
        cwd: str,
        updated_ms: int,
        prompts: list[str],
        has_conversation: bool = True,
        is_subagent: bool = False,
        parent_id: str | None = None,
        root_parent_id: str | None = None,
        store_meta_hex: bool = True,
    ) -> Path:
        d = root / ws / chat_id
        d.mkdir(parents=True)
        meta = {
            "schemaVersion": 1,
            "createdAtMs": updated_ms - 1000,
            "updatedAtMs": updated_ms,
            "hasConversation": has_conversation,
            "title": title,
            "cwd": cwd,
        }
        if is_subagent:
            meta["isSubagent"] = True
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        if prompts:
            (d / "prompt_history.json").write_text(
                json.dumps(prompts, ensure_ascii=False), encoding="utf-8"
            )
        if parent_id:
            self._write_subagent_store(
                d,
                chat_id,
                parent_id=parent_id,
                root_parent_id=root_parent_id,
                as_hex=store_meta_hex,
            )
        return d

    def _write_subagent_store(
        self,
        chat_dir: Path,
        chat_id: str,
        *,
        parent_id: str,
        root_parent_id: str | None = None,
        as_hex: bool = True,
    ) -> None:
        payload = {
            "agentId": chat_id,
            "name": "New Agent",
            "subagentInfo": {
                "parentAgentId": parent_id,
                "rootParentAgentId": root_parent_id or parent_id,
                "typeName": "explore",
                "toolCallId": "call-test",
            },
        }
        raw = json.dumps(payload, ensure_ascii=False)
        value = raw.encode("utf-8").hex() if as_hex else raw
        conn = sqlite3.connect(str(chat_dir / "store.db"))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta(key, value) VALUES ('0', ?)", (value,))
        conn.commit()
        conn.close()

    def test_scan_sessions_reads_meta_and_prompt_history(self) -> None:
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            self._chat(
                root,
                "ws1",
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                title="Cursor CLI Support",
                cwd=cwd,
                updated_ms=1_700_000_000_000,
                prompts=["最新一句", "最早一句"],
            )
            self._chat(
                root,
                "ws1",
                "empty-chat",
                title="",
                cwd=cwd,
                updated_ms=1_700_000_000_100,
                prompts=[],
                has_conversation=False,
            )
            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_processes", return_value=[]
            ):
                sessions = scan_cursor.scan_sessions(limit=10)
            self.assertEqual(len(sessions), 1)
            s = sessions[0]
            self.assertEqual(s["source"], "cursor")
            self.assertEqual(s["id"], "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
            self.assertEqual(s["native_title"], "Cursor CLI Support")
            self.assertEqual(s["cwd"], cwd)
            self.assertEqual(s["last_user_msg"], "最新一句")
            self.assertEqual(s["first_user_msg"], "最早一句")
            self.assertTrue(s["path"].endswith(s["id"]) or s["path"].endswith("store.db"))

    def test_scan_signature_tracks_file_stat_not_parent_dir_mtime(self) -> None:
        """祖先目录 touch 不得改变签名；meta 变更必须改变；仅 WAL 追加不得改变。

        列表级 ``scan_signature`` 故意不含 ``store.db-wal``——流式写入只动 WAL
        时要复用上一轮 ``scan_sessions``。正文缓存仍通过 per-session
        ``extra_version`` 带上 WAL。
        """
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            chat_dir = self._chat(
                root,
                "ws1",
                "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                title="签名",
                cwd=cwd,
                updated_ms=1_700_000_000_000,
                prompts=["hello"],
            )
            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_pid_snapshot", return_value=()
            ):
                first = scan_cursor.scan_signature()
                os.utime(root / "ws1", None)
                after_dir = scan_cursor.scan_signature()
                wal = chat_dir / "store.db-wal"
                wal.write_bytes(b"wal-bytes")
                after_wal = scan_cursor.scan_signature()
                meta = chat_dir / "meta.json"
                meta.write_text(meta.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                after_file = scan_cursor.scan_signature()

        self.assertEqual(first, after_dir)
        self.assertEqual(first, after_wal)
        self.assertNotEqual(first, after_file)

    def test_scan_filters_self_generated_title_sessions(self) -> None:
        """标题生成 `agent -p` 落盘会话必须过滤 PROMPT_MARKER（首条/fallback/原生标题）。"""
        from corral.runtime.host_extension import corral_host_extension
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            noise = f"{titles.PROMPT_MARKER}（JSON 数组…）"
            self._chat(
                root,
                "ws1",
                "real-chat-id-0001-0002-0003-000000000001",
                title="真实会话",
                cwd=cwd,
                updated_ms=1_700_000_000_000,
                prompts=["真实的用户问题"],
            )
            self._chat(
                root,
                "ws1",
                "noise-first-id-0001-0002-0003-000000000002",
                title="噪音首条",
                cwd=cwd,
                updated_ms=1_700_000_000_100,
                prompts=[noise],
            )
            self._chat(
                root,
                "ws1",
                "noise-native-id-0001-0002-0003-000000000003",
                title=noise,
                cwd=cwd,
                updated_ms=1_700_000_000_200,
                prompts=["普通用户消息"],
            )
            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_processes", return_value=[]
            ):
                sessions = scan_cursor.scan_sessions(limit=10, host=corral_host_extension())

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["id"], "real-chat-id-0001-0002-0003-000000000001")
        self.assertEqual(sessions[0]["first_user_msg"], "真实的用户问题")

    def test_scan_filters_subagent_sessions(self) -> None:
        """Cursor subagent 即使带 title/cwd/prompt 也不得进入列表。"""
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            self._chat(
                root,
                "ws1",
                "parent-chat-id-0001-0002-0003-000000000001",
                title="父会话",
                cwd=cwd,
                updated_ms=1_700_000_000_000,
                prompts=["用户的顶层问题"],
            )
            self._chat(
                root,
                "ws1",
                "subagent-id-0001-0002-0003-000000000002",
                title="Subagent 任务",
                cwd=cwd,
                updated_ms=1_700_000_000_100,
                prompts=["派生给 subagent 的任务"],
                is_subagent=True,
            )
            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_processes", return_value=[]
            ):
                sessions = scan_cursor.scan_sessions(limit=10)

        self.assertEqual([s["id"] for s in sessions], ["parent-chat-id-0001-0002-0003-000000000001"])

    def test_decode_store_meta_value_accepts_json_and_hex(self) -> None:
        from corral.scan import cursor as scan_cursor

        payload = {"agentId": "abc", "subagentInfo": {"parentAgentId": "parent"}}
        raw = json.dumps(payload)
        self.assertEqual(scan_cursor._decode_store_meta_value(raw)["agentId"], "abc")
        self.assertEqual(
            scan_cursor._decode_store_meta_value(raw.encode().hex())["subagentInfo"]["parentAgentId"],
            "parent",
        )
        self.assertIsNone(scan_cursor._decode_store_meta_value("not-json-or-hex"))

    def test_live_flags_resume_subagent_marks_parent_live(self) -> None:
        """主会话进程不在、子代理 --resume 仍在时，父会话必须是进行中。"""
        from corral.scan import cursor as scan_cursor

        parent_id = "23da49b0-a1da-427c-bc5b-cb713f19668f"
        sub_id = "70c7eb76-2bac-4497-bd3f-24dd94680443"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            self._chat(
                root, "ws1", parent_id, title="父会话", cwd=cwd,
                updated_ms=1_700_000_000_000, prompts=["顶层任务"],
            )
            self._chat(
                root, "ws1", sub_id, title="Subagent", cwd=cwd,
                updated_ms=1_700_000_000_100, prompts=["子任务"],
                is_subagent=True, parent_id=parent_id,
            )
            real_cwd = os.path.realpath(cwd)
            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_processes", return_value=[(4242, real_cwd)]
            ), mock.patch.object(
                scan_cursor,
                "process_command_line",
                return_value=f"/Users/x/.local/bin/agent --force --resume {sub_id}",
            ), mock.patch.object(
                scan_cursor, "_cursor_store_paths_for_pids", return_value={4242: []}
            ), mock.patch.object(scan_cursor, "process_environ", return_value={}):
                sessions = scan_cursor.scan_sessions(limit=10)

        self.assertEqual([s["id"] for s in sessions], [parent_id])
        self.assertTrue(sessions[0]["live"])
        self.assertEqual(sessions[0]["pid"], 4242)

    def test_live_flags_open_subagent_store_marks_parent_live(self) -> None:
        """无 --resume、只打开了子代理 store.db 时，也要把 live 记到父会话。"""
        from corral.scan import cursor as scan_cursor

        parent_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee1"
        sub_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeee2"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            self._chat(
                root, "ws1", parent_id, title="父会话", cwd=cwd,
                updated_ms=1_700_000_000_000, prompts=["顶层任务"],
            )
            self._chat(
                root, "ws1", sub_id, title="Subagent", cwd=cwd,
                updated_ms=1_700_000_000_100, prompts=["子任务"],
                is_subagent=True, parent_id=parent_id, store_meta_hex=False,
            )
            real_cwd = os.path.realpath(cwd)
            open_paths = {8801: [f"/Users/x/.cursor/chats/ws1/{sub_id}/store.db"]}
            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_processes", return_value=[(8801, real_cwd)]
            ), mock.patch.object(
                scan_cursor,
                "process_command_line",
                return_value="/Users/x/.local/bin/agent --force",
            ), mock.patch.object(
                scan_cursor, "_cursor_store_paths_for_pids", return_value=open_paths
            ), mock.patch.object(scan_cursor, "process_environ", return_value={}):
                sessions = scan_cursor.scan_sessions(limit=10)

        self.assertEqual([s["id"] for s in sessions], [parent_id])
        self.assertTrue(sessions[0]["live"])
        self.assertEqual(sessions[0]["pid"], 8801)

    def test_live_flags_parent_pid_wins_over_subagent_pid(self) -> None:
        """父进程仍在时不要被子代理 pid 覆盖，避免托管画面绑错窗。"""
        from corral.scan import cursor as scan_cursor

        parent_id = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        sub_id = "cccccccc-dddd-eeee-ffff-111111111111"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            self._chat(
                root, "ws1", parent_id, title="父会话", cwd=cwd,
                updated_ms=1_700_000_000_000, prompts=["顶层任务"],
            )
            self._chat(
                root, "ws1", sub_id, title="Subagent", cwd=cwd,
                updated_ms=1_700_000_000_100, prompts=["子任务"],
                is_subagent=True, parent_id=parent_id,
            )
            real_cwd = os.path.realpath(cwd)
            agents = [(111, real_cwd), (222, real_cwd)]
            cmdlines = {
                111: f"/Users/x/.local/bin/agent --force --resume {parent_id}",
                222: f"/Users/x/.local/bin/agent --force --resume {sub_id}",
            }

            def fake_cmdline(pid: int) -> str:
                return cmdlines[pid]

            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_processes", return_value=agents
            ), mock.patch.object(
                scan_cursor, "process_command_line", side_effect=fake_cmdline
            ), mock.patch.object(
                scan_cursor, "_cursor_store_paths_for_pids", return_value={}
            ), mock.patch.object(scan_cursor, "process_environ", return_value={}):
                sessions = scan_cursor.scan_sessions(limit=10)

        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0]["live"])
        self.assertEqual(sessions[0]["pid"], 111)

    def test_live_flags_nested_subagent_binds_root_parent(self) -> None:
        """嵌套子代理必须绑到用户能看见的顶层父会话，不能停在中间层。"""
        from corral.scan import cursor as scan_cursor

        root_id = "dddddddd-eeee-ffff-aaaa-222222222222"
        mid_id = "eeeeeeee-ffff-aaaa-bbbb-333333333333"
        leaf_id = "ffffffff-aaaa-bbbb-cccc-444444444444"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            self._chat(
                root, "ws1", root_id, title="顶层", cwd=cwd,
                updated_ms=1_700_000_000_000, prompts=["顶层任务"],
            )
            self._chat(
                root, "ws1", mid_id, title="中间层", cwd=cwd,
                updated_ms=1_700_000_000_050, prompts=["中间任务"],
                is_subagent=True, parent_id=root_id,
            )
            self._chat(
                root, "ws1", leaf_id, title="叶子", cwd=cwd,
                updated_ms=1_700_000_000_100, prompts=["叶子任务"],
                is_subagent=True, parent_id=mid_id, root_parent_id=root_id,
            )
            real_cwd = os.path.realpath(cwd)
            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_processes", return_value=[(9001, real_cwd)]
            ), mock.patch.object(
                scan_cursor,
                "process_command_line",
                return_value=f"/Users/x/.local/bin/agent --force --resume {leaf_id}",
            ), mock.patch.object(
                scan_cursor, "_cursor_store_paths_for_pids", return_value={9001: []}
            ), mock.patch.object(scan_cursor, "process_environ", return_value={}):
                sessions = scan_cursor.scan_sessions(limit=10)

        self.assertEqual([s["id"] for s in sessions], [root_id])
        self.assertTrue(sessions[0]["live"])
        self.assertEqual(sessions[0]["pid"], 9001)

    def test_live_flags_subagent_without_parent_store_does_not_guess(self) -> None:
        """子代理没有父会话指针时，禁止按同目录猜测绑到别的卡。"""
        from corral.scan import cursor as scan_cursor

        parent_id = "12121212-3434-5656-7878-909090909090"
        sub_id = "34343434-5656-7878-9090-121212121212"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            self._chat(
                root, "ws1", parent_id, title="父会话", cwd=cwd,
                updated_ms=1_700_000_000_000, prompts=["顶层任务"],
            )
            self._chat(
                root, "ws1", sub_id, title="Subagent", cwd=cwd,
                updated_ms=1_700_000_000_100, prompts=["子任务"],
                is_subagent=True,
            )
            real_cwd = os.path.realpath(cwd)
            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_processes", return_value=[(7007, real_cwd)]
            ), mock.patch.object(
                scan_cursor,
                "process_command_line",
                return_value=f"/Users/x/.local/bin/agent --force --resume {sub_id}",
            ), mock.patch.object(
                scan_cursor, "_cursor_store_paths_for_pids", return_value={7007: []}
            ), mock.patch.object(scan_cursor, "process_environ", return_value={}):
                sessions = scan_cursor.scan_sessions(limit=10)

        self.assertEqual([s["id"] for s in sessions], [parent_id])
        self.assertFalse(sessions[0]["live"])
        self.assertIsNone(sessions[0]["pid"])

    def test_load_conversation_extracts_user_query_and_assistant(self) -> None:
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            chat = Path(td) / "chat"
            chat.mkdir()
            db = chat / "store.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            user_blob = json.dumps(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "<user_query>\n帮我加上cursor-cli支持\n</user_query>",
                        }
                    ],
                }
            ).encode()
            asst_blob = json.dumps(
                {
                    "role": "assistant",
                    "content": "先摸清现有运行时适配器的模式",
                }
            ).encode()
            conn.execute("INSERT INTO blobs VALUES ('u1', ?)", (user_blob,))
            conn.execute("INSERT INTO blobs VALUES ('a1', ?)", (asst_blob,))
            conn.execute("INSERT INTO blobs VALUES ('bin1', ?)", (b"\n\x20not-json",))
            conn.commit()
            conn.close()
            messages = scan_cursor.load_conversation(str(chat))
            self.assertEqual(
                [(m.role, m.text) for m in messages],
                [
                    ("user", "帮我加上cursor-cli支持"),
                    ("assistant", "先摸清现有运行时适配器的模式"),
                ],
            )

    def test_load_conversation_reads_uncheckpointed_wal_tail(self) -> None:
        """Cursor store.db 长期 WAL：最新轮次常只在 -wal 里，immutable 会漏掉尾巴。

        真机：预览和小窗都缺最后几条；主库几乎空时甚至读不到 blobs 表。
        """
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            chat = Path(td) / "chat"
            chat.mkdir()
            db = chat / "store.db"
            conn = sqlite3.connect(str(db))
            self.assertEqual(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
            # 先落一条并 checkpoint，主库有表结构；再追加尾巴且故意不 checkpoint。
            conn.execute(
                "INSERT INTO blobs VALUES ('u1', ?)",
                (
                    json.dumps(
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "<user_query>\n旧问\n</user_query>"}],
                        }
                    ).encode(),
                ),
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute(
                "INSERT INTO blobs VALUES ('u2', ?)",
                (
                    json.dumps(
                        {
                            "role": "user",
                            "content": [{"type": "text", "text": "<user_query>\n最新一句\n</user_query>"}],
                        }
                    ).encode(),
                ),
            )
            conn.execute(
                "INSERT INTO blobs VALUES ('a2', ?)",
                (json.dumps({"role": "assistant", "content": "最新答复"}).encode(),),
            )
            conn.commit()
            # 保持写连接未 checkpoint，模拟 agent 仍占用 / 刚写完的真实状态。
            self.assertTrue((chat / "store.db-wal").is_file())
            self.assertGreater((chat / "store.db-wal").stat().st_size, 0)

            # 对照：immutable=1 看不到 WAL 尾巴（本回归要修掉的旧行为）。
            uri_imm = f"file:{db.resolve()}?mode=ro&immutable=1"
            with sqlite3.connect(uri_imm, uri=True) as frozen:
                frozen_n = frozen.execute("SELECT count(*) FROM blobs").fetchone()[0]
            self.assertEqual(frozen_n, 1)

            messages = scan_cursor.load_conversation(str(chat))
            self.assertEqual(
                [(m.role, m.text) for m in messages],
                [
                    ("user", "旧问"),
                    ("user", "最新一句"),
                    ("assistant", "最新答复"),
                ],
            )
            conn.close()

    def test_live_flags_bind_resume_and_open_store_separately_in_same_cwd(self) -> None:
        """同 cwd 下旧 --resume 与打开了新 store.db 的 agent 不得串台。

        真机故障演进：cwd→最新会话兜底会把空壳/接力进程绑到同目录更早的历史，
        侧边栏标题对、右栏却是另一场对话或空白欢迎页。
        """
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            old_id = "ba6f7c50-b53e-4beb-ae6d-7e4345cfbfa2"
            new_id = "961e443e-5e59-4b7d-bb7a-213b016bf119"
            self._chat(
                root,
                "ws1",
                old_id,
                title="Hello There",
                cwd=cwd,
                updated_ms=1_700_000_000_000,
                prompts=["旧会话"],
            )
            self._chat(
                root,
                "ws1",
                new_id,
                title="HY2 Node Debug",
                cwd=cwd,
                updated_ms=1_700_000_100_000,
                prompts=["新接续"],
            )
            real_cwd = os.path.realpath(cwd)
            agents = [
                (22807, real_cwd),  # 接力新建，无 --resume，但已打开新会话 store.db
                (25594, real_cwd),  # 旧会话 --resume
            ]
            cmdlines = {
                22807: (
                    "/Users/x/.local/bin/agent --force --add-dir /tmp/history "
                    "你正在接力一个来自 Claude 的会话"
                ),
                25594: (
                    f"/Users/x/.local/bin/agent --force --resume {old_id}"
                ),
            }
            open_paths = {
                22807: [
                    f"/Users/x/.cursor/chats/ws1/{new_id}/store.db",
                ],
                25594: [],
            }

            def fake_cmdline(pid: int) -> str:
                return cmdlines[pid]

            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_processes", return_value=agents
            ), mock.patch.object(
                scan_cursor, "process_command_line", side_effect=fake_cmdline
            ), mock.patch.object(
                scan_cursor, "_cursor_store_paths_for_pids", return_value=open_paths
            ), mock.patch.object(
                scan_cursor, "process_environ", return_value={}
            ):
                sessions = scan_cursor.scan_sessions(limit=10)

            by_id = {s["id"]: s for s in sessions}
            self.assertTrue(by_id[old_id]["live"])
            self.assertEqual(by_id[old_id]["pid"], 25594)
            self.assertTrue(by_id[new_id]["live"])
            self.assertEqual(by_id[new_id]["pid"], 22807)

    def test_live_flags_do_not_bind_blank_agent_to_older_cwd_history(self) -> None:
        """无 resume、未打开 store.db 的空壳 agent 不得冒充同目录历史会话。"""
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            history_id = "210fc78f-20ea-4e94-89c6-257fe566b674"
            self._chat(
                root,
                "ws1",
                history_id,
                title="Agent Runtime Selector",
                cwd=cwd,
                updated_ms=1_700_000_000_000,
                prompts=["我想加个顶栏"],
            )
            real_cwd = os.path.realpath(cwd)
            agents = [(80250, real_cwd)]

            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_processes", return_value=agents
            ), mock.patch.object(
                scan_cursor,
                "process_command_line",
                return_value="/Users/x/.local/bin/agent --force",
            ), mock.patch.object(
                scan_cursor, "_cursor_store_paths_for_pids", return_value={80250: []}
            ), mock.patch.object(
                scan_cursor,
                "process_environ",
                return_value={"CORRAL_SESSION_ID": "01a1d8c7"},  # 空白新建临时 id
            ):
                sessions = scan_cursor.scan_sessions(limit=10)

            self.assertEqual(len(sessions), 1)
            self.assertFalse(sessions[0]["live"])
            self.assertIsNone(sessions[0]["pid"])

    def test_live_flags_bind_via_corral_session_env(self) -> None:
        """托管注入的完整 CORRAL_SESSION_ID 可在无 resume / 尚未打开 db 时精确绑定。"""
        from corral.runtime.host_extension import corral_host_extension
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            chat_id = "5c2ab03a-4f61-4c80-9a47-a4c6c540e1c9"
            self._chat(
                root,
                "ws1",
                chat_id,
                title=None,
                cwd=cwd,
                updated_ms=1_700_000_000_000,
                prompts=["接续"],
            )
            real_cwd = os.path.realpath(cwd)

            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_processes", return_value=[(55021, real_cwd)]
            ), mock.patch.object(
                scan_cursor,
                "process_command_line",
                return_value="/Users/x/.local/bin/agent --force",
            ), mock.patch.object(
                scan_cursor, "_cursor_store_paths_for_pids", return_value={55021: []}
            ), mock.patch.object(
                scan_cursor,
                "process_environ",
                return_value={"CORRAL_SESSION_ID": chat_id},
            ):
                sessions = scan_cursor.scan_sessions(limit=10, host=corral_host_extension())

            self.assertTrue(sessions[0]["live"])
            self.assertEqual(sessions[0]["pid"], 55021)

    def test_resume_id_from_cmdline_parses_equals_and_skips_minus_one(self) -> None:
        from corral.scan import cursor as scan_cursor

        self.assertEqual(
            scan_cursor._resume_id_from_cmdline(
                "agent --resume=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            ),
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        )
        self.assertIsNone(scan_cursor._resume_id_from_cmdline("agent --resume=-1"))
        self.assertIsNone(scan_cursor._resume_id_from_cmdline("agent --force"))

    def test_live_flags_prefer_blank_host_over_secondary_resume(self) -> None:
        """同一 chat 并存无 resume 托管与二次 --resume 时，live pid 绑前者。

        否则 annotate 会挂到 corral-cursor-<uuid>，占位卡 corral-cursor-<8位>
        退不掉，侧边栏双卡。
        """
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cwd = str(root / "proj")
            Path(cwd).mkdir()
            chat_id = "cdadd85a-bdf5-4d65-bb52-92e8f2a84b60"
            self._chat(
                root,
                "ws1",
                chat_id,
                title="Corral Session Issue",
                cwd=cwd,
                updated_ms=1_700_000_000_000,
                prompts=["奇怪的现象"],
            )
            real_cwd = os.path.realpath(cwd)
            blank_pid = 3294260
            resume_pid = 3306659
            agents = [
                (resume_pid, real_cwd),  # 故意把 resume 放前面，确保不是靠遍历顺序
                (blank_pid, real_cwd),
            ]
            cmdlines = {
                blank_pid: "/Users/x/.local/bin/agent --force",
                resume_pid: f"/Users/x/.local/bin/agent --force --resume {chat_id}",
            }
            open_paths = {
                blank_pid: [f"/Users/x/.cursor/chats/ws1/{chat_id}/store.db"],
                resume_pid: [f"/Users/x/.cursor/chats/ws1/{chat_id}/store.db"],
            }

            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_processes", return_value=agents
            ), mock.patch.object(
                scan_cursor,
                "process_command_line",
                side_effect=lambda pid: cmdlines[pid],
            ), mock.patch.object(
                scan_cursor, "_cursor_store_paths_for_pids", return_value=open_paths
            ), mock.patch.object(
                scan_cursor, "process_environ", return_value={}
            ):
                sessions = scan_cursor.scan_sessions(limit=10)

            self.assertEqual(len(sessions), 1)
            self.assertTrue(sessions[0]["live"])
            self.assertEqual(sessions[0]["pid"], blank_pid)

    def test_chat_ids_from_open_paths_dedupes_wal_shm(self) -> None:
        from corral.scan import cursor as scan_cursor

        chat_id = "8150d335-b9dd-445c-8ed4-32b0276406fa"
        paths = [
            f"/Users/x/.cursor/chats/ws/{chat_id}/store.db-shm",
            f"/Users/x/.cursor/chats/ws/{chat_id}/store.db",
            f"/Users/x/.cursor/chats/ws/{chat_id}/store.db-wal",
            "/Users/x/.cursor/chats/ws/other/meta.json",
        ]
        self.assertEqual(scan_cursor._chat_ids_from_open_paths(paths), [chat_id])

    def test_is_cursor_agent_cmdline_accepts_wrapper_skips_worker(self) -> None:
        """Cursor agent 判活必须认 wrapper/cmdline，并排除 worker-server。"""
        from corral.scan.common import is_cursor_agent_cmdline

        self.assertTrue(
            is_cursor_agent_cmdline(
                "/home/u/.local/bin/agent --use-system-ca "
                "/home/u/.local/share/cursor-agent/versions/2026.07.20/index.js --force"
            )
        )
        self.assertTrue(
            is_cursor_agent_cmdline(
                "/home/u/.local/share/cursor-agent/versions/x/node "
                "/home/u/.local/share/cursor-agent/versions/x/index.js --force "
                "--resume aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            )
        )
        self.assertFalse(
            is_cursor_agent_cmdline(
                "/home/u/.local/share/cursor-agent/versions/x/node "
                "/home/u/.local/share/cursor-agent/versions/x/index.js worker-server"
            )
        )
        self.assertFalse(is_cursor_agent_cmdline("claude --resume x"))
        self.assertFalse(is_cursor_agent_cmdline(""))

    def test_is_pi_cmdline_accepts_node_wrapper_skips_title_generator(self) -> None:
        from corral.scan.common import is_pi_cmdline

        self.assertTrue(is_pi_cmdline("pi --approve"))
        self.assertTrue(
            is_pi_cmdline(
                "node /home/x/.npm-global/lib/node_modules/"
                "@earendil-works/pi-coding-agent/dist/cli.js --approve"
            )
        )
        self.assertFalse(is_pi_cmdline("pi --approve --no-session --no-tools --print hi"))
        self.assertFalse(is_pi_cmdline("claude --resume x"))
        self.assertFalse(is_pi_cmdline(""))

    def test_live_processes_agent_finds_mainthread_renamed_process(self) -> None:
        """comm 被改成 MainThread 时 pgrep -x agent 为空，仍须按 cmdline 找到 agent。

        真机（2026-07-23）：Cursor agent 的 /proc/pid/comm=MainThread，侧边栏
        live 全灭 → 占位卡无法退役 → 同一会话双卡（临时 id + 真实 UUID）。
        """
        from corral.scan import common

        agent_pid = 424242
        cwd = "/tmp/corral-agent-live-test"

        def fake_check_output(argv, **kwargs):
            if argv[:2] == ["pgrep", "-x"]:
                raise subprocess.CalledProcessError(1, argv)
            raise AssertionError(f"unexpected check_output: {argv}")

        with mock.patch.object(
            common.subprocess, "check_output", side_effect=fake_check_output
        ), mock.patch.object(
            common, "_cursor_agent_pids_by_cmdline", return_value=[agent_pid]
        ), mock.patch.object(
            common.os, "readlink", return_value=cwd
        ), mock.patch.object(
            common.sys, "platform", "linux"
        ), mock.patch.object(
            common.os.path, "realpath", side_effect=lambda p: p
        ):
            found = common.live_processes("agent")

        self.assertEqual(found, [(agent_pid, cwd)])

    def test_live_processes_batches_cwd_lsof_on_macos_and_caches_pid_set(self) -> None:
        """macOS 查 cwd 必须一次合并 lsof；pid 集合不变时不得再 fork。"""
        from corral.scan import common

        common.clear_live_cwd_cache()
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            result = mock.Mock()
            result.stdout = (
                b"p11\nfcwd\nn/tmp/a\n"
                b"p22\nfcwd\nn/tmp/b\n"
            )
            return result

        with mock.patch.object(
            common, "_pids_for_process_name", return_value=[11, 22]
        ), mock.patch.object(
            common.sys, "platform", "darwin"
        ), mock.patch.object(
            common.subprocess, "run", side_effect=fake_run
        ), mock.patch.object(
            common.os.path, "realpath", side_effect=lambda path: path
        ):
            first = common.live_processes("agent")
            second = common.live_processes("agent")

        self.assertEqual(first, [(11, "/tmp/a"), (22, "/tmp/b")])
        self.assertEqual(second, first)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:5], ["lsof", "-a", "-d", "cwd", "-Fn"])
        self.assertEqual(calls[0][-2:], ["-p", "11,22"])
        common.clear_live_cwd_cache()

    def test_process_command_line_caches_until_pid_leaves_live_set(self) -> None:
        """同一 pid 在仍存活期间不得反复 ps；离开 live 集合后才失效。"""
        from corral.scan import common

        common.clear_live_cwd_cache()
        calls: list[list[str]] = []

        def fake_check_output(argv, **kwargs):
            calls.append(list(argv))
            if argv[:2] == ["ps", "-p"]:
                return b"agent --resume abc\n"
            raise AssertionError(f"unexpected check_output: {argv}")

        with mock.patch.object(
            common, "_pids_for_process_name", side_effect=[[99], [88]]
        ), mock.patch.object(
            common.sys, "platform", "darwin"
        ), mock.patch.object(
            common.subprocess, "check_output", side_effect=fake_check_output
        ):
            common.live_pid_snapshot("agent")
            first = common.process_command_line(99)
            second = common.process_command_line(99)
            common.live_pid_snapshot("agent")
            third = common.process_command_line(99)

        self.assertEqual(first, "agent --resume abc")
        self.assertEqual(second, first)
        self.assertEqual(third, first)
        self.assertEqual(len(calls), 2)
        common.clear_live_cwd_cache()


class DeleteSessionScanTests(unittest.TestCase):
    """各运行时 scan.delete_session：验证目标会话被彻底抹掉，且不影响其他会话。

    各运行时存储形态不同（单文件 / 每会话目录 / 共享 SQLite），覆盖每一种，
    重点断言「删对了、没删多」——尤其是 OpenCode 共享库，误删会波及其他会话。
    """

    def test_claude_delete_session_removes_only_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target.jsonl"
            other = Path(td) / "other.jsonl"
            target.write_text('{"type":"user"}\n', encoding="utf-8")
            other.write_text('{"type":"user"}\n', encoding="utf-8")

            scan_claude.delete_session(str(target))

            self.assertFalse(target.exists())
            self.assertTrue(other.exists())

    def test_claude_delete_session_missing_file_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = str(Path(td) / "does-not-exist.jsonl")
            scan_claude.delete_session(missing)  # 不应抛异常

    def test_codex_delete_session_removes_only_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "rollout-2026-01-01T00-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
            other = Path(td) / "rollout-2026-01-02T00-00-00-11111111-2222-3333-4444-555555555555.jsonl"
            target.write_text("{}\n", encoding="utf-8")
            other.write_text("{}\n", encoding="utf-8")

            scan_codex.delete_session(str(target))

            self.assertFalse(target.exists())
            self.assertTrue(other.exists())

    def test_pi_delete_session_removes_only_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "2026-01-01T00-00-00-000Z_target.jsonl"
            other = Path(td) / "2026-01-01T00-00-01-000Z_other.jsonl"
            target.write_text('{"type":"session"}\n', encoding="utf-8")
            other.write_text('{"type":"session"}\n', encoding="utf-8")

            scan_pi.delete_session(str(target))

            self.assertFalse(target.exists())
            self.assertTrue(other.exists())

    def test_kimi_delete_session_removes_whole_session_dir_not_just_wire_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sessions_dir = Path(td) / "sessions"
            target_dir = _make_kimi_session(
                sessions_dir, "wd1", "session_target",
                {"workDir": "/tmp", "title": "目标会话"},
                [_kimi_user_event("你好", 1000)],
            )
            other_dir = _make_kimi_session(
                sessions_dir, "wd1", "session_other",
                {"workDir": "/tmp", "title": "其他会话"},
                [_kimi_user_event("在吗", 1000)],
            )
            wire_path = str(target_dir / "agents" / "main" / "wire.jsonl")

            scan_kimi.delete_session(wire_path)

            self.assertFalse(target_dir.exists())  # 整个会话目录（state.json 等）都要没了
            self.assertTrue(other_dir.exists())
            self.assertTrue((other_dir / "state.json").exists())

    def test_cursor_delete_session_removes_whole_chat_dir(self) -> None:
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target_dir = root / "ws1" / "target-chat"
            other_dir = root / "ws1" / "other-chat"
            target_dir.mkdir(parents=True)
            other_dir.mkdir(parents=True)
            (target_dir / "meta.json").write_text("{}", encoding="utf-8")
            (target_dir / "store.db").write_text("", encoding="utf-8")
            (other_dir / "meta.json").write_text("{}", encoding="utf-8")

            # path 是 store.db（存在时的常见情况），delete_session 要能反推出会话目录。
            scan_cursor.delete_session(str(target_dir / "store.db"))

            self.assertFalse(target_dir.exists())
            self.assertTrue(other_dir.exists())

    def test_cursor_delete_session_accepts_dir_path_when_no_store_db(self) -> None:
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            target_dir = Path(td) / "ws1" / "target-chat"
            target_dir.mkdir(parents=True)
            (target_dir / "meta.json").write_text("{}", encoding="utf-8")

            scan_cursor.delete_session(str(target_dir))

            self.assertFalse(target_dir.exists())

    def test_opencode_delete_session_removes_only_target_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_opencode_db(
                db_path,
                sessions=[
                    {"id": "ses_target", "directory": "/tmp", "title": "目标会话",
                     "time_created": 1000, "time_updated": 1000},
                    {"id": "ses_other", "directory": "/tmp", "title": "其他会话",
                     "time_created": 2000, "time_updated": 2000},
                ],
                messages=[
                    {"id": "msg_target", "session_id": "ses_target", "time_created": 1000,
                     "data": {"role": "user"}},
                    {"id": "msg_other", "session_id": "ses_other", "time_created": 2000,
                     "data": {"role": "user"}},
                ],
                parts=[
                    {"id": "part_target", "message_id": "msg_target", "session_id": "ses_target",
                     "time_created": 1000, "data": {"type": "text", "text": "目标正文"}},
                    {"id": "part_other", "message_id": "msg_other", "session_id": "ses_other",
                     "time_created": 2000, "data": {"type": "text", "text": "其他正文"}},
                ],
            )

            scan_opencode.delete_session(str(db_path), "ses_target")

            conn = sqlite3.connect(str(db_path))
            try:
                session_ids = {row[0] for row in conn.execute("SELECT id FROM session")}
                message_ids = {row[0] for row in conn.execute("SELECT id FROM message")}
                part_ids = {row[0] for row in conn.execute("SELECT id FROM part")}
            finally:
                conn.close()

            self.assertEqual(session_ids, {"ses_other"})
            self.assertEqual(message_ids, {"msg_other"})
            self.assertEqual(part_ids, {"part_other"})

            # 其他会话仍能正常扫描出来，证明库本身没有被破坏。
            with mock.patch.object(scan_opencode, "_db_paths", return_value=[str(db_path)]), \
                 mock.patch.object(scan_opencode, "live_processes", return_value=[]):
                remaining = scan_opencode.scan_sessions(limit=10)
            self.assertEqual([s["id"] for s in remaining], ["ses_other"])


class PiScanTests(unittest.TestCase):
    def setUp(self) -> None:
        scan_pi.reset_live_session_overrides()

    def tearDown(self) -> None:
        scan_pi.reset_live_session_overrides()

    def test_scan_and_preview_only_follow_current_branch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            session = Path(td) / "2026-01-01T00-00-00-000Z_demo.jsonl"
            _write_jsonl(session, [
                {"type": "session", "id": "pi-demo", "timestamp": "2026-01-01T00:00:00Z", "cwd": td},
                {
                    "type": "session_info", "id": "meta", "parentId": None,
                    "timestamp": "2026-01-01T00:00:01Z", "name": "Pi 标题",
                },
                {
                    "type": "message", "id": "u1", "parentId": "meta",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "首个需求"}]},
                },
                {
                    "type": "message", "id": "old", "parentId": "u1",
                    "timestamp": "2026-01-01T00:00:03Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "旧分支"}]},
                },
                {
                    "type": "message", "id": "new", "parentId": "u1",
                    "timestamp": "2026-01-01T00:00:04Z",
                    "message": {"role": "assistant", "content": [
                        {"type": "thinking", "thinking": "忽略"}, {"type": "text", "text": "当前分支"},
                    ]},
                },
            ])
            with mock.patch.object(scan_pi, "SESSIONS_DIR", td):
                sessions = scan_pi.scan_sessions(limit=10)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["id"], "pi-demo")
            self.assertEqual(sessions[0]["native_title"], "Pi 标题")
            self.assertEqual(sessions[0]["last_agent_msg"], "当前分支")
            self.assertEqual([item.text for item in scan_pi.load_conversation(str(session))], ["首个需求", "当前分支"])

    def test_v1_session_without_parent_ids_still_lists_and_previews(self) -> None:
        """Pi v1 jsonl 没有 id/parentId；不得当成空会话丢掉。"""
        with tempfile.TemporaryDirectory() as td:
            session = Path(td) / "2026-01-01T00-00-00-000Z_legacy.jsonl"
            _write_jsonl(session, [
                {
                    "type": "session", "id": "legacy-v1", "version": 1,
                    "timestamp": "2026-01-01T00:00:00Z", "cwd": td,
                },
                {
                    "type": "message",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "message": {"role": "user", "content": "旧版提问"},
                },
                {
                    "type": "message",
                    "timestamp": "2026-01-01T00:00:03Z",
                    "message": {"role": "assistant", "content": [
                        {"type": "text", "text": "旧版回复"},
                    ]},
                },
            ])
            with mock.patch.object(scan_pi, "SESSIONS_DIR", td):
                sessions = scan_pi.scan_sessions(limit=10)
            self.assertEqual([item["id"] for item in sessions], ["legacy-v1"])
            self.assertEqual(sessions[0]["first_user_msg"], "旧版提问")
            self.assertEqual(sessions[0]["last_agent_msg"], "旧版回复")
            self.assertEqual(
                [item.text for item in scan_pi.load_conversation(str(session))],
                ["旧版提问", "旧版回复"],
            )

    def test_isolation_dir_sessions_do_not_starve_heap_history(self) -> None:
        """托管隔离目录的新 jsonl 不得占满 limit，把默认堆里的历史挤掉。"""
        with tempfile.TemporaryDirectory() as td:
            heap_dir = Path(td) / "--proj--"
            old_ids = [f"old{index}" for index in range(4)]
            for index, session_id in enumerate(old_ids):
                path = heap_dir / f"2026-01-0{index + 1}T00-00-00-000Z_{session_id}.jsonl"
                _write_jsonl(path, [
                    {
                        "type": "session", "id": session_id,
                        "timestamp": f"2026-01-0{index + 1}T00:00:00Z", "cwd": "/proj",
                    },
                    {
                        "type": "message", "id": f"u-{session_id}", "parentId": None,
                        "timestamp": f"2026-01-0{index + 1}T00:00:01Z",
                        "message": {"role": "user", "content": [{"type": "text", "text": f"历史{index}"}]},
                    },
                ])
                os.utime(path, (1_000_000 + index, 1_000_000 + index))
            new_ids = [f"new{index}" for index in range(4)]
            for index, session_id in enumerate(new_ids):
                path = (
                    heap_dir / f"corral-aaa{index}"
                    / f"2026-08-2{index}T00-00-00-000Z_{session_id}.jsonl"
                )
                _write_jsonl(path, [
                    {
                        "type": "session", "id": session_id,
                        "timestamp": f"2026-08-2{index}T00:00:00Z", "cwd": "/proj",
                    },
                    {
                        "type": "message", "id": f"u-{session_id}", "parentId": None,
                        "timestamp": f"2026-08-2{index}T00:00:01Z",
                        "message": {"role": "user", "content": [{"type": "text", "text": f"隔离{index}"}]},
                    },
                ])
                os.utime(path, (2_000_000 + index, 2_000_000 + index))
            with mock.patch.object(scan_pi, "SESSIONS_DIR", td):
                sessions = scan_pi.scan_sessions(limit=3, host=corral_host_extension())
            ids = {item["id"] for item in sessions}
            self.assertTrue({"old1", "old2", "old3"}.issubset(ids), ids)
            self.assertTrue({"new1", "new2", "new3"}.issubset(ids), ids)
            self.assertNotIn("old0", ids)
            self.assertNotIn("new0", ids)

    def test_keep_ids_survive_scan_limit(self) -> None:
        """置顶/分组记住的 Pi 会话即使落在 mtime 配额外也必须扫回来。"""
        with tempfile.TemporaryDirectory() as td:
            heap_dir = Path(td) / "--proj--"
            for index, session_id in enumerate(["old0", "old1", "old2", "old3"]):
                path = heap_dir / f"2026-01-0{index + 1}T00-00-00-000Z_{session_id}.jsonl"
                _write_jsonl(path, [
                    {
                        "type": "session", "id": session_id,
                        "timestamp": f"2026-01-0{index + 1}T00:00:00Z", "cwd": "/proj",
                    },
                    {
                        "type": "message", "id": f"u-{session_id}", "parentId": None,
                        "timestamp": f"2026-01-0{index + 1}T00:00:01Z",
                        "message": {"role": "user", "content": [{"type": "text", "text": f"历史{index}"}]},
                    },
                ])
                os.utime(path, (1_000_000 + index, 1_000_000 + index))
            with mock.patch.object(scan_pi, "SESSIONS_DIR", td):
                sessions = scan_pi.scan_sessions(limit=2, keep_ids={"old0"})
            ids = [item["id"] for item in sessions]
            self.assertIn("old0", ids)
            self.assertIn("old3", ids)
            self.assertIn("old2", ids)
            self.assertNotIn("old1", ids)

    def test_live_flags_bind_session_flag_and_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd = str(Path(td) / "proj")
            Path(cwd).mkdir()
            resumed_id = "019ffa0b-6679-7e5e-bfd9-1615e07cf643"
            hosted_id = "abcd1234"
            resumed = Path(td) / f"2026-08-13T07-34-38-969Z_{resumed_id}.jsonl"
            hosted = Path(td) / f"2026-08-13T07-35-00-000Z_{hosted_id}.jsonl"
            _write_jsonl(resumed, [
                {"type": "session", "id": resumed_id, "timestamp": "2026-08-13T07:34:38Z", "cwd": cwd},
                {
                    "type": "message", "id": "u1", "parentId": None,
                    "timestamp": "2026-08-13T07:34:39Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "恢复"}]},
                },
            ])
            _write_jsonl(hosted, [
                {"type": "session", "id": hosted_id, "timestamp": "2026-08-13T07:35:00Z", "cwd": cwd},
                {
                    "type": "message", "id": "u2", "parentId": None,
                    "timestamp": "2026-08-13T07:35:01Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "新建"}]},
                },
            ])
            real_cwd = os.path.realpath(cwd)
            cmdlines = {
                11: f"node /opt/pi-coding-agent/dist/cli.js --approve --session {resumed}",
                22: "pi --approve --session-id abcd1234",
            }

            def fake_cmdline(pid: int) -> str:
                return cmdlines[pid]

            with mock.patch.object(scan_pi, "SESSIONS_DIR", td), mock.patch.object(
                scan_pi, "live_processes", return_value=[(11, real_cwd), (22, real_cwd)]
            ), mock.patch.object(
                scan_pi, "process_command_line", side_effect=fake_cmdline
            ), mock.patch.object(
                scan_pi, "open_file_paths", return_value={11: [], 22: []}
            ), mock.patch.object(
                scan_pi, "process_environ", return_value={}
            ):
                sessions = scan_pi.scan_sessions(limit=10)
            by_id = {item["id"]: item for item in sessions}
            self.assertTrue(by_id[resumed_id]["live"])
            self.assertEqual(by_id[resumed_id]["pid"], 11)
            self.assertTrue(by_id[hosted_id]["live"])
            self.assertEqual(by_id[hosted_id]["pid"], 22)

    def test_live_flags_open_jsonl_wins_over_hosted_session_id(self) -> None:
        """进程内 /new 换了 jsonl 后，不能继续把 live 钉在启动时的 --session-id 上。"""
        with tempfile.TemporaryDirectory() as td:
            cwd = str(Path(td) / "proj")
            Path(cwd).mkdir()
            hosted_id = "74cd9122"
            switched_id = "01a00dcc-d48e-7e7b-a7ad-1f524a3e13db"
            self._write_pi_session(
                Path(td), hosted_id, cwd, "2026-08-17T02:49:20.902Z", "开发机CPU内存优化",
            )
            switched = self._write_pi_session(
                Path(td), switched_id, cwd, "2026-08-17T03:38:42.702Z", "多Agent并行防重复发布",
            )
            sessions = self._scan_with_live(
                td,
                processes=[(1988097, os.path.realpath(cwd))],
                cmdlines={1988097: f"pi --approve --session-id {hosted_id}"},
                environ={1988097: {"CORRAL_SESSION_ID": hosted_id}},
                open_paths={1988097: [str(switched)]},
            )
            by_id = {item["id"]: item for item in sessions}
            self.assertTrue(by_id[switched_id]["live"])
            self.assertEqual(by_id[switched_id]["pid"], 1988097)
            self.assertFalse(by_id[hosted_id]["live"])
            self.assertIsNone(by_id[hosted_id]["pid"])

    def test_live_flags_remember_in_process_session_switch_after_jsonl_closes(self) -> None:
        """Pi 写完即关 jsonl；TUI 必须记住 /new 之后的当前会话，不能弹回旧 ident。"""
        with tempfile.TemporaryDirectory() as td:
            cwd = str(Path(td) / "proj")
            Path(cwd).mkdir()
            hosted_id = "74cd9122"
            switched_id = "01a00dcc-d48e-7e7b-a7ad-1f524a3e13db"
            self._write_pi_session(
                Path(td), hosted_id, cwd, "2026-08-17T02:49:20.902Z", "开发机CPU内存优化",
            )
            switched = self._write_pi_session(
                Path(td), switched_id, cwd, "2026-08-17T03:38:42.702Z", "多Agent并行防重复发布",
            )
            first = self._scan_with_live(
                td,
                processes=[(1988097, os.path.realpath(cwd))],
                cmdlines={1988097: "pi"},
                environ={1988097: {"CORRAL_SESSION_ID": hosted_id}},
                open_paths={1988097: [str(switched)]},
            )
            by_id = {item["id"]: item for item in first}
            self.assertTrue(by_id[switched_id]["live"])
            self.assertFalse(by_id[hosted_id]["live"])
            second = self._scan_with_live(
                td,
                processes=[(1988097, os.path.realpath(cwd))],
                cmdlines={1988097: "pi"},
                environ={1988097: {"CORRAL_SESSION_ID": hosted_id}},
                open_paths={1988097: []},
            )
            by_id = {item["id"]: item for item in second}
            self.assertTrue(by_id[switched_id]["live"])
            self.assertEqual(by_id[switched_id]["pid"], 1988097)
            self.assertFalse(by_id[hosted_id]["live"])

    def test_live_flags_do_not_bind_short_env_to_uuid_history(self) -> None:
        """8 位托管 ident 不得拿去前缀碰 uuidv7 历史，否则同分屏两格会串台。"""
        with tempfile.TemporaryDirectory() as td:
            cwd = str(Path(td) / "proj")
            Path(cwd).mkdir()
            history_id = "019ffa0b-6679-7e5e-bfd9-1615e07cf643"
            path = Path(td) / f"2026-08-13T07-34-38-969Z_{history_id}.jsonl"
            _write_jsonl(path, [
                {"type": "session", "id": history_id, "timestamp": "2026-08-13T07:34:38Z", "cwd": cwd},
                {
                    "type": "message", "id": "u1", "parentId": None,
                    "timestamp": "2026-08-13T07:34:39Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "旧会话"}]},
                },
            ])
            with mock.patch.object(scan_pi, "SESSIONS_DIR", td), mock.patch.object(
                scan_pi, "live_processes", return_value=[(33, os.path.realpath(cwd))]
            ), mock.patch.object(
                scan_pi, "process_command_line", return_value="pi --approve"
            ), mock.patch.object(
                scan_pi, "open_file_paths", return_value={33: []}
            ), mock.patch.object(
                scan_pi, "process_environ",
                return_value={"CORRAL_SESSION_ID": "abcd1234"},
            ):
                sessions = scan_pi.scan_sessions(limit=10)
            self.assertEqual(len(sessions), 1)
            self.assertFalse(sessions[0]["live"])
            self.assertIsNone(sessions[0]["pid"])

    def _scan_with_live(
        self,
        sessions_dir: str,
        *,
        processes: list[tuple[int, str]],
        cmdlines: dict[int, str] | None = None,
        environ: dict[int, dict[str, str]] | None = None,
        starts: dict[int, float | None] | None = None,
        open_paths: dict[int, list[str]] | None = None,
        limit: int = 10,
        host=None,
        live_map_dir: str | None = None,
    ) -> list:
        cmdlines = cmdlines or {}
        environ = environ or {}
        starts = starts or {}
        open_paths = open_paths or {}
        with mock.patch.object(scan_pi, "SESSIONS_DIR", sessions_dir), mock.patch.object(
            scan_pi, "live_processes", return_value=processes
        ), mock.patch.object(
            scan_pi, "process_command_line",
            side_effect=lambda pid: cmdlines.get(pid, "pi --approve"),
        ), mock.patch.object(
            scan_pi, "process_environ",
            side_effect=lambda pid, **kwargs: environ.get(pid, {}),
        ), mock.patch.object(
            scan_pi, "process_start_time",
            side_effect=lambda pid: starts.get(pid),
        ), mock.patch.object(
            scan_pi, "open_file_paths",
            return_value=open_paths,
        ):
            if live_map_dir is None and host is None:
                return scan_pi.scan_sessions(limit=limit)
            import dataclasses

            base = host if host is not None else corral_host_extension()
            if live_map_dir is not None:
                base = dataclasses.replace(base, pi_live_map_dir=str(live_map_dir))
            return scan_pi.scan_sessions(limit=limit, host=base)

    def _write_pi_session(
        self, directory: Path, session_id: str, cwd: str, created: str, text: str,
    ) -> Path:
        stamp = created.replace(":", "-").replace(".", "-")
        path = directory / f"{stamp}_{session_id}.jsonl"
        _write_jsonl(path, [
            {"type": "session", "id": session_id, "timestamp": created, "cwd": cwd},
            {
                "type": "message", "id": f"{session_id}-u1", "parentId": session_id,
                "timestamp": created,
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            },
        ])
        return path

    def test_live_flags_same_cwd_two_tuis_bind_each_created_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            first_created = "2023-11-14T22:15:00.000Z"
            second_created = "2023-11-14T22:16:40.000Z"
            self._write_pi_session(sessions_dir, "pi-first", cwd, first_created, "先开")
            self._write_pi_session(sessions_dir, "pi-second", cwd, second_created, "后开")
            first_ts = datetime(2023, 11, 14, 22, 15, tzinfo=timezone.utc).timestamp()
            second_ts = datetime(2023, 11, 14, 22, 16, 40, tzinfo=timezone.utc).timestamp()
            sessions = self._scan_with_live(
                str(sessions_dir),
                processes=[(11, cwd), (22, cwd)],
                cmdlines={11: "pi --approve", 22: "pi --approve"},
                starts={11: first_ts - 10, 22: second_ts - 10},
            )
            by_id = {item["id"]: item for item in sessions}
            self.assertTrue(by_id["pi-first"]["live"])
            self.assertEqual(by_id["pi-first"]["pid"], 11)
            self.assertTrue(by_id["pi-second"]["live"])
            self.assertEqual(by_id["pi-second"]["pid"], 22)

    def test_live_flags_do_not_bind_new_tui_to_older_cwd_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            created = "2023-11-14T22:13:20.000Z"
            self._write_pi_session(sessions_dir, "pi-old", cwd, created, "旧问题")
            created_ts = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc).timestamp()
            sessions = self._scan_with_live(
                str(sessions_dir),
                processes=[(99, cwd)],
                cmdlines={99: "pi --approve"},
                starts={99: created_ts + 400},
            )
            self.assertFalse(sessions[0]["live"])

    def test_live_flags_continue_binds_newest_unmarked_in_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            older = self._write_pi_session(
                sessions_dir, "pi-older", cwd, "2023-11-14T22:00:00.000Z", "更早",
            )
            newer = self._write_pi_session(
                sessions_dir, "pi-newer", cwd, "2023-11-14T22:10:00.000Z", "更新",
            )
            os.utime(older, (1_700_000_000, 1_700_000_000))
            os.utime(newer, (1_700_000_600, 1_700_000_600))
            sessions = self._scan_with_live(
                str(sessions_dir),
                processes=[(44, cwd)],
                cmdlines={44: "pi --approve -c"},
            )
            by_id = {item["id"]: item for item in sessions}
            self.assertTrue(by_id["pi-newer"]["live"])
            self.assertEqual(by_id["pi-newer"]["pid"], 44)
            self.assertFalse(by_id["pi-older"]["live"])

    def test_live_flags_reject_override_pointing_to_session_created_before_process(self) -> None:
        """磁盘/内存里写坏的「pid -> 会话」记忆：目标会话比进程还早创建，不可能是
        进程内 /new 切换的结果，必须剔掉并回落到启动旗标/环境变量的硬证据。
        真实事故：同项目两条分屏，B 进程的记忆被写成 A 会话，处理顺序颠倒时
        A 抢走 B 的 pid、B 丢 live，两个分屏串台显示同一份终端。
        """
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            first_created = "2023-11-14T22:15:00.000Z"
            second_created = "2023-11-14T22:16:40.000Z"
            self._write_pi_session(sessions_dir, "pi-first", cwd, first_created, "读取Claude历史")
            self._write_pi_session(sessions_dir, "pi-second", cwd, second_created, "快速MVP")
            first_ts = datetime(2023, 11, 14, 22, 15, 0, tzinfo=timezone.utc).timestamp()
            second_ts = datetime(2023, 11, 14, 22, 16, 40, tzinfo=timezone.utc).timestamp()
            scan_pi._pid_session_override[22] = "pi-first"  # 坏记忆：目标比进程早 100 秒创建
            sessions = self._scan_with_live(
                str(sessions_dir),
                processes=[(22, cwd), (11, cwd)],  # 坏记忆的 pid 先处理，才能暴露优先级问题
                cmdlines={11: "pi --approve --session-id pi-first", 22: "pi --approve --session-id pi-second"},
                environ={11: {"CORRAL_SESSION_ID": "pi-first"}, 22: {"CORRAL_SESSION_ID": "pi-second"}},
                starts={11: first_ts - 10, 22: second_ts - 10},
            )
            by_id = {item["id"]: item for item in sessions}
            self.assertEqual(by_id["pi-first"]["pid"], 11)
            self.assertEqual(by_id["pi-second"]["pid"], 22)
            self.assertNotIn(22, scan_pi._pid_session_override)

    def test_disk_live_map_prunes_impossible_override(self) -> None:
        """pi-live-pids.json 里已落盘的坏记忆，重新加载时必须剔除并回写磁盘，
        否则每次重启 corral 都会再次串台。
        """
        with tempfile.TemporaryDirectory() as cache_dir, tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            first_created = "2023-11-14T22:15:00.000Z"
            second_created = "2023-11-14T22:16:40.000Z"
            self._write_pi_session(sessions_dir, "pi-first", cwd, first_created, "读取Claude历史")
            self._write_pi_session(sessions_dir, "pi-second", cwd, second_created, "快速MVP")
            first_ts = datetime(2023, 11, 14, 22, 15, 0, tzinfo=timezone.utc).timestamp()
            second_ts = datetime(2023, 11, 14, 22, 16, 40, tzinfo=timezone.utc).timestamp()
            with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": cache_dir}):
                live_map = os.path.join(cache_dir, "pi-live-pids.json")
                scan_pi._write_live_map({scan_pi._persist_key(22, second_ts - 10): "pi-first"}, live_map)
                self._scan_with_live(
                    str(sessions_dir),
                    processes=[(22, cwd), (11, cwd)],
                    cmdlines={11: "pi --approve --session-id pi-first", 22: "pi --approve --session-id pi-second"},
                    environ={11: {"CORRAL_SESSION_ID": "pi-first"}, 22: {"CORRAL_SESSION_ID": "pi-second"}},
                    starts={11: first_ts - 10, 22: second_ts - 10},
                    host=corral_host_extension(),
                )
                self.assertEqual(scan_pi._read_live_map(live_map), {})
                # 合法记忆（目标在进程启动后创建，/new 场景）必须保留。
                good = {scan_pi._persist_key(11, first_ts - 10): "pi-second"}
                scan_pi._write_live_map(good, live_map)
                scan_pi.reset_live_session_overrides()
                sessions = self._scan_with_live(
                    str(sessions_dir),
                    processes=[(11, cwd), (22, cwd)],
                    cmdlines={11: "pi --approve --session-id pi-first", 22: "pi --approve --session-id pi-second"},
                    environ={11: {"CORRAL_SESSION_ID": "pi-first"}, 22: {"CORRAL_SESSION_ID": "pi-second"}},
                    starts={11: first_ts - 10, 22: second_ts - 10},
                    host=corral_host_extension(),
                )
                by_id = {item["id"]: item for item in sessions}
                self.assertEqual(by_id["pi-second"]["pid"], 11)  # /new 后的记忆胜过日 ident
                self.assertEqual(scan_pi._read_live_map(live_map), good)

    def test_follow_switch_correlation_does_not_steal_bound_session(self) -> None:
        """写字节增长与文件 mtime 增长是两次独立采样：同目录两条会话同时活跃时
        可能错位对上，不能把已绑在别的活进程上的会话抢过来。
        """
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            first_created = "2023-11-14T22:15:00.000Z"
            second_created = "2023-11-14T22:16:40.000Z"
            first_path = self._write_pi_session(sessions_dir, "pi-first", cwd, first_created, "A")
            self._write_pi_session(sessions_dir, "pi-second", cwd, second_created, "B")
            first_ts = datetime(2023, 11, 14, 22, 15, 0, tzinfo=timezone.utc).timestamp()
            starts = {11: first_ts - 10, 22: first_ts - 5}
            base_env = {
                "cmdlines": {11: "pi --approve --session-id pi-first", 22: "pi --approve --session-id pi-second"},
                "environ": {11: {"CORRAL_SESSION_ID": "pi-first"}, 22: {"CORRAL_SESSION_ID": "pi-second"}},
            }
            # 预热轮：记住两份采样基线（写字节/mtime），保证下一轮错位信号能触发。
            os.utime(first_path, (time.time() + 50, time.time() + 50))
            with mock.patch.object(scan_pi, "_pid_write_bytes", side_effect=lambda pid: {11: 100, 22: 100}[pid]), \
                 mock.patch.object(scan_pi, "_pid_cpu_ticks", side_effect=lambda pid: {11: 5, 22: 5}[pid]):
                scan1 = self._scan_with_live(
                    str(sessions_dir), processes=[(11, cwd), (22, cwd)],
                    cmdlines=base_env["cmdlines"], environ=base_env["environ"], starts=starts,
                )
            by_id = {item["id"]: item for item in scan1}
            self.assertEqual(by_id["pi-first"]["pid"], 11)
            self.assertEqual(by_id["pi-second"]["pid"], 22)
            # 第二轮：只有 A 的文件在长，写字节却长在 22 身上（采样错位）。
            os.utime(first_path, (time.time() + 200, time.time() + 200))
            with mock.patch.object(scan_pi, "_pid_write_bytes", side_effect=lambda pid: {11: 100, 22: 200}[pid]), \
                 mock.patch.object(scan_pi, "_pid_cpu_ticks", side_effect=lambda pid: {11: 5, 22: 5}[pid]):
                scan2 = self._scan_with_live(
                    str(sessions_dir), processes=[(11, cwd), (22, cwd)],
                    cmdlines=base_env["cmdlines"], environ=base_env["environ"], starts=starts,
                )
            by_id = {item["id"]: item for item in scan2}
            self.assertEqual(by_id["pi-first"]["pid"], 11, "A 不能被 22 抢走")
            self.assertEqual(by_id["pi-second"]["pid"], 22)

    def test_follow_idle_claim_yields_to_free_process_in_same_cwd(self) -> None:
        """同目录裸 pi 新开的会话不能被空闲的托管进程认领成自己的 /new 结果。

        真实事故：corral 托管的 Pi 会话 A 空闲者，用户在另一终端裸 `pi` 开了会
        话 B；空闲认领把 B 抢给 A 的进程并写入持久记忆，随后 annotate 把 A 那
        格的托管名贴到 B 头上，右栏 A 格的 Your prompts 小窗串台成 B 的提问。
        """
        with tempfile.TemporaryDirectory() as cache_dir, tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            self._write_pi_session(sessions_dir, "aaaa1111", cwd, "2023-11-14T22:15:00.000Z", "A 的提问")
            self._write_pi_session(sessions_dir, "bbbb2222", cwd, "2023-11-14T22:40:00.000Z", "B 的提问")
            hosted_start = datetime(2023, 11, 14, 22, 14, 50, tzinfo=timezone.utc).timestamp()
            free_start = datetime(2023, 11, 14, 22, 39, 50, tzinfo=timezone.utc).timestamp()
            with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": cache_dir}):
                sessions = self._scan_with_live(
                    str(sessions_dir),
                    processes=[(11, cwd), (22, cwd)],
                    cmdlines={11: "pi --approve --session-id aaaa1111", 22: "pi"},
                    starts={11: hosted_start, 22: free_start},
                    host=corral_host_extension(),
                )
                by_id = {item["id"]: item for item in sessions}
                self.assertTrue(by_id["aaaa1111"]["live"])
                self.assertEqual(by_id["aaaa1111"]["pid"], 11)
                self.assertTrue(by_id["bbbb2222"]["live"])
                self.assertEqual(by_id["bbbb2222"]["pid"], 22)
                self.assertNotIn(11, scan_pi._pid_session_override)

    def test_follow_idle_claim_still_works_without_free_process(self) -> None:
        """控制组：同目录没有裸进程时，/new 后的新会话仍要被空闲认领跟上。"""
        with tempfile.TemporaryDirectory() as cache_dir, tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            self._write_pi_session(sessions_dir, "aaaa1111", cwd, "2023-11-14T22:15:00.000Z", "旧会话提问")
            self._write_pi_session(sessions_dir, "01a0new1", cwd, "2023-11-14T22:40:00.000Z", "/new 后提问")
            started = datetime(2023, 11, 14, 22, 14, 50, tzinfo=timezone.utc).timestamp()
            with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": cache_dir}):
                sessions = self._scan_with_live(
                    str(sessions_dir),
                    processes=[(11, cwd)],
                    cmdlines={11: "pi --approve --session-id aaaa1111"},
                    starts={11: started},
                )
                by_id = {item["id"]: item for item in sessions}
                self.assertTrue(by_id["01a0new1"]["live"])
                self.assertEqual(by_id["01a0new1"]["pid"], 11)
                self.assertFalse(by_id["aaaa1111"]["live"])
                self.assertEqual(scan_pi._pid_session_override.get(11), "01a0new1")

    def test_poisoned_override_yields_to_free_owner_with_hard_evidence(self) -> None:
        """被污染的 pid 记忆（含磁盘）在有硬启动证据且目标另有属主时必须让位。

        否则升级修复后，已经写进 pi-live-pids.json 的坏记忆会每轮重演串台。
        """
        with tempfile.TemporaryDirectory() as cache_dir, tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            self._write_pi_session(sessions_dir, "aaaa1111", cwd, "2023-11-14T22:15:00.000Z", "A 的提问")
            self._write_pi_session(sessions_dir, "bbbb2222", cwd, "2023-11-14T22:40:00.000Z", "B 的提问")
            hosted_start = datetime(2023, 11, 14, 22, 14, 50, tzinfo=timezone.utc).timestamp()
            free_start = datetime(2023, 11, 14, 22, 39, 50, tzinfo=timezone.utc).timestamp()
            with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": cache_dir}):
                live_map = os.path.join(cache_dir, "pi-live-pids.json")
                scan_pi._write_live_map({scan_pi._persist_key(11, hosted_start): "bbbb2222"}, live_map)
                sessions = self._scan_with_live(
                    str(sessions_dir),
                    processes=[(11, cwd), (22, cwd)],
                    cmdlines={11: "pi --approve --session-id aaaa1111", 22: "pi"},
                    starts={11: hosted_start, 22: free_start},
                    host=corral_host_extension(),
                )
                by_id = {item["id"]: item for item in sessions}
                self.assertEqual(by_id["aaaa1111"].get("pid"), 11, "硬证据应胜过被污染的记忆")
                self.assertEqual(by_id["bbbb2222"].get("pid"), 22)
                self.assertEqual(scan_pi._read_live_map(live_map), {}, "磁盘上的坏记忆也要一并剔掉")

    def test_encode_pi_session_cwd_and_isolation_dir(self) -> None:
        from corral.runtime.host_extension import hosted_session_dir, is_isolation_dir

        with tempfile.TemporaryDirectory() as td:
            encoded = scan_pi.encode_pi_session_cwd(td)
            stripped = os.path.realpath(td).lstrip("/\\")
            safe = stripped.replace("/", "-").replace("\\", "-").replace(":", "-")
            self.assertEqual(encoded, f"--{safe}--")
            hosted = hosted_session_dir(td, "abcd1234")
            self.assertTrue(hosted.endswith(os.path.join(encoded, "corral-abcd1234")))
            self.assertTrue(is_isolation_dir(hosted))
            self.assertFalse(is_isolation_dir(td))

    def test_live_flags_session_dir_isolates_two_hosted_panes(self) -> None:
        """同 cwd 两个托管 pane，cmdline 都是 pi，只靠隔离目录绑各自 jsonl。"""
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            dir_a = sessions_dir / "corral-aaaa1111"
            dir_b = sessions_dir / "corral-bbbb2222"
            dir_a.mkdir(parents=True)
            dir_b.mkdir()
            self._write_pi_session(dir_a, "sess-a", cwd, "2023-11-14T22:15:00.000Z", "A 的提问")
            self._write_pi_session(dir_b, "sess-b", cwd, "2023-11-14T22:16:00.000Z", "B 的提问")
            sessions = self._scan_with_live(
                str(sessions_dir),
                processes=[(11, cwd), (22, cwd)],
                cmdlines={11: "pi", 22: "pi"},
                environ={
                    11: {scan_pi.PI_SESSION_DIR_ENV: os.path.realpath(str(dir_a))},
                    22: {scan_pi.PI_SESSION_DIR_ENV: os.path.realpath(str(dir_b))},
                },
                host=corral_host_extension(),
            )
            by_id = {item["id"]: item for item in sessions}
            self.assertEqual(by_id["sess-a"]["pid"], 11)
            self.assertEqual(by_id["sess-b"]["pid"], 22)

    def test_live_flags_session_dir_host_does_not_claim_default_dir_newcomer(self) -> None:
        """隔离目录里的空闲托管进程不得把默认堆里裸 pi 的新文件认成自己的 /new。"""
        with tempfile.TemporaryDirectory() as cache_dir, tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            dir_a = sessions_dir / "corral-aaaa1111"
            dir_a.mkdir(parents=True)
            self._write_pi_session(dir_a, "aaaa1111", cwd, "2023-11-14T22:15:00.000Z", "A 的提问")
            self._write_pi_session(sessions_dir, "bbbb2222", cwd, "2023-11-14T22:40:00.000Z", "B 的提问")
            hosted_start = datetime(2023, 11, 14, 22, 14, 50, tzinfo=timezone.utc).timestamp()
            free_start = datetime(2023, 11, 14, 22, 39, 50, tzinfo=timezone.utc).timestamp()
            with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": cache_dir}):
                sessions = self._scan_with_live(
                    str(sessions_dir),
                    processes=[(11, cwd), (22, cwd)],
                    cmdlines={11: "pi", 22: "pi"},
                    environ={
                        11: {
                            scan_pi.PI_SESSION_DIR_ENV: os.path.realpath(str(dir_a)),
                            "CORRAL_SESSION_ID": "aaaa1111",
                        },
                    },
                    starts={11: hosted_start, 22: free_start},
                )
                by_id = {item["id"]: item for item in sessions}
                self.assertEqual(by_id["aaaa1111"]["pid"], 11)
                self.assertEqual(by_id["bbbb2222"]["pid"], 22)

    def test_live_flags_empty_session_dir_does_not_pair_default_dir_file(self) -> None:
        """隔离目录还空时也要把进程钉住，禁止回落到 cwd 配对抢走默认堆新文件。"""
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            dir_a = sessions_dir / "corral-aaaa1111"
            dir_a.mkdir(parents=True)
            self._write_pi_session(sessions_dir, "bbbb2222", cwd, "2023-11-14T22:40:00.000Z", "B 的提问")
            sessions = self._scan_with_live(
                str(sessions_dir),
                processes=[(11, cwd)],
                cmdlines={11: "pi"},
                environ={11: {scan_pi.PI_SESSION_DIR_ENV: os.path.realpath(str(dir_a))}},
            )
            self.assertEqual(len(sessions), 1)
            self.assertFalse(sessions[0]["live"])
            self.assertIsNone(sessions[0]["pid"])

    def test_live_flags_session_dir_follows_new_file_in_same_dir(self) -> None:
        """同隔离目录 /new 后 live 跟最新 jsonl，旧 ident 标结束。"""
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            hosted = sessions_dir / "corral-aaaa1111"
            hosted.mkdir(parents=True)
            old = self._write_pi_session(
                hosted, "aaaa1111", cwd, "2023-11-14T22:15:00.000Z", "旧提问",
            )
            new = self._write_pi_session(
                hosted, "01a0new1", cwd, "2023-11-14T22:40:00.000Z", "/new 后提问",
            )
            os.utime(old, (1_000_000_000, 1_000_000_000))
            os.utime(new, (1_000_000_100, 1_000_000_100))
            sessions = self._scan_with_live(
                str(sessions_dir),
                processes=[(11, cwd)],
                cmdlines={11: "pi"},
                environ={11: {scan_pi.PI_SESSION_DIR_ENV: os.path.realpath(str(hosted))}},
                host=corral_host_extension(),
            )
            by_id = {item["id"]: item for item in sessions}
            self.assertEqual(by_id["01a0new1"]["pid"], 11)
            self.assertTrue(by_id["01a0new1"]["live"])
            self.assertFalse(by_id["aaaa1111"]["live"])
            self.assertIsNone(by_id["aaaa1111"]["pid"])

    def test_live_flags_resume_keeps_session_file_in_its_dir(self) -> None:
        """恢复 --session path 时 session-dir 是该文件所在目录，live 仍是这份。"""
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            hosted = sessions_dir / "corral-oldident"
            hosted.mkdir(parents=True)
            path = self._write_pi_session(
                hosted, "uuid-resume", cwd, "2023-11-14T22:15:00.000Z", "恢复这份",
            )
            directory = os.path.realpath(str(hosted))
            sessions = self._scan_with_live(
                str(sessions_dir),
                processes=[(11, cwd)],
                cmdlines={11: f"pi --approve --session {path}"},
                environ={11: {scan_pi.PI_SESSION_DIR_ENV: directory}},
            )
            by_id = {item["id"]: item for item in sessions}
            self.assertEqual(by_id["uuid-resume"]["pid"], 11)

    def test_live_flags_resume_in_default_dir_does_not_bind_newer_sibling(self) -> None:
        """恢复默认堆里的旧文件时，不得因 --session-dir 指向该堆而改绑到更新的另一条。"""
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            older = self._write_pi_session(
                sessions_dir, "uuid-old", cwd, "2023-11-14T22:15:00.000Z", "要恢复的旧会话",
            )
            newer = self._write_pi_session(
                sessions_dir, "uuid-new", cwd, "2023-11-14T22:40:00.000Z", "堆里更新的会话",
            )
            os.utime(older, (1_000_000_000, 1_000_000_000))
            os.utime(newer, (1_000_000_100, 1_000_000_100))
            dump = os.path.realpath(str(sessions_dir))
            sessions = self._scan_with_live(
                dump,
                processes=[(11, cwd)],
                cmdlines={11: f"pi --approve --session {older} --session-dir {dump}"},
                environ={11: {scan_pi.PI_SESSION_DIR_ENV: dump}},
            )
            by_id = {item["id"]: item for item in sessions}
            self.assertEqual(by_id["uuid-old"]["pid"], 11)
            self.assertFalse(by_id["uuid-new"]["live"])

    def test_live_flags_skip_print_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            created = "2023-11-14T22:15:00.000Z"
            self._write_pi_session(sessions_dir, "pi-print", cwd, created, "请生成标题")
            created_ts = datetime(2023, 11, 14, 22, 15, tzinfo=timezone.utc).timestamp()
            sessions = self._scan_with_live(
                str(sessions_dir),
                processes=[(55, cwd)],
                cmdlines={55: "pi --print 请生成标题"},
                starts={55: created_ts - 10},
            )
            self.assertFalse(sessions[0]["live"])

    def test_is_pi_tui_cmdline(self) -> None:
        self.assertTrue(scan_pi.is_pi_tui_cmdline("pi --approve"))
        self.assertTrue(scan_pi.is_pi_tui_cmdline("pi --approve --session /tmp/a.jsonl"))
        self.assertTrue(scan_pi.is_pi_tui_cmdline("node /opt/pi-coding-agent/dist/cli.js --approve"))
        self.assertTrue(scan_pi.is_pi_tui_cmdline("pi -c"))
        self.assertTrue(scan_pi.is_pi_tui_cmdline(
            "pi --approve Task: picking up a session. Please list files and "
            "update the install config"
        ))
        self.assertFalse(scan_pi.is_pi_tui_cmdline("pi --print hello"))
        self.assertFalse(scan_pi.is_pi_tui_cmdline("pi -p hello"))
        self.assertFalse(scan_pi.is_pi_tui_cmdline("pi auth login"))
        self.assertFalse(scan_pi.is_pi_tui_cmdline("pi --list-models"))
        self.assertFalse(scan_pi.is_pi_tui_cmdline("pi install"))

    def test_live_flags_bind_handoff_prompt_containing_subcommand_words(self) -> None:
        """接力位置参数里的 list/install 不得把仍在跑的 TUI 判成非交互。"""
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            created = "2023-11-14T22:15:00.000Z"
            self._write_pi_session(sessions_dir, "pi-handoff", cwd, created, "接力任务")
            created_ts = datetime(2023, 11, 14, 22, 15, tzinfo=timezone.utc).timestamp()
            sessions = self._scan_with_live(
                str(sessions_dir),
                processes=[(42, cwd)],
                cmdlines={42: (
                    "pi --approve Task: picking up a session. Please list files "
                    "and update the install config"
                )},
                starts={42: created_ts - 10},
            )
            self.assertTrue(sessions[0]["live"])
            self.assertEqual(sessions[0]["pid"], 42)

    def test_live_flags_ignore_session_flag_inside_handoff_prompt(self) -> None:
        """位置参数里的 ``--session`` 不得把新建 TUI 错绑到被接力的历史上。"""
        with tempfile.TemporaryDirectory() as td:
            cwd_dir = Path(td) / "proj"
            cwd_dir.mkdir()
            cwd = os.path.realpath(str(cwd_dir))
            sessions_dir = Path(td) / "sessions"
            sessions_dir.mkdir()
            created_old = "2023-11-14T22:00:00.000Z"
            created_new = "2023-11-14T22:15:00.000Z"
            self._write_pi_session(sessions_dir, "pi-old", cwd, created_old, "原会话")
            self._write_pi_session(sessions_dir, "pi-new", cwd, created_new, "接力新建")
            created_ts = datetime(2023, 11, 14, 22, 15, tzinfo=timezone.utc).timestamp()
            sessions = self._scan_with_live(
                str(sessions_dir),
                processes=[(42, cwd)],
                cmdlines={42: (
                    "pi --approve Task: picking up a session. "
                    "Resume with --session /tmp/pi-old.jsonl"
                )},
                starts={42: created_ts - 10},
            )
            by_id = {item["id"]: item for item in sessions}
            self.assertTrue(by_id["pi-new"]["live"])
            self.assertEqual(by_id["pi-new"]["pid"], 42)
            self.assertFalse(by_id["pi-old"]["live"])


class StartupLatencyTests(unittest.TestCase):
    """首屏延迟测量：改动扫描/界面/标题相关代码后必须跑这个用例并如实汇报耗时。

    见 AGENTS.md「验证要求」：corral 首屏（启动到首次渲染）延迟目标 ≤1s，
    界面层改用 Textual 后这条已从硬性红线放宽为非阻断项——但仍要求实测并
    汇报数值，不能不测。这里只做粗粒度的灾难性回归防护（>5s 才真正判定失败，
    比如不小心引入了一次同步网络调用），1s 目标本身只打印不作为断言依据：
    共享机器上跑其他重负载任务时，真实扫描性能会被拖到 1s 以上而与代码质量
    无关（真实测过：mongodump 之类的重 I/O 任务在跑时，本机 load average 到
    8+，同一次扫描耗时能从 <1s 抖到 1.7s+）。
    registry.scan_all() 就是 main() 里 store.load() 实际同步阻塞首屏的调用；
    本机无真实会话数据时（例如 CI/新机）跳过，避免假失败。
    """

    def test_scan_all_first_screen_latency(self) -> None:
        from corral.runtime import default_registry

        has_data = os.path.isdir(scan_claude.PROJECTS_DIR) or bool(scan_codex._find_all_session_files())
        if not has_data:
            self.skipTest("本机无真实会话数据，首屏延迟测量跳过")

        registry = default_registry()
        t0 = time.perf_counter()
        registry.scan_all(50)
        elapsed = time.perf_counter() - t0
        print(f"\n[首屏延迟] registry.scan_all(50) 耗时 {elapsed * 1000:.0f}ms"
              f"（目标 ≤1000ms，非阻断项；共享机器负载高时会自然超出）")

        self.assertLess(
            elapsed, 5.0,
            f"registry.scan_all(50) 耗时 {elapsed * 1000:.0f}ms，"
            f"远超 1s 目标的合理误差范围，需要排查是否引入了灾难性性能回归"
            f"（而不是机器负载波动）",
        )


if __name__ == "__main__":
    unittest.main()
