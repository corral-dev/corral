"""Silent reclaim of inactive hosted sessions.

Hosted agents (tmux sessions on the keepalive socket) never went away on their
own, so a long day of work left dozens of idle agents plus their helper
processes resident until the machine swapped itself to a crawl. This module
stops the ones that are provably inactive, without telling anyone.

The contract is recorded in ``docs/MAINTAINER_GUIDE.md`` ("Silent automatic
reclaim of inactive hosted sessions is ON by default"). In short:

* silent: no toast, bell, banner or notification — only an ``observe`` audit
  event per reclaimed session carrying every piece of evidence;
* inactive means every condition holds and *unknown never counts* (history-backed
  sessions: finished outcome, old history and old session input; placeholder
  sessions without history: old terminal output too — see
  ``WINDOW_OUTPUT_GATES_HISTORY``; every session: no pending background work
  and no live non-helper descendant processes — see ``busycheck``);
* never reachable from startup or session-creation paths — callers invoke
  ``maybe_reclaim`` from a background tick only;
* stopping a session never deletes history; native resume brings it back.

Layout: ``evaluate`` / ``decide`` are pure (plain data in, verdicts out);
``probe_hosted`` and the small ``_build_context`` step gather the facts;
``apply`` performs the kills; ``maybe_reclaim`` is the throttled, exception-proof
entry point.

Retirement (``docs/design/HOSTED_SESSION_LIFECYCLE_DESIGN.md`` §2-§6) adds: a
per-session state name to every verdict, the §4.2 policy numbers (absolute floor,
resume grace, cooldown, resident budget, pressure hysteresis), the §6 runtime
constraints, and a durable record per retirement written before the kill
(``corral.retirement``). The three owner decisions live in that module as
``ABSOLUTE_IDLE_FLOOR_MINUTES`` (D1) and ``AUDIT_REQUIRED_BEFORE_RETIREMENT``
(D2). D3 ("no automatic resume after a server loss unless opted in") was
**superseded** by the owner ruling of 2026-11-15 (§7.4 item 5, §7.6): a lost
server heals silently, so nothing gates a restore and the only cap is the rate
cap in ``retirement.RESTORE_IN_FLIGHT_MAX``.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from sesskit.models import session_key
from sesskit.titles import STATUS_ABORTED, STATUS_DONE, STATUS_PENDING

from corral import busycheck, hosted_lifecycle, keepalive, liveness, observe, reclaim_pass, retirement
from corral.legacy_names import (
    ALL_SOCKET_NAMES,
    cache_dir,
    env_is_disabled,
    getenv,
    socket_for_session,
    tmux_base_argv,
)

DEFAULT_IDLE_MINUTES = 60.0
DEFAULT_PRESSURE_IDLE_MINUTES = 60.0
GRACE_SECONDS = 30 * 60.0
# §4.2 (updated 2026-11-18): normally keep the 8 newest parked sessions
# resident and retire any excess beyond 8 that are idle >= 60 min (uncapped
# per pass). Under armed pressure, keep 0 resident and retire all eligible.
RESIDENT_BUDGET = 8
PRESSURE_RESIDENT_BUDGET = 0
# §4.2: a resume makes a pane re-read history and look idle; ending it again is
# the worst possible behaviour. Anti-flap cooldown sits next to it.
RESUME_GRACE_SECONDS = 10 * 60.0
COOLDOWN_SECONDS = 30 * 60.0
# D1 lives in ``retirement`` as an invariant; re-exported so a caller reading
# this module's policy numbers sees the floor without a second import.
ABSOLUTE_IDLE_FLOOR_MINUTES = retirement.ABSOLUTE_IDLE_FLOOR_MINUTES

# One pass per interval machine-wide (5 minutes); a young process waits before
# its first pass so that opening a TUI never kills anything (clock at import).
_MIN_INTERVAL_SECONDS = 300.0
_MIN_PROCESS_AGE_SECONDS = 120.0
_STARTED_AT = time.monotonic()
_STAMP_NAME = "reclaim.stamp"

# Swap used/total is sticky: macOS swaps out untouched pages while memory is
# comfortable and does not compact when pressure clears, so it stays high for
# hours after recovery and must never be a pressure trigger (§3.6). It is kept
# for the audit only, through ``pressure_facts``.
_DARWIN_PRESSURE_LEVEL = 2  # kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical
_PSI_AVG60 = 10.0
_MEM_AVAILABLE_RATIO = 0.10

# tmux ``window_activity`` is the last time the pane *printed* something. Idle agent TUIs
# keep redrawing (measured: Codex sessions finished a day ago printed 0.1 minutes ago,
# OpenCode ones ~4.5 minutes ago), so gating a history-backed session on it would
# reclaim nothing. Real history plus the session input clock decide those sessions;
# ``window_activity`` still gates placeholder sessions, which have no history at all.
# Flip to True to demand quiet output from every session as well.
WINDOW_OUTPUT_GATES_HISTORY = False

_LIST_FORMAT = "#{session_name}|#{session_created}|#{session_activity}|#{window_activity}"
_CLIENT_FORMAT = "#{client_session}|#{client_control_mode}"
_TERMINAL_STATUS = frozenset({STATUS_DONE, STATUS_ABORTED})

# Sockets to inspect; ``None`` means the real keepalive sockets. Tests inject a
# private socket so nothing here can ever touch a user's live sessions.
_SOCKETS: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Hosted:
    """One hosted tmux session as tmux reports it."""

    name: str
    runtime_id: str
    ident: str
    socket: str = ""
    created_at: float = 0.0
    session_activity: float = 0.0
    window_activity: float = 0.0
    foreign_clients: int = 0  # attached clients that are not control-mode


@dataclass(frozen=True)
class History:
    """What the caller's session list says about a hosted session."""

    status_tag: str = ""
    provisional: bool = False
    active_at: float | None = None  # newest history timestamp; None = unknown
    runtime_id: str = ""
    session_id: str = ""
    protect: bool = False  # caller veto (e.g. phone is viewing / task in flight)
    history_path: str = ""  # native history file; feeds the transcript signal
    cwd: str = ""  # working directory; needed to build a resume plan


@dataclass(frozen=True)
class BackgroundState:
    """The shared background-work verdict for one pass (see ``busycheck``).

    ``busy_names`` holds sessions whose pane tree already shows live
    non-helper work (checked eagerly, before history is consulted).
    ``checker`` stays around for the lazy transcript signal, which
    ``evaluate`` runs only for sessions that would otherwise read as idle.
    """

    busy_names: Collection[str] = ()
    evidence: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    checker: Any = None  # busycheck.BusyChecker | None

    def check_transcript(self, host: Hosted, entries: Sequence[History]) -> tuple[bool, dict[str, Any]] | None:
        """(pending, evidence) for the transcript signal; None when the
        signal does not apply (no checker, or a runtime without a mapped
        transcript check)."""
        if self.checker is None:
            return None
        runtime_id = host.runtime_id
        for entry in entries:
            if entry.runtime_id:
                runtime_id = entry.runtime_id
                break
        verdict = self.checker.verdict(host.name, runtime_id=runtime_id)
        if verdict.reason == "background_transcript":
            return True, dict(verdict.evidence)
        if "transcript" in verdict.evidence:
            return False, dict(verdict.evidence)
        return None


