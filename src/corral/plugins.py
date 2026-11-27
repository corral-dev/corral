"""Stdlib-only external executable plugin runtime and ``corral plugin`` CLI.

Design authority: ``docs/design/PLUGIN_ARCHITECTURE_DESIGN.md``.
Plugins live under ``$XDG_CONFIG_HOME/corral/plugins/<name>/`` so upgrades to
Core via Homebrew, ``install.sh``, or ``uv`` never wipe user-installed plugins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from corral.cli_contract import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    config_home,
    corral_bin,
    error_envelope,
    ok_envelope,
    plugins_dir,
    state_dir,
    tsv_cell,
)
from corral.state_contracts import (
    acquire_host_lease,
    list_hidden_sessions,
    list_host_leases,
    register_hidden_session,
    release_host_lease,
    unregister_hidden_session,
)

SCHEMA_VERSION = 1
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_CMD_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

ACTIVE_RUNTIMES_CSV = "claude,codex,opencode,cursor,pi,antigravity"

RESERVED_COMMANDS: frozenset[str] = frozenset(
    {
        "list",
        "search",
        "show",
        "export",
        "share",
        "context",
        "plan",
        "describe",
        "diagnose",
        "cache",
        "remote",
        "login",
        "logout",
        "whoami",
        "shim",
        "observer",
        "tcc",
        "update",
        "skills",
        "plugin",
        "internal",
        "flex",
        "claude",
        "codex",
        "opencode",
        "cursor",
        "pi",
        "antigravity",
        "kimi",
    }
)

OFFICIAL_PLUGINS: dict[str, dict[str, Any]] = {
    "workflow": {
        "commands": ("workflow", "wf"),
        "description": "Multi-stage Python workflow orchestration, capability routing, and playbooks.",
        "repo_subdir": "plugins/workflow",
        "default_repo": "corral-dev/corral",
    },
}


class PluginError(Exception):
    """Structured plugin error rendered as an agent-friendly JSON envelope."""

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: int = EXIT_ERROR,
        *,
        hint: str | None = None,
        next_commands: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.hint = hint
        self.next_commands = list(next_commands or [])


def _core_version() -> str:
    try:
        from corral import __version__

        return str(__version__)
    except Exception:
        return "0.0.0"


def _atomic_write_text(dest: Path, text: str, *, mode: int = 0o600) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(dest.parent, 0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, dest)
    finally:
        tmp_path.unlink(missing_ok=True)


def _atomic_write_json(dest: Path, payload: Any, *, mode: int = 0o600) -> None:
    _atomic_write_text(dest, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", mode=mode)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_inside(plugin_dir: Path, rel_path: str, *, field_name: str) -> Path:
    if not isinstance(rel_path, str) or not rel_path.strip() or "\x00" in rel_path:
        raise PluginError("invalid_manifest", f"Invalid {field_name!r} in plugin manifest.", EXIT_USAGE)
    candidate = Path(rel_path)
    if candidate.is_absolute():
        raise PluginError("invalid_manifest", f"{field_name!r} must be a relative path inside plugin dir.", EXIT_USAGE)
    plugin_resolved = plugin_dir.resolve()
    resolved = (plugin_resolved / candidate).resolve()
    try:
        resolved.relative_to(plugin_resolved)
    except ValueError as exc:
        raise PluginError(
            "invalid_manifest",
            f"Path traversal rejected for {field_name!r}: {rel_path}",
            EXIT_USAGE,
        ) from exc
    return resolved


def _is_executable_safe(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    if st.st_mode & stat.S_IWOTH:
        return False
    return bool(st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def validate_manifest(data: Any, plugin_dir: Path, *, require_entrypoint: bool = True) -> dict[str, Any]:
    """Validate a ``plugin.json`` manifest and resolve safe paths."""
    if not isinstance(data, dict):
        raise PluginError("invalid_manifest", "plugin.json must be a JSON object.", EXIT_USAGE)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise PluginError(
            "invalid_manifest",
            f"Unsupported plugin schema_version {data.get('schema_version')!r}; expected {SCHEMA_VERSION}.",
            EXIT_USAGE,
        )
    name = data.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PluginError("invalid_manifest", f"Invalid plugin name {name!r}.", EXIT_USAGE)
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise PluginError("invalid_manifest", "Plugin version must be a non-empty string.", EXIT_USAGE)

    raw_cmds = data.get("commands")
    if not isinstance(raw_cmds, list) or not raw_cmds:
        raise PluginError("invalid_manifest", "Plugin commands must be a non-empty list.", EXIT_USAGE)
    commands: list[str] = []
    seen_cmds: set[str] = set()
    for cmd in raw_cmds:
        if not isinstance(cmd, str) or not _CMD_RE.match(cmd):
            raise PluginError("invalid_manifest", f"Invalid plugin command token {cmd!r}.", EXIT_USAGE)
        if cmd in RESERVED_COMMANDS:
            raise PluginError(
                "reserved_command",
                f"Plugin {name!r} cannot shadow reserved Core command {cmd!r}.",
                EXIT_CONFLICT,
            )
        if cmd not in seen_cmds:
            seen_cmds.add(cmd)
            commands.append(cmd)

    entrypoint_rel = data.get("entrypoint")
    entrypoint_path = _resolve_inside(plugin_dir, str(entrypoint_rel or ""), field_name="entrypoint")
    if require_entrypoint and not _is_executable_safe(entrypoint_path):
        raise PluginError(
            "invalid_entrypoint",
            f"Plugin {name!r} entrypoint missing, not executable, or world-writable: {entrypoint_path}",
            EXIT_ERROR,
        )

    describe_path: Path | None = None
    if data.get("describe_file"):
        describe_path = _resolve_inside(plugin_dir, str(data["describe_file"]), field_name="describe_file")

    skills_path: Path | None = None
    if data.get("skills_dir"):
        skills_path = _resolve_inside(plugin_dir, str(data["skills_dir"]), field_name="skills_dir")

    hide_prefixes = [
        p for p in (data.get("session_hide_prefixes") or []) if isinstance(p, str) and p
    ]
    remote_ns = [
        ns for ns in (data.get("remote_namespaces") or []) if isinstance(ns, str) and ns
    ]
    rpc_args = [
        str(a) for a in (data.get("rpc_args") or []) if isinstance(a, str) and a
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "version": version.strip(),
        "description": str(data.get("description") or "").strip(),
        "core_api": str(data.get("core_api") or "").strip(),
        "runtime": str(data.get("runtime") or "executable").strip(),
        "commands": commands,
        "entrypoint_rel": str(entrypoint_rel),
        "entrypoint": str(entrypoint_path),
        "rpc_args": rpc_args,
        "describe_file": str(describe_path) if describe_path is not None else None,
        "skills_dir": str(skills_path) if skills_path is not None else None,
        "session_hide_prefixes": hide_prefixes,
        "remote_namespaces": remote_ns,
        "enabled": bool(data.get("enabled", True)),
        "plugin_dir": str(plugin_dir.resolve()),
        "source": data.get("source") if isinstance(data.get("source"), dict) else {},
    }


def _scan_installed_manifests(root: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    loaded: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    if not root.is_dir():
        return loaded, errors
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        manifest_path = entry / "plugin.json"
        if not manifest_path.is_file():
            continue
        try:
            raw = _read_json(manifest_path)
            validated = validate_manifest(raw, entry, require_entrypoint=False)
            loaded[validated["name"]] = validated
        except (OSError, ValueError, PluginError) as exc:
            errors.append({"dir": str(entry), "error": str(exc)})
    return loaded, errors


def _compile_index(manifests: dict[str, dict[str, Any]]) -> dict[str, Any]:
    commands_map: dict[str, str] = {}
    hide_prefixes: list[str] = []
    hide_seen: set[str] = set()
    remote_namespaces: dict[str, str] = {}
    plugins_entries: dict[str, dict[str, Any]] = {}

    for name in sorted(manifests):
        spec = manifests[name]
        enabled = bool(spec.get("enabled", True))
        if enabled:
            for cmd in spec["commands"]:
                owner = commands_map.get(cmd)
                if owner is not None and owner != name:
                    raise PluginError(
                        "command_conflict",
                        f"Command {cmd!r} is claimed by both {owner!r} and {name!r}.",
                        EXIT_CONFLICT,
                    )
                commands_map[cmd] = name
            for prefix in spec.get("session_hide_prefixes") or []:
                if prefix not in hide_seen:
                    hide_seen.add(prefix)
                    hide_prefixes.append(prefix)
            for ns in spec.get("remote_namespaces") or []:
                remote_namespaces[ns] = name

        plugins_entries[name] = {
            "version": spec["version"],
            "description": spec["description"],
            "core_api": spec["core_api"],
            "runtime": spec["runtime"],
            "commands": list(spec["commands"]),
            "entrypoint": spec["entrypoint"],
            "rpc_args": list(spec["rpc_args"]),
            "describe_file": spec["describe_file"],
            "skills_dir": spec["skills_dir"],
            "session_hide_prefixes": list(spec["session_hide_prefixes"]),
            "remote_namespaces": list(spec["remote_namespaces"]),
            "plugin_dir": spec["plugin_dir"],
            "enabled": enabled,
            "source": spec.get("source") or {},
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "commands": commands_map,
        "session_hide_prefixes": hide_prefixes,
        "remote_namespaces": remote_namespaces,
        "plugins": plugins_entries,
    }


def rebuild_index(root: Path | str | None = None) -> dict[str, Any]:
    """Scan installed manifests, detect collisions, and atomically write ``index.json``."""
    p_root = plugins_dir(root)
    manifests, errors = _scan_installed_manifests(p_root)
    if errors:
        first = errors[0]
        raise PluginError(
            "invalid_manifest",
            f"Invalid plugin manifest in {first['dir']}: {first['error']}",
            EXIT_ERROR,
        )
    index = _compile_index(manifests)
    if manifests or (p_root / "index.json").exists():
        _atomic_write_json(p_root / "index.json", index, mode=0o600)
    return index


def _empty_index() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "",
        "commands": {},
        "session_hide_prefixes": [],
        "remote_namespaces": {},
        "plugins": {},
    }


def load_index(root: Path | str | None = None) -> dict[str, Any]:
    """Fast-path read of ``plugins/index.json`` with fail-safe manifest rebuild."""
    p_root = plugins_dir(root)
    index_path = p_root / "index.json"
    if index_path.is_file():
        try:
            data = _read_json(index_path)
            if (
                isinstance(data, dict)
                and data.get("schema_version") == SCHEMA_VERSION
                and isinstance(data.get("commands"), dict)
                and isinstance(data.get("plugins"), dict)
            ):
                return data
        except (OSError, ValueError):
            pass

    if not p_root.is_dir():
        return _empty_index()
    has_plugins = any(
        entry.is_dir() and not entry.name.startswith(".") and (entry / "plugin.json").is_file()
        for entry in p_root.iterdir()
    )
    if not has_plugins:
        return _empty_index()
    try:
        from corral import observe

        observe.event("plugin_index_fallback", path=str(index_path))
    except Exception:
        pass
    manifests, _ = _scan_installed_manifests(p_root)
    try:
        return _compile_index(manifests)
    except PluginError:
        return _empty_index()


def resolve_command(cmd: str, *, root: Path | str | None = None) -> dict[str, Any] | None:
    """Resolve an enabled installed plugin descriptor for top-level command ``cmd``."""
    if not cmd or cmd in RESERVED_COMMANDS:
        return None
    index = load_index(root)
    plugin_name = index.get("commands", {}).get(cmd)
    if not isinstance(plugin_name, str) or not plugin_name:
        return None
    entry = index.get("plugins", {}).get(plugin_name)
    if not isinstance(entry, dict) or not entry.get("enabled", True):
        return None
    result = dict(entry)
    result["name"] = plugin_name
    return result


def resolve_path_plugin(cmd: str) -> str | None:
    """Resolve a standalone ``corral-<cmd>`` binary on ``PATH`` (e.g. external tools)."""
    if not cmd or cmd in RESERVED_COMMANDS or not _CMD_RE.match(cmd):
        return None
    return shutil.which(f"corral-{cmd}")


def official_hint_for_command(cmd: str) -> str | None:
    """Return official plugin name if ``cmd`` is a known uninstalled plugin command."""
    for name, info in OFFICIAL_PLUGINS.items():
        if cmd == name or cmd in info["commands"]:
            return name
    return None


def remote_capabilities(root: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Return ``hello.capabilities.plugins`` summary for enabled remote-aware plugins."""
    try:
        index = load_index(root)
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, entry in sorted((index.get("plugins") or {}).items()):
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        namespaces = [ns for ns in (entry.get("remote_namespaces") or []) if isinstance(ns, str) and ns]
        if namespaces:
            out[name] = {
                "version": str(entry.get("version") or ""),
                "namespaces": namespaces,
            }
    return out


