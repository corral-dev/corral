"""``corral run …``: submit or create delegated work, then observe it durably.

Writes record intent first and hand execution to a detached supervisor
(``corral.control_worker``); every read is side-effect free and never starts,
repairs or reclaims anything.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from agent_session_control.supervisor import file_lock

from corral.control import (
    NO_SENDER,
    build_agent_text,
    caller_identity,
    control_capability,
    fresh_plan,
    resolve_sender,
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
# A2A quotas count live, unsettled runs. A target's queued runs share one
# supervisor owner, so these bounds stop a loop before it can occupy every
# machine-wide execution slot or generate an unbounded model-turn backlog.
MAX_A2A_ACTOR_RUNS = 4
MAX_A2A_TARGET_RUNS = 4


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

    if lock_held(store, "run:" + run_id):
        return True
    try:
        record = store.get(run_id, private=True)
    except ControlError:
        return False
    spec = record.get("spec") or {}
    target = spec.get("session_ref") if spec.get("mode") == "submit" else None
    # Additional runs on one target converge on its queue owner. They do not
    # need their own process or heartbeat to be supervised.
    return bool(target and lock_held(store, "target-supervisor:" + target))


def _spawn_supervisor(store: RunStore, run_id: str) -> bool:
    """Start the detached owner and confirm its heartbeat; never runs on dry-run."""
    logs = store.root / "logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {k: v for k, v in os.environ.items()
           if k not in {"CORRAL_SESSION_ID", "SC_SESSION_ID", "CORRAL_RUNTIME", "SC_RUNTIME"}}
    record = store.get(run_id, private=True)
    argv = [sys.executable, "-m", "corral.control_worker", run_id]
    if (record.get("spec") or {}).get("mode") == "submit":
        argv.append("--target-queue")
    with open(logs / f"{run_id}.log", "ab") as log:
        subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            cwd=str(store.root), env=env, start_new_session=True, close_fds=True,
        )
    deadline = time.monotonic() + _SUPERVISOR_CONFIRM
    while time.monotonic() < deadline:
        if _supervisor_alive(store, run_id):
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
        "from_agent": record.get("from_agent"), "last_from_agent": record.get("last_from_agent"),
        "report_back": record.get("report_back"),
        "model_check": record.get("model_check"), "finality": record.get("finality"),
        "queue_reason": record.get("queue_reason") if record.get("execution") == "queued" else None,
        "queued_behind": record.get("queued_behind") if record.get("execution") == "queued" else None,
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


def _is_a2a(record: dict) -> bool:
    # ``from_agent`` on a create run records who launched the session; the
    # creation brief itself is owner input, not peer mail (2026-11-10).
    return bool(record.get("a2a") or (record.get("mode") == "submit" and record.get("from_agent")))


def _owner_key(record: dict) -> str:
    if record.get("mode") == "submit" and record.get("session_ref"):
        return "target:" + str(record["session_ref"])
    return "run:" + str(record["run_id"])


def _occupancy(records: list[dict]) -> list[dict]:
    now = time.time()
    grouped: dict[str, dict] = {}
    for record in records:
        key = _owner_key(record)
        item = grouped.setdefault(key, {
            "owner": key,
            "target": record.get("session_ref"),
            "senders": set(),
            "runs": 0,
            "oldest_s": 0,
        })
        item["runs"] += 1
        sender = record.get("from_agent")
        if sender:
            item["senders"].add(sender)
        item["oldest_s"] = max(item["oldest_s"], max(0, int(now - float(record.get("created_at") or now))))
    return [{**item, "senders": sorted(item["senders"])} for item in grouped.values()]


def _capacity_error(quota: str, message: str, records: list[dict]) -> ControlError:
    return ControlError(
        "capacity_exceeded", message, EXIT_CONFLICT,
        hint="Wait for the existing A2A work to settle, or pause the peer channel before reviewing it.",
        details={"quota": quota, "occupancy": _occupancy(records)},
        next_commands=[f"{_bin()} run list --top 20", f"{_bin()} a2a status"],
    )


def _guard_capacity(store: RunStore, spec: dict | None = None) -> None:
    spec = spec or {}
    live = [record for record in store.unsettled() if _supervisor_alive(store, record["run_id"])]
    owners = {_owner_key(record) for record in live}
    new_owner = "target:" + str(spec.get("session_ref")) if spec.get("mode") == "submit" else None
    if len(owners) >= MAX_ACTIVE_RUNS and new_owner not in owners:
        raise _capacity_error(
            "machine", f"{len(owners)} delegated execution owners are already active (max {MAX_ACTIVE_RUNS}).", live,
        )
    if not _is_a2a(spec):
        return
    sender = spec.get("from_agent") or "unattributed"
    sender_live = [record for record in live if _is_a2a(record)
                   and (record.get("from_agent") or "unattributed") == sender]
    if len(sender_live) >= MAX_A2A_ACTOR_RUNS:
        raise _capacity_error(
            "sender", f"{sender} already has {len(sender_live)} live A2A runs (max {MAX_A2A_ACTOR_RUNS}).",
            sender_live,
        )
    target = spec.get("session_ref")
    target_live = [record for record in live if _is_a2a(record) and record.get("session_ref") == target]
    if target and len(target_live) >= MAX_A2A_TARGET_RUNS:
        raise _capacity_error(
            "target", f"{target} already has {len(target_live)} live A2A runs (max {MAX_A2A_TARGET_RUNS}).",
            target_live,
        )


def _admit(store: RunStore, actor: str, key: str, spec: dict, initial: dict, args, target: dict | None) -> dict:
    existing = store.find(key)
    if getattr(args, "dry_run", False):
        if existing is not None and existing.pop("_digest") != digest(spec):
            raise ControlError("idempotency_conflict", "This idempotency key already identifies a different request.",
                               EXIT_CONFLICT)
        planned = existing or {"run_id": None, "version": None, **initial}
        return _ok({"status": "existing" if existing else "planned", "dry_run": True,
                    "run": {k: planned.get(k) for k in ("run_id", "version", "mode", "runtime", "session_ref", "cwd",
                                                         "delivery", "execution", "requested_model", "from_agent")},
                    "target": target, "launch": spec.get("launch_preview")})
    needs_spawn = False
    if existing is None:
        # Capacity and intent creation are one cross-process admission step;
        # concurrent senders cannot both pass the same last free slot.
        with file_lock(str(store.root), "control-admission", blocking=True):
            _guard_capacity(store, spec)
            if spec.get("mode") == "submit" and spec.get("session_ref"):
                # The target owner makes its final empty-queue check under the
                # same handoff lock. A new record therefore either joins the
                # current owner or reliably starts the next one — never slips
                # between an owner's last read and its lock release.
                with file_lock(str(store.root), "target-admission:" + spec["session_ref"], blocking=True):
                    record, created = store.create(actor, key, spec, initial)
                    needs_spawn = created and not _supervisor_alive(store, record["run_id"])
            else:
                record, created = store.create(actor, key, spec, initial)
                needs_spawn = created
    else:
        record, created = store.create(actor, key, spec, initial)
    confirmed = True
    if record["execution"] not in SETTLED and (needs_spawn or not _supervisor_alive(store, record["run_id"])):
        confirmed = _spawn_supervisor(store, record["run_id"])
    view = public_run(store, store.get(record["run_id"]))
    data = {"status": "created" if created else "existing", "dry_run": False, "run": view, "target": target,
            "next_commands": _next(record["run_id"])}
    if not confirmed:
        data["warnings"] = [f"Supervisor heartbeat not seen within {_SUPERVISOR_CONFIRM:.0f}s; "
                            f"check {_bin()} run status {record['run_id']}."]
    return _ok(data)


def cmd_submit(args, registry, *, session: dict | None = None) -> dict:
    prompt = _read_prompt(args)
    key = _key(args)
    from_agent = resolve_sender(getattr(args, "from_agent", None), registry)
    no_footer = bool(getattr(args, "no_reply_footer", False))
    report_back = bool(getattr(args, "report_back", False)) and from_agent is not None
    explicit_none = str(getattr(args, "from_agent", "") or "").strip().lower() == NO_SENDER
    a2a = from_agent is not None or explicit_none
    store = RunStore()
    if a2a and store.a2a_paused():
        raise ControlError("a2a_paused", "Agent-to-agent messaging is paused; resume it before sending.",
                           EXIT_CONFLICT, hint=f"Resume with {_bin()} a2a resume; no message was created.",
                           next_commands=[f"{_bin()} a2a status", f"{_bin()} a2a resume"])
    send_text = build_agent_text(prompt, from_agent=from_agent, no_footer=no_footer, report_back=report_back,
                                 unattributed=explicit_none)
    caller = caller_identity()
    existing = store.find(key)
    if existing is not None:
        saved = store.get(existing["run_id"], private=True)
        spec = {**saved["spec"], "session_ref": args.target, "prompt": send_text,
                "on_busy": args.on_busy, "queue_timeout": max(0, args.queue_timeout), "caller": caller,
                "task_ref": getattr(args, "task_ref", None)}
        if from_agent is not None:
            spec["from_agent"] = from_agent
        else:
            spec.pop("from_agent", None)
        if report_back:
            spec["report_back"] = True
        else:
            spec.pop("report_back", None)
        # Reconcile immutable input before consulting a target that may now be
        # hosted, missing or executing. A retry is not a fresh authorization.
        return _admit(store, caller["actor"], key, spec, saved, args, None)
    session = session or resolve_target(registry, args.target)
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
    prompt_digest = digest(send_text)
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
            "cwd": target["cwd"], "history_path": target["history_path"], "prompt": send_text,
            "on_busy": args.on_busy, "queue_timeout": max(0, args.queue_timeout), "caller": caller,
            "task_ref": getattr(args, "task_ref", None)}
    if from_agent is not None:
        spec["from_agent"] = from_agent
    if a2a:
        spec["a2a"] = True
    if report_back:
        spec["report_back"] = True
    initial = {"mode": "submit", "prompt_digest": prompt_digest, "runtime": target["runtime"],
               "session_ref": target["session_ref"],
               "cwd": target["cwd"], "history_path": target["history_path"], "delivery": "accepted",
               "execution": "queued", "acceptance": "unverified", "requested_model": None,
               "task_ref": spec["task_ref"], "target_control": mode}
    if from_agent is not None:
        initial["from_agent"] = from_agent
    if a2a:
        initial["a2a"] = True
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

    key = _key(args)
    from_agent = resolve_sender(getattr(args, "from_agent", None), registry)
    # The creation brief is the owner's task, not a peer message: wrapping it
    # made a fresh session treat its first input as mail from another agent
    # and answer the creator. Attribution stays in the run spec.
    prompt = _read_prompt(args)
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
    if from_agent is not None:
        spec["from_agent"] = initial["from_agent"] = from_agent
    target = {"session_ref": None, "runtime": runtime.id, "cwd": cwd, "control": "create",
              "control_reason": "Corral launches a new hosted session for this run."}
    return _admit(store, caller["actor"], key, spec, initial, args, target)


def cmd_send(args, registry) -> dict:
    """One-step agent-to-agent message: resolve, read the version and submit once."""
    message = getattr(args, "message", None)
    if message and getattr(args, "prompt_file", None):
        raise ControlError("usage_error", "Pass the message inline or with --prompt-file, not both.", EXIT_USAGE)
    session = resolve_target(registry, args.target)
    target = target_payload(session)
    sender = resolve_sender(getattr(args, "from_agent", None), registry)
    if sender is not None and sender == target["session_ref"]:
        raise ControlError("self_target", "An agent cannot message its own session.", EXIT_CONFLICT)
    if sender is not None:
        _check_send_rate(sender, target["session_ref"])
    submit = argparse.Namespace(
        target=target["session_ref"], prompt=message, prompt_file=getattr(args, "prompt_file", None),
        idempotency_key=getattr(args, "idempotency_key", None) or f"send-{uuid.uuid4().hex}",
        if_version=target["version"], on_busy=args.on_busy, queue_timeout=args.queue_timeout,
        allow_concurrent=False, task_ref=None, dry_run=False, from_agent=sender or "none",
        no_reply_footer=bool(getattr(args, "no_reply_footer", False)),
        report_back=not bool(getattr(args, "no_report_back", False)), compact=getattr(args, "compact", False))
    result = cmd_submit(submit, registry, session=session)
    data = result.get("data") or {}
    run = data.get("run") or {}
    if run.get("run_id"):
        data["delivery_note"] = (
            f"The receiver's final answer will arrive in {sender} as a new message when its turn ends."
            if sender is not None and not getattr(args, "no_report_back", False)
            else "Observe the receiver's turn with run wait / run result."
        )
    return result


#: Burst guard for agent-to-agent messages (loops between two agents stop on
#: their own, as in Claude Code's cross-session messaging): at most this many
#: messages from one sender to one session per window.
SEND_BURST_LIMIT = 12
SEND_BURST_WINDOW = 600.0


def _check_send_rate(sender: str, target: str) -> None:
    store = RunStore()
    if not store.exists():
        return
    cutoff = time.time() - SEND_BURST_WINDOW
    recent = [r for r in store.recent(500, None)
              if r.get("session_ref") == target and r.get("from_agent") == sender
              and float(r.get("created_at") or 0) >= cutoff]
    if len(recent) >= SEND_BURST_LIMIT:
        raise ControlError(
            "rate_limited",
            f"{sender} already sent {len(recent)} messages to {target} in the last "
            f"{int(SEND_BURST_WINDOW // 60)} minutes.",
            EXIT_CONFLICT,
            hint="Batch the rest into one message, or wait for the receiver to answer.",
        )


def _enqueue(args, body: dict) -> dict:
    store = RunStore()
    record = store.get(args.run_id)
    if not args.if_version:
        raise ControlError("confirmation_required", "Pass --if-version from a fresh run status.", EXIT_USAGE,
                           next_commands=[f"{_bin()} run status {args.run_id}"])
    message, created = store.enqueue(args.run_id, caller_identity()["actor"], _key(args), body, args.if_version)
    from_agent = body.get("from_agent") if isinstance(body, dict) else None
    if from_agent is not None and record.get("last_from_agent") != from_agent:
        try:
            store.update(args.run_id, {"last_from_agent": from_agent})
        except ControlError:
            pass
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
    from_agent = resolve_sender(getattr(args, "from_agent", None), registry)
    no_footer = bool(getattr(args, "no_reply_footer", False))
    send_text = build_agent_text(prompt, from_agent=from_agent, no_footer=no_footer)
    body: dict = {"action": "steer", "prompt": send_text}
    if from_agent is not None:
        body["from_agent"] = from_agent
    return _enqueue(args, body)


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
                "from_agent": record.get("from_agent"), "last_from_agent": record.get("last_from_agent"),
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


def _a2a_status_data(store: RunStore) -> dict:
    live = [record for record in store.unsettled()
            if _is_a2a(record) and _supervisor_alive(store, record["run_id"])]
    return {
        **store.a2a_pause_state(),
        "active_runs": len(live),
        "execution_owners": len({_owner_key(record) for record in live}),
        "occupancy": _occupancy(live),
        "limits": {
            "per_sender": MAX_A2A_ACTOR_RUNS,
            "per_target": MAX_A2A_TARGET_RUNS,
            "machine_owners": MAX_ACTIVE_RUNS,
        },
    }


def cmd_a2a_status(args, registry) -> dict:
    return _ok(_a2a_status_data(RunStore()))


def cmd_a2a_pause(args, registry) -> dict:
    store = RunStore()
    store.set_a2a_paused(True, actor=caller_identity()["actor"])
    return _ok({**_a2a_status_data(store),
                "note": "New peer sends are refused; queued input and report-back stay deferred until resume."})


def cmd_a2a_resume(args, registry) -> dict:
    from corral import control_worker

    store = RunStore()
    store.set_a2a_paused(False, actor=caller_identity()["actor"])
    recovered = []
    warnings = []
    for record in store.unsettled():
        if not _is_a2a(record) or _supervisor_alive(store, record["run_id"]):
            continue
        if _spawn_supervisor(store, record["run_id"]):
            recovered.append(record["run_id"])
        else:
            warnings.append(f"Supervisor not confirmed for {record['run_id']}.")
    reported = []
    for candidate in store.recent(1000, None):
        record = store.get(candidate["run_id"], private=True)
        spec = record.get("spec") or {}
        if not spec.get("a2a") or not spec.get("report_back") or record.get("execution") not in SETTLED:
            continue
        if record.get("report_back"):
            continue
        outcome = control_worker.report_back(store, record["run_id"], registry=registry)
        if outcome:
            reported.append({"run_id": record["run_id"], **outcome})
    data = {**_a2a_status_data(store), "recovered": recovered, "reported": reported}
    if warnings:
        data["warnings"] = warnings
    return _ok(data)


HANDLERS = {
    "a2a status": cmd_a2a_status, "a2a pause": cmd_a2a_pause, "a2a resume": cmd_a2a_resume,
    "run submit": cmd_submit, "run create": cmd_create, "run status": cmd_status, "run events": cmd_events,
    "run wait": cmd_wait, "run result": cmd_result, "run message": cmd_message, "run answer": cmd_answer,
    "run interrupt": cmd_interrupt, "run list": cmd_list, "run recover": cmd_recover, "send": cmd_send,
}


def dispatch(command: str, args, registry) -> dict:
    from corral.agent_api import ApiError

    try:
        return HANDLERS[command](args, registry)
    except ControlError as exc:
        raise ApiError(exc.code, exc.message, exc.exit_code, hint=exc.hint, next_commands=exc.next_commands,
                       details=exc.details) from exc
