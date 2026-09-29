"""Pi coding agent 运行时适配器。"""

from __future__ import annotations

from corral.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from corral.runtime.base import BaseRuntime, usable_cwd
from corral.scan import pi as scan_pi

# 与 `--session` / `--continue` / `--resume` / `--no-session` 互斥；`--fork` 可以一起用。
_SESSION_ID_BLOCKING = {"--session", "--continue", "-c", "--resume", "-r", "--no-session"}


def _insert_after_approve(argv: tuple[str, ...], *items: str) -> tuple[str, ...]:
    insert_at = 1
    if len(argv) > 1 and argv[1] in ("--approve", "-a"):
        insert_at = 2
    return (*argv[:insert_at], *items, *argv[insert_at:])


def bind_hosted_ident(plan: LaunchPlan, ident: str) -> LaunchPlan:
    """钉托管 ident：新建/接力/分叉用 ``--session-id``，恢复用 ``--session``。

    ``--session-id`` 让首份落盘 id 与占位卡相同（与 ``--session`` 互斥，可与
    ``--fork`` 并用）。会话写回 Pi 默认 cwd 目录，原生 ``/resume`` 可见；
    分屏与 pane 的精确关联改由 corral-session-identity 扩展的 claim 提供
    （见 ``docs/design/PI_SESSION_IDENTITY_EXTENSION_DESIGN.md``），不再
    注入 ``--session-dir`` 小房间——隔离目录曾让 subagent 抢占主 pane、
    并把 ``/resume`` 圈死在单会话目录里。
    """
    if not plan.argv or plan.argv[0] != "pi":
        return plan
    argv = plan.argv
    if ident and "--session-id" not in argv and not _SESSION_ID_BLOCKING.intersection(argv):
        argv = _insert_after_approve(argv, "--session-id", ident)
    return LaunchPlan(argv, plan.cwd)


class PiRuntime(BaseRuntime):
    id = "pi"
    display_name = "Pi"
    executable = "pi"
    history_reading_hint = "Pi coding agent JSONL；首行 session header，沿 message entry 的 parentId 回溯当前活动分支。"
    auto_approve_args = ("--approve",)

    def scan_signature(self) -> object | None:
        return scan_pi.scan_signature()

    def scan_sessions(self, limit: int, keep_ids: set[str] | None = None) -> list[SessionInfo]:
        from corral.runtime.host_extension import corral_host_extension
        from corral.runtime.sesskit_bridge import call_scan

        return call_scan(
            scan_pi.scan_sessions, limit=limit, keep_ids=keep_ids, host=corral_host_extension()
        )

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        from corral.runtime.sesskit_bridge import load_runtime_conversation

        return load_runtime_conversation(session)

    def delete_session(self, session: SessionInfo) -> None:
        scan_pi.delete_session(str(session.get("path") or ""))

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            (self.executable, *self.auto_approve_args, "--session", str(session["path"])),
            usable_cwd(str(session.get("cwd") or "")),
        )

    def build_fork_plan(self, session: SessionInfo) -> LaunchPlan | None:
        return LaunchPlan(
            (self.executable, *self.auto_approve_args, "--fork", str(session["path"])),
            usable_cwd(str(session.get("cwd") or "")),
        )

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        return LaunchPlan(
            (self.executable, *self.auto_approve_args, handoff.render_prompt()),
            usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan((self.executable, *self.auto_approve_args), usable_cwd(cwd))
