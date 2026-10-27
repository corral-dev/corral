"""Local recent assistant/project preferences for the TUI New Session dialog."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from corral.legacy_names import getenv
from corral.runtime.registry import ACTIVE_RUNTIME_IDS

MAX_COMBOS = 100


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


def _read(path: Path) -> tuple[list[RecentStart], set[tuple[str, str]]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return [], set()
    if not isinstance(data, list):
        return [], set()
    combos = []
    seen = set()
    dismissed = set()
    for row in data:
        if not isinstance(row, dict):
            continue
        runtime, cwd, label = (row.get(key) for key in ("runtime_id", "cwd", "project_label"))
        if (not isinstance(runtime, str) or runtime not in ACTIVE_RUNTIME_IDS
                or not isinstance(cwd, str) or not Path(cwd).is_absolute()):
            continue
        if row.get("removed") is True:
            dismissed.add((runtime, cwd))
            continue
        if not isinstance(label, str) or not label.strip() or (runtime, cwd) in seen:
            continue
        seen.add((runtime, cwd))
        combos.append(RecentStart(runtime, cwd, label))
    return combos[:MAX_COMBOS], dismissed


def load(candidates: Iterable[RecentStart] = ()) -> list[RecentStart]:
    """Merge remembered starts with normalized history without reviving removals."""
    combos, dismissed = _read(_path())
    result = []
    seen = set(dismissed)
    for item in (*combos, *candidates):
        key = (item.runtime_id, item.cwd)
        if (key in seen or item.runtime_id not in ACTIVE_RUNTIME_IDS
                or not Path(item.cwd).is_absolute() or not item.project_label.strip()):
            continue
        seen.add(key)
        result.append(item)
        if len(result) == MAX_COMBOS:
            break
    return result


def _update(combo: RecentStart, *, remove: bool = False) -> None:
    path = _path()
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            existing, dismissed = _read(path)
            key = (combo.runtime_id, combo.cwd)
            combos = [item for item in existing if (item.runtime_id, item.cwd) != key]
            if remove:
                dismissed.add(key)
            else:
                dismissed.discard(key)
                combos.insert(0, combo)
            with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                data = [asdict(item) for item in combos[:MAX_COMBOS]]
                data.extend({"runtime_id": runtime, "cwd": cwd, "removed": True}
                            for runtime, cwd in sorted(dismissed))
                json.dump(data, stream)
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
