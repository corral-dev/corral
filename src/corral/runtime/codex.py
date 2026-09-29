"""Codex CLI 运行时适配器。"""

from __future__ import annotations

import os

from corral.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from corral.runtime.base import BaseRuntime, usable_cwd
from corral.scan import codex as scan_codex

_NO_DAEMON = "--no-daemon"


class CodexRuntime(BaseRuntime):
    id = "codex"
    display_name = "Codex"
    executable = "codex"
    history_reading_hint = (
        "Codex rollout JSONL；重点关注 session_meta、event_msg、response_item，"
        "以及 user_message、agent_message、task_complete、工具调用与结果。"
    )
    auto_approve_args = ("--dangerously-bypass-approvals-and-sandbox",)

    def scan_signature(self) -> object | None:
        from corral.codex_identity import claim_signature

        return (scan_codex.scan_signature(), claim_signature())

    def scan_sessions(self, limit: int, keep_ids: set[str] | None = None) -> list[SessionInfo]:
        from corral.codex_identity import live_claims
        from corral.runtime.host_extension import corral_host_extension
        from corral.runtime.sesskit_bridge import call_scan

        return call_scan(
            scan_codex.scan_sessions,
            limit=limit,
            keep_ids=keep_ids,
            host=corral_host_extension(),
            host_claim_provider=live_claims,
        )

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        from corral.runtime.sesskit_bridge import load_runtime_conversation

        return load_runtime_conversation(session)

    def delete_session(self, session: SessionInfo) -> None:
        scan_codex.delete_session(str(session.get("path") or ""))

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                "resume",
                _NO_DAEMON,
                *self.auto_approve_args,
                str(session["id"]),
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_continue_plan(self, session: SessionInfo, instruction: str) -> LaunchPlan:
        """构造供外部执行器使用的非交互式 Codex 原生续接计划。"""
        return LaunchPlan(
            argv=(
                self.executable,
                "exec",
                "resume",
                *self.auto_approve_args,
                str(session["id"]),
                instruction,
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_fork_plan(self, session: SessionInfo) -> LaunchPlan | None:
        return LaunchPlan(
            argv=(
                self.executable,
                "fork",
                _NO_DAEMON,
                *self.auto_approve_args,
                str(session["id"]),
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        history_dir = os.path.dirname(handoff.history_path)
        return LaunchPlan(
            argv=(
                self.executable,
                _NO_DAEMON,
                *self.auto_approve_args,
                "--add-dir",
                history_dir,
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                _NO_DAEMON,
                *self.auto_approve_args,
            ),
            cwd=usable_cwd(cwd),
        )

    def compose_passthrough_argv(self, user_args: tuple[str, ...]) -> tuple[str, ...]:
        argv = super().compose_passthrough_argv(user_args)
        if _NO_DAEMON in user_args or any(
            arg == "--remote" or arg.startswith("--remote=") for arg in user_args
        ):
            return argv
        return (argv[0], _NO_DAEMON, *argv[1:])