@dataclass(frozen=True)
class Context:
    """Everything ``evaluate`` needs besides the session itself."""

    now: float
    pressure: bool = False
    busy_pairs: Collection[tuple[str, str]] = ()
    waiting_pairs: Collection[tuple[str, str]] = ()
    viewed: Collection[str] = ()
    pinned_keys: Collection[str] = ()
    idle_minutes: float = DEFAULT_IDLE_MINUTES
    pressure_idle_minutes: float = DEFAULT_PRESSURE_IDLE_MINUTES
    grace_seconds: float = GRACE_SECONDS
    background: BackgroundState | None = None
    # --- retirement policy (§4.2, §5) ---
    floor_minutes: float = ABSOLUTE_IDLE_FLOOR_MINUTES
    resume_grace_seconds: float = RESUME_GRACE_SECONDS
    cooldown_seconds: float = COOLDOWN_SECONDS
    resident_budget: int = RESIDENT_BUDGET
    pressure_resident_budget: int = PRESSURE_RESIDENT_BUDGET
    # Budget-ladder threshold for this pass; only ever set while pressure is armed.
    budget_threshold_minutes: float | None = None
    resumed_at: Mapping[str, float] = field(default_factory=dict)
    retired_at: Mapping[str, float] = field(default_factory=dict)
    cpu: Mapping[str, float] = field(default_factory=dict)
    pane_pids: Mapping[str, int] = field(default_factory=dict)
    shared_opencode_server: bool = False
    dry_run: bool = False

    @property
    def threshold_minutes(self) -> float:
        """The raw age threshold before the invariant floor is applied."""
        if self.pressure:
            return min(self.idle_minutes, self.pressure_idle_minutes)
        return self.idle_minutes

    @property
    def effective_idle_minutes(self) -> float:
        """The threshold this pass actually uses (floor-clamped, ladder-applied).

        Read-only for surfaces: the session list may show this number with
        ``threshold_reason`` so the owner can tell that something is shortened.
        """
        base = self.threshold_minutes
        if self.budget_threshold_minutes is not None:
            base = min(base, self.budget_threshold_minutes)
        return max(self.floor_minutes, base)

    @property
    def threshold_reason(self) -> str:
        if self.budget_threshold_minutes is not None and self.effective_idle_minutes < self.idle_minutes:
            return "budget_pressure"
        if self.threshold_minutes < self.idle_minutes:
            return "pressure"
        if self.effective_idle_minutes > self.threshold_minutes:
            return "floor"
        return "default"

    @property
    def budget(self) -> int:
        return self.pressure_resident_budget if self.pressure else self.resident_budget


@dataclass(frozen=True)
class Verdict:
    host: Hosted
    reclaim: bool
    reason: str  # "inactive" or the protection that held
    idle_seconds: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    # §2 state name for this verdict; the record and the audit carry it so a
    # retired session is never mistaken for a crashed one.
    state: str = "parked"
    # True when every protection cleared and only the idle clock was consulted:
    # the resident-budget count is exactly this set.
    parked: bool = False
    # Resume inputs copied from the history entry, so ``apply`` can write a
    # self-contained record without holding the session list (§6.2, INV-5).
    session_id: str = ""
    history_path: str = ""
    cwd: str = ""
    has_history: bool = False

    @property
    def name(self) -> str:
        return self.host.name


@dataclass
class PassReport:
    """Observable result of one pass; the block reasons must never be swallowed."""

    at: float = 0.0
    hosted: int = 0
    parked: int = 0
    pressure: bool = False
    pressure_samples: int = 0
    swap_ratio: float | None = None
    pressure_level: int | None = None
    effective_idle_minutes: float = DEFAULT_IDLE_MINUTES
    threshold_reason: str = "default"
    resident_budget: int = RESIDENT_BUDGET
    over_budget: bool = False
    budget_steps: int = 0
    dry_run: bool = False
    retired: list[str] = field(default_factory=list)
    blocked: dict[str, int] = field(default_factory=dict)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def block(self, reason: str, name: str = "") -> None:
        self.blocked[reason] = self.blocked.get(reason, 0) + 1
        if name:
            self.skipped.append({"session": name, "reason": reason})

    def as_event(self) -> dict[str, Any]:
        return {
            "hosted": self.hosted,
            "parked": self.parked,
            "pressure": self.pressure,
            "pressure_samples": self.pressure_samples,
            "swap_ratio": self.swap_ratio,
            "pressure_level": self.pressure_level,
            "effective_idle_minutes": round(self.effective_idle_minutes, 1),
            "threshold_reason": self.threshold_reason,
            "resident_budget": self.resident_budget,
            "over_budget": self.over_budget,
            "budget_steps": self.budget_steps,
            "retired": len(self.retired),
            "blocked": dict(self.blocked),
            "dry_run": self.dry_run,
        }


# ---------------------------------------------------------------- policy knobs


def enabled() -> bool:
    """On by default; ``CORRAL_RECLAIM=0`` turns it off. Needs tmux."""
    return not env_is_disabled("RECLAIM") and shutil.which("tmux") is not None


def _minutes_env(suffix: str, default: float) -> float:
    raw = getenv(suffix)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(1.0, value) if value > 0 else default


def policy_from_env(now: float, **facts: Any) -> Context:
    return Context(
        now=now,
        idle_minutes=_minutes_env("RECLAIM_IDLE_MINUTES", DEFAULT_IDLE_MINUTES),
        pressure_idle_minutes=_minutes_env(
            "RECLAIM_PRESSURE_IDLE_MINUTES", DEFAULT_PRESSURE_IDLE_MINUTES,
        ),
        resident_budget=_budget_env(),
        dry_run=_flag_env("RECLAIM_DRY_RUN"),
        **facts,
    )


def _budget_env() -> int:
    """``CORRAL_RECLAIM_BUDGET=0`` disables only the budget lever."""
    raw = getenv("RECLAIM_BUDGET")
    if raw is None:
        return RESIDENT_BUDGET
    try:
        return max(0, int(raw))
    except ValueError:
        return RESIDENT_BUDGET


