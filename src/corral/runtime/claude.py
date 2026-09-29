"""Claude Code 运行时适配器。"""

from __future__ import annotations

import json
import os

from corral.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from corral.runtime.base import BaseRuntime, usable_cwd
from corral.scan import claude as scan_claude


class ClaudeRuntime(BaseRuntime):
    id = "claude"
    display_name = "Claude"
    executable = "claude"
    history_reading_hint = (
        "Claude Code JSONL；重点关注 user、assistant、tool_use、tool_result、"
        "last-prompt 等记录及其 message.content。"
    )
    _AUTO_APPROVE_ARGS = ("--dangerously-skip-permissions",)
    # Claude Code (verified 2.1.284, anthropics/claude-code#96795) holds a pending
    # AskUserQuestion out of the transcript until it is answered, so the phone can't
    # see it. Any PreToolUse hook matching the tool makes it persist within ~2s;
    # this one is a no-op and leaves the permission flow unchanged.
    _QUESTION_HOOK_SETTINGS = json.dumps(
        {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "AskUserQuestion", "hooks": [{"type": "command", "command": "true"}]}
                ]
            }
        },
        separators=(",", ":"),
    )

    @property
    def auto_approve_args(self) -> tuple[str, ...]:  # type: ignore[override]
        """默认全自动放行；唯一例外是 root/sudo 下 Claude 自己拒绝这个参数。

        这不是安全权衡（项目既定默认就是跳过全部权限问询，见 `AGENTS.md`），而是
        "加了就起不来"的事实：Claude Code 在非 Windows 平台检测到 uid 为 0 且不在
        已知沙箱里时，带该参数会直接以退出码 1 结束。以 root 身份敲 `corral claude`
        的用户如果被垫上这个参数，拿到的是一个起不来的会话，比不放行糟糕得多。
        沙箱标记（`IS_SANDBOX` / `CLAUDE_CODE_BUBBLEWRAP`）存在时 Claude 会跳过该
        检查，此时照常放行。
        """
        if os.name == "nt":
            return self._AUTO_APPROVE_ARGS
        if getattr(os, "geteuid", None) is None or os.geteuid() != 0:
            return self._AUTO_APPROVE_ARGS
        if os.environ.get("IS_SANDBOX") or os.environ.get("CLAUDE_CODE_BUBBLEWRAP"):
            return self._AUTO_APPROVE_ARGS
        return ()

    def hosted_args(self, user_args: tuple[str, ...] = ()) -> tuple[str, ...]:
        """Auto-approve plus the question-persisting hook, unless the user brings settings."""
        extra = tuple(arg for arg in self.auto_approve_args if arg not in user_args)
        if "--settings" in user_args:
            return extra
        return (*extra, "--settings", self._QUESTION_HOOK_SETTINGS)

    def compose_passthrough_argv(self, user_args: tuple[str, ...]) -> tuple[str, ...]:
        return (self.executable, *self.hosted_args(user_args), *user_args)

    def scan_signature(self) -> object | None:
        return scan_claude.scan_signature()

    def scan_sessions(self, limit: int, keep_ids: set[str] | None = None) -> list[SessionInfo]:
        from corral.runtime.sesskit_bridge import call_scan

        return call_scan(scan_claude.scan_sessions, limit=limit, keep_ids=keep_ids)

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        from corral.runtime.sesskit_bridge import load_runtime_conversation

        return load_runtime_conversation(session)

    def delete_session(self, session: SessionInfo) -> None:
        scan_claude.delete_session(str(session.get("path") or ""))

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.hosted_args(),
                "--resume",
                str(session["id"]),
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_continue_plan(self, session: SessionInfo, instruction: str) -> LaunchPlan:
        """构造供外部执行器使用的非交互式 Claude 原生续接计划。"""
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "--resume",
                str(session["id"]),
                "--print",
                instruction,
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_fork_plan(self, session: SessionInfo) -> LaunchPlan | None:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.hosted_args(),
                "--resume",
                str(session["id"]),
                "--fork-session",
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        history_dir = os.path.dirname(handoff.history_path)
        return LaunchPlan(
            argv=(
                self.executable,
                "--add-dir",
                history_dir,
                *self.hosted_args(),
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.hosted_args(),
            ),
            cwd=usable_cwd(cwd),
        )
