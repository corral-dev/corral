"""Remember the remote on/off switch across login and reboot.

``corral remote on`` arms OS autostart (macOS LaunchAgent / Linux systemd --user).
``corral remote off`` disarms it. The wanted flag in ``remote.json`` is the source of
truth; the unit files only exist while wanted is true.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from corral.legacy_names import env_is_set

_LABEL = "com.x0c.corral.remote"
_SYSTEMD_UNIT = "corral-remote.service"
# Phone requests are user-facing: the user waits on the phone for list /
# detail / input echo, so the daemon runs unthrottled like an app. Adaptive
# is not an option: launchd only lifts Adaptive jobs on XPC-connection
# activity, and this daemon serves over websockets/relay with no XPC, so it
# would stay Background-clamped. Same call as the keepalive server job
# (tmux_server.py). Background scanning still yields via
# schedprio.demote_background() (thread QoS + nice), which the process class
# does not override.
_PROCESS_TYPE = "Interactive"


def autostart_allowed() -> bool:
    """Skip real OS registration under test / explicit state overrides."""
    flag = os.environ.get("CORRAL_NO_AUTOSTART", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return False
    if env_is_set("STATE_DIR") or env_is_set("CACHE_DIR"):
        return False
    return True


def serve_argv() -> list[str]:
    return [sys.executable, "-m", "corral", "remote", "_serve"]


def is_installed() -> bool:
    if not autostart_allowed():
        return False
    if sys.platform == "darwin":
        return _darwin_plist_path().is_file() and _darwin_plist_is_fresh()
    if sys.platform.startswith("linux"):
        return _linux_unit_path().is_file()
    return False


def enable() -> str:
    """Arm autostart and ensure the service is loaded. Empty string on success."""
    if not autostart_allowed():
        return ""
    if sys.platform == "darwin":
        return _darwin_enable()
    if sys.platform.startswith("linux"):
        return _linux_enable()
    return ""


def disable() -> str:
    """Disarm autostart so reboot / KeepAlive cannot bring the service back."""
    if not autostart_allowed():
        return ""
    if sys.platform == "darwin":
        return _darwin_disable()
    if sys.platform.startswith("linux"):
        return _linux_disable()
    return ""


def _home() -> Path:
    return Path.home()


def _log_dir() -> Path:
    path = _home() / "Library" / "Logs" / "corral" if sys.platform == "darwin" else (
        Path(os.environ.get("XDG_STATE_HOME") or (_home() / ".local" / "state")) / "corral" / "log"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path_env() -> str:
    extras = [
        str(_home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    current = os.environ.get("PATH", "")
    parts = []
    seen: set[str] = set()
    for item in extras + current.split(":"):
        if item and item not in seen:
            seen.add(item)
            parts.append(item)
    return ":".join(parts)


# ---------------------------------------------------------------------------
# macOS LaunchAgent
# ---------------------------------------------------------------------------

def _darwin_plist_path() -> Path:
    return _home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _darwin_plist_is_fresh() -> bool:
    """A pre-ProcessType plist is stale: report not-installed so status nudges
    a re-`on`, whose enable() rewrites the plist and rebootstraps the job.
    Unreadable files keep the old answer (installed) — never break the
    switch display over a hand-edited plist."""
    try:
        with _darwin_plist_path().open("rb") as fh:
            payload = plistlib.load(fh)
    except Exception:  # noqa: BLE001 - unreadable plist keeps the old answer
        return True
    if not isinstance(payload, dict):
        return True
    return payload.get("ProcessType", "Standard") == _PROCESS_TYPE


def _darwin_domain() -> str:
    return f"gui/{os.getuid()}"


def _darwin_write_plist() -> Path:
    agents = _darwin_plist_path().parent
    agents.mkdir(parents=True, exist_ok=True)
    logs = _log_dir()
    payload = {
        "Label": _LABEL,
        "ProgramArguments": serve_argv(),
        "ProcessType": _PROCESS_TYPE,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "WorkingDirectory": str(_home()),
        "StandardOutPath": str(logs / "remote.out.log"),
        "StandardErrorPath": str(logs / "remote.err.log"),
        "EnvironmentVariables": {
            "HOME": str(_home()),
            "PATH": _path_env(),
            "LANG": os.environ.get("LANG") or "en_US.UTF-8",
        },
    }
    path = _darwin_plist_path()
    with path.open("wb") as fh:
        plistlib.dump(payload, fh, sort_keys=False)
    try:
        path.chmod(0o644)
    except OSError:
        pass
    return path


def _darwin_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _darwin_enable() -> str:
    path = _darwin_write_plist()
    domain = _darwin_domain()
    target = f"{domain}/{_LABEL}"
    # Replace any prior registration so the plist on disk is what runs.
    _darwin_run(["launchctl", "bootout", target])
    # bootout returns before the job finishes unloading; bootstrapping too
    # soon fails and (on the already-running `on` path, where the caller
    # ignores our return) leaves no job loaded at all.
    _wait_for_unload(target)
    boot = _darwin_run(["launchctl", "bootstrap", domain, str(path)])
    if boot.returncode != 0:
        time.sleep(2.0)
        boot = _darwin_run(["launchctl", "bootstrap", domain, str(path)])
    if boot.returncode != 0:
        # Older macOS / non-gui sessions: fall back to load.
        loaded = _darwin_run(["launchctl", "load", "-w", str(path)])
        if loaded.returncode != 0:
            detail = (boot.stderr or boot.stdout or loaded.stderr or loaded.stdout or "").strip()
            return detail or "launchctl bootstrap failed"
    # Start if needed; do not -k (that would bounce an already-healthy instance).
    kicked = _darwin_run(["launchctl", "kickstart", target])
    if kicked.returncode != 0:
        # Some domains reject kickstart when RunAtLoad already started the job.
        pass
    return ""


def _wait_for_unload(target: str, timeout: float = 5.0) -> None:
    """Poll until bootout's teardown is visible; never raises or blocks long."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = _darwin_run(["launchctl", "print", target])
        if probe.returncode != 0:
            return
        time.sleep(0.2)


