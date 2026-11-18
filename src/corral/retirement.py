"""Durable retirement records and the decisions around them.

A retired hosted session must be distinguishable from one that crashed, and it
must be resumable afterwards (``docs/design/HOSTED_SESSION_LIFECYCLE_DESIGN.md``
§2 ``retired``, §5 INV-5/INV-9/INV-13, §6, §7.4). Today ``reclaim`` produces a
verdict and then kills; nothing is written down, so the log cannot answer why a
session ended or what it would take to bring it back. This module is that
memory, plus the three owner decisions the design leaves open:

* **D1** — :data:`ABSOLUTE_IDLE_FLOOR_MINUTES`. Budget pressure may shorten the
  idle threshold, never below this, and it is an invariant rather than a
  product setting (no environment knob reads it).
* **D2** — :data:`AUDIT_REQUIRED_BEFORE_RETIREMENT`. No durable record, no
  kill. The write is verified (``append`` + ``fsync`` + size check), and a
  failure is returned to the caller so the pass can report it instead of
  swallowing it the way ``reclaim._audit`` used to.
* **D3** — **superseded** by the owner ruling of 2026-11-15 (§7.4 item 5,
  §7.6): the tool heals silently, so nothing may gate a restore and nothing may
  announce one. The replacement is :data:`RESTORE_IN_FLIGHT_MAX` and
  :func:`restore_session`, a single entry point a loss detector can call.

Layout: records are plain JSONL (``retired-sessions.jsonl``) because they must
outlive the process and be readable by a human after a bad hour; resume markers
(``retirement-state.json``) are short-lived and pruned. Nothing here imports
``reclaim`` so the dependency stays one-way.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corral.legacy_names import cache_dir, socket_for_session, tmux_base_argv

# ------------------------------------------------------------------- decisions


# D1: the idle threshold may be shortened under pressure, never below this.
# An invariant, deliberately not an environment knob: a product setting here is
# exactly the "degrade the owner's sessions" lever the design rules out.
ABSOLUTE_IDLE_FLOOR_MINUTES = 30.0

# D2: a retirement without a durable, verified record does not happen.
# Flip to False to restore the old swallow-and-kill behaviour.
AUDIT_REQUIRED_BEFORE_RETIREMENT = True

# D3 (no automatic resume after a server loss unless opted in) was **superseded**
# by the owner ruling of 2026-11-15 (§7.4 item 5, §7.6): the tool heals silently,
# so nothing may gate a restore and nothing may announce one. The replacement
# lives here as :data:`RESTORE_IN_FLIGHT_MAX` and :func:`restore_session`.

RECORD_FILE = "retired-sessions.jsonl"
RESUME_STATE_FILE = "retirement-state.json"
RESUME_RETRY_FILE = "resume-retries.json"
# INV-15 / §6.5: Corral absorbs a failed resume itself. Bounded attempts with
# backoff, every attempt and cause recorded, never a user-facing action.
RESUME_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (60.0, 300.0, 900.0)
RESUME_RETRY_MAX_ATTEMPTS = 3
# §7.6 (owner ruling 2026-11-15, supersedes D3): a lost server heals silently and
# unconditionally, so no setting gates a restore. The only cap is a RATE — at most
# this many restores in flight, the next starting as the previous reaches its
# first frame. A cap on the total would abandon sessions the owner still expects.
RESTORE_IN_FLIGHT_MAX = 3
# Restore order at §7.6 step 2: work in progress first (its absence is noticed
# within seconds), then an open question, then everything else.
RESTORE_PRIORITY = {"working": 0, "waiting_on_user": 1}
# A runtime's own answer that the record is gone is unresolvable, not transient.
TERMINAL_RESUME_CAUSES = frozenset({"not_found", "session_gone"})
# Runtimes whose resume command is addressed by the native history file itself:
# no readable history means no guaranteed resume, so they stay resident (§6.5).
RESUME_NEEDS_HISTORY = frozenset({"pi", "claude"})
# How much of the record file a pass reads: enough for every cooldown window,
# bounded so a year of retirements cannot slow a 60 s tick.
_RECORD_TAIL_BYTES = 512 * 1024
_RESUME_MARKER_TTL_SECONDS = 24 * 3600.0
_SESSION_LOCK_WAIT_SECONDS = 10.0
# The whole-server operation guard (INV-11). The migration script owns the file;
# a stale one must not block retirement for ever.
SERVER_OPERATION_LOCK = "keepalive-migration.lock"
_SERVER_OPERATION_STALE_SECONDS = 3600.0

# §6.3: Codex resume can leave a thread stuck at "Working" when the interrupted
# turn still had a tool call open, repairable only by hand. A Codex session is
# therefore never retired on a timestamp: it needs terminal history, a clean
# process tree *and* a verified transcript tail.
TRANSCRIPT_REQUIRED_RUNTIMES = frozenset({"codex"})
# §6.4 (D5 recommendation A): OpenCode's account-wide `opencode serve` owns the
# sessions, so a pane that looks idle may still be executing. Treat a reachable
# shared server as busy and record the limitation in every verdict.
SHARED_SERVER_RUNTIMES = frozenset({"opencode"})


# --------------------------------------------------------------------- records


@dataclass(frozen=True)
class RetirementRecord:
    """One session ended by Corral, with everything needed to resume it."""

    name: str
    runtime_id: str
    ident: str
    retired_at: float
    outcome: str = "retired"  # "retired" | "kill_failed"
    session_id: str = ""
    history_path: str = ""
    cwd: str = ""
    status_tag: str = ""
    state: str = "parked"
    idle_seconds: float = 0.0
    reason: str = "inactive"
    pane_pid: int = 0
    server_pid: int = 0
    resume_argv: tuple[str, ...] = ()
    resume_cwd: str = ""
    resume_ready: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "name": self.name,
            "runtime_id": self.runtime_id,
            "ident": self.ident,
            "retired_at": self.retired_at,
            "outcome": self.outcome,
            "session_id": self.session_id,
            "history_path": self.history_path,
            "cwd": self.cwd,
            "status_tag": self.status_tag,
            "state": self.state,
            "idle_seconds": self.idle_seconds,
            "reason": self.reason,
            "pane_pid": self.pane_pid,
            "server_pid": self.server_pid,
            "resume_argv": list(self.resume_argv),
            "resume_cwd": self.resume_cwd,
            "resume_ready": self.resume_ready,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RetirementRecord | None:
        try:
            name = str(data["name"])
            retired_at = float(data["retired_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if not name or retired_at <= 0:
            return None
        try:
            pane_pid = int(data.get("pane_pid") or 0)
            server_pid = int(data.get("server_pid") or 0)
        except (TypeError, ValueError):
            pane_pid = server_pid = 0
        argv = data.get("resume_argv") or ()
        evidence = data.get("evidence")
        return cls(
            name=name,
            runtime_id=str(data.get("runtime_id") or ""),
            ident=str(data.get("ident") or ""),
            retired_at=retired_at,
            outcome=str(data.get("outcome") or "retired"),
            session_id=str(data.get("session_id") or ""),
            history_path=str(data.get("history_path") or ""),
            cwd=str(data.get("cwd") or ""),
            status_tag=str(data.get("status_tag") or ""),
            state=str(data.get("state") or "parked"),
            idle_seconds=float(data.get("idle_seconds") or 0.0),
            reason=str(data.get("reason") or "inactive"),
            pane_pid=pane_pid,
            server_pid=server_pid,
            resume_argv=tuple(str(part) for part in argv) if isinstance(argv, (list, tuple)) else (),
            resume_cwd=str(data.get("resume_cwd") or ""),
            resume_ready=bool(data.get("resume_ready")),
            evidence=dict(evidence) if isinstance(evidence, Mapping) else {},
        )


def record_path() -> Path:
    return cache_dir() / RECORD_FILE


def append_record(record: RetirementRecord, *, path: Path | None = None) -> bool:
    """Append one record durably; False when the write cannot be verified.

    A caller that sees False must not end the session (D2). Verification is the
    file size after the append, not just "no exception": a short write or a
    filesystem that lied must not pass as a record.
    """
    target = path or record_path()
    blob = (json.dumps(record.to_dict(), ensure_ascii=False, default=str) + "\n").encode("utf-8")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        before = target.stat().st_size if target.exists() else 0
        with open(target, "ab") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        if target.stat().st_size != before + len(blob):
            return False
        if before == 0:
            # A brand-new file also needs its directory entry on disk.
            directory = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return True
    except OSError:
        return False


def read_records(*, path: Path | None = None, limit: int = 500) -> list[RetirementRecord]:
    """Newest last; unreadable or malformed lines are skipped, never raised."""
    target = path or record_path()
    try:
        size = target.stat().st_size
    except OSError:
        return []
    try:
        with open(target, "rb") as handle:
            if size > _RECORD_TAIL_BYTES:
                handle.seek(size - _RECORD_TAIL_BYTES)
                handle.readline()  # drop the partial first line
            raw = handle.read()
    except OSError:
        return []
    records: list[RetirementRecord] = []
    for line in raw.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, Mapping):
            continue
        record = RetirementRecord.from_dict(payload)
        if record is not None:
            records.append(record)
    return records[-max(1, limit):]


def retired_at_map(*, now: float, window_seconds: float, path: Path | None = None) -> dict[str, float]:
    """Session name -> timestamp of its latest *completed* retirement.

    Only records inside the cooldown window are kept, which is all the decision
    needs and keeps the map small on a busy day.
    """
    latest: dict[str, float] = {}
    for record in read_records(path=path):
        if record.outcome != "retired":
            continue
        if now - record.retired_at > window_seconds:
            continue
        latest[record.name] = max(latest.get(record.name, 0.0), record.retired_at)
    return latest


# -------------------------------------------------------------- resume markers


def _state_path() -> Path:
    return cache_dir() / RESUME_STATE_FILE


def note_resume(*keys: str, now: float | None = None) -> bool:
    """Mark a session as freshly resumed (INV-4 resume grace).

    Keys are whatever the caller can address the session by — the canonical
    session key, a hosted name, or both; the decision checks all of them.
    Best effort: a failed marker costs grace, never data.
    """
    stamp = time.time() if now is None else float(now)
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        state = _read_state()
        resumed = {
            key: value for key, value in state.get("resumed", {}).items()
            if isinstance(value, (int, float)) and stamp - value <= _RESUME_MARKER_TTL_SECONDS
        }
        for key in keys:
            if key:
                resumed[str(key)] = stamp
        state["resumed"] = resumed
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def _read_state() -> dict[str, Any]:
    try:
        payload = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def resumed_at_map(*, now: float, window_seconds: float) -> dict[str, float]:
    resumed = _read_state().get("resumed")
    if not isinstance(resumed, Mapping):
        return {}
    return {
        str(key): float(value)
        for key, value in resumed.items()
        if isinstance(value, (int, float)) and 0 <= now - float(value) <= window_seconds
    }


# ---------------------------------------------------------------- pane identity


@dataclass(frozen=True)
class PaneIdentity:
    pane_pid: int
    server_pid: int

    @property
    def known(self) -> bool:
        return self.pane_pid > 0 and self.server_pid > 0


def pane_identity(name: str, socket: str = "") -> PaneIdentity | None:
    """Pane pid plus tmux server pid for one session, or None when unreadable.

    Read twice per retirement (decision time and immediately before the kill) so
    a pane that was replaced in between cancels the retirement (INV-6).
    """
    target = socket or socket_for_session(name)
    try:
        out = subprocess.check_output(
            [
                *tmux_base_argv(target), "list-panes", "-t", f"={name}",
                "-F", "#{pid}|#{pane_pid}",
            ],
            stderr=subprocess.DEVNULL,
            timeout=1.5,
            env=_tmux_env(),
        ).decode()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    parts = (out.strip().splitlines() or [""])[0].split("|")
    if len(parts) != 2:
        return None
    try:
        server_pid, pane_pid = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    identity = PaneIdentity(pane_pid=pane_pid, server_pid=server_pid)
    return identity if identity.known else None


def _tmux_env() -> dict[str, str]:
    """Environment for spawning live tmux, matching ``keepalive.tmux_env``.

    The identity probe runs next to the kill, so it must see the same socket
    layout; the stripped variables are the ones ``keepalive`` strips for the
    same reasons (host-poisoned library paths and CI tracking).
    """
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("RUNNER_TRACKING_ID", None)
    env.pop("TMUX_TMPDIR", None)
    env.setdefault("LANG", "C.UTF-8")
    return env


def server_operation_in_progress(*, now: float | None = None) -> bool:
    """INV-11: a keepalive migration or server restart is underway.

    An operation that crashed may leave the lock behind, so a lock older than an
    hour no longer blocks; anything else reads as "in progress" because not
    knowing is never a licence to end a session.
    """
    stamp = time.time() if now is None else float(now)
    lock = cache_dir() / SERVER_OPERATION_LOCK
    try:
        age = stamp - lock.stat().st_mtime
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return age < _SERVER_OPERATION_STALE_SECONDS


# -------------------------------------------------------------- per-session lock


def _lock_path(canonical: str) -> Path:
    """Same file ``remote.sessions._resume_process_lock`` uses for resume.

    Keeping the naming identical is what makes INV-7 real: retirement and
    resume contend for one advisory lock, whichever is second re-reads first.
    """
    digest = hashlib.sha1(canonical.encode()).hexdigest()[:24]
    return cache_dir() / "resume-locks" / (digest + ".lock")


@contextlib.contextmanager
def session_lock(canonical: str, *, wait_seconds: float = _SESSION_LOCK_WAIT_SECONDS):
    """Yield True when the per-session lock is held, False when it is not.

    Bounded wait: a resume that is mid-launch wins and the retirement is
    skipped (recorded) rather than blocked for a whole tick.
    """
    try:
        folder = _lock_path(canonical).parent
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = open(_lock_path(canonical), "a+")
    except OSError:
        yield False
        return
    acquired = False
    deadline = time.monotonic() + max(0.0, wait_seconds)
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


# ------------------------------------------------------------- resume planning


def build_resume_plan(
    runtime_id: str, *, session_id: str, history_path: str, cwd: str,
) -> tuple[tuple[str, ...], str] | None:
    """(argv, cwd) of the native resume command, or None when it cannot be built.

    §6.2: every runtime resumes through its own ``build_resume_plan``; the
    record stores what that plan was, so a disputed retirement can be resumed
    from the log alone.
    """
    from corral.runtime.registry import default_registry

    session = {"source": runtime_id, "id": session_id, "path": history_path, "cwd": cwd}
    try:
        runtime = default_registry().get(runtime_id)
        plan = runtime.build_resume_plan(session)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - a plan we cannot build is a retirement we cannot justify
        return None
    argv = tuple(str(part) for part in plan.argv)
    if not argv:
        return None
    return argv, str(plan.cwd or "")


def resume_plan(record: RetirementRecord):
    """The stored resume plan for a retired session (falls back to rebuilding)."""
    from corral.models import LaunchPlan

    if record.resume_argv:
        return LaunchPlan(argv=tuple(record.resume_argv), cwd=record.resume_cwd or None)
    built = build_resume_plan(
        record.runtime_id,
        session_id=record.session_id,
        history_path=record.history_path,
        cwd=record.cwd,
    )
    if built is None:
        return None
    argv, cwd = built
    return LaunchPlan(argv=argv, cwd=cwd)


def history_readable(path: str) -> bool:
    """The native history exists and can be opened: the resume's only input."""
    if not path:
        return False
    try:
        with open(path, "rb") as handle:
            handle.read(1)
    except OSError:
        return False
    return True


