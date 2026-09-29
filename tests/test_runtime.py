from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import titles
from corral.i18n import t
from corral.models import Handoff, LaunchPlan, LaunchRequest, NewSessionRequest, session_key
from corral.runtime import BaseRuntime, LaunchError, RuntimeRegistry, default_registry
from corral.runtime import pi as runtime_pi


def _prepare_copy_request(registry: RuntimeRegistry, session, title: str):
    """CI 没有安装各家 CLI；复制计划测试只关心 fork/clone 路径，不依赖本机二进制。"""
    with mock.patch.object(BaseRuntime, "is_available", return_value=True):
        return registry.prepare_copy_request(session, title)


def _make_minimal_opencode_db(path: Path, session_id: str, title: str) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE session (id text PRIMARY KEY, project_id text, parent_id text, "
            "slug text, directory text, title text, version text, "
            "time_created integer, time_updated integer, time_archived integer)"
        )
        conn.execute("CREATE TABLE message (id text PRIMARY KEY, session_id text, "
                      "time_created integer, time_updated integer, data text)")
        conn.execute("CREATE TABLE part (id text PRIMARY KEY, message_id text, session_id text, "
                      "time_created integer, time_updated integer, data text)")
        conn.execute(
            "INSERT INTO session VALUES (?,'global',NULL,'x','/tmp',?, '1.0.0', 0, 0, NULL)",
            (session_id, title),
        )
        conn.commit()
    finally:
        conn.close()


class FakeRuntime(BaseRuntime):
    id = "gemini"
    display_name = "Gemini"
    executable = "gemini"
    history_reading_hint = "测试格式"

    def scan_sessions(self, limit: int, keep_ids: set[str] | None = None) -> list[dict]:
        return []

    def load_conversation(self, session: dict) -> list:
        return []

    def build_resume_plan(self, session: dict) -> LaunchPlan:
        return LaunchPlan((self.executable, "--resume", str(session["id"])), None)

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        return LaunchPlan((self.executable, handoff.render_prompt()), None)

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan((self.executable,), cwd)


class BrokenRuntime(BaseRuntime):
    """scan_sessions 必抛异常的假运行时，供验证 scan_all 的异常隔离。"""

    id = "broken"
    display_name = "Broken"
    executable = "broken"
    history_reading_hint = "测试格式"

    def scan_sessions(self, limit: int, keep_ids: set[str] | None = None) -> list[dict]:
        raise RuntimeError("模拟某条真实会话记录触发的未预料解析异常")

    def load_conversation(self, session: dict) -> list:
        return []

    def build_resume_plan(self, session: dict) -> LaunchPlan:
        return LaunchPlan((self.executable, "--resume", str(session["id"])), None)

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        return LaunchPlan((self.executable, handoff.render_prompt()), None)

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan((self.executable,), cwd)


class CachedRuntime(FakeRuntime):
    """带可变签名和结果的假运行时，供验证扫描缓存契约。"""

    id = "cached"

    def __init__(self, *, fail: bool = False) -> None:
        self.signature = 1
        self.fail = fail
        self.calls = 0
        self.session = {
            "source": self.id,
            "id": "cached-session",
            "cwd": "/tmp",
            "fallback_title": "缓存会话",
        }

    def scan_signature(self) -> object:
        return self.signature

    def scan_sessions(self, limit: int, keep_ids: set[str] | None = None) -> list[dict]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("模拟瞬时扫描失败")
        return [self.session]


