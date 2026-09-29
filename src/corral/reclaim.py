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
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sesskit.models import session_key
from sesskit.titles import STATUS_ABORTED, STATUS_DONE, STATUS_PENDING

from corral import busycheck, keepalive, liveness, observe
from corral.legacy_names import (
    ALL_SOCKET_NAMES,
    cache_dir,
    env_is_disabled,
    getenv,
    socket_for_session,
    tmux_base_argv,
)

DEFAULT_IDLE_MINUTES = 120.0
DEFAULT_PRESSURE_IDLE_MINUTES = 10.0
GRACE_SECONDS = 30 * 60.0
MAX_PER_PASS = 3

# One pass per interval machine-wide; a young process waits before its first pass
# so that opening a TUI never kills anything (the clock starts at first import).
_MIN_INTERVAL_SECONDS = 60.0
_MIN_PROCESS_AGE_SECONDS = 120.0
_STARTED_AT = time.monotonic()
_STAMP_NAME = "reclaim.stamp"

_SWAP_RATIO = 0.6
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
    viewed: Collection[str] = ()
    pinned_keys: Collection[str] = ()
    idle_minutes: float = DEFAULT_IDLE_MINUTES
    pressure_idle_minutes: float = DEFAULT_PRESSURE_IDLE_MINUTES
    grace_seconds: float = GRACE_SECONDS
    max_per_pass: int = MAX_PER_PASS
    background: BackgroundState | None = None

    @property
    def threshold_minutes(self) -> float:
        if self.pressure:
            return min(self.idle_minutes, self.pressure_idle_minutes)
        return self.idle_minutes


@dataclass(frozen=True)
class Verdict:
    host: Hosted
    reclaim: bool
    reason: str  # "inactive" or the protection that held
    idle_seconds: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.host.name


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
        **facts,
    )


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


def evaluate(host: Hosted, entries: Sequence[History], ctx: Context) -> Verdict:
    """Decide one hosted session. Reclaimable only if every protection is clear."""
    now = ctx.now
    real = [e for e in entries if not e.provisional]
    status = real[0].status_tag if real else (entries[0].status_tag if entries else "")
    evidence: dict[str, Any] = {
        "runtime": host.runtime_id,
        "socket": host.socket,
        "status_tag": status,
        "provisional": bool(entries) and not real,
        "pressure": ctx.pressure,
        "threshold_min": ctx.threshold_minutes,
        "created_age_min": _minutes(now - host.created_at) if _valid(host.created_at) else None,
    }

    def held(reason: str) -> Verdict:
        return Verdict(host, False, reason, evidence=evidence)

    if not entries:
        return held("history_unknown")
    if any(e.protect for e in entries):
        return held("caller_protected")
    if not _valid(host.created_at) or now - host.created_at < ctx.grace_seconds:
        return held("grace")
    if host.name in ctx.viewed:
        return held("viewed")
    if host.foreign_clients > 0:
        return held("attached_client")
    if _is_pinned(host, entries, ctx.pinned_keys):
        return held("pinned")
    if _is_busy(host, entries, ctx.busy_pairs):
        return held("busy")
    if ctx.background is not None and host.name in ctx.background.busy_names:
        evidence["background"] = dict(ctx.background.evidence.get(host.name, {}))
        return held("background_busy")
    blocked = _history_block(real)
    if blocked:
        return held(blocked)
    window_gates = not real or WINDOW_OUTPUT_GATES_HISTORY
    if not _valid(host.session_activity) or (window_gates and not _valid(host.window_activity)):
        return held("tmux_unknown")

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
    if idle < ctx.threshold_minutes * 60.0:
        return Verdict(host, False, "recent_activity", idle, evidence)
    if ctx.background is not None:
        transcript = ctx.background.check_transcript(host, entries)
        if transcript is not None:
            pending, transcript_evidence = transcript
            evidence["background"] = transcript_evidence
            if pending:
                return Verdict(host, False, "background_busy", idle, evidence)
    return Verdict(host, True, "inactive", idle, evidence)


