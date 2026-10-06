#!/usr/bin/env python3
"""Dependency-free maintainer operations; never a product or test-pass cache."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

VERSION = "1.0.0"
JSON_OUTPUT = not sys.stdout.isatty()
IDENTIFIER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}\Z")
SECRET = re.compile(r"(?i)(?:--|\b)(?:password|token|secret|api[-_]key)(?:=|\s|$)")


class Failure(Exception):
    def __init__(self, code, message, exit_code=1, **detail):
        super().__init__(message)
        self.code, self.exit_code, self.detail = code, exit_code, detail


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("usage_error", message, 2)


def emit(data=None, error=None):
    if not JSON_OUTPUT:
        if error:
            print(f"{error['code']}: {error['message']}")
        elif data.get("run_id"):
            print(f"Operation {data['run_id']}: {data.get('status', 'recorded')}")
            if data.get("receipt_path"):
                print(f"Receipt: {data['receipt_path']}")
        elif data.get("task"):
            print(f"{data['task']}: {data['goal']}\nNext: {data['next']}")
        else:
            for key, value in data.items():
                print(f"{key}: {value}")
        return
    print(
        json.dumps(
            {"ok": error is None, "data": data, "error": error, "meta": {"version": 1, "tool_version": VERSION}},
            ensure_ascii=False,
        )
    )


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise Failure("usage_error", "Use a simple name without slashes or whitespace.", 2)
    return value


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".receipt-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise Failure("not_found", "No saved record exists at this path.", 3) from exc
    except (ValueError, OSError) as exc:
        raise Failure("invalid_record", "Saved record is unreadable; do not replay it.") from exc


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if result.returncode:
        raise Failure("environment", "Cannot inspect the selected Git checkout.")
    return result.stdout


def checkout(value):
    repo = Path(value).expanduser().resolve()
    if not repo.is_dir():
        raise Failure("not_found", "Repository directory does not exist.", 3)
    top = Path(os.fsdecode(git(repo, "rev-parse", "--show-toplevel")).strip()).resolve()
    if top != repo:
        raise Failure("usage_error", "Select the component repository root with --repo.", 2)
    return repo


def identity(repo, steps=()):
    digest = hashlib.sha256()
    files = sorted(
        set(git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"))
        - {b"", b"TASKBOARD.md"}
    )
    for name in files:
        path = repo / os.fsdecode(name)
        digest.update(name + b"\0")
        if path.is_symlink():
            digest.update(b"link:" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(str(path.stat().st_mode & 0o777).encode())
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            digest.update(b"missing")
    tools = []
    commands = [s[k] for s in steps for k in ("argv", "verify_argv") if s.get(k)]
    for argv in commands:
        resolved = str(repo / argv[0]) if "/" in argv[0] and not Path(argv[0]).is_absolute() else shutil.which(argv[0])
        if resolved:
            path = Path(resolved).resolve()
            stat = path.stat()
            tools.append([str(path), stat.st_size, stat.st_mtime_ns])
        else:
            tools.append([argv[0], None])
    packages = hashlib.sha256()
    for metadata in sorted((repo / ".venv").glob("lib/python*/site-packages/*.dist-info/METADATA")):
        packages.update(metadata.name.encode() + str(metadata.parent.name).encode() + metadata.read_bytes())
    return {
        "repo": str(repo),
        "commit": git(repo, "rev-parse", "HEAD").decode().strip(),
        "source_sha256": digest.hexdigest(),
        "python": sys.executable,
        "python_version": sys.version,
        "tools": tools,
        "checkout_packages_sha256": packages.hexdigest(),
    }


def state_root(args):
    if args.state_dir:
        return Path(args.state_dir).expanduser().resolve()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")).expanduser()
    if not config.is_absolute():
        raise Failure("usage_error", "XDG_CONFIG_HOME must be an absolute directory.", 2)
    return config / "corral/development"


def repo_state(root, repo):
    return root / "checkouts" / hashlib.sha256(str(repo).encode()).hexdigest()[:20]


def resource_status(root, resource):
    name = identifier(resource)
    path = root / "resources" / (name + ".lock")
    if not path.exists():
        return {"resource": name, "busy": False, "owner": None}
    with path.open("rb") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner_path = path.with_suffix(".json")
            owner = read(owner_path) if owner_path.exists() else None
            return {"resource": name, "busy": True, "owner": owner}
    return {"resource": name, "busy": False, "owner": None}


@contextlib.contextmanager
def resource_lock(root, resource, owner):
    if not resource:
        yield None
        return
    path = root / "resources" / (identifier(resource) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as stream:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Failure(
                "resource_busy",
                "Resource is in use; inspect its owner, do not remove the lock.",
                5,
                resource=resource_status(root, resource),
            ) from exc
        atomic(path.with_suffix(".json"), owner)
        # Do not unlink or explicitly unlock: an inherited child descriptor may still own it.
        yield stream.fileno()


def command(argv):
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x and "\0" not in x for x in argv):
        raise Failure("usage_error", "Commands must be nonempty arrays of arguments.", 2)
    if any(SECRET.search(x) for x in argv):
        raise Failure("usage_error", "Keep credentials out of argv; use the existing tool's credential store.", 2)
    return argv


def plan(args):
    if args.plan:
        data = read(Path(args.plan).expanduser())
        if not isinstance(data, dict) or set(data) != {"steps"}:
            raise Failure("usage_error", "Plan must contain only a steps array.", 2)
        steps = data["steps"]
    else:
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        steps = [
            {
                "name": "command",
                "argv": command(argv),
                "resource": args.resource,
                "timeout": args.timeout,
                "retry_safe": False,
            }
        ]
    if not isinstance(steps, list) or not steps:
        raise Failure("usage_error", "Plan must have at least one step.", 2)
    names = set()
    for step in steps:
        if not isinstance(step, dict) or set(step) - {
            "name",
            "argv",
            "verify_argv",
            "resource",
            "timeout",
            "retry_safe",
        }:
            raise Failure("usage_error", "Unknown step fields; see describe.", 2)
        name = identifier(step.get("name", ""))
        if name in names:
            raise Failure("usage_error", "Step names must be unique.", 2)
        names.add(name)
        command(step.get("argv"))
        if step.get("verify_argv") is not None:
            command(step["verify_argv"])
        if step.get("resource"):
            identifier(step["resource"])
        if not isinstance(step.get("retry_safe", False), bool):
            raise Failure("usage_error", "retry_safe must be boolean.", 2)
        timeout = step.get("timeout", 1800)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400:
            raise Failure("usage_error", "Step timeout must be between 0 and 86400 seconds.", 2)
    return steps


def execute(argv, repo, log, timeout, lock_fd):
    process = subprocess.Popen(
        argv,
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
        pass_fds=() if lock_fd is None else (lock_fd,),
    )

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        return process.wait(timeout=timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise
    finally:
        signal.signal(signal.SIGTERM, previous)


def worker(root, path):
    record = read(path)
    # The submitter writes the child PID before handing off receipt ownership.
    deadline = time.monotonic() + 5
    while record.get("worker_pid") != os.getpid():
        if time.monotonic() >= deadline:
            raise Failure("handoff_failed", "Worker did not receive its submission identity.")
        time.sleep(0.02)
        record = read(path)
    verify_plan(record)
    repo = Path(record["identity"]["repo"])
    owner = {"run_id": record["run_id"], "repo": str(repo), "pid": os.getpid(), "started": time.time()}
    with resource_lock(root, "run-" + record["run_id"], owner):
        record = read(path)
        record["receipt_path"] = str(path)
        record.update(status="running", worker_pid=os.getpid())
        atomic(path, record)
        try:
            for index, step in enumerate(record["steps"]):
                state = record["results"][index]
                if state["status"] == "succeeded":
                    continue
                if identity(repo, record["steps"]) != record["identity"]:
                    raise Failure("source_drift", "Source, commit or tool identity changed; create a new plan.", 5)
                with resource_lock(root, step.get("resource"), {**owner, "step": step["name"]}) as fd:
                    state.update(status="running", started=time.time())
                    atomic(path, record)
                    log_path = path.parent / (step["name"] + ".log")
                    state["log_path"] = str(log_path)
                    with log_path.open("ab") as log:
                        os.chmod(log_path, 0o600)
                        rc = execute(step["argv"], repo, log, step.get("timeout", 1800), fd)
                        state.update(exit_code=rc, status="failed" if rc else "executed")
                        atomic(path, record)
                        if rc:
                            raise Failure("command_failed", "Step failed; inspect its full log.", step=step["name"])
                        if step.get("verify_argv"):
                            # A failed verifier must never cause an execution to be replayed.
                            state["verification"] = "running"
                            atomic(path, record)
                            rc = execute(step["verify_argv"], repo, log, step.get("timeout", 1800), fd)
                            state["verification"] = "passed" if rc == 0 else "failed"
                            atomic(path, record)
                            if rc:
                                raise Failure(
                                    "verification_failed",
                                    "Execution finished but verification failed; reconcile before replay.",
                                )
                        else:
                            state["verification"] = "not_requested"
                    if identity(repo, record["steps"]) != record["identity"]:
                        raise Failure(
                            "source_drift", "Workspace changed during execution; result cannot verify the new state.", 5
                        )
                    state.update(status="succeeded", finished=time.time())
                    atomic(path, record)
            record["status"] = "succeeded"
        except Failure as exc:
            record.update(status="failed", error={"code": exc.code, "message": str(exc), **exc.detail})
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            record.update(status="unknown", error={"code": "timeout_or_interruption", "message": type(exc).__name__})
        except Exception as exc:
            record.update(status="unknown", error={"code": "execution_unknown", "message": type(exc).__name__})
        finally:
            record["updated"] = time.time()
            atomic(path, record)
    return record


def status(root, path):
    record = read(path)
    if record["status"] in {"queued", "running"}:
        lock = resource_status(root, "run-" + record["run_id"])
        pid = record.get("worker_pid")
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                pass
        # Only queued handoff gets a short grace period; PID existence is not resource ownership.
        grace = record["status"] == "queued" and alive and time.time() - record.get("submitted", 0) < 5
        if not lock["busy"] and not grace:
            record["status"] = "unknown"
            record["error"] = {
                "code": "worker_missing",
                "message": "Worker stopped without a terminal receipt; do not replay blindly.",
            }
    record["receipt_path"] = str(path)
    return record


def launch_inner(args, root, repo):
    base = repo_state(root, repo)
    if args.resume:
        path = base / "runs" / identifier(args.resume) / "receipt.json"
        record = status(root, path)
        verify_plan(record)
        if record["status"] in {"running", "queued", "unknown"}:
            raise Failure("conflict", "Operation is active or uncertain; inspect status and reconcile.", 5)
        if identity(repo, record["steps"]) != record["identity"]:
            raise Failure("source_drift", "Source or tool identity changed; use a new plan.", 5)
        if record["status"] == "succeeded":
            return record
        for step, result in zip(record["steps"], record["results"], strict=True):
            if result["status"] == "succeeded":
                continue
            if result["status"] != "pending" and not (result["status"] == "failed" and step.get("retry_safe")):
                raise Failure("reconcile_required", "This step may have side effects; resume will not replay it.", 5)
    else:
        steps = plan(args)
        source = identity(repo, steps)
        run_id = uuid.uuid4().hex
        path = base / "runs" / run_id / "receipt.json"
        record = {
            "run_id": run_id,
            "identity": source,
            "steps": steps,
            "plan_sha256": plan_digest(steps),
            "status": "queued",
            "created": time.time(),
            "results": [{"name": s["name"], "status": "pending"} for s in steps],
        }
    if args.require_clean and git(repo, "status", "--porcelain"):
        raise Failure("uncommitted_source", "Commit the entire releasable workspace before delivery.", 5)
    if args.dry_run:
        return {**record, "dry_run": True, "receipt_path": str(path)}
    record.update(status="queued", error=None, worker_pid=os.getpid(), submitted=time.time())
    atomic(path, record)
    if args.background:
        argv = [sys.executable, str(Path(__file__).resolve()), "--state-dir", str(root), "_worker", str(path)]
        with (path.parent / "worker.log").open("ab") as log:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        record["worker_pid"] = process.pid
        atomic(path, record)
        return {
            "run_id": record["run_id"],
            "status": "submitted",
            "worker_pid": process.pid,
            "receipt_path": str(path),
            "next_command": [
                sys.executable,
                str(Path(__file__).resolve()),
                "--state-dir",
                str(root),
                "status",
                "--repo",
                str(repo),
                "--run",
                record["run_id"],
            ],
        }
    return worker(root, path)


def plan_digest(steps):
    return hashlib.sha256(json.dumps(steps, sort_keys=True).encode()).hexdigest()


def verify_plan(record):
    if record.get("plan_sha256") != plan_digest(record["steps"]):
        raise Failure("plan_changed", "Saved command plan changed; create a new operation.", 5)


def launch(args, root, repo):
    if not args.resume or args.dry_run:
        return launch_inner(args, root, repo)
    owner = {"pid": os.getpid(), "repo": str(repo), "started": time.time()}
    # Serialize concurrent resume submissions separately from the executing worker.
    with resource_lock(root, "submit-" + identifier(args.resume), owner):
        return launch_inner(args, root, repo)


def doctor(repo):
    component = (
        "apple" if (repo / "project.yml").exists() else "cli" if (repo / "scripts/dev_env.py").exists() else "other"
    )
    required = {
        "apple": ["git", "python3", "xcodebuild", "xcodegen", "swift"],
        "cli": ["git", "python3", "uv", "tmux"],
        "other": ["git", "python3"],
    }[component]
    tools = {name: shutil.which(name) for name in required}
    dirty = git(repo, "status", "--porcelain").decode().splitlines()
    next_commands = []
    if component == "cli":
        next_commands = [[sys.executable, str(repo / "scripts/dev_env.py"), "doctor", "--repo", str(repo), "--json"]]
    elif component == "apple":
        next_commands = [
            [sys.executable, str(repo / "scripts/client_diag.py"), "--json", "doctor"],
            ["ios-deliver", "--json", "doctor"],
        ]
    checks = []
    for argv in next_commands:
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=20, cwd=repo)
            envelope = json.loads(result.stdout)
            details = envelope.get("data") or {}
            selected = {
                key: value
                for key, value in details.items()
                if key in {"ready", "summary", "blockers", "warnings", "missingTools", "reportsAvailable", "tools"}
            }
            checks.append(
                {
                    "command": argv,
                    "exit_code": result.returncode,
                    "ok": envelope.get("ok") is True,
                    "data": selected,
                    "error": envelope.get("error"),
                }
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            checks.append({"command": argv, "ok": False, "error": type(exc).__name__})
    return {
        "repo": str(repo),
        "component": component,
        "tools": tools,
        "missing_tools": [k for k, v in tools.items() if not v],
        "changed_files": len(dirty),
        "source_committed": not dirty,
        "tool_prerequisites_present": all(tools.values()),
        "ready_for_delivery": False,
        "next_commands": next_commands,
        "checks": checks,
        "behavior_verified": False,
        "capture_and_device_status": "use platform diagnostics; not inferred from tool presence",
    }


def parser():
    p = Parser(description=__doc__)
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--state-dir", help="Override isolated state root (tests or separate profiles).")
    p.add_argument("--json", action="store_true", help="Use JSON output (also the non-TTY default).")
    sub = p.add_subparsers(dest="action", required=True, parser_class=Parser)
    sub.add_parser("describe")
    d = sub.add_parser("doctor")
    d.add_argument("--repo", required=True)
    r = sub.add_parser("run")
    r.add_argument("--repo", required=True)
    mode = r.add_mutually_exclusive_group()
    mode.add_argument("--plan")
    mode.add_argument("--resume")
    r.add_argument("--resource", help="Machine-wide named resource, e.g. corral-mac-acceptance.")
    r.add_argument("--timeout", type=float, default=1800)
    r.add_argument("--background", action="store_true")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--require-clean", action="store_true")
    r.add_argument("argv", nargs=argparse.REMAINDER)
    s = sub.add_parser("status")
    s.add_argument("--repo", required=True)
    s.add_argument("--run", required=True)
    resources = sub.add_parser("resources")
    resources.add_argument("--resource", required=True)
    c = sub.add_parser("checkpoint")
    c.add_argument("--repo", required=True)
    c.add_argument("--task", required=True)
    c.add_argument("--goal")
    c.add_argument("--next")
    c.add_argument("--evidence", action="append", default=[])
    c.add_argument("--dry-run", action="store_true")
    w = sub.add_parser("_worker", help=argparse.SUPPRESS)
    w.add_argument("receipt")
    for child in sub.choices.values():
        child.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    return p


def main():
    global JSON_OUTPUT
    try:
        args = parser().parse_args()
        JSON_OUTPUT = args.json or not sys.stdout.isatty()
        root = state_root(args)
        if args.action == "describe":
            result = {
                "commands": {
                    "doctor": "read",
                    "resources": "read",
                    "status": "read",
                    "checkpoint": "read or write_local when --goal is present",
                    "run": "executes supplied argv; --dry-run is read-only",
                },
                "plan": {
                    "steps": [
                        {
                            "name": "unique-name",
                            "argv": ["tool", "argument"],
                            "verify_argv": ["tool", "verify"],
                            "resource": "optional-name",
                            "timeout": 1800,
                            "retry_safe": False,
                        }
                    ]
                },
                "state_dir": str(root),
                "exit_codes": {"success": 0, "failed": 1, "usage": 2, "missing": 3, "conflict": 5},
                "notes": (
                    "No shell interpolation. No automatic retry of uncertain writes. "
                    "Logs are local; keep credentials in Keychain or existing tools."
                ),
            }
            result["arguments"] = {
                name: [
                    {"flags": action.option_strings or [action.dest], "required": action.required, "help": action.help}
                    for action in child._actions
                    if action.dest != "help"
                ]
                for group in parser()._actions
                if isinstance(group, argparse._SubParsersAction)
                for name, child in group.choices.items()
                if not name.startswith("_")
            }
        elif args.action == "resources":
            result = resource_status(root, args.resource)
        elif args.action == "_worker":
            result = worker(root, Path(args.receipt))
        else:
            repo = checkout(args.repo)
            if args.action == "doctor":
                result = doctor(repo)
            elif args.action == "status":
                result = status(root, repo_state(root, repo) / "runs" / identifier(args.run) / "receipt.json")
            elif args.action == "run":
                if (args.plan or args.resume) and (args.argv or args.resource or args.timeout != 1800):
                    raise Failure(
                        "usage_error", "Plan/resume cannot be combined with argv, resource or timeout overrides.", 2
                    )
                result = launch(args, root, repo)
            else:
                path = repo_state(root, repo) / "checkpoints" / (identifier(args.task) + ".json")
                if args.goal is None:
                    if args.next or args.evidence:
                        raise Failure("usage_error", "Checkpoint writes require --goal and --next.", 2)
                    result = read(path)
                    result["current_identity"] = identity(repo)
                    result["source_changed"] = result["identity"] != result["current_identity"]
                else:
                    if not args.next or SECRET.search(args.goal + args.next):
                        raise Failure(
                            "usage_error", "Provide a next action and keep credentials out of checkpoints.", 2
                        )
                    result = {
                        "task": args.task,
                        "goal": args.goal,
                        "next": args.next,
                        "evidence": args.evidence,
                        "identity": identity(repo),
                        "updated": time.time(),
                    }
                    old = read(path) if path.exists() else None
                    unchanged = old is not None and all(
                        old.get(key) == result[key] for key in ("task", "goal", "next", "evidence", "identity")
                    )
                    if unchanged:
                        result = old
                    elif not args.dry_run:
                        atomic(path, result)
                    result["dry_run"] = args.dry_run
                    result["changed"] = not unchanged and not args.dry_run
                    result["would_change"] = not unchanged
        failed = result.get("status") in {"failed", "unknown"}
        if failed:
            error = result.get("error") or {"code": "failed", "message": "Inspect the operation receipt."}
            emit(
                error={
                    **error,
                    "run_id": result["run_id"],
                    "status": result["status"],
                    "receipt_path": result.get("receipt_path"),
                    "results": result["results"],
                }
            )
        else:
            emit(result)
        if failed:
            return 5 if error["code"] in {"resource_busy", "source_drift", "conflict"} else 1
        return 0
    except Failure as exc:
        emit(error={"code": exc.code, "message": str(exc), **exc.detail})
        return exc.exit_code
    except (OSError, ValueError, TypeError, KeyError) as exc:
        emit(
            error={
                "code": "environment",
                "message": type(exc).__name__,
                "hint": "Inspect inputs and local filesystem access; no automatic repair was performed.",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
