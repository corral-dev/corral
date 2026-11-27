"""Main-session-list visibility helpers for workflow-owned child sessions.

Dual classification ensures workflow child sessions stay hidden from the main
session lists across TUI, remote/Apple clients and default ``corral list`` /
``corral search`` even after cache resets or cold scans:
1. Authoritative SQLite index (``workflow_sessions`` in ``workflows.sqlite3``).
2. Compact envelope marker embedded in the initial brief (``[corral-wf:<wf_id>:<step_id>]``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from corral_workflow.state_bridge import session_key
from corral_workflow.store import WorkflowStore

WORKFLOW_MARKER_PREFIX = "[corral-wf:"
_MARKER_RE = re.compile(r"\[corral-wf:([A-Za-z0-9_.-]+):([A-Za-z0-9_.:-]+)\]")


def workflow_marker(wf_id: str, step_id: str) -> str:
    return f"[corral-wf:{wf_id}:{step_id}]"


def parse_workflow_marker(text: str | None) -> tuple[str, str] | None:
    if not isinstance(text, str) or WORKFLOW_MARKER_PREFIX not in text:
        return None
    match = _MARKER_RE.search(text)
    if not match:
        return None
    return match.group(1), match.group(2)


def known_workflow_keys(root: Path | str | None = None) -> set[str]:
    """Read-only snapshot of registered workflow session keys (never creates storage)."""
    return WorkflowStore(root).known_workflow_session_keys()


def is_workflow_session(session: dict[str, Any], *, known_keys: set[str] | None = None) -> bool:
    """Return True when ``session`` belongs to a Corral workflow step."""
    if not isinstance(session, dict):
        return False
    if session.get("workflow_id"):
        return True
    keys = known_keys if known_keys is not None else known_workflow_keys()
    if keys:
        if session_key(session) in keys:
            return True
        keepalive_name = session.get("keepalive_name")
        if isinstance(keepalive_name, str) and keepalive_name in keys:
            return True
        raw_id = session.get("id")
        if isinstance(raw_id, str) and raw_id in keys:
            return True
    for field in ("first_user_msg", "title", "fallback_title"):
        if parse_workflow_marker(session.get(field)) is not None:
            return True
    return False
