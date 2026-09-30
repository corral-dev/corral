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
    """True when a live worker heartbeat is fresh (best-effort, never raises)."""
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
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(
                {"pid": os.getpid(), "updated_at": time.time()},
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
    if registry is None:
        from corral.runtime import default_registry

        registry = default_registry()
    try:
        # Prefer shared: when the remote daemon just published, this pass is
        # free; otherwise scan locally and publish for the consumers.
        registry.scan_all(
            max(int(limit), PUBLISH_LIMIT_FLOOR),
            keep_ids_by_runtime=_remembered_keep_ids(),
            prefer_shared=True,
        )
        return True
    except Exception:  # noqa: BLE001 — worker passes must never raise
        return False


def run_loop(
    *,
    limit: int,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    parent_pid: int | None = None,
    max_passes: int | None = None,
    registry=None,
) -> int:
    """Worker main loop. Returns a process exit code, never raises."""
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
    passes = 0
    try:
        while True:
            if parent_pid and _parent_dead(parent_pid):
                return 0
            _write_heartbeat()
            run_once(limit=limit, registry=registry)
            passes += 1
            if max_passes is not None and passes >= max_passes:
                return 0
            deadline = time.monotonic() + max(0.1, float(interval))
            while time.monotonic() < deadline:
                if parent_pid and _parent_dead(parent_pid):
                    return 0
                time.sleep(min(0.2, deadline - time.monotonic()))
    except Exception:  # noqa: BLE001 — worker must die quietly, not alarm
        return 1
    finally:
        try:
            lock_handle.close()
        except Exception:  # noqa: BLE001
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
