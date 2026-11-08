"""跨运行时共享的数据模型。

会话列表项与对话消息的权威实现在 SessKit；本模块保留 Corral 产品类型
（接力 / 启动）并 re-export SessKit 会话类型以保持 ``from corral.models import …``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sesskit.models import (  # noqa: F401 — re-export
    ConversationMessage,
    effective_session_time,
    format_message_time,
    make_session_info,
    parse_session_key,
    session_key,
)
from sesskit.models import (
    SessionInfo as _SessKitSessionInfo,
)

from corral.i18n import t

SHELL_RUNTIME_ID = "shell"


class SessionInfo(_SessKitSessionInfo, total=False):
    """SessKit session metadata plus Corral-owned hosting and attention state."""

    keepalive_name: str
    provisional: bool
    attention_kind: str
    attention_token: str | None
    attention_updated_at: float


#: Sender-attribution values produced by :func:`infer_session_origin`.
#: ``agent`` means the session was initiated by another agent (a native
#: subagent thread, or a Corral-relayed brief authored by a calling agent),
#: not by a human at the keyboard. ``system`` means Corral automation noise
#: (e.g. background title-generation prompts). ``assistant`` is not a
#: session-level value; it only appears per message (see corral.transcript).
SESSION_ORIGINS = ("human", "agent", "system", "unknown")


def _relation_kind_target(relation: object) -> tuple[str | None, str | None]:
    """Read ``(kind, target)`` off a SessKit SessionRelation or a plain dict."""
    if isinstance(relation, dict):
        kind = relation.get("kind")
        target = relation.get("target")
    else:
        kind = getattr(relation, "kind", None)
        target = getattr(relation, "target", None)
    kind = kind if isinstance(kind, str) and kind else None
    target = target if isinstance(target, str) and target else None
    return kind, target


def _first_user_text(messages: object) -> str | None:
    """First non-empty user-role text from raw conversation messages, if given."""
    if not messages:
        return None
    for message in messages:  # type: ignore[union-attr]
        if isinstance(message, dict):
            role, text = message.get("role"), message.get("text")
        else:
            role, text = getattr(message, "role", None), getattr(message, "text", None)
        if role == "user" and isinstance(text, str) and text.strip():
            return text
    return None


def infer_session_origin(
    session: SessionInfo | dict,
    messages: object = None,
    relations: object = None,
) -> dict:
    """Infer who initiated a session: a human, another agent, or automation.

    Pure Corral-side product attribution over SessKit normalized evidence; it
    never re-parses native history. ``messages`` is the raw conversation
    (``ConversationMessage`` objects or plain dicts) and ``relations`` is the
    output of ``sesskit.relations.session_relations`` (or plain
    ``{"kind": ..., "target": ...}`` dicts); both are optional so the cheap
    ``list`` path can call this on scan excerpts alone while ``show`` can pass
    the full conversation for the stronger raw-wrapper check.

    Note list-path excerpts are already handoff-peeled by the host extension,
    so an excerpt-only hit reports ``medium`` confidence; pass ``messages``
    for the ``high`` raw-wrapper check. ``agent_id`` is always None in W1: no
    actor records exist yet. W2 control runs will supply the submitting actor.
    Returned dict is JSON-serializable: ``initiated_by``, ``agent_id``,
    ``confidence`` (``high``/``medium``/``low``) and ``evidence``.
    """
    # Lazy imports: corral.titles imports this module, so a top-level import
    # would be circular; both targets are stdlib-only at module load.
    from corral.runtime.host_extension import handoff_task, split_handoff_text
    from corral.titles import is_title_generation_prompt

    evidence: list[str] = []
    first_user = session.get("first_user_msg") or ""  # type: ignore[union-attr]
    raw_first = _first_user_text(messages)
    try:
        from corral.control import parse_agent_header

        header_sender = parse_agent_header(raw_first) or parse_agent_header(first_user)
    except Exception:
        header_sender = None
    if header_sender is not None:
        return {
            "initiated_by": "agent",
            "agent_id": header_sender,
            "confidence": "high",
            "evidence": ["corral.agent_header"],
        }
    run_text = raw_first if raw_first is not None else first_user
    run_mark = re.search(r"\[Corral run=(\S+) message=\S+\]", run_text or "")
    if run_mark is not None:
        return {
            "initiated_by": "agent",
            "agent_id": None,
            "confidence": "medium",
            "evidence": [f"corral.run_marker={run_mark.group(1)}"],
        }
    if is_title_generation_prompt(first_user) or (
        raw_first is not None and is_title_generation_prompt(raw_first)
    ):
        return {
            "initiated_by": "system",
            "agent_id": None,
            "confidence": "high",
            "evidence": ["corral.title_prompt_marker"],
        }

    for relation in relations or ():
        kind, target = _relation_kind_target(relation)  # type: ignore[arg-type]
        if kind == "subagent":
            evidence.append("sesskit.relation=subagent" + (f":{target}" if target else ""))
    thread_source = session.get("thread_source")  # type: ignore[union-attr]
    if thread_source == "subagent":
        evidence.append("codex.thread_source=subagent")
    elif thread_source:
        evidence.append(f"thread_source={thread_source}")
    if evidence:
        return {
            "initiated_by": "agent",
            "agent_id": None,
            "confidence": "high",
            "evidence": evidence,
        }

    if raw_first is not None and handoff_task(raw_first) is not None:
        return {
            "initiated_by": "agent",
            "agent_id": None,
            "confidence": "high",
            "evidence": ["corral.handoff_wrapper"],
        }
    if first_user and handoff_task(first_user) is not None:
        return {
            "initiated_by": "agent",
            "agent_id": None,
            "confidence": "high",
            "evidence": ["corral.handoff_wrapper"],
        }
    if first_user:
        inherited, _digest = split_handoff_text(first_user)
        if inherited:
            return {
                "initiated_by": "agent",
                "agent_id": None,
                "confidence": "medium",
                "evidence": ["corral.handoff_wrapper_peeled"],
            }

    if (first_user or "").strip() or raw_first:
        return {
            "initiated_by": "human",
            "agent_id": None,
            "confidence": "medium",
            "evidence": ["default:parser-filtered-user-text"],
        }
    return {
        "initiated_by": "unknown",
        "agent_id": None,
        "confidence": "low",
        "evidence": ["no-user-message"],
    }


def is_shell_session(session: SessionInfo | dict) -> bool:
    """内嵌自由 shell 分屏占位，不是 AI 助手会话。"""
    return str(session.get("source") or "") == SHELL_RUNTIME_ID


@dataclass(frozen=True)
class Handoff:
    """源运行时导出的统一接力信息。"""

    source_runtime_id: str
    source_runtime_name: str
    title: str
    history_path: str
    original_cwd: str
    history_reading_hint: str
    conversation_digest: str = ""
    package_path: str = ""

    def render_prompt(self) -> str:
        """生成目标运行时收到的首条用户提示词。"""
        cwd = self.original_cwd or t("handoff.cwd_unknown")
        sections = [
            t("handoff.task", title=self.title),
            t("handoff.intro", name=self.source_runtime_name),
            t(
                "handoff.history",
                path=self.history_path,
                cwd=cwd,
                hint=self.history_reading_hint,
            ),
        ]
        if self.package_path:
            sections.append(t("handoff.package", path=self.package_path))
        if self.conversation_digest:
            sections.append(t("handoff.digest_intro", digest=self.conversation_digest))
            sections.append(t("handoff.read_with_digest"))
        else:
            sections.append(t("handoff.read_without_digest"))
        sections.append(t("handoff.closing"))
        return "\n\n".join(sections)


@dataclass(frozen=True)
class LaunchRequest:
    """用户在界面中确认的启动选择。"""

    session: SessionInfo
    target_runtime_id: str
    title: str
    force_new: bool = False
    copy_session: bool = False


@dataclass(frozen=True)
class NewSessionRequest:
    """用户在界面中确认的“空白新会话”选择：不关联任何已有会话历史。"""

    target_runtime_id: str
    cwd: str


@dataclass(frozen=True)
class LaunchPlan:
    """可独立测试、最终交给操作系统执行的启动计划。"""

    argv: tuple[str, ...]
    cwd: str | None