def resume_block_reason(
    runtime_id: str, *, session_id: str, history_path: str, cwd: str,
) -> str | None:
    """INV-15: None only when a resume is guaranteed by construction (§6.5).

    A session whose resume could fail as a user task is not eligible for
    retirement at all: occupying memory is the cheaper error. Blocks are
    ``resume_unverified`` (no verified resume path on this machine), 
    ``history_unreadable`` (the runtime's resume is addressed by a history file
    that is missing or unreadable) and ``runtime_transcript_unverified`` for the
    §6.3 Codex hazard, which ``evaluate`` applies earlier.
    """
    from corral.runtime.registry import default_registry

    try:
        registry = default_registry()
        if runtime_id not in registry.ids:
            return "resume_unverified"
        runtime = registry.get(runtime_id)
    except Exception:  # noqa: BLE001 - a registry we cannot read proves nothing
        return "resume_unverified"
    executable = str(getattr(runtime, "executable", "") or "")
    if not executable or not shutil.which(executable):
        return "resume_unverified"
    if not session_id:
        return "resume_unverified"
    if runtime_id in RESUME_NEEDS_HISTORY and not history_readable(history_path):
        return "history_unreadable"
    if build_resume_plan(
        runtime_id, session_id=session_id, history_path=history_path, cwd=cwd,
    ) is None:
        return "resume_unverified"
    return None