class RuntimeTests(unittest.TestCase):
    def _session(self, source: str, history_path: str, cwd: str) -> dict:
        return {
            "source": source,
            "id": "session-123",
            "path": history_path,
            "cwd": cwd,
            "fallback_title": "修复会话接力",
        }

    def test_native_resume_keeps_runtime_specific_command(self) -> None:
        registry = default_registry()
        session = self._session("claude", "/tmp/not-needed.jsonl", "/tmp/not-exists")

        plan = registry.build_launch_plan(LaunchRequest(session, "claude", "修复会话接力"))

        self.assertEqual(
            plan.argv,
            ("claude", "--dangerously-skip-permissions", "--resume", "session-123"),
        )
        self.assertIsNone(plan.cwd)

    def test_pi_resume_fork_and_handoff_plans_use_session_path_without_rewriting_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "pi.jsonl"
            history.write_text('{"type":"session"}\n', encoding="utf-8")
            session = self._session("pi", str(history), td)
            registry = default_registry()

            resumed = registry.build_launch_plan(LaunchRequest(session, "pi", "继续 Pi 会话"))
            self.assertEqual(resumed.argv, ("pi", "--approve", "--session", str(history)))
            forked = registry.build_launch_plan(
                _prepare_copy_request(registry, session, "继续 Pi 会话")
            )
            self.assertEqual(forked.argv, ("pi", "--approve", "--fork", str(history)))
            stamped = runtime_pi.bind_hosted_ident(forked, "abcd1234")
            self.assertEqual(
                stamped.argv,
                ("pi", "--approve", "--session-id", "abcd1234", "--fork", str(history)),
            )
            self.assertNotIn("--session-dir", stamped.argv)
            resumed_bound = runtime_pi.bind_hosted_ident(resumed, "abcd1234")
            self.assertEqual(
                resumed_bound.argv,
                ("pi", "--approve", "--session", str(history)),
            )
            self.assertNotIn("--session-id", resumed_bound.argv)
            self.assertNotIn("--session-dir", resumed_bound.argv)
            handoff = registry.build_launch_plan(
                LaunchRequest(session, "claude", "交给 Claude", force_new=True)
            )
            self.assertIn(str(history), handoff.argv[-1])
            self.assertEqual(history.read_text(encoding="utf-8"), '{"type":"session"}\n')

    def test_pi_is_registered_and_builds_blank_and_cross_runtime_plans(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = default_registry()
            self.assertIn("pi", registry.ids)
            blank = registry.build_new_session_plan(NewSessionRequest("pi", td))
            self.assertEqual(blank.argv, ("pi", "--approve"))
            self.assertEqual(blank.cwd, td)

            history = Path(td) / "source.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("claude", str(history), td)
            plan = registry.build_launch_plan(LaunchRequest(session, "pi", "交给 Pi"))
            self.assertEqual(plan.argv[:2], ("pi", "--approve"))
            self.assertNotIn("--session", plan.argv)
            self.assertIn(str(history), plan.argv[-1])

    def test_force_new_same_runtime_uses_handoff_not_resume(self) -> None:
        """高级操作同助手另起：读历史后新建，不得带原生 --resume。"""
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "claude.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("claude", str(history), td)

            plan = default_registry().build_launch_plan(
                LaunchRequest(session, "claude", "原会话卡住另起", force_new=True)
            )

            self.assertEqual(plan.argv[0], "claude")
            self.assertNotIn("--resume", plan.argv)
            self.assertIn(str(history), plan.argv[-1])
            self.assertIn(t("handoff.intro", name="Claude"), plan.argv[-1])
            self.assertEqual(plan.cwd, td)

    def test_copy_session_claude_uses_native_fork(self) -> None:
        """复制会话：Claude 走官方 --fork-session，不读历史另起。"""
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "claude.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("claude", str(history), td)
            registry = default_registry()

            request = _prepare_copy_request(registry, session, "复杂讨论")
            self.assertTrue(request.copy_session)
            self.assertEqual(request.session["id"], session["id"])

            plan = registry.build_launch_plan(request)
            self.assertEqual(plan.argv[0], "claude")
            self.assertIn("--resume", plan.argv)
            self.assertIn("--fork-session", plan.argv)
            self.assertIn("session-123", plan.argv)
            self.assertNotIn(t("handoff.intro", name="Claude"), " ".join(plan.argv))

    def test_copy_session_codex_and_opencode_use_native_fork(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "rollout.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            registry = default_registry()

            codex = self._session("codex", str(history), td)
            codex_req = _prepare_copy_request(registry, codex, "讨论")
            codex_plan = registry.build_launch_plan(codex_req)
            self.assertEqual(codex_plan.argv[:2], ("codex", "fork"))
            self.assertIn("--no-daemon", codex_plan.argv)
            self.assertIn("session-123", codex_plan.argv)

            opencode = self._session("opencode", str(history), td)
            oc_req = _prepare_copy_request(registry, opencode, "讨论")
            oc_plan = registry.build_launch_plan(oc_req)
            self.assertIn("-s", oc_plan.argv)
            self.assertIn("--fork", oc_plan.argv)
            self.assertIn("session-123", oc_plan.argv)

    def test_copy_session_cursor_clones_directory(self) -> None:
        """Cursor 无官方分叉：磁盘复制目录并换新 id，再原生恢复。"""
        import json
        import uuid

        with tempfile.TemporaryDirectory() as td:
            old_id = str(uuid.uuid4())
            chat_dir = Path(td) / old_id
            chat_dir.mkdir()
            (chat_dir / "meta.json").write_text(
                json.dumps(
                    {
                        "cwd": td,
                        "title": "原标题",
                        "hasConversation": True,
                        "createdAtMs": 1_700_000_000_000,
                        "updatedAtMs": 1_700_000_000_000,
                    }
                ),
                encoding="utf-8",
            )
            (chat_dir / "prompt_history.json").write_text(
                json.dumps(["你好世界"]), encoding="utf-8"
            )
            (chat_dir / "store.db").write_bytes(b"")
            session = self._session("cursor", str(chat_dir / "store.db"), td)
            session["id"] = old_id

            registry = default_registry()
            request = _prepare_copy_request(registry, session, "原标题")
            self.assertFalse(request.copy_session)
            self.assertNotEqual(request.session["id"], old_id)
            self.assertTrue(
                str(request.session.get("native_title") or "").endswith(
                    ("（副本）", " (copy)", t("session.title.copy_suffix").strip())
                )
            )
            new_dir = Path(td) / request.session["id"]
            self.assertTrue((new_dir / "store.db").is_file())
            self.assertTrue((chat_dir / "store.db").is_file(), "原会话不得被改动")

            plan = registry.build_launch_plan(request)
            self.assertIn("--resume", plan.argv)
            self.assertIn(request.session["id"], plan.argv)
            self.assertNotIn("--fork-session", plan.argv)

    def test_copy_session_kimi_clones_directory(self) -> None:
        import json
        import uuid

        with tempfile.TemporaryDirectory() as td:
            old_id = f"session_{uuid.uuid4()}"
            session_dir = Path(td) / "ws" / old_id
            wire_dir = session_dir / "agents" / "main"
            wire_dir.mkdir(parents=True)
            (session_dir / "state.json").write_text(
                json.dumps(
                    {
                        "workDir": td,
                        "title": "Kimi 讨论",
                        "updatedAt": "2026-08-01T00:00:00Z",
                        "agents": {"main": {"homedir": str(session_dir)}},
                    }
                ),
                encoding="utf-8",
            )
            user_event = {
                "type": "context.append_message",
                "time": 1_700_000_000_000,
                "message": {
                    "role": "user",
                    "origin": {"kind": "user"},
                    "content": [{"type": "text", "text": "解释一下这个名词"}],
                },
            }
            (wire_dir / "wire.jsonl").write_text(
                json.dumps(user_event) + "\n", encoding="utf-8"
            )
            session = self._session("kimi", str(wire_dir / "wire.jsonl"), td)
            session["id"] = old_id

            registry = default_registry()
            request = _prepare_copy_request(registry, session, "Kimi 讨论")
            self.assertFalse(request.copy_session)
            self.assertNotEqual(request.session["id"], old_id)
            new_dir = Path(td) / "ws" / request.session["id"]
            self.assertTrue((new_dir / "agents" / "main" / "wire.jsonl").is_file())
            state = json.loads((new_dir / "state.json").read_text(encoding="utf-8"))
            self.assertIn(str(new_dir), str(state.get("agents", {})))
            self.assertNotIn(old_id, json.dumps(state))

            plan = registry.build_launch_plan(request)
            self.assertIn("-S", plan.argv)
            self.assertIn(request.session["id"], plan.argv)

    def test_prepare_copy_request_rejects_unavailable_runtime(self) -> None:
        session = self._session("claude", "/tmp/claude.jsonl", "/tmp")
        registry = default_registry()
        with mock.patch.object(BaseRuntime, "is_available", return_value=False):
            with self.assertRaises(LaunchError) as raised:
                registry.prepare_copy_request(session, "复杂讨论")
        self.assertEqual(
            str(raised.exception),
            t("launch.copy_not_installed", name="Claude"),
        )

    def test_claude_session_can_handoff_to_codex(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "claude.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("claude", str(history), td)

            plan = default_registry().build_launch_plan(
                LaunchRequest(session, "codex", "修复会话接力")
            )

            self.assertEqual(plan.argv[0], "codex")
            self.assertNotIn("resume", plan.argv)
            self.assertIn("--no-daemon", plan.argv)
            self.assertIn("--add-dir", plan.argv)
            self.assertIn(str(history), plan.argv[-1])
            self.assertIn("修复会话接力", plan.argv[-1])
            self.assertIn("Claude Code JSONL", plan.argv[-1])
            self.assertEqual(plan.cwd, td)

    def test_codex_session_can_handoff_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "codex.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("codex", str(history), td)

            plan = default_registry().build_launch_plan(
                LaunchRequest(session, "claude", "继续重构工具")
            )

            self.assertEqual(plan.argv[0], "claude")
            self.assertNotIn("--resume", plan.argv)
            self.assertIn("--add-dir", plan.argv)
            self.assertIn("Codex rollout JSONL", plan.argv[-1])

    def test_cross_runtime_requires_history_file(self) -> None:
        session = self._session("claude", "/tmp/missing-session-history.jsonl", os.getcwd())
        missing = os.path.abspath("/tmp/missing-session-history.jsonl")

        with self.assertRaises(LaunchError) as raised:
            default_registry().build_launch_plan(
                LaunchRequest(session, "codex", "修复会话接力")
            )
        self.assertEqual(str(raised.exception), t("launch.history_missing", path=missing))

    def test_cross_runtime_requires_history_path(self) -> None:
        """占位卡 / 未落盘会话 path 为空：跨助手接力必须明确拒绝，不得伪造路径。"""
        session = self._session("claude", "", os.getcwd())

        with self.assertRaises(LaunchError) as raised:
            default_registry().build_launch_plan(
                LaunchRequest(session, "codex", "修复会话接力")
            )
        self.assertEqual(str(raised.exception), t("launch.no_history_path"))

    def test_opencode_resume_plan(self) -> None:
        registry = default_registry()
        session = self._session("opencode", "/tmp/not-needed.db", "/tmp/not-exists")

        plan = registry.build_launch_plan(LaunchRequest(session, "opencode", "修复会话接力"))

        self.assertEqual(plan.argv, ("opencode", "--auto", "-s", "session-123"))
        self.assertIsNone(plan.cwd)

    def test_opencode_continue_plan(self) -> None:
        registry = default_registry()
        session = self._session("opencode", "/tmp/not-needed.db", "/tmp/not-exists")

        plan = registry.get("opencode").build_continue_plan(session, "继续处理未完成的任务")

        self.assertEqual(
            plan.argv,
            ("opencode", "run", "--auto", "-s", "session-123", "继续处理未完成的任务"),
        )

    def test_opencode_new_session_plan_has_no_handoff_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan = default_registry().build_new_session_plan(NewSessionRequest("opencode", td))

        self.assertEqual(plan.argv, ("opencode", "--auto"))
        self.assertEqual(plan.cwd, td)

    def test_claude_session_can_handoff_to_opencode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "claude.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("claude", str(history), td)

            plan = default_registry().build_launch_plan(
                LaunchRequest(session, "opencode", "修复会话接力")
            )

            self.assertEqual(plan.argv[0], "opencode")
            self.assertIn("--prompt", plan.argv)
            self.assertNotIn("-s", plan.argv)
            # 跨助手接力要读别处的历史，必然触发「外部目录」权限询问，必须自动放行。
            self.assertIn("--auto", plan.argv)
            self.assertIn("修复会话接力", plan.argv[-1])
            self.assertIn("Claude Code JSONL", plan.argv[-1])
            self.assertEqual(plan.cwd, td)

    def test_opencode_session_can_handoff_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "opencode.db"
            _make_minimal_opencode_db(db_path, "ses_abc123", "修复登录")
            session = self._session("opencode", str(db_path), td)
            session["id"] = "ses_abc123"

            plan = default_registry().build_launch_plan(
                LaunchRequest(session, "claude", "继续接力")
            )

            self.assertEqual(plan.argv[0], "claude")
            self.assertIn("opencode session export", plan.argv[-1])
            self.assertIn("ses_abc123", plan.argv[-1])
            # 接力提示词里的正文位置是 v2 口径（两表两路径），不得回退到 v1 三表描述。
            self.assertIn("session_message", plan.argv[-1])
            self.assertIn("session_v2", plan.argv[-1])
            self.assertNotIn("part.data", plan.argv[-1])

    def test_opencode_handoff_requires_db_file(self) -> None:
        session = self._session("opencode", "/tmp/missing-opencode.db", os.getcwd())
        missing = os.path.abspath("/tmp/missing-opencode.db")

        with self.assertRaises(LaunchError) as raised:
            default_registry().build_launch_plan(
                LaunchRequest(session, "claude", "修复会话接力")
            )
        self.assertEqual(str(raised.exception), t("launch.history_missing", path=missing))

    def test_kimi_resume_plan(self) -> None:
        registry = default_registry()
        session = self._session("kimi", "/tmp/not-needed.jsonl", "/tmp/not-exists")

        plan = registry.build_launch_plan(LaunchRequest(session, "kimi", "修复会话接力"))

        self.assertEqual(plan.argv, ("kimi", "-y", "-S", "session-123"))
        self.assertIsNone(plan.cwd)

    def test_kimi_new_session_plan_has_no_handoff_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan = default_registry().build_new_session_plan(NewSessionRequest("kimi", td))

        self.assertEqual(plan.argv, ("kimi", "-y"))
        self.assertEqual(plan.cwd, td)

    def test_claude_session_can_handoff_to_kimi(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "claude.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("claude", str(history), td)

            plan = default_registry().build_launch_plan(
                LaunchRequest(session, "kimi", "修复会话接力")
            )

        self.assertEqual(plan.argv[0], "kimi")
        self.assertIn("--add-dir", plan.argv)
        self.assertIn("-p", plan.argv)  # Kimi 接力目标走非交互 prompt 模式（见适配器说明）
        self.assertNotIn("-S", plan.argv)
        self.assertIn("修复会话接力", plan.argv[-1])
        self.assertIn("Claude Code JSONL", plan.argv[-1])
        self.assertEqual(plan.cwd, td)

    def test_kimi_session_can_handoff_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "wire.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("kimi", str(history), td)

            plan = default_registry().build_launch_plan(
                LaunchRequest(session, "claude", "继续接力")
            )

        self.assertEqual(plan.argv[0], "claude")
        self.assertNotIn("--resume", plan.argv)
        self.assertIn("--add-dir", plan.argv)
        self.assertIn("context.append_message", plan.argv[-1])

    def test_registry_accepts_new_runtime_without_pairwise_logic(self) -> None:
        registry = RuntimeRegistry((*default_registry(), FakeRuntime()))
        with tempfile.TemporaryDirectory() as td:
            history = Path(td) / "claude.jsonl"
            history.write_text("{}\n", encoding="utf-8")
            session = self._session("claude", str(history), td)

            plan = registry.build_launch_plan(
                LaunchRequest(session, "gemini", "验证扩展能力")
            )

        self.assertEqual(registry.ids, ("claude", "codex", "opencode", "kimi", "cursor", "pi", "gemini"))
        self.assertEqual(plan.argv[0], "gemini")
        self.assertIn("验证扩展能力", plan.argv[-1])

    def test_cursor_resume_plan(self) -> None:
        registry = default_registry()
        session = self._session("cursor", "/tmp/chat-dir", "/tmp/not-exists")
        session["id"] = "chat-1"

        plan = registry.build_launch_plan(LaunchRequest(session, "cursor", "继续"))

        self.assertEqual(plan.argv, ("agent", "--force", "--resume", "chat-1"))
        self.assertIsNone(plan.cwd)

    def test_default_registry_includes_cursor(self) -> None:
        reg = default_registry()
        self.assertIn("cursor", reg.ids)
        self.assertEqual(reg.get("cursor").executable, "agent")

    def test_cursor_availability_accepts_primary_or_alias_command(self) -> None:
        runtime = default_registry().get("cursor")

        for installed, expected in (({"agent"}, True), ({"cursor-agent"}, True), (set(), False)):
            with self.subTest(installed=installed):
                with mock.patch(
                    "corral.runtime.base.shutil.which",
                    side_effect=lambda name, available=installed: (
                        f"/tmp/{name}" if name in available else None
                    ),
                ):
                    self.assertEqual(runtime.is_available(), expected)

    def test_passthrough_pads_force_once_for_cursor(self) -> None:
        reg = default_registry()
        plan = reg.build_passthrough_plan("cursor", ["--model", "auto"])
        self.assertEqual(plan.argv[:3], ("agent", "--force", "--model"))
        plan2 = reg.build_passthrough_plan("cursor", ["--force", "hi"])
        self.assertEqual(plan2.argv.count("--force"), 1)

    def test_claude_new_session_plan_has_no_handoff_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan = default_registry().build_new_session_plan(NewSessionRequest("claude", td))

        self.assertEqual(plan.argv, ("claude", "--dangerously-skip-permissions"))
        self.assertEqual(plan.cwd, td)

    def test_codex_new_session_plan_has_no_handoff_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan = default_registry().build_new_session_plan(NewSessionRequest("codex", td))

        self.assertEqual(
            plan.argv,
            ("codex", "--no-daemon", "--dangerously-bypass-approvals-and-sandbox"),
        )
        self.assertEqual(plan.cwd, td)

    def test_new_session_plan_drops_nonexistent_cwd(self) -> None:
        plan = default_registry().build_new_session_plan(
            NewSessionRequest("claude", "/tmp/does-not-exist-sc-test")
        )

        self.assertIsNone(plan.cwd)

    def test_new_session_plan_dispatches_to_registered_runtime(self) -> None:
        registry = RuntimeRegistry((*default_registry(), FakeRuntime()))
        with tempfile.TemporaryDirectory() as td:
            plan = registry.build_new_session_plan(NewSessionRequest("gemini", td))

        self.assertEqual(plan.argv, ("gemini",))
        self.assertEqual(plan.cwd, td)

    def test_scan_all_isolates_exception_from_one_runtime(self) -> None:
        # 单个运行时的扫描异常（如某条真实会话记录触发未预料的解析 bug）不能
        # 拖垮其余运行时的结果，也不能让 corral 首屏直接崩溃退出。
        registry = RuntimeRegistry((FakeRuntime(), BrokenRuntime()))

        scanned = registry.scan_all(limit=10)

        self.assertEqual(scanned["gemini"], [])
        self.assertEqual(scanned["broken"], [])

    def test_scan_cache_copies_sessions_on_save_and_each_return(self) -> None:
        runtime = CachedRuntime()
        registry = RuntimeRegistry((runtime,))

        first = registry.scan_all(limit=10)[runtime.id]
        runtime.session["keepalive_name"] = "污染源结果"
        first[0]["runtime_status"] = "污染首次返回"

        second = registry.scan_all(limit=10)[runtime.id]
        self.assertEqual(runtime.calls, 1)
        self.assertNotIn("keepalive_name", second[0])
        self.assertNotIn("runtime_status", second[0])

        second[0]["runtime_status"] = "污染缓存命中返回"
        third = registry.scan_all(limit=10)[runtime.id]
        self.assertNotIn("runtime_status", third[0])

    def test_nested_history_scan_signature_skips_unchanged_rescan(self) -> None:
        import json

        from corral.runtime.cursor import CursorRuntime
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            chat = root / "ws" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            chat.mkdir(parents=True)
            (chat / "meta.json").write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "createdAtMs": 1,
                    "updatedAtMs": 2,
                    "hasConversation": True,
                    "title": "签名跳过",
                    "cwd": str(root),
                }),
                encoding="utf-8",
            )
            (chat / "prompt_history.json").write_text('["hello"]', encoding="utf-8")
            calls = {"n": 0}
            real = scan_cursor.scan_sessions

            def counting(*args, **kwargs):
                calls["n"] += 1
                return real(*args, **kwargs)

            runtime = CursorRuntime()
            registry = RuntimeRegistry((runtime,))
            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_pid_snapshot", return_value=()
            ), mock.patch.object(
                scan_cursor, "live_processes", return_value=[]
            ), mock.patch.object(
                scan_cursor, "scan_sessions", side_effect=counting
            ):
                first = registry.scan_all(limit=10)[runtime.id]
                second = registry.scan_all(limit=10)[runtime.id]
        self.assertEqual(calls["n"], 1)
        self.assertEqual([session["id"] for session in first], [
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ])
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertTrue(registry.last_scan_cache_hit_all)

    def test_cursor_wal_rewrite_does_not_bust_scan_cache(self) -> None:
        """Streaming WAL writes must not force a full Cursor list rescan."""
        import json

        from corral.runtime.cursor import CursorRuntime
        from corral.scan import cursor as scan_cursor

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            chat = root / "ws" / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            chat.mkdir(parents=True)
            (chat / "meta.json").write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "createdAtMs": 1,
                    "updatedAtMs": 2,
                    "hasConversation": True,
                    "title": "WAL 不破缓存",
                    "cwd": str(root),
                }),
                encoding="utf-8",
            )
            (chat / "prompt_history.json").write_text('["hello"]', encoding="utf-8")
            (chat / "store.db").write_bytes(b"db")
            calls = {"n": 0}
            real = scan_cursor.scan_sessions

            def counting(*args, **kwargs):
                calls["n"] += 1
                return real(*args, **kwargs)

            runtime = CursorRuntime()
            registry = RuntimeRegistry((runtime,))
            with mock.patch.object(scan_cursor, "CHATS_DIR", str(root)), mock.patch.object(
                scan_cursor, "live_pid_snapshot", return_value=()
            ), mock.patch.object(
                scan_cursor, "live_processes", return_value=[]
            ), mock.patch.object(
                scan_cursor, "scan_sessions", side_effect=counting
            ):
                registry.scan_all(limit=10)
                (chat / "store.db-wal").write_bytes(b"wal-1")
                registry.scan_all(limit=10)
                (chat / "store.db-wal").write_bytes(b"wal-2-longer")
                registry.scan_all(limit=10)
        self.assertEqual(calls["n"], 1)
        self.assertTrue(registry.last_scan_cache_hit_all)

    def test_scan_failure_returns_old_cache_without_overwriting_it(self) -> None:
        runtime = CachedRuntime()
        registry = RuntimeRegistry((runtime,))
        original = registry.scan_all(limit=10)[runtime.id]

        runtime.signature = 2
        runtime.fail = True
        stale = registry.scan_all(limit=10)[runtime.id]
        self.assertEqual(stale, original)

        # 失败不能把新签名写进缓存；同一签名恢复后必须重新扫描。
        runtime.fail = False
        runtime.session = {**runtime.session, "id": "recovered-session"}
        recovered = registry.scan_all(limit=10)[runtime.id]
        self.assertEqual(runtime.calls, 3)
        self.assertEqual(recovered[0]["id"], "recovered-session")

    def test_first_scan_failure_degrades_to_empty_result(self) -> None:
        runtime = CachedRuntime(fail=True)

        scanned = RuntimeRegistry((runtime,)).scan_all(limit=10)

        self.assertEqual(scanned[runtime.id], [])

    def test_passthrough_plan_prepends_auto_approve_args(self) -> None:
        plan = default_registry().build_passthrough_plan("claude", ["把测试修到全绿"])

        self.assertEqual(plan.argv, ("claude", "--dangerously-skip-permissions", "把测试修到全绿"))
        self.assertIsNone(plan.cwd)

    def test_passthrough_plan_does_not_duplicate_user_supplied_auto_approve_arg(self) -> None:
        plan = default_registry().build_passthrough_plan(
            "codex", ["--dangerously-bypass-approvals-and-sandbox", "resume"]
        )

        self.assertEqual(
            plan.argv,
            ("codex", "--no-daemon", "--dangerously-bypass-approvals-and-sandbox", "resume"),
        )

    def test_codex_passthrough_uses_current_account_unless_remote_is_explicit(self) -> None:
        registry = default_registry()
        bare = registry.build_passthrough_plan("codex", [])
        self.assertEqual(bare.argv[:2], ("codex", "--no-daemon"))

        remote = registry.build_passthrough_plan("codex", ["--remote", "unix://"])
        self.assertNotIn("--no-daemon", remote.argv)

        explicit = registry.build_passthrough_plan("codex", ["--no-daemon"])
        self.assertEqual(explicit.argv.count("--no-daemon"), 1)

    def test_opencode_passthrough_prepends_auto_for_bare_tui(self) -> None:
        plan = default_registry().build_passthrough_plan("opencode", [])

        self.assertEqual(plan.argv, ("opencode", "--auto"))

    def test_opencode_passthrough_puts_auto_after_run_subcommand(self) -> None:
        """--auto 前置会让 OpenCode 把 run 当成项目路径，静默变成另一件事。"""
        plan = default_registry().build_passthrough_plan(
            "opencode", ["run", "把测试修到全绿"]
        )

        self.assertEqual(plan.argv, ("opencode", "run", "--auto", "把测试修到全绿"))

    def test_opencode_passthrough_skips_auto_for_subcommands_that_reject_it(self) -> None:
        """stats/session/auth 等子命令不认 --auto，垫上会用法错误退出。"""
        for args in (["stats"], ["session", "export", "ses_123"], ["auth", "login"]):
            with self.subTest(args=args):
                plan = default_registry().build_passthrough_plan("opencode", args)
                self.assertEqual(plan.argv, ("opencode", *args))

    def test_opencode_subcommands_match_v2_help(self) -> None:
        """SUBCOMMANDS 与 v2（2.0.16 `--help`）全集一致：cli.py 靠它区分直启透传与项目快捷启动。"""
        from corral.runtime.opencode import OpenCodeRuntime

        self.assertEqual(
            OpenCodeRuntime.SUBCOMMANDS,
            frozenset(
                (
                    "upgrade", "update", "uninstall", "acp", "api", "debug",
                    "auth", "mcp", "plugin", "models", "stats", "mini", "run",
                    "session", "service", "reload", "pair", "serve",
                )
            ),
        )
        for dead in ("attach", "agent", "export", "import", "github", "pr", "db", "completion"):
            self.assertNotIn(dead, OpenCodeRuntime.SUBCOMMANDS)

    def test_opencode_passthrough_passes_mini_through_verbatim(self) -> None:
        """mini 不认 --auto：`corral opencode mini …` 原样透传，不垫参数。"""
        for args in (["mini"], ["mini", "--replay"], ["mini", "-m", "agent"]):
            with self.subTest(args=args):
                plan = default_registry().build_passthrough_plan("opencode", args)
                self.assertEqual(plan.argv, ("opencode", *args))

    def test_opencode_passthrough_mini_wins_over_same_name_path(self) -> None:
        """cwd 下有 mini 同名目录也不能把它当成项目路径（否则会前置 --auto 导致起不来）。"""
        with tempfile.TemporaryDirectory() as td:
            mini_dir = Path(td) / "mini"
            mini_dir.mkdir()
            old_cwd = os.getcwd()
            os.chdir(td)
            try:
                plan = default_registry().build_passthrough_plan("opencode", ["mini"])
            finally:
                os.chdir(old_cwd)
            self.assertEqual(plan.argv, ("opencode", "mini"))

    def test_opencode_passthrough_treats_leading_path_as_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            for token in (td, "./proj", "../proj", "~/proj"):
                with self.subTest(token=token):
                    plan = default_registry().build_passthrough_plan("opencode", [token])
                    self.assertEqual(plan.argv, ("opencode", "--auto", token))

    def test_opencode_passthrough_respects_user_supplied_auto(self) -> None:
        plan = default_registry().build_passthrough_plan("opencode", ["run", "--auto", "x"])

        self.assertEqual(plan.argv, ("opencode", "run", "--auto", "x"))

    def test_passthrough_plan_dispatches_to_registered_runtime(self) -> None:
        registry = RuntimeRegistry((*default_registry(), FakeRuntime()))

        plan = registry.build_passthrough_plan("gemini", ["--foo"])

        self.assertEqual(plan.argv, ("gemini", "--foo"))
        self.assertIsNone(plan.cwd)

    def test_session_key_is_runtime_scoped(self) -> None:
        claude = {"source": "claude", "id": "same"}
        codex = {"source": "codex", "id": "same"}

        self.assertNotEqual(session_key(claude), session_key(codex))

    def test_generated_title_cache_is_runtime_scoped(self) -> None:
        sessions = [
            {
                "source": "claude",
                "id": "same",
                "size_bytes": 10,
                "size_kb": 0.1,
                "fallback_title": "Claude 任务",
            },
            {
                "source": "codex",
                "id": "same",
                "size_bytes": 20,
                "size_kb": 0.2,
                "fallback_title": "Codex 任务",
            },
        ]
        cache = {}
        generated = {"claude:same": "Claude 标题", "codex:same": "Codex 标题"}

        with (
            mock.patch.object(titles, "generate_titles_batch", return_value=generated),
            mock.patch.object(titles, "save_cache", return_value=None),
        ):
            # 显式注入一个真值 generator：CI 环境没有安装 claude/codex，若依赖
            # refresh_titles 内部的 titlegen.resolve_generator() 自动探测，会在
            # 探测不到任何 CLI 时提前返回空字典，导致下面 mock 的
            # generate_titles_batch 根本不会被调用（本机因为装了 claude/codex，
            # 探测能成功，掩盖了这个问题，只有干净的 CI 环境才会暴露）。
            result = titles.refresh_titles(sessions, cache, generator=mock.Mock())

        self.assertEqual(result, generated)
        self.assertEqual(cache["claude:same"]["title"], "Claude 标题")
        self.assertEqual(cache["codex:same"]["title"], "Codex 标题")


if __name__ == "__main__":
    unittest.main()
