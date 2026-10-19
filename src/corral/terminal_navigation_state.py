"""Cross-process input ownership and frame epochs for native prompt navigation."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


def _path(name: str, suffix: str) -> Path:
    from corral.keepalive import tmux_argv_for_session

    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    key = hashlib.sha256(json.dumps([*tmux_argv_for_session(name), name]).encode()).hexdigest()
    return root / "corral" / "terminal-navigation" / (key + suffix)


@dataclass(frozen=True)
class NavigationFrame:
    revision: str = ""
    active: bool = False


def frame_state(name: str) -> NavigationFrame:
    try:
        data = json.loads(_path(name, ".json").read_text())
        active = bool(data["active"]) and 0 <= time.time() - data["started"] < data.get("lease_seconds", 10)
        if active:
            try:
                os.kill(data["pid"], 0)
            except ProcessLookupError:
                active = False
        return NavigationFrame(data["revision"], active)
    except (OSError, ValueError, KeyError, TypeError):
        return NavigationFrame()


def publish(name: str, *, active: bool, lease_seconds: float = 10.0) -> None:
    path = _path(name, ".json")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    try:
        payload = dict(revision=uuid.uuid4().hex, active=active, pid=os.getpid(), started=time.time())
        if lease_seconds != 10.0:
            payload["lease_seconds"] = lease_seconds
        temporary.write_text(json.dumps(payload))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def cancellation_revision(name: str) -> int:
    try:
        return _path(name, ".cancel").stat().st_mtime_ns
    except OSError:
        return 0


def cancel(name: str) -> None:
    try:
        if frame_state(name).active:
            _path(name, ".cancel").touch()
    except OSError:
        # Optional navigation metadata must never make ordinary input fail.
        return


class InputLock:
    """A reentrant thread lock with one advisory process lock at its outer edge."""

    def __init__(self, name: str):
        path = _path(name, ".lock")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        self._lock = threading.RLock()
        self._depth = 0

    def __enter__(self):
        self._lock.acquire()
        try:
            if self._depth == 0:
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            self._depth += 1
            return self
        except BaseException:
            self._lock.release()
            raise

    def __exit__(self, *_):
        self._depth -= 1
        if self._depth == 0:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._lock.release()

    def __del__(self):
        if hasattr(self, "_fd"):
            os.close(self._fd)
