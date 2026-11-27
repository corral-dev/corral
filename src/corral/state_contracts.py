"""Versioned Core state contracts decoupling extensions from Core internals.

Core reads these SQLite contracts read-only (never importing plugins):
- ``hidden-sessions.sqlite3``: child sessions excluded from main session lists.
- ``host-leases.sqlite3``: active plugin-owned hosted sessions protected from reclaim.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from corral.cli_contract import config_home, plugins_dir, state_dir

HIDDEN_SESSIONS_DB = "hidden-sessions.sqlite3"
HOST_LEASES_DB = "host-leases.sqlite3"
BUILTIN_HIDDEN_PREFIXES: tuple[str, ...] = ("[corral-hidden:", "[corral-wf:")
_BUSY_TIMEOUT_MS = 5000

_HIDDEN_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hidden_sessions (
    session_key    TEXT PRIMARY KEY,
    keepalive_name TEXT,
    raw_id         TEXT,
    owner          TEXT NOT NULL,
    group_id       TEXT,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hidden_owner ON hidden_sessions(owner);
CREATE INDEX IF NOT EXISTS idx_hidden_keepalive ON hidden_sessions(keepalive_name);
"""

_LEASES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS host_leases (
    owner          TEXT NOT NULL,
    lease_id       TEXT NOT NULL,
    keepalive_name TEXT NOT NULL,
    pid            INTEGER NOT NULL,
    acquired_at    REAL NOT NULL,
    expires_at     REAL NOT NULL,
    PRIMARY KEY (owner, lease_id)
);
CREATE INDEX IF NOT EXISTS idx_leases_keepalive ON host_leases(keepalive_name);
"""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


@contextmanager
def _connect_rw(db_path: Path, schema_sql: str):
    _ensure_private_dir(db_path.parent)
    conn = sqlite3.connect(str(db_path), timeout=_BUSY_TIMEOUT_MS / 1000.0, isolation_level="IMMEDIATE")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(schema_sql)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        try:
            if db_path.exists():
                os.chmod(db_path, 0o600)
        except OSError:
            pass


@contextmanager
def _connect_ro(db_path: Path):
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=_BUSY_TIMEOUT_MS / 1000.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Generic session visibility contract (hidden-sessions.sqlite3)
# ---------------------------------------------------------------------------


def hidden_sessions_db_path(root: Path | str | None = None) -> Path:
    return state_dir(root) / HIDDEN_SESSIONS_DB


def register_hidden_session(
    *,
    session_key: str,
    owner: str,
    keepalive_name: str | None = None,
    raw_id: str | None = None,
    group_id: str | None = None,
    root: Path | str | None = None,
) -> None:
    """Register one hidden child session so main session lists omit it."""
    key = str(session_key or "").strip()
    if not key:
        raise ValueError("session_key must be non-empty")
    owner_name = str(owner or "").strip()
    if not owner_name:
        raise ValueError("owner must be non-empty")
    db_path = hidden_sessions_db_path(root)
    now = time.time()
    with _connect_rw(db_path, _HIDDEN_SCHEMA_SQL) as conn:
        conn.execute(
            """
            INSERT INTO hidden_sessions (session_key, keepalive_name, raw_id, owner, group_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_key) DO UPDATE SET
                keepalive_name=COALESCE(excluded.keepalive_name, hidden_sessions.keepalive_name),
                raw_id=COALESCE(excluded.raw_id, hidden_sessions.raw_id),
                owner=excluded.owner,
                group_id=COALESCE(excluded.group_id, hidden_sessions.group_id)
            """,
            (key, keepalive_name or None, raw_id or None, owner_name, group_id or None, now),
        )


def unregister_hidden_session(
    *,
    session_key: str | None = None,
    owner: str | None = None,
    group_id: str | None = None,
    root: Path | str | None = None,
) -> int:
    """Remove matching hidden-session registrations; returns deleted row count."""
    db_path = hidden_sessions_db_path(root)
    if not db_path.is_file():
        return 0
    clauses: list[str] = []
    params: list[Any] = []
    if session_key:
        clauses.append("session_key = ?")
        params.append(session_key)
    if owner:
        clauses.append("owner = ?")
        params.append(owner)
    if group_id:
        clauses.append("group_id = ?")
        params.append(group_id)
    if not clauses:
        raise ValueError("Provide at least one filter (session_key, owner, or group_id)")
    with _connect_rw(db_path, _HIDDEN_SCHEMA_SQL) as conn:
        cur = conn.execute(f"DELETE FROM hidden_sessions WHERE {' AND '.join(clauses)}", params)
        return int(cur.rowcount or 0)


def list_hidden_sessions(
    *,
    owner: str | None = None,
    root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Read-only snapshot of registered hidden sessions."""
    db_path = hidden_sessions_db_path(root)
    if not db_path.is_file():
        return []
    try:
        with _connect_ro(db_path) as conn:
            if owner:
                rows = conn.execute(
                    "SELECT session_key, keepalive_name, raw_id, owner, group_id, created_at "
                    "FROM hidden_sessions WHERE owner = ? ORDER BY created_at DESC",
                    (owner,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT session_key, keepalive_name, raw_id, owner, group_id, created_at "
                    "FROM hidden_sessions ORDER BY created_at DESC"
                ).fetchall()
    except sqlite3.Error:
        return []
    return [dict(row) for row in rows]


def _legacy_workflow_db_paths() -> list[Path]:
    paths: list[Path] = []
    wf_override = os.environ.get("CORRAL_WORKFLOW_ROOT", "").strip()
    if wf_override:
        paths.append(Path(wf_override).expanduser() / "workflows.sqlite3")
    default_wf = config_home() / "workflows" / "workflows.sqlite3"
    if default_wf not in paths:
        paths.append(default_wf)
    return paths


def known_hidden_session_keys(root: Path | str | None = None) -> set[str]:
    """Read-only union of registered hidden session keys, keepalive names, and raw IDs."""
    keys: set[str] = set()
    db_path = hidden_sessions_db_path(root)
    if db_path.is_file():
        try:
            with _connect_ro(db_path) as conn:
                rows = conn.execute(
                    "SELECT session_key, keepalive_name, raw_id FROM hidden_sessions"
                ).fetchall()
            for row in rows:
                for col in ("session_key", "keepalive_name", "raw_id"):
                    val = row[col]
                    if isinstance(val, str) and val:
                        keys.add(val)
        except sqlite3.Error:
            pass

    # Backward compatibility: pre-Wave-1 workflow sessions recorded only in workflows.sqlite3.
    if root is None:
        for wf_db in _legacy_workflow_db_paths():
            if not wf_db.is_file():
                continue
            try:
                with _connect_ro(wf_db) as conn:
                    rows = conn.execute("SELECT session_key, keepalive_name FROM workflow_sessions").fetchall()
                for row in rows:
                    for col in ("session_key", "keepalive_name"):
                        val = row[col]
                        if isinstance(val, str) and val:
                            keys.add(val)
            except sqlite3.Error:
                continue
    return keys


def hidden_marker_prefixes(plugins_root: Path | str | None = None) -> tuple[str, ...]:
    """Return active marker prefixes used for cold-scan session hiding."""
    prefixes: list[str] = list(BUILTIN_HIDDEN_PREFIXES)
    seen = set(prefixes)
    index_path = plugins_dir(plugins_root) / "index.json"
    if index_path.is_file():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            raw = data.get("session_hide_prefixes")
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, str) and item and item not in seen:
                        seen.add(item)
                        prefixes.append(item)
    return tuple(prefixes)


