"""Project shells for the desktop and phone clients' project terminal.

A project shell is the user's login shell started in a project folder inside a
detached tmux session on the keepalive server. It outlives client
disconnects, app restarts and remote-service restarts; only ``exit``, an
explicit close or a reboot ends it. History is the shell's own.

Clients reach a shell with the desktop terminal stream (`terminal_stream.py`)
under the key ``shell:<id>``. Unlike agent panes, a shell has no TUI viewer,
so its grid follows the latest active viewer (attach, resize or typing), which
is tmux's own default policy; see `ShellTerminalStream`.

Names use ``SHELL_SESSION_PREFIX`` (``corralsh-``), which every agent path
ignores: scanning, hosted-card adoption, idle reclaim and pressure reaping
only match ``corral-`` / legacy prefixes. Metadata lives on the tmux session
itself (``@corral_project``), so there is no state file to drift.

Design: apple/docs/design/PROJECT_TERMINAL_DESIGN.md.
"""

from __future__ import annotations

import os
import pwd
import re
import secrets
import shutil
import subprocess
import threading
from pathlib import Path

from corral import embed, keepalive
from corral.legacy_names import SHELL_SESSION_PREFIX
from corral.remote.terminal_stream import TerminalStream

KEY_PREFIX = "shell:"
MAX_SHELLS = 16
_ID = re.compile(r"[0-9a-f]{8}")
_TIMEOUT = 3.0
# Unit / record separators: folder names may contain tabs and newlines.
_SEP = "\x1f"
_END = "\x1e"
_LIST_FORMAT = _SEP.join((
    "#{session_name}", "#{session_created}", "#{@corral_project}",
    "#{pane_current_path}", "#{pane_current_command}",
)) + _END


class ShellError(Exception):
    """``code`` is ``folder_missing``, ``limit``, ``ended`` or ``start_failed``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def is_shell_key(key: str) -> bool:
    return str(key or "").startswith(KEY_PREFIX)


def name_for_key(key: str) -> str:
    """tmux name for ``shell:<id>``; empty for anything that is not a shell id."""
    ident = str(key or "")[len(KEY_PREFIX):]
    if not is_shell_key(key) or not _ID.fullmatch(ident):
        return ""
    return f"{SHELL_SESSION_PREFIX}{ident}"


def key_for_name(name: str) -> str:
    return f"{KEY_PREFIX}{name[len(SHELL_SESSION_PREFIX):]}"


def login_shell() -> str:
    """The account's login shell (not ``$SHELL`` of whoever started the service)."""
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        shell = ""
    if shell and os.access(shell, os.X_OK):
        return shell
    return os.environ.get("SHELL") or "/bin/sh"


def _tmux(*args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            [*keepalive.tmux_argv(f"{SHELL_SESSION_PREFIX}x"), *args],
            capture_output=True, timeout=_TIMEOUT, check=False, env=keepalive.tmux_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def list_shells() -> list[dict]:
    """Every live project shell, oldest first."""
    if shutil.which("tmux") is None:
        return []
    proc = _tmux("list-sessions", "-F", _LIST_FORMAT)
    if proc is None or proc.returncode != 0:
        return []
    shells = []
    for record in proc.stdout.decode("utf-8", "replace").split(_END):
        parts = record.lstrip("\n").split(_SEP)
        if len(parts) != 5 or not parts[0].startswith(SHELL_SESSION_PREFIX):
            continue
        name, created, project, cwd, command = parts
        if not name_for_key(key_for_name(name)):
            continue
        shells.append(_entry(name, created, project, cwd, command))
    shells.sort(key=lambda shell: (shell["created"], shell["key"]))
    return shells


def _entry(name: str, created: str, project: str, cwd: str, command: str) -> dict:
    project = project or cwd
    command = command.lstrip("-")
    return {
        "key": key_for_name(name),
        "project": project,
        "name": Path(project).name or project,
        "cwd": cwd or project,
        "command": command,
        # A program other than the shell runs in the foreground: closing asks first.
        "busy": bool(command) and command != Path(login_shell()).name,
        "created": int(created) if created.isdigit() else 0,
    }


_open_lock = threading.Lock()


def open_shell(cwd: str, cols: int, rows: int) -> dict:
    """Start a login shell in ``cwd`` (home when empty) and return its entry."""
    folder = Path(os.path.expanduser(cwd.strip() or "~"))
    if not folder.is_dir():
        raise ShellError("folder_missing")
    folder = folder.resolve()
    with _open_lock:
        if len(list_shells()) >= MAX_SHELLS:
            raise ShellError("limit")
        keepalive.ensure_server()
        name = f"{SHELL_SESSION_PREFIX}{secrets.token_hex(4)}"
        width, height = embed.normalize_host_size(cols, rows)
        argv = [
            "-f", keepalive.ensure_config_file(), "new-session", "-d", "-s", name,
            "-x", str(width), "-y", str(height), "-c", str(folder),
            "-e", "CORRAL_PROJECT_SHELL=1", *_locale_env(),
            "--", login_shell(), "-l",
        ]
        proc = _tmux(*argv)
        if proc is None or proc.returncode != 0:
            detail = (proc.stderr.decode("utf-8", "replace").strip() if proc else "") or "tmux"
            raise ShellError("start_failed", detail)
        _tmux("set-option", "-t", name, "@corral_project", str(folder))
    embed.note_alive(name)
    for shell in list_shells():
        if shell["key"] == key_for_name(name):
            return shell
    return _entry(name, "0", str(folder), str(folder), Path(login_shell()).name)


def _locale_env() -> list[str]:
    """A service started by launchd may carry no locale; shells need UTF-8 for CJK."""
    if any(os.environ.get(var) for var in ("LC_ALL", "LC_CTYPE", "LANG")):
        return []
    return ["-e", "LANG=en_US.UTF-8"]


def close_shell(key: str) -> bool:
    name = name_for_key(key)
    if not name:
        return False
    embed.close_channel(name)
    return keepalive.kill(name)


def alive(name: str) -> bool:
    proc = _tmux("has-session", "-t", f"={name}")
    return proc is not None and proc.returncode == 0


class ShellTerminalStream(TerminalStream):
    """A shell's stream: the grid is the latest active viewer's, not the widest.

    Attaching, resizing and typing make a viewer active (tmux ``window-size
    latest``); when the active viewer leaves, the most recent remaining one
    takes over. Shells are absent from the TUI's viewer registry.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._active: str | None = None

    def vote(self, viewer: str, cols: int, rows: int) -> tuple[int, int]:
        size = (max(1, int(cols)), max(1, int(rows)))
        with self._votes_lock:
            self._votes.pop(viewer, None)
            self._votes[viewer] = size
            self._active = viewer
        self._apply_size(size)
        return size

    def activate(self, viewer: str) -> None:
        with self._votes_lock:
            if viewer == self._active or viewer not in self._votes:
                return
            self._active = viewer
            size = self._votes[viewer]
        self._apply_size(size)

    def withdraw(self, viewer: str) -> None:
        with self._votes_lock:
            self._votes.pop(viewer, None)
            if self._active == viewer:
                self._active = next(reversed(self._votes), None)
            size = self._votes.get(self._active) if self._active else None
        if size is not None:
            self._apply_size(size)

    def _effective_size(self, votes: dict[str, tuple[int, int]]) -> tuple[int, int] | None:
        return votes.get(self._active) if self._active else None

    def _release_views(self, viewers: list[str]) -> None:
        pass
