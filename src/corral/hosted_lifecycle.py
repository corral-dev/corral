"""Durable lifecycle journal and hosted-session inventory for the keepalive server.

Implements §7 of ``docs/design/HOSTED_SESSION_LIFECYCLE_DESIGN.md`` ("server loss
as a first-class case"). Two local records, both under the Corral cache directory
and both deliberately outside ``events.log``:

``hosted-lifecycle.log``
    Append-only JSONL journal of the four §7.2 events, one JSON object per line
    keyed by ``event``. Retained 7 days and pruned by whole day once it passes
    2 MiB, then trimmed from the oldest line if one day alone exceeds the cap, so
    it stays bounded without ever rotating a server death out within minutes the
    way the 256 KiB ``events.log`` budget does (2026-11-15 incident, §7.1).

``hosted-inventory.json``
    Continuously refreshed list of the sessions the product server hosts (name,
    runtime, ident, session id, history path, cwd, state), plus the pid and start
    time of the server that hosted them and its last recorded exit. Rewritten at
    most once per 60 s by the reclaim pass and immediately on host/stop. It is
    what makes "who was on the server when it died" answerable at the next start.

Contract:

- Total. No public function raises into a host, stop, scan or start path. A
  failed write is *not* swallowed (unlike ``reclaim._audit``): it is surfaced as
  an ``error`` event in ``events.log`` with the full traceback in
  ``embed-error.log``, and remembered in :func:`last_error`. A missing
  ``keepalive_server_started`` must stay distinguishable from "no server was
  ever started".
- ``unknown`` is never zero. An unreadable socket, inventory or attention
  database produces no "everything is gone" conclusion.
- Paths resolve per call, so ``CORRAL_CACHE_DIR`` redirection works in tests
  (``observe.EVENTS_LOG`` is resolved at import and does not).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from corral.legacy_names import ALL_SOCKET_NAMES, SOCKET_NAME, cache_dir

SERVER_STARTED = "keepalive_server_started"
SERVER_EXIT = "keepalive_server_exit"
SERVER_LOST = "keepalive_server_lost"
SESSION_DIED = "hosted_session_died"

LOG_NAME = "hosted-lifecycle.log"
INVENTORY_NAME = "hosted-inventory.json"
LOCK_NAME = "hosted-lifecycle.lock"

MAX_LOG_BYTES = 2 * 1024 * 1024
RETENTION_DAYS = 7
INVENTORY_INTERVAL_SECONDS = 60.0
STOPPED_TTL_SECONDS = 24 * 3600
MAX_STOPPED_ENTRIES = 200
MAX_CRASH_NAMES = 5
_MAX_OBSERVED_SOCKETS = 4

_INVENTORY_VERSION = 1
_SUBPROCESS_TIMEOUT = 2.0
_FIELDS = "|".join((
    "#{session_name}", "#{session_created}", "#{session_activity}", "#{window_activity}",
    "#{pane_current_path}",
))
_TS_RE = re.compile(r'"ts":\s*"(\d{4}-\d{2}-\d{2})')
_SWAP_USED_RE = re.compile(r"used\s*=\s*([\d.]+)M")
_SWAP_TOTAL_RE = re.compile(r"total\s*=\s*([\d.]+)M")
_PAGE_SIZE_RE = re.compile(r"page size of (\d+) bytes")
_CRASH_REPORT_DIR = Path("/Library/Logs/DiagnosticReports")

_last_error: dict[str, str] | None = None


# ------------------------------------------------------------------------ paths


def log_path() -> Path:
    return cache_dir() / LOG_NAME


def inventory_path() -> Path:
    return cache_dir() / INVENTORY_NAME


def lock_path() -> Path:
    return cache_dir() / LOCK_NAME


def last_error() -> dict[str, str] | None:
    """Last failed record, for diagnostics and tests; None after a clean write."""
    return dict(_last_error) if _last_error else None


def reset_for_tests() -> None:
    global _last_error
    _last_error = None


# -------------------------------------------------------------------- recording


def record(event: str, **fields: Any) -> bool:
    """Append one lifecycle record. Returns False (never raises) when unwritable."""
    global _last_error
    try:
        # `pid` belongs to the server or session under discussion (§7.2); the
        # writer is attributed separately so `corral diagnose`-style readers keep
        # the §7 field names.
        payload = {"ts": _now_iso(), "event": event, "corral_pid": os.getpid(), **fields}
        encoded = (json.dumps(payload, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _prune_log(path, len(encoded))
        _append(path, encoded)
    except OSError as exc:
        _surface(event, exc)
        return False
    _last_error = None
    return True


def _append(path: Path, encoded: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)


def _surface(event: str, exc: BaseException) -> None:
    """Report a failed lifecycle write instead of hiding it (rule 13, §7.5)."""
    global _last_error
    _last_error = {"event": event, "error": f"{type(exc).__name__}: {exc}", "at": _now_iso()}
    try:
        from corral import observe

        observe.log_exception(f"hosted-lifecycle:{event}", exc)
    except Exception:  # noqa: BLE001 - the diagnostic channel is itself best effort
        pass


def _prune_log(path: Path, incoming: int) -> None:
    """Keep the journal bounded: whole days past the retention window, then size."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size + incoming <= MAX_LOG_BYTES:
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    kept = _retained_lines(lines)
    budget = MAX_LOG_BYTES - incoming
    blob = "\n".join(kept) + ("\n" if kept else "")
    if len(blob.encode("utf-8")) > budget:
        blob = "\n".join(_newest_lines_within(kept, budget)) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(blob, encoding="utf-8")
    os.replace(tmp, path)