def _flag_env(suffix: str) -> bool:
    raw = (getenv(suffix) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def pressure_knobs() -> dict[str, float]:
    """§4.2 pressure hysteresis: samples, spacing and the disarm window."""
    samples: int = reclaim_pass.PRESSURE_SAMPLES
    raw_samples = (getenv("RECLAIM_PRESSURE_SAMPLES") or "").strip()
    if raw_samples:
        try:
            samples = max(1, int(raw_samples))
        except ValueError:
            pass
    disarm = reclaim_pass.PRESSURE_DISARM_SECONDS
    raw_disarm = (getenv("RECLAIM_PRESSURE_DISARM_MINUTES") or "").strip()
    if raw_disarm:
        try:
            disarm = max(0.0, float(raw_disarm)) * 60.0
        except ValueError:
            pass
    return {
        "samples": samples,
        "gap_seconds": reclaim_pass.PRESSURE_SAMPLE_GAP_SECONDS,
        "disarm_seconds": disarm,
    }


# ------------------------------------------------------------ pure decision step


def _is_busy(host: Hosted, entries: Sequence[History], busy: Collection[tuple[str, str]]) -> bool:
    known = {(e.runtime_id, e.session_id) for e in entries if e.session_id}
    for runtime_id, session_id in busy:
        if (runtime_id, session_id) in known:
            return True
        if runtime_id == host.runtime_id and keepalive._ident_matches_session_id(
            host.ident, session_id,
        ):
            return True
    return False


def _is_pinned(host: Hosted, entries: Sequence[History], pinned: Collection[str]) -> bool:
    keys = {session_key({"source": host.runtime_id, "id": host.ident})}
    keys.update(
        session_key({"source": e.runtime_id, "id": e.session_id})
        for e in entries
        if e.runtime_id and e.session_id
    )
    return any(key in pinned for key in keys)


def _history_block(real: Sequence[History]) -> str | None:
    """Protection code when real history does not prove the turn is over."""
    for entry in real:
        if entry.status_tag == STATUS_PENDING:
            return "history_pending"
        if entry.status_tag not in _TERMINAL_STATUS or entry.active_at is None:
            return "history_unknown"
    return None


def _minutes(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds / 60.0, 1)


def _valid(stamp: float) -> bool:
    return stamp > 0


def _resume_stamp(host: Hosted, session_id: str, ctx: Context) -> float | None:
    """Newest resume marker that names this session, whatever key wrote it."""
    stamps = [
        stamp
        for key in (host.name, f"{host.runtime_id}:{host.ident}", f"{host.runtime_id}:{session_id}")
        if key and (stamp := ctx.resumed_at.get(key)) is not None
    ]
    return max(stamps) if stamps else None


def _best_entry(entries: Sequence[History]) -> History | None:
    real = [e for e in entries if not e.provisional]
    if real:
        return real[0]
    return entries[0] if entries else None


def evaluate(host: Hosted, entries: Sequence[History], ctx: Context) -> Verdict:
    """Decide one hosted session. Reclaimable only if every protection is clear.

    Order follows §4.3: every protection first, then the state of the turn, then
    the runtime constraint (§6), then the idle clock plus the floor, then S1.
    ``unknown`` never counts as idle at any step (INV-12).
    """
    now = ctx.now
    real = [e for e in entries if not e.provisional]
    best = _best_entry(entries)
    status = best.status_tag if best else ""
    runtime_id = (best.runtime_id if best and best.runtime_id else host.runtime_id)
    resume_inputs = {
        "session_id": best.session_id if best else "",
        "history_path": best.history_path if best else "",
        "cwd": best.cwd if best else "",
        "has_history": bool(real),
    }
    evidence: dict[str, Any] = {
        "runtime": host.runtime_id,
        "socket": host.socket,
        "status_tag": status,
        "provisional": bool(entries) and not real,
        "pressure": ctx.pressure,
        "threshold_min": ctx.threshold_minutes,
        "effective_threshold_min": ctx.effective_idle_minutes,
        "threshold_reason": ctx.threshold_reason,
        "floor_min": ctx.floor_minutes,
        "resume_grace_min": round(ctx.resume_grace_seconds / 60.0, 1),
        "cooldown_min": round(ctx.cooldown_seconds / 60.0, 1),
        "created_age_min": _minutes(now - host.created_at) if _valid(host.created_at) else None,
    }

    def held(reason: str, state: str = "parked") -> Verdict:
        return Verdict(
            host, False, reason, evidence=evidence, state=state, **resume_inputs,
        )

    def stopped(reason: str, idle: float, state: str = "parked", parked: bool = True) -> Verdict:
        return Verdict(
            host, False, reason, idle, evidence, state=state, parked=parked, **resume_inputs,
        )

    if not entries:
        return held("history_unknown", "unknown")
    if any(e.protect for e in entries):
        return held("caller_protected")
    if not _valid(host.created_at) or now - host.created_at < ctx.grace_seconds:
        return held("grace")
    if host.name in ctx.viewed:
        return held("viewed")
    if host.foreign_clients > 0:
        # INV-3: a person attached in a terminal is using the session.
        evidence["foreign_clients"] = host.foreign_clients
        return held("attached_client")
    if _is_pinned(host, entries, ctx.pinned_keys):
        return held("pinned")
    if _is_busy(host, entries, ctx.waiting_pairs):
        # §2 waiting_on_user: a structured question is open.
        return held("busy", "waiting_on_user")
    if _is_busy(host, entries, ctx.busy_pairs):
        return held("busy", "working")
    # INV-4 / INV-13: resume grace and the per-session cooldown.
    resumed = _resume_stamp(host, resume_inputs["session_id"], ctx)
    if resumed is not None and 0 <= now - resumed < ctx.resume_grace_seconds:
        evidence["resumed_age_min"] = _minutes(now - resumed)
        return held("resume_grace")
    cooldown = ctx.retired_at.get(host.name)
    if cooldown is not None and 0 <= now - cooldown < ctx.cooldown_seconds:
        evidence["cooldown_remaining_min"] = _minutes(ctx.cooldown_seconds - (now - cooldown))
        return held("cooldown")
    if retirement.runtime_shares_background_server(runtime_id):
        evidence["opencode_shared_server"] = bool(ctx.shared_opencode_server)
        if ctx.shared_opencode_server:
            # §6.4 D5(A): the shared server may still be executing this session.
            return held("opencode_shared_server")
    if ctx.background is not None and host.name in ctx.background.busy_names:
        evidence["background"] = dict(ctx.background.evidence.get(host.name, {}))
        return held("background_busy", "working")
    blocked = _history_block(real)
    if blocked:
        # INV-2: a pending status tag is an open tool call, never a finished turn.
        return held(blocked, "waiting_on_tool" if blocked == "history_pending" else "unknown")
    if retirement.runtime_requires_transcript(runtime_id):
        # §6.3: Codex must never be retired on a timestamp. Its resume can leave a
        # thread stuck at "Working", so require terminal history, a clean tree
        # (checked above) *and* a verified transcript tail; without a reader for
        # this runtime the tail is unknown, which keeps the session resident.
        transcript = ctx.background.check_transcript(host, entries) if ctx.background else None
        pending, transcript_evidence = transcript if transcript is not None else (None, {})
        if transcript_evidence:
            evidence["background"] = dict(transcript_evidence)
        if pending:
            return held("background_busy", "waiting_on_tool")
        if not transcript_evidence.get("checked"):
            return held("runtime_transcript_unverified", "unknown")
    window_gates = not real or WINDOW_OUTPUT_GATES_HISTORY
    if not _valid(host.session_activity) or (window_gates and not _valid(host.window_activity)):
        return held("tmux_unknown", "unknown")

    ages = {"session_idle_min": now - host.session_activity}
    if real:
        ages["history_idle_min"] = now - max(e.active_at or 0.0 for e in real)
    if window_gates:
        ages["window_idle_min"] = now - host.window_activity
    evidence.update({key: _minutes(age) for key, age in ages.items()})
    evidence["window_gates"] = window_gates
    if not window_gates and _valid(host.window_activity):
        evidence["window_idle_min"] = _minutes(now - host.window_activity)  # audit only
    idle = min(ages.values())
    evidence["idle_min"] = _minutes(idle)
    # INV-4: the absolute floor is an invariant, whatever the budget or pressure says.
    if idle < ctx.floor_minutes * 60.0:
        return stopped("floor", idle)
    if idle < ctx.effective_idle_minutes * 60.0:
        # Parked, just not forgotten yet: this is exactly the resident set the
        # §4.2 budget counts.
        return stopped("recent_activity", idle)
    # S1: cumulative CPU delta. Only a nonzero delta is evidence of work; a
    # missing sample is unknown and neither vetoes nor permits (§3.1).
    cpu = ctx.cpu.get(host.name)
    evidence["cpu_delta_s"] = None if cpu is None else round(cpu, 3)
    if cpu is not None and cpu > reclaim_pass.CPU_WORK_TOLERANCE_SECONDS:
        return stopped("cpu_active", idle, state="working", parked=False)
    if ctx.background is not None:
        transcript = ctx.background.check_transcript(host, entries)
        if transcript is not None:
            pending, transcript_evidence = transcript
            evidence["background"] = transcript_evidence
            if pending:
                # INV-2: an enqueue without a remove is an open tool call.
                return stopped("background_busy", idle, state="waiting_on_tool", parked=False)
    return Verdict(host, True, "inactive", idle, evidence, state="parked", parked=True, **resume_inputs)


def decide(
    hosted: Sequence[Hosted], history: Mapping[str, Sequence[History]], ctx: Context,
) -> list[Verdict]:
    """Reclaimable sessions, longest idle first, with no per-pass cap.

    Normally keeps the newest ``resident_budget`` (8) parked sessions resident
    and retires only the excess beyond that budget that qualify as idle. Under
    pressure (``pressure_resident_budget == 0``), retires every eligible session.
    Ties at the budget-truncation boundary are ordered by last-measured
    ``phys_footprint`` (biggest first).
    """
    return _pick([evaluate(host, history.get(host.name, ()), ctx) for host in hosted], ctx)


def _break_ties(picked: list[Verdict], cap: int, ctx: Context) -> list[Verdict]:
    """Order the candidates that the cap would truncate, by measured footprint."""
    boundary = picked[cap - 1].idle_seconds
    tied_in = [verdict for verdict in picked[:cap] if verdict.idle_seconds == boundary]
    tied_out = [verdict for verdict in picked[cap:] if verdict.idle_seconds == boundary]
    if not tied_out:
        return picked
    group = (tied_in + tied_out)[: cap + 4]
    measured = {verdict.name: _footprint_bytes(verdict, ctx) for verdict in group}
    ordered = sorted(
        tied_in + tied_out,
        key=lambda verdict: measured.get(verdict.name) or 0,
        reverse=True,
    )
    head = [verdict for verdict in picked[:cap] if verdict.idle_seconds != boundary]
    tail = [verdict for verdict in picked[cap:] if verdict.idle_seconds != boundary]
    return head + ordered + tail


def _footprint_bytes(verdict: Verdict, ctx: Context) -> int | None:
    """``phys_footprint`` of the pane pid; only used to break an exact tie."""
    pane_pid = ctx.pane_pids.get(verdict.name)
    if not pane_pid:
        return None
    if not shutil.which("footprint"):
        return None
    try:
        out = subprocess.check_output(
            ["footprint", "-p", str(pane_pid), "-f", "bytes"],
            stderr=subprocess.DEVNULL, timeout=2,
        ).decode(errors="ignore")
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"phys_footprint:\s*([\d,]+)", out)
    if not match:
        match = re.search(r"([\d,]{6,})", out)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


