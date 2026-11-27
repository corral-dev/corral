"""SQLite WAL state store for Corral workflows, capability metadata and circuit health.

Follows DBOS SQLite durability conventions: ``isolation_level="IMMEDIATE"``,
WAL journal mode, generous ``busy_timeout=30000`` for 30-way concurrency, and
``BEGIN IMMEDIATE`` migrations. Read-only operations never create storage on
disk, and every write appends an actor-attributed row to ``audit_log`` in the
same transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from corral_workflow.errors import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    WorkflowError,
)

ENV_WORKFLOW_ROOT = "CORRAL_WORKFLOW_ROOT"
STORE_FILENAME = "workflows.sqlite3"
SCHEMA_VERSION = 2

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS inventory_targets (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    runtime TEXT,
    model TEXT,
    family TEXT,
    available INTEGER NOT NULL,
    supports_model_override INTEGER NOT NULL,
    auth_hint TEXT NOT NULL,
    cli_version TEXT,
    source_path TEXT,
    evidence_json TEXT NOT NULL,
    scanned_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_targets (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    kind TEXT NOT NULL,
    runtime TEXT,
    model TEXT,
    cost_tier TEXT,
    speed_tier TEXT,
    strengths_json TEXT,
    avoid_for_json TEXT,
    max_concurrency INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS user_profiles (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    candidates_json TEXT NOT NULL,
    degradation_policy TEXT NOT NULL DEFAULT 'pause_on_exhaust',
    version TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS registry_state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS target_health (
    target_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    failure_class TEXT,
    reason TEXT,
    opened_at REAL,
    cooldown_until REAL,
    cooldown_level INTEGER NOT NULL DEFAULT 0,
    half_open_owner TEXT,
    version TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    wf_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    script_path TEXT,
    script_sha256 TEXT,
    script_source TEXT,
    cwd TEXT,
    args_json TEXT NOT NULL,
    status TEXT NOT NULL,
    active_phase TEXT,
    max_concurrency INTEGER NOT NULL,
    owner_pid INTEGER,
    cancel_requested_at REAL,
    result_json TEXT,
    error_json TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_nodes (
    wf_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    step_key TEXT,
    input_hash TEXT,
    parent_id TEXT,
    node_type TEXT NOT NULL,
    kind TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_json TEXT,
    resolved_json TEXT,
    observed_json TEXT,
    schema_json TEXT,
    output_json TEXT,
    output_committed INTEGER NOT NULL DEFAULT 0,
    session_ref TEXT,
    keepalive_name TEXT,
    worktree_path TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    progress_note TEXT,
    error_json TEXT,
    started_at REAL,
    finished_at REAL,
    duration_ms INTEGER,
    updated_at REAL NOT NULL,
    PRIMARY KEY (wf_id, node_id),
    FOREIGN KEY (wf_id) REFERENCES workflow_runs(wf_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workflow_nodes_step_key ON workflow_nodes(wf_id, step_key);
CREATE INDEX IF NOT EXISTS idx_workflow_nodes_session ON workflow_nodes(session_ref);

CREATE TABLE IF NOT EXISTS workflow_edges (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    wf_id TEXT NOT NULL,
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    edge_kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (wf_id) REFERENCES workflow_runs(wf_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workflow_edges_wf ON workflow_edges(wf_id, seq);

CREATE TABLE IF NOT EXISTS workflow_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    wf_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    node_id TEXT,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (wf_id) REFERENCES workflow_runs(wf_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_workflow_events_wf ON workflow_events(wf_id, seq);

CREATE TABLE IF NOT EXISTS workflow_sessions (
    session_key TEXT PRIMARY KEY,
    wf_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    phase TEXT,
    keepalive_name TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_sessions_keepalive ON workflow_sessions(keepalive_name);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    before_version TEXT,
    after_version TEXT,
    detail_json TEXT,
    timestamp REAL NOT NULL,
    timestamp_utc TEXT NOT NULL
);
"""


def workflow_root(override: Path | str | None = None) -> Path:
    """Resolve the workflow state root directory without creating it."""
    if override is not None:
        return Path(override).expanduser()
    env = os.environ.get(ENV_WORKFLOW_ROOT, "").strip()
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".config")
    return base / "corral" / "workflows"


