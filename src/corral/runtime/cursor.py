"""Cursor Agent CLI 运行时适配器。"""

from __future__ import annotations

import json
import os
import shutil

from corral.i18n import t
from corral.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from corral.runtime.base import (
    BaseRuntime,
    LaunchError,
    normalize_fork_title,
    usable_cwd,
)
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
            cloned = scan_cursor.clone_session(session)
        except ValueError as exc:
            raise LaunchError(str(exc)) from exc
        return _retitle_cursor_fork(cloned, session)

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


def _retitle_cursor_fork(cloned: SessionInfo, source: SessionInfo) -> SessionInfo:
    """Rewrite a fresh Cursor clone's title to the adopted fork suffix.

    SessKit stamps its legacy copy suffix into the clone's meta.json; the
    re-scanned fork would otherwise keep showing copy wording. Only the newly
    created clone directory is ever written; the source history is untouched.
    The title is written atomically (temp file + os.replace) so a failed write
    never truncates existing data, and any failure removes the fresh clone
    directory before raising.
    """
    fork_title = normalize_fork_title(cloned.get("native_title") or "")
    if not fork_title:
        return cloned
    clone_path = os.path.abspath(str(cloned.get("path") or ""))
    chat_dir = clone_path if os.path.isdir(clone_path) else os.path.dirname(clone_path)
    source_path = os.path.abspath(str(source.get("path") or ""))
    source_dir = source_path if os.path.isdir(source_path) else os.path.dirname(source_path)
    if not chat_dir or chat_dir == source_dir:
        raise LaunchError(
            t("launch.fork_title_failed", error="clone directory matches source directory")
        )
    meta_path = os.path.join(chat_dir, "meta.json")
    try:
        with open(meta_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object in {meta_path}")
        if data.get("title") == fork_title and cloned.get("native_title") == fork_title:
            return cloned
        data["title"] = fork_title
        tmp_path = meta_path + ".fork-title-tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, meta_path)
    except (OSError, ValueError) as exc:
        shutil.rmtree(chat_dir, ignore_errors=True)
        raise LaunchError(t("launch.fork_title_failed", error=exc)) from exc
    cloned["native_title"] = fork_title
    return cloned