def is_hidden_session(
    session: dict[str, Any],
    *,
    known_keys: set[str] | None = None,
    prefixes: Sequence[str] | None = None,
) -> bool:
    """Return True if ``session`` belongs to a plugin/workflow and should stay hidden."""
    if not isinstance(session, dict):
        return False
    if session.get("workflow_id") or session.get("hidden_by"):
        return True
    keys = known_keys if known_keys is not None else known_hidden_session_keys()
    if keys:
        from corral.models import session_key

        if session_key(session) in keys:
            return True
        keepalive_name = session.get("keepalive_name")
        if isinstance(keepalive_name, str) and keepalive_name in keys:
            return True
        raw_id = session.get("id")
        if isinstance(raw_id, str) and raw_id in keys:
            return True
    active_prefixes = prefixes if prefixes is not None else hidden_marker_prefixes()
    if active_prefixes:
        for field in ("first_user_msg", "title", "fallback_title"):
            text = session.get(field)
            if isinstance(text, str) and any(prefix in text for prefix in active_prefixes):
                return True
    return False


# ---------------------------------------------------------------------------
# Generic host leases contract (host-leases.sqlite3)
# ---------------------------------------------------------------------------


def host_leases_db_path(root: Path | str | None = None) -> Path:
    return state_dir(root) / HOST_LEASES_DB


