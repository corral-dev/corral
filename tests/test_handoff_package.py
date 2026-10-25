"""Handoff package: verbatim user messages, budgeted recent tail and workspace state."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from corral import handoff_package as hp
from corral.i18n import t
from corral.models import ConversationMessage, Handoff
from corral.remote.richmsg import RichMessage, ToolCall
from corral.runtime.base import BaseRuntime


def _msg(role: str, text: str, tools: list[ToolCall] | None = None, ts: float | None = None) -> RichMessage:
    return RichMessage(seq=0, role=role, text=text, timestamp=ts, tools=tools or [])


def _tool(summary: str, status: str = "ok", output: str = "", kind: str = "shell") -> ToolCall:
    return ToolCall(call_id=summary, name="Bash", kind=kind, summary=summary, status=status, output=output)


class UserMessagesSectionTests(unittest.TestCase):
    def test_keeps_every_user_message_verbatim_and_collapses_repeated_nudges(self) -> None:
        correction = "这也太拥挤了，没必要\n下拉要在原地展开，" + "不要居中弹窗" * 30
        conversation = [
            ConversationMessage("user", "审视一下 TUI 界面"),
            ConversationMessage("assistant", "好的"),
            ConversationMessage("user", "继续"),
            ConversationMessage("user", "继续"),
            ConversationMessage("user", "继续"),
            ConversationMessage("user", correction),
        ]
        section = hp.user_messages_section(conversation)
        self.assertIn("审视一下 TUI 界面", section)
        self.assertIn(correction, section)  # multi-line and longer than the old 200-char clip
        self.assertEqual(section.count("\n继续\n"), 1)
        self.assertIn("(×3 in a row)", section)
        self.assertNotIn("好的", section)

    def test_long_message_is_clipped_with_a_pointer_to_the_history(self) -> None:
        section = hp.user_messages_section([ConversationMessage("user", "x" * (hp.USER_MESSAGE_LIMIT + 50))])
        self.assertIn("[50 more characters in the history file]", section)

    def test_over_budget_keeps_first_request_and_newest_messages(self) -> None:
        big = "y" * 3000
        conversation = [ConversationMessage("user", "最初需求")]
        conversation += [ConversationMessage("user", f"{i}:{big}") for i in range(20)]
        section = hp.user_messages_section(conversation)
        self.assertIn("最初需求", section)
        self.assertIn("19:", section)
        self.assertNotIn("\n0:", section)
        self.assertIn("earlier user messages omitted", section)

    def test_nested_handoff_prompt_is_peeled_to_its_task(self) -> None:
        nested = Handoff(
            source_runtime_id="pi", source_runtime_name="Pi", title="修复行显示",
            history_path="/tmp/h.jsonl", original_cwd="/tmp", history_reading_hint="hint",
            conversation_digest=f"{t('handoff.digest.recent')}\n{t('handoff.role.user')}: 行号错位",
        ).render_prompt()
        section = hp.user_messages_section([ConversationMessage("user", nested)])
        self.assertIn("修复行显示", section)
        self.assertNotIn(t("handoff.intro", name="Pi"), section)


class TailSectionTests(unittest.TestCase):
    def test_renders_assistant_text_tools_and_recent_tool_output(self) -> None:
        tail = [
            _msg("user", "跑一下测试"),
            _msg("assistant", "先跑全量。", [_tool("ci-test.py", output="2763 passed")]),
        ]
        section = hp.tail_section(tail)
        self.assertIn("先跑全量。", section)
        self.assertIn("- tool [shell] ci-test.py (ok)", section)
        self.assertIn("    2763 passed", section)
        self.assertNotIn("In-flight", section)

    def test_identical_consecutive_turns_collapse(self) -> None:
        tail = [_msg("assistant", "", [_tool("codemode")]) for _ in range(5)]
        section = hp.tail_section(tail)
        self.assertEqual(section.count("- tool [shell] codemode (ok)"), 1)
        self.assertIn("(×5 identical turns)", section)

    def test_only_the_newest_tool_turn_can_be_in_flight(self) -> None:
        stale = _msg("assistant", "", [_tool("old probe", status="running")])
        done = _msg("assistant", "", [_tool("notarize", status="ok")])
        self.assertNotIn("In-flight", hp.tail_section([stale, done]))
        running = _msg("assistant", "", [_tool("publish-release.sh", status="running")])
        section = hp.tail_section([done, running])
        self.assertIn("### In-flight operation at handoff", section)
        self.assertIn("publish-release.sh", section)

    def test_budget_keeps_newest_and_notes_omission(self) -> None:
        tail = [_msg("assistant", f"step {i} " + "z" * 5000) for i in range(30)]
        section = hp.tail_section(tail)
        self.assertIn("step 29", section)
        self.assertNotIn("step 0 ", section)
        self.assertIn("Earlier activity omitted", section)
        self.assertLess(len(section), hp.TAIL_BUDGET + 8000)

    def test_tool_output_detail_is_limited_to_the_latest_calls(self) -> None:
        tail = [_msg("assistant", "", [_tool(f"cmd{i}", output=f"out{i}")]) for i in range(10)]
        section = hp.tail_section(tail)
        self.assertIn("out9", section)
        self.assertNotIn("out0", section)


class WorkspaceStateTests(unittest.TestCase):
    def _git(self, repo: str, *args: str) -> None:
        subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)

    def _commit(self, repo: str, *args: str) -> None:
        self._git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", *args)

    def test_reports_status_diffstat_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as repo:
            self._git(repo, "init", "-q", "-b", "main")
            self._commit(repo, "--allow-empty", "-m", "first commit")
            Path(repo, "a.txt").write_text("hello\n")
            self._git(repo, "add", "a.txt")
            self._commit(repo, "-m", "add a")
            Path(repo, "a.txt").write_text("hello\nworld\n")
            Path(repo, "new.txt").write_text("n\n")
            state = hp.workspace_state(repo)
        self.assertIn("## 3. Workspace state at handoff", state)
        self.assertIn(" M a.txt", state)
        self.assertIn("?? new.txt", state)
        self.assertIn("1 file changed", state)
        self.assertIn("add a", state)

    def test_non_repository_outside_home_has_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as plain:
            with mock.patch.object(os.path, "expanduser", return_value="/nonexistent-home"):
                self.assertEqual(hp.workspace_state(plain), "")
        self.assertEqual(hp.workspace_state(""), "")


class WritePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._cache = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self._cache.name})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._cache.cleanup()

    def _write(self, conversation, tail=None) -> str:
        with mock.patch.object(hp, "_read_tail", return_value=(tail or [], False)):
            return hp.write_package(
                runtime_name="Claude Code",
                session={"source": "claude", "id": "abc/123", "cwd": ""},
                history_path="/tmp/h.jsonl",
                conversation=conversation,
            )

    def test_writes_private_file_under_cache_and_prunes_old_packages(self) -> None:
        old = Path(self._cache.name, "handoff", "stale.md")
        old.parent.mkdir(parents=True)
        old.write_text("old")
        past = time.time() - hp.RETENTION_SECONDS - 60
        os.utime(old, (past, past))

        path = self._write([ConversationMessage("user", "修复侧栏")])

        self.assertTrue(path.startswith(str(Path(self._cache.name).resolve())))
        self.assertIn("abc-123", path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        body = Path(path).read_text()
        self.assertIn("修复侧栏", body)
        self.assertIn("History file (authoritative): `/tmp/h.jsonl`", body)
        self.assertFalse(old.exists())

    def test_nothing_useful_returns_empty_path(self) -> None:
        self.assertEqual(self._write([]), "")

    def test_failure_degrades_to_empty_path(self) -> None:
        with mock.patch.object(hp, "render_package", side_effect=RuntimeError("boom")):
            self.assertEqual(self._write([ConversationMessage("user", "x")]), "")


class ExportHandoffPackageTests(unittest.TestCase):
    class _Runtime(BaseRuntime):
        id = "claude"
        display_name = "Claude"
        executable = "claude"
        history_reading_hint = "hint"

        def scan_sessions(self, limit, keep_ids=None):
            return []

        def load_conversation(self, session):
            return [ConversationMessage("user", "把接力做好"), ConversationMessage("assistant", "收到")]

        def build_resume_plan(self, session):
            raise NotImplementedError

        def build_new_plan(self, handoff):
            raise NotImplementedError

        def build_new_session_plan(self, cwd):
            raise NotImplementedError

    def test_prompt_names_the_package_and_keeps_digest(self) -> None:
        with tempfile.TemporaryDirectory() as cache, tempfile.NamedTemporaryFile(suffix=".jsonl") as history:
            with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": cache}):
                handoff = self._Runtime().export_handoff({"path": history.name, "cwd": "/tmp", "id": "s1"}, "标题")
                self.assertTrue(handoff.package_path)
                self.assertIn("把接力做好", Path(handoff.package_path).read_text())
            prompt = handoff.render_prompt()
        self.assertIn(t("handoff.package", path=handoff.package_path), prompt)
        self.assertIn(f"{t('handoff.role.user')}: 把接力做好", prompt)

    def test_prompt_without_package_has_no_package_section(self) -> None:
        handoff = Handoff("claude", "Claude", "标题", "/tmp/h.jsonl", "/tmp", "hint")
        marker = t("handoff.package", path="").splitlines()[0].strip()
        self.assertNotIn(marker, handoff.render_prompt())


if __name__ == "__main__":
    unittest.main()
