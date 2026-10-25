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
import re
import shlex
import signal
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
# A worker that loaded the previous provider/code contract keeps stamping its
# memoized cohort and can never be reused: every consumer rejects its snapshot
# and cold-scans locally (5–28 s, CPU pegged) while the stale worker also holds
# the singleton flock, so no current worker can take over. It self-exits on a
# fresh on-disk version check; a consumer reaps it when that code predates the
# check (2026-10-25 freeze).
WORKER_MODULE_MARKER = "corral.scan_worker"
REAP_TERM_TIMEOUT_SECONDS = 2.0

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


def _installed_provider_version() -> str:
    """Distribution version of SessKit installed on disk right now.

    ``cache.sesskit_provider_version()`` is memoized on purpose (a process's
    cached rows must match the parser it actually loaded), so a long-lived
    worker needs this disk read to notice an upgrade that happened after it
    started. Never raises.
    """
    try:
        from importlib import metadata

        distribution = metadata.distribution("sesskit")
        installed = str(distribution.version or "").strip()
        loaded_path = getattr(sys.modules.get("sesskit"), "__file__", None)
        if loaded_path and os.path.realpath(loaded_path) != os.path.realpath(
            distribution.locate_file("sesskit/__init__.py")
        ):
            # Editable/PYTHONPATH imports may not belong to this distribution.
            # Their metadata can stay at the build-time version indefinitely.
            # Compare the actual source's version, without reimporting it.
            with open(loaded_path, encoding="utf-8") as handle:
                match = re.search(
                    r"^__version__\s*=\s*[\"']([^\"']+)[\"']",
                    handle.read(), re.MULTILINE,
                )
            return match.group(1) if match else ""
        return installed
    except Exception:  # noqa: BLE001 — drift detection is best-effort
        return ""


def provider_drift() -> bool:
    """True when the install on disk no longer matches this process's parser.

    A parser-version bump in Corral does not have to ship a new SessKit, so the
    package version is checked as well. Never raises.
    """
    try:
        from corral.cache import sesskit_provider_version

        loaded = str(sesskit_provider_version() or "").strip()
        installed = _installed_provider_version()
        if loaded and loaded != "unknown" and installed and installed != loaded:
            return True
    except Exception:  # noqa: BLE001 — fall through to the package check
        pass
    try:
        from corral import updater

        return bool(updater.install_replaced())
    except Exception:  # noqa: BLE001 — unknown install state is not drift
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def _singleton_free() -> bool:
    """True when no process currently holds the worker singleton lock.

    A terminated child stays visible to ``kill(pid, 0)`` while it is a zombie;
    the released flock is the authoritative "worker is gone" signal.
    """
    try:
        _heartbeat_path, lock_path = _paths()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a", encoding="utf-8") as handle:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (ImportError, OSError):
                return False
        return True
    except Exception:  # noqa: BLE001 — unknown lock state keeps the wait
        return False


