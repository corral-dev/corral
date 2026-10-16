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
# Most recent consume miss (reason + publisher facts, counts only — never
# session ids). Misses can occur every few seconds per process, so the events
# log only gets a periodic reminder per reason; the in-memory record always
# reflects the latest miss and is what unit tests assert on.
MISS_EVENT_MIN_INTERVAL_SECONDS = 300.0
_MISS_LOCK = threading.Lock()
_LAST_MISS: dict[str, object] = {"at": 0.0, "reason": "none", "detail": {}}
_LAST_MISS_EVENT: dict[str, object] = {"at": 0.0, "reason": ""}


def last_miss() -> dict[str, object]:
    """Most recent consume miss: ``reason`` + ``at`` + publisher-fact ``detail``."""
    with _MISS_LOCK:
        snapshot = dict(_LAST_MISS)
    snapshot["detail"] = dict(snapshot.get("detail") or {})
    return snapshot


def _note_miss(reason: str, **detail: object) -> None:
    """Record why ``try_consume`` fell through to a local scan.

    Always updates the in-memory record; emits a throttled
    ``scan_index_miss`` event (at most one per reason per
    ``MISS_EVENT_MIN_INTERVAL_SECONDS``) so a field ``events.log`` shows
    whether misses are TTL, limit, keep-id, cohort or absence — instead of
    a bare ``shared_index=false`` on every line. Details carry counts and
    versions only, never session ids or titles.
    """
    now_mono = time.monotonic()
    with _MISS_LOCK:
        _LAST_MISS.update({"at": time.time(), "reason": reason, "detail": dict(detail)})
        last_at = float(_LAST_MISS_EVENT.get("at") or 0.0)
        last_reason = str(_LAST_MISS_EVENT.get("reason") or "")
        if reason == last_reason and now_mono - last_at < MISS_EVENT_MIN_INTERVAL_SECONDS:
            return
        _LAST_MISS_EVENT.update({"at": now_mono, "reason": reason})
    isolate = (getenv("ISOLATE_MANAGED_HOSTS", "") or "").strip().lower()
    if isolate in {"1", "true", "yes", "on"}:
        return
    try:
        from corral import observe

        observe.event("scan_index_miss", reason=reason, **detail)
    except Exception:  # noqa: BLE001 — diagnostics must never break scans
        pass


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
        _note_miss("disabled")
        return None
    try:
        path = index_path()
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        _note_miss("unreadable")
        return None
    if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
        _note_miss("version")
        return None
    if payload.get("provider_cohort") != provider_cohort():
        # An obsolete shared worker's snapshot must never be reused by a new
        # provider cohort: fall through to a local scan instead.
        _note_miss("cohort")
        return None
    try:
        published_at = float(payload.get("published_at") or 0)
        published_limit = int(payload.get("limit") or 0)
    except (TypeError, ValueError):
        _note_miss("version")
        return None
    if published_limit < limit:
        _note_miss("limit", published_limit=published_limit, need_limit=limit)
        return None
    age = time.time() - published_at
    if age < 0 or age > max_age:
        _note_miss(
            "ttl", age_s=round(age, 1), max_age_s=max_age,
            published_limit=published_limit,
        )
        return None
    sessions = payload.get("sessions")
    if not isinstance(sessions, dict):
        _note_miss("sessions")
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
            _note_miss("keep_ids", runtime=str(runtime_id), missing_count=len(keep_ids))
            return None
        present = {str(item.get("id") or "") for item in bucket if isinstance(item, dict)}
        # Keep ids the publisher's own scan could not cover (stale pins/groups
        # pointing at long-gone sessions) can never be covered by any scan, so
        # requiring them would fail every consume. Newly pinned ids the
        # publisher never saw are NOT excused and still force a local scan.
        excused = uncoverable.get(runtime_id) or set()
        missing = (set(keep_ids) - excused) - present
        if missing:
            _note_miss(
                "keep_ids", runtime=str(runtime_id), missing_count=len(missing),
                published_count=len(present),
            )
            return None
    narrowed: dict[str, list[dict[str, Any]]] = {}
    for runtime_id, bucket in sessions.items():
        if not isinstance(bucket, list):
            continue
        keep_ids = keep_ids_by_runtime.get(runtime_id) or set()
        narrowed[runtime_id] = _narrow_bucket(bucket, limit, keep_ids)
    return narrowed


def _covered_by_fresh_index(limit: int) -> bool:
    """True when a fresh STRICTLY WIDER index already serves our consumers.

    A narrower publish would only clobber it: slower-cadence deep consumers
    (remote daemon, the worker itself) then see ``published_limit < need`` on
    every pass — measured 2026-10-16 (``published_limit=50, need_limit=200``
    on every daemon/worker line while the TUI republished 50 every ~13s).
    Same-depth publishes always proceed (fresh data must land; the
    identical-keys throttle already skips true duplicates), so this only
    ever refuses to NARROW a fresh index. Skipping is safe for freshness:
    the covering index is at most TTL old, arrivals are picked up by the
    next parse of any process, and once it goes stale narrow publishers
    resume — a stuck worker degrades to status quo within one TTL instead
    of silencing everyone. Cohort/version/unreadable indexes never cover
    (``published_meta`` returns None for those), so mixed-version peers
    keep publishing for their own cohort. Never raises.
    """
    try:
        meta = published_meta()
    except Exception:  # noqa: BLE001 — no usable cover means publish
        return False
    if meta is None:
        return False
    try:
        published_at, published_limit = meta
        age = time.time() - float(published_at)
    except (TypeError, ValueError):
        return False
    return (
        int(published_limit) > int(limit)
        and 0 <= age <= DEFAULT_TTL_SECONDS
    )


def publish(
    scanned: dict[str, list[dict[str, Any]]],
    *,
    limit: int,
    keep_ids_by_runtime: dict[str, set[str]] | None = None,
) -> None:
    """Atomically replace the shared index after a successful local scan."""
    if not _shared_index_enabled():
        return
    if _covered_by_fresh_index(limit):
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
