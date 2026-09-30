#!/usr/bin/env python3
"""Prepare, inspect, and run commands in a checkout-owned Python environment."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
RUFF_VERSION = "0.16.1"
PYTEST_VERSION = "9.1.1"
_PROJECTS = {
    "corral": {"package": "corral", "runtime": ("textual", "websockets", "sesskit")},
    "sesskit": {"package": "sesskit", "runtime": ()},
}
_SUMMARY_LINES = 18
_COMMAND_TIMEOUT = 60 * 60


class CliParser(argparse.ArgumentParser):
    json_requested = False

    def error(self, message: str) -> None:
        if self.json_requested or "--json" in sys.argv:
            emit(
                {
                    "ok": False,
                    "data": None,
                    "error": {"code": "usage_error", "message": message},
                    "meta": {},
                },
                2,
                json_mode=True,
            )
            raise SystemExit(2)
        super().error(message)


def emit(result: dict[str, Any], exit_code: int, *, json_mode: bool) -> int:
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    elif result["ok"]:
        data = result["data"] or {}
        print(data.get("summary", "Ready."))
        for warning in data.get("warnings", ()):
            print(f"Warning: {warning}")
        if data.get("log_path"):
            print(f"Details: {data['log_path']}")
    else:
        error = result.get("error") or {}
        print(f"Error [{error.get('code', 'failure')}]: {error.get('message', 'Command failed.')}", file=sys.stderr)
        for next_command in error.get("next_commands", ()):
            print(f"Next: {next_command}", file=sys.stderr)
        if error.get("log_path"):
            print(f"Details: {error['log_path']}", file=sys.stderr)
    return exit_code


def envelope(data: dict[str, Any] | None = None, *, error: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": error is None, "data": data if error is None else None, "error": error, "meta": {"version": 1}}


def _venv_dir(repo: Path) -> Path:
    return repo / ".venv"


def _venv_python(repo: Path) -> Path:
    if os.name == "nt":
        return _venv_dir(repo) / "Scripts" / "python.exe"
    return _venv_dir(repo) / "bin" / "python"


def _log_dir(repo: Path) -> Path:
    return _venv_dir(repo) / "dev-env-logs"


def _command_name(argv: list[str]) -> str:
    if not argv:
        return "command"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(argv[0]).name)[:32] or "command"


def _new_log_path(repo: Path, command: str) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return _log_dir(repo) / f"{stamp}-{os.getpid()}-{command}.log"


def _relative_log(path: Path, repo: Path) -> str:
    return path.relative_to(repo).as_posix()


def _run_logged(
    argv: list[str],
    *,
    repo: Path,
    env: dict[str, str] | None = None,
    timeout: int = _COMMAND_TIMEOUT,
) -> tuple[int, Path, str]:
    """Run without a shell, preserving complete output but returning only a short tail."""
    log_path = _new_log_path(repo, _command_name(argv))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"argv0={Path(argv[0]).name if argv else ''} argc={len(argv)}\n")
        log.write(f"cwd={repo}\n\n")
        log.flush()
        try:
            completed = subprocess.run(
                argv,
                cwd=repo,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            log.write(f"\ncommand timed out after {timeout}s\n")
            returncode = 124
        except OSError as exc:
            log.write(f"\ncommand could not start: {type(exc).__name__}\n")
            returncode = 127
    with log_path.open("rb") as log:
        log.seek(0, os.SEEK_END)
        size = log.tell()
        log.seek(max(0, size - 64 * 1024))
        tail_bytes = log.read()
    tail_lines = tail_bytes.decode("utf-8", errors="replace").splitlines()[-_SUMMARY_LINES:]
    tail = "\n".join(line[:500] for line in tail_lines)
    return returncode, log_path, tail


def _read_project(repo: Path) -> tuple[str, str]:
    text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    project_block = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
    if not project_block:
        raise ValueError("pyproject.toml has no [project] table")
    name = re.search(r'(?m)^name\s*=\s*["\']([^"\']+)["\']', project_block.group(1))
    version = re.search(r'(?m)^version\s*=\s*["\']([^"\']+)["\']', project_block.group(1))
    if not name or not version:
        raise ValueError("pyproject.toml must declare project name and version")
    return name.group(1).lower().replace("-", ""), version.group(1)


def validate_repo(value: str) -> tuple[Path, str, str]:
    repo = Path(value).expanduser().resolve()
    if not repo.is_dir() or not (repo / "pyproject.toml").is_file():
        raise ValueError("--repo must name a checkout directory containing pyproject.toml")
    project_name, project_version = _read_project(repo)
    if project_name not in _PROJECTS:
        raise ValueError("supported targets are Corral and SessKit checkouts")
    package = _PROJECTS[project_name]["package"]
    if not (repo / "src" / package / "__init__.py").is_file():
        raise ValueError(f"checkout is missing src/{package}/__init__.py")
    return repo, project_name, project_version


def _uv_env(repo: Path, *, python: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("UV_PROJECT", None)
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    env.pop("UV_CONFIG_FILE", None)
    env.pop("UV_WORKING_DIR", None)
    env.pop("VIRTUAL_ENV", None)
    env["UV_PROJECT_ENVIRONMENT"] = str(_venv_dir(repo))
    if python:
        env["UV_PYTHON"] = python
    return env


def _python_env(repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = str(repo / "src")
    env["VIRTUAL_ENV"] = str(_venv_dir(repo))
    env["PATH"] = str(_venv_dir(repo) / ("Scripts" if os.name == "nt" else "bin")) + os.pathsep + env.get("PATH", "")
    return env


def _sesskit_pin() -> dict[str, str] | None:
    pin_file = SCRIPT_ROOT / "scripts" / "sesskit_dep.py"
    if not pin_file.is_file():
        return None
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run(
            [sys.executable, str(pin_file), "json"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=10,
            check=False,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    values = {
        "version": payload.get("version"),
        "url": payload.get("wheel_url"),
        "sha256": payload.get("wheel_sha256"),
    }
    return values if all(isinstance(value, str) and value for value in values.values()) else None


def _probe_code(package: str, runtime_packages: tuple[str, ...]) -> str:
    return "\n".join(
        (
            "import importlib, importlib.metadata, json, sys",
            f"package = {package!r}",
            f"runtime_names = {runtime_packages!r}",
            "result = {'executable': sys.executable, 'python_version': sys.version.split()[0], 'prefix': sys.prefix, 'base_prefix': sys.base_prefix, 'packages': {}}",
            "try:",
            "    module = importlib.import_module(package)",
            "    result['source'] = getattr(module, '__file__', None)",
            "    result['source_version'] = getattr(module, '__version__', None)",
            "except Exception as exc:",
            "    result['source_error'] = type(exc).__name__ + ': ' + str(exc)",
            "for name in runtime_names:",
            "    try:",
            "        dist = importlib.metadata.distribution(name)",
            "        raw = dist.read_text('direct_url.json')",
            "        result['packages'][name] = {'version': dist.version, 'direct_url': json.loads(raw) if raw else None}",
            "    except importlib.metadata.PackageNotFoundError:",
            "        result['packages'][name] = None",
            "json.dump(result, sys.stdout)",
        )
    )


def _probe_environment(repo: Path, project_name: str) -> dict[str, Any] | None:
    python = _venv_python(repo)
    if not python.is_file():
        return None
    package = _PROJECTS[project_name]["package"]
    runtime = _PROJECTS[project_name]["runtime"]
    try:
        result = subprocess.run(
            [str(python), "-c", _probe_code(package, runtime)],
            cwd=repo,
            env=_python_env(repo),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"probe_error": type(exc).__name__}
    if result.returncode != 0:
        return {"probe_error": "environment_probe_failed"}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"probe_error": "invalid_environment_probe"}


def _tool_version(repo: Path, module: str) -> str | None:
    python = _venv_python(repo)
    if not python.is_file():
        return None
    try:
        result = subprocess.run(
            [str(python), "-m", module, "--version"],
            cwd=repo,
            env=_python_env(repo),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    output = (result.stdout or result.stderr).strip().splitlines()[0]
    version = re.search(r"\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?", output)
    return version.group(0) if version else output[:120]


def _lock_status(repo: Path, project_name: str, uv_path: str | None) -> dict[str, Any]:
    lock = repo / "uv.lock"
    if not lock.is_file():
        return {"state": "missing", "path": "uv.lock"}
    if not uv_path:
        return {"state": "unverified", "path": "uv.lock"}
    env = _uv_env(repo)
    env["UV_OFFLINE"] = "1"
    try:
        result = subprocess.run(
            [uv_path, "--no-config", "lock", "--check", "--offline", "--project", str(repo)],
            cwd=repo,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"state": "unverified", "path": "uv.lock"}
    return {"state": "current" if result.returncode == 0 else "stale_or_unresolvable", "path": "uv.lock"}


def doctor(repo_value: str) -> tuple[dict[str, Any], bool]:
    repo, project_name, project_version = validate_repo(repo_value)
    uv_path = shutil.which("uv")
    uv_version = None
    if uv_path:
        try:
            result = subprocess.run(
                [uv_path, "--version"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10, check=False
            )
            if result.returncode == 0:
                uv_version = result.stdout.strip()[:120]
        except (OSError, subprocess.TimeoutExpired):
            pass

    venv_python = _venv_python(repo)
    env_info = _probe_environment(repo, project_name)
    package = _PROJECTS[project_name]["package"]
    expected_source = (repo / "src" / package / "__init__.py").resolve()
    actual_source = Path(env_info["source"]).resolve() if env_info and env_info.get("source") else None
    source_matches = actual_source == expected_source

    tools: dict[str, Any] = {"uv": {"available": bool(uv_path), "version": uv_version}}
    expected_tools = {"ruff": RUFF_VERSION}
    if project_name == "sesskit":
        expected_tools["pytest"] = PYTEST_VERSION
    for name, expected in expected_tools.items():
        version = _tool_version(repo, name) if env_info else None
        tools[name] = {"available": version is not None, "version": version, "expected": expected}

    runtime_packages = (env_info or {}).get("packages", {})
    package_checks: dict[str, Any] = {}
    runtime_ok = bool(env_info and source_matches)
    if project_name == "corral":
        for name in _PROJECTS[project_name]["runtime"]:
            found = runtime_packages.get(name)
            package_checks[name] = {"installed": found is not None, "version": found.get("version") if found else None}
            runtime_ok = runtime_ok and found is not None
        pin = _sesskit_pin()
        sesskit = runtime_packages.get("sesskit")
        pin_match = bool(pin and sesskit and sesskit.get("version") == pin["version"])
        artifact_match = False
        installed_sha256 = None
        if pin_match and sesskit:
            direct_url = sesskit.get("direct_url") or {}
            archive = direct_url.get("archive_info", {})
            hashes = archive.get("hashes", {})
            installed_sha256 = hashes.get("sha256")
            # uv validates the supplied #sha256 during prepare but omits it
            # from PEP 610 metadata. If a tool records a digest, require an
            # exact match; otherwise the exact source URL plus the prepare log
            # proves which digest-checked artifact was installed.
            artifact_match = (
                direct_url.get("url", "") == pin["url"]
                and installed_sha256 in (None, pin["sha256"])
            )
        package_checks["sesskit_pin"] = {
            "expected_version": pin["version"] if pin else None,
            "expected_sha256": pin["sha256"] if pin else None,
            "version_matches": pin_match,
            "published_artifact_matches": artifact_match,
            "digest_recorded_in_install_metadata": bool(installed_sha256) if sesskit else False,
        }
        runtime_ok = runtime_ok and pin_match and artifact_match

    lock = _lock_status(repo, project_name, uv_path)
    warnings: list[str] = []
    if lock["state"] not in {"current", "missing"}:
        warnings.append("uv.lock is stale or cannot be resolved without network; prepare uses the existing pinned resolution")

    tmux_path = shutil.which("tmux") if project_name == "corral" else None
    if project_name == "corral":
        tools["tmux"] = {"available": bool(tmux_path), "path": Path(tmux_path).name if tmux_path else None}
    pip_check_ok: bool | None = None
    if venv_python.is_file():
        try:
            pip_check = subprocess.run(
                [str(uv_path), "pip", "check", "--python", str(venv_python)] if uv_path else [str(venv_python), "-m", "pip", "check"],
                cwd=repo,
                env=_uv_env(repo),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            pip_check_ok = pip_check.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pip_check_ok = False

    tools_ok = bool(uv_path) and all(
        item["available"] and item["version"] == item["expected"]
        for item in tools.values()
        if "expected" in item
    )
    if project_name == "corral":
        tools_ok = tools_ok and bool(tmux_path)
    ready = bool(venv_python.is_file() and env_info and source_matches and runtime_ok and tools_ok and pip_check_ok)
    blockers: list[str] = []
    if not uv_path:
        blockers.append("uv is unavailable")
    if not venv_python.is_file():
        blockers.append("checkout .venv is missing")
    if env_info and env_info.get("probe_error"):
        blockers.append("checkout interpreter probe failed")
    if env_info and not source_matches:
        blockers.append("imported package does not resolve to this checkout's src tree")
    if not runtime_ok:
        blockers.append("one or more declared runtime packages or SessKit artifact pins mismatch")
    missing_tools = [name for name, item in tools.items() if item.get("expected") and (not item["available"] or item["version"] != item["expected"])]
    if missing_tools:
        blockers.append("required development tools missing or version-mismatched: " + ", ".join(missing_tools))
    if project_name == "corral" and not tmux_path:
        blockers.append("tmux is unavailable for Corral integration tests")
    if pip_check_ok is False:
        blockers.append("installed package requirements are inconsistent")
    data = {
        "summary": f"{'Ready' if ready else 'Needs preparation'}: {project_name} {project_version} ({'environment ready' if ready else 'dependencies or tools missing/mismatched'}).",
        "repo": repo.name,
        "project": project_name,
        "project_version": project_version,
        "ready": ready,
        "interpreter": {
            "path": str(venv_python) if venv_python.is_file() else None,
            "version": env_info.get("python_version") if env_info else None,
            "prefix": env_info.get("prefix") if env_info else None,
        },
        "source": {"expected": f"src/{package}/__init__.py", "matches_checkout": source_matches},
        "tools": tools,
        "packages": package_checks,
        "dependency_check": pip_check_ok,
        "lock": lock,
        "warnings": warnings,
        "blockers": blockers,
    }
    return data, ready


def _build_parser() -> argparse.ArgumentParser:
    parser = CliParser(description="Prepare and run tests in a selected Corral or SessKit checkout.")
    parser.add_argument("--json", action="store_true", help="emit the stable JSON envelope")
    subparsers = parser.add_subparsers(
        dest="action", required=True, parser_class=CliParser,
    )
    for action, help_text in (
        ("doctor", "Read-only environment and dependency diagnostics"),
        ("check", "Concise dependency readiness check before tests"),
        ("prepare", "Create or update the checkout-owned environment"),
    ):
        command = subparsers.add_parser(action, help=help_text)
        command.add_argument("--json", action="store_true", dest="json_command", help=argparse.SUPPRESS)
        command.add_argument("--repo", required=True, help="explicit Corral or SessKit checkout path")
        command.json_requested = False
        if action == "prepare":
            command.add_argument("--python", help="interpreter used to create a new .venv (default: this CLI interpreter)")
    run = subparsers.add_parser("run", help="run a command with this checkout's source and .venv")
    run.add_argument("--json", action="store_true", dest="json_command", help=argparse.SUPPRESS)
    run.add_argument("--repo", required=True, help="explicit Corral or SessKit checkout path")
    run.add_argument("command", nargs=argparse.REMAINDER, help="command and arguments, after --")
    run.json_requested = False
    return parser


def _json_mode(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False) or getattr(args, "json_command", False))


def _error(code: str, message: str, *, log_path: str | None = None, next_commands: list[str] | None = None) -> dict[str, Any]:
    details: dict[str, Any] = {"code": code, "message": message}
    if log_path:
        details["log_path"] = log_path
    if next_commands:
        details["next_commands"] = next_commands
    return envelope(error=details)


def _prepare(repo: Path, project_name: str, python: str | None) -> tuple[bool, Path | None, str]:
    uv_path = shutil.which("uv")
    if not uv_path:
        return False, None, "uv is unavailable; install the official uv tool, then rerun prepare"
    venv = _venv_dir(repo)
    venv_python = _venv_python(repo)
    if venv.is_symlink():
        return False, None, ".venv is a symlink; refusing to modify an environment outside this checkout"
    if venv.exists() and not venv_python.is_file():
        return False, None, ".venv exists but is not a recognized virtual environment; move it yourself before preparing"
    env = _uv_env(repo, python=python)
    logs: list[str] = []
    if not venv_python.is_file():
        create = subprocess.run(
            [uv_path, "--no-config", "venv", "--python", python or sys.executable, str(venv)],
            cwd=repo,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if create.returncode:
            failure_log = repo / f".venv-create-{os.getpid()}.log"
            failure_log.write_text(
                f"uv venv exited {create.returncode}\n{create.stdout}{create.stderr}", encoding="utf-8"
            )
            return False, failure_log, f"uv could not create the checkout environment: {(create.stderr or create.stdout).strip()[:300]}"
        initial_log = _new_log_path(repo, "venv")
        initial_log.parent.mkdir(parents=True, exist_ok=True)
        initial_log.write_text(f"argv0=uv argc=4\ncwd={repo}\n\n{create.stdout}{create.stderr}", encoding="utf-8")
        logs.append(_relative_log(initial_log, repo))

    if project_name == "sesskit":
        sync = [uv_path, "--no-config", "sync", "--locked", "--group", "dev", "--project", str(repo)]
    else:
        sync = [uv_path, "--no-config", "sync", "--frozen", "--extra", "remote", "--no-install-project", "--project", str(repo)]
    code, log_path, tail = _run_logged(sync, repo=repo, env=env)
    logs.append(_relative_log(log_path, repo))
    if code:
        return False, log_path, f"dependency sync failed (exit {code}); {tail[-400:]}"

    if project_name == "corral":
        pin = _sesskit_pin()
        if not pin:
            return False, log_path, "the repository-pinned SessKit artifact could not be read"
        requirement = f"sesskit @ {pin['url']}#sha256={pin['sha256']}"
        install = [uv_path, "pip", "install", "--python", str(_venv_python(repo)), f"ruff=={RUFF_VERSION}", requirement]
        code, log_path, tail = _run_logged(install, repo=repo, env=env)
        logs.append(_relative_log(log_path, repo))
        if code:
            return False, log_path, f"pinned lint/SessKit artifact install failed (exit {code}); {tail[-400:]}"
    summary = f"Prepared {project_name} using its locked dependencies and checkout .venv. Logs: {', '.join(logs)}"
    return True, log_path, summary


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    raw_args = argv if argv is not None else sys.argv[1:]
    json_requested = "--json" in raw_args
    parser.json_requested = json_requested
    for subparser in parser._subparsers._group_actions[0].choices.values():
        subparser.json_requested = json_requested
    args = parser.parse_args(argv)
    json_mode = _json_mode(args)
    try:
        repo, project_name, _project_version = validate_repo(args.repo)
    except (OSError, ValueError) as exc:
        return emit(_error("invalid_repo", str(exc)), 2, json_mode=json_mode)

    if args.action in {"doctor", "check"}:
        data, ready = doctor(str(repo))
        if args.action == "check":
            data = {
                "summary": data["summary"],
                "project": data["project"],
                "ready": data["ready"],
                "missing_tools": [name for name, info in data["tools"].items() if info.get("expected") and not info["available"]],
                "dependency_check": data["dependency_check"],
                "lock_state": data["lock"]["state"],
                "warnings": data["warnings"],
                "blockers": data["blockers"],
            }
        return emit(envelope(data), 0 if ready else 1, json_mode=json_mode)

    if args.action == "prepare":
        ok, log_path, summary = _prepare(repo, project_name, args.python)
        if not ok:
            return emit(_error("prepare_failed", summary, log_path=_relative_log(log_path, repo) if log_path else None), 1, json_mode=json_mode)
        data, ready = doctor(str(repo))
        data["summary"] = summary if ready else f"Prepared {project_name}, but readiness checks still fail."
        data["log_path"] = _relative_log(log_path, repo) if log_path else None
        if not ready:
            return emit(envelope(error={"code": "not_ready", "message": data["summary"], "log_path": data["log_path"], "diagnostics": data}), 1, json_mode=json_mode)
        return emit(envelope(data), 0, json_mode=json_mode)

    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        return emit(_error("usage_error", "run requires a command after --"), 2, json_mode=json_mode)
    readiness, ready = doctor(str(repo))
    if not ready:
        return emit(
            _error("not_ready", "prepare this checkout and resolve the reported dependency/tool mismatches before running tests", next_commands=[f"python {SCRIPT_ROOT / 'scripts' / 'dev_env.py'} prepare --repo {shlex.quote(str(repo))}"]),
            1,
            json_mode=json_mode,
        )
    python = str(_venv_python(repo))
    if command[0] in {"python", "python3"}:
        command[0] = python
    elif command[0] in {"ruff", "pytest"}:
        tool_path = _venv_dir(repo) / ("Scripts" if os.name == "nt" else "bin") / (command[0] + (".exe" if os.name == "nt" else ""))
        if tool_path.exists():
            command[0] = str(tool_path)
    env = _python_env(repo)
    if project_name == "corral":
        env["CORRAL_ISOLATE_MANAGED_HOSTS"] = "1"
    code, log_path, tail = _run_logged(command, repo=repo, env=env)
    data = {
        "summary": f"Command {'passed' if code == 0 else f'failed with exit {code}'}; full output is in the log.",
        "project": project_name,
        "exit_code": code,
        "log_path": _relative_log(log_path, repo),
        "tail": tail,
    }
    if code:
        return emit(envelope(error={"code": "command_failed", "message": data["summary"], "log_path": data["log_path"], "exit_code": code, "tail": tail}), 1, json_mode=json_mode)
    return emit(envelope(data), 0, json_mode=json_mode)


if __name__ == "__main__":
    raise SystemExit(main())