def acquire_host_lease(
    *,
    owner: str,
    lease_id: str,
    keepalive_name: str,
    pid: int,
    ttl_seconds: float = 300.0,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """Acquire or renew a reclaim-protection lease for a hosted tmux session."""
    owner_name = str(owner or "").strip()
    lid = str(lease_id or "").strip()
    host = str(keepalive_name or "").strip()
    if not owner_name or not lid or not host:
        raise ValueError("owner, lease_id, and keepalive_name must be non-empty")
    now = time.time()
    expires_at = now + max(1.0, float(ttl_seconds))
    db_path = host_leases_db_path(root)
    with _connect_rw(db_path, _LEASES_SCHEMA_SQL) as conn:
        conn.execute(
            """
            INSERT INTO host_leases (owner, lease_id, keepalive_name, pid, acquired_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner, lease_id) DO UPDATE SET
                keepalive_name=excluded.keepalive_name,
                pid=excluded.pid,
                expires_at=excluded.expires_at
            """,
            (owner_name, lid, host, int(pid), now, expires_at),
        )
        # Writers prune long-expired rows (> 24h); read-only probes never mutate.
        conn.execute("DELETE FROM host_leases WHERE expires_at < ?", (now - 86400.0,))
    return {
        "owner": owner_name,
        "lease_id": lid,
        "keepalive_name": host,
        "pid": int(pid),
        "acquired_at": now,
        "expires_at": expires_at,
    }


def release_host_lease(
    *,
    owner: str,
    lease_id: str,
    root: Path | str | None = None,
) -> bool:
    """Release an existing host lease; returns True if a row was removed."""
    db_path = host_leases_db_path(root)
    if not db_path.is_file():
        return False
    with _connect_rw(db_path, _LEASES_SCHEMA_SQL) as conn:
        cur = conn.execute(
            "DELETE FROM host_leases WHERE owner = ? AND lease_id = ?",
            (owner, lease_id),
        )
        return bool(cur.rowcount and cur.rowcount > 0)


def list_host_leases(*, now: float | None = None, root: Path | str | None = None) -> list[dict[str, Any]]:
    """Return currently active host leases for diagnostics."""
    db_path = host_leases_db_path(root)
    if not db_path.is_file():
        return []
    ts = time.time() if now is None else float(now)
    with _connect_ro(db_path) as conn:
        rows = conn.execute(
            "SELECT owner, lease_id, keepalive_name, pid, acquired_at, expires_at FROM host_leases"
        ).fetchall()
    active: list[dict[str, Any]] = []
    for row in rows:
        exp = float(row["expires_at"])
        pid = int(row["pid"])
        if exp > ts or (pid > 0 and _pid_alive(pid) and (ts - exp) < 60.0):
            active.append(dict(row))
    return active


def active_leased_hosts(
    *,
    now: float | None = None,
    root: Path | str | None = None,
) -> set[str] | None:
    """Return active leased keepalive names, or None if the lease store is unreadable.

    Fail-closed semantics: a missing database yields an empty set (no active
    leases), whereas an existing corrupt/unreadable database returns ``None`` so
    ``reclaim`` refuses to stop hosted sessions without full context.
    """
    db_path = host_leases_db_path(root)
    if not db_path.exists():
        return set()
    try:
        rows = list_host_leases(now=now, root=root)
    except (sqlite3.Error, OSError, ValueError):
        return None
    return {str(row["keepalive_name"]) for row in rows if row.get("keepalive_name")}
