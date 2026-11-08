"""Unified share transcript — implemented by SessKit.

``SCHEMA_ID`` is ``sesskit.transcript/v1``. Corral readers additionally accept
the host's legacy ``corral.share/v1`` alias, supplied via the host extension
(``legacy_schema_ids``); neutral SessKit readers accept only ``SCHEMA_ID``.
"""

from __future__ import annotations

import re

from sesskit.models import format_message_time
from sesskit.transcript import *  # noqa: F403
from sesskit.transcript import EVENT_TYPES, SCHEMA_ID, count_events, load_events

from corral.runtime.host_extension import LEGACY_SCHEMA_IDS as _HOST_LEGACY_IDS

try:
    from sesskit.transcript import accepted_schema_ids as _accepted_schema_ids

    LEGACY_SCHEMA_IDS = tuple(
        ident for ident in _accepted_schema_ids(legacy_ids=_HOST_LEGACY_IDS) if ident != SCHEMA_ID
    )
except ImportError:
    # Older SessKit without the seam: fall back to its bundled legacy tuple.
    from sesskit.transcript import LEGACY_SCHEMA_IDS

__all__ = [
    "EVENT_TYPES",
    "LEGACY_SCHEMA_IDS",
    "MESSAGE_ORIGINS",
    "SCHEMA_ID",
    "annotate_conversation",
    "count_events",
    "load_events",
    "message_origin",
]


#: Sender-attribution values produced by :func:`message_origin`.
#: ``agent`` means the message content was authored by another (calling)
#: agent and relayed via Corral (handoff wrapper, or a W2 control-run mark),
#: not typed by a human. ``system`` means Corral/runtime automation noise.
#: ``assistant`` is the serving runtime's own output; it is never a
#: calling-agent submission. ``unknown`` is empty or undecidable text.
MESSAGE_ORIGINS = ("human", "agent", "system", "assistant", "unknown")

#: Visible correlation marker every Corral control run appends to its input.
_RUN_MARKER = re.compile(r"\[Corral run=(\S+) message=\S+\]")


def message_origin(
    role: object,
    text: object,
    *,
    activity_origin: object = None,
    agent_id: object = None,
    run_ref: object = None,
) -> dict:
    """Attribute one conversation message to its sender.

    Pure projection over already-extracted message text plus Corral-owned
    markers; it never re-parses native history. ``activity_origin`` is the
    SessKit typed ``user_message`` origin (``human``/``system``/``injected``)
    when the caller has it. ``run_ref``/``agent_id`` is the W2 seam: when the
    control layer knows a message was submitted by a delegated run, passing
    its run reference (and actor id when known) marks the message ``agent``
    with ``high`` confidence, overriding the text default. Unmarked surviving
    user text defaults to ``human`` because SessKit already filtered system
    injections at parse time. Returned dict is JSON-serializable: ``origin``,
    ``agent_id``, ``confidence`` and ``evidence``.
    """
    # Lazy imports: corral.titles imports corral.models (not this module), but
    # transcript.py sits on the agent_api fast path, so keep import-time
    # weight unchanged and resolve these stdlib-only modules at call time.
    from corral.runtime.host_extension import handoff_task
    from corral.titles import is_title_generation_prompt

    evidence: list[str] = []
    if run_ref is not None or agent_id is not None:
        if run_ref is not None:
            evidence.append(f"control.run={run_ref}")
        if agent_id is not None:
            evidence.append(f"agent_id={agent_id}")
        return {
            "origin": "agent",
            "agent_id": agent_id if isinstance(agent_id, str) else None,
            "confidence": "high",
            "evidence": evidence,
        }
    if role == "assistant":
        return {
            "origin": "assistant",
            "agent_id": None,
            "confidence": "high",
            "evidence": ["role=assistant"],
        }
    body = text if isinstance(text, str) else ""
    try:
        from corral.control import parse_agent_header

        header_sender = parse_agent_header(body)
    except Exception:
        header_sender = None
    if header_sender is not None:
        return {
            "origin": "agent",
            "agent_id": header_sender,
            "confidence": "high",
            "evidence": ["corral.agent_header"],
        }
    run_mark = _RUN_MARKER.search(body)
    if run_mark is not None:
        return {
            "origin": "agent",
            "agent_id": None,
            "confidence": "medium",
            "evidence": [f"corral.run_marker={run_mark.group(1)}"],
        }
    if is_title_generation_prompt(body):
        return {
            "origin": "system",
            "agent_id": None,
            "confidence": "high",
            "evidence": ["corral.title_prompt_marker"],
        }
    if handoff_task(body) is not None:
        return {
            "origin": "agent",
            "agent_id": None,
            "confidence": "high",
            "evidence": ["corral.handoff_wrapper"],
        }
    if activity_origin in ("injected", "system"):
        return {
            "origin": "system",
            "agent_id": None,
            "confidence": "high",
            "evidence": [f"sesskit.activity.origin={activity_origin}"],
        }
    if not body.strip():
        return {
            "origin": "unknown",
            "agent_id": None,
            "confidence": "low",
            "evidence": ["empty-text"],
        }
    return {
        "origin": "human",
        "agent_id": None,
        "confidence": "medium",
        "evidence": ["default:conversation-user-text"],
    }