# ---------------------------------------------------------------- memory pressure


_SWAP_FIELD = re.compile(r"(total|used)\s*=\s*([\d.]+)\s*([KMGT]?)", re.IGNORECASE)
_UNITS = {"": 1.0, "K": 1024.0, "M": 1024.0**2, "G": 1024.0**3, "T": 1024.0**4}


def _parse_swap_ratio(text: str) -> float | None:
    """``vm.swapusage`` -> used/total, or None when it cannot be read."""
    values: dict[str, float] = {}
    for label, number, unit in _SWAP_FIELD.findall(text):
        values[label.lower()] = float(number) * _UNITS[unit.upper()]
    total = values.get("total", 0.0)
    if total <= 0 or "used" not in values:
        return None
    return values["used"] / total


def _parse_psi_avg60(text: str) -> float | None:
    for line in text.splitlines():
        if line.startswith("some "):
            match = re.search(r"avg60=([\d.]+)", line)
            return float(match.group(1)) if match else None
    return None


def _parse_mem_available_ratio(text: str) -> float | None:
    fields = dict(re.findall(r"^(MemTotal|MemAvailable):\s+(\d+)", text, re.MULTILINE))
    total, available = int(fields.get("MemTotal", 0)), fields.get("MemAvailable")
    if total <= 0 or available is None:
        return None
    return int(available) / total


_LIBC = None


