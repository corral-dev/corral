"""Stdlib-only bridges to Corral host state contracts and LLM gateway config.

Keeps ``corral_workflow`` completely independent of the ``corral`` main package
(and therefore free of Textual, TUI, and remote dependencies).
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ACTIVE_RUNTIME_IDS: tuple[str, ...] = ("claude", "codex", "opencode", "cursor", "pi", "antigravity")

_PREFERRED_GATEWAY_MODEL = "budget-chat"
_MODEL_CATALOG_TTL_SECONDS = 60.0
_model_cache: tuple[float, str] | None = None

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


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "corral"


def default_workflow_root() -> Path:
    return config_home() / "workflows"


def resolve_state_dir(store_root: Path | None = None) -> Path:
    """Resolve state dir without polluting host ~/.config during isolated tests."""
    explicit = os.environ.get("CORRAL_STATE_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("XDG_CONFIG_HOME", "").strip():
        return config_home() / "state"
    if store_root is not None:
        try:
            if Path(store_root).resolve() != default_workflow_root().resolve():
                return Path(store_root) / "state"
        except OSError:
            return Path(store_root) / "state"
    return config_home() / "state"


def corral_bin() -> str:
    override = os.environ.get("CORRAL_BIN", "").strip()
    if override:
        return override
    return shutil.which("corral") or "corral"


def caller_identity() -> dict[str, str | None]:
    runtime = (os.environ.get("CORRAL_RUNTIME") or os.environ.get("SC_RUNTIME") or "").strip() or None
    ident = (os.environ.get("CORRAL_SESSION_ID") or os.environ.get("SC_SESSION_ID") or "").strip() or None
    actor = (os.environ.get("CORRAL_ACTOR") or "").strip() or (
        f"{runtime}:{ident}" if runtime and ident else "local-user"
    )
    return {"actor": actor, "runtime": runtime, "pane_ident": ident}


def session_key(session: dict[str, Any]) -> str:
    return f"{session.get('source') or ''}:{session.get('id') or ''}"


@contextmanager
def _connect_rw(db_path: Path, schema_sql: str):
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level="IMMEDIATE")
    try:
        conn.execute("PRAGMA busy_timeout=5000")
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


def register_hidden_session(
    *,
    session_key_value: str | None,
    keepalive_name: str | None,
    wf_id: str,
    store_root: Path | None = None,
) -> None:
    keys = [k for k in (session_key_value, keepalive_name) if isinstance(k, str) and k]
    if not keys:
        return
    raw_id = None
    if isinstance(session_key_value, str) and ":" in session_key_value:
        raw_id = session_key_value.split(":", 1)[1] or None
    db_path = resolve_state_dir(store_root) / "hidden-sessions.sqlite3"
    now = time.time()
    try:
        with _connect_rw(db_path, _HIDDEN_SCHEMA_SQL) as conn:
            for key in keys:
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
                    (key, keepalive_name or None, raw_id, "workflow", wf_id, now),
                )
    except OSError:
        pass
    except sqlite3.Error:
        pass


def acquire_host_lease(
    *,
    lease_id: str,
    keepalive_name: str,
    ttl_seconds: float = 300.0,
    store_root: Path | None = None,
) -> None:
    if not keepalive_name:
        return
    db_path = resolve_state_dir(store_root) / "host-leases.sqlite3"
    now = time.time()
    expires_at = now + max(1.0, float(ttl_seconds))
    try:
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
                ("workflow", lease_id, keepalive_name, os.getpid(), now, expires_at),
            )
    except (OSError, sqlite3.Error):
        pass


def release_host_lease(
    *,
    lease_id: str | None = None,
    keepalive_name: str | None = None,
    store_root: Path | None = None,
) -> None:
    db_path = resolve_state_dir(store_root) / "host-leases.sqlite3"
    if not db_path.is_file():
        return
    try:
        with _connect_rw(db_path, _LEASES_SCHEMA_SQL) as conn:
            if lease_id:
                conn.execute("DELETE FROM host_leases WHERE owner = ? AND lease_id = ?", ("workflow", lease_id))
            elif keepalive_name:
                conn.execute(
                    "DELETE FROM host_leases WHERE owner = ? AND keepalive_name = ?",
                    ("workflow", keepalive_name),
                )
    except (OSError, sqlite3.Error):
        pass


# ---------------------------------------------------------------------------
# LLM Gateway config helpers (mirrors corral.titlegen configuration)
# ---------------------------------------------------------------------------


def _gateway_config_path() -> Path:
    configured = os.environ.get("CORRAL_LLM_GATEWAY_CONFIG", "").strip()
    if configured:
        return Path(configured).expanduser()
    return config_home() / "llm-gateway.json"


def _gateway_setting(env_name: str, config_name: str, default: str = "") -> str:
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    try:
        config = json.loads(_gateway_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    val = config.get(config_name, "") if isinstance(config, dict) else ""
    return str(val).strip() or default


def gateway_url() -> str:
    return _gateway_setting("CORRAL_LLM_GATEWAY_URL", "base_url", "").rstrip("/")


def gateway_key() -> str | None:
    return _gateway_setting("CORRAL_LLM_GATEWAY_KEY", "api_key") or None


def fetch_model_aliases() -> list[str]:
    url = gateway_url()
    if not url:
        return []
    request = urllib.request.Request(
        f"{url}/models",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read(1_000_000).decode("utf-8"))
    except (OSError, TimeoutError, ValueError, UnicodeError, urllib.error.HTTPError):
        return []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    aliases: list[str] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
            aliases.append(item["id"].strip())
    return aliases


def gateway_model() -> str | None:
    global _model_cache
    explicit = _gateway_setting("CORRAL_TITLE_MODEL", "model") or None
    if explicit:
        return explicit
    now = time.monotonic()
    if _model_cache is not None and now - _model_cache[0] < _MODEL_CATALOG_TTL_SECONDS:
        return _model_cache[1]
    aliases = fetch_model_aliases()
    if not aliases:
        return None
    chosen = _PREFERRED_GATEWAY_MODEL if _PREFERRED_GATEWAY_MODEL in aliases else aliases[0]
    _model_cache = (now, chosen)
    return chosen


def assistant_text(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        text = "".join(parts)
        return text or None
    return None