# ------------------------------------------------------- INV-15: resume retries


@dataclass(frozen=True)
class ResumeAttempt:
    """One session's bounded retry sequence for a resume Corral must absorb."""

    key: str
    attempts: int
    first_at: float
    next_at: float
    cause: str
    history_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "first_at": self.first_at,
            "next_at": self.next_at,
            "cause": self.cause,
            "history_path": self.history_path,
        }

    @classmethod
    def from_dict(cls, key: str, data: Mapping[str, Any]) -> ResumeAttempt | None:
        try:
            return cls(
                key=str(key),
                attempts=int(data["attempts"]),
                first_at=float(data["first_at"]),
                next_at=float(data["next_at"]),
                cause=str(data.get("cause") or "unknown"),
                history_path=str(data.get("history_path") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _retry_path() -> Path:
    return cache_dir() / RESUME_RETRY_FILE


def load_resume_retries() -> dict[str, ResumeAttempt]:
    try:
        payload = json.loads(_retry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    retries: dict[str, ResumeAttempt] = {}
    for key, raw in payload.items():
        attempt = ResumeAttempt.from_dict(str(key), raw) if isinstance(raw, Mapping) else None
        if attempt is not None:
            retries[str(key)] = attempt
    return retries


def _save_resume_retries(retries: Mapping[str, ResumeAttempt]) -> bool:
    target = _retry_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({key: attempt.to_dict() for key, attempt in retries.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return True
    except OSError:
        return False


def schedule_resume_retry(
    key: str,
    *,
    cause: str,
    now: float | None = None,
    history_path: str = "",
) -> ResumeAttempt | None:
    """Absorb one failed resume. Returns the next attempt, or None when terminal.

    §6.5: never a user-facing retry. A runtime saying the record is gone, or the
    bounded attempt count running out, is established as unresolvable and ends
    the sequence with ``resume_failed_terminal``; the transcript path is kept in
    that event and in a durable record so the content stays reachable.
    """
    stamp = time.time() if now is None else float(now)
    retries = load_resume_retries()
    previous = retries.get(key)
    attempts = (previous.attempts if previous else 0) + 1
    first_at = previous.first_at if previous else stamp
    path = history_path or (previous.history_path if previous else "")
    if cause in TERMINAL_RESUME_CAUSES or attempts > RESUME_RETRY_MAX_ATTEMPTS:
        fail_resume_terminal(key, cause=cause, now=stamp, history_path=path, attempts=attempts, first_at=first_at)
        return None
    backoff = RESUME_RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RESUME_RETRY_BACKOFF_SECONDS) - 1)]
    attempt = ResumeAttempt(
        key=key, attempts=attempts, first_at=first_at, next_at=stamp + backoff,
        cause=str(cause or "unknown"), history_path=path,
    )
    retries[key] = attempt
    _save_resume_retries(retries)
    _event("resume_retry", session=key, attempt=attempts, cause=attempt.cause,
           next_in_seconds=round(backoff, 1), history_path=path)
    return attempt


def due_resume_retries(now: float | None = None) -> list[ResumeAttempt]:
    """Retries whose backoff has elapsed, oldest first.

    Pre-boot retries from an earlier OS boot are discarded so rebooting never
    auto-launches agents in the background (2026-11-18 owner ruling).
    """
    from corral import hosted_lifecycle

    stamp = time.time() if now is None else float(now)
    boot_ts = hosted_lifecycle.boot_time()
    retries = load_resume_retries()
    kept: dict[str, ResumeAttempt] = {}
    dropped = False
    for key, attempt in retries.items():
        if (
            boot_ts is not None
            and stamp >= boot_ts
            and attempt.first_at > 1_000_000_000.0
            and attempt.first_at < boot_ts
        ):
            dropped = True
            continue
        kept[key] = attempt
    if dropped:
        _save_resume_retries(kept)
    return sorted(
        (attempt for attempt in kept.values() if attempt.next_at <= stamp),
        key=lambda attempt: attempt.next_at,
    )


def clear_resume_retry(key: str) -> None:
    """The resume succeeded (or the user resumed later): drop the sequence."""
    retries = load_resume_retries()
    if retries.pop(key, None) is not None:
        _save_resume_retries(retries)


def fail_resume_terminal(
    key: str,
    *,
    cause: str,
    now: float | None = None,
    history_path: str = "",
    attempts: int = 0,
    first_at: float | None = None,
) -> bool:
    """Report an unresolvable resume once, with its cause and transcript path.

    Nothing here asks the owner to do anything: the event states the fact and
    names the history file, so the conversation stays reachable while the pane
    does not come back.
    """
    stamp = time.time() if now is None else float(now)
    retries = load_resume_retries()
    previous = retries.pop(key, None)
    _save_resume_retries(retries)
    count = attempts or (previous.attempts if previous else 0)
    path = history_path or (previous.history_path if previous else "")
    _event("resume_failed_terminal", session=key, cause=cause, attempts=count,
           history_path=path, started_at=first_at)
    runtime_id, _, ident = key.partition(":")
    return append_record(RetirementRecord(
        name=key,
        runtime_id=runtime_id,
        ident=ident,
        retired_at=stamp,
        outcome="resume_failed",
        history_path=path,
        state="retired",
        reason=str(cause or "unknown"),
        evidence={"attempts": count, "cause": cause, "history_path": path},
    ))


def _event(name: str, **fields: Any) -> None:
    """Observability is best effort; the durable record is the fallback."""
    try:
        from corral import observe

        observe.event(name, **fields)
    except Exception:  # noqa: BLE001
        pass


# -------------------------------------------------------- §7.6: silent restore


@dataclass(frozen=True)
class RestoreResult:
    """Outcome of one silent restore attempt. Never a shape the user must act on."""

    key: str
    state: str  # "restored" | "pending" | "rate_limited" | "unresolvable"
    attempts: int = 0
    cause: str = ""
    history_path: str = ""
    next_in_seconds: float | None = None

    def as_event(self) -> dict[str, Any]:
        return {
            "session": self.key,
            "state": self.state,
            "attempts": self.attempts,
            "cause": self.cause,
            "history_path": self.history_path,
            "next_in_seconds": self.next_in_seconds,
        }


def restore_session(
    key: str,
    resume: Callable[[], Any],
    *,
    runtime_id: str = "",
    session_id: str = "",
    history_path: str = "",
    cwd: str = "",
    in_flight: int = 0,
    now: float | None = None,
) -> RestoreResult:
    """Restore one session silently: the single entry point for §7.6 healing.

    Contract for whatever detects the loss (the supervisor, the retry tick, or a
    surface that already knows it needs the session back):

    * ``resume`` is a zero-argument callable that performs the native resume
      (in practice ``SessionHub._resume_locked(key)``); it raises on failure.
    * ``in_flight`` is how many restores the caller already has running. At
      ``RESTORE_IN_FLIGHT_MAX`` the call returns ``rate_limited`` without doing
      anything; the cap is a rate, so the caller retries the same key later.
    * The per-session ``flock`` of INV-7 is taken here, so no caller needs to
      take it again and two detectors cannot race into two panes.
    * Nothing raises, nothing returns a message meant for a person, and no
      setting can disable it. Failures come back as ``pending`` (Corral will
      retry on its own, recorded as ``resume_retry``) or ``unresolvable``
      (established final, recorded as ``resume_failed_terminal`` with the
      transcript path so the content stays reachable).
    """
    stamp = time.time() if now is None else float(now)
    if in_flight >= max(1, RESTORE_IN_FLIGHT_MAX):
        return RestoreResult(key, "rate_limited", history_path=history_path)
    previous = load_resume_retries().get(key)
    attempts = previous.attempts if previous else 0
    if runtime_id:
        block = resume_block_reason(
            runtime_id,
            session_id=session_id,
            history_path=history_path or (previous.history_path if previous else ""),
            cwd=cwd,
        )
        if block is not None:
            fail_resume_terminal(
                key, cause=block, now=stamp,
                history_path=history_path or (previous.history_path if previous else ""),
                attempts=attempts, first_at=previous.first_at if previous else stamp,
            )
            return RestoreResult(key, "unresolvable", attempts, block, history_path)
    with session_lock(key) as held:
        if not held:
            # Another Corral process is already restoring it; that is the lock
            # doing its job, not a failure.
            return RestoreResult(key, "pending", attempts + 1, "lock_busy", history_path)
        try:
            resume()
        except Exception as exc:  # noqa: BLE001 - absorbed, never handed to a person
            cause = str(getattr(exc, "code", "") or type(exc).__name__)
            attempt = schedule_resume_retry(key, cause=cause, now=stamp, history_path=history_path)
            if attempt is None:
                return RestoreResult(key, "unresolvable", attempts + 1, cause, history_path)
            return RestoreResult(
                key, "pending", attempt.attempts, cause, history_path,
                next_in_seconds=max(0.0, attempt.next_at - stamp),
            )
        clear_resume_retry(key)
        note_resume(key)
        _event("resume_restored", session=key, attempts=attempts + 1)
        return RestoreResult(key, "restored", attempts + 1, history_path=history_path)


def restore_order_key(entry: Any) -> tuple[int, float]:
    """§7.6 step 2 ordering: working, then waiting_on_user, then the rest."""
    state = _field(entry, "state_at_loss") or _field(entry, "state") or ""
    priority = RESTORE_PRIORITY.get(str(state), 2)
    stamp = _field(entry, "retired_at") or _field(entry, "at") or 0.0
    try:
        return priority, -float(stamp)
    except (TypeError, ValueError):
        return priority, 0.0


def ordered_restore_candidates(entries: Iterable[Any]) -> list[Any]:
    """The restore queue, in the order §7.6 asks for. No total cap."""
    return sorted(entries, key=restore_order_key)


def _field(entry: Any, key: str) -> Any:
    if isinstance(entry, Mapping):
        return entry.get(key)
    return getattr(entry, key, None)


def runtime_requires_transcript(runtime_id: str) -> bool:
    return runtime_id in TRANSCRIPT_REQUIRED_RUNTIMES


def runtime_shares_background_server(runtime_id: str) -> bool:
    return runtime_id in SHARED_SERVER_RUNTIMES


def record_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Copy of the decision evidence safe to persist (JSON-ready, bounded)."""
    return {str(key): value for key, value in evidence.items()}
