"""Kimi Code CLI 运行时适配器。"""

from __future__ import annotations

import os

from corral.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from corral.runtime.base import BaseRuntime, LaunchError, usable_cwd
from corral.scan import kimi as scan_kimi


class KimiRuntime(BaseRuntime):
    id = "kimi"
    display_name = "Kimi"
    executable = "kimi"
    history_reading_hint = (
        "Kimi Code CLI 的 wire.jsonl（协议事件逐行 JSON）：用户消息是 "
        'type=="context.append_message" 且 message.role=="user"，正文在 message.content 里 '
        'type=="text" 的分片；助手正文是 type=="context.append_loop_event" 且 '
        'event.type=="content.part" 且 event.part.type=="text"（part.type=="think" 是思考过程，'
        "忽略）。开头体量很大的 config.update（系统提示）、llm.tools_snapshot、llm.request、"
        "usage.record 等都是协议噪音，可直接跳过。"
    )
    # Kimi 2.1.1+ turned `-y/--yolo` into "Ask When Needed" (risky actions still
    # prompt); `--auto` is "Never Ask" and is accepted at the root command, so
    # unattended launches (native resume, blank new session, direct subcommand) use it.
    auto_approve_args = ("--auto",)

    def scan_signature(self) -> object | None:
        return scan_kimi.scan_signature()

    def scan_sessions(self, limit: int, keep_ids: set[str] | None = None) -> list[SessionInfo]:
        from corral.runtime.host_extension import corral_host_extension
        from corral.runtime.sesskit_bridge import call_scan

        return call_scan(
            scan_kimi.scan_sessions, limit=limit, keep_ids=keep_ids, host=corral_host_extension()
        )

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        from corral.runtime.sesskit_bridge import load_runtime_conversation

        return load_runtime_conversation(session)

    def delete_session(self, session: SessionInfo) -> None:
        scan_kimi.delete_session(str(session.get("path") or ""))

    def clone_session(self, session: SessionInfo) -> SessionInfo:
        try:
            return scan_kimi.clone_session(session)
        except ValueError as exc:
            raise LaunchError(str(exc)) from exc

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "-S",
                str(session["id"]),
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        # Kimi 的 `-p/--prompt` 是非交互「跑一个 prompt 并打印」模式，跑完即退出；根命令
        # 不接受位置参数形式的初始 prompt，交互式 TUI 也没有从命令行预置首条消息的入口
        # （见 MAINTAINER_GUIDE「Kimi 接力目标限制」）。因此接力到 Kimi 只能走非交互模式：
        # 用 --add-dir 把源历史目录纳入工作区，让 Kimi 读取原始历史并把最后一个未完成任务
        # 跑完打印结果，随后用户可用 `kimi -c` 在同一会话上继续交互。
        history_dir = os.path.dirname(handoff.history_path)
        return LaunchPlan(
            argv=(
                self.executable,
                "--add-dir",
                history_dir,
                *self.auto_approve_args,
                "-p",
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
            ),
            cwd=usable_cwd(cwd),
        )