def _format_describe_entry(spec: dict[str, Any], *, full: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": spec["name"],
        "help": spec.get("help", ""),
        "risk": spec.get("risk", "read"),
        "args": [],
    }
    for arg in spec.get("args", []) or []:
        if not isinstance(arg, dict):
            continue
        arg_entry: dict[str, Any] = {"flags": arg.get("flags", [])}
        kwargs = arg.get("kwargs") if isinstance(arg.get("kwargs"), dict) else {}
        for key in ("help", "default", "choices", "required", "nargs"):
            if key in kwargs:
                arg_entry[key] = kwargs[key]
        if "type" in arg_entry:
            pass
        elif "type" in arg:
            arg_entry["type"] = arg["type"]
        elif isinstance(kwargs.get("type"), type):
            arg_entry["type"] = kwargs["type"].__name__
        elif isinstance(kwargs.get("type"), str):
            arg_entry["type"] = kwargs["type"]
        entry["args"].append(arg_entry)
    if spec.get("examples") and full:
        entry["examples"] = list(spec["examples"])
    if full:
        entry["fields"] = spec.get("fields", {})
    return entry


def _read_plugin_commands(entry: dict[str, Any]) -> list[dict[str, Any]]:
    desc_file = entry.get("describe_file")
    if isinstance(desc_file, str) and desc_file and Path(desc_file).is_file():
        try:
            raw = _read_json(Path(desc_file))
            if isinstance(raw, dict) and isinstance(raw.get("commands"), list):
                return [c for c in raw["commands"] if isinstance(c, dict) and c.get("name")]
            if isinstance(raw, list):
                return [c for c in raw if isinstance(c, dict) and c.get("name")]
        except (OSError, ValueError):
            pass
    return []