def _retained_lines(lines: Sequence[str]) -> list[str]:
    cutoff = (dt.date.today() - dt.timedelta(days=RETENTION_DAYS)).isoformat()
    kept: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        match = _TS_RE.search(line[:80])
        if match is None or match.group(1) >= cutoff:
            kept.append(line)  # unparsable lines are never dropped silently
    return kept


def _newest_lines_within(lines: Sequence[str], budget: int) -> list[str]:
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line.encode("utf-8")) + 1
        if used + cost > budget and kept:
            break
        kept.append(line)
        used += cost
    kept.reverse()
    return kept


# -------------------------------------------------------------------- inventory


def read_inventory() -> dict[str, Any]:
    """Last written inventory; an unreadable or foreign file reads as empty."""
    try:
        data = json.loads(inventory_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if not isinstance(data.get("sessions"), dict):
        data["sessions"] = {}
    if not isinstance(data.get("recently_stopped"), dict):
        data["recently_stopped"] = {}
    return data


def _write_inventory(payload: Mapping[str, Any]) -> None:
    path = inventory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n"
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def _lock() -> Iterator[bool]:
    """Non-blocking process lock: a busy peer is already writing the same state."""
    try:
        path = lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "w", encoding="ascii")
    except OSError as exc:
        _surface("inventory_lock", exc)
        yield False
        return
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def refresh_inventory(
    *,
    sessions: Iterable[Mapping[str, Any]] = (),
    sockets: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Rewrite the hosted-session inventory from a live tmux listing.

    ``sessions`` are Corral's own session dicts (``keepalive_name``, ``id``,
    ``path``, ``cwd``): they supply the resume inputs tmux cannot know. Runs at
    most once per ``INVENTORY_INTERVAL_SECONDS`` unless ``force``; a host or stop
    path passes ``force=True``. An unreadable socket writes nothing.
    """
    try:
        return _refresh_inventory(sessions=sessions, sockets=sockets, env=env, force=force)
    except Exception as exc:  # noqa: BLE001 - inventory refresh must never disturb a pass
        _surface("inventory", exc)
        return read_inventory()


def _refresh_inventory(
    *,
    sessions: Iterable[Mapping[str, Any]],
    sockets: Sequence[str] | None,
    env: Mapping[str, str] | None,
    force: bool,
) -> dict[str, Any]:
    if os.environ.get("CORRAL_ISOLATE_MANAGED_HOSTS") == "1":
        return read_inventory()  # isolated test resources must not see a real server
    resolved = tuple(sockets) if sockets is not None else ALL_SOCKET_NAMES
    if not resolved:
        return read_inventory()
    environment = _env(env)
    path = inventory_path()
    if not force and _fresh(path):
        return read_inventory()
    live = _probe_sessions(resolved, environment)
    if live is None:
        return read_inventory()  # unknown is not zero (INV-12)
    with _lock() as held:
        if not held:
            return read_inventory()
        inventory = read_inventory()
        previous = dict(inventory.get("sessions") or {})
        inventory, reported = _reconcile_server(inventory, environment, records=previous)
        if not inventory.get("socket"):
            inventory = _adopt_server(inventory, resolved, environment, len(previous), len(live))
        busy = _busy_names()
        sources = _sources(sessions)
        entries = {
            name: _session_entry(name, entry, sources.get(name) or {}, busy)
            for name, entry in live.items()
        }
        for name, entry in previous.items():
            if name in live or name in reported or _recently_stopped(inventory, name):
                continue
            _record_session_died(name, entry, cause="pane_died")
        inventory = {
            **inventory,
            "version": _INVENTORY_VERSION,
            "updated_at": _now_iso(),
            "sessions": entries,
            "recently_stopped": _prune_stopped(dict(inventory.get("recently_stopped") or {})),
        }
        inventory.pop("pending_restores", None)
        _write_inventory(inventory)
        return inventory


def _fresh(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime < INVENTORY_INTERVAL_SECONDS
    except OSError:
        return False


def _sources(sessions: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """name -> resume inputs from Corral's own session dicts."""
    out: dict[str, dict[str, Any]] = {}
    for session in sessions:
        name = str(session.get("keepalive_name") or "")
        if not name:
            continue
        candidate = {
            "session_id": str(session.get("id") or ""),
            "history_path": str(session.get("path") or ""),
            "cwd": str(session.get("cwd") or ""),
        }
        current = out.get(name)
        if current is None or (not current.get("history_path") and candidate["history_path"]):
            out[name] = candidate
    return out


