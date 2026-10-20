"""Corral-owned delegation: exact targets, admission checks and result correlation.

Execution goes through Corral's hosted terminals (``SessionHub``); observation
consumes SessKit's normalized activity. No vendor control API is involved.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from corral.control_store import EXIT_NOT_FOUND, EXIT_USAGE, ControlError, digest
from corral.models import session_key
from corral.runtime.registry import ACTIVE_RUNTIME_IDS

MAX_PROMPT_CHARS = 100_000
_RESOLVE_DEPTH = 200
_EVENT_TEXT_LIMIT = 1_200
_RESULT_TEXT_LIMIT = 20_000
_MARKER = re.compile(r"\[Corral run=(run_[0-9a-f]+) message=([\w-]+)\]")


def marker(run_id: str, message_id: str) -> str:
    return f"[Corral run={run_id} message={message_id}]"


def wire_prompt(run_id: str, message_id: str, prompt: str) -> str:
    """The visible envelope lets SessKit evidence prove which run an input belongs to."""
    return f"{prompt}\n\n{marker(run_id, message_id)}"


def validate_prompt(text: str) -> str:
    if not text or not text.strip():
        raise ControlError("usage_error", "The prompt is empty.", EXIT_USAGE)
    if "\x00" in text:
        raise ControlError("usage_error", "The prompt contains a NUL byte.", EXIT_USAGE)
    if len(text) > MAX_PROMPT_CHARS:
        raise ControlError("usage_error", f"The prompt exceeds {MAX_PROMPT_CHARS} characters.", EXIT_USAGE,
                           hint="Write long material to a file in the project and reference its path in the brief.")
    return text


def caller_identity() -> dict:
    """The calling agent as Corral knows it; hosted panes export their own identity."""
    from corral.legacy_names import getenv

    runtime = getenv("RUNTIME") or None
    ident = getenv("SESSION_ID") or None
    actor = os.environ.get("CORRAL_ACTOR") or (f"{runtime}:{ident}" if runtime and ident else "local-user")
    return {"actor": actor, "runtime": runtime, "pane_ident": ident}


# -- targets -------------------------------------------------------------------


def resolve_target(registry, ref: str) -> dict:
    """Resolve one complete ``runtime:id`` without recency guessing."""
    runtime_id, sep, ident = str(ref or "").partition(":")
    if not sep or not ident:
        raise ControlError("usage_error", "Control writes need a complete runtime:id reference.", EXIT_USAGE,
                           hint="Copy runtime and id from search/show output, e.g. claude:<full-session-id>.")
    if runtime_id not in ACTIVE_RUNTIME_IDS:
        raise ControlError("usage_error", f"Unsupported runtime: {runtime_id}", EXIT_USAGE,
                           hint="Supported runtimes: " + ", ".join(sorted(ACTIVE_RUNTIME_IDS)))
    runtime = registry.get(runtime_id)
    sessions = runtime.scan_sessions(_RESOLVE_DEPTH, keep_ids={ident})
    matches = [s for s in sessions if s.get("id") == ident]
    if len(matches) != 1:
        raise ControlError("not_found", f"No session has the exact reference {ref}.", EXIT_NOT_FOUND,
                           hint="Prefixes are not accepted for writes; use the full id from search or show.")
    from corral import liveness

    liveness.annotate(matches)
    return matches[0]


def control_capability(session: dict) -> tuple[str, str]:
    """How a run would reach this session: (mode, reason)."""
    if session.get("live") and not session.get("keepalive_name"):
        return "external", "A process outside Corral owns this session; resuming would fork its history."
    if session.get("keepalive_name"):
        return "hosted", "Corral hosts this session; input goes to its existing terminal."
    return "resume", "The session is stopped; Corral resumes it natively in a hosted terminal."


def target_payload(session: dict) -> dict:
    mode, reason = control_capability(session)
    ref = session_key(session)
    stable = {"session_ref": ref, "cwd": str(session.get("cwd") or ""), "history": str(session.get("path") or ""),
              "host": session.get("keepalive_name") or None, "mode": mode}
    return {"session_ref": ref, "runtime": session.get("source"), "cwd": stable["cwd"],
            "history_path": stable["history"], "control": mode, "control_reason": reason,
            "version": "t1:" + digest(stable)[:20]}


def validate_directory(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ControlError("usage_error", "--cwd must be an absolute project path.", EXIT_USAGE)
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ControlError("invalid_directory", f"The project directory does not exist: {resolved}", EXIT_NOT_FOUND)
    return str(resolved)


def fresh_plan(runtime, cwd: str, model: str | None):
    """Adapter-owned launch arguments; a model applies to this launch only."""
    from corral.runtime import LaunchError

    plan = runtime.build_new_session_plan(cwd)
    if model:
        try:
            plan = runtime.with_model(plan, model)
        except LaunchError as exc:
            raise ControlError("model_selection_unsupported", str(exc), EXIT_USAGE,
                               hint="Omit --model to inherit the runtime's own default, or pick another runtime.") \
                from exc
    return plan


def self_target(session: dict, caller: dict) -> bool:
    name = str(session.get("keepalive_name") or "")
    return bool(caller.get("pane_ident")) and name.endswith("-" + str(caller["pane_ident"]))


# -- correlation -----------------------------------------------------------------


def own_message(text: str | None, run_id: str) -> str | None:
    match = _MARKER.search((text or "")[:400])
    return match.group(2) if match and match.group(1) == run_id else None


def _trim(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit} more chars]"


def compact_event(event) -> dict:
    out = {"type": event.type, "native_seq": event.seq, "ts": event.ts}
    if event.text:
        out["text"] = _trim(event.text, _EVENT_TEXT_LIMIT)
    if event.name:
        out["tool"] = event.name
    if event.call_id:
        out["call_id"] = event.call_id
    if event.result is not None:
        out["result"] = getattr(event.result, "status", None)
    if event.error is not None:
        out["error"] = {"kind": getattr(event.error, "kind", None),
                        "message": _trim(getattr(event.error, "message", None), 400)}
    if event.interaction is not None:
        out["interaction"] = {"purpose": event.interaction.purpose, "resolution": event.interaction.resolution,
                              "request_id": event.interaction.request_id}
    return out


def correlate(events: list, turns: list, run_id: str, baseline_seq: int) -> dict:
    """Bind this run's own inputs (marker after the baseline) to their native turns.

    Inputs from anyone else inside the bound range make attribution ``mixed``.
    Markers quoted in assistant or tool output never count as delivery.
    """
    fresh = [e for e in events if e.seq > baseline_seq]
    own = {e.seq: own_message(e.text, run_id) for e in fresh if e.type == "user_message"}
    own = {seq: mid for seq, mid in own.items() if mid}
    if not own:
        return {"recorded": False}
    first = min(own)
    last_own = max(own)
    turn = next((t for t in turns if t.start_seq <= last_own <= t.end_seq), None)
    end = turn.end_seq if turn else max(e.seq for e in fresh)
    selected = [e for e in fresh if first <= e.seq <= end]
    foreign = [e for e in selected if e.type == "user_message" and e.seq not in own and (e.text or "").strip()]
    pending = [e for e in selected if e.interaction is not None and e.interaction.resolution == "pending"]
    return {
        "recorded": True,
        "messages": sorted(set(own.values())),
        "first_seq": first, "end_seq": end,
        "turn_id": turn.turn_id if turn else None,
        "outcome": turn.outcome.status if turn else "in_progress",
        "outcome_evidence": turn.outcome.evidence.origin if turn else "unknown",
        "outcome_error": _trim(getattr(turn.outcome.error, "message", None), 400) if turn and turn.outcome.error
        else None,
        "attribution": "mixed" if foreign else "correlated",
        "current_turn": not any(e.type == "user_message" and e.seq > end for e in fresh),
        "pending_interaction": bool(pending),
        "events": selected,
    }


def build_result(observation: dict) -> dict:
    events = observation.get("events") or []
    replies = [e for e in events if e.type == "assistant_message" and (e.text or "").strip()]
    tools = [e for e in events if e.type == "tool_call"]
    errors = [compact_event(e) for e in events if e.error is not None]
    usage = {}
    models = []
    for event in events:
        if event.usage is None:
            continue
        if event.usage.model:
            models.append(event.usage.model)
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            value = getattr(event.usage, field, None)
            if isinstance(value, (int, float)):
                usage[field] = usage.get(field, 0) + value
    stamps = [e.ts for e in events if e.ts]
    return {
        "final_response": _trim(replies[-1].text, _RESULT_TEXT_LIMIT) if replies else None,
        "assistant_messages": len(replies),
        "tool_calls": len(tools),
        "tools_used": sorted({e.name for e in tools if e.name}),
        "errors": errors[-10:],
        "usage": usage or None,
        "observed_models": sorted(set(models)),
        "started_at": min(stamps) if stamps else None,
        "finished_at": max(stamps) if stamps else None,
        "turn_id": observation.get("turn_id"),
        "attribution": observation.get("attribution"),
        "outcome_evidence": observation.get("outcome_evidence"),
        "acceptance": "unverified",
    }
