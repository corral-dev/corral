"""Start the keepalive tmux server with app-class scheduling on macOS.

The tmux server outlives whoever started it, and every hosted agent inherits
the server's scheduling class. A server first started from a launchd job (a
web terminal, the phone remote daemon) carries launchd's default clamp:
throttled CPU and low-priority I/O. Measured under load, a busy loop inside
such a server got 14% CPU versus 48-64% from an iTerm shell. Userland QoS calls
cannot lift that clamp, so the server is started as its own launchd job with
``ProcessType=Interactive`` and ``tmux -D`` (foreground: the job *is* the
server). Contract: ``docs/MAINTAINER_GUIDE.md`` "Keepalive server scheduling
class".

Everything here is best effort: any failure returns and the caller's
``new-session`` starts a self-daemonizing server exactly as before.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import plistlib
import shutil
import socket as socket_mod
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

from corral.legacy_names import SOCKET_NAME, cache_dir, env_is_disabled

LABEL_PREFIX = "com.x0c.corral.keepalive"
_START_TIMEOUT = 3.0
_CALL_TIMEOUT = 2.0
# Only these go into the plist on disk; everything else (tokens, proxies) is
# seeded in memory with set-environment after the server is up.
_PLIST_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMUX_TMPDIR", "USER", "SHELL")
_NOT_SEEDED = frozenset({"TMUX", "TMUX_PANE", "TERM", "TERM_PROGRAM", "_", "SHLVL", "PWD", "OLDPWD"})


def label(socket: str) -> str:
    return f"{LABEL_PREFIX}.{socket}"


def applies(socket: str) -> bool:
    """Only the product socket on macOS; never for tests or when opted out."""
    return (
        sys.platform == "darwin"
        and socket == SOCKET_NAME
        and os.environ.get("CORRAL_ISOLATE_MANAGED_HOSTS") != "1"
        and not env_is_disabled("KEEPALIVE_LAUNCHD")
        and shutil.which("launchctl") is not None
    )


def socket_path(socket: str, env: Mapping[str, str]) -> Path:
    base = env.get("TMUX_TMPDIR") or "/tmp"
    return Path(base) / f"tmux-{os.getuid()}" / socket


def server_running(socket: str, env: Mapping[str, str]) -> bool:
    """A connectable socket means a live server; a stale file does not."""
    path = socket_path(socket, env)
    probe = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def ensure_server(socket: str, config_path: str, env: Mapping[str, str]) -> bool:
    """Make sure the server runs as an Interactive launchd job.

    Returns True when this call started it. Never raises.
    """
    try:
        if not applies(socket) or server_running(socket, env):
            return False
        with _start_lock(socket):
            if server_running(socket, env):
                return False
            if not _bootstrap(socket, config_path, env) or not _wait_running(socket, env):
                return False
            _seed_environment(socket, env)
            return True
    except Exception:  # noqa: BLE001 - fall back to tmux's own server start
        return False


def plist_payload(socket: str, config_path: str, env: Mapping[str, str], tmux: str) -> dict:
    job_env = {key: env[key] for key in _PLIST_ENV_KEYS if env.get(key)}
    log = str(cache_dir() / "keepalive-server.log")
    return {
        "Label": label(socket),
        "ProgramArguments": [tmux, "-D", "-L", socket, "-f", config_path],
        "EnvironmentVariables": job_env,
        "ProcessType": "Interactive",
        "RunAtLoad": True,
        "KeepAlive": False,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _bootstrap(socket: str, config_path: str, env: Mapping[str, str]) -> bool:
    tmux = shutil.which("tmux", path=env.get("PATH"))
    if tmux is None:
        return False
    directory = cache_dir() / "launchd"
    directory.mkdir(parents=True, exist_ok=True)
    plist = directory / f"{label(socket)}.plist"
    tmp = plist.with_suffix(".tmp")
    with open(tmp, "wb") as fh:
        plistlib.dump(plist_payload(socket, config_path, env, tmux), fh)
    os.replace(tmp, plist)
    domain = f"gui/{os.getuid()}"
    # A loaded job whose server has exited must be unloaded before bootstrap.
    _launchctl("bootout", f"{domain}/{label(socket)}")
    return _launchctl("bootstrap", domain, str(plist))


def _launchctl(*args: str) -> bool:
    try:
        proc = subprocess.run(
            ["launchctl", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=_CALL_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _wait_running(socket: str, env: Mapping[str, str]) -> bool:
    deadline = time.monotonic() + _START_TIMEOUT
    while time.monotonic() < deadline:
        if server_running(socket, env):
            return True
        time.sleep(0.05)
    return False


def seed_commands(env: Mapping[str, str]) -> list[str]:
    """One tmux invocation: ``set-environment -g K V ; set-environment …``."""
    args: list[str] = []
    for key, value in sorted(env.items()):
        if key in _NOT_SEEDED or not key or "=" in key:
            continue
        if args:
            args.append(";")
        args += ["set-environment", "-g", key, value]
    return args


def _seed_environment(socket: str, env: Mapping[str, str]) -> None:
    args = seed_commands(env)
    if not args:
        return
    tmux = shutil.which("tmux", path=env.get("PATH")) or "tmux"
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            [tmux, "-L", socket, *args], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=_CALL_TIMEOUT, check=False, env=dict(env),
        )


@contextlib.contextmanager
def _start_lock(socket: str) -> Iterator[None]:
    """Serialize starts across processes (TUI windows, remote daemon)."""
    directory = cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f"keepalive-server-{socket}.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ------------------------------------------------------------------ diagnosis


def server_pid(socket: str, env: Mapping[str, str]) -> int | None:
    if not server_running(socket, env):
        return None
    try:
        out = subprocess.run(
            ["tmux", "-L", socket, "display-message", "-p", "#{pid}"],
            capture_output=True, text=True, timeout=_CALL_TIMEOUT, check=False, env=dict(env),
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return int(out) if out.isdigit() else None


def scheduling_priority(pid: int) -> int | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "pri=", "-p", str(pid)], capture_output=True, text=True,
            timeout=_CALL_TIMEOUT, check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return int(out) if out.isdigit() else None


def server_report(socket: str, env: Mapping[str, str]) -> dict:
    """For ``corral diagnose``: is the running server app-class or clamped?"""
    pid = server_pid(socket, env)
    if pid is None:
        return {"running": False}
    priority = scheduling_priority(pid)
    launched = _launchctl("print", f"gui/{os.getuid()}/{label(socket)}")
    return {
        "running": True,
        "pid": pid,
        "priority": priority,
        "interactive_job": launched,
        # launchd-default and background jobs sit at 20 or lower; apps at 31.
        "clamped": sys.platform == "darwin" and priority is not None and priority < 31,
    }