def _read_heartbeat() -> dict | None:
    try:
        heartbeat_path, _lock_path = _paths()
        with open(heartbeat_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001 — absence of a worker is normal
        return None


def _process_command(pid: int) -> str:
    """Command line for ``pid`` via POSIX ``ps``; empty when unavailable."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, read-only query
            ["ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _is_scan_worker_process(pid: int) -> bool:
    """Guard against killing a recycled pid that is not our helper."""
    try:
        argv = shlex.split(_process_command(pid))
    except ValueError:
        return False
    return (
        len(argv) >= 3
        and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", os.path.basename(argv[0]), re.I)
        is not None
        and argv[1:3] == ["-m", WORKER_MODULE_MARKER]
    )


def obsolete_worker_pid() -> int | None:
    """PID of a registered worker whose heartbeat cohort is obsolete.

    Only a live process that still runs ``corral.scan_worker`` qualifies.
    Heartbeats without a cohort (pre-cohort installs) qualify too: no current
    consumer can reuse them, and they hold the same singleton slot.
    """
    if not _worker_enabled() or provider_drift():
        # A previous-generation consumer cannot decide which newer worker is
        # obsolete. In particular it must never evict the current install.
        return None
    payload = _read_heartbeat()
    if payload is None:
        return None
    try:
        pid = int(payload.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 0 or pid == os.getpid() or not _pid_alive(pid):
        return None
    try:
        from corral.cache import provider_cohort

        current = provider_cohort()
    except Exception:  # noqa: BLE001 — cannot judge; leave the process alone
        return None
    if payload.get("cohort") == current:
        return None
    if not _is_scan_worker_process(pid):
        return None
    # ps can race another consumer's takeover. Do not signal from a heartbeat
    # that was replaced while we were checking the process.
    if _read_heartbeat() != payload:
        return None
    return pid


def reap_obsolete_worker(*, timeout: float = REAP_TERM_TIMEOUT_SECONDS) -> int | None:
    """Terminate a verified obsolete scan worker; returns its pid or ``None``.

    Safety: the pid must come from our heartbeat, be alive, carry a mismatched
    (or missing) cohort, and its command line must run ``corral.scan_worker``.
    Anything else — recycled pid, unrelated process, unreadable state — is left
    untouched. Never raises.
    """
    pid = obsolete_worker_pid()
    if pid is None:
        return None
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return None
    try:
        from corral import observe

        observe.event("scan_worker_reaped", worker_pid=pid)
    except Exception:  # noqa: BLE001 — diagnostics are best-effort
        pass
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        if not _pid_alive(pid) or _singleton_free():
            break
        time.sleep(0.05)
    return pid


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


def _republish(scanned, *, limit: int, keep_ids, expected_published_at=None):
    """Refresh completed buckets, optionally comparing the observed publication."""
    try:
        from corral import scan_index

        return scan_index.publish(
            scanned,
            limit=max(int(limit), PUBLISH_LIMIT_FLOOR),
            keep_ids_by_runtime=keep_ids,
            expected_published_at=expected_published_at,
        )
    except Exception:  # noqa: BLE001 — index is optional acceleration
        return None


class _ScanKeepAlive:
    """Keep completed snapshots and heartbeats fresh during a blocking parse.

    Only this thread mutates its private snapshot/reference time. The main
    loop supplies completed, non-mutated buckets and joins before touching
    its own state again. Index CAS also coordinates the registry's concurrent
    end-of-scan publish and other processes' completed scans.
    """

    def __init__(self, buckets, *, limit, keep_ids, published_wall, parent_pid):
        self.buckets = buckets
        self.limit = limit
        self.keep_ids = {key: set(ids) for key, ids in keep_ids.items()}
        self.published_wall = published_wall
        self.parent_pid = parent_pid
        self.last_heartbeat = time.monotonic()
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="corral-scan-keepalive",
        )

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop.set()
        self.thread.join()

    def _tick(self) -> None:
        now = time.monotonic()
        if now - self.last_heartbeat >= HEARTBEAT_MIN_INTERVAL_SECONDS:
            if (self.parent_pid and _parent_dead(self.parent_pid)) or provider_drift():
                self.stop.set()
                return
            _write_heartbeat()
            self.last_heartbeat = now
        meta = _published_meta()
        if meta is None:
            return  # a first parse must complete before we can publish
        if meta[0] > self.published_wall:
            adopted = _try_adopt(limit=self.limit, keep_ids=self.keep_ids)
            if adopted is None:
                return  # never overwrite a newer but non-covering snapshot
            self.buckets = adopted
            # Use the originally observed time: a raced newer publication
            # is then re-adopted or rejected by CAS, never overwritten.
            self.published_wall = meta[0]
        if self.buckets is not None and time.time() - meta[0] >= REPUBLISH_GAP_SECONDS:
            published = _republish(
                self.buckets, limit=self.limit, keep_ids=self.keep_ids,
                expected_published_at=meta[0],
            )
            if published is not None:
                self.published_wall = published

    def _run(self) -> None:
        gap = min(1.0, HEARTBEAT_MIN_INTERVAL_SECONDS, REPUBLISH_GAP_SECONDS / 2)
        while not self.stop.wait(gap):
            self._tick()


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
    last_buckets_keep_ids = {}
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
                if provider_drift():
                    # The install moved on while this process kept the old
                    # parser loaded. Its rows/snapshots can never satisfy the
                    # new cohort, so exit before holding the slot any longer.
                    try:
                        from corral import observe

                        observe.event(
                            "scan_worker_provider_drift",
                            installed=str(_installed_provider_version() or ""),
                            worker_pid=os.getpid(),
                        )
                    except Exception:  # noqa: BLE001 — exit regardless
                        pass
                    return 0
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
                if meta is not None and meta[0] > our_publish_wall:
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
                    last_buckets_keep_ids = keep_ids
                    our_publish_wall = meta[0] if meta is not None else now_wall
                else:
                    # Hold the consumers on the current buckets while the
                    # (possibly multi-second) parse runs, so a slow pass
                    # under load does not age the index past the consume TTL
                    # and trigger routine local-scan fallbacks.
                    if last_buckets is not None:
                        stale = _published_meta()
                        if stale is not None and now_wall - stale[0] >= REPUBLISH_GAP_SECONDS:
                            published = _republish(
                                last_buckets, limit=limit, keep_ids=last_buckets_keep_ids,
                                expected_published_at=stale[0],
                            )
                            if published is not None:
                                our_publish_wall = published
                    with _ScanKeepAlive(
                        last_buckets, limit=limit, keep_ids=last_buckets_keep_ids,
                        published_wall=our_publish_wall, parent_pid=parent_pid,
                    ):
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
                        last_buckets_keep_ids = keep_ids
                        completed_meta = _published_meta()
                        our_publish_wall = completed_meta[0] if completed_meta else time.time()
                        # Backoff starts on completion, not before a slow
                        # parse (which otherwise exhausts its own next gap).
                        last_action_mono = time.monotonic()
                        next_parse_at = last_action_mono + _parse_gap_seconds(
                            streak, floor, pressured
                        )
            else:
                # Skip the parse; keep the index fresh for consumers without
                # overwriting a fresher foreign publish.
                meta = _published_meta()
                if meta is None:
                    next_parse_at = now_mono  # index vanished: parse next pass
                elif meta[0] > our_publish_wall:
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
                        last_buckets_keep_ids = keep_ids
                    our_publish_wall = meta[0]
                elif now_wall - meta[0] >= REPUBLISH_GAP_SECONDS:
                    published = _republish(
                        last_buckets, limit=limit, keep_ids=last_buckets_keep_ids,
                        expected_published_at=meta[0],
                    )
                    if published is not None:
                        our_publish_wall = published
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


def _reap_child_later(proc) -> None:
    """Collect a spawned worker so a long-lived parent cannot keep a zombie.

    The caller ignores the returned Popen; nobody would call ``wait`` until the
    parent exits. A terminated obsolete worker otherwise stays visible to
    ``kill(pid, 0)`` and clutters process state for the rest of the session.
    """
    wait = getattr(proc, "wait", None)
    if not callable(wait):
        return

    def _wait() -> None:
        try:
            wait()
        except Exception:  # noqa: BLE001 — collection is best-effort
            pass

    try:
        threading.Thread(
            target=_wait, daemon=True, name="corral-scan-worker-wait",
        ).start()
    except Exception:  # noqa: BLE001 — no thread means the OS still reaps at exit
        pass


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
        # An obsolete worker (previous provider/code cohort) still holds the
        # singleton flock and can never be reused; it would otherwise keep the
        # consumer on multi-second local scans forever. Verified before killing
        # (see reap_obsolete_worker): the counter resets only after it is gone
        # or after the 30 s retry window.
        if provider_drift():
            return None
        reap_obsolete_worker()
        # Another consumer may already have replaced the obsolete worker, or
        # the incumbent may still be scanning without a fresh heartbeat. Do
        # not fork a guaranteed singleton loser (nor parse in another worker).
        if is_active() or not _singleton_free():
            return None
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
        _reap_child_later(proc)
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