def utc_iso(ts: float | None = None) -> str:
    value = time.time() if ts is None else ts
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def compute_version(payload: dict[str, Any], *, exclude: tuple[str, ...] = ()) -> str:
    """Deterministic 12-char SHA-256 optimistic-lock fingerprint over canonical JSON."""
    excluded = set(exclude)
    filtered = {k: v for k, v in payload.items() if k not in excluded}
    canonical = json.dumps(filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_loads(raw: str | None, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _row_get(row: sqlite3.Row, col: str, default: Any = None) -> Any:
    return row[col] if col in row.keys() else default


def _pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class WorkflowStore:
    """Transactional SQLite store for workflow state, catalog metadata and health."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = workflow_root(root)
        self.path = self.root / STORE_FILENAME

    def exists(self) -> bool:
        return self.path.is_file()

    def _open_connection(self, *, write: bool) -> sqlite3.Connection:
        if write:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.is_symlink():
                raise WorkflowError(
                    "unsafe_storage",
                    f"Workflow store must not be a symlink: {self.path}",
                    EXIT_ERROR,
                )
            conn = sqlite3.connect(self.path, timeout=30.0, isolation_level="IMMEDIATE")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        else:
            if not self.path.is_file():
                raise WorkflowError(
                    "store_missing",
                    f"Workflow store does not exist yet: {self.path}",
                    EXIT_NOT_FOUND,
                )
            conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        if write:
            conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema(conn)
        else:
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            if ver > SCHEMA_VERSION:
                conn.close()
                raise WorkflowError(
                    "store_too_new",
                    f"Workflow store schema v{ver} is newer than supported v{SCHEMA_VERSION}.",
                    EXIT_ERROR,
                    hint="Upgrade corral before reading this workflow database.",
                )
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
        if ver > SCHEMA_VERSION:
            raise WorkflowError(
                "store_too_new",
                f"Workflow store schema v{ver} is newer than supported v{SCHEMA_VERSION}.",
                EXIT_ERROR,
                hint="Upgrade corral before writing to this workflow database.",
            )
        if ver == 0:
            conn.executescript(_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return
        if ver < SCHEMA_VERSION:
            run_cols = {row[1] for row in conn.execute("PRAGMA table_info(workflow_runs)").fetchall()}
            for col_name, col_type in (
                ("script_source", "TEXT"),
                ("cwd", "TEXT"),
                ("owner_pid", "INTEGER"),
                ("cancel_requested_at", "REAL"),
            ):
                if col_name not in run_cols:
                    conn.execute(f"ALTER TABLE workflow_runs ADD COLUMN {col_name} {col_type}")
            node_cols = {row[1] for row in conn.execute("PRAGMA table_info(workflow_nodes)").fetchall()}
            if "input_hash" not in node_cols:
                conn.execute("ALTER TABLE workflow_nodes ADD COLUMN input_hash TEXT")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self._open_connection(write=write)
        try:
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _audit(
        self,
        conn: sqlite3.Connection,
        *,
        actor: str,
        action: str,
        target_id: str,
        before_version: str | None = None,
        after_version: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        now = time.time()
        conn.execute(
            """
            INSERT INTO audit_log
                (actor, action, target_id, before_version, after_version, detail_json, timestamp, timestamp_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor,
                action,
                target_id,
                before_version,
                after_version,
                _json_dumps(detail) if detail is not None else None,
                now,
                utc_iso(now),
            ),
        )

    # -- Local inventory targets ------------------------------------------------

    def list_inventory_targets(self) -> list[dict[str, Any]]:
        if not self.exists():
            return []
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM inventory_targets ORDER BY id ASC").fetchall()
        return [self._row_to_inventory(r) for r in rows]

    def replace_inventory_targets(self, items: list[dict[str, Any]], *, actor: str = "system") -> list[dict[str, Any]]:
        now = time.time()
        with self.connection(write=True) as conn:
            conn.execute("DELETE FROM inventory_targets")
            for item in items:
                conn.execute(
                    """
                    INSERT INTO inventory_targets
                        (id, kind, runtime, model, family, available, supports_model_override,
                         auth_hint, cli_version, source_path, evidence_json, scanned_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        item["kind"],
                        item.get("runtime"),
                        item.get("model"),
                        item.get("family"),
                        1 if item.get("available", True) else 0,
                        1 if item.get("supports_model_override", True) else 0,
                        item.get("auth_hint") or "unknown",
                        item.get("cli_version"),
                        item.get("source_path"),
                        _json_dumps(item.get("evidence") or {}),
                        float(item.get("scanned_at") or now),
                    ),
                )
            self._audit(conn, actor=actor, action="inventory.scan", target_id="inventory", detail={"count": len(items)})
        return self.list_inventory_targets()

    def latest_inventory_scan_time(self) -> float | None:
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT MAX(scanned_at) AS ts FROM inventory_targets").fetchone()
        return float(row["ts"]) if row and row["ts"] is not None else None

    @staticmethod
    def _row_to_inventory(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "origin": "scanned",
            "kind": row["kind"],
            "runtime": row["runtime"],
            "model": row["model"],
            "family": row["family"],
            "available": bool(row["available"]),
            "supports_model_override": bool(row["supports_model_override"]),
            "auth_hint": row["auth_hint"],
            "cli_version": row["cli_version"],
            "source_path": row["source_path"],
            "evidence": _json_loads(row["evidence_json"], {}),
            "scanned_at": row["scanned_at"],
        }

    # -- User target overrides & custom targets ---------------------------------

    def list_user_targets(self) -> list[dict[str, Any]]:
        if not self.exists():
            return []
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM user_targets ORDER BY id ASC").fetchall()
        return [self._row_to_user_target(r) for r in rows]

    def get_user_target(self, target_id: str) -> dict[str, Any] | None:
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM user_targets WHERE id = ?", (target_id,)).fetchone()
        return self._row_to_user_target(row) if row else None

    def upsert_user_target(
        self,
        record: dict[str, Any],
        *,
        expected_version: str | None,
        actor: str,
        require_version_if_exists: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        target_id = record["id"]
        mutable = {
            "id": target_id,
            "origin": record["origin"],
            "kind": record["kind"],
            "runtime": record.get("runtime"),
            "model": record.get("model"),
            "cost_tier": record.get("cost_tier"),
            "speed_tier": record.get("speed_tier"),
            "strengths": list(record.get("strengths") or []),
            "avoid_for": list(record.get("avoid_for") or []),
            "max_concurrency": record.get("max_concurrency"),
            "enabled": bool(record.get("enabled", True)),
            "notes": str(record.get("notes") or ""),
        }
        new_version = compute_version(mutable)
        now = time.time()
        with self.connection(write=True) as conn:
            existing = conn.execute("SELECT * FROM user_targets WHERE id = ?", (target_id,)).fetchone()
            before_version = existing["version"] if existing else None
            if existing is not None and require_version_if_exists:
                if not expected_version:
                    raise WorkflowError(
                        "confirmation_required",
                        f"Target '{target_id}' already exists; pass --version {before_version} to update it.",
                        EXIT_USAGE,
                        hint="Read the target first and pass its current version token.",
                        context={"target_id": target_id, "current_version": before_version},
                    )
                if expected_version != before_version:
                    raise WorkflowError(
                        "conflict",
                        (
                            f"Version mismatch for target '{target_id}': "
                            f"expected {expected_version}, current is {before_version}."
                        ),
                        EXIT_CONFLICT,
                        hint="Re-read the target before retrying.",
                        context={
                            "target_id": target_id,
                            "expected_version": expected_version,
                            "current_version": before_version,
                        },
                    )
            status = "updated" if existing is not None else "created"
            conn.execute(
                """
                INSERT INTO user_targets
                    (id, origin, kind, runtime, model, cost_tier, speed_tier,
                     strengths_json, avoid_for_json, max_concurrency, enabled, notes, version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    origin=excluded.origin,
                    kind=excluded.kind,
                    runtime=excluded.runtime,
                    model=excluded.model,
                    cost_tier=excluded.cost_tier,
                    speed_tier=excluded.speed_tier,
                    strengths_json=excluded.strengths_json,
                    avoid_for_json=excluded.avoid_for_json,
                    max_concurrency=excluded.max_concurrency,
                    enabled=excluded.enabled,
                    notes=excluded.notes,
                    version=excluded.version,
                    updated_at=excluded.updated_at
                """,
                (
                    mutable["id"],
                    mutable["origin"],
                    mutable["kind"],
                    mutable["runtime"],
                    mutable["model"],
                    mutable["cost_tier"],
                    mutable["speed_tier"],
                    _json_dumps(mutable["strengths"]),
                    _json_dumps(mutable["avoid_for"]),
                    mutable["max_concurrency"],
                    1 if mutable["enabled"] else 0,
                    mutable["notes"],
                    new_version,
                    now,
                ),
            )
            self._audit(
                conn,
                actor=actor,
                action=f"target.{status}",
                target_id=target_id,
                before_version=before_version,
                after_version=new_version,
            )
            saved = conn.execute("SELECT * FROM user_targets WHERE id = ?", (target_id,)).fetchone()
        return status, self._row_to_user_target(saved)

    def delete_user_target(
        self,
        target_id: str,
        *,
        expected_version: str | None,
        force: bool = False,
        actor: str,
    ) -> dict[str, Any]:
        if not self.exists():
            raise WorkflowError("not_found", f"User target '{target_id}' not found.", EXIT_NOT_FOUND)
        with self.connection(write=True) as conn:
            existing = conn.execute("SELECT * FROM user_targets WHERE id = ?", (target_id,)).fetchone()
            if existing is None:
                raise WorkflowError("not_found", f"User target '{target_id}' not found.", EXIT_NOT_FOUND)
            before_version = existing["version"]
            if not expected_version:
                raise WorkflowError(
                    "confirmation_required",
                    f"Deleting target '{target_id}' requires --version {before_version}.",
                    EXIT_USAGE,
                    context={"target_id": target_id, "current_version": before_version},
                )
            if expected_version != before_version:
                raise WorkflowError(
                    "conflict",
                    (
                        f"Version mismatch for target '{target_id}': "
                        f"expected {expected_version}, current is {before_version}."
                    ),
                    EXIT_CONFLICT,
                    context={
                        "target_id": target_id,
                        "expected_version": expected_version,
                        "current_version": before_version,
                    },
                )
            if not force:
                referencing = []
                for prow in conn.execute("SELECT id, candidates_json FROM user_profiles").fetchall():
                    cands = _json_loads(prow["candidates_json"], [])
                    if target_id in cands:
                        referencing.append(prow["id"])
                if referencing:
                    raise WorkflowError(
                        "conflict",
                        f"Target '{target_id}' is referenced by profiles: {', '.join(sorted(referencing))}.",
                        EXIT_CONFLICT,
                        hint="Update those profiles first or pass --force.",
                        context={"target_id": target_id, "referencing_profiles": sorted(referencing)},
                    )
            removed = self._row_to_user_target(existing)
            conn.execute("DELETE FROM user_targets WHERE id = ?", (target_id,))
            self._audit(
                conn,
                actor=actor,
                action="target.deleted",
                target_id=target_id,
                before_version=before_version,
                after_version=None,
            )
        return removed

    @staticmethod
    def _row_to_user_target(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "origin": row["origin"],
            "kind": row["kind"],
            "runtime": row["runtime"],
            "model": row["model"],
            "cost_tier": row["cost_tier"],
            "speed_tier": row["speed_tier"],
            "strengths": _json_loads(row["strengths_json"], []),
            "avoid_for": _json_loads(row["avoid_for_json"], []),
            "max_concurrency": row["max_concurrency"],
            "enabled": bool(row["enabled"]),
            "notes": row["notes"],
            "version": row["version"],
            "updated_at": row["updated_at"],
        }

    # -- User profiles ----------------------------------------------------------

    def list_user_profiles(self) -> list[dict[str, Any]]:
        if not self.exists():
            return []
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM user_profiles ORDER BY id ASC").fetchall()
        return [self._row_to_user_profile(r) for r in rows]

    def get_user_profile(self, profile_id: str) -> dict[str, Any] | None:
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM user_profiles WHERE id = ?", (profile_id,)).fetchone()
        return self._row_to_user_profile(row) if row else None

    def upsert_user_profile(
        self,
        record: dict[str, Any],
        *,
        expected_version: str | None,
        actor: str,
        require_version_if_exists: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        profile_id = record["id"]
        mutable = {
            "id": profile_id,
            "origin": record["origin"],
            "description": str(record.get("description") or ""),
            "candidates": list(record.get("candidates") or []),
            "degradation_policy": str(record.get("degradation_policy") or "pause_on_exhaust"),
        }
        new_version = compute_version(mutable)
        now = time.time()
        with self.connection(write=True) as conn:
            existing = conn.execute("SELECT * FROM user_profiles WHERE id = ?", (profile_id,)).fetchone()
            before_version = existing["version"] if existing else None
            if existing is not None and require_version_if_exists:
                if not expected_version:
                    raise WorkflowError(
                        "confirmation_required",
                        f"Profile '{profile_id}' already exists; pass --version {before_version} to update it.",
                        EXIT_USAGE,
                        context={"profile_id": profile_id, "current_version": before_version},
                    )
                if expected_version != before_version:
                    raise WorkflowError(
                        "conflict",
                        (
                            f"Version mismatch for profile '{profile_id}': "
                            f"expected {expected_version}, current is {before_version}."
                        ),
                        EXIT_CONFLICT,
                        context={
                            "profile_id": profile_id,
                            "expected_version": expected_version,
                            "current_version": before_version,
                        },
                    )
            status = "updated" if existing is not None else "created"
            conn.execute(
                """
                INSERT INTO user_profiles
                    (id, origin, description, candidates_json, degradation_policy, version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    origin=excluded.origin,
                    description=excluded.description,
                    candidates_json=excluded.candidates_json,
                    degradation_policy=excluded.degradation_policy,
                    version=excluded.version,
                    updated_at=excluded.updated_at
                """,
                (
                    mutable["id"],
                    mutable["origin"],
                    mutable["description"],
                    _json_dumps(mutable["candidates"]),
                    mutable["degradation_policy"],
                    new_version,
                    now,
                ),
            )
            self._audit(
                conn,
                actor=actor,
                action=f"profile.{status}",
                target_id=profile_id,
                before_version=before_version,
                after_version=new_version,
            )
            saved = conn.execute("SELECT * FROM user_profiles WHERE id = ?", (profile_id,)).fetchone()
        return status, self._row_to_user_profile(saved)

    def delete_user_profile(
        self,
        profile_id: str,
        *,
        expected_version: str | None,
        actor: str,
        action: str = "profile.deleted",
    ) -> dict[str, Any]:
        if not self.exists():
            raise WorkflowError("not_found", f"User profile '{profile_id}' not found.", EXIT_NOT_FOUND)
        with self.connection(write=True) as conn:
            existing = conn.execute("SELECT * FROM user_profiles WHERE id = ?", (profile_id,)).fetchone()
            if existing is None:
                raise WorkflowError("not_found", f"User profile '{profile_id}' not found.", EXIT_NOT_FOUND)
            before_version = existing["version"]
            if not expected_version:
                raise WorkflowError(
                    "confirmation_required",
                    f"Modifying profile '{profile_id}' requires --version {before_version}.",
                    EXIT_USAGE,
                    context={"profile_id": profile_id, "current_version": before_version},
                )
            if expected_version != before_version:
                raise WorkflowError(
                    "conflict",
                    (
                        f"Version mismatch for profile '{profile_id}': "
                        f"expected {expected_version}, current is {before_version}."
                    ),
                    EXIT_CONFLICT,
                    context={
                        "profile_id": profile_id,
                        "expected_version": expected_version,
                        "current_version": before_version,
                    },
                )
            removed = self._row_to_user_profile(existing)
            conn.execute("DELETE FROM user_profiles WHERE id = ?", (profile_id,))
            self._audit(
                conn,
                actor=actor,
                action=action,
                target_id=profile_id,
                before_version=before_version,
                after_version=None,
            )
        return removed

    @staticmethod
    def _row_to_user_profile(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "origin": row["origin"],
            "description": row["description"],
            "candidates": _json_loads(row["candidates_json"], []),
            "degradation_policy": row["degradation_policy"],
            "version": row["version"],
            "updated_at": row["updated_at"],
        }

    # -- Registry state ---------------------------------------------------------

    def get_registry_state(self, key: str) -> Any:
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT value_json FROM registry_state WHERE key = ?", (key,)).fetchone()
        return _json_loads(row["value_json"], None) if row else None

    def set_registry_state(self, key: str, value: Any, *, actor: str = "system") -> None:
        now = time.time()
        with self.connection(write=True) as conn:
            conn.execute(
                """
                INSERT INTO registry_state (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at
                """,
                (key, _json_dumps(value), now),
            )
            self._audit(conn, actor=actor, action=f"registry.{key}", target_id=key)

    # -- Target health & shared circuit breaker ---------------------------------

    def list_target_health(self) -> list[dict[str, Any]]:
        if not self.exists():
            return []
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM target_health ORDER BY target_id ASC").fetchall()
        return [self._row_to_health(r) for r in rows]

    def get_target_health(self, target_id: str) -> dict[str, Any]:
        if self.exists():
            with self.connection() as conn:
                row = conn.execute("SELECT * FROM target_health WHERE target_id = ?", (target_id,)).fetchone()
            if row is not None:
                return self._row_to_health(row)
        default_payload = {
            "target_id": target_id,
            "state": "closed",
            "failure_count": 0,
            "consecutive_failures": 0,
            "failure_class": None,
            "reason": None,
            "opened_at": None,
            "cooldown_until": None,
            "cooldown_remaining_s": 0.0,
            "cooldown_level": 0,
            "half_open_owner": None,
        }
        return {
            **default_payload,
            "version": compute_version({"target_id": target_id, "state": "closed", "cooldown_level": 0}),
            "updated_at": None,
        }

    def record_target_failure(
        self,
        target_id: str,
        *,
        failure_class: str,
        reason: str,
        open_immediately: bool,
        cooldown_seconds: float,
        failure_threshold: int = 3,
        now: float | None = None,
    ) -> dict[str, Any]:
        ts = time.time() if now is None else now
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM target_health WHERE target_id = ?", (target_id,)).fetchone()
            fail_count = (row["failure_count"] if row else 0) + 1
            consec = (row["consecutive_failures"] if row else 0) + 1
            prev_level = row["cooldown_level"] if row else 0
            was_half_open = row is not None and row["state"] == "half_open"

            should_open = open_immediately or was_half_open or consec >= failure_threshold
            if should_open:
                level = (
                    min(prev_level + 1, 4)
                    if (was_half_open or (row and row["state"] == "open"))
                    else max(1, prev_level)
                )
                multiplier = 2 ** max(0, level - 1)
                eff_cooldown = min(cooldown_seconds * multiplier, 3600.0)
                state = "open"
                opened_at = ts
                cooldown_until = ts + eff_cooldown
            else:
                level = prev_level
                state = row["state"] if row else "closed"
                opened_at = row["opened_at"] if row else None
                cooldown_until = row["cooldown_until"] if row else None

            version = compute_version({"target_id": target_id, "state": state, "cooldown_level": level})
            conn.execute(
                """
                INSERT INTO target_health
                    (target_id, state, failure_count, consecutive_failures, failure_class, reason,
                     opened_at, cooldown_until, cooldown_level, half_open_owner, version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    state=excluded.state,
                    failure_count=excluded.failure_count,
                    consecutive_failures=excluded.consecutive_failures,
                    failure_class=excluded.failure_class,
                    reason=excluded.reason,
                    opened_at=excluded.opened_at,
                    cooldown_until=excluded.cooldown_until,
                    cooldown_level=excluded.cooldown_level,
                    half_open_owner=NULL,
                    version=excluded.version,
                    updated_at=excluded.updated_at
                """,
                (
                    target_id,
                    state,
                    fail_count,
                    consec,
                    failure_class,
                    reason[:500],
                    opened_at,
                    cooldown_until,
                    level,
                    version,
                    ts,
                ),
            )
            saved = conn.execute("SELECT * FROM target_health WHERE target_id = ?", (target_id,)).fetchone()
        return self._row_to_health(saved)

    def record_target_success(self, target_id: str, *, now: float | None = None) -> dict[str, Any]:
        ts = time.time() if now is None else now
        version = compute_version({"target_id": target_id, "state": "closed", "cooldown_level": 0})
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT failure_count FROM target_health WHERE target_id = ?", (target_id,)).fetchone()
            fail_count = row["failure_count"] if row else 0
            conn.execute(
                """
                INSERT INTO target_health
                    (target_id, state, failure_count, consecutive_failures, failure_class, reason,
                     opened_at, cooldown_until, cooldown_level, half_open_owner, version, updated_at)
                VALUES (?, 'closed', ?, 0, NULL, NULL, NULL, NULL, 0, NULL, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    state='closed',
                    consecutive_failures=0,
                    failure_class=NULL,
                    reason=NULL,
                    opened_at=NULL,
                    cooldown_until=NULL,
                    cooldown_level=0,
                    half_open_owner=NULL,
                    version=excluded.version,
                    updated_at=excluded.updated_at
                """,
                (target_id, fail_count, version, ts),
            )
            saved = conn.execute("SELECT * FROM target_health WHERE target_id = ?", (target_id,)).fetchone()
        return self._row_to_health(saved)

    def try_claim_half_open_probe(self, target_id: str, *, owner: str, now: float | None = None) -> bool:
        """Atomically transition an expired ``open`` circuit to single-flight ``half_open``."""
        ts = time.time() if now is None else now
        if not self.exists():
            return True
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM target_health WHERE target_id = ?", (target_id,)).fetchone()
            if row is None or row["state"] == "closed":
                return True
            if row["state"] == "open":
                cooldown_until = row["cooldown_until"] or 0.0
                if ts < cooldown_until:
                    return False
                version = compute_version(
                    {"target_id": target_id, "state": "half_open", "cooldown_level": row["cooldown_level"]}
                )
                conn.execute(
                    "UPDATE target_health SET state='half_open', half_open_owner=?, "
                    "version=?, updated_at=? WHERE target_id=?",
                    (owner, version, ts, target_id),
                )
                return True
            if row["state"] == "half_open":
                return row["half_open_owner"] == owner
        return False

    def release_half_open_probe(self, target_id: str, *, owner: str, now: float | None = None) -> None:
        """Release a claimed ``half_open`` probe back to expired ``open`` if cancelled before result."""
        if not self.exists():
            return
        ts = time.time() if now is None else now
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM target_health WHERE target_id = ?", (target_id,)).fetchone()
            if row is None or row["state"] != "half_open" or row["half_open_owner"] != owner:
                return
            version = compute_version(
                {"target_id": target_id, "state": "open", "cooldown_level": row["cooldown_level"]}
            )
            conn.execute(
                "UPDATE target_health SET state='open', half_open_owner=NULL, cooldown_until=?, "
                "version=?, updated_at=? WHERE target_id=?",
                (ts, version, ts, target_id),
            )

    def reset_target_health(
        self,
        target_id: str,
        *,
        expected_version: str | None,
        actor: str,
    ) -> dict[str, Any]:
        current = self.get_target_health(target_id)
        if not expected_version:
            raise WorkflowError(
                "confirmation_required",
                f"Resetting health for '{target_id}' requires --version {current['version']}.",
                EXIT_USAGE,
                context={"target_id": target_id, "current_version": current["version"]},
            )
        if expected_version != current["version"]:
            raise WorkflowError(
                "conflict",
                (
                    f"Version mismatch for health '{target_id}': "
                    f"expected {expected_version}, current is {current['version']}."
                ),
                EXIT_CONFLICT,
                context={
                    "target_id": target_id,
                    "expected_version": expected_version,
                    "current_version": current["version"],
                },
            )
        now = time.time()
        new_version = compute_version({"target_id": target_id, "state": "closed", "cooldown_level": 0})
        with self.connection(write=True) as conn:
            conn.execute(
                """
                INSERT INTO target_health
                    (target_id, state, failure_count, consecutive_failures, failure_class, reason,
                     opened_at, cooldown_until, cooldown_level, half_open_owner, version, updated_at)
                VALUES (?, 'closed', 0, 0, NULL, NULL, NULL, NULL, 0, NULL, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    state='closed',
                    consecutive_failures=0,
                    failure_class=NULL,
                    reason=NULL,
                    opened_at=NULL,
                    cooldown_until=NULL,
                    cooldown_level=0,
                    half_open_owner=NULL,
                    version=excluded.version,
                    updated_at=excluded.updated_at
                """,
                (target_id, new_version, now),
            )
            self._audit(
                conn,
                actor=actor,
                action="health.reset",
                target_id=target_id,
                before_version=current["version"],
                after_version=new_version,
            )
            saved = conn.execute("SELECT * FROM target_health WHERE target_id = ?", (target_id,)).fetchone()
        return self._row_to_health(saved)

    @staticmethod
    def _row_to_health(row: sqlite3.Row) -> dict[str, Any]:
        now = time.time()
        cooldown_until = row["cooldown_until"]
        remaining = max(0.0, round(cooldown_until - now, 1)) if cooldown_until is not None else 0.0
        return {
            "target_id": row["target_id"],
            "state": row["state"],
            "failure_count": row["failure_count"],
            "consecutive_failures": row["consecutive_failures"],
            "failure_class": row["failure_class"],
            "reason": row["reason"],
            "opened_at": row["opened_at"],
            "cooldown_until": cooldown_until,
            "cooldown_remaining_s": remaining,
            "cooldown_level": row["cooldown_level"],
            "half_open_owner": row["half_open_owner"],
            "version": row["version"],
            "updated_at": row["updated_at"],
        }

    # -- Workflow runs, nodes, edges, events & session hiding index -------------

    def create_workflow_run(
        self,
        *,
        wf_id: str,
        name: str,
        script_path: str | None,
        script_sha256: str | None,
        script_source: str | None = None,
        cwd: str | None = None,
        owner_pid: int | None = None,
        args: dict[str, Any],
        max_concurrency: int,
        actor: str,
    ) -> dict[str, Any]:
        now = time.time()
        with self.connection(write=True) as conn:
            conn.execute(
                """
                INSERT INTO workflow_runs
                    (wf_id, name, script_path, script_sha256, script_source, cwd, args_json, status, active_phase,
                     max_concurrency, owner_pid, cancel_requested_at, result_json, error_json,
                     created_at, started_at, finished_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'running', NULL, ?, ?, NULL, NULL, NULL, ?, ?, NULL, ?)
                """,
                (
                    wf_id,
                    name,
                    script_path,
                    script_sha256,
                    script_source,
                    cwd,
                    _json_dumps(args),
                    max_concurrency,
                    owner_pid,
                    now,
                    now,
                    now,
                ),
            )
            self._append_event(
                conn, wf_id, "workflow.started", None, {"name": name, "max_concurrency": max_concurrency, "cwd": cwd}
            )
            self._audit(conn, actor=actor, action="workflow.start", target_id=wf_id, detail={"name": name, "cwd": cwd})
            row = conn.execute("SELECT * FROM workflow_runs WHERE wf_id = ?", (wf_id,)).fetchone()
        return self._row_to_run(row)

    def set_workflow_owner(
        self,
        wf_id: str,
        owner_pid: int | None,
        *,
        clear_cancel: bool = False,
        script_source: str | None = None,
        script_sha256: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM workflow_runs WHERE wf_id = ?", (wf_id,)).fetchone()
            if row is None:
                raise WorkflowError("not_found", f"Workflow '{wf_id}' not found.", EXIT_NOT_FOUND)
            if clear_cancel:
                conn.execute(
                    """
                    UPDATE workflow_runs
                    SET status='running', owner_pid=?, cancel_requested_at=NULL, error_json=NULL,
                        finished_at=NULL,
                        script_source=COALESCE(?, script_source),
                        script_sha256=COALESCE(?, script_sha256),
                        updated_at=?
                    WHERE wf_id=?
                    """,
                    (owner_pid, script_source, script_sha256, now, wf_id),
                )
                self._append_event(conn, wf_id, "workflow.status", None, {"status": "running", "resumed": True})
                if actor is not None:
                    self._audit(conn, actor=actor, action="workflow.resume", target_id=wf_id)
            else:
                conn.execute(
                    "UPDATE workflow_runs SET owner_pid=?, updated_at=? WHERE wf_id=?",
                    (owner_pid, now, wf_id),
                )
            updated = conn.execute("SELECT * FROM workflow_runs WHERE wf_id = ?", (wf_id,)).fetchone()
        return self._row_to_run(updated)

    def get_workflow_script_source(self, wf_id: str) -> str | None:
        if not self.exists():
            raise WorkflowError("not_found", f"Workflow '{wf_id}' not found.", EXIT_NOT_FOUND)
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM workflow_runs WHERE wf_id = ?", (wf_id,)).fetchone()
            if row is None:
                raise WorkflowError("not_found", f"Workflow '{wf_id}' not found.", EXIT_NOT_FOUND)
            return _row_get(row, "script_source")

    def is_cancel_requested(self, wf_id: str) -> bool:
        if not self.exists():
            return False
        try:
            with self.connection() as conn:
                row = conn.execute("SELECT * FROM workflow_runs WHERE wf_id = ?", (wf_id,)).fetchone()
        except (WorkflowError, sqlite3.Error):
            return False
        if row is None:
            return False
        return row["status"] in ("cancelling", "cancelled") or _row_get(row, "cancel_requested_at") is not None

    def request_cancel_workflow(self, wf_id: str, *, actor: str) -> dict[str, Any]:
        """Atomically request cancellation for a workflow run.

        If a live owner process exists, sets ``cancel_requested_at`` and moves the
        workflow to ``cancelling`` so the owner can stop in-flight work and settle
        to ``cancelled``. Otherwise marks active nodes and the run ``cancelled``
        immediately. Never overwrites already-terminal runs.
        """
        now = time.time()
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM workflow_runs WHERE wf_id = ?", (wf_id,)).fetchone()
            if row is None:
                raise WorkflowError("not_found", f"Workflow '{wf_id}' not found.", EXIT_NOT_FOUND)
            status = row["status"]
            owner_pid = _row_get(row, "owner_pid")
            if status in ("completed", "failed", "cancelled"):
                return {
                    "workflow": self._row_to_run(row),
                    "already_terminal": True,
                    "owner_pid": owner_pid,
                    "owner_alive": False,
                }
            owner_alive = bool(owner_pid and owner_pid != os.getpid() and _pid_alive(owner_pid))
            cancel_ts = _row_get(row, "cancel_requested_at") or now
            if owner_alive:
                conn.execute(
                    """
                    UPDATE workflow_runs
                    SET status='cancelling', cancel_requested_at=?, updated_at=?
                    WHERE wf_id=?
                    """,
                    (cancel_ts, now, wf_id),
                )
                self._append_event(
                    conn, wf_id, "workflow.cancel_requested", None, {"status": "cancelling", "owner_pid": owner_pid}
                )
            else:
                cancel_err = _json_dumps({"code": "cancelled", "message": "Workflow was cancelled."})
                conn.execute(
                    """
                    UPDATE workflow_nodes
                    SET status='cancelled', finished_at=COALESCE(finished_at, ?), updated_at=?
                    WHERE wf_id=? AND status IN ('running', 'queued')
                    """,
                    (now, now, wf_id),
                )
                conn.execute(
                    """
                    UPDATE workflow_runs
                    SET status='cancelled', owner_pid=NULL, cancel_requested_at=?,
                        error_json=?, finished_at=COALESCE(finished_at, ?), updated_at=?
                    WHERE wf_id=?
                    """,
                    (cancel_ts, cancel_err, now, now, wf_id),
                )
                self._append_event(conn, wf_id, "workflow.status", None, {"status": "cancelled"})
            self._audit(
                conn,
                actor=actor,
                action="workflow.cancel",
                target_id=wf_id,
                detail={"owner_pid": owner_pid, "owner_alive": owner_alive},
            )
            updated = conn.execute("SELECT * FROM workflow_runs WHERE wf_id = ?", (wf_id,)).fetchone()
            counts = self._node_counts(conn, wf_id)
        wf = self._row_to_run(updated)
        wf["counts"] = counts
        return {
            "workflow": wf,
            "already_terminal": False,
            "owner_pid": owner_pid,
            "owner_alive": owner_alive,
        }

    def finalize_cancelled_workflow(self, wf_id: str, *, actor: str) -> dict[str, Any]:
        """Mark remaining active nodes and the workflow itself ``cancelled`` after owner exit."""
        now = time.time()
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM workflow_runs WHERE wf_id = ?", (wf_id,)).fetchone()
            if row is None:
                raise WorkflowError("not_found", f"Workflow '{wf_id}' not found.", EXIT_NOT_FOUND)
            if row["status"] not in ("completed", "failed"):
                cancel_err = _json_dumps({"code": "cancelled", "message": "Workflow was cancelled."})
                conn.execute(
                    """
                    UPDATE workflow_nodes
                    SET status='cancelled', finished_at=COALESCE(finished_at, ?), updated_at=?
                    WHERE wf_id=? AND status IN ('running', 'queued')
                    """,
                    (now, now, wf_id),
                )
                conn.execute(
                    """
                    UPDATE workflow_runs
                    SET status='cancelled', owner_pid=NULL,
                        cancel_requested_at=COALESCE(cancel_requested_at, ?),
                        error_json=COALESCE(error_json, ?),
                        finished_at=COALESCE(finished_at, ?),
                        updated_at=?
                    WHERE wf_id=?
                    """,
                    (now, cancel_err, now, now, wf_id),
                )
                if row["status"] != "cancelled":
                    self._append_event(conn, wf_id, "workflow.status", None, {"status": "cancelled"})
                    self._audit(conn, actor=actor, action="workflow.cancelled", target_id=wf_id)
            updated = conn.execute("SELECT * FROM workflow_runs WHERE wf_id = ?", (wf_id,)).fetchone()
            counts = self._node_counts(conn, wf_id)
        wf = self._row_to_run(updated)
        wf["counts"] = counts
        return wf

    def mark_running_nodes_cancelled(self, wf_id: str) -> int:
        if not self.exists():
            return 0
        now = time.time()
        with self.connection(write=True) as conn:
            cur = conn.execute(
                """
                UPDATE workflow_nodes
                SET status='cancelled', finished_at=COALESCE(finished_at, ?), updated_at=?
                WHERE wf_id=? AND status IN ('running', 'queued')
                """,
                (now, now, wf_id),
            )
            return int(cur.rowcount or 0)

    def update_workflow_run(
        self,
        wf_id: str,
        *,
        status: str | None = None,
        active_phase: str | None = None,
        result: Any = ...,
        error: dict[str, Any] | None = ...,
        finished: bool = False,
        actor: str | None = None,
        allow_from_cancelled: bool = False,
        clear_owner: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        with self.connection(write=True) as conn:
            row = conn.execute("SELECT * FROM workflow_runs WHERE wf_id = ?", (wf_id,)).fetchone()
            if row is None:
                raise WorkflowError("not_found", f"Workflow '{wf_id}' not found.", EXIT_NOT_FOUND)
            cancel_requested = (
                row["status"] in ("cancelling", "cancelled")
                or _row_get(row, "cancel_requested_at") is not None
            )
            new_status = status if status is not None else row["status"]
            new_phase = active_phase if active_phase is not None else row["active_phase"]
            new_result = row["result_json"] if result is ... else _json_dumps(result)
            new_error = row["error_json"] if error is ... else (_json_dumps(error) if error is not None else None)
            finished_at = now if finished else row["finished_at"]
            owner_pid = None if (finished or clear_owner) else _row_get(row, "owner_pid")

            if cancel_requested and not allow_from_cancelled:
                # Never allow a late completion/progress write to overwrite a cancelled run.
                if finished or status == "cancelled":
                    new_status = "cancelled"
                    new_result = row["result_json"]
                    new_error = _json_dumps({"code": "cancelled", "message": "Workflow was cancelled."})
                    finished_at = row["finished_at"] or now
                    owner_pid = None
                else:
                    new_status = row["status"]
                    new_result = row["result_json"]
                    new_error = row["error_json"]

            conn.execute(
                """
                UPDATE workflow_runs
                SET status=?, active_phase=?, result_json=?, error_json=?, owner_pid=?, finished_at=?, updated_at=?
                WHERE wf_id=?
                """,
                (new_status, new_phase, new_result, new_error, owner_pid, finished_at, now, wf_id),
            )
            if new_status != row["status"]:
                self._append_event(
                    conn, wf_id, "workflow.status", None, {"status": new_status, "active_phase": new_phase}
                )
            if actor is not None and status is not None:
                self._audit(conn, actor=actor, action=f"workflow.{new_status}", target_id=wf_id)
            updated = conn.execute("SELECT * FROM workflow_runs WHERE wf_id = ?", (wf_id,)).fetchone()
        return self._row_to_run(updated)

    def get_workflow_run(self, wf_id: str) -> dict[str, Any]:
        if not self.exists():
            raise WorkflowError("not_found", f"Workflow '{wf_id}' not found.", EXIT_NOT_FOUND)
        with self.connection() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                raise WorkflowError("not_found", f"Workflow '{wf_id}' not found.", EXIT_NOT_FOUND)
            row = conn.execute("SELECT * FROM workflow_runs WHERE wf_id = ?", (wf_id,)).fetchone()
            if row is None:
                raise WorkflowError("not_found", f"Workflow '{wf_id}' not found.", EXIT_NOT_FOUND)
            counts = self._node_counts(conn, wf_id)
        data = self._row_to_run(row)
        data["counts"] = counts
        return data

    def list_workflow_runs(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if not self.exists():
            return []
        with self.connection() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                return []
            if status:
                rows = conn.execute(
                    "SELECT * FROM workflow_runs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, max(1, limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT ?",
                    (max(1, limit),),
                ).fetchall()
            items = []
            for row in rows:
                item = self._row_to_run(row)
                item["counts"] = self._node_counts(conn, row["wf_id"])
                items.append(item)
        return items

    def upsert_node(
        self,
        wf_id: str,
        node_id: str,
        *,
        step_key: str | None = None,
        input_hash: str | None = None,
        parent_id: str | None = None,
        node_type: str = "step",
        kind: str = "agent",
        role: str = "",
        status: str = "queued",
        requested: dict[str, Any] | None = None,
        resolved: dict[str, Any] | None = None,
        observed: dict[str, Any] | None = None,
        schema: dict[str, Any] | None = None,
        output: Any = ...,
        output_committed: bool | None = None,
        session_ref: str | None = None,
        keepalive_name: str | None = None,
        worktree_path: str | None = None,
        attempt: int | None = None,
        progress_note: str | None = None,
        error: dict[str, Any] | None = ...,
        started_at: float | None = None,
        finished_at: float | None = None,
        duration_ms: int | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self.connection(write=True) as conn:
            if status == "completed":
                run_row = conn.execute(
                    "SELECT status, cancel_requested_at FROM workflow_runs WHERE wf_id = ?",
                    (wf_id,),
                ).fetchone()
                if run_row is not None and (
                    run_row["status"] in ("cancelling", "cancelled")
                    or _row_get(run_row, "cancel_requested_at") is not None
                ):
                    status = "cancelled"
                    output = ...
                    output_committed = False
                    error = {"code": "cancelled", "message": "Workflow was cancelled."}
            existing = conn.execute(
                "SELECT * FROM workflow_nodes WHERE wf_id = ? AND node_id = ?",
                (wf_id, node_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO workflow_nodes
                        (wf_id, node_id, step_key, input_hash, parent_id, node_type, kind, role, status,
                         requested_json, resolved_json, observed_json, schema_json, output_json,
                         output_committed, session_ref, keepalive_name, worktree_path, attempt,
                         progress_note, error_json, started_at, finished_at, duration_ms, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        wf_id,
                        node_id,
                        step_key,
                        input_hash,
                        parent_id,
                        node_type,
                        kind,
                        role or node_id,
                        status,
                        _json_dumps(requested) if requested is not None else None,
                        _json_dumps(resolved) if resolved is not None else None,
                        _json_dumps(observed) if observed is not None else None,
                        _json_dumps(schema) if schema is not None else None,
                        None if output is ... else _json_dumps(output),
                        1 if output_committed else 0,
                        session_ref,
                        keepalive_name,
                        worktree_path,
                        attempt or 0,
                        progress_note,
                        None if error is ... or error is None else _json_dumps(error),
                        started_at,
                        finished_at,
                        duration_ms,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE workflow_nodes SET
                        step_key = COALESCE(?, step_key),
                        input_hash = COALESCE(?, input_hash),
                        parent_id = COALESCE(?, parent_id),
                        role = COALESCE(NULLIF(?, ''), role),
                        status = ?,
                        requested_json = COALESCE(?, requested_json),
                        resolved_json = COALESCE(?, resolved_json),
                        observed_json = COALESCE(?, observed_json),
                        schema_json = COALESCE(?, schema_json),
                        output_json = ?,
                        output_committed = ?,
                        session_ref = COALESCE(?, session_ref),
                        keepalive_name = COALESCE(?, keepalive_name),
                        worktree_path = COALESCE(?, worktree_path),
                        attempt = COALESCE(?, attempt),
                        progress_note = COALESCE(?, progress_note),
                        error_json = ?,
                        started_at = COALESCE(?, started_at),
                        finished_at = COALESCE(?, finished_at),
                        duration_ms = COALESCE(?, duration_ms),
                        updated_at = ?
                    WHERE wf_id = ? AND node_id = ?
                    """,
                    (
                        step_key,
                        input_hash,
                        parent_id,
                        role,
                        status,
                        _json_dumps(requested) if requested is not None else None,
                        _json_dumps(resolved) if resolved is not None else None,
                        _json_dumps(observed) if observed is not None else None,
                        _json_dumps(schema) if schema is not None else None,
                        existing["output_json"] if output is ... else _json_dumps(output),
                        existing["output_committed"] if output_committed is None else (1 if output_committed else 0),
                        session_ref,
                        keepalive_name,
                        worktree_path,
                        attempt,
                        progress_note,
                        existing["error_json"] if error is ... else (_json_dumps(error) if error is not None else None),
                        started_at,
                        finished_at,
                        duration_ms,
                        now,
                        wf_id,
                        node_id,
                    ),
                )
            if session_ref or keepalive_name:
                self._register_workflow_session(
                    conn,
                    wf_id=wf_id,
                    node_id=node_id,
                    phase=parent_id,
                    session_ref=session_ref,
                    keepalive_name=keepalive_name,
                )
            self._append_event(
                conn,
                wf_id,
                "node.upsert",
                node_id,
                {"node_type": node_type, "kind": kind, "role": role or node_id, "status": status},
            )
            saved = conn.execute(
                "SELECT * FROM workflow_nodes WHERE wf_id = ? AND node_id = ?",
                (wf_id, node_id),
            ).fetchone()
        if session_ref or keepalive_name:
            from corral_workflow.state_bridge import register_hidden_session

            register_hidden_session(
                session_key_value=session_ref,
                keepalive_name=keepalive_name,
                wf_id=wf_id,
                store_root=self.root,
            )
        return self._row_to_node(saved)

    def get_node(self, wf_id: str, node_id: str) -> dict[str, Any]:
        if not self.exists():
            raise WorkflowError("not_found", f"Workflow step '{wf_id}/{node_id}' not found.", EXIT_NOT_FOUND)
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM workflow_nodes WHERE wf_id = ? AND node_id = ?",
                (wf_id, node_id),
            ).fetchone()
        if row is None:
            raise WorkflowError("not_found", f"Workflow step '{wf_id}/{node_id}' not found.", EXIT_NOT_FOUND)
        return self._row_to_node(row)

    def find_completed_checkpoint(self, wf_id: str, step_key: str) -> dict[str, Any] | None:
        if not self.exists():
            return None
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM workflow_nodes
                WHERE wf_id = ? AND step_key = ? AND status = 'completed'
                ORDER BY updated_at DESC LIMIT 1
                """,
                (wf_id, step_key),
            ).fetchone()
        return self._row_to_node(row) if row else None

    def clear_checkpoints_from(
        self, wf_id: str, step_key: str, *, actor: str, ignore_missing: bool = False
    ) -> list[str]:
        """Reset ``step_key`` and all nodes started at or after it so resume re-executes them."""
        if not self.exists():
            if ignore_missing:
                return []
            raise WorkflowError("not_found", f"Workflow '{wf_id}' not found.", EXIT_NOT_FOUND)
        with self.connection(write=True) as conn:
            anchor = conn.execute(
                """
                SELECT node_id, started_at, updated_at FROM workflow_nodes
                WHERE wf_id = ? AND step_key = ?
                ORDER BY COALESCE(started_at, updated_at) DESC LIMIT 1
                """,
                (wf_id, step_key),
            ).fetchone()
            if anchor is None:
                if ignore_missing:
                    return []
                raise WorkflowError(
                    "not_found",
                    f"Step key '{step_key}' not found in workflow '{wf_id}'.",
                    EXIT_NOT_FOUND,
                )
            cutoff = anchor["started_at"] if anchor["started_at"] is not None else anchor["updated_at"]
            rows = conn.execute(
                """
                SELECT node_id, step_key FROM workflow_nodes
                WHERE wf_id = ? AND node_type = 'step'
                  AND (step_key = ? OR COALESCE(started_at, updated_at) >= ?)
                """,
                (wf_id, step_key, cutoff),
            ).fetchall()
            cleared: list[str] = []
            for row in rows:
                conn.execute(
                    """
                    UPDATE workflow_nodes
                    SET status = 'queued', input_hash = NULL, output_json = NULL, output_committed = 0,
                        error_json = NULL, finished_at = NULL, duration_ms = NULL, updated_at = ?
                    WHERE wf_id = ? AND node_id = ?
                    """,
                    (time.time(), wf_id, row["node_id"]),
                )
                cleared.append(row["step_key"] or row["node_id"])
            self._audit(
                conn,
                actor=actor,
                action="workflow.clear_checkpoints",
                target_id=wf_id,
                detail={"from_step": step_key, "cleared": cleared},
            )
        return cleared

    def commit_step_output(
        self,
        wf_id: str,
        node_id: str,
        payload: Any,
        *,
        actor: str,
    ) -> dict[str, Any]:
        """Atomically persist validated structured JSON output submitted via ``corral workflow finish``."""
        now = time.time()
        with self.connection(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM workflow_nodes WHERE wf_id = ? AND node_id = ?",
                (wf_id, node_id),
            ).fetchone()
            if row is None:
                raise WorkflowError("not_found", f"Workflow step '{wf_id}/{node_id}' not found.", EXIT_NOT_FOUND)
            conn.execute(
                """
                UPDATE workflow_nodes
                SET output_json = ?, output_committed = 1, updated_at = ?
                WHERE wf_id = ? AND node_id = ?
                """,
                (_json_dumps(payload), now, wf_id, node_id),
            )
            self._append_event(conn, wf_id, "step.output_committed", node_id, {"committed": True})
            self._audit(conn, actor=actor, action="step.finish", target_id=f"{wf_id}/{node_id}")
            saved = conn.execute(
                "SELECT * FROM workflow_nodes WHERE wf_id = ? AND node_id = ?",
                (wf_id, node_id),
            ).fetchone()
        return self._row_to_node(saved)

    def record_step_log(self, wf_id: str, node_id: str, message: str) -> dict[str, Any]:
        now = time.time()
        text = message.strip()[:300]
        with self.connection(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM workflow_nodes WHERE wf_id = ? AND node_id = ?",
                (wf_id, node_id),
            ).fetchone()
            if row is None:
                raise WorkflowError("not_found", f"Workflow step '{wf_id}/{node_id}' not found.", EXIT_NOT_FOUND)
            conn.execute(
                "UPDATE workflow_nodes SET progress_note = ?, updated_at = ? WHERE wf_id = ? AND node_id = ?",
                (text, now, wf_id, node_id),
            )
            self._append_event(conn, wf_id, "step.log", node_id, {"message": text})
            saved = conn.execute(
                "SELECT * FROM workflow_nodes WHERE wf_id = ? AND node_id = ?",
                (wf_id, node_id),
            ).fetchone()
        return self._row_to_node(saved)

    def add_edge(self, wf_id: str, from_node: str, to_node: str, edge_kind: str = "sequence") -> None:
        now = time.time()
        with self.connection(write=True) as conn:
            conn.execute(
                "INSERT INTO workflow_edges (wf_id, from_node, to_node, edge_kind, created_at) VALUES (?, ?, ?, ?, ?)",
                (wf_id, from_node, to_node, edge_kind, now),
            )
            self._append_event(
                conn,
                wf_id,
                "edge.add",
                to_node,
                {"from": from_node, "to": to_node, "edge_kind": edge_kind},
            )

    def get_graph(self, wf_id: str) -> dict[str, Any]:
        run = self.get_workflow_run(wf_id)
        with self.connection() as conn:
            nodes = [
                self._row_to_node(r)
                for r in conn.execute(
                    "SELECT * FROM workflow_nodes WHERE wf_id = ? "
                    "ORDER BY COALESCE(started_at, updated_at) ASC, node_id ASC",
                    (wf_id,),
                ).fetchall()
            ]
            edges = [
                {
                    "seq": r["seq"],
                    "from": r["from_node"],
                    "to": r["to_node"],
                    "edge_kind": r["edge_kind"],
                    "created_at": r["created_at"],
                }
                for r in conn.execute(
                    "SELECT * FROM workflow_edges WHERE wf_id = ? ORDER BY seq ASC",
                    (wf_id,),
                ).fetchall()
            ]
        return {"workflow": run, "nodes": nodes, "edges": edges}

    def list_events(self, wf_id: str, *, after: int = 0, limit: int = 100) -> dict[str, Any]:
        _ = self.get_workflow_run(wf_id)
        capped = max(1, min(limit, 500))
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM workflow_events WHERE wf_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (wf_id, after, capped + 1),
            ).fetchall()
        has_more = len(rows) > capped
        rows = rows[:capped]
        events = [
            {
                "seq": r["seq"],
                "wf_id": r["wf_id"],
                "kind": r["kind"],
                "node_id": r["node_id"],
                "payload": _json_loads(r["payload_json"], {}),
                "created_at": r["created_at"],
                "created_at_utc": utc_iso(r["created_at"]),
            }
            for r in rows
        ]
        next_cursor = events[-1]["seq"] if events else after
        return {"events": events, "next_cursor": next_cursor, "has_more": has_more}

    def get_workflow_optimization_signals(
        self,
        name: str,
        *,
        script_path: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Aggregate historical execution metrics to guide workflow reuse and optimization."""
        empty = {
            "runs_analyzed": 0,
            "completed": 0,
            "failed": 0,
            "blocked": 0,
            "cancelled": 0,
            "running": 0,
            "success_rate": None,
            "avg_elapsed_ms": None,
            "latest_run": None,
            "slowest_steps": [],
            "degraded_steps": [],
            "failure_hotspots": [],
        }
        if not self.exists():
            return empty
        capped = max(1, min(limit, 100))
        with self.connection() as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                return empty
            if script_path:
                run_rows = conn.execute(
                    """
                    SELECT * FROM workflow_runs
                    WHERE name = ? OR script_path = ?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (name, script_path, capped),
                ).fetchall()
            else:
                run_rows = conn.execute(
                    "SELECT * FROM workflow_runs WHERE name = ? ORDER BY created_at DESC LIMIT ?",
                    (name, capped),
                ).fetchall()
            if not run_rows:
                return empty
            wf_ids = [r["wf_id"] for r in run_rows]
            placeholders = ",".join("?" for _ in wf_ids)
            node_rows = conn.execute(
                f"SELECT * FROM workflow_nodes WHERE wf_id IN ({placeholders}) AND node_type = 'step'",
                wf_ids,
            ).fetchall()

        counts = {"completed": 0, "failed": 0, "blocked": 0, "cancelled": 0, "running": 0}
        elapsed_vals: list[int] = []
        latest_rec = self._row_to_run(run_rows[0])
        for row in run_rows:
            st = row["status"]
            if st in counts:
                counts[st] += 1
            elif st in {"cancelling", "waiting_input"}:
                counts["running"] += 1
            if row["started_at"] is not None and row["finished_at"] is not None:
                elapsed_vals.append(max(0, int((row["finished_at"] - row["started_at"]) * 1000)))

        terminal = counts["completed"] + counts["failed"] + counts["blocked"] + counts["cancelled"]
        success_rate = round(counts["completed"] / terminal, 3) if terminal > 0 else None
        avg_elapsed = int(sum(elapsed_vals) / len(elapsed_vals)) if elapsed_vals else None

        step_stats: dict[str, dict[str, Any]] = {}
        for nrow in node_rows:
            key = nrow["step_key"] or nrow["role"] or nrow["node_id"]
            bucket = step_stats.setdefault(
                key,
                {
                    "step": key,
                    "kind": nrow["kind"],
                    "executions": 0,
                    "durations": [],
                    "failures": 0,
                    "retries": 0,
                    "degraded_count": 0,
                    "degraded_targets": set(),
                },
            )
            bucket["executions"] += 1
            if nrow["duration_ms"] is not None:
                bucket["durations"].append(int(nrow["duration_ms"]))
            if nrow["status"] in {"failed", "blocked"}:
                bucket["failures"] += 1
            if (nrow["attempt"] or 0) > 1:
                bucket["retries"] += int(nrow["attempt"]) - 1
            resolved = _json_loads(nrow["resolved_json"], None)
            if isinstance(resolved, dict) and resolved.get("degraded"):
                bucket["degraded_count"] += 1
                if resolved.get("target_id"):
                    bucket["degraded_targets"].add(str(resolved["target_id"]))

        slowest = []
        degraded = []
        hotspots = []
        for item in step_stats.values():
            if item["durations"]:
                avg_ms = int(sum(item["durations"]) / len(item["durations"]))
                max_ms = max(item["durations"])
                slowest.append(
                    {
                        "step": item["step"],
                        "kind": item["kind"],
                        "executions": item["executions"],
                        "avg_duration_ms": avg_ms,
                        "max_duration_ms": max_ms,
                    }
                )
            if item["degraded_count"] > 0:
                degraded.append(
                    {
                        "step": item["step"],
                        "degraded_count": item["degraded_count"],
                        "fallback_targets": sorted(item["degraded_targets"]),
                    }
                )
            if item["failures"] > 0 or item["retries"] > 0:
                hotspots.append(
                    {
                        "step": item["step"],
                        "failures": item["failures"],
                        "retries": item["retries"],
                        "executions": item["executions"],
                    }
                )

        slowest.sort(key=lambda x: (-x["avg_duration_ms"], x["step"]))
        degraded.sort(key=lambda x: (-x["degraded_count"], x["step"]))
        hotspots.sort(key=lambda x: (-x["failures"], -x["retries"], x["step"]))

        return {
            "runs_analyzed": len(run_rows),
            **counts,
            "success_rate": success_rate,
            "avg_elapsed_ms": avg_elapsed,
            "latest_run": {
                "wf_id": latest_rec["wf_id"],
                "status": latest_rec["status"],
                "script_sha256": latest_rec["script_sha256"],
                "elapsed_ms": latest_rec["elapsed_ms"],
                "created_at_utc": latest_rec["created_at_utc"],
            },
            "slowest_steps": slowest[:5],
            "degraded_steps": degraded[:5],
            "failure_hotspots": hotspots[:5],
        }

    def get_exported_skills(self) -> dict[str, dict[str, Any]]:
        """Return Corral-owned thin exported workflow skill records keyed by absolute SKILL.md path."""
        data = self.get_registry_state("exported_skills")
        if not isinstance(data, dict):
            return {}
        return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}

    def record_exported_skill(self, skill_md_path: str, record: dict[str, Any], *, actor: str) -> None:
        current = self.get_exported_skills()
        current[skill_md_path] = dict(record)
        self.set_registry_state("exported_skills", current, actor=actor)

    def remove_exported_skill(self, skill_md_path: str, *, actor: str) -> None:
        current = self.get_exported_skills()
        if skill_md_path in current:
            current.pop(skill_md_path, None)
            self.set_registry_state("exported_skills", current, actor=actor)

    def record_playbook_audit(
        self,
        *,
        actor: str,
        action: str,
        target_id: str,
        before_version: str | None = None,
        after_version: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self.connection(write=True) as conn:
            self._audit(
                conn,
                actor=actor,
                action=action,
                target_id=target_id,
                before_version=before_version,
                after_version=after_version,
                detail=detail,
            )

    def register_workflow_session(
        self,
        *,
        wf_id: str,
        node_id: str,
        phase: str | None = None,
        session_ref: str | None = None,
        keepalive_name: str | None = None,
    ) -> None:
        with self.connection(write=True) as conn:
            self._register_workflow_session(
                conn,
                wf_id=wf_id,
                node_id=node_id,
                phase=phase,
                session_ref=session_ref,
                keepalive_name=keepalive_name,
            )
        if session_ref or keepalive_name:
            from corral_workflow.state_bridge import register_hidden_session

            register_hidden_session(
                session_key_value=session_ref,
                keepalive_name=keepalive_name,
                wf_id=wf_id,
                store_root=self.root,
            )

    def _register_workflow_session(
        self,
        conn: sqlite3.Connection,
        *,
        wf_id: str,
        node_id: str,
        phase: str | None,
        session_ref: str | None,
        keepalive_name: str | None,
    ) -> None:
        now = time.time()
        keys = [k for k in (session_ref, keepalive_name) if k]
        for key in keys:
            conn.execute(
                """
                INSERT INTO workflow_sessions (session_key, wf_id, node_id, phase, keepalive_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    wf_id=excluded.wf_id,
                    node_id=excluded.node_id,
                    phase=COALESCE(excluded.phase, workflow_sessions.phase),
                    keepalive_name=COALESCE(excluded.keepalive_name, workflow_sessions.keepalive_name)
                """,
                (key, wf_id, node_id, phase, keepalive_name, now),
            )

    def known_workflow_session_keys(self) -> set[str]:
        """Fast read-only snapshot of all registered workflow child session keys."""
        if not self.exists():
            return set()
        try:
            with self.connection() as conn:
                rows = conn.execute("SELECT session_key, keepalive_name FROM workflow_sessions").fetchall()
        except (WorkflowError, sqlite3.Error):
            return set()
        keys: set[str] = set()
        for row in rows:
            if row["session_key"]:
                keys.add(row["session_key"])
            if row["keepalive_name"]:
                keys.add(row["keepalive_name"])
        return keys

    def _append_event(
        self,
        conn: sqlite3.Connection,
        wf_id: str,
        kind: str,
        node_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO workflow_events (wf_id, kind, node_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (wf_id, kind, node_id, _json_dumps(payload), time.time()),
        )

    @staticmethod
    def _node_counts(conn: sqlite3.Connection, wf_id: str) -> dict[str, int]:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM workflow_nodes WHERE wf_id = ? AND node_type = 'step' GROUP BY status",
            (wf_id,),
        ).fetchall()
        counts = {
            "total": 0,
            "queued": 0,
            "running": 0,
            "waiting_input": 0,
            "blocked": 0,
            "completed": 0,
            "failed": 0,
            "cancelled": 0,
        }
        for row in rows:
            st = row["status"]
            cnt = int(row["cnt"])
            counts["total"] += cnt
            if st in counts:
                counts[st] += cnt
        return counts

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
        started = row["started_at"]
        finished = row["finished_at"]
        elapsed_ms = None
        if started is not None:
            end = finished if finished is not None else time.time()
            elapsed_ms = max(0, int((end - started) * 1000))
        return {
            "wf_id": row["wf_id"],
            "name": row["name"],
            "script_path": row["script_path"],
            "script_sha256": row["script_sha256"],
            "has_script_snapshot": _row_get(row, "script_source") is not None,
            "cwd": _row_get(row, "cwd"),
            "args": _json_loads(row["args_json"], {}),
            "status": row["status"],
            "active_phase": row["active_phase"],
            "max_concurrency": row["max_concurrency"],
            "owner_pid": _row_get(row, "owner_pid"),
            "cancel_requested_at": _row_get(row, "cancel_requested_at"),
            "result": _json_loads(row["result_json"], None),
            "error": _json_loads(row["error_json"], None),
            "created_at": row["created_at"],
            "created_at_utc": utc_iso(row["created_at"]),
            "started_at": started,
            "finished_at": finished,
            "elapsed_ms": elapsed_ms,
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "wf_id": row["wf_id"],
            "node_id": row["node_id"],
            "step_key": row["step_key"],
            "input_hash": _row_get(row, "input_hash"),
            "parent_id": row["parent_id"],
            "node_type": row["node_type"],
            "kind": row["kind"],
            "role": row["role"],
            "status": row["status"],
            "requested": _json_loads(row["requested_json"], None),
            "resolved": _json_loads(row["resolved_json"], None),
            "observed": _json_loads(row["observed_json"], None),
            "schema": _json_loads(row["schema_json"], None),
            "output": _json_loads(row["output_json"], None),
            "output_committed": bool(row["output_committed"]),
            "session_ref": row["session_ref"],
            "keepalive_name": row["keepalive_name"],
            "worktree_path": row["worktree_path"],
            "attempt": row["attempt"],
            "progress_note": row["progress_note"],
            "error": _json_loads(row["error_json"], None),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "duration_ms": row["duration_ms"],
            "updated_at": row["updated_at"],
        }