def _busy_names() -> set[tuple[str, str]]:
    from corral.attention import AttentionStore

    pairs = AttentionStore().busy_pairs()
    return set(pairs or ())  # unreadable database means "nobody known busy"


def _session_entry(
    name: str,
    live: Mapping[str, Any],
    source: Mapping[str, Any],
    busy: set[tuple[str, str]],
) -> dict[str, Any]:
    from corral import keepalive, liveness

    parsed = liveness._parse_managed_session_name(name)
    runtime, ident = parsed if parsed is not None else ("", "")
    activity = max(
        (value for value in (live.get("session_activity"), live.get("window_activity"))
         if isinstance(value, (int, float))),
        default=0.0,
    )
    working = any(
        rid == runtime and keepalive._ident_matches_session_id(ident, sid) for rid, sid in busy
    )
    return {
        "runtime": runtime,
        "ident": ident,
        "socket": str(live.get("socket") or ""),
        "session_id": str(source.get("session_id") or ""),
        "history_path": str(source.get("history_path") or ""),
        "cwd": str(source.get("cwd") or live.get("cwd") or ""),
        # §2's richer state machine is not implemented; this records working vs
        # unknown from attention evidence rather than pretending to know "parked".
        "state": "working" if working else "unknown",
        "state_at": _iso(activity) if activity else None,
        "created_at": _iso(live.get("created_at")) if live.get("created_at") else None,
    }


def _probe_sessions(sockets: Sequence[str], env: Mapping[str, str]) -> dict[str, dict[str, Any]] | None:
    """Managed live sessions per socket; None when a running socket cannot be read.

    A probe that raises is "unknown" too: an unreadable source is never allowed to
    become "no sessions" (INV-12), and it must not turn into a loss record.
    """
    try:
        return _probe_sessions_unsafe(sockets, env)
    except Exception:  # noqa: BLE001 - unknown is not zero
        return None


