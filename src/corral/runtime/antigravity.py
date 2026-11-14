"""Google Antigravity CLI (`agy`) runtime adapter."""

from __future__ import annotations

import os
from pathlib import Path

from corral import liveness
from corral.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from corral.runtime.base import BaseRuntime, composer_between_rules, usable_cwd


def _handoff_add_dir(raw_history_path: str) -> str:
    """Return an `--add-dir` root covering the handoff file and sibling brain artifacts."""
    abs_path = os.path.abspath(raw_history_path)
    if os.path.isdir(abs_path):
        return abs_path
    path = Path(abs_path)
    parts = path.parts
    for idx in range(len(parts) - 2, 0, -1):
        if parts[idx] == "brain" and idx + 1 < len(parts):
            # Covers `<root>/brain/<cid>/.system_generated/logs/transcript_full.jsonl`
            # plus sibling brain artifacts without exposing neighboring sessions.
            return str(Path(*parts[: idx + 2]))
    return os.path.dirname(abs_path)


class AntigravityRuntime(BaseRuntime):
    id = "antigravity"
    display_name = "Antigravity"
    executable = "agy"
    history_reading_hint = (
        "Google Antigravity CLI conversation data (~/.gemini/antigravity-cli/ or "
        "$ANTIGRAVITY_APP_DATA_DIR): full transcript JSONL at "
        "brain/<conversation_id>/.system_generated/logs/transcript_full.jsonl "
        "(step_index, source, type, status, created_at, content, thinking, tool_calls, "
        "error, media), plus conversations/<conversation_id>.db and "
        "conversation_summaries.db. Open read-only; never mutate source history."
    )
    auto_approve_args = ("--dangerously-skip-permissions",)
    interrupt_keys = ("Escape",)
    model_flag = "--model"

    SUBCOMMANDS: frozenset[str] = frozenset({
        "acp",
        "agent",
        "agents",
        "changelog",
        "help",
        "install",
        "mcp",
        "mic-serve",
        "models",
        "plugin",
        "plugins",
        "remote-control",
        "update",
    })
    _AUTO_APPROVE_SUBCOMMANDS: frozenset[str] = frozenset({"acp"})
    _INFO_FLAGS: frozenset[str] = frozenset({"-h", "--help", "--version"})

    def composer_region(self, screen: str) -> str | None:
        return composer_between_rules(screen)

    def scan_signature(self) -> object | None:
        from corral.scan import antigravity as scan_antigravity

        return scan_antigravity.scan_signature()

    def scan_sessions(self, limit: int, keep_ids: set[str] | None = None) -> list[SessionInfo]:
        from corral.runtime.host_extension import corral_host_extension
        from corral.runtime.sesskit_bridge import call_scan
        from corral.scan import antigravity as scan_antigravity

        sessions = call_scan(
            scan_antigravity.scan_sessions,
            limit=limit,
            keep_ids=keep_ids,
            host=corral_host_extension(),
        )
        liveness.annotate_antigravity_sessions(sessions, force_override=True)
        return sessions

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        from corral.runtime.sesskit_bridge import load_runtime_conversation

        return load_runtime_conversation(session)

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "--conversation",
                str(session["id"]),
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_continue_plan(self, session: SessionInfo, instruction: str) -> LaunchPlan:
        """Non-interactive single-turn continuation plan via `agy -p`."""
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "-p",
                instruction,
                "--conversation",
                str(session["id"]),
                "--output-format",
                "json",
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "--add-dir",
                _handoff_add_dir(handoff.history_path),
                "-i",
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan(
            argv=(self.executable, *self.auto_approve_args),
            cwd=usable_cwd(cwd),
        )

    def compose_passthrough_argv(self, user_args: tuple[str, ...]) -> tuple[str, ...]:
        """Insert `--dangerously-skip-permissions` only where `agy` accepts it.

        Subcommands such as `models`, `mcp`, `plugin`, `help`, `update` reject
        `--dangerously-skip-permissions` (`Error: flags provided but not defined`),
        whereas root chat and `acp` declare it (`acp` expects it after `acp`).
        """
        if not user_args:
            return (self.executable, *self.auto_approve_args)
        if any(arg in self.auto_approve_args for arg in user_args):
            return (self.executable, *user_args)
        head = user_args[0]
        if head in self._INFO_FLAGS and len(user_args) == 1:
            return (self.executable, *user_args)
        if head in self.SUBCOMMANDS:
            if head in self._AUTO_APPROVE_SUBCOMMANDS:
                return (self.executable, head, *self.auto_approve_args, *user_args[1:])
            return (self.executable, *user_args)
        return (self.executable, *self.auto_approve_args, *user_args)

    def delete_session(self, session: SessionInfo) -> None:
        from corral.scan import antigravity as scan_antigravity

        scan_antigravity.delete_session(str(session.get("path") or session.get("id") or ""))
