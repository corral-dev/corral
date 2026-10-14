"""Local recent assistant/project preferences for the TUI New Session dialog."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from corral.legacy_names import getenv
from corral.runtime.registry import ACTIVE_RUNTIME_IDS

MAX_COMBOS = 5


@dataclass(frozen=True)
class RecentStart:
    runtime_id: str
    cwd: str
    project_label: str


def _path() -> Path:
    override = getenv("QUICK_START_PATH")
    if override:
        return Path(override).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "corral" / "recent-session-combos.json"


def _read(path: Path) -> list[RecentStart]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    combos = []
    seen = set()
    for row in data:
        if not isinstance(row, dict):
            continue
        runtime, cwd, label = (row.get(key) for key in ("runtime_id", "cwd", "project_label"))
        if (not isinstance(runtime, str) or runtime not in ACTIVE_RUNTIME_IDS
                or not isinstance(cwd, str) or not Path(cwd).is_absolute()):
            continue
        if not isinstance(label, str) or not label.strip() or (runtime, cwd) in seen:
            continue
        seen.add((runtime, cwd))
        combos.append(RecentStart(runtime, cwd, label))
    return combos[:MAX_COMBOS]


def load() -> list[RecentStart]:
    """Atomic snapshots allow lock-free reads, including while another TUI writes."""
    return _read(_path())


def _update(combo: RecentStart, *, remove: bool = False) -> None:
    path = _path()
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            combos = [item for item in _read(path)
                      if (item.runtime_id, item.cwd) != (combo.runtime_id, combo.cwd)]
            if not remove:
                combos.insert(0, combo)
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump([asdict(item) for item in combos[:MAX_COMBOS]], stream)
            os.replace(temporary, path)
    except OSError:
        # A preference write must not interrupt a successfully started session.
        pass
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)


def record(runtime_id: str, cwd: str) -> None:
    if runtime_id in ACTIVE_RUNTIME_IDS and cwd:
        path = Path(cwd).expanduser().resolve()
        _update(RecentStart(runtime_id, str(path), path.name or str(path)))


def remove(combo: RecentStart) -> None:
    _update(combo, remove=True)
