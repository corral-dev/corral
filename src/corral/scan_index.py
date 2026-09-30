"""Cross-process shared scan index.

One process's completed ``scan_all`` result can be reused by others (TUI windows
and ``corral remote``) so the same history is not re-read from disk every few
seconds. The index is acceleration only: any failure or coverage miss falls
through to a local scan.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from corral.cache import cache_dir, provider_cohort
from corral.cache import enabled as cache_enabled
from corral.legacy_names import getenv

INDEX_VERSION = 1
# Shared snapshots additionally carry the provider cohort (consumer extraction
# semantics + SessKit build). A new cohort never adopts an older contract's
# snapshot: the remote's cold baseline only consumes freshly parsed rows.
# Covering indexes (publisher scanned at least as deep as the consumer) stay
# usable this long. Consumers still force a local scan on their own cadence
# (see SessionStore._FULL_MERGE_INTERVAL) so new sessions cannot stall forever.
DEFAULT_TTL_SECONDS = 12.0
# Same-process publishes closer together than this with identical result keys
# are skipped: the on-disk payload is hundreds of KB and every scan_all would
# otherwise rewrite it (~3 s TUI cadence). Still well inside DEFAULT_TTL_SECONDS
# so consumers keep seeing a fresh index.
PUBLISH_MIN_INTERVAL_SECONDS = 5.0
# Publisher keep ids that its own scan could not cover (stale pins/groups) are
# recorded so consumers can excuse them; capped to bound the payload.
MAX_UNCOVERABLE_KEEP_IDS = 2000
# A killed publisher can leave its tmp file behind; entries older than this are
# removed best-effort on the next publish.
STALE_TMP_MAX_AGE_SECONDS = 300.0

# Last publish by this process: monotonic time, result limit, and per-runtime
# session-key fingerprint used for the throttle above. Module-level because
# scan_all callers are sequential per process for a given registry.
_PUBLISH_LOCK = threading.Lock()
_LAST_PUBLISH: dict[str, object] = {"at": 0.0, "limit": 0, "keys": None}


def _shared_index_enabled() -> bool:
    """Shared index is off when the derived cache is off, or under test isolation.

    ``CORRAL_ISOLATE_MANAGED_HOSTS=1`` (set by ``ci-test.py``) must also disable
    this file: otherwise a real-disk publish from one test is consumed by the
    next mock-based ``scan_all`` and fixtures fill with the developer's sessions.
    """
    if not cache_enabled():
        return False
    isolate = (getenv("ISOLATE_MANAGED_HOSTS", "") or "").strip().lower()
    return isolate not in {"1", "true", "yes", "on"}


def index_path():
    return cache_dir() / "scan-index.json"


def published_meta() -> tuple[float, int] | None:
    """``(published_at, limit)`` of the current index file, or ``None``.

    Lets publishers tell their own keep-alive republishes apart from fresher
    foreign ones without paying for a full consume. Never raises.
    """
    try:
        with open(index_path(), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
        return None
    if payload.get("provider_cohort") != provider_cohort():
        # Older provider contract (or a cohort-less legacy publish): never
        # establish a baseline from it.
        return None
    try:
        return float(payload.get("published_at") or 0), int(payload.get("limit") or 0)
    except (TypeError, ValueError):
        return None


def try_consume(
    limit: int,
    keep_ids_by_runtime: dict[str, set[str]] | None = None,
    *,
    max_age: float = DEFAULT_TTL_SECONDS,
) -> dict[str, list[dict[str, Any]]] | None:
    """Return a deep-copy of a fresh covering index, or ``None`` to scan locally."""
    if not _shared_index_enabled():
        return None
    try:
        path = index_path()
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
        return None
    if payload.get("provider_cohort") != provider_cohort():
        # An obsolete shared worker's snapshot must never be reused by a new
        # provider cohort: fall through to a local scan instead.
        return None
    try:
        published_at = float(payload.get("published_at") or 0)
        published_limit = int(payload.get("limit") or 0)
    except (TypeError, ValueError):
        return None
    if published_limit < limit:
        return None
    age = time.time() - published_at
    if age < 0 or age > max_age:
        return None
    sessions = payload.get("sessions")
    if not isinstance(sessions, dict):
        return None
    keep_ids_by_runtime = keep_ids_by_runtime or {}
    uncoverable_raw = payload.get("uncoverable_keep")
    uncoverable: dict[str, set[str]] = {}
    if isinstance(uncoverable_raw, dict):
        for runtime_id, ids in uncoverable_raw.items():
            if isinstance(ids, list):
                uncoverable[str(runtime_id)] = {
                    str(item) for item in ids if isinstance(item, str)
                }
    for runtime_id, keep_ids in keep_ids_by_runtime.items():
        if not keep_ids:
            continue
        bucket = sessions.get(runtime_id)
        if not isinstance(bucket, list):
            return None
        present = {str(item.get("id") or "") for item in bucket if isinstance(item, dict)}
        # Keep ids the publisher's own scan could not cover (stale pins/groups
        # pointing at long-gone sessions) can never be covered by any scan, so
        # requiring them would fail every consume. Newly pinned ids the
        # publisher never saw are NOT excused and still force a local scan.
        excused = uncoverable.get(runtime_id) or set()
        if not (set(keep_ids) - excused) <= present:
            return None
    narrowed: dict[str, list[dict[str, Any]]] = {}
    for runtime_id, bucket in sessions.items():
        if not isinstance(bucket, list):
            continue
        keep_ids = keep_ids_by_runtime.get(runtime_id) or set()
        narrowed[runtime_id] = _narrow_bucket(bucket, limit, keep_ids)
    return narrowed


def publish(
    scanned: dict[str, list[dict[str, Any]]],
    *,
    limit: int,
    keep_ids_by_runtime: dict[str, set[str]] | None = None,
) -> None:
    """Atomically replace the shared index after a successful local scan."""
    if not _shared_index_enabled():
        return
    try:
        keep_payload = {
            runtime_id: sorted(ids)
            for runtime_id, ids in (keep_ids_by_runtime or {}).items()
            if ids
        }
        present_by_runtime: dict[str, set[str]] = {
            runtime_id: {
                str(item.get("id") or "")
                for item in bucket
                if isinstance(item, dict)
            }
            for runtime_id, bucket in scanned.items()
        }
        # Keep ids this very scan could not cover are stale (deleted/expired
        # sessions still pinned or grouped). Recording them lets consumers with
        # the same sidebar memory excuse them instead of missing forever.
        uncoverable: dict[str, list[str]] = {}
        for runtime_id, ids in (keep_ids_by_runtime or {}).items():
            if not ids:
                continue
            missing = sorted(
                {str(item) for item in ids} - present_by_runtime.get(runtime_id, set())
            )[:MAX_UNCOVERABLE_KEEP_IDS]
            if missing:
                uncoverable[str(runtime_id)] = missing
        fingerprint: dict[str, tuple[str, ...]] = {
            runtime_id: tuple(sorted(present))
            for runtime_id, present in present_by_runtime.items()
        }
        now_mono = time.monotonic()
        path = index_path()
        with _PUBLISH_LOCK:
            last = _LAST_PUBLISH
            if (
                str(last.get("path") or "") == str(path)
                and now_mono - float(last.get("at") or 0.0) < PUBLISH_MIN_INTERVAL_SECONDS
                and last.get("limit") == int(limit)
                and last.get("keys") == fingerprint
            ):
                return
            last["at"] = now_mono
            last["path"] = str(path)
            last["limit"] = int(limit)
            last["keys"] = fingerprint
        payload = {
            "version": INDEX_VERSION,
            "provider_cohort": provider_cohort(),
            "published_at": time.time(),
            "limit": int(limit),
            "keep_ids": keep_payload,
            "uncoverable_keep": uncoverable,
            "sessions": {
                runtime_id: [dict(session) for session in bucket]
                for runtime_id, bucket in scanned.items()
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
        _cleanup_stale_tmps(path)
    except Exception:  # noqa: BLE001 — index is optional acceleration
        return


def _cleanup_stale_tmps(path) -> None:
    """Remove tmp files left by killed publishers; never fail the publish."""
    try:
        now = time.time()
        for candidate in path.parent.glob(f"{path.name}.tmp.*"):
            try:
                if candidate.name == f"{path.name}.tmp.{os.getpid()}":
                    continue
                if now - candidate.stat().st_mtime > STALE_TMP_MAX_AGE_SECONDS:
                    candidate.unlink()
            except OSError:
                continue
    except Exception:  # noqa: BLE001 — best effort only
        pass


def _narrow_bucket(
    sessions: list,
    limit: int,
    keep_ids: set[str],
) -> list[dict[str, Any]]:
    """Re-apply a smaller limit onto a covering publisher result."""
    keep_ids = {str(item) for item in keep_ids}
    ranked = sorted(
        (item for item in sessions if isinstance(item, dict)),
        key=lambda session: float(session.get("file_mtime") or session.get("mtime") or 0),
        reverse=True,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for session in ranked:
        session_id = str(session.get("id") or "")
        if session_id in seen:
            continue
        if len(out) < limit or session_id in keep_ids:
            out.append(dict(session))
            seen.add(session_id)
    for session in ranked:
        session_id = str(session.get("id") or "")
        if session_id in keep_ids and session_id not in seen:
            out.append(dict(session))
            seen.add(session_id)
    return out