def _message_parts(message: object) -> tuple[object, object, object]:
    """Split a ConversationMessage or plain dict into ``(role, text, timestamp)``."""
    if isinstance(message, dict):
        timestamp = message.get("timestamp", message.get("mtime"))
        return message.get("role"), message.get("text"), timestamp
    return getattr(message, "role", None), getattr(message, "text", None), getattr(message, "timestamp", None)


def _run_mark_parts(mark: object) -> tuple[object, object]:
    """Split one ``run_marks`` entry into ``(agent_id, run_ref)``."""
    if isinstance(mark, dict):
        return mark.get("agent_id"), mark.get("run_ref")
    if isinstance(mark, str):
        return None, mark
    return None, None


def annotate_conversation(
    messages: list,
    *,
    activity_origins: list | None = None,
    run_marks: list | None = None,
    include_evidence: bool = True,
) -> list[dict]:
    """Project conversation messages to ``show``-shaped dicts with origin.

    Pure helper for W2/coordinator wiring into ``agent_api.cmd_show`` (which
    W1 must not edit): output entries carry the existing ``show`` message
    keys (``role``/``text``/``time``/``mtime``) plus additive ``origin``,
    with ``agent_id`` only when known and ``confidence``/``evidence`` only
    when ``include_evidence`` is true (keep ``list --compact``-style compact
    callers on ``origin`` alone). ``activity_origins`` and ``run_marks`` must
    be parallel to ``messages`` when given; a length mismatch raises
    ``ValueError`` rather than silently misaligning attribution.
    """
    if activity_origins is not None and len(activity_origins) != len(messages):
        raise ValueError(
            f"activity_origins length {len(activity_origins)} != messages length {len(messages)}"
        )
    if run_marks is not None and len(run_marks) != len(messages):
        raise ValueError(f"run_marks length {len(run_marks)} != messages length {len(messages)}")
    annotated: list[dict] = []
    for index, message in enumerate(messages):
        role, text, timestamp = _message_parts(message)
        agent_id, run_ref = _run_mark_parts(run_marks[index]) if run_marks is not None else (None, None)
        origin = message_origin(
            role,
            text,
            activity_origin=activity_origins[index] if activity_origins is not None else None,
            agent_id=agent_id,
            run_ref=run_ref,
        )
        entry = {
            "role": role,
            "text": text if isinstance(text, str) else "",
            "time": format_message_time(timestamp) if timestamp else None,
            "mtime": timestamp,
            "origin": origin["origin"],
        }
        if origin["agent_id"] is not None:
            entry["agent_id"] = origin["agent_id"]
        if include_evidence:
            entry["confidence"] = origin["confidence"]
            entry["evidence"] = origin["evidence"]
        annotated.append(entry)
    return annotated
