"""Dedicated history-scanning subprocess (perf-C).

The TUI process shares one Python GIL between Textual rendering and history
parsing, so every full ``scan_all`` steals UI time. This module moves parsing
to a background subprocess: it loops ``scan_all`` + shared-index ``publish``
while consumers (TUI, phone remote daemon) only ``try_consume`` (~ms) and fall
back to an in-process scan when the index is missing/stale/unusable.

Lifecycle: per-user singleton via an flock'd lock file plus a heartbeat file.
A second TUI window reuses the live worker instead of spawning another. The
worker demotes itself to background priority on start and exits when its
parent pid changes (orphan backstop); consumers rate-limit respawns, so a dead
worker silently degrades to pre-C behavior instead of a frozen list.

Only stdlib imports at top level: ``python -m corral.scan_worker`` must start
fast and never pull Textual. Any failure degrades to "no worker", never to a
broken UI.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

# Deep enough for every consumer: the TUI scans ~50 per runtime, the phone
# remote daemon ~200. A shallower publish can never serve a deeper consumer
# (``published_limit < limit`` is a miss), so one worker covers both.
PUBLISH_LIMIT_FLOOR = 200
# Seconds between passes. Keeps index age (scan time + interval) inside the
# 12 s consume TTL under normal load; back-to-back scanning under write
# thrash is inherent (someone must parse) and now happens off the TUI's GIL.
DEFAULT_INTERVAL_SECONDS = 2.0
# Consumer verdict: heartbeat older than this means no live worker; the store
# then resumes its periodic forced local scan. Must exceed a slow pass
# (scan seconds + interval) with margin, so a busy worker is not misread.
HEARTBEAT_MAX_AGE_SECONDS = 20.0
# Worker writes its heartbeat at most this often; well inside the max age
# above, so a backed-off worker still reads as live.
HEARTBEAT_MIN_INTERVAL_SECONDS = 5.0
# Churn backoff (perf-H): while consecutive full parses show the same
# per-runtime session ids (message/mtime churn, no arrivals), the gap between
# full parses ramps from the poll floor up to this cap. A new/removed session
# id, changed keep ids, or a long FS-quiet stretch resets to the floor, so
# idle-to-arrival latency is unchanged. A watcher-reported new session
# history file (``HistoryWatcher.arrival_seq``) also resets, so arrivals during
# sustained churn are parsed on the next pass instead of waiting out this cap.
CHURN_BACKOFF_MAX_SECONDS = 24.0
# Same cap under memory pressure: cold rescans stop compounding swap thrash.
CHURN_BACKOFF_PRESSURED_MAX_SECONDS = 60.0
# Between full parses the worker keep-alive republishes its last buckets this
# often. Above the 5 s identical-keys publish throttle, inside the 12 s
# consume TTL, so consumers keep shared-hitting and never routinely fall back
# to local scans. DEFAULT_TTL_SECONDS is unchanged.
REPUBLISH_GAP_SECONDS = 8.0
# No history FS event for this long means the churn stopped: come back to the
# poll floor even mid-backoff (the next pass shared-hits cheaply when idle).
QUIET_RESET_SECONDS = 30.0
# Fastest a single process may (re)spawn a worker; prevents fork storms when
# several windows notice a stale heartbeat at once.
RESPAWN_MIN_INTERVAL_SECONDS = 30.0
HEARTBEAT_FILENAME = "scan-worker.json"
LOCK_FILENAME = "scan-worker.lock"

_ENSURE_LOCK = threading.Lock()
_LAST_SPAWN_MONO = 0.0


def _worker_enabled() -> bool:
    """False under test isolation or when the derived cache is off."""
    from corral.cache import enabled as cache_enabled
    from corral.legacy_names import getenv

    if not cache_enabled():
        return False
    isolate = (getenv("ISOLATE_MANAGED_HOSTS", "") or "").strip().lower()
    return isolate not in {"1", "true", "yes", "on"}


def _paths() -> tuple[object, object]:
    from corral.cache import cache_dir

    directory = cache_dir()
    return directory / HEARTBEAT_FILENAME, directory / LOCK_FILENAME


def is_active(*, max_age: float = HEARTBEAT_MAX_AGE_SECONDS) -> bool:
    """True when a live worker heartbeat is fresh (best-effort, never raises).

    The heartbeat's provider cohort must match this process's cohort: an
    obsolete shared worker (older SessKit contract) is never treated as live
    by a new provider cohort, so its snapshots can never become anyone's
    baseline. Consumers fall back to local scans until a current worker owns
    the singleton slot.
    """
    if not _worker_enabled():
        return False
    try:
        heartbeat_path, _lock_path = _paths()
        with open(heartbeat_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            return False
        updated_at = float(payload.get("updated_at") or 0)
        pid = int(payload.get("pid") or 0)
        if pid <= 0 or time.time() - updated_at > max_age:
            return False
        os.kill(pid, 0)
        try:
            from corral.cache import provider_cohort
        except Exception:  # noqa: BLE001 — cohort module broken; scans are too
            return False
        cohort = payload.get("cohort")
        if not isinstance(cohort, str) or cohort != provider_cohort():
            return False
        return True
    except Exception:  # noqa: BLE001 — absence of a worker is normal
        return False


def _write_heartbeat() -> None:
    from corral.cache import cache_dir

    try:
        directory = cache_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / HEARTBEAT_FILENAME
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        try:
            from corral.cache import provider_cohort

            cohort: object = provider_cohort()
        except Exception:  # noqa: BLE001 — cohort-less heartbeat reads as inactive
            cohort = None
        heartbeat_payload: dict[str, object] = {"pid": os.getpid(), "updated_at": time.time()}
        if isinstance(cohort, str) and cohort:
            heartbeat_payload["cohort"] = cohort
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                heartbeat_payload,
                handle,
            )
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — heartbeat is best-effort
        pass


def _acquire_singleton_lock():
    """Non-blocking exclusive lock; None when another worker holds it."""
    try:
        _heartbeat_path, lock_path = _paths()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "w")  # noqa: PTH123 — kept open while held
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, OSError):
            handle.close()
            return None
        return handle
    except Exception:  # noqa: BLE001 — never break the caller
        return None


def _parent_dead(expected_parent_pid: int) -> bool:
    try:
        return os.getppid() != int(expected_parent_pid)
    except Exception:  # noqa: BLE001 — e.g. no getppid on some platforms
        return False


def _remembered_keep_ids() -> dict[str, set[str]]:
    try:
        from corral.split_layout import remembered_ids_by_runtime

        return remembered_ids_by_runtime()
    except Exception:  # noqa: BLE001 — sidebar memory is optional input
        return {}


def run_once(*, limit: int, registry=None) -> bool:
    """One scan+publish pass. Returns False only when disabled."""
    if not _worker_enabled():
        return False
    scanned, _shared = _scan_shared(limit=limit, registry=registry)
    return scanned is not None


def _scan(*, limit: int, registry=None, keep_ids=None, prefer_shared: bool):
    """Scan and publish; returns (buckets, shared) or (None, False) on error.

    ``keep_ids`` overrides the sidebar lookup (tests); otherwise the worker
    reads the same layout DB as the TUI so ``uncoverable_keep`` semantics
    match the consumers. Parse passes force ``prefer_shared=False``: our own
    keep-alive republishes keep the index fresh, so shared-consuming there
    would re-adopt our own stale buckets forever.
    """
    if registry is None:
        from corral.runtime import default_registry

        registry = default_registry()
    try:
        # Prefer shared: when the remote daemon just published, this pass is
        # free; otherwise scan locally and publish for the consumers.
        result = registry.scan_all(
            max(int(limit), PUBLISH_LIMIT_FLOOR),
            keep_ids_by_runtime=(
                keep_ids if keep_ids is not None else _remembered_keep_ids()
            ),
            prefer_shared=prefer_shared,
        )
        return result, bool(registry.last_scan_shared)
    except Exception:  # noqa: BLE001 — worker passes must never raise
        return None, False


def _scan_shared(*, limit: int, registry=None, keep_ids=None):
    return _scan(limit=limit, registry=registry, keep_ids=keep_ids, prefer_shared=True)


def _scan_local(*, limit: int, registry=None, keep_ids=None):
    return _scan(limit=limit, registry=registry, keep_ids=keep_ids, prefer_shared=False)


def _bucket_fingerprint(scanned) -> dict[str, tuple[str, ...]] | None:
    """Per-runtime sorted session ids; arrivals/departures change it, pure
    message/mtime churn does not. Small enough to compare every pass."""
    try:
        return {
            str(runtime_id): tuple(
                sorted(
                    str(item.get("id") or "")
                    for item in bucket
                    if isinstance(item, dict)
                )
            )
            for runtime_id, bucket in scanned.items()
        }
    except Exception:  # noqa: BLE001 — fingerprint is advisory only
        return None


def _freeze_keep_ids(keep_ids) -> dict[str, tuple[str, ...]]:
    """Hashable snapshot of the keep-ids input for change detection."""
    try:
        return {
            str(runtime_id): tuple(sorted({str(item) for item in ids}))
            for runtime_id, ids in (keep_ids or {}).items()
            if ids
        }
    except Exception:  # noqa: BLE001 — on error force the safe path (parse)
        return {}


def _parse_gap_seconds(streak: int, floor: float, pressured: bool) -> float:
    """Gap before the next full parse after ``streak`` consecutive unchanged
    parses. Doubles from the poll floor to the churn cap (higher when memory
    pressured); a changed fingerprint resets the streak to zero."""
    cap = (
        CHURN_BACKOFF_PRESSURED_MAX_SECONDS
        if pressured
        else CHURN_BACKOFF_MAX_SECONDS
    )
    try:
        gap = max(0.1, float(floor)) * (2 ** max(0, int(streak)))
    except (TypeError, ValueError, OverflowError):
        return cap
    return min(gap, cap)


def _memory_pressured() -> bool:
    """Best-effort pressure probe for the backoff cap (never raises)."""
    try:
        from corral.history_watch import memory_pressured

        return bool(memory_pressured())
    except Exception:  # noqa: BLE001 — probe failure means normal cadence
        return False


def _new_history_watcher():
    """FS-event source for idle detection; inert null object on any failure."""
    try:
        from corral.history_watch import HistoryWatcher

        watcher = HistoryWatcher()
        watcher.start()
        return watcher
    except Exception:  # noqa: BLE001 — timed cadence alone is correct
        return None


def _published_meta():
    """(published_at, limit) of the shared index, or None when unreadable."""
    try:
        from corral import scan_index

        return scan_index.published_meta()
    except Exception:  # noqa: BLE001 — missing index just means parse
        return None


def _try_adopt(*, limit: int, keep_ids):
    """Adopt a fresher foreign publish (usually the remote daemon's); None on
    any miss so the caller keeps its own buckets."""
    try:
        from corral import scan_index

        return scan_index.try_consume(
            max(int(limit), PUBLISH_LIMIT_FLOOR), keep_ids
        )
    except Exception:  # noqa: BLE001 — adoption is optional acceleration
        return None


def _republish(scanned, *, limit: int, keep_ids) -> None:
    """Refresh ``published_at`` on unchanged buckets (never raises)."""
    try:
        from corral import scan_index

        scan_index.publish(
            scanned,
            limit=max(int(limit), PUBLISH_LIMIT_FLOOR),
            keep_ids_by_runtime=keep_ids,
        )
    except Exception:  # noqa: BLE001 — index is optional acceleration
        pass


def run_loop(
    *,
    limit: int,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    parent_pid: int | None = None,
    max_passes: int | None = None,
    registry=None,
    watcher=None,
) -> int:
    """Worker main loop. Returns a process exit code, never raises.

    Passes run at most every ``interval`` (the poll floor). Full parses back
    off while consecutive parses show unchanged session ids (perf-H churn
    backoff); between parses the worker keep-alive republishes its last
    buckets so the shared index stays fresh for consumers. ``watcher`` is a
    ``HistoryWatcher`` (FS-event idle detection); tests inject a stub, and
    passing None builds (and stops) a real one.
    """
    if not _worker_enabled():
        return 2
    try:
        from corral.schedprio import demote_background

        demote_background()
    except Exception:  # noqa: BLE001 — priority is best-effort
        pass
    if parent_pid is None:
        try:
            parent_pid = os.getppid()
        except Exception:  # noqa: BLE001
            parent_pid = 0
    lock_handle = _acquire_singleton_lock()
    if lock_handle is None:
        return 0  # another worker owns the singleton slot; not an error
    floor = max(0.1, float(interval))
    if registry is None:
        # One registry for the whole loop: its per-runtime signature cache
        # then skips unchanged runtimes on every pass, so a churn-round
        # re-parse only pays the runtimes that actually moved. A fresh
        # registry per pass would re-parse everything every time.
        from corral.runtime import default_registry

        registry = default_registry()
    own_watcher = watcher is None
    if own_watcher:
        watcher = _new_history_watcher()
    passes = 0
    streak = 0
    fingerprint = None
    last_buckets = None
    last_keeps_frozen = None
    next_parse_at = 0.0  # first pass always parses
    our_publish_wall = 0.0
    last_event_mono = time.monotonic()  # quiet measured from worker start
    last_action_mono = 0.0
    last_heartbeat_mono = 0.0
    pressured = False
    arrival_seq = int(getattr(watcher, "arrival_seq", 0) or 0) if watcher is not None else 0
    try:
        while True:
            if parent_pid and _parent_dead(parent_pid):
                return 0
            now_mono = time.monotonic()
            if watcher is not None:
                try:
                    if watcher.wait(0):
                        last_event_mono = now_mono
                    watcher.clear()
                    seq = int(getattr(watcher, "arrival_seq", 0) or 0)
                    if seq != arrival_seq:
                        # A new session history file: parse on the next pass
                        # instead of waiting out churn backoff (up to 60 s
                        # under memory pressure) — consumers need the arrival.
                        arrival_seq = seq
                        streak = 0
                        next_parse_at = min(next_parse_at, now_mono)
                except Exception:  # noqa: BLE001 — watcher is advisory
                    pass
            if now_mono - last_action_mono < floor:
                time.sleep(min(0.2, floor - (now_mono - last_action_mono)))
                continue
            if parent_pid and _parent_dead(parent_pid):
                return 0
            last_action_mono = now_mono = time.monotonic()
            if now_mono - last_heartbeat_mono >= HEARTBEAT_MIN_INTERVAL_SECONDS:
                _write_heartbeat()
                last_heartbeat_mono = now_mono
            now_wall = time.time()
            keep_ids = _remembered_keep_ids()
            frozen = _freeze_keep_ids(keep_ids)
            if last_keeps_frozen is not None and frozen != last_keeps_frozen:
                streak = 0  # new pins/groups may need coverage: parse now
                next_parse_at = now_mono
            last_keeps_frozen = frozen
            if (
                last_buckets is not None
                and next_parse_at > now_mono + floor
                and last_event_mono is not None
                and now_mono - last_event_mono > QUIET_RESET_SECONDS
            ):
                next_parse_at = now_mono + floor  # churn stopped: back to floor
                streak = 0
            if last_buckets is None or now_mono >= next_parse_at:
                # A parse pass. Our own keep-alive republishes keep the index
                # fresh, so shared-consuming here would re-adopt our own stale
                # buckets forever; only a strictly newer foreign publish (the
                # remote daemon did the parse work for us) is adopted.
                meta = _published_meta()
                adopted = None
                if meta is not None and meta[0] > our_publish_wall + 1.0:
                    adopted = _try_adopt(limit=limit, keep_ids=keep_ids)
                if adopted is not None:
                    adopted_fp = _bucket_fingerprint(adopted)
                    if (
                        fingerprint is not None
                        and adopted_fp is not None
                        and adopted_fp != fingerprint
                    ):
                        streak = 0  # arrival via a foreign publish
                        next_parse_at = now_mono + floor
                    elif adopted_fp == fingerprint:
                        streak += 1  # daemon covered this churn round
                        next_parse_at = now_mono + _parse_gap_seconds(
                            streak, floor, pressured
                        )
                    fingerprint = adopted_fp
                    last_buckets = adopted
                    our_publish_wall = meta[0] if meta is not None else now_wall
                else:
                    # Hold the consumers on the current buckets while the
                    # (possibly multi-second) parse runs, so a slow pass
                    # under load does not age the index past the consume TTL
                    # and trigger routine local-scan fallbacks.
                    if last_buckets is not None:
                        stale = _published_meta()
                        if stale is not None and now_wall - stale[0] >= REPUBLISH_GAP_SECONDS:
                            _republish(last_buckets, limit=limit, keep_ids=keep_ids)
                            our_publish_wall = time.time()
                    scanned, _shared = _scan_local(
                        limit=limit, registry=registry, keep_ids=keep_ids
                    )
                    if scanned is None:
                        next_parse_at = now_mono + floor
                    else:
                        pressured = _memory_pressured()
                        parsed = _bucket_fingerprint(scanned)
                        quiet = now_mono - last_event_mono > QUIET_RESET_SECONDS
                        if quiet:
                            streak = 0  # idle parses are signature-hit cheap
                        elif (
                            fingerprint is not None
                            and parsed is not None
                            and parsed == fingerprint
                        ):
                            streak += 1
                        else:
                            streak = 0
                        fingerprint = parsed
                        last_buckets = scanned
                        our_publish_wall = now_wall
                        next_parse_at = now_mono + _parse_gap_seconds(
                            streak, floor, pressured
                        )
            else:
                # Skip the parse; keep the index fresh for consumers without
                # overwriting a fresher foreign publish.
                meta = _published_meta()
                if meta is None:
                    next_parse_at = now_mono  # index vanished: parse next pass
                elif meta[0] > our_publish_wall + 1.0:
                    adopted = _try_adopt(limit=limit, keep_ids=keep_ids)
                    if adopted is not None:
                        adopted_fp = _bucket_fingerprint(adopted)
                        if (
                            fingerprint is not None
                            and adopted_fp is not None
                            and adopted_fp != fingerprint
                        ):
                            streak = 0
                            next_parse_at = now_mono + floor
                        fingerprint = adopted_fp
                        last_buckets = adopted
                    our_publish_wall = meta[0]
                elif now_wall - meta[0] >= REPUBLISH_GAP_SECONDS:
                    _republish(last_buckets, limit=limit, keep_ids=keep_ids)
                    our_publish_wall = time.time()
            passes += 1
            if max_passes is not None and passes >= max_passes:
                return 0
    except Exception:  # noqa: BLE001 — worker must die quietly, not alarm
        return 1
    finally:
        try:
            lock_handle.close()
        except Exception:  # noqa: BLE001
            pass
        if own_watcher and watcher is not None:
            try:
                watcher.stop()
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                pass


def worker_limit(tui_limit: int) -> int:
    """Publish depth covering both the TUI and the remote daemon (200)."""
    try:
        return max(int(tui_limit), PUBLISH_LIMIT_FLOOR)
    except (TypeError, ValueError):
        return PUBLISH_LIMIT_FLOOR


def ensure_scan_worker(tui_limit: int):
    """Start the singleton worker if none is live. Returns Popen/None.

    Cheap when a worker is active (one heartbeat stat + pid check); spawns at
    most once per RESPAWN_MIN_INTERVAL_SECONDS per process. Never raises.
    """
    global _LAST_SPAWN_MONO
    if not _worker_enabled():
        return None
    try:
        with _ENSURE_LOCK:
            if is_active():
                return None
            now_mono = time.monotonic()
            if now_mono - _LAST_SPAWN_MONO < RESPAWN_MIN_INTERVAL_SECONDS:
                return None
            _LAST_SPAWN_MONO = now_mono
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, same-install python
            [
                sys.executable,
                "-m",
                "corral.scan_worker",
                "--limit",
                str(worker_limit(tui_limit)),
                "--parent-pid",
                str(os.getpid()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return proc
    except Exception:  # noqa: BLE001 — no worker is a valid state
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corral history scan worker")
    parser.add_argument("--limit", type=int, default=PUBLISH_LIMIT_FLOOR)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--parent-pid", type=int, default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.once:
        return 0 if run_once(limit=args.limit) else 2
    return run_loop(limit=args.limit, interval=args.interval, parent_pid=args.parent_pid)


if __name__ == "__main__":
    raise SystemExit(main())
