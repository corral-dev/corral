"""Cursor Agent CLI 运行时适配器。"""

from __future__ import annotations

import os

from corral.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from corral.runtime.base import BaseRuntime, LaunchError, usable_cwd
from corral.scan import cursor as scan_cursor


class CursorRuntime(BaseRuntime):
    id = "cursor"
    display_name = "Cursor"
    executable = "agent"
    # Cursor 的安装脚本同时放下 `agent` 和 `cursor-agent` 两个入口（官方文档现以
    # `agent` 为准，`cursor-agent` 作为兼容名保留）。可执行文件仍用官方主名 `agent`，
    # 但直启子命令两个名字都认——用户敲哪个都能进来。
    executable_aliases = ("cursor-agent",)
    history_reading_hint = (
        "Cursor Agent CLI 会话目录（~/.cursor/chats/<workspace>/<chatId>/）："
        "meta.json 是标题与工作目录；prompt_history.json 是用户输入（最新在前）；"
        "完整对话在 store.db 的 blobs 表里（JSON 消息含 role/content，用户正文常在 "
        "<user_query> 标签中）。请只读打开，不要改写原会话。"
    )
    auto_approve_args = ("--force",)

    def scan_signature(self) -> object | None:
        return scan_cursor.scan_signature()

    def scan_sessions(self, limit: int, keep_ids: set[str] | None = None) -> list[SessionInfo]:
        from corral.runtime.host_extension import corral_host_extension
        from corral.runtime.sesskit_bridge import call_scan

        return call_scan(
            scan_cursor.scan_sessions, limit=limit, keep_ids=keep_ids, host=corral_host_extension()
        )

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        from corral.runtime.sesskit_bridge import load_runtime_conversation

        return load_runtime_conversation(session)

    def delete_session(self, session: SessionInfo) -> None:
        scan_cursor.delete_session(str(session.get("path") or ""))

    def clone_session(self, session: SessionInfo) -> SessionInfo:
        try:
            return scan_cursor.clone_session(session)
        except ValueError as exc:
            raise LaunchError(str(exc)) from exc

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "--resume",
                str(session["id"]),
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_continue_plan(self, session: SessionInfo, instruction: str) -> LaunchPlan:
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

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        history_path = handoff.history_path
        history_dir = (
            history_path if os.path.isdir(history_path) else os.path.dirname(history_path)
        )
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "--add-dir",
                history_dir,
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan(
            argv=(self.executable, *self.auto_approve_args),
            cwd=usable_cwd(cwd),
        )
