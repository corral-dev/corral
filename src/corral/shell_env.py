"""Resolve and merge user shell PATH entries for GUI and launchd host processes."""

from __future__ import annotations

import functools
import os
import pwd
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_SENTINEL_BEGIN = "__CORRAL_PATH_BEGIN__"
_SENTINEL_END = "__CORRAL_PATH_END__"
_SYSTEM_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
_SKIP_PROBE_VALUES = frozenset({"1", "true", "yes", "on"})


def _normalize_dir(entry: str) -> str:
    stripped = entry.strip()
    if not stripped:
        return ""
    if len(stripped) > 1:
        return stripped.rstrip("/")
    return stripped


def is_minimal_path(path_value: str, home: Path | None = None) -> bool:
    """Return True when PATH contains only system or basic static install bins."""
    h = home or Path.home()
    minimal = {
        *( _normalize_dir(p) for p in _SYSTEM_DIRS ),
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/local/sbin",
        _normalize_dir(str(h / ".local" / "bin")),
    }
    parts = [_normalize_dir(part) for part in path_value.split(":") if part.strip()]
    if not parts:
        return True
    return all(part in minimal for part in parts)


def parse_sentinel_path(output: str) -> str | None:
    """Extract the last sentinel-wrapped PATH string, ignoring shell startup noise."""
    start = output.rfind(_SENTINEL_BEGIN)
    if start < 0:
        return None
    after_start = start + len(_SENTINEL_BEGIN)
    end = output.find(_SENTINEL_END, after_start)
    if end < 0:
        return None
    extracted = output[after_start:end].strip()
    return extracted or None


def user_login_shell() -> str:
    """Return the account's executable login shell."""
    try:
        pw_shell = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        pw_shell = ""
    if pw_shell and os.access(pw_shell, os.X_OK):
        return pw_shell
    env_shell = os.environ.get("SHELL", "")
    if env_shell and os.access(env_shell, os.X_OK):
        return env_shell
    return "/bin/zsh" if sys.platform == "darwin" else "/bin/sh"


def probe_command(shell_path: str) -> str:
    """Build a shell snippet that prints PATH wrapped in sentinels."""
    if os.path.basename(shell_path) == "fish":
        return f"printf '{_SENTINEL_BEGIN}%s{_SENTINEL_END}' (string join : $PATH)"
    return f'printf "{_SENTINEL_BEGIN}%s{_SENTINEL_END}" "$PATH"'


def _minimal_probe_env() -> dict[str, str]:
    env: dict[str, str] = {
        "HOME": str(Path.home()),
        "PATH": ":".join(_SYSTEM_DIRS),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "CORRAL_PATH_PROBE": "1",
    }
    for key in ("USER", "LOGNAME", "LANG"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


@functools.lru_cache(maxsize=1)
def probe_login_shell_path(shell: str | None = None) -> str:
    """Query the user's login shell for its configured PATH once per process."""
    skip = os.environ.get("CORRAL_SKIP_SHELL_PATH_PROBE", "").strip().lower()
    if skip in _SKIP_PROBE_VALUES:
        return ""
    shell_path = shell or user_login_shell()
    cmd = probe_command(shell_path)
    base_env = _minimal_probe_env()
    for flags, timeout in ((["-l", "-i", "-c"], 3.0), (["-l", "-c"], 2.0)):
        try:
            proc = subprocess.run(
                [shell_path, *flags, cmd],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=base_env,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            continue
        if proc.returncode == 0:
            parsed = parse_sentinel_path(proc.stdout or "")
            if parsed:
                return parsed
    return ""


def _semver_key(text: str) -> tuple[int, ...]:
    nums = [int(tok) for tok in re.findall(r"\d+", text)]
    return tuple(nums)


def _matching_subdirs(base: Path, pattern: str, *, suffix: str = "bin") -> list[str]:
    if not base.is_dir():
        return []
    matches = [p for p in base.glob(pattern) if p.is_dir()]
    matches.sort(key=lambda p: (_semver_key(p.name), p.name), reverse=True)
    results: list[str] = []
    for item in matches:
        candidate = item / suffix if suffix else item
        if candidate.is_dir():
            results.append(str(candidate))
    return results


def discover_common_bin_dirs(home: Path | None = None) -> list[str]:
    """Return existing tool-manager bin directories followed by standard system bins."""
    h = home or Path.home()
    found: list[str] = []

    def add_if_dir(path: Path) -> None:
        if path.is_dir():
            found.append(str(path))

    add_if_dir(h / ".local" / "share" / "nodejs" / "current" / "bin")
    found.extend(_matching_subdirs(h / ".local" / "share" / "nodejs", "current*"))
    found.extend(_matching_subdirs(h / ".nvm" / "versions" / "node", "*"))

    optional_dirs = (
        h / ".volta" / "bin",
        h / ".fnm" / "aliases" / "default" / "bin",
        h / "Library" / "Application Support" / "fnm" / "aliases" / "default" / "bin",
        h / ".local" / "share" / "fnm" / "aliases" / "default" / "bin",
        h / ".local" / "share" / "mise" / "shims",
        h / ".asdf" / "shims",
        h / ".nodenv" / "shims",
        h / ".npm-global" / "bin",
        h / "Library" / "pnpm",
        h / ".local" / "share" / "pnpm",
        h / ".bun" / "bin",
        h / ".claude" / "local" / "bin",
        h / ".claude" / "local",
        h / ".opencode" / "bin",
        h / ".antigravity" / "antigravity" / "bin",
        h / ".cargo" / "bin",
    )
    for directory in optional_dirs:
        add_if_dir(directory)

    found.extend(
        [
            str(h / ".local" / "bin"),
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/usr/local/sbin",
            *_SYSTEM_DIRS,
        ]
    )
    return found


def merge_path_entries(*sources: str | Sequence[str]) -> str:
    """Merge PATH strings or sequences, keeping first-seen order without duplicates."""
    parts: list[str] = []
    seen: set[str] = set()
    for source in sources:
        items = source.split(":") if isinstance(source, str) else source
        for raw in items:
            item = raw.strip()
            if item and item not in seen:
                seen.add(item)
                parts.append(item)
    return ":".join(parts)


def resolve_user_path(
    *,
    current_path: str | None = None,
    preserved_path: str = "",
    home: Path | None = None,
    include_shell: bool = True,
) -> str:
    """Resolve an enriched PATH merging shell, preserved unit, current, and common dirs."""
    cur = os.environ.get("PATH", "") if current_path is None else current_path
    h = home or Path.home()
    shell_path = probe_login_shell_path() if include_shell else ""
    common = discover_common_bin_dirs(h)
    if is_minimal_path(cur, home=h):
        return merge_path_entries(shell_path, preserved_path, cur, common)
    return merge_path_entries(cur, shell_path, preserved_path, common)


def enrich_process_path(*, preserved_path: str = "", include_shell: bool = True) -> str:
    """Update ``os.environ['PATH']`` in place and return the resolved PATH."""
    resolved = resolve_user_path(
        current_path=os.environ.get("PATH", ""),
        preserved_path=preserved_path,
        include_shell=include_shell,
    )
    os.environ["PATH"] = resolved
    return resolved