def _sysctlbyname(name: str, size: int) -> bytes | None:
    """Read a sysctl in-process; None on any failure."""
    global _LIBC
    try:
        import ctypes

        if _LIBC is None:
            libc = ctypes.CDLL(None, use_errno=True)
            libc.sysctlbyname.argtypes = (
                ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                ctypes.c_void_p, ctypes.c_size_t,
            )
            libc.sysctlbyname.restype = ctypes.c_int
            _LIBC = libc
        buffer = ctypes.create_string_buffer(size)
        length = ctypes.c_size_t(size)
        if _LIBC.sysctlbyname(name.encode(), buffer, ctypes.byref(length), None, ctypes.c_size_t(0)) != 0:
            return None
        if length.value > size:
            return None
        return buffer.raw[:length.value]
    except Exception:  # noqa: BLE001 - fall back to the command
        return None


def _sysctl_native(name: str) -> str | None:
    """Same text ``sysctl -n`` prints for the two keys the probe reads.

    The remote refresh loop and scan worker probe pressure every second; two
    ``sysctl`` children per probe cost more CPU than the rest of an idle tick.
    """
    if name == "vm.swapusage":
        raw = _sysctlbyname(name, 32)  # struct xsw_usage
        if raw is None or len(raw) != 32:
            return None
        total, _available, used = struct.unpack_from("=QQQ", raw)
        mib = 1024 * 1024
        return f"total = {total / mib:.2f}M used = {used / mib:.2f}M free = {(total - used) / mib:.2f}M"
    if name != "kern.memorystatus_vm_pressure_level":
        return None
    raw = _sysctlbyname(name, 8)
    if raw is None or len(raw) not in (4, 8):
        return None
    return str(int.from_bytes(raw, sys.byteorder, signed=True))


def _sysctl(name: str) -> str | None:
    if sys.platform == "darwin":
        value = _sysctl_native(name)
        if value is not None:
            return value
    try:
        return subprocess.check_output(
            ["sysctl", "-n", name], stderr=subprocess.DEVNULL, timeout=2,
        ).decode()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _darwin_pressure() -> bool:
    """Only the discrete pressure level triggers; swap ratio is informational.

    §3.6: ``vm.swapusage`` used/total is sticky — macOS swaps out untouched
    pages while memory is comfortable and does not compact when pressure clears,
    so OR-ing it in would arm the pressure threshold for hours after recovery.
    """
    level = (_sysctl("kern.memorystatus_vm_pressure_level") or "").strip()
    return level.isdigit() and int(level) >= _DARWIN_PRESSURE_LEVEL


def _read(path: str) -> str:
    with open(path, encoding="ascii", errors="ignore") as handle:
        return handle.read()


def _linux_pressure() -> bool:
    try:
        avg60 = _parse_psi_avg60(_read("/proc/pressure/memory"))
        if avg60 is not None and avg60 >= _PSI_AVG60:
            return True
    except OSError:
        pass
    ratio = _parse_mem_available_ratio(_read("/proc/meminfo"))
    return ratio is not None and ratio < _MEM_AVAILABLE_RATIO


def memory_pressure() -> bool:
    """True when the machine is short of memory. Any failure means False."""
    try:
        if sys.platform == "darwin":
            return _darwin_pressure()
        if sys.platform.startswith("linux"):
            return _linux_pressure()
    except Exception:  # noqa: BLE001 - a broken probe must never block or crash a pass
        pass
    return False


def pressure_facts() -> dict[str, Any]:
    """Machine-memory numbers for the audit; swap is recorded, never a trigger.

    In-process sysctl reads on Darwin, so this costs no fork; on any failure the
    fields are None rather than a guess.
    """
    facts: dict[str, Any] = {"swap_ratio": None, "pressure_level": None}
    try:
        if sys.platform == "darwin":
            facts["swap_ratio"] = _parse_swap_ratio(_sysctl("vm.swapusage") or "")
            level = (_sysctl("kern.memorystatus_vm_pressure_level") or "").strip()
            facts["pressure_level"] = int(level) if level.isdigit() else None
        elif sys.platform.startswith("linux"):
            facts["swap_ratio"] = _linux_swap_ratio(_read("/proc/meminfo"))
    except Exception:  # noqa: BLE001 - facts are for the audit only
        pass
    return facts


def _linux_swap_ratio(text: str) -> float | None:
    """Swap used/total from ``/proc/meminfo``; informational, like Darwin's."""
    fields = dict(re.findall(r"^(SwapTotal|SwapFree):\s+(\d+)", text, re.MULTILINE))
    total, free = int(fields.get("SwapTotal", 0)), int(fields.get("SwapFree", 0))
    if total <= 0:
        return None
    return max(0.0, min(1.0, (total - free) / total))


# ------------------------------------------------------------------ gathering facts


def _sockets() -> tuple[str, ...]:
    return _SOCKETS if _SOCKETS is not None else ALL_SOCKET_NAMES


