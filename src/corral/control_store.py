"""Durable delegation runs: thin host wrapper over the shared operation store.

Generic SQLite mechanics (schema, same-transaction audit, immutable
idempotency) live in :mod:`agent_session_control.store`; this module only
pins Corral's historical identity: the exact on-disk path, the actor/marker
namespace and the supervisor name. There is no automatic migration: the file
stays ``<root>/runs.sqlite3`` because the wrapper selects the library's
default-file namespace explicitly (``namespace="corral"`` would silently
open ``runs.corral.sqlite3``). Reads never create or repair storage.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from agent_session_control.errors import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_TIMEOUT,
    EXIT_USAGE,
    ControlError,
)
from agent_session_control.store import (
    MAX_ACTIVE_RUNS,
    SCHEMA_VERSION,
    SETTLED,
    TERMINAL,
    digest,
    run_version,
)
from agent_session_control.store import (
    RunStore as _SharedRunStore,
)

__all__ = [
    "CONTROL_DIRNAME",
    "DEFAULT_ACTOR",
    "EXIT_CONFLICT",
    "EXIT_ERROR",
    "EXIT_NOT_FOUND",
    "EXIT_TIMEOUT",
    "EXIT_USAGE",
    "MAX_ACTIVE_RUNS",
    "NAMESPACE",
    "SCHEMA_VERSION",
    "SETTLED",
    "STORE_NAMESPACE",
    "SUPERVISOR",
    "TERMINAL",
    "ControlError",
    "RunStore",
    "control_home",
    "digest",
    "library_store_config",
    "run_version",
    "store_root",
]

TERMINAL = TERMINAL
SETTLED = SETTLED
SUPERVISOR = "corral-supervisor"

#: Actor/marker namespace Corral injects into the shared library. The
#: library's neutral default mentions no downstream product; this selects
#: the legacy Corral identity. It is independent of the database filename.
NAMESPACE = "corral"
#: Historical on-disk location (``$XDG_CONFIG_HOME/corral/control``).
CONTROL_DIRNAME = "control"
#: Default actor when the calling agent exports no hosted identity.
DEFAULT_ACTOR = "local-user"
#: Library file-selection namespace that keeps the historical ``runs.sqlite3``
#: filename. Never set this to :data:`NAMESPACE`: that would silently open a
#: different database and strand existing user state.
STORE_NAMESPACE = "default"


def control_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "corral" / CONTROL_DIRNAME


def store_root(override: Path | str | None = None) -> Path:
    """Explicit store-root injection for the shared library.

    Returns ``override`` unchanged when given; otherwise the historical
    Corral root. The library never infers this from host-prefixed
    environment variables itself.
    """
    return Path(override) if override is not None else control_home()


def library_store_config(
    root: Path | str | None = None, *, namespace: str = NAMESPACE, supervisor: str = SUPERVISOR
) -> dict:
    """Config mapping documenting how Corral constructs the shared store.

    The file-selection namespace is always :data:`STORE_NAMESPACE` (the
    historical ``runs.sqlite3``); ``namespace`` names the actor/marker
    identity only. Pure: never creates storage.
    """
    return {
        "root": store_root(root),
        "namespace": namespace,
        "store_namespace": STORE_NAMESPACE,
        "supervisor": supervisor,
        "schema_version": SCHEMA_VERSION,
        "settled_states": sorted(SETTLED),
        "max_active_runs": MAX_ACTIVE_RUNS,
    }


class RunStore(_SharedRunStore):
    """Corral's operation store: the shared implementation, Corral's identity.

    Same file, same schema, same audit/actor values, same error codes as the
    historical implementation. Only the two legacy error codes the shared
    layer generalizes (``unsafe_storage``, ``store_too_new``) are mapped back
    here so existing readers observe byte-identical failures.
    """

    def __init__(self, root: Path | str | None = None):
        super().__init__(store_root(root), namespace=STORE_NAMESPACE, supervisor=SUPERVISOR)

    @contextmanager
    def connection(self, *, write: bool = False):
        if write:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.is_symlink():
                raise ControlError("unsafe_storage", "The control database must not be a symlink.", EXIT_CONFLICT)
            if self.path.is_file():
                probe = sqlite3.connect(self.path)
                try:
                    version = probe.execute("PRAGMA user_version").fetchone()[0]
                finally:
                    probe.close()
                if version > SCHEMA_VERSION:
                    raise ControlError(
                        "store_too_new", "Run storage was written by a newer Corral; upgrade first.", EXIT_CONFLICT
                    )
        with super().connection(write=write) as conn:
            yield conn