def _darwin_disable() -> str:
    domain = _darwin_domain()
    target = f"{domain}/{_LABEL}"
    path = _darwin_plist_path()
    _darwin_run(["launchctl", "bootout", target])
    if path.is_file():
        _darwin_run(["launchctl", "unload", "-w", str(path)])
        try:
            path.unlink()
        except OSError as exc:
            return str(exc)
    return ""


# ---------------------------------------------------------------------------
# Linux systemd --user
# ---------------------------------------------------------------------------

def _linux_unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (_home() / ".config"))
    return base / "systemd" / "user" / _SYSTEMD_UNIT


def _linux_write_unit() -> Path:
    path = _linux_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    logs = _log_dir()
    argv = " ".join(_shell_quote(part) for part in serve_argv())
    body = "\n".join(
        [
            "[Unit]",
            "Description=Corral phone handoff",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={argv}",
            "Restart=on-failure",
            "RestartSec=5",
            f"Environment=HOME={_shell_quote(str(_home()))}",
            f"Environment=PATH={_shell_quote(_path_env())}",
            f"StandardOutput=append:{logs / 'remote.out.log'}",
            f"StandardError=append:{logs / 'remote.err.log'}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )
    path.write_text(body, encoding="utf-8")
    try:
        path.chmod(0o644)
    except OSError:
        pass
    return path


def _shell_quote(value: str) -> str:
    if not value:
        return "''"
    if all(ch.isalnum() or ch in "/._-:" for ch in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _linux_systemctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _linux_enable() -> str:
    if shutil.which("systemctl") is None:
        return ""
    _linux_write_unit()
    reloaded = _linux_systemctl(["daemon-reload"])
    if reloaded.returncode != 0:
        return (reloaded.stderr or reloaded.stdout or "daemon-reload failed").strip()
    enabled = _linux_systemctl(["enable", "--now", _SYSTEMD_UNIT])
    if enabled.returncode != 0:
        return (enabled.stderr or enabled.stdout or "systemctl enable failed").strip()
    return ""


def _linux_disable() -> str:
    if shutil.which("systemctl") is None:
        path = _linux_unit_path()
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            return str(exc)
        return ""
    _linux_systemctl(["disable", "--now", _SYSTEMD_UNIT])
    path = _linux_unit_path()
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return str(exc)
    _linux_systemctl(["daemon-reload"])
    return ""