def describe_plugin_commands(target: str, *, root: Path | str | None = None) -> tuple[str, Any] | None:
    """Inspect installed plugin ``describe.json`` for ``corral describe <target>``.

    Returns ``("group", list_of_entries)`` when ``target`` matches a plugin name or
    top-level command alias (e.g. ``workflow`` / ``wf``), or ``("command", entry)``
    when it matches an exact leaf command (e.g. ``workflow run``).
    """
    index = load_index(root)
    plugins_map = index.get("plugins") or {}

    # 1. Exact plugin name or top-level command alias -> list plugin subcommands
    matched_plugin_name: str | None = None
    if target in plugins_map and plugins_map[target].get("enabled", True):
        matched_plugin_name = target
    elif target in (index.get("commands") or {}):
        matched_plugin_name = index["commands"][target]
    if matched_plugin_name and matched_plugin_name in plugins_map:
        entry = plugins_map[matched_plugin_name]
        if entry.get("enabled", True):
            cmds = _read_plugin_commands(entry)
            return ("group", [_format_describe_entry(spec, full=False) for spec in cmds])

    # 2. Exact leaf command across enabled plugins (also map alias prefix e.g. "wf run" -> "workflow run")
    parts = target.split(None, 1)
    alias_owner = (index.get("commands") or {}).get(parts[0]) if parts else None
    for pname, entry in sorted(plugins_map.items()):
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        cmds = _read_plugin_commands(entry)
        for spec in cmds:
            if spec["name"] == target:
                return ("command", _format_describe_entry(spec, full=True))
            if alias_owner == pname and len(parts) == 2:
                spec_parts = spec["name"].split(None, 1)
                if len(spec_parts) == 2 and spec_parts[1] == parts[1]:
                    return ("command", _format_describe_entry(spec, full=True))
    return None


