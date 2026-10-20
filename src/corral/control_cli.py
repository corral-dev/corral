"""``corral run …``: submit or create delegated work, then observe it durably.

Writes record intent first and hand execution to a detached supervisor
(``corral.control_worker``); every read is side-effect free and never starts,
repairs or reclaims anything.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from corral.control import (
    caller_identity,
    control_capability,
    fresh_plan,
    resolve_target,
    self_target,
    target_payload,
    validate_directory,
    validate_prompt,
)
from corral.control_store import (
    EXIT_CONFLICT,
    EXIT_NOT_FOUND,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    MAX_ACTIVE_RUNS,
    SETTLED,
    ControlError,
    RunStore,
    digest,
)
from corral.runtime.registry import ACTIVE_RUNTIME_IDS

_SUPERVISOR_CONFIRM = 4.0
_WAIT_POLL = 0.5
_MAX_WAIT = 600


def _bin() -> str:
    from corral.agent_api import _bin as agent_bin

    return agent_bin()


def _ok(data) -> dict:
    from corral.agent_api import _ok as agent_ok

    return agent_ok(data)


def _read_prompt(args) -> str:
    inline, path = getattr(args, "prompt", None), getattr(args, "prompt_file", None)
    if bool(inline) == bool(path):
        raise ControlError("usage_error", "Pass exactly one of --prompt or --prompt-file.", EXIT_USAGE)
    if inline:
        return validate_prompt(inline)
    if path == "-":
        if sys.stdin.isatty():
            raise ControlError("usage_error", "--prompt-file - needs piped stdin.", EXIT_USAGE)
        return validate_prompt(sys.stdin.read())
    try:
        with open(path, encoding="utf-8") as handle:
            return validate_prompt(handle.read())
    except OSError as exc:
        raise ControlError("usage_error", f"Cannot read --prompt-file: {exc}", EXIT_USAGE) from exc


def _key(args) -> str:
    key = str(getattr(args, "idempotency_key", "") or "").strip()
    if not key or len(key) > 200 or any(ord(c) < 32 for c in key):
        raise ControlError("usage_error", "--idempotency-key must be 1-200 printable characters.", EXIT_USAGE,
                           hint="Use a stable key per intended delegation, e.g. review-2026-10-20-p0-login.")
    return key


def _supervisor_alive(store: RunStore, run_id: str) -> bool:
    from corral.control_worker import lock_held

    return lock_held(store, "run:" + run_id)


def _spawn_supervisor(store: RunStore, run_id: str) -> bool:
    """Start the detached owner and confirm its heartbeat; never runs on dry-run."""
    logs = store.root / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {k: v for k, v in os.environ.items()
           if k not in {"CORRAL_SESSION_ID", "SC_SESSION_ID", "CORRAL_RUNTIME", "SC_RUNTIME"}}
    with open(logs / f"{run_id}.log", "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "corral.control_worker", run_id],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            cwd=str(store.root), env=env, start_new_session=True, close_fds=True,
        )
    deadline = time.monotonic() + _SUPERVISOR_CONFIRM
    while time.monotonic() < deadline:
        if store.get(run_id).get("supervisor"):
            return True
        time.sleep(0.1)
    return False


def _next(run_id: str) -> list[str]:
    exe = _bin()
    return [f"{exe} run wait {run_id} --timeout 120", f"{exe} run status {run_id}",
            f"{exe} run events {run_id} --after 0"]


def public_run(store: RunStore, record: dict, *, compact: bool = False) -> dict:
    """The stable run view; heartbeat freshness and lock ownership are reported separately."""
    run_id = record["run_id"]
    sup = record.get("supervisor") or None
    view = {
        "run_id": run_id, "version": record["version"], "mode": record.get("mode"),
        "runtime": record.get("runtime"), "session_ref": record.get("session_ref"),
        "cwd": record.get("cwd"), "delivery": record.get("delivery"), "execution": record.get("execution"),
        "acceptance": record.get("acceptance", "unverified"), "settled": record.get("execution") in SETTLED,
        "questions": record.get("questions") or [], "error": record.get("error"),
        "requested_model": record.get("requested_model"), "observed_model": record.get("observed_model"),
        "attribution": record.get("attribution"), "turn_id": record.get("turn_id"),
        "model_check": record.get("model_check"), "finality": record.get("finality"),
        "queue_reason": record.get("queue_reason") if record.get("execution") == "queued" else None,
        "created_at": record.get("created_at"), "updated_at": record.get("updated_at"),
        "supervisor": {
            "alive": _supervisor_alive(store, run_id) if record.get("execution") not in SETTLED else False,
            "pid": sup.get("pid") if sup else None,
            "heartbeat_at": sup.get("heartbeat_at") if sup else None,
        },
    }
    if not compact:
        messages = store.messages(run_id)
        view["messages"] = messages[-20:]
        view["messages_truncated"] = len(messages) > 20
        view["host"] = record.get("host")
        view["history_path"] = record.get("history_path")
    return view


# -- writes ----------------------------------------------------------------------


def _guard_capacity(store: RunStore) -> None:
    live = [r for r in store.unsettled() if _supervisor_alive(store, r["run_id"])]
    if len(live) >= MAX_ACTIVE_RUNS:
        raise ControlError("capacity_exceeded",
                           f"{len(live)} delegated runs are already active (max {MAX_ACTIVE_RUNS}).", EXIT_CONFLICT,
                           hint="Wait for a run to settle or interrupt one.",
                           next_commands=[f"{_bin()} run list --top 20"])


def _admit(store: RunStore, actor: str, key: str, spec: dict, initial: dict, args, target: dict | None) -> dict:
    existing = store.find(key)
    if existing is None and not getattr(args, "dry_run", False):
        _guard_capacity(store)
    if getattr(args, "dry_run", False):
        if existing is not None and existing.pop("_digest") != digest(spec):
            raise ControlError("idempotency_conflict", "This idempotency key already identifies a different request.",
                               EXIT_CONFLICT)
        planned = existing or {"run_id": None, "version": None, **initial}
        return _ok({"status": "existing" if existing else "planned", "dry_run": True,
                    "run": {k: planned.get(k) for k in ("run_id", "version", "mode", "runtime", "session_ref", "cwd",
                                                         "delivery", "execution", "requested_model")},
                    "target": target, "launch": spec.get("launch_preview")})
    record, created = store.create(actor, key, spec, initial)
    confirmed = True
    if record["execution"] not in SETTLED and not _supervisor_alive(store, record["run_id"]):
        confirmed = _spawn_supervisor(store, record["run_id"])
    view = public_run(store, store.get(record["run_id"]))
    data = {"status": "created" if created else "existing", "dry_run": False, "run": view, "target": target,
            "next_commands": _next(record["run_id"])}
    if not confirmed:
        data["warnings"] = [f"Supervisor heartbeat not seen within {_SUPERVISOR_CONFIRM:.0f}s; "
                            f"check {_bin()} run status {record['run_id']}."]
    return _ok(data)


def cmd_submit(args, registry) -> dict:
    prompt = _read_prompt(args)
    key = _key(args)
    store = RunStore()
    caller = caller_identity()
    existing = store.find(key)
    if existing is not None:
        saved = store.get(existing["run_id"], private=True)
        spec = {**saved["spec"], "session_ref": args.target, "prompt": prompt,
                "on_busy": args.on_busy, "queue_timeout": max(0, args.queue_timeout), "caller": caller,
                "task_ref": getattr(args, "task_ref", None)}
        # Reconcile immutable input before consulting a target that may now be
        # hosted, missing or executing. A retry is not a fresh authorization.
        return _admit(store, caller["actor"], key, spec, saved, args, None)
    session = resolve_target(registry, args.target)
    target = target_payload(session)
    caller = caller_identity()
    mode, reason = control_capability(session)
    dry = getattr(args, "dry_run", False)
    if mode == "external":
        raise ControlError("external_session", reason, EXIT_CONFLICT,
                           hint="Wait for its owner to exit, then retry; Corral never forks a live history.",
                           next_commands=[f"{_bin()} show {target['session_ref']} --messages 4"])
    if self_target(session, caller):
        raise ControlError("self_target", "A session cannot delegate to itself.", EXIT_CONFLICT)
    if not os.path.isdir(target["cwd"]):
        raise ControlError("project_missing", f"The session's project directory is gone: {target['cwd']}",
                           EXIT_NOT_FOUND)
    store = RunStore()
    prompt_digest = digest(prompt)
    if store.find(key) is None and not args.allow_concurrent:
        twins = [r for r in store.for_target(target["session_ref"]) if r.get("prompt_digest") == prompt_digest]
        if twins:
            raise ControlError(
                "duplicate_active_run", "An unsettled run already carries this brief to this session.", EXIT_CONFLICT,
                hint="Observe the existing run instead of submitting again; pass --allow-concurrent only on purpose.",
                details={"runs": [public_run(store, r, compact=True) for r in twins]},
                next_commands=[f"{_bin()} run status {twins[0]['run_id']}"])
    if not dry and args.if_version != target["version"]:
        raise ControlError(
            "confirmation_required" if not args.if_version else "conflict",
            "Submit needs --if-version from a fresh read of this exact target.",
            EXIT_USAGE if not args.if_version else EXIT_CONFLICT,
            hint="Run the same submit with --dry-run, inspect target, then pass its target.version.",
            details={"target": target},
            next_commands=[f"{_bin()} run submit {target['session_ref']} --dry-run "
                           f"--prompt-file <brief> --idempotency-key {key}"],
        )
    spec = {"mode": "submit", "runtime": target["runtime"], "session_ref": target["session_ref"],
            "cwd": target["cwd"], "history_path": target["history_path"], "prompt": prompt,
            "on_busy": args.on_busy, "queue_timeout": max(0, args.queue_timeout), "caller": caller,
            "task_ref": getattr(args, "task_ref", None)}
    initial = {"mode": "submit", "prompt_digest": prompt_digest, "runtime": target["runtime"],
               "session_ref": target["session_ref"],
               "cwd": target["cwd"], "history_path": target["history_path"], "delivery": "accepted",
               "execution": "queued", "acceptance": "unverified", "requested_model": None,
               "task_ref": spec["task_ref"], "target_control": mode}
    return _admit(store, caller["actor"], key, spec, initial, args, target)


_PROTECTED_DIRS = ("", "Desktop", "Documents")


def _guard_create_directory(cwd: str) -> None:
    from pathlib import Path

    home = Path.home().resolve()
    protected = {Path("/").resolve(), *(home / name for name in _PROTECTED_DIRS)}
    if Path(cwd) in protected:
        raise ControlError("directory_not_allowed",
                           "Agents are not launched in /, the home root, Desktop or Documents.", EXIT_USAGE,
                           hint="Pass the project's own directory.")


def cmd_create(args, registry) -> dict:
    from corral import embed

    prompt = _read_prompt(args)
    key = _key(args)
    if args.runtime not in ACTIVE_RUNTIME_IDS:
        raise ControlError("usage_error", f"Unsupported runtime: {args.runtime}", EXIT_USAGE,
                           hint="Supported runtimes: " + ", ".join(sorted(ACTIVE_RUNTIME_IDS)))
    store = RunStore()
    caller = caller_identity()
    model = (args.model or "").strip() or None
    existing = store.find(key)
    if existing is not None:
        saved = store.get(existing["run_id"], private=True)
        spec = {**saved["spec"], "runtime": args.runtime, "prompt": prompt, "model": model,
                "cwd": str(Path(args.cwd).expanduser().resolve()), "caller": caller,
                "task_ref": getattr(args, "task_ref", None)}
        return _admit(store, caller["actor"], key, spec, saved, args, None)
    runtime = registry.get(args.runtime)
    if not runtime.is_available():
        raise ControlError("runtime_unavailable", f"{runtime.display_name} is not installed on this machine.",
                           EXIT_NOT_FOUND)
    if not embed.available():
        raise ControlError("tmux_unavailable", "Corral hosting needs tmux, which is not available.", EXIT_NOT_FOUND)
    cwd = validate_directory(args.cwd)
    _guard_create_directory(cwd)
    model = (args.model or "").strip() or None
    store = RunStore()
    if runtime.id in {"opencode", "cursor"} and store.find(key) is None:
        unbound = [r for r in store.unsettled()
                   if r.get("mode") == "create" and r.get("runtime") == runtime.id and r.get("cwd") == cwd
                   and not r.get("session_ref")]
        if unbound:
            raise ControlError(
                "concurrent_unbound_create",
                f"Another new {runtime.display_name} session in this project has no exact identity yet.",
                EXIT_CONFLICT, hint="Wait until that run reports a session_ref, then create again.",
                next_commands=[f"{_bin()} run wait {unbound[0]['run_id']} --timeout 60"])
    plan = fresh_plan(runtime, cwd, model)
    caller = caller_identity()
    spec = {"mode": "create", "runtime": runtime.id, "cwd": cwd, "model": model, "prompt": prompt,
            "on_busy": "steer", "queue_timeout": 0, "caller": caller, "task_ref": getattr(args, "task_ref", None),
            "launch_preview": {"argv": list(plan.argv), "cwd": plan.cwd}}
    initial = {"mode": "create", "runtime": runtime.id, "session_ref": None, "cwd": cwd, "delivery": "accepted",
               "execution": "queued", "acceptance": "unverified", "requested_model": model,
               "model_evidence": "pending" if model else "runtime_default", "task_ref": spec["task_ref"]}
    target = {"session_ref": None, "runtime": runtime.id, "cwd": cwd, "control": "create",
              "control_reason": "Corral launches a new hosted session for this run."}
    return _admit(store, caller["actor"], key, spec, initial, args, target)


def _enqueue(args, body: dict) -> dict:
    store = RunStore()
    record = store.get(args.run_id)
    if not args.if_version:
        raise ControlError("confirmation_required", "Pass --if-version from a fresh run status.", EXIT_USAGE,
                           next_commands=[f"{_bin()} run status {args.run_id}"])
    message, created = store.enqueue(args.run_id, caller_identity()["actor"], _key(args), body, args.if_version)
    if record["execution"] not in SETTLED and not _supervisor_alive(store, args.run_id):
        raise ControlError("supervisor_lost", "The message is queued but no supervisor owns this run.",
                           EXIT_CONFLICT, details={"message": message},
                           next_commands=[f"{_bin()} run recover {args.run_id}"])
    return _ok({"status": "created" if created else "existing", "message": message,
                "run": public_run(store, store.get(args.run_id)), "next_commands": _next(args.run_id)})


def cmd_message(args, registry) -> dict:
    prompt = _read_prompt(args)
    record = RunStore().get(args.run_id)
    if record.get("questions"):
        raise ControlError("waiting_input", "The run has a pending native question; answer it with run answer.",
                           EXIT_CONFLICT, details={"questions": record["questions"]},
                           next_commands=[f"{_bin()} run status {args.run_id}"])
    if record.get("model_check") == "mismatch":
        raise ControlError("model_mismatch", "The session runs a different model than requested; steering is blocked.",
                           EXIT_CONFLICT, details={"requested_model": record.get("requested_model"),
                                                   "observed_model": record.get("observed_model")})
    return _enqueue(args, {"action": "steer", "prompt": prompt})


def cmd_answer(args, registry) -> dict:
    try:
        answers = json.loads(args.answers_json)
    except json.JSONDecodeError as exc:
        raise ControlError("usage_error", f"--answers-json is not valid JSON: {exc}", EXIT_USAGE) from exc
    if not isinstance(answers, list) or not all(isinstance(a, dict) and a.get("question_id") for a in answers):
        raise ControlError("usage_error", "--answers-json must be a list of {question_id, selected?, text?}.",
                           EXIT_USAGE)
    record = RunStore().get(args.run_id)
    known = {q.get("request_id") for q in record.get("questions") or []}
    if args.request_id not in known:
        raise ControlError("question_not_pending", "That request_id is not a pending question of this run.",
                           EXIT_CONFLICT, details={"questions": record.get("questions") or []},
                           next_commands=[f"{_bin()} run status {args.run_id}"])
    return _enqueue(args, {"action": "answer", "request_id": args.request_id, "answers": answers})


def cmd_interrupt(args, registry) -> dict:
    return _enqueue(args, {"action": "interrupt"})


def cmd_recover(args, registry) -> dict:
    store = RunStore()
    record = store.get(args.run_id)
    if record["execution"] in SETTLED and record["execution"] != "unknown":
        return _ok({"status": "settled", "run": public_run(store, record)})
    if _supervisor_alive(store, args.run_id):
        return _ok({"status": "unchanged", "run": public_run(store, record)})
    if record["execution"] == "unknown":
        store.update(args.run_id, {"execution": "running"})
    confirmed = _spawn_supervisor(store, args.run_id)
    data = {"status": "recovered", "run": public_run(store, store.get(args.run_id)),
            "next_commands": _next(args.run_id)}
    if not confirmed:
        data["warnings"] = ["Supervisor heartbeat not confirmed yet."]
    return _ok(data)


# -- reads --------------------------------------------------------------------------


def cmd_status(args, registry) -> dict:
    store = RunStore()
    runs, failures = [], {}
    for run_id in dict.fromkeys(args.run_ids):
        try:
            runs.append(public_run(store, store.get(run_id), compact=args.compact))
        except ControlError as exc:
            failures[run_id] = exc.code
    if not runs and failures:
        raise ControlError("not_found", "None of the requested runs exist.", EXIT_NOT_FOUND,
                           details={"failed_runs": failures})
    return _ok({"runs": runs, "failed_runs": failures, "partial": bool(failures)})


def cmd_events(args, registry) -> dict:
    store = RunStore()
    page = store.events(args.run_id, max(0, args.after), max(1, min(args.limit, 500)))
    record = store.get(args.run_id)
    return _ok({"run_id": args.run_id, "execution": record["execution"], "settled": record["execution"] in SETTLED,
                **page})


def _actionable(store: RunStore, record: dict, until: str) -> str | None:
    execution = record.get("execution")
    if execution in SETTLED:
        return execution
    if until == "settled":
        return None
    if record.get("delivery") == "rejected":
        return "rejected"
    if any(m["state"] == "unknown" for m in store.messages(record["run_id"])):
        return "delivery_uncertain"
    if execution == "waiting_input":
        latest = max((m for m in store.messages(record["run_id"]) if m["action"] in {"answer", "steer"}),
                     key=lambda m: m.get("created_at", 0), default=None)
        if latest is None or latest["state"] in {"rejected", "unknown"}:
            return "waiting_input"
        return None if latest.get("created_at", 0) > record.get("updated_at", 0) else "waiting_input"
    if not _supervisor_alive(store, record["run_id"]):
        return "supervisor_lost"
    return None


def cmd_wait(args, registry) -> dict:
    store = RunStore()
    timeout = max(0, min(args.timeout, _MAX_WAIT))
    deadline = time.monotonic() + timeout
    ids = list(dict.fromkeys(args.run_ids))
    while True:
        records = [store.get(run_id) for run_id in ids]
        ready = {r["run_id"]: reason for r in records if (reason := _actionable(store, r, args.until))}
        if ready or time.monotonic() >= deadline:
            break
        time.sleep(_WAIT_POLL)
    views = [public_run(store, r, compact=True) for r in records]
    if not ready:
        raise ControlError("wait_timeout", f"No run became actionable within {timeout}s; the work continues.",
                           EXIT_TIMEOUT, hint="A wait timeout never stops the target; wait again or read events.",
                           details={"runs": views}, next_commands=[f"{_bin()} run wait {' '.join(ids)} --timeout 120"])
    return _ok({"ready": ready, "runs": views})


def cmd_result(args, registry) -> dict:
    store = RunStore()
    record = store.get(args.run_id)
    if record["execution"] not in SETTLED:
        raise ControlError("result_not_ready", f"The run is still {record['execution']}.", EXIT_CONFLICT,
                           details={"run": public_run(store, record, compact=True)},
                           next_commands=[f"{_bin()} run wait {args.run_id} --timeout 120"])
    view = public_run(store, record)
    return _ok({**view, "result": record.get("result"),
                "note": "execution=completed means the delegated turn ended; review the work before "
                        "treating the task as done."})


def cmd_list(args, registry) -> dict:
    store = RunStore()
    if not store.exists():
        return _ok({"runs": [], "returned": 0})
    actor = caller_identity()["actor"] if args.mine else None
    records = store.recent(max(1, min(args.top, 200)) if not args.target else 10_000, actor)
    if args.target:
        records = [r for r in records if r.get("session_ref") == args.target][: max(1, min(args.top, 200))]
    if args.active:
        records = [r for r in records if r.get("execution") not in SETTLED]
    runs = [public_run(store, r, compact=True) for r in records]
    return _ok({"runs": runs, "returned": len(runs), "actor_filter": actor, "target_filter": args.target})


HANDLERS = {
    "run submit": cmd_submit, "run create": cmd_create, "run status": cmd_status, "run events": cmd_events,
    "run wait": cmd_wait, "run result": cmd_result, "run message": cmd_message, "run answer": cmd_answer,
    "run interrupt": cmd_interrupt, "run list": cmd_list, "run recover": cmd_recover,
}


def dispatch(command: str, args, registry) -> dict:
    from corral.agent_api import ApiError

    try:
        return HANDLERS[command](args, registry)
    except ControlError as exc:
        raise ApiError(exc.code, exc.message, exc.exit_code, hint=exc.hint, next_commands=exc.next_commands,
                       details=exc.details) from exc
