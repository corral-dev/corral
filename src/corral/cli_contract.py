"""Lightweight shared CLI contract and path helpers (stdlib-only).

Shared by ``agent_api``, ``plugins``, and internal state commands without
importing Textual, SessKit, or runtime registries.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

AGENT_API_VERSION = 1

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 5
EXIT_TIMEOUT = 6


def corral_bin() -> str:
    """Resolve the exact invocation identity for machine-copied ``next_commands``."""
    override = os.environ.get("CORRAL_BIN", "").strip()
    if override:
        return override
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0.endswith("__main__.py"):
        return f"{sys.executable} -m corral"
    if argv0:
        candidate = argv0 if os.path.isabs(argv0) else os.path.abspath(argv0)
        try:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            pass
    return shutil.which("corral") or "corral"


def config_home() -> Path:
    """Return Corral's per-user config directory (``$XDG_CONFIG_HOME/corral``)."""
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "corral"


def plugins_dir(root: Path | str | None = None) -> Path:
    """Return the plugin installation root (outside Homebrew/site-packages)."""
    return Path(root).expanduser() if root is not None else config_home() / "plugins"


def state_dir(root: Path | str | None = None) -> Path:
    """Return the shared state-contracts directory (``~/.config/corral/state``)."""
    return Path(root).expanduser() if root is not None else config_home() / "state"


def ok_envelope(
    data: Any,
    *,
    version: int = AGENT_API_VERSION,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standard ``{ok, data, error, meta}`` success envelope."""
    merged_meta: dict[str, Any] = {"version": version}
    if meta:
        merged_meta.update(meta)
    return {
        "ok": True,
        "data": data,
        "error": None,
        "meta": merged_meta,
    }


def error_envelope(
    code: str,
    message: str,
    *,
    hint: str | None = None,
    next_commands: list[str] | None = None,
    details: Any = None,
    version: int = AGENT_API_VERSION,
) -> dict[str, Any]:
    """Build a standard ``{ok, data, error, meta}`` failure envelope."""
    err: dict[str, Any] = {
        "code": code,
        "message": message,
        "hint": hint,
        "next_commands": list(next_commands or []),
    }
    if details is not None:
        err["details"] = details
    return {
        "ok": False,
        "data": None,
        "error": err,
        "meta": {"version": version},
    }


def tsv_cell(value: Any) -> str:
    """Sanitize one TSV field so rows remain strictly single-line."""
    return str(value if value is not None else "").replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()