def decide(
    hosted: Sequence[Hosted], history: Mapping[str, Sequence[History]], ctx: Context,
) -> list[Verdict]:
    """Reclaimable sessions, longest idle first, capped per pass."""
    picked = [
        verdict
        for verdict in (evaluate(h, history.get(h.name, ()), ctx) for h in hosted)
        if verdict.reclaim
    ]
    picked.sort(key=lambda verdict: verdict.idle_seconds, reverse=True)
    return picked[: max(0, ctx.max_per_pass)]


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


def _sysctl(name: str) -> str | None:
    try:
        return subprocess.check_output(
            ["sysctl", "-n", name], stderr=subprocess.DEVNULL, timeout=2,
        ).decode()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _darwin_pressure() -> bool:
    ratio = _parse_swap_ratio(_sysctl("vm.swapusage") or "")
    if ratio is not None and ratio >= _SWAP_RATIO:
        return True
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


def _build_context(
    now: float,
    hosted: Sequence[Hosted] = (),
    history: Mapping[str, Sequence[History]] | None = None,
) -> Context | None:
    """Collect the protections that live outside the session list.

    Any source that cannot be read makes the whole pass a no-op: not knowing who
    is busy, watching, or pinned is never a licence to stop a session.
    """
    from corral import embed, split_layout
    from corral.attention import AttentionStore

    busy = AttentionStore().busy_pairs()
    viewed = embed.live_host_viewer_names()
    pinned = split_layout.pinned_keys_effective()
    if busy is None or viewed is None or pinned is None:
        return None
    background = _background_state(hosted, history or {})
    if background is None:
        return None
    return policy_from_env(
        now,
        pressure=memory_pressure(),
        busy_pairs=frozenset(busy),
        viewed=frozenset(viewed),
        pinned_keys=frozenset(pinned),
        background=background,
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
    if host.socket == socket_for_session(host.name):
        return keepalive.kill(host.name)
    return _kill_direct(host.name, host.socket)


def _after_kill(name: str) -> None:
    """Drop in-process caches that would otherwise keep a dead session 'alive'."""
    try:
        liveness.forget_alive(name)
        from corral import embed

        embed.close_channel(name)
    except Exception:  # noqa: BLE001 - housekeeping only
        pass


def _audit(event: str, verdict: Verdict) -> None:
    try:
        observe.event(event, session=verdict.name, reason=verdict.reason, **verdict.evidence)
    except Exception:  # noqa: BLE001 - the audit must not turn into a crash either
        pass


def apply(
    verdicts: Sequence[Verdict],
    ctx: Context,
    *,
    kill: Callable[[Hosted], bool] = _default_kill,
    pressure_fn: Callable[[], bool] = memory_pressure,
    after_kill: Callable[[str], None] = _after_kill,
) -> list[str]:
    """Stop the chosen sessions, writing the audit event first.

    Picked under pressure, a pass stops early once the pressure clears; from then
    on only sessions that also meet the normal idle threshold still qualify.
    """
    baseline_seconds = ctx.idle_minutes * 60.0
    pressured = ctx.pressure
    reclaimed: list[str] = []
    for verdict in verdicts:
        if pressured and reclaimed and not pressure_fn():
            pressured = False
        if not pressured and verdict.idle_seconds < baseline_seconds:
            continue
        _audit("reclaim", verdict)
        if not kill(verdict.host):
            _audit("reclaim_failed", verdict)
            continue
        after_kill(verdict.name)
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


def _run_pass(sessions_provider: Callable[[], Iterable[Mapping[str, Any]]], now: float) -> list[str]:
    hosted = probe_hosted()
    if not hosted:
        return []
    history = history_by_name(sessions_provider() or ())
    ctx = _build_context(now, hosted, history)
    if ctx is None:
        return []
    return apply(decide(hosted, history, ctx), ctx)


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