def build_plugin_env(plugin: dict[str, Any], *, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build deterministic environment variables injected into a plugin subprocess."""
    env = dict(base_env if base_env is not None else os.environ)
    cfg = config_home()
    st_dir = state_dir()
    cache = os.environ.get("CORRAL_CACHE_DIR", "").strip()
    if not cache:
        try:
            from corral.legacy_names import cache_dir

            cache = str(cache_dir())
        except Exception:
            cache = str(Path.home() / ".cache" / "corral")
    env["CORRAL_BIN"] = corral_bin()
    env["CORRAL_CORE_VERSION"] = _core_version()
    env["CORRAL_PLUGIN_NAME"] = str(plugin.get("name") or "")
    env["CORRAL_PLUGIN_DIR"] = str(plugin.get("plugin_dir") or "")
    env["CORRAL_CONFIG_DIR"] = str(cfg)
    env["CORRAL_STATE_DIR"] = str(st_dir)
    env["CORRAL_CACHE_DIR"] = cache
    env["CORRAL_ACTIVE_RUNTIMES"] = ACTIVE_RUNTIMES_CSV
    env["CORRAL_REMOTE_SOCKET"] = str(st_dir / "remote-events.sock")
    return env


def exec_plugin(plugin: dict[str, Any], argv: list[str]) -> int:
    """Execute an installed plugin entrypoint with injected Corral context."""
    entrypoint = Path(str(plugin["entrypoint"]))
    if not _is_executable_safe(entrypoint):
        raise PluginError(
            "invalid_entrypoint",
            f"Plugin entrypoint is missing or not executable: {entrypoint}",
            EXIT_ERROR,
            hint=f"Run '{corral_bin()} plugin doctor --fix'",
            next_commands=[f"{corral_bin()} plugin doctor --fix"],
        )
    env = build_plugin_env(plugin)
    cmd = [str(entrypoint), *argv]
    if os.name != "nt" and os.environ.get("CORRAL_PLUGIN_NO_EXEC") != "1":
        try:
            os.execve(str(entrypoint), cmd, env)
        except OSError as exc:
            raise PluginError(
                "exec_failed",
                f"Failed to execute plugin {plugin.get('name')}: {exc}",
                EXIT_ERROR,
            ) from exc
    try:
        return subprocess.call(cmd, env=env)
    except OSError as exc:
        raise PluginError("exec_failed", f"Failed to execute plugin {plugin.get('name')}: {exc}", EXIT_ERROR) from exc


def exec_path_plugin(executable: str, argv: list[str]) -> int:
    """Execute a ``corral-<cmd>`` binary found on ``PATH``."""
    plugin = {"name": Path(executable).name.removeprefix("corral-"), "plugin_dir": ""}
    env = build_plugin_env(plugin)
    cmd = [executable, *argv]
    if os.name != "nt" and os.environ.get("CORRAL_PLUGIN_NO_EXEC") != "1":
        os.execve(executable, cmd, env)
    return subprocess.call(cmd, env=env)


def _find_official_source(name: str) -> Path | None:
    info = OFFICIAL_PLUGINS.get(name)
    if not info:
        return None
    override = os.environ.get("CORRAL_PLUGIN_SOURCE_DIR", "").strip()
    if override:
        cand = Path(override).expanduser() / name
        if (cand / "plugin.json").is_file():
            return cand.resolve()
    try:
        import corral

        # Checkout layout: cli/src/corral/__init__.py -> cli/plugins/<name>
        pkg_file = Path(corral.__file__ or "").resolve()
        repo_root = pkg_file.parent.parent.parent
        cand = repo_root / info["repo_subdir"]
        if (cand / "plugin.json").is_file():
            return cand.resolve()
    except Exception:
        pass
    return None


def _discover_console_script(src_dir: Path) -> str | None:
    pyproject = src_dir / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        import tomllib

        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        scripts = ((data.get("project") or {}).get("scripts")) or {}
        for _, target in scripts.items():
            if isinstance(target, str) and ":" in target:
                return target
    except Exception:
        pass
    return None


def _write_editable_launcher(
    bin_path: Path,
    *,
    python_exe: str,
    src_root: Path,
    entry_target: str,
) -> None:
    mod_name, _, func_name = entry_target.partition(":")
    script = (
        f"#!{python_exe}\n"
        "from __future__ import annotations\n"
        "import sys\n"
        f"sys.path.insert(0, {str(src_root)!r})\n"
        f"from {mod_name} import {func_name}\n"
        "if __name__ == '__main__':\n"
        f"    raise SystemExit({func_name}())\n"
    )
    _atomic_write_text(bin_path, script, mode=0o755)


def _write_venv_launcher(bin_path: Path, *, venv_bin: Path, target_name: str, entry_target: str | None) -> None:
    venv_cli = venv_bin / target_name
    venv_py = venv_bin / "python"
    if venv_cli.is_file():
        script = (
            f"#!{venv_py}\n"
            "from __future__ import annotations\n"
            "import os, sys\n"
            f"os.execv({str(venv_cli)!r}, [{str(venv_cli)!r}, *sys.argv[1:]])\n"
        )
        _atomic_write_text(bin_path, script, mode=0o755)
        return
    if entry_target:
        mod_name, _, func_name = entry_target.partition(":")
        script = (
            f"#!{venv_py}\n"
            "from __future__ import annotations\n"
            "import sys\n"
            f"from {mod_name} import {func_name}\n"
            "if __name__ == '__main__':\n"
            f"    raise SystemExit({func_name}())\n"
        )
        _atomic_write_text(bin_path, script, mode=0o755)
        return
    raise PluginError("install_failed", f"Installed venv has no entrypoint {venv_cli}", EXIT_ERROR)


def _provision_isolated_venv(
    venv_dir: Path,
    src_dir: Path,
    *,
    python_exe: str,
    editable: bool = False,
) -> None:
    uv = shutil.which("uv")
    find_links = os.environ.get("CORRAL_PLUGIN_FIND_LINKS", "").strip()
    offline = os.environ.get("CORRAL_PLUGIN_OFFLINE") == "1"
    no_build_iso = os.environ.get("CORRAL_PLUGIN_NO_BUILD_ISOLATION") == "1"
    if uv:
        r_venv = subprocess.run([uv, "venv", "--python", python_exe, str(venv_dir)], capture_output=True, text=True)
        if r_venv.returncode == 0:
            pip_cmd = [uv, "pip", "install", "--python", str(venv_dir / "bin" / "python")]
            if find_links:
                pip_cmd.extend(["--find-links", find_links])
            if offline:
                pip_cmd.append("--offline")
            if no_build_iso:
                pip_cmd.append("--no-build-isolation")
            if editable:
                pip_cmd.extend(["-e", str(src_dir)])
            else:
                pip_cmd.append(str(src_dir))
            r_pip = subprocess.run(pip_cmd, capture_output=True, text=True)
            if r_pip.returncode == 0:
                return
            shutil.rmtree(venv_dir, ignore_errors=True)

    r_std = subprocess.run([python_exe, "-m", "venv", str(venv_dir)], capture_output=True, text=True)
    if r_std.returncode != 0:
        raise PluginError("venv_failed", f"Failed to create plugin venv: {r_std.stderr or r_std.stdout}", EXIT_ERROR)
    venv_py = str(venv_dir / "bin" / "python")
    pip_cmd = [venv_py, "-m", "pip", "install"]
    if find_links:
        pip_cmd.extend(["--find-links", find_links])
    if offline:
        pip_cmd.append("--no-index")
    if no_build_iso:
        pip_cmd.append("--no-build-isolation")
    if editable:
        pip_cmd.extend(["-e", str(src_dir)])
    else:
        pip_cmd.append(str(src_dir))
    r_pip = subprocess.run(pip_cmd, capture_output=True, text=True)
    if r_pip.returncode != 0:
        raise PluginError(
            "install_failed",
            f"Failed to install plugin dependencies: {(r_pip.stderr or r_pip.stdout).strip()}",
            EXIT_ERROR,
        )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _copy_if_exists(src: Path, dst: Path) -> None:
    if src.is_file():
        _atomic_write_text(dst, src.read_text(encoding="utf-8"), mode=0o600)
    elif src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)


def install_plugin(
    spec: str,
    *,
    editable: bool = False,
    dry_run: bool = False,
    python_exe: str | None = None,
    root: Path | str | None = None,
) -> dict[str, Any]:
    """Install a plugin from an official name or local directory into ``plugins/``."""
    p_root = plugins_dir(root)
    raw_spec = str(spec or "").strip()
    if not raw_spec:
        raise PluginError("usage_error", "Provide a plugin name or local directory path.", EXIT_USAGE)

    src_dir: Path | None = None
    is_official = raw_spec in OFFICIAL_PLUGINS
    cand = Path(raw_spec).expanduser()
    if cand.exists():
        src_dir = cand.resolve()
    elif is_official:
        src_dir = _find_official_source(raw_spec)
        if src_dir is None:
            info = OFFICIAL_PLUGINS[raw_spec]
            raise PluginError(
                "plugin_source_unavailable",
                f"Official plugin {raw_spec!r} source is not bundled in this environment.",
                EXIT_NOT_FOUND,
                hint=(
                    f"Pass a local checkout path ({info['repo_subdir']}) or set "
                    "CORRAL_PLUGIN_SOURCE_DIR to the plugins directory."
                ),
            )
    else:
        raise PluginError("not_found", f"Unknown plugin or missing directory: {raw_spec}", EXIT_NOT_FOUND)

    manifest_path = src_dir / "plugin.json"
    if not manifest_path.is_file():
        raise PluginError("invalid_manifest", f"Missing plugin.json in {src_dir}", EXIT_USAGE)
    raw_manifest = _read_json(manifest_path)
    validated = validate_manifest(raw_manifest, src_dir, require_entrypoint=False)
    name = validated["name"]

    # Check command conflicts against other currently enabled plugins before staging.
    current_index = load_index(p_root)
    for cmd in validated["commands"]:
        owner = current_index.get("commands", {}).get(cmd)
        if owner is not None and owner != name:
            raise PluginError(
                "command_conflict",
                f"Command {cmd!r} is already claimed by installed plugin {owner!r}.",
                EXIT_CONFLICT,
            )

    dest_dir = p_root / name
    if dry_run:
        return {
            "action": "install",
            "dry_run": True,
            "name": name,
            "version": validated["version"],
            "mode": "editable" if editable else validated["runtime"],
            "source": str(src_dir),
            "target": str(dest_dir),
            "commands": validated["commands"],
        }

    p_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = p_root / f".staging-{name}-{os.getpid()}-{int(time.time() * 1000)}"
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup: Path | None = None
    try:
        chosen_py = python_exe or sys.executable
        bin_dir = staging / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry_rel = validated["entrypoint_rel"]
        entry_dst = staging / entry_rel
        entry_dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        console_target = _discover_console_script(src_dir)
        src_pkg_root = src_dir / "src"

        if editable:
            if not console_target or not src_pkg_root.is_dir():
                src_bin = src_dir / entry_rel
                if src_bin.is_file():
                    shutil.copy2(src_bin, entry_dst)
                    os.chmod(entry_dst, 0o755)
                else:
                    raise PluginError(
                        "invalid_plugin",
                        f"Editable plugin {name!r} needs pyproject console script + src/ or {entry_rel}.",
                        EXIT_USAGE,
                    )
            else:
                _write_editable_launcher(
                    entry_dst,
                    python_exe=chosen_py,
                    src_root=src_pkg_root,
                    entry_target=console_target,
                )
            source_meta: dict[str, Any] = {"type": "editable", "path": str(src_dir), "python": chosen_py}
        elif validated["runtime"] == "python-venv" and (src_dir / "pyproject.toml").is_file():
            venv_dir = staging / ".venv"
            _provision_isolated_venv(venv_dir, src_dir, python_exe=chosen_py, editable=False)
            _write_venv_launcher(
                entry_dst,
                venv_bin=venv_dir / "bin",
                target_name=Path(entry_rel).name,
                entry_target=console_target,
            )
            source_meta = {"type": "local", "path": str(src_dir), "python": chosen_py}
        else:
            src_bin = src_dir / entry_rel
            if not src_bin.is_file():
                raise PluginError("invalid_entrypoint", f"Missing executable {src_bin}", EXIT_ERROR)
            shutil.copy2(src_bin, entry_dst)
            os.chmod(entry_dst, 0o755)
            source_meta = {"type": "binary", "path": str(src_dir), "sha256": _sha256_file(entry_dst)}

        manifest_out = dict(raw_manifest)
        manifest_out["enabled"] = True
        manifest_out["source"] = source_meta
        _atomic_write_json(staging / "plugin.json", manifest_out, mode=0o600)

        if validated["describe_file"]:
            rel_desc = raw_manifest["describe_file"]
            _copy_if_exists(src_dir / rel_desc, staging / rel_desc)
        if validated["skills_dir"]:
            rel_skills = raw_manifest["skills_dir"]
            _copy_if_exists(src_dir / rel_skills, staging / rel_skills)

        validate_manifest(_read_json(staging / "plugin.json"), staging, require_entrypoint=True)

        if dest_dir.exists():
            backup = p_root / f".backup-{name}-{os.getpid()}"
            if backup.exists():
                shutil.rmtree(backup)
            os.replace(dest_dir, backup)
        os.replace(staging, dest_dir)
        index = rebuild_index(p_root)
        if backup is not None and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        if backup is not None and backup.exists() and not dest_dir.exists():
            try:
                os.replace(backup, dest_dir)
            except OSError:
                pass
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "action": "install",
        "dry_run": False,
        "name": name,
        "version": validated["version"],
        "mode": "editable" if editable else validated["runtime"],
        "target": str(dest_dir),
        "commands": index["plugins"][name]["commands"],
    }


def upgrade_plugin(
    name: str | None = None,
    *,
    all_plugins: bool = False,
    dry_run: bool = False,
    root: Path | str | None = None,
) -> dict[str, Any]:
    p_root = plugins_dir(root)
    index = load_index(p_root)
    plugins_map = index.get("plugins") or {}
    targets = sorted(plugins_map) if all_plugins else ([name] if name else [])
    if not targets:
        raise PluginError("usage_error", "Specify a plugin name or pass --all.", EXIT_USAGE)
    results = []
    for pname in targets:
        entry = plugins_map.get(pname)
        if not isinstance(entry, dict):
            raise PluginError("not_found", f"Plugin {pname!r} is not installed.", EXIT_NOT_FOUND)
        src = entry.get("source") or {}
        src_path = src.get("path")
        if not src_path:
            raise PluginError(
                "upgrade_unavailable",
                f"Plugin {pname!r} has no recorded source path.",
                EXIT_USAGE,
            )
        res = install_plugin(
            str(src_path),
            editable=src.get("type") == "editable",
            dry_run=dry_run,
            python_exe=src.get("python"),
            root=p_root,
        )
        results.append(res)
    return {"action": "upgrade", "dry_run": dry_run, "plugins": results}


def remove_plugin(
    name: str,
    *,
    purge_state: bool = False,
    dry_run: bool = False,
    root: Path | str | None = None,
) -> dict[str, Any]:
    p_root = plugins_dir(root)
    dest_dir = p_root / name
    if not dest_dir.exists():
        raise PluginError("not_found", f"Plugin {name!r} is not installed.", EXIT_NOT_FOUND)
    removed_state: list[str] = []
    if purge_state and name == "workflow":
        wf_root = Path(os.environ.get("CORRAL_WORKFLOW_ROOT") or (config_home() / "workflows")).expanduser()
        if wf_root.exists():
            removed_state.append(str(wf_root))
    if dry_run:
        return {
            "action": "remove",
            "dry_run": True,
            "name": name,
            "target": str(dest_dir),
            "purge_state": purge_state,
            "state_paths": removed_state,
        }
    shutil.rmtree(dest_dir)
    if purge_state:
        unregister_hidden_session(owner=name)
        for sp in removed_state:
            shutil.rmtree(sp, ignore_errors=True)
    rebuild_index(p_root)
    return {
        "action": "remove",
        "dry_run": False,
        "name": name,
        "target": str(dest_dir),
        "purge_state": purge_state,
        "state_paths": removed_state,
    }


def set_plugin_enabled(
    name: str,
    *,
    enabled: bool,
    dry_run: bool = False,
    root: Path | str | None = None,
) -> dict[str, Any]:
    p_root = plugins_dir(root)
    manifest_path = p_root / name / "plugin.json"
    if not manifest_path.is_file():
        raise PluginError("not_found", f"Plugin {name!r} is not installed.", EXIT_NOT_FOUND)
    raw = _read_json(manifest_path)
    validated = validate_manifest(raw, manifest_path.parent, require_entrypoint=False)
    if enabled:
        idx = load_index(p_root)
        for cmd in validated["commands"]:
            owner = idx.get("commands", {}).get(cmd)
            if owner is not None and owner != name:
                raise PluginError(
                    "command_conflict",
                    f"Command {cmd!r} is already claimed by enabled plugin {owner!r}.",
                    EXIT_CONFLICT,
                )
    if not dry_run:
        raw["enabled"] = bool(enabled)
        _atomic_write_json(manifest_path, raw, mode=0o600)
        rebuild_index(p_root)
    return {
        "action": "enable" if enabled else "disable",
        "dry_run": dry_run,
        "name": name,
        "enabled": bool(enabled),
    }


def list_plugins(*, root: Path | str | None = None) -> dict[str, Any]:
    p_root = plugins_dir(root)
    index = load_index(p_root)
    items: list[dict[str, Any]] = []
    for name, entry in sorted((index.get("plugins") or {}).items()):
        if not isinstance(entry, dict):
            continue
        ep = Path(str(entry.get("entrypoint") or ""))
        healthy = _is_executable_safe(ep)
        items.append(
            {
                "name": name,
                "version": str(entry.get("version") or ""),
                "enabled": bool(entry.get("enabled", True)),
                "commands": list(entry.get("commands") or []),
                "runtime": str(entry.get("runtime") or "executable"),
                "status": "ready" if healthy else "broken_entrypoint",
                "entrypoint": str(ep),
            }
        )
    # Also surface standalone PATH executables (e.g. corral-flex).
    path_plugins: list[dict[str, str]] = []
    seen_path_cmds: set[str] = set()
    for folder in (os.environ.get("PATH") or "").split(os.pathsep):
        if not folder:
            continue
        f_path = Path(folder)
        if not f_path.is_dir():
            continue
        try:
            for cand in sorted(f_path.iterdir()):
                if not cand.name.startswith("corral-"):
                    continue
                cmd = cand.name.removeprefix("corral-")
                if not cmd or cmd in seen_path_cmds:
                    continue
                if _is_executable_safe(cand):
                    seen_path_cmds.add(cmd)
                    path_plugins.append({"command": cmd, "entrypoint": str(cand)})
        except OSError:
            continue
    return {
        "plugins_dir": str(p_root),
        "plugins": items,
        "path_plugins": path_plugins,
    }


def doctor_plugins(*, fix: bool = False, root: Path | str | None = None) -> dict[str, Any]:
    p_root = plugins_dir(root)
    manifests, errors = _scan_installed_manifests(p_root)
    checks: list[dict[str, Any]] = []
    repaired: list[str] = []

    for err in errors:
        checks.append({"name": Path(err["dir"]).name, "ok": False, "issue": err["error"]})

    for name, spec in sorted(manifests.items()):
        pdir = Path(spec["plugin_dir"])
        ep = Path(spec["entrypoint"])
        issue: str | None = None
        if not ep.is_file():
            issue = f"missing entrypoint: {ep}"
        elif not _is_executable_safe(ep):
            issue = f"entrypoint not executable or unsafe mode: {ep}"
        elif spec["runtime"] == "python-venv":
            venv_py = pdir / ".venv" / "bin" / "python"
            if (pdir / ".venv").exists():
                probe = subprocess.run([str(venv_py), "-c", "import sys"], capture_output=True)
                if probe.returncode != 0:
                    issue = "broken isolated .venv interpreter"
        if issue and fix:
            src = spec.get("source") or {}
            src_path = src.get("path")
            if src_path and Path(str(src_path)).is_dir():
                install_plugin(
                    str(src_path),
                    editable=src.get("type") == "editable",
                    dry_run=False,
                    root=p_root,
                )
                repaired.append(name)
                issue = None
        checks.append({"name": name, "ok": issue is None, "issue": issue})

    index_rebuilt = False
    if fix and not errors:
        rebuild_index(p_root)
        index_rebuilt = True
    index_exists = (p_root / "index.json").is_file()
    return {
        "healthy": all(c["ok"] for c in checks) and not errors,
        "plugins_dir": str(p_root),
        "index_exists": index_exists,
        "index_rebuilt": index_rebuilt,
        "repaired": repaired,
        "checks": checks,
    }


class _PluginArgParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise PluginError("usage_error", message, EXIT_USAGE, hint=f"Run '{corral_bin()} plugin --help'")


def _emit(
    payload: dict[str, Any],
    *,
    json_mode: bool,
    tsv_lines: list[str] | None = None,
    human: str | None = None,
) -> None:
    if tsv_lines is not None:
        print("\n".join(tsv_lines))
        return
    if json_mode or human is None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(human)


def cli_main(argv: list[str]) -> int:
    """Entry point for ``corral plugin …``."""
    parser = _PluginArgParser(prog="corral plugin", description="Manage external Corral plugins.")
    sub = parser.add_subparsers(dest="subcommand")

    p_list = sub.add_parser("list", help="List installed plugins and PATH extensions")
    p_list.add_argument("--format", choices=["json", "tsv"], default="json")
    p_list.add_argument("--json", action="store_const", const="json", dest="format")

    p_inst = sub.add_parser("install", help="Install a plugin from official name or local directory")
    p_inst.add_argument("spec", help="Plugin name (e.g. workflow) or local path")
    p_inst.add_argument("--editable", action="store_true", help="Link local source tree in editable mode")
    p_inst.add_argument("--dry-run", action="store_true")
    p_inst.add_argument("--python", dest="python_exe", help="Python interpreter path for isolated venv")
    p_inst.add_argument("--json", action="store_true", dest="json_mode")

    p_upg = sub.add_parser("upgrade", help="Upgrade installed plugin(s)")
    p_upg.add_argument("name", nargs="?")
    p_upg.add_argument("--all", action="store_true", dest="all_plugins")
    p_upg.add_argument("--dry-run", action="store_true")
    p_upg.add_argument("--json", action="store_true", dest="json_mode")

    p_rm = sub.add_parser("remove", help="Uninstall a plugin")
    p_rm.add_argument("name")
    p_rm.add_argument("--purge-state", action="store_true", help="Also remove plugin-owned state databases")
    p_rm.add_argument("--dry-run", action="store_true")
    p_rm.add_argument("--json", action="store_true", dest="json_mode")

    p_en = sub.add_parser("enable", help="Enable an installed plugin")
    p_en.add_argument("name")
    p_en.add_argument("--dry-run", action="store_true")
    p_en.add_argument("--json", action="store_true", dest="json_mode")

    p_dis = sub.add_parser("disable", help="Disable an installed plugin without removing it")
    p_dis.add_argument("name")
    p_dis.add_argument("--dry-run", action="store_true")
    p_dis.add_argument("--json", action="store_true", dest="json_mode")

    p_doc = sub.add_parser("doctor", help="Check plugin manifests, index, and isolated environments")
    p_doc.add_argument("--fix", action="store_true", help="Rebuild index and repair broken venvs/launchers")
    p_doc.add_argument("--json", action="store_true", dest="json_mode")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_OK
    except PluginError as exc:
        _emit(
            error_envelope(exc.code, exc.message, hint=exc.hint, next_commands=exc.next_commands),
            json_mode=True,
        )
        return exc.exit_code

    if not args.subcommand:
        parser.print_help()
        return EXIT_OK

    json_mode = bool(getattr(args, "json_mode", False)) or not sys.stdout.isatty()
    try:
        if args.subcommand == "list":
            data = list_plugins()
            if getattr(args, "format", "json") == "tsv":
                lines = ["name\tversion\tenabled\tcommands\truntime\tstatus"]
                for p in data["plugins"]:
                    lines.append(
                        "\t".join(
                            (
                                tsv_cell(p["name"]),
                                tsv_cell(p["version"]),
                                "true" if p["enabled"] else "false",
                                tsv_cell(",".join(p["commands"])),
                                tsv_cell(p["runtime"]),
                                tsv_cell(p["status"]),
                            )
                        )
                    )
                lines.append(f"# count={len(data['plugins'])} path_plugins={len(data['path_plugins'])}")
                _emit(ok_envelope(data), json_mode=False, tsv_lines=lines)
            else:
                _emit(ok_envelope(data), json_mode=True)
            return EXIT_OK

        if args.subcommand == "install":
            data = install_plugin(
                args.spec,
                editable=args.editable,
                dry_run=args.dry_run,
                python_exe=args.python_exe,
            )
            _emit(ok_envelope(data, meta={"dry_run": args.dry_run}), json_mode=json_mode or True)
            return EXIT_OK

        if args.subcommand == "upgrade":
            data = upgrade_plugin(args.name, all_plugins=args.all_plugins, dry_run=args.dry_run)
            _emit(ok_envelope(data, meta={"dry_run": args.dry_run}), json_mode=json_mode or True)
            return EXIT_OK

        if args.subcommand == "remove":
            data = remove_plugin(args.name, purge_state=args.purge_state, dry_run=args.dry_run)
            _emit(ok_envelope(data, meta={"dry_run": args.dry_run}), json_mode=json_mode or True)
            return EXIT_OK

        if args.subcommand in {"enable", "disable"}:
            data = set_plugin_enabled(args.name, enabled=(args.subcommand == "enable"), dry_run=args.dry_run)
            _emit(ok_envelope(data, meta={"dry_run": args.dry_run}), json_mode=json_mode or True)
            return EXIT_OK

        if args.subcommand == "doctor":
            data = doctor_plugins(fix=args.fix)
            _emit(ok_envelope(data), json_mode=json_mode or True)
            return EXIT_OK if data["healthy"] else EXIT_ERROR
    except PluginError as exc:
        _emit(
            error_envelope(exc.code, exc.message, hint=exc.hint, next_commands=exc.next_commands),
            json_mode=True,
        )
        return exc.exit_code
    return EXIT_OK


def internal_cli_main(argv: list[str]) -> int:
    """Entry point for ``corral internal …`` state-contract helpers for polyglot plugins."""
    parser = _PluginArgParser(prog="corral internal", description="Internal state-contract CLI for Corral plugins.")
    sub = parser.add_subparsers(dest="group")

    p_lease = sub.add_parser("lease", help="Manage host-leases.sqlite3 entries")
    lease_sub = p_lease.add_subparsers(dest="action")
    p_acq = lease_sub.add_parser("acquire")
    p_acq.add_argument("--owner", required=True)
    p_acq.add_argument("--lease-id", required=True)
    p_acq.add_argument("--keepalive", required=True)
    p_acq.add_argument("--pid", type=int, default=os.getpid())
    p_acq.add_argument("--ttl", type=float, default=300.0)

    p_rel = lease_sub.add_parser("release")
    p_rel.add_argument("--owner", required=True)
    p_rel.add_argument("--lease-id", required=True)

    lease_sub.add_parser("list")

    p_hid = sub.add_parser("hidden", help="Manage hidden-sessions.sqlite3 entries")
    hid_sub = p_hid.add_subparsers(dest="action")
    p_hadd = hid_sub.add_parser("add")
    p_hadd.add_argument("--session-key", required=True)
    p_hadd.add_argument("--owner", required=True)
    p_hadd.add_argument("--keepalive")
    p_hadd.add_argument("--raw-id")
    p_hadd.add_argument("--group-id")

    p_hrm = hid_sub.add_parser("remove")
    p_hrm.add_argument("--session-key")
    p_hrm.add_argument("--owner")
    p_hrm.add_argument("--group-id")

    p_hls = hid_sub.add_parser("list")
    p_hls.add_argument("--owner")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_OK
    except PluginError as exc:
        _emit(error_envelope(exc.code, exc.message, hint=exc.hint, next_commands=exc.next_commands), json_mode=True)
        return exc.exit_code

    if args.group == "lease":
        if args.action == "acquire":
            data = acquire_host_lease(
                owner=args.owner,
                lease_id=args.lease_id,
                keepalive_name=args.keepalive,
                pid=args.pid,
                ttl_seconds=args.ttl,
            )
            _emit(ok_envelope(data), json_mode=True)
            return EXIT_OK
        if args.action == "release":
            removed = release_host_lease(owner=args.owner, lease_id=args.lease_id)
            _emit(ok_envelope({"released": removed}), json_mode=True)
            return EXIT_OK
        if args.action == "list":
            _emit(ok_envelope({"leases": list_host_leases()}), json_mode=True)
            return EXIT_OK

    if args.group == "hidden":
        if args.action == "add":
            register_hidden_session(
                session_key=args.session_key,
                owner=args.owner,
                keepalive_name=args.keepalive,
                raw_id=args.raw_id,
                group_id=args.group_id,
            )
            _emit(ok_envelope({"registered": args.session_key}), json_mode=True)
            return EXIT_OK
        if args.action == "remove":
            try:
                count = unregister_hidden_session(
                    session_key=args.session_key,
                    owner=args.owner,
                    group_id=args.group_id,
                )
            except ValueError as exc:
                _emit(error_envelope("usage_error", str(exc)), json_mode=True)
                return EXIT_USAGE
            _emit(ok_envelope({"removed": count}), json_mode=True)
            return EXIT_OK
        if args.action == "list":
            _emit(ok_envelope({"sessions": list_hidden_sessions(owner=args.owner)}), json_mode=True)
            return EXIT_OK

    parser.print_help()
    return EXIT_OK