def _probe_sessions_unsafe(
    sockets: Sequence[str], env: Mapping[str, str],
) -> dict[str, dict[str, Any]] | None:
    from corral import liveness, tmux_server

    if shutil.which("tmux", path=env.get("PATH")) is None:
        return {}
    live: dict[str, dict[str, Any]] = {}
    for socket in sockets:
        if not tmux_server.server_running(socket, env):
            continue
        out = _tmux(socket, ("list-sessions", "-F", _FIELDS), env)
        if out is None:
            return None
        for line in out.splitlines():
            parts = line.split("|", 4)
            if not parts or liveness._parse_managed_session_name(parts[0]) is None:
                continue
            live[parts[0]] = {
                "socket": socket,
                "created_at": _float(parts[1]) if len(parts) > 1 else None,
                "session_activity": _float(parts[2]) if len(parts) > 2 else None,
                "window_activity": _float(parts[3]) if len(parts) > 3 else None,
                "cwd": parts[4] if len(parts) > 4 else "",
            }
    return live


def _tmux(socket: str, args: Sequence[str], env: Mapping[str, str]) -> str | None:
    from corral.legacy_names import tmux_base_argv

    try:
        proc = subprocess.run(
            [*tmux_base_argv(socket), *args], capture_output=True, text=True,
            timeout=_SUBPROCESS_TIMEOUT, check=False, env=dict(env),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _recently_stopped(inventory: Mapping[str, Any], name: str) -> bool:
    return name in (inventory.get("recently_stopped") or {})


def _prune_stopped(stopped: Mapping[str, Any]) -> dict[str, Any]:
    cutoff = time.time() - STOPPED_TTL_SECONDS
    fresh = {
        name: stamp for name, stamp in stopped.items()
        if isinstance(stamp, (int, float)) and float(stamp) >= cutoff
    }
    if len(fresh) <= MAX_STOPPED_ENTRIES:
        return fresh
    newest = sorted(fresh.items(), key=lambda item: float(item[1]), reverse=True)
    return dict(newest[:MAX_STOPPED_ENTRIES])


# ------------------------------------------------------------------ server state


def observe_server(
    socket: str,
    *,
    config_path: str = "",
    env: Mapping[str, str] | None = None,
    started: bool | None = None,
) -> dict[str, Any]:
    """Record a keepalive server start and reconcile any unrecorded loss.

    ``started`` is what ``ensure_server`` reports: True when this call started the
    server, False/None when it found one running. A server pid already in the
    inventory is not recorded twice.
    """
    try:
        return _observe_server(socket, config_path=config_path, env=env, started=started)
    except Exception as exc:  # noqa: BLE001 - a host path must survive a broken record
        _surface(SERVER_STARTED, exc)
        return read_inventory()


def _observe_server(
    socket: str,
    *,
    config_path: str,
    env: Mapping[str, str] | None,
    started: bool | None,
) -> dict[str, Any]:
    environment = _env(env)
    rebased = False
    with _lock() as held:
        if not held:
            return read_inventory()
        inventory = read_inventory()
        tracked = str(inventory.get("socket") or "")
        recorded_pid = _int(inventory.get("server_pid"))
        pid = _server_pid(socket, environment)
        if pid is None:
            return inventory  # nothing running yet; tmux starts it on new-session
        expected = len(inventory.get("sessions") or {})
        lost = False
        if tracked in ("", socket):
            if recorded_pid is not None and recorded_pid != pid:
                inventory, _reported = _reconcile_server(inventory, environment)
                lost = True
            if lost or recorded_pid != pid:
                _record_started(
                    socket, pid, config_path, environment, started, expected,
                    sessions_live=len(_probe_sessions((socket,), environment) or {}),
                )
                rebased = True
            inventory = {
                **inventory,
                "version": _INVENTORY_VERSION,
                "socket": socket,
                "server_pid": pid,
                "server_started_at": _server_started_at(pid),
                "server_running": True,
                "updated_at": _now_iso(),
            }
            _write_inventory(inventory)
        else:
            # A transition socket: record the observation, leave the tracked product
            # server untouched so a rename-era socket cannot rewrite its history.
            # Dedupe by pid so this can never become one line per host launch.
            observed = dict(inventory.get("observed") or {})
            if _int(observed.get(socket)) != pid:
                _record_started(
                    socket, pid, config_path, environment, started, expected,
                    sessions_live=len(_probe_sessions((socket,), environment) or {}),
                )
                observed[socket] = pid
                inventory["observed"] = dict(sorted(observed.items())[-_MAX_OBSERVED_SOCKETS:])
                _write_inventory(inventory)
    if rebased:
        # A fresh server is the moment the post-mortem baseline matters: record the
        # sessions it already hosts now, not only at the next 60 s pass.
        return refresh_inventory(force=True, sockets=(socket,), env=environment)
    return inventory


def _adopt_server(
    inventory: dict[str, Any],
    sockets: Sequence[str],
    env: Mapping[str, str],
    sessions_expected: int,
    sessions_live: int,
) -> dict[str, Any]:
    """Record a server Corral found running but never wrote down itself."""
    socket = SOCKET_NAME if SOCKET_NAME in sockets else sockets[0]
    pid = _server_pid(socket, env)
    if pid is None:
        return inventory
    _record_started(
        socket, pid, "", env, None, sessions_expected, sessions_live=sessions_live,
    )
    return {
        **inventory,
        "version": _INVENTORY_VERSION,
        "socket": socket,
        "server_pid": pid,
        "server_started_at": _server_started_at(pid),
        "server_running": True,
        "updated_at": _now_iso(),
    }


def _record_started(
    socket: str,
    pid: int,
    config_path: str,
    env: Mapping[str, str],
    started: bool | None,
    sessions_expected: int,
    *,
    sessions_live: int | None = None,
) -> bool:
    from corral import tmux_server

    # Scheduling class is corroborating evidence, never a prerequisite: a failing
    # diagnosis probe must not cost the start record (only a failed write does).
    report: Mapping[str, Any] = {}
    try:
        raw = tmux_server.server_report(socket, env)
        if isinstance(raw, Mapping):
            report = raw
    except Exception:  # noqa: BLE001 - optional evidence
        report = {}
    priority = report.get("priority")
    interactive = bool(report.get("interactive_job"))
    if sessions_live is None:
        sessions_live = len(_probe_sessions((socket,), env) or {})
    return record(
        SERVER_STARTED,
        socket=socket,
        pid=pid,
        tmux_version=_tmux_version(env),
        config_path=str(config_path or ""),
        config_digest=_digest(config_path),
        launchd_label=f"{tmux_server.LABEL_PREFIX}.{socket}",
        interactive_job=interactive,
        priority=priority,
        plist_digest=_digest(tmux_server.cache_dir() / "launchd"
                             / f"{tmux_server.LABEL_PREFIX}.{socket}.plist"),
        started_by_ensure_server=bool(started),
        source="ensure_server" if started else "found_running",
        sessions_expected=sessions_expected,
        sessions_live=sessions_live,
        server_started_at=_server_started_at(pid),
    )


def record_server_exit(
    socket: str,
    *,
    reason: str,
    initiated_by: str = "",
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Record that Corral is about to end the server, before it acts.

    Its presence is the difference between "we stopped it" and "it disappeared";
    :func:`confirm_server_exit` closes the record with the after facts.
    """
    try:
        environment = _env(env)
        pid = _server_pid(socket, environment)
        snapshot = {
            "socket": socket,
            "pid": pid,
            "reason": reason,
            "initiated_by": initiated_by,
            "observed_at": _now_iso(),
            "socket_answers": pid is not None,
            "sessions_before": _count_sessions(socket, environment),
            "uptime_seconds": _uptime_seconds(pid),
        }
        record(SERVER_EXIT, phase="requested", **snapshot)
        with _lock() as held:
            if held:
                inventory = read_inventory()
                inventory["last_exit"] = {
                    "pid": pid, "reason": reason, "initiated_by": initiated_by,
                    "at": snapshot["observed_at"],
                }
                inventory["server_running"] = False
                inventory["updated_at"] = snapshot["observed_at"]
                _write_inventory(inventory)
        return snapshot
    except Exception as exc:  # noqa: BLE001 - a stop path must survive a broken record
        _surface(SERVER_EXIT, exc)
        return {}


def confirm_server_exit(
    snapshot: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Close a :func:`record_server_exit` record with what the stop left behind."""
    try:
        if not snapshot:
            return False
        environment = _env(env)
        socket = str(snapshot.get("socket") or SOCKET_NAME)
        pid = _server_pid(socket, environment)
        return record(
            SERVER_EXIT,
            phase="observed",
            socket=socket,
            pid=snapshot.get("pid"),
            reason=snapshot.get("reason"),
            initiated_by=snapshot.get("initiated_by"),
            observed_at=_now_iso(),
            uptime_seconds=snapshot.get("uptime_seconds"),
            socket_answers=pid is not None,
            sessions_before=snapshot.get("sessions_before"),
            sessions_after=_count_sessions(socket, environment),
        )
    except Exception as exc:  # noqa: BLE001
        _surface(SERVER_EXIT, exc)
        return False


def _reconcile_server(
    inventory: dict[str, Any],
    env: Mapping[str, str],
    *,
    records: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], set[str]]:
    """Mark the tracked server's sessions dead when its pid changed or vanished.

    2026-11-18 owner ruling: Corral never auto-launches agent sessions in the
    background after server loss or machine reboot, so ``pending_restores`` is
    never populated and any stale queue from earlier versions is cleared.
    """
    socket = str(inventory.get("socket") or "")
    recorded_pid = _int(inventory.get("server_pid"))
    if not socket or recorded_pid is None:
        return inventory, set()
    pid = _server_pid(socket, env)
    if pid == recorded_pid:
        return inventory, set()
    sessions = dict(records if records is not None else inventory.get("sessions") or {})
    last_exit = dict(inventory.get("last_exit") or {})
    recorded_exit = _int(last_exit.get("pid")) == recorded_pid
    prior_ts = _parse_iso(inventory.get("server_started_at")) or _parse_iso(inventory.get("updated_at"))
    current_boot = boot_time()
    rebooted = (
        not recorded_exit
        and current_boot is not None
        and prior_ts is not None
        and current_boot > prior_ts
    )
    cause = "machine_reboot" if rebooted else "server_lost"
    reported: set[str] = set()
    if not recorded_exit:
        # A recorded exit names the reason and the requester; its absence is itself
        # the finding this journal exists for (§7.3).
        record(
            SERVER_LOST,
            socket=socket,
            last_pid=recorded_pid,
            last_heartbeat=inventory.get("updated_at"),
            recorded_exit=False,
            machine_reboot=rebooted,
            observed_at=_now_iso(),
            socket_answers=pid is not None,
            sessions_before=len(sessions),
            sessions_after=_count_sessions(socket, env),
            new_pid=pid,
            **_survival_evidence(inventory.get("updated_at")),
        )
    for name, entry in sessions.items():
        if str((entry or {}).get("socket") or socket) != socket:
            continue  # another socket's session: left to the per-session diff
        _record_session_died(
            name, entry, cause=cause,
            server_exit_recorded=recorded_exit,
            initiated_by=last_exit.get("initiated_by") or "",
        )
        reported.add(name)
    surviving = {name: entry for name, entry in sessions.items() if name not in reported}
    updated = {
        **inventory,
        "server_pid": pid,
        "server_started_at": _server_started_at(pid) if pid is not None else None,
        "server_running": pid is not None,
        "sessions": surviving,
        "updated_at": _now_iso(),
    }
    updated.pop("pending_restores", None)
    return updated, reported


def _record_session_died(name: str, entry: Mapping[str, Any], *, cause: str, **extra: Any) -> bool:
    return record(
        SESSION_DIED,
        name=name,
        runtime=entry.get("runtime") or "",
        ident=entry.get("ident") or "",
        socket=entry.get("socket") or "",
        cause=cause,
        last_state=entry.get("state") or "unknown",
        last_state_at=entry.get("state_at"),
        session_id=entry.get("session_id") or "",
        history_path=entry.get("history_path") or "",
        cwd=entry.get("cwd") or "",
        **extra,
    )


def forget_session(name: str, *, reason: str = "corral:kill-session") -> bool:
    """Drop a session Corral stopped from the inventory (so its death is not a loss)."""
    try:
        if not name:
            return False
        with _lock() as held:
            if not held:
                return False
            inventory = read_inventory()
            sessions = dict(inventory.get("sessions") or {})
            existed = sessions.pop(name, None) is not None
            stopped = dict(inventory.get("recently_stopped") or {})
            stopped[name] = time.time()
            inventory["sessions"] = sessions
            inventory["recently_stopped"] = _prune_stopped(stopped)
            inventory["last_session_stop"] = {"name": name, "reason": reason, "at": _now_iso()}
            inventory["updated_at"] = _now_iso()
            _write_inventory(inventory)
            return existed
    except Exception as exc:  # noqa: BLE001 - forget refuses to break a stop path
        _surface("hosted_session_stopped", exc)
        return False


# ------------------------------------------------------------------- environment


def _survival_evidence(last_heartbeat: Any) -> dict[str, Any]:
    """What Corral can still establish about the machine at detection time (§7.2)."""
    return {
        "memory": _memory_facts(),
        "crash_reports": _crash_report_facts(last_heartbeat),
        "server_log_tail": _server_log_tail(),
    }


def _server_log_tail(max_bytes: int = 4096, max_lines: int = 20) -> str:
    """Bounded recent tail of launchd keepalive-server.log, empty if absent."""
    path = cache_dir() / "keepalive-server.log"
    try:
        size = path.stat().st_size
        if size <= 0:
            return ""
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(-max_bytes, os.SEEK_END)
            raw = fh.read(max_bytes)
    except OSError:
        return ""
    lines = [line for line in raw.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return "\n".join(lines[-max_lines:])


def _pending_restore_key(item: Mapping[str, Any]) -> str:
    return str(item.get("session_id") or item.get("name") or "")


def _merge_pending_restores(
    existing: Sequence[Mapping[str, Any]],
    added: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for entry in (*existing, *added):
        if not isinstance(entry, Mapping):
            continue
        item = dict(entry)
        key = _pending_restore_key(item)
        if not key:
            continue
        by_key[key] = item
    return list(by_key.values())


def pending_restores() -> list[dict[str, Any]]:
    """Auto-restore after server loss is disabled by the 2026-11-18 owner ruling."""
    return []


def consume_pending_restores(identifiers: Iterable[str]) -> int:
    """Drop processed or vanished entries from pending_restores under the inventory lock."""
    targets = {str(item) for item in identifiers if item}
    if not targets:
        return 0
    try:
        with _lock() as held:
            if not held:
                return 0
            inventory = read_inventory()
            existing = list(inventory.get("pending_restores") or ())
            if not existing:
                return 0
            kept: list[dict[str, Any]] = []
            removed = 0
            for raw in existing:
                if not isinstance(raw, Mapping):
                    continue
                item = dict(raw)
                name = str(item.get("name") or "")
                sid = str(item.get("session_id") or "")
                if (name and name in targets) or (sid and sid in targets):
                    removed += 1
                    continue
                kept.append(item)
            if removed == 0:
                return 0
            if kept:
                inventory["pending_restores"] = kept
            else:
                inventory.pop("pending_restores", None)
            inventory["updated_at"] = _now_iso()
            _write_inventory(inventory)
            return removed
    except Exception as exc:  # noqa: BLE001 - heal queue cleanup must never raise
        _surface("pending_restores", exc)
        return 0


def _memory_facts() -> dict[str, Any]:
    facts: dict[str, Any] = {"swap_used_mb": None, "swap_total_mb": None,
                             "free_mb": None, "compressed_mb": None}
    if sys.platform != "darwin":
        return facts
    swap = _run(("sysctl", "-n", "vm.swapusage")) or ""
    used = _SWAP_USED_RE.search(swap)
    total = _SWAP_TOTAL_RE.search(swap)
    if used:
        facts["swap_used_mb"] = _float(used.group(1))
    if total:
        facts["swap_total_mb"] = _float(total.group(1))
    stats = _run(("vm_stat",)) or ""
    page_match = _PAGE_SIZE_RE.search(stats)
    page = int(page_match.group(1)) if page_match else 4096
    for line, key in (("Pages free", "free_mb"), ("Pages occupied by compressor", "compressed_mb")):
        value = _vm_stat_pages(stats, line)
        if value is not None:
            facts[key] = round(value * page / (1024 * 1024), 1)
    return facts


def _vm_stat_pages(stats: str, label: str) -> int | None:
    for line in stats.splitlines():
        if line.startswith(label):
            _, _, raw = line.partition(":")
            digits = raw.strip().rstrip(".").replace(",", "")
            return int(digits) if digits.isdigit() else None
    return None


def _crash_report_facts(last_heartbeat: Any) -> dict[str, Any]:
    start = _parse_iso(last_heartbeat) or (time.time() - 3600)
    facts: dict[str, Any] = {"available": _CRASH_REPORT_DIR.is_dir(),
                             "window_start": _iso(start), "count": 0, "recent": []}
    if not facts["available"]:
        return facts
    found: list[tuple[float, str]] = []
    try:
        for entry in _CRASH_REPORT_DIR.iterdir():  # one listing, no `log show` (§7.2)
            if entry.suffix not in {".ips", ".crash", ".diag"}:
                continue
            with contextlib.suppress(OSError):
                mtime = entry.stat().st_mtime
                if mtime >= start:
                    found.append((mtime, entry.name))
    except OSError:
        facts["available"] = False
        return facts
    found.sort(reverse=True)
    facts["count"] = len(found)
    facts["recent"] = [name for _, name in found[:MAX_CRASH_NAMES]]
    return facts


def _server_pid(socket: str, env: Mapping[str, str]) -> int | None:
    """Server pid, or None when it is unknown; a raising probe is never a loss."""
    from corral import tmux_server

    try:
        return tmux_server.server_pid(socket, env)
    except Exception:  # noqa: BLE001 - unknown is not zero
        return None


def _count_sessions(socket: str, env: Mapping[str, str]) -> int | None:
    live = _probe_sessions((socket,), env)
    return None if live is None else len(live)


def _server_started_at(pid: int | None) -> str | None:
    uptime = _uptime_seconds(pid)
    return _iso(time.time() - uptime) if uptime is not None else None


def _uptime_seconds(pid: int | None) -> int | None:
    if not pid:
        return None
    # `etime` is portable (`etimes` is GNU-only); format is [[dd-]hh:]mm[:ss].
    out = _run(("ps", "-o", "etime=", "-p", str(pid)))
    return _parse_etime(out) if out is not None else None


def _parse_etime(text: str) -> int | None:
    raw = text.strip()
    if not raw:
        return None
    days = 0
    if "-" in raw:
        head, _, raw = raw.partition("-")
        if not head.isdigit():
            return None
        days = int(head)
    parts = raw.split(":")
    if not 1 < len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return days * 86400 + seconds


def _tmux_version(env: Mapping[str, str]) -> str:
    out = _run(("tmux", "-V"), env=env)
    return out.strip() if out else ""


def _digest(path: Any) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    except (OSError, TypeError, ValueError):
        return ""


def _run(argv: Sequence[str], env: Mapping[str, str] | None = None) -> str | None:
    try:
        proc = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
            check=False, env=dict(env) if env is not None else None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _env(env: Mapping[str, str] | None) -> dict[str, str]:
    if env is not None:
        return dict(env)
    from corral import keepalive

    return keepalive.tmux_env()


_BOOT_SEC_RE = re.compile(r"sec\s*=\s*(\d+)")
_LINUX_BTIME_RE = re.compile(r"^btime\s+(\d+)", re.MULTILINE)


def boot_time() -> float | None:
    """Host OS boot epoch seconds, or None when unavailable."""
    try:
        if sys.platform == "darwin":
            out = _run(("sysctl", "-n", "kern.boottime")) or ""
            match = _BOOT_SEC_RE.search(out)
            return float(match.group(1)) if match else None
        if sys.platform.startswith("linux"):
            raw = Path("/proc/stat").read_text(encoding="ascii", errors="ignore")
            match = _LINUX_BTIME_RE.search(raw)
            return float(match.group(1)) if match else None
    except Exception:  # noqa: BLE001 - best-effort diagnostic helper
        return None
    return None


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _iso(stamp: float) -> str:
    return dt.datetime.fromtimestamp(float(stamp)).isoformat(timespec="seconds")


def _parse_iso(value: Any) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
