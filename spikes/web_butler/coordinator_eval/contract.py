"""Coordinator command boundary: validate the coordinator's actions against the document,
ledger, projects and assistants.

The product executes nothing the coordinator proposes until it passes these checks; rejections
go back to the coordinator with reasons. Mechanics only — no judgement about *whether* an
action is wise.
"""

from __future__ import annotations

import re

# Required fields and their types for every action kind.
# "string"     → non-empty str
# "nullable_string" → str or None (None means omit assistant)
# "strings"    → list of str
# "boolean"    → bool
ACTION_FIELDS = {
    "create_task": {"id": "string", "project": "string", "assistant": "nullable_string",
                    "instruction": "string", "anchors": "strings", "depends_on": "strings"},
    "update_task": {"id": "string", "instruction": "string", "anchors": "strings"},
    "reanchor": {"id": "string", "anchors": "strings"},
    "steer": {"id": "string", "message": "string", "interrupt": "boolean"},
    "stop": {"id": "string", "reason": "string"},
    "reassign": {"id": "string", "assistant": "string", "instruction": "string", "reason": "string"},
    "ask": {"near": "string", "text": "string"},
    "mark_done": {"id": "string", "quote": "string", "evidence": "string", "summary": "string"},
    "reopen_as_followup": {"of": "string", "id": "string", "instruction": "string", "anchors": "strings"},
}

# Which ledger states each action may target.
LEGAL_STATES = {
    "update_task": {"queued", "blocked"},
    "reanchor": {"queued", "blocked", "running"},
    "steer": {"running"},
    "stop": {"queued", "blocked", "running"},
    "reassign": {"queued", "blocked", "running"},
    "mark_done": {"running"},
    "reopen_as_followup": {"done"},
}


def block_texts(document: list[str]) -> list[str]:
    """Plain text of each block as the owner sees it: markers removed, struck characters kept."""
    return [re.sub(r"<!--/?coordinator-->", "", t).replace("~~", "") for t in document]


def validate(actions: list[dict], document: list[str], ledger: list[dict], projects: dict,
             assistants: dict) -> list[str]:
    errors: list[str] = []
    blocks = block_texts(document)
    tasks = {t["id"]: t for t in ledger}
    components = {c for comps in projects.values() for c in comps}
    usable = {name for name, status in assistants.items() if status == "usable"}
    new_ids: set[str] = set()

    _NEEDS_ANCHORS = {"create_task", "update_task", "reanchor", "reopen_as_followup"}

    def quote_ok(q: str, where: str) -> None:
        if not q or not any(q in b for b in blocks):
            errors.append(f"{where}: quote {q!r} is not a contiguous span of one current block "
                          "(struck characters still occupy their positions)")

    def _check_type(val: object, expected: str, where: str) -> bool:
        """Return True iff *val* matches *expected* (from ACTION_FIELDS)."""
        if expected == "string":
            return isinstance(val, str) and val != ""
        if expected == "nullable_string":
            return val is None or isinstance(val, str)
        if expected == "strings":
            return isinstance(val, list) and all(isinstance(v, str) for v in val)
        if expected == "boolean":
            return isinstance(val, bool)
        return False  # unknown type name — already caught by the key-existence check

    for i, a in enumerate(actions):
        kind = a.get("type")
        where = f"action {i + 1} ({kind})"
        target = a.get("id") if kind != "reopen_as_followup" else a.get("of")

        # -- unknown action type --
        if kind not in ACTION_FIELDS:
            errors.append(f"{where}: unknown action type; allowed: "
                          f"{', '.join(sorted(ACTION_FIELDS))}")
            continue  # no further checks make sense

        # -- shape: required fields present and correctly typed --
        for field, ftype in ACTION_FIELDS[kind].items():
            val = a.get(field)
            if field not in a:
                errors.append(f"{where}: missing required field {field!r}")
            elif (ftype == "nullable_string" and val is not None and not isinstance(val, str)):
                errors.append(f"{where}: {field!r} must be a string or null, got {type(val).__name__}")
            elif not _check_type(val, ftype, where):
                errors.append(f"{where}: {field!r} must be {ftype}, got {type(val).__name__}"
                              f" {val!r}")

        # -- at least one anchor --
        if kind in _NEEDS_ANCHORS and len(a.get("anchors") or []) == 0:
            errors.append(f"{where}: needs at least one anchor quote")

        if kind in ("create_task", "reopen_as_followup"):
            nid = a.get("id")
            if not nid or nid in tasks or nid in new_ids:
                errors.append(f"{where}: new task id {nid!r} is missing or already used")
            new_ids.add(nid)
        if kind in LEGAL_STATES:
            t = tasks.get(target)
            if t is None:
                errors.append(f"{where}: task {target!r} does not exist")
            elif t["state"] not in LEGAL_STATES[kind]:
                errors.append(f"{where}: not allowed on a {t['state']} task (allowed: "
                              f"{', '.join(sorted(LEGAL_STATES[kind]))})")
        if kind == "create_task":
            if a.get("project") not in components:
                errors.append(f"{where}: project {a.get('project')!r} is not a known component")
            for dep in a.get("depends_on") or []:
                if dep not in tasks and dep not in new_ids:
                    errors.append(f"{where}: dependency {dep!r} does not exist")
                elif dep == a.get("id"):
                    errors.append(f"{where}: a task cannot depend on itself")
        if kind in ("create_task", "reassign") and a.get("assistant") not in (None, *usable):
            errors.append(f"{where}: assistant {a.get('assistant')!r} is not usable now")
        if kind == "reassign" and not a.get("assistant"):
            errors.append(f"{where}: reassign needs a usable assistant")
        for q in a.get("anchors") or []:
            quote_ok(q, where)
        if kind == "mark_done":
            quote_ok(a.get("quote"), where)
            report = (tasks.get(target) or {}).get("worker_report") or ""
            evidence = a.get("evidence") or ""
            if not evidence or evidence not in report:
                errors.append(f"{where}: evidence must be copied exactly from the worker report "
                              "and say how the result was checked")
        if kind == "ask":
            quote_ok(a.get("near"), where)
    return errors