def _tmux_out(socket: str, *args: str) -> str | None:
    try:
        return subprocess.check_output(
            [*tmux_base_argv(socket), *args],
            stderr=subprocess.DEVNULL,
            timeout=keepalive.SUBPROCESS_TIMEOUT,
            env=keepalive.tmux_env(),
        ).decode()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _float(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return 0.0


def _foreign_clients(socket: str) -> dict[str, int] | None:
    """Per-session count of attached clients that are not control-mode ones.

    Corral's own control channels are control-mode; anything else is a person
    attached in a terminal. An unset ``client_control_mode`` (older tmux) reads
    as "not control", which errs on the protective side.
    """
    out = _tmux_out(socket, "list-clients", "-F", _CLIENT_FORMAT)
    if out is None:
        return None
    counts: dict[str, int] = {}
    for line in out.splitlines():
        session, _, control = line.partition("|")
        if session and control.strip() != "1":
            counts[session] = counts.get(session, 0) + 1
    return counts


def _probe_socket(socket: str) -> list[Hosted] | None:
    if _SOCKETS is None and not liveness._socket_may_exist(socket):
        return None
    out = _tmux_out(socket, "list-sessions", "-F", _LIST_FORMAT)
    clients = _foreign_clients(socket) if out else {}
    if out is None or clients is None:
        return None  # cannot see the whole picture: do not act on this socket
    hosted: list[Hosted] = []
    for line in out.splitlines():
        parts = line.split("|")
        parsed = liveness._parse_managed_session_name(parts[0]) if len(parts) == 4 else None
        if parsed is None:
            continue
        hosted.append(Hosted(
            name=parts[0], runtime_id=parsed[0], ident=parsed[1], socket=socket,
            created_at=_float(parts[1]), session_activity=_float(parts[2]),
            window_activity=_float(parts[3]), foreign_clients=clients.get(parts[0], 0),
        ))
    return hosted


def probe_hosted() -> list[Hosted]:
    """Managed tmux sessions on the inspected sockets (empty when isolated)."""
    if _SOCKETS is None and os.environ.get("CORRAL_ISOLATE_MANAGED_HOSTS") == "1":
        return []
    seen: set[str] = set()
    hosted: list[Hosted] = []
    for socket in _sockets():
        for host in _probe_socket(socket) or ():
            if host.name not in seen:
                seen.add(host.name)
                hosted.append(host)
    return hosted


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def history_from_session(session: Mapping[str, Any]) -> History:
    """Boil one caller session dict down to what the decision needs."""
    stamps = [
        stamp
        for stamp in (_number(session.get(k)) for k in ("event_time", "mtime", "file_mtime"))
        if stamp is not None
    ]
    return History(
        status_tag=str(session.get("status_tag") or ""),
        provisional=bool(session.get("provisional")),
        active_at=max(stamps) if stamps else None,
        runtime_id=str(session.get("source") or ""),
        session_id=str(session.get("id") or ""),
        protect=bool(session.get("reclaim_protect")),
        history_path=str(session.get("path") or ""),
        cwd=str(session.get("cwd") or ""),
    )


def history_by_name(sessions: Iterable[Mapping[str, Any]]) -> dict[str, list[History]]:
    grouped: dict[str, list[History]] = {}
    for session in sessions:
        name = str(session.get("keepalive_name") or "")
        if name:
            grouped.setdefault(name, []).append(history_from_session(session))
    return grouped


def _background_state(
    hosted: Sequence[Hosted], history: Mapping[str, Sequence[History]],
) -> BackgroundState | None:
    """Shared background-work verdict for this pass.

    None when the probe itself is unusable — the caller then blocks the whole
    pass, because an unreadable protection source is never a licence to stop
    a session.
    """
    paths: dict[str, str] = {}
    for host in hosted:
        for entry in history.get(host.name, ()):
            if entry.history_path:
                paths[host.name] = entry.history_path
                break
    checker = busycheck.BusyChecker.from_probe(sockets=_sockets(), history_paths=paths)
    if checker is None:
        return None
    busy = checker.process_busy_names([host.name for host in hosted])
    return BackgroundState(
        busy_names=frozenset(busy),
        evidence={name: dict(verdict.evidence) for name, verdict in busy.items()},
        checker=checker,
    )


def _unreadable_context_source(
    busy: Any, waiting: Any, viewed: Any, pinned: Any, delegated: Any,
) -> str | None:
    for name, value in (
        ("attention_busy", busy),
        ("attention_waiting", waiting),
        ("host_viewers", viewed),
        ("pinned_keys", pinned),
        ("delegated_runs", delegated),
    ):
        if value is None:
            return name
    return None


def _build_context(
    now: float,
    hosted: Sequence[Hosted] = (),
    history: Mapping[str, Sequence[History]] | None = None,
    *,
    pressure: bool | None = None,
) -> Context | None:
    """Collect the protections that live outside the session list.

    Any source that cannot be read makes the whole pass a no-op: not knowing who
    is busy, watching, or pinned is never a licence to stop a session. The
    pressure value is folded in by the caller (it carries hysteresis state);
    when omitted the raw probe is used, which is what isolated tests expect.
    """
    from corral import embed, split_layout
    from corral.attention import AttentionStore
    from corral.control_store import RunStore

    store = AttentionStore()
    busy = store.busy_pairs()
    waiting = store.waiting_pairs()
    viewed = embed.live_host_viewer_names()
    pinned = split_layout.pinned_keys_effective()
    # Hosts of unsettled Agent delegation runs are leased like viewed ones.
    delegated = RunStore().active_hosts()
    missing = _unreadable_context_source(busy, waiting, viewed, pinned, delegated)
    if missing is not None:
        try:
            observe.event("reclaim_blocked", hosted=len(hosted), blocked={"context_unavailable": 1}, source=missing)
        except Exception:  # noqa: BLE001
            pass
        return None
    viewed = set(viewed) | delegated
    background = _background_state(hosted, history or {})
    if background is None:
        try:
            observe.event("reclaim_blocked", hosted=len(hosted), blocked={"context_unavailable": 1}, source="busycheck")
        except Exception:  # noqa: BLE001
            pass
        return None
    if pressure is None:
        pressure = memory_pressure()
    checker = background.checker
    readings: dict[str, tuple[int, float, str]] = {}
    pane_pids: dict[str, int] = {}
    if checker is not None:
        for host in hosted:
            sample = checker.tree_cpu(host.name)
            if sample is not None:
                readings[host.name] = sample
            pane_pid = checker.pane_pid(host.name)
            if pane_pid:
                pane_pids[host.name] = pane_pid
    cpu = reclaim_pass.observe_cpu(readings, now) if readings else {}
    # §6.4: a reachable account-wide server makes every OpenCode pane unprovable.
    shared_server = False
    if any(retirement.runtime_shares_background_server(host.runtime_id) for host in hosted):
        if checker is None:
            return None
        verdict = checker.shared_server_running("opencode")
        if verdict is None:
            try:
                observe.event(
                    "reclaim_blocked", hosted=len(hosted),
                    blocked={"context_unavailable": 1}, source="opencode_server",
                )
            except Exception:  # noqa: BLE001
                pass
            return None
        shared_server = verdict
    return policy_from_env(
        now,
        pressure=pressure,
        busy_pairs=frozenset(busy),
        waiting_pairs=frozenset(waiting),
        viewed=frozenset(viewed),
        pinned_keys=frozenset(pinned),
        background=background,
        cpu=cpu,
        pane_pids=pane_pids,
        shared_opencode_server=shared_server,
        retired_at=retirement.retired_at_map(now=now, window_seconds=COOLDOWN_SECONDS),
        resumed_at=retirement.resumed_at_map(now=now, window_seconds=RESUME_GRACE_SECONDS),
    )


# ---------------------------------------------------------------------- applying


def _kill_direct(name: str, socket: str) -> bool:
    try:
        proc = subprocess.run(
            [*tmux_base_argv(socket), "kill-session", "-t", f"={name}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=keepalive.SUBPROCESS_TIMEOUT, check=False, env=keepalive.tmux_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _default_kill(host: Hosted) -> bool:
    """End one session with the exact-match target, never a prefix match (INV-6)."""
    socket = host.socket or socket_for_session(host.name)
    stopped = _kill_direct(host.name, socket)
    if stopped:
        # 同样是「我们停的」：摘掉清单条目，免得下一轮误记成 pane_died。
        hosted_lifecycle.forget_session(host.name, reason="corral:kill-session")
    return stopped


def _after_kill(name: str) -> None:
    """Drop in-process caches that would otherwise keep a dead session 'alive'."""
    try:
        liveness.forget_alive(name)
        from corral import embed

        embed.close_channel(name)
    except Exception:  # noqa: BLE001 - housekeeping only
        pass


def _audit(event: str, verdict: Verdict, *, reason: str | None = None, **extra: Any) -> bool:
    """Write the observe event first; False when it could not be written.

    The old version swallowed the error and the kill proceeded anyway — the bug
    D2 names. The *durable* gate is ``retirement.append_record``; this mirrors
    the same evidence into ``events.log`` for continuity and reports failure
    instead of hiding it.
    """
    try:
        observe.event(
            event,
            session=verdict.name,
            reason=reason or verdict.reason,
            state=verdict.state,
            **{**verdict.evidence, **extra},
        )
    except Exception:  # noqa: BLE001 - the caller decides whether that blocks
        return False
    return True


def _default_record(record: retirement.RetirementRecord) -> bool:
    """Durable retirement record; ``retirement`` owns verification and D2."""
    return retirement.append_record(record)


def _record_for(verdict: Verdict, ctx: Context, identity: retirement.PaneIdentity) -> retirement.RetirementRecord:
    """Everything needed to resume, plus every field the verdict used (INV-14)."""
    host = verdict.host
    plan = None
    if verdict.session_id or verdict.history_path:
        plan = retirement.build_resume_plan(
            host.runtime_id,
            session_id=verdict.session_id,
            history_path=verdict.history_path,
            cwd=verdict.cwd,
        )
    return retirement.RetirementRecord(
        name=verdict.name,
        runtime_id=host.runtime_id,
        ident=host.ident,
        retired_at=ctx.now,
        session_id=verdict.session_id,
        history_path=verdict.history_path,
        cwd=verdict.cwd,
        status_tag=str(verdict.evidence.get("status_tag") or ""),
        state=verdict.state,
        idle_seconds=verdict.idle_seconds,
        reason=verdict.reason,
        pane_pid=identity.pane_pid,
        server_pid=identity.server_pid,
        resume_argv=plan[0] if plan else (),
        resume_cwd=plan[1] if plan else "",
        resume_ready=bool(plan),
        evidence=retirement.record_evidence(verdict.evidence),
    )


def _supersede(
    record: retirement.RetirementRecord,
    outcome: str,
    write: Callable[[retirement.RetirementRecord], bool],
    now: float,
) -> None:
    """Record that the intent did not become a retirement, keeping it durable."""
    write(replace(record, outcome=outcome, retired_at=max(now, record.retired_at)))


def _retire(
    verdict: Verdict,
    ctx: Context,
    *,
    kill: Callable[[Hosted], bool],
    after_kill: Callable[[str], None],
    record: Callable[[retirement.RetirementRecord], bool],
    identity: Callable[[str, str], retirement.PaneIdentity | None],
    resume_guard: Callable[..., str | None],
    report: PassReport,
) -> bool:
    """One retirement: lock, record, verify identity, kill (INV-5..INV-10)."""
    name = verdict.name
    host = verdict.host
    canonical = session_key({"source": host.runtime_id, "id": host.ident})
    if ctx.dry_run:
        # §9.1: full evidence, nothing killed.
        _audit("reclaim_would", verdict)
        report.skipped.append({"session": name, "reason": "dry_run"})
        return False
    # INV-7: retirement and native resume contend for the same per-session lock.
    with retirement.session_lock(canonical) as held:
        if not held:
            report.block("resume_race", name)
            return False
        before = identity(name, host.socket)
        if before is None:
            # INV-6/INV-12: cannot identify the pane, so cannot safely end it.
            report.block("pane_unknown", name)
            return False
        planned = _record_for(verdict, ctx, before)
        if verdict.has_history:
            # INV-15/§6.5: retire only where the resume is guaranteed by
            # construction. Memory is the cheaper error than an unrestorable
            # session, and a failure must never become a task for the owner.
            block = resume_guard(
                host.runtime_id,
                session_id=verdict.session_id,
                history_path=verdict.history_path,
                cwd=verdict.cwd,
            )
            if block is not None:
                report.block(block, name)
                return False
        if not record(planned):
            # D2: no durable record, no kill — and the block is reported.
            report.block("audit_unwritable", name)
            return False
        if not _audit("reclaim", verdict):
            _supersede(planned, "kill_failed", record, ctx.now)
            report.block("audit_unwritable", name)
            return False
        after = identity(name, host.socket)
        if after != before:
            _supersede(planned, "kill_failed", record, ctx.now)
            report.block("pane_replaced", name)
            return False
        if not kill(host):
            _audit("reclaim_failed", verdict)
            _supersede(planned, "kill_failed", record, ctx.now)
            report.block("kill_failed", name)
            return False
        after_kill(name)
        return True


def apply(
    verdicts: Sequence[Verdict],
    ctx: Context,
    *,
    kill: Callable[[Hosted], bool] = _default_kill,
    pressure_fn: Callable[[], bool] | None = None,
    after_kill: Callable[[str], None] = _after_kill,
    record: Callable[[retirement.RetirementRecord], bool] | None = None,
    identity: Callable[[str, str], retirement.PaneIdentity | None] | None = None,
    resume_guard: Callable[..., str | None] | None = None,
    report: PassReport | None = None,
) -> list[str]:
    """Stop the chosen sessions: durable record first, then an exact-match kill.

    Picked under pressure, a pass stops early once the pressure clears; from then
    on only sessions that also meet the normal idle threshold still qualify.
    Every refusal is counted in ``report.blocked`` — a failed record must never
    look like a quiet pass (D2).
    """
    report = report if report is not None else PassReport(at=ctx.now)
    record = record or _default_record
    identity = identity or retirement.pane_identity
    resume_guard = resume_guard or retirement.resume_block_reason
    check_pressure = pressure_fn or memory_pressure
    baseline_seconds = max(ctx.idle_minutes, ctx.floor_minutes) * 60.0
    pressured = ctx.pressure
    reclaimed: list[str] = []
    if retirement.server_operation_in_progress(now=ctx.now):
        # INV-11: never during a keepalive migration or server restart.
        for verdict in verdicts:
            report.block("server_operation", verdict.name)
        return []
    for verdict in verdicts:
        if pressured and reclaimed and not check_pressure():
            pressured = False
        if not pressured:
            if verdict.idle_seconds < baseline_seconds:
                continue
            if (
                report.parked > 0
                and ctx.resident_budget > 0
                and report.parked - len(reclaimed) <= ctx.resident_budget
            ):
                break
        if _retire(
            verdict, ctx, kill=kill, after_kill=after_kill, record=record,
            identity=identity, resume_guard=resume_guard, report=report,
        ):
            reclaimed.append(verdict.name)
    return reclaimed


# ------------------------------------------------------------------- entry point


def _claim_slot(now: float) -> int | None:
    """Take the machine-wide pass slot, or None when throttled / another pass runs."""
    try:
        directory = cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(directory / _STAMP_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    try:
        raw = os.read(fd, 64).decode("ascii", "ignore").strip()
        last = float(raw) if raw else 0.0
        if 0 <= now - last < _MIN_INTERVAL_SECONDS:
            _release_slot(fd)
            return None
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{now:.3f}".encode("ascii"))
    except (OSError, ValueError):
        _release_slot(fd)
        return None
    return fd


def _release_slot(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _pick(verdicts: Sequence[Verdict], ctx: Context) -> list[Verdict]:
    """Order eligible sessions (longest idle first) and apply resident budget."""
    picked = [verdict for verdict in verdicts if verdict.reclaim]
    if not picked:
        return []
    picked.sort(key=lambda verdict: verdict.idle_seconds, reverse=True)
    budget = max(0, ctx.budget)
    if budget <= 0:
        return picked
    parked_count = sum(1 for verdict in verdicts if verdict.parked)
    excess = max(0, parked_count - budget)
    if excess <= 0:
        return []
    if len(picked) > excess:
        picked = _break_ties(picked, excess, ctx)
    return picked[:excess]


def _pass_state(now: float) -> tuple[reclaim_pass.PressureState, dict[str, Any]]:
    """Fold this pass's pressure read into the hysteresis state (§3.6)."""
    state = reclaim_pass.advance(reclaim_pass.load_state(), memory_pressure(), now, **pressure_knobs())
    facts = pressure_facts()
    state = replace(state, swap_ratio=facts["swap_ratio"], pressure_level=facts["pressure_level"])
    reclaim_pass.save_state(state)
    return state, facts


def _run_pass(sessions_provider: Callable[[], Iterable[Mapping[str, Any]]], now: float) -> list[str]:
    hosted = probe_hosted()
    if not hosted:
        inv = hosted_lifecycle.read_inventory()
        if inv.get("server_pid") is not None or inv.get("sessions"):
            sessions = list(sessions_provider() or ())
            hosted_lifecycle.refresh_inventory(sessions=sessions, sockets=_sockets(), force=True)
        return []
    sessions = list(sessions_provider() or ())
    # §7 lifecycle: refresh the hosted inventory, record sessions whose pane died
    # while the server lives, and reconcile a server pid that changed. Read-only
    # with respect to reclaim decisions; it never raises.
    hosted_lifecycle.refresh_inventory(sessions=sessions, sockets=_sockets())
    history = history_by_name(sessions)
    state, facts = _pass_state(now)
    ctx = _build_context(now, hosted, history, pressure=state.armed)
    if ctx is None:
        return []
    report = PassReport(
        at=now,
        hosted=len(hosted),
        pressure=state.armed,
        pressure_samples=state.true_count,
        swap_ratio=facts["swap_ratio"],
        pressure_level=facts["pressure_level"],
        dry_run=ctx.dry_run,
    )
    verdicts = [evaluate(host, history.get(host.name, ()), ctx) for host in hosted]
    parked = sum(1 for verdict in verdicts if verdict.parked)
    over_budget = ctx.resident_budget > 0 and parked > ctx.budget
    state = reclaim_pass.escalate(state, over_budget=over_budget and state.armed)
    reclaim_pass.save_state(state)
    report.parked = parked
    report.over_budget = over_budget
    report.budget_steps = state.budget_steps
    report.effective_idle_minutes = ctx.effective_idle_minutes
    report.threshold_reason = ctx.threshold_reason
    report.resident_budget = ctx.budget
    for verdict in verdicts:
        if not verdict.reclaim:
            report.blocked[verdict.reason] = report.blocked.get(verdict.reason, 0) + 1
    report.retired = apply(_pick(verdicts, ctx), ctx, report=report)
    _record_pass(report)
    return report.retired


_LAST_REPORT: PassReport | None = None


def _record_pass(report: PassReport) -> None:
    """One pass event per pass, and a loud block event when nothing retired."""
    global _LAST_REPORT
    _LAST_REPORT = report
    try:
        observe.event("reclaim_pass", **report.as_event())
        if not report.retired and report.hosted and report.blocked:
            # §4.4 step 3: loud in the log, silent in the UI.
            observe.event("reclaim_blocked", **report.as_event())
    except Exception:  # noqa: BLE001 - observability must not break the pass
        pass


def last_pass() -> PassReport | None:
    """The most recent pass of this process, for tests and diagnosis."""
    return _LAST_REPORT


def policy_view(now: float | None = None) -> dict[str, Any]:
    """Policy numbers a session list may read; no forks, no destructive work.

    ``effective_idle_minutes`` below ``DEFAULT_IDLE_MINUTES`` means this machine
    is currently retiring earlier than the shipped contract, and
    ``threshold_reason`` says why (``pressure``, ``budget_pressure`` or
    ``floor``). Handed to the UI owner so a shortened threshold can be shown;
    this module renders nothing.
    """
    report = _LAST_REPORT
    if report is not None:
        return {
            "effective_idle_minutes": report.effective_idle_minutes,
            "threshold_reason": report.threshold_reason,
            "pressure": report.pressure,
            "resident_budget": report.resident_budget,
            "parked": report.parked,
            "over_budget": report.over_budget,
        }
    state = reclaim_pass.load_state()
    ctx = policy_from_env(time.time() if now is None else now, pressure=state.armed)
    return {
        "effective_idle_minutes": ctx.effective_idle_minutes,
        "threshold_reason": ctx.threshold_reason,
        "pressure": state.armed,
        "resident_budget": ctx.budget,
        "parked": None,
        "over_budget": None,
    }


def _maybe_reclaim(
    sessions_provider: Callable[[], Iterable[Mapping[str, Any]]] | None, now: float | None,
) -> list[str]:
    if sessions_provider is None or not enabled():
        return []
    if time.monotonic() - _STARTED_AT < _MIN_PROCESS_AGE_SECONDS:
        return []
    when = time.time() if now is None else float(now)
    fd = _claim_slot(when)
    if fd is None:
        return []
    try:
        return _run_pass(sessions_provider, when)
    finally:
        _release_slot(fd)


def maybe_reclaim(
    sessions_provider: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
    *,
    now: float | None = None,
) -> list[str]:
    """Stop provably inactive hosted sessions; returns the tmux names it stopped.

    Throttled to one pass per minute across every Corral process, skipped while
    this process is young, silent, and safe from any thread. ``sessions_provider``
    yields the caller's annotated session dicts; without it (or for a hosted
    session it does not list) history is unknown and nothing is stopped. A
    session dict may carry ``reclaim_protect: True`` to veto its own reclaim.
    Never raises.
    """
    try:
        return _maybe_reclaim(sessions_provider, now)
    except Exception as exc:  # noqa: BLE001 - reclaim must never disturb its caller
        try:
            observe.debug("reclaim_error", error=repr(exc))
        except Exception:  # noqa: BLE001
            pass
        return []
