#!/usr/bin/env python3
"""One-command Corral release for the CLI, Mac and iPhone clients.

Dependency-free maintainer tool. Every command prints one compact JSON envelope so an agent
can drive a release in a handful of calls; full logs stay in the release workspace.
Procedure and decisions: docs/RELEASE_RUNBOOK.md.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CLI = Path(__file__).resolve().parents[1]
WORKSPACE = CLI.parent
APPLE = WORKSPACE / "apple"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
RELEASES = CONFIG_HOME / "corral/development/releases"
PRIVATE = CONFIG_HOME / "corral/release"
AGENT_WORKFLOW = CLI / "scripts/agent_workflow.py"
CLI_REPO = "corral-dev/corral"
MAC_PUBLIC_REPO = "corral-dev/corral-apple"
TAP_FORMULA = "x0c/tap/corral"
REQUIRED_WHEELS = 5
WAIT_SECONDS = 540
RESOURCE_WAIT_SECONDS = 3600
EXCERPT_LINES = 20

COMPONENTS = ("cli", "mac", "ios")
PRODUCT_PATHS = {
    "cli": ("src/", "rust/", "pyproject.toml", "Cargo.toml", "Cargo.lock", "uv.lock", "install.sh"),
    "mac": ("Shared/", "macOS/", "Configuration/", "project.yml", "Package.swift"),
    "ios": ("Shared/", "iOS/", "NotificationService/", "Configuration/", "project.yml", "Package.swift"),
}
CLI_VERSION_FILES = {
    "pyproject.toml": 'version = "{}"',
    "Cargo.toml": 'version = "{}"',
    "Cargo.lock": 'name = "corral-native"\nversion = "{}"',
    "uv.lock": 'name = "corral"\nversion = "{}"',
    "src/corral/__init__.py": '__version__ = "{}"',
}
# The iPhone version is the project-wide setting; the Mac target overrides it one level deeper.
APPLE_VERSION_PATTERNS = {
    "ios": re.compile(r'^    MARKETING_VERSION: "([0-9.]+)"\n    CURRENT_PROJECT_VERSION: "([0-9]+)"', re.M),
    "mac": re.compile(r'^        MARKETING_VERSION: "([0-9.]+)"\n        CURRENT_PROJECT_VERSION: "([0-9]+)"', re.M),
}
LANES = {
    "cli": ["cli.env", "cli.gate", "cli.build", "cli.linux", "cli.tag", "cli.publish", "cli.verify", "cli.install"],
    "ios": ["ios.build", "ios.deliver", "ios.push"],
    "mac": ["mac.export", "mac.public", "mac.analyze", "mac.prepare", "mac.install", "mac.publish"],
}
# Phases with external effects; rehearsals skip them.
PUBLISHING = {"cli.tag", "cli.publish", "cli.verify", "cli.install", "ios.deliver", "ios.push",
              "mac.install", "mac.publish"}
EXCERPT = re.compile(r"(?i)(error|fail|traceback|exception|warning:|denied|timed out|refus|错误|失败)")


class Failure(Exception):
    def __init__(self, message: str, *, log: Path | None = None, excerpt: list[str] | None = None, hint: str = ""):
        super().__init__(message)
        self.log, self.excerpt, self.hint = log, excerpt or [], hint


# ---------- output ----------

def emit(data=None, error=None) -> None:
    print(json.dumps({"ok": error is None, "data": data, "error": error}, ensure_ascii=False, separators=(",", ":")))


def excerpt(log: Path, limit: int = EXCERPT_LINES) -> list[str]:
    """The lines that most likely explain a failure, so agents rarely open whole logs."""
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return []
    hits = [line.strip()[:300] for line in lines if EXCERPT.search(line) and not line.lstrip().startswith("export ")]
    tail = [line.strip()[:300] for line in lines[-8:]]
    picked = list(dict.fromkeys(hits[-(limit - len(tail)):] + tail))
    return picked[-limit:]


# ---------- versions and notes (pure helpers) ----------

def next_version(version: str, step: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    if step == "major":
        return f"{major + 1}.0.0"
    if step == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def cli_version(repo: Path) -> str:
    return re.search(r'^version = "([^"]+)"', (repo / "pyproject.toml").read_text(), re.M).group(1)


def apple_versions(project: str) -> dict[str, tuple[str, int]]:
    versions = {}
    for name, pattern in APPLE_VERSION_PATTERNS.items():
        matches = pattern.findall(project)
        if len(matches) != 1:
            raise Failure(f"project.yml must hold exactly one {name} version block")
        versions[name] = (matches[0][0], int(matches[0][1]))
    return versions


def bump_cli_files(repo: Path, old: str, new: str) -> list[str]:
    changed = []
    for name, template in CLI_VERSION_FILES.items():
        path = repo / name
        text = path.read_text()
        if text.count(template.format(old)) < 1:
            raise Failure(f"{name} does not declare version {old}")
        path.write_text(text.replace(template.format(old), template.format(new), 1))
        changed.append(name)
    return changed


def bump_apple_project(project: str, targets: dict[str, tuple[str, int]]) -> str:
    for name, (version, build) in targets.items():
        prefix = "    " if name == "ios" else "        "
        replacement = f'{prefix}MARKETING_VERSION: "{version}"\n{prefix}CURRENT_PROJECT_VERSION: "{build}"'
        project, count = APPLE_VERSION_PATTERNS[name].subn(replacement, project)
        if count != 1:
            raise Failure(f"project.yml {name} version block not found")
    return project


def unreleased(changelog: str) -> dict:
    """Read `## Unreleased`: notes readiness, requested step and hold."""
    match = re.search(r"^## Unreleased[ \t]*\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    if not match:
        return {"notes": "missing", "step": "patch", "hold": None, "body": ""}
    body = match.group(1)
    hold = re.search(r"^Release: hold\b[ \t]*(.*)$", body, re.M)
    step = re.search(r"^Release: (minor|major)\b", body, re.M)
    notes = re.sub(r"^Release: .*\n?", "", body, flags=re.M).strip()
    english = re.search(r"^### English\n(.*?)(?=^### |\Z)", notes, re.M | re.S)
    chinese = re.search(r"^### 简体中文\n(.*?)(?=^### |\Z)", notes, re.M | re.S)
    ready = bool(english and chinese and "- " in english.group(1) and "- " in chinese.group(1))
    return {"notes": "ready" if ready else "missing", "step": step.group(1) if step else "patch",
            "hold": hold.group(1).strip() or "unspecified" if hold else None, "body": notes + "\n"}


def promote_unreleased(changelog: str, heading: str) -> str:
    """Turn `## Unreleased` into the version heading, dropping release-control lines."""
    match = re.search(r"^## Unreleased[ \t]*\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    if not match:
        raise Failure("CHANGELOG.md has no ## Unreleased section")
    body = re.sub(r"^Release: .*\n?", "", match.group(1), flags=re.M).strip()
    return changelog[:match.start()] + f"## {heading}\n\n{body}\n\n" + changelog[match.end():].lstrip("\n")


def apple_heading(versions: dict[str, tuple[str, int]]) -> str:
    parts = []
    if "mac" in versions:
        parts.append("Mac {} ({})".format(*versions["mac"]))
    if "ios" in versions:
        parts.append("iPhone {} ({})".format(*versions["ios"]))
    return " / ".join(parts)


def changed_product_files(files: list[str], component: str) -> list[str]:
    prefixes = PRODUCT_PATHS[component]
    return [name for name in files if any(name == p or (p.endswith("/") and name.startswith(p)) for p in prefixes)]


# ---------- known issues ----------

WARNING_PATTERNS = (
    re.compile(r"^(?:.*/)?([^/\s:]+):\d+(?::\d+)?: warning: (.+)$"),
    re.compile(r"^\S+ \S+ ([A-Za-z_-]+)\[\d+:\d+\] warning: (.+)$"),
    re.compile(r"^(?:--- )?(xcodebuild): WARNING: (.+)$"),
    re.compile(r"^(cargo):warning=(.+)$"),
    re.compile(r"^(?:\S+ )?(warning): (.+)$"),
)


def warnings_in(text: str) -> list[str]:
    """Normalized, de-duplicated `origin: message` warnings; code-frame echoes are ignored."""
    found = []
    for raw in text.splitlines():
        line = raw.strip()
        for pattern in WARNING_PATTERNS:
            match = pattern.match(line)
            if match:
                found.append(f"{match.group(1)}: {match.group(2).strip()}")
                break
    return list(dict.fromkeys(found))


def load_known(paths: list[Path]) -> dict[str, list[dict]]:
    known = {"warnings": [], "test_failures": []}
    for path in paths:
        if path.exists():
            data = json.loads(path.read_text())
            for key in known:
                known[key] += data.get(key, [])
    return known


def unknown_warnings(warnings: list[str], known: list[dict], scope: str) -> tuple[list[str], list[str]]:
    rules = [entry for entry in known if scope in entry.get("scopes", [scope])]
    matched, new = [], []
    for warning in warnings:
        rule = next((entry for entry in rules if re.search(entry["match"], warning)), None)
        (matched if rule else new).append(rule["id"] if rule else warning)
    return list(dict.fromkeys(matched)), new


def unknown_failures(failed: list[str], known: list[dict], platform: str) -> tuple[list[str], list[str]]:
    ids = {entry["id"] for entry in known if entry.get("platform") == platform}
    # Subtests report as "test.id (param=...)"; the register names the test id.
    base = [(name, name.split(" (")[0]) for name in failed]
    return sorted({b for _, b in base if b in ids}), sorted(n for n, b in base if b not in ids)


def ci_report(log_text: str) -> dict | None:
    reports = [line.split("CORRAL_CI_TEST_REPORT:", 1)[1] for line in log_text.splitlines()
               if "CORRAL_CI_TEST_REPORT:" in line]
    return json.loads(reports[-1]) if reports else None


# ---------- process helpers ----------

def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode:
        raise Failure(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def run_logged(argv: list[str], log: Path, *, cwd: Path | None = None, env: dict | None = None,
               timeout: float = 3600, input_bytes: bytes | None = None) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as output:
        output.write(f"\n$ {' '.join(argv)}\n".encode())
        output.flush()
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                                input=input_bytes, timeout=timeout)
    return result.returncode


def must(argv: list[str], log: Path, message: str, **kwargs) -> None:
    if run_logged(argv, log, **kwargs):
        raise Failure(message, log=log, excerpt=excerpt(log))


def path_env(*front: Path, **values: str) -> dict:
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*(str(p) for p in front), env.get("PATH", "")])
    env.update(values)
    return env


def config() -> dict:
    path = PRIVATE / "config.json"
    return json.loads(path.read_text()) if path.exists() else {}


# ---------- planning ----------

def last_version_commit(repo: Path, needle: str, path: str) -> str | None:
    return git(repo, "log", "-1", "--format=%H", "-S", needle, "--", path, check=False) or None


def published(repo_slug: str, prefix: str) -> str | None:
    result = subprocess.run(["gh", "api", f"repos/{repo_slug}/releases/latest", "--jq", ".tag_name"],
                            capture_output=True, text=True, timeout=30)
    tag = result.stdout.strip()
    return tag[len(prefix):] if result.returncode == 0 and tag.startswith(prefix) else None


def component_plan(component: str, repo: Path, current: str, needle: str, needle_path: str, changelog: dict,
                   live: str | None) -> dict:
    since = last_version_commit(repo, needle, needle_path)
    files = git(repo, "diff", "--name-only", f"{since}..HEAD").splitlines() if since else []
    product = changed_product_files(files, component)
    subjects = git(repo, "log", "--format=%s", f"{since}..HEAD", "--", *PRODUCT_PATHS[component]).splitlines() \
        if since and product else []
    info = {"current": current, "next": next_version(current, changelog["step"]),
            "product_files": len(product), "notes": changelog["notes"], "commits": subjects[:30]}
    if changelog["hold"]:
        info["hold"] = changelog["hold"]
    if live is not None and live != current:
        info["published"] = live
    info["recommended"] = bool(product) and changelog["notes"] == "ready" and not changelog["hold"]
    return info


def plan() -> dict:
    cli_notes = unreleased((CLI / "CHANGELOG.md").read_text())
    apple_notes = unreleased((APPLE / "CHANGELOG.md").read_text())
    project = (APPLE / "project.yml").read_text()
    apple = apple_versions(project)
    current = cli_version(CLI)
    components = {
        "cli": component_plan("cli", CLI, current, f'version = "{current}"', "pyproject.toml", cli_notes,
                              published(CLI_REPO, "v")),
        "mac": component_plan("mac", APPLE, apple["mac"][0], f'MARKETING_VERSION: "{apple["mac"][0]}"',
                              "project.yml", apple_notes, published(MAC_PUBLIC_REPO, "mac-v")),
        "ios": component_plan("ios", APPLE, apple["ios"][0], f'MARKETING_VERSION: "{apple["ios"][0]}"',
                              "project.yml", apple_notes, None),
    }
    blockers = []
    # Uncommitted release files would be overwritten by the bump; block only the affected components.
    for repo, names, owners in ((CLI, [*CLI_VERSION_FILES, "CHANGELOG.md"], ("cli",)),
                                (APPLE, ["project.yml", "CHANGELOG.md"], ("mac", "ios"))):
        dirty = git(repo, "diff", "--name-only", "HEAD", "--", *names).split()
        for component in owners if dirty else ():
            components[component]["blocked"] = f"uncommitted {' '.join(dirty)} in {repo.name}"
            components[component]["recommended"] = False
    active = latest_state()
    if active and active["status"] == "running" and driver_alive(active):
        blockers.append(f"release {active['id']} is still running")
    return {"components": components, "recommended": [c for c in COMPONENTS if components[c]["recommended"]],
            "blockers": blockers}


# ---------- state ----------

@contextlib.contextmanager
def locked(workspace: Path):
    """Serialize read-modify-write of state.json across the per-lane driver processes."""
    with (workspace / "state.lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def state_path(workspace: Path) -> Path:
    return workspace / "state.json"


def read_state(workspace: Path) -> dict:
    return json.loads(state_path(workspace).read_text())


def write_state(workspace: Path, state: dict) -> None:
    fd, name = tempfile.mkstemp(dir=workspace, prefix=".state-")
    with os.fdopen(fd, "w") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=1)
    os.replace(name, state_path(workspace))


def latest_state(release_id: str | None = None) -> dict | None:
    if release_id:
        path = RELEASES / release_id
        return read_state(path) if state_path(path).exists() else None
    candidates = sorted((p for p in RELEASES.glob("*") if state_path(p).exists()), key=lambda p: p.name)
    return read_state(candidates[-1]) if candidates else None


def lane_alive(state: dict, lane: str) -> bool:
    pid = (state.get("driver_pids") or {}).get(lane)
    if not pid and state.get("driver_pid"):
        # Workspaces started before per-lane drivers ran every lane in one process.
        return legacy_driver_alive(state)
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if time.time() - (state.get("driver_spawned") or {}).get(lane, 0) < 10:
        return True  # A new driver may not hold its lock yet.
    with contextlib.suppress(OSError), (Path(state["workspace"]) / f"driver-{lane}.lock").open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    return False


def legacy_driver_alive(state: dict) -> bool:
    with contextlib.suppress(OSError), (Path(state["workspace"]) / "driver.lock").open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def lanes_of(state: dict) -> list[str]:
    return sorted({phase["lane"] for phase in state["phases"].values()})


def driver_alive(state: dict) -> bool:
    return any(lane_alive(state, lane) for lane in lanes_of(state))


def update_phase(workspace: Path, name: str, **values) -> None:
    with locked(workspace):
        state = read_state(workspace)
        state["phases"][name].update(values)
        write_state(workspace, state)


def note(workspace: Path, key: str, value) -> None:
    with locked(workspace):
        state = read_state(workspace)
        state.setdefault("results", {})[key] = value
        write_state(workspace, state)


# ---------- start ----------

def start(args, rehearse: bool) -> dict:
    report = plan()
    if report["blockers"] and not rehearse:
        raise Failure("release blocked: " + "; ".join(report["blockers"]))
    selected = args.components.split(",") if args.components else report["recommended"]
    if not selected or any(c not in COMPONENTS for c in selected):
        raise Failure("choose components from cli, mac, ios; plan recommends none", hint="run plan")
    for component in selected:
        info = report["components"][component]
        if not rehearse and info["notes"] != "ready":
            raise Failure(f"{component}: write bilingual ## Unreleased notes first", hint="see RELEASE_RUNBOOK")
        if not rehearse and info.get("blocked"):
            raise Failure(f"{component}: {info['blocked']}", hint="wait for that work to be committed")
        if not rehearse and info.get("hold"):
            raise Failure(f"{component}: release is on hold: {info['hold']}")
    release_id = time.strftime("%Y%m%d-%H%M%S") + ("-rehearsal" if rehearse else "")
    workspace = RELEASES / release_id
    workspace.mkdir(parents=True)
    step = args.bump
    project = apple_versions((APPLE / "project.yml").read_text())
    versions = {}
    for component in selected:
        info = report["components"][component]
        new = next_version(info["current"], step) if step else info["next"]
        versions[component] = {"from": info["current"], "to": new}
        if component in project:
            versions[component]["build"] = project[component][1] + 1
    clones = {}
    for name, repo in (("cli", CLI), ("apple", APPLE)):
        if name == "cli" and "cli" not in selected:
            continue
        if name == "apple" and not {"mac", "ios"} & set(selected):
            continue
        clones[name] = prepare_clone(repo, workspace / name, versions, rehearse, workspace)
    phases = {}
    for lane, names in LANES.items():
        if lane not in selected:
            continue
        for name in names:
            skip = (rehearse and name in PUBLISHING) or (name == "ios.push" and "mac" in selected)
            phases[name] = {"lane": lane, "status": "skipped" if skip else "pending"}
    state = {"id": release_id, "mode": "rehearsal" if rehearse else "release", "workspace": str(workspace),
             "created": time.time(), "status": "running", "components": selected, "versions": versions,
             "commits": clones, "phases": phases, "results": {}}
    write_state(workspace, state)
    if not rehearse:
        taskboard(release_id, f"进行中: {', '.join(selected)}")
    spawn_drivers(workspace, lanes_of(state))
    return status_view(read_state(workspace))


def prepare_clone(repo: Path, clone: Path, versions: dict, rehearse: bool, workspace: Path) -> str:
    """Commit the version bump (in the clone only for rehearsals) and snapshot that commit."""
    if not rehearse:
        bump(repo, versions, workspace, rehearse=False)
    git(WORKSPACE, "clone", "-q", "--no-hardlinks", "--single-branch", "--branch", "main", str(repo), str(clone))
    for remote in git(repo, "remote").split():
        url = git(repo, "remote", "get-url", remote)
        if remote == "origin":
            git(clone, "remote", "set-url", "origin", url)
        else:
            git(clone, "remote", "add", remote, url)
    if rehearse:
        bump(clone, versions, workspace, rehearse=True)
    return git(clone, "rev-parse", "HEAD")


REHEARSAL_NOTES = "## Unreleased\n\n### English\n\n- Rehearsal build.\n\n### 简体中文\n\n- 演练构建。\n\n"


def bump(repo: Path, versions: dict, workspace: Path, *, rehearse: bool) -> None:
    changelog = repo / "CHANGELOG.md"
    if rehearse and unreleased(changelog.read_text())["notes"] != "ready":
        text = changelog.read_text()
        first = re.search(r"^## ", text, re.M)
        changelog.write_text(text[:first.start()] + REHEARSAL_NOTES + text[first.start():] if first else text)
    if (repo / "pyproject.toml").exists():
        old, new = versions["cli"]["from"], versions["cli"]["to"]
        files = bump_cli_files(repo, old, new)
        notes = unreleased(changelog.read_text())["body"]
        changelog.write_text(promote_unreleased(changelog.read_text(), new))
        (workspace / "cli-notes.md").write_text(notes)
        message = f"release: v{new}"
    else:
        targets = {c: (v["to"], v["build"]) for c, v in versions.items() if c in ("mac", "ios")}
        project = repo / "project.yml"
        project.write_text(bump_apple_project(project.read_text(), targets))
        notes = unreleased(changelog.read_text())["body"]
        heading = apple_heading(targets)
        changelog.write_text(promote_unreleased(changelog.read_text(), heading))
        (workspace / "apple-notes.md").write_text(notes)
        files = ["project.yml"]
        message = f"release: {heading}"
    # Commit only the release files; other agents' staged or unstaged work stays untouched.
    git(repo, "commit", "-q", "-m", message, "--", *files, "CHANGELOG.md")


def spawn_drivers(workspace: Path, lanes: list[str]) -> None:
    """One detached driver per lane, so a failed lane can resume while the others keep running."""
    log = (workspace / "driver.log").open("ab")
    with locked(workspace):
        state = read_state(workspace)
        for key in ("driver_pids", "driver_spawned"):
            if not isinstance(state.get(key), dict):
                state[key] = {}  # Single-driver workspaces stored scalars here.
        for lane in lanes:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "_drive", str(workspace), lane],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            state["driver_pids"][lane] = process.pid
            state["driver_spawned"][lane] = time.time()
        state.update(status="running", finished=None)
        write_state(workspace, state)


def taskboard(release_id: str, status: str | None) -> None:
    """Own one row per repository TASKBOARD; remove it when the release completes."""
    row_prefix = f"| Release {release_id} (release.py) |"
    for repo in (CLI, APPLE):
        path = repo / "TASKBOARD.md"
        if not path.exists():
            continue
        lines = [line for line in path.read_text().splitlines(keepends=True) if not line.startswith(row_prefix)]
        if status:
            stamp = time.strftime("%Y-%m-%d %H:%M")
            row = f"{row_prefix} {status} | release.py pinned clones, release files and publication | " \
                  f"{stamp} | {stamp} | Workspace ~/.config/corral/development/releases/{release_id} |\n"
            index = next((i + 1 for i, line in enumerate(lines) if line.startswith("|---")), len(lines))
            lines.insert(index, row)
        path.write_text("".join(lines))


# ---------- driver ----------

class Context:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.state = read_state(workspace)
        self.cli = workspace / "cli"
        self.apple = workspace / "apple"
        self.logs = workspace / "logs"
        self.config = config()
        self.known = load_known([CLI / "scripts/release_known_issues.json",
                                 APPLE / "scripts/release_known_issues.json"])
        self.rehearsal = self.state["mode"] == "rehearsal"

    def version(self, component: str) -> str:
        return self.state["versions"][component]["to"]

    def log(self, phase: str) -> Path:
        return self.logs / f"{phase}.log"

    def workflow(self, phase: str, repo: Path, argv: list[str], *, resource: str | None = None,
                 timeout: float = 3600) -> Path:
        """Run through agent_workflow for its machine-wide resource locks and receipts."""
        command = [sys.executable, str(AGENT_WORKFLOW), "--json", "run", "--repo", str(repo), "--require-clean",
                   "--timeout", str(int(timeout))]
        if resource:
            command += ["--resource", resource]
        deadline = time.monotonic() + RESOURCE_WAIT_SECONDS
        while True:
            result = subprocess.run([*command, "--", *argv], capture_output=True, text=True)
            envelope = json.loads(result.stdout or "{}")
            error = envelope.get("error") or {}
            if error.get("code") != "resource_busy":
                break
            if time.monotonic() >= deadline:
                raise Failure(f"resource {resource} stayed busy for an hour", hint="inspect its owner")
            update_phase(self.workspace, phase, note=f"waiting for {resource}")
            time.sleep(20)
        data = envelope.get("data") or {}
        # A failed run reports its step results, including the log path, inside the error.
        results = data.get("results") or error.get("results") or [{}]
        step_log = Path(results[0].get("log_path") or self.log(phase))
        update_phase(self.workspace, phase, log=str(step_log), note=None)
        if data.get("status") != "succeeded":
            raise Failure(error.get("message") or f"{phase} failed", log=step_log, excerpt=excerpt(step_log))
        return step_log

    def warnings_gate(self, logs: list[Path], scope: str) -> str:
        found = []
        for log in logs:
            if log.exists():
                found += warnings_in(log.read_text(errors="replace"))
        matched, new = unknown_warnings(list(dict.fromkeys(found)), self.known["warnings"], scope)
        if new:
            raise Failure(f"{len(new)} new warning(s)", excerpt=new[:EXCERPT_LINES],
                          hint="fix owned code or register an assessed entry in scripts/release_known_issues.json")
        return f"warnings: {len(matched)} known" if matched else "no warnings"


def phase_cli_env(ctx: Context) -> str:
    must([sys.executable, "scripts/dev_env.py", "prepare", "--repo", str(ctx.cli)], ctx.log("cli.env"),
         "checkout environment preparation failed", cwd=ctx.cli)
    return "prepared .venv"


def phase_cli_gate(ctx: Context) -> str:
    log = ctx.workflow("cli.gate", ctx.cli, ["env", "-u", "TEXTUAL_DISABLE_KITTY_KEY", ".venv/bin/python",
                                              "scripts/ci-test.py"], resource="corral-cli-checks")
    report = ci_report(log.read_text(errors="replace")) or {}
    return f"{report.get('ran_tests', '?')} tests, {report.get('skipped', 0)} skipped, 0 failed"


def phase_cli_build(ctx: Context) -> str:
    dist = ctx.workspace / "dist"
    shutil.rmtree(dist, ignore_errors=True)
    log = ctx.log("cli.build")
    must(["bash", "scripts/build-dist.sh", str(dist)], log, "package build failed", cwd=ctx.cli,
         env=path_env(ctx.cli / ".venv/bin"))
    missing = (dist / "not-built.txt").read_text().strip()
    wheels = sorted(p.name for p in dist.glob("*.whl"))
    if missing or len(wheels) != REQUIRED_WHEELS or not list(dist.glob("*.tar.gz")):
        raise Failure(f"incomplete package set ({len(wheels)} wheels; not built: {missing or 'none'})", log=log)
    if any(f"-{ctx.version('cli')}-" not in name for name in wheels):
        raise Failure("wheel versions differ from the release version", log=log)
    return f"{len(wheels)} wheels + sdist; " + ctx.warnings_gate([log], "cli.build")


def phase_cli_linux(ctx: Context) -> str:
    host = ctx.config.get("linux_host")
    if not host:
        raise Skip("no linux_host in ~/.config/corral/release/config.json")
    log = ctx.log("cli.linux")
    remote = f".cache/corral-release/{ctx.state['id']}"
    arch = subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, "uname -m"], capture_output=True, text=True,
                          timeout=60).stdout.strip()
    wheel = next(iter(sorted((ctx.workspace / "dist").glob(f"*manylinux*{arch}*.whl"))), None)
    if not wheel:
        raise Failure(f"no manylinux wheel for {arch or 'unreachable host'}")
    archive = subprocess.run(["git", "-C", str(ctx.cli), "archive", "HEAD"], capture_output=True, check=True).stdout
    must(["ssh", host, f"rm -rf {remote} && mkdir -p {remote}/source {remote}/wheels && tar -xf - -C {remote}/source"],
         log, "cannot copy the source snapshot to the Linux host", input_bytes=archive)
    must(["scp", "-q", str(wheel), f"{host}:{remote}/wheels/"], log, "cannot copy the wheel to the Linux host")
    script = (f"cd {remote}/source && uv venv -q --python 3.12 .venv && "
              "uv sync -q --frozen --no-install-project --extra remote && "
              "uv pip install -q --python .venv/bin/python --no-deps ../wheels/*.whl && "
              ".venv/bin/python -c 'import corral, corral._native; print(\"installed\", corral.__version__)' && "
              # The suite runs Ruff from PATH, as an activated environment would provide.
              "env -u TEXTUAL_DISABLE_KITTY_KEY PATH=\"$PWD/.venv/bin:$PATH\" .venv/bin/python scripts/ci-test.py")
    run_logged(["ssh", host, script], log, timeout=3600)
    text = log.read_text(errors="replace")
    if f"installed {ctx.version('cli')}" not in text:
        raise Failure("published wheel did not install or import on Linux", log=log, excerpt=excerpt(log))
    report = ci_report(text)
    if not report or not report.get("coverage_ok", False):
        raise Failure("Linux suite did not complete with full coverage", log=log, excerpt=excerpt(log))
    failed = report.get("failed_ids", []) if report.get("phase") == "final" else []
    known, new = unknown_failures(failed, ctx.known["test_failures"], "linux")
    if new:
        raise Failure(f"{len(new)} new Linux failure(s)", log=log, excerpt=new[:EXCERPT_LINES],
                      hint="fix, or reproduce on the previous tag and register the environment cause")
    subprocess.run(["ssh", host, f"rm -rf {remote}"], capture_output=True, timeout=120)
    note(ctx.workspace, "linux_known_failures", known)
    return f"{report.get('ran_tests')} tests on {arch}; {len(known)} known failure(s)"


def push(repo: Path, remote: str, refs: list[str], log: Path, env: dict | None = None) -> None:
    """Push tags first; `main` may already have moved on remotely, which is fine when it contains HEAD."""
    tags = [r for r in refs if r != "main"]
    if tags:
        must(["git", "-C", str(repo), "push", remote, *tags], log, f"pushing {' '.join(tags)} to {remote} failed",
             env=env)
    if "main" in refs and run_logged(["git", "-C", str(repo), "push", remote, "main"], log, env=env):
        git(repo, "fetch", "-q", remote, "main")
        if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD"]).returncode:
            raise Failure(f"{remote}/main has diverged from the release commit", log=log, excerpt=excerpt(log))


def phase_cli_tag(ctx: Context) -> str:
    tag = "v" + ctx.version("cli")
    log = ctx.log("cli.tag")
    existing = git(ctx.cli, "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}", check=False)
    if existing and existing != git(ctx.cli, "rev-parse", "HEAD"):
        raise Failure(f"tag {tag} already points at another commit")
    if not existing:
        git(ctx.cli, "tag", "-a", tag, "-F", str(ctx.workspace / "cli-notes.md"))
    env = path_env(ctx.cli / ".venv/bin")  # the pre-push hook runs the checkout's Ruff
    for remote in ("origin", "github"):
        push(ctx.cli, remote, ["main", tag], log, env)
    return f"{tag} pushed to origin and github"


def phase_cli_publish(ctx: Context) -> str:
    tag = "v" + ctx.version("cli")
    venv = ctx.cli / ".venv/bin"
    ctx.workflow("cli.publish", ctx.cli, [
        "env", f"PATH={venv}{os.pathsep}{os.environ.get('PATH', '')}",
        f"RELEASE_NOTES_FILE={ctx.workspace / 'cli-notes.md'}", f"CORRAL_DIST_DIR={ctx.workspace / 'dist'}",
        f"CORRAL_REQUIRE_WHEELS={REQUIRED_WHEELS}", "bash", "scripts/publish-release.sh", tag,
    ], resource="corral-cli-publication", timeout=3600)
    return f"https://github.com/{CLI_REPO}/releases/tag/{tag}"


def gh_json(*args: str) -> dict:
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise Failure(f"gh api {args[0]} failed: {result.stderr.strip()[:200]}")
    return json.loads(result.stdout)


def phase_cli_verify(ctx: Context) -> str:
    tag = "v" + ctx.version("cli")
    release = gh_json(f"repos/{CLI_REPO}/releases/tags/{tag}")
    names = [asset["name"] for asset in release["assets"]]
    wheels = [n for n in names if n.endswith(".whl")]
    problems = []
    if release["draft"] or release["prerelease"]:
        problems.append("release is draft or prerelease")
    if gh_json(f"repos/{CLI_REPO}/releases/latest")["tag_name"] != tag:
        problems.append("release is not latest")
    if len(wheels) < REQUIRED_WHEELS or not any(n.endswith(".tar.gz") for n in names) or "SHA256SUMS" not in names:
        problems.append(f"assets incomplete: {len(wheels)} wheels")
    if "### 简体中文" not in (release.get("body") or ""):
        problems.append("bilingual release notes missing")
    formula = gh_json("repos/x0c/homebrew-tap/contents/Formula/corral.rb")
    if f'version "{ctx.version("cli")}"' not in base64.b64decode(formula["content"]).decode():
        problems.append("Homebrew formula not updated")
    if problems:
        raise Failure("; ".join(problems))
    return f"{len(wheels)} wheels, sdist, checksums, notes and formula verified"


def phase_cli_install(ctx: Context) -> str:
    log = ctx.log("cli.install")
    version = ctx.version("cli")
    must(["bash", "scripts/dev-install.sh"], log, "local editable installation failed", cwd=CLI)
    output = subprocess.run(["corral", "--version"], capture_output=True, text=True).stdout.splitlines()
    if not output or version not in output[0]:
        raise Failure(f"installed corral does not report {version}", log=log)
    result = f"editable {version}"
    if subprocess.run(["brew", "list", "--versions", "corral"], capture_output=True).returncode == 0:
        tap = subprocess.run(["brew", "--repo", "x0c/tap"], capture_output=True, text=True).stdout.strip()
        env = path_env(HOMEBREW_NO_AUTO_UPDATE="1")
        must(["git", "-C", tap, "pull", "-q", "--ff-only"], log, "Homebrew tap refresh failed")
        run_logged(["brew", "upgrade", TAP_FORMULA], log, env=env, timeout=1800)
        must(["brew", "test", TAP_FORMULA], log, "brew test failed", env=env, timeout=600)
        listed = subprocess.run(["brew", "list", "--versions", "corral"], capture_output=True, text=True).stdout
        if version not in listed:
            raise Failure(f"Homebrew still has {listed.strip()}", log=log, excerpt=excerpt(log))
        result += f"; Homebrew {version} upgraded and tested"
    return result


def phase_ios_build(ctx: Context) -> str:
    log = ctx.workflow("ios.build", ctx.apple, ["env", "CONFIGURATION=Release", "DESTINATION=generic/platform=iOS",
                                                 "bash", "scripts/ios_sim.sh", "build"], resource="corral-apple-build")
    if not ios_app(ctx).is_dir():
        raise Failure("Release iPhone app was not produced", log=log)
    return ctx.warnings_gate([log], "apple")


def ios_app(ctx: Context) -> Path:
    return ctx.apple / ".derivedData/Build/Products/Release-iphoneos/Corral.app"


def phase_ios_deliver(ctx: Context) -> str:
    device = ctx.config.get("ios_device")
    notes = (ctx.workspace / "apple-notes.md").read_text()
    summary = next((line[2:].strip() for line in notes.splitlines() if line.startswith("- ")), "")[:200]
    argv = ["ios-deliver", "--json", "run", str(ios_app(ctx)), "--notes", summary, "--wait"]
    if device:
        argv[4:4] = ["--device", device]
    else:
        argv[4:4] = ["--mode", "upload"]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
    ctx.log("ios.deliver").write_text(result.stdout + result.stderr)
    data = (json.loads(result.stdout.splitlines()[-1]) if result.stdout.strip() else {}).get("data") or {}
    uploaded = data.get("release_id") and not data.get("upload_error")
    if not uploaded:
        raise Failure("AppShelf upload was not confirmed", log=ctx.log("ios.deliver"),
                      excerpt=[json.dumps(data.get("upload_error") or data.get("error"))])
    install = "installed" if data.get("installation") == "verified" else \
        f"not installed ({(data.get('install_error') or {}).get('code', data.get('installation'))})"
    note(ctx.workspace, "ios", {"page_url": data.get("page_url"), "install": install, "job": data.get("job_id")})
    return f"uploaded to {data.get('page_url')}; {install}"


def phase_ios_push(ctx: Context) -> str:
    push(ctx.apple, "origin", ["main"], ctx.log("ios.push"))
    return "release commit pushed to origin"


def phase_mac_export(ctx: Context) -> str:
    tree = ctx.workspace / "public-tree"
    shutil.rmtree(tree, ignore_errors=True)
    recipe = Path(ctx.config.get("apple_export_recipe", PRIVATE / "apple-export/recipe.json")).expanduser()
    log = ctx.log("mac.export")
    code = run_logged([sys.executable, str(CLI / "scripts/export_public_source.py"), "--source", str(ctx.apple),
                       "--commit", "HEAD", "--output", str(tree), "--recipe", str(recipe),
                       "--receipt", str(ctx.workspace / "export-receipt.json")], log)
    if code:
        first = log.read_text(errors="replace").split("\n$ ", 1)[-1].splitlines()[1:2]
        message = json.loads(first[0])["error"]["message"] if first and first[0].startswith("{") else "export failed"
        raise Failure(message, log=log, hint=f"update {recipe} (replace_all or a per-path rule)")
    receipt = json.loads((ctx.workspace / "export-receipt.json").read_text())
    return f"{receipt['file_count']} files, {len(receipt['transforms'])} transformed"


def phase_mac_public(ctx: Context) -> str:
    public = ctx.workspace / "public"
    log = ctx.log("mac.public")
    if not (public / ".git").exists():
        for attempt in range(3):
            shutil.rmtree(public, ignore_errors=True)
            if not run_logged(["git", "clone", "-q", f"https://github.com/{MAC_PUBLIC_REPO}.git", str(public)], log):
                break
            if attempt == 2:
                raise Failure("cannot clone the public Apple repository", log=log, excerpt=excerpt(log))
            time.sleep(10)
    must(["rsync", "-a", "--delete", "--exclude", ".git", f"{ctx.workspace / 'public-tree'}/", f"{public}/"], log,
         "cannot synchronize the public tree")
    if git(public, "status", "--porcelain"):
        git(public, "add", "-A")
        git(public, "commit", "-q", "-m", "release: " + apple_heading(
            {c: (v["to"], v.get("build", 0)) for c, v in ctx.state["versions"].items() if c == "mac"}))
        return f"public commit {git(public, 'rev-parse', '--short', 'HEAD')}"
    return "public source unchanged"


def phase_mac_analyze(ctx: Context) -> str:
    log = ctx.workflow("mac.analyze", ctx.apple, ["bash", "-c", (
        "xcodegen generate -q && xcodebuild -project Corral.xcodeproj -scheme CorralMac -configuration Debug "
        "-destination platform=macOS -derivedDataPath .derivedData-mac -skipPackagePluginValidation build analyze")],
        resource="corral-apple-build")
    return ctx.warnings_gate([log], "apple")


def phase_mac_prepare(ctx: Context) -> str:
    receipt_dir = ctx.workspace / "mac"
    receipt = receipt_dir / "receipt.json"
    if receipt_dir.exists() and (not receipt.exists() or json.loads(receipt.read_text()).get("phase") != "prepared"):
        shutil.rmtree(receipt_dir)  # An interrupted preparation restarts from a clean receipt directory.
    ctx.workflow("mac.prepare", ctx.apple, [
        "python3", "scripts/publish-mac-release.py", "--prepare-only", "--public-source", str(ctx.workspace / "public"),
        "--notes-file", str(ctx.workspace / "apple-notes.md"), "--receipt-dir", str(receipt_dir),
    ], resource="corral-apple-build")
    return ctx.warnings_gate([receipt_dir / "archive.log", receipt_dir / "export.log"], "apple")


def phase_mac_install(ctx: Context) -> str:
    ctx.workflow("mac.install", ctx.apple, ["python3", "scripts/publish-mac-release.py", "--install-local",
                                            str(ctx.workspace / "mac")], resource="corral-mac-acceptance")
    version, build = ctx.version("mac"), str(ctx.state["versions"]["mac"]["build"])
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = subprocess.run([sys.executable, str(APPLE / "scripts/client_diag.py"), "--json", "status", "--active"],
                                capture_output=True, text=True)
        with contextlib.suppress(ValueError, AttributeError):
            for client in json.loads(result.stdout)["data"]["clients"]:
                if (client.get("platform"), client.get("version"), client.get("build")) == ("macOS", version, build) \
                        and (client.get("state") or {}).get("connection") == "ready":
                    return f"installed {version} ({build}); connected to the host"
        time.sleep(5)
    note(ctx.workspace, "mac_connection", "not confirmed within 90 s")
    return f"installed {version} ({build}); host connection not confirmed"


def phase_mac_publish(ctx: Context) -> str:
    receipt_dir = ctx.workspace / "mac"
    ctx.workflow("mac.publish", ctx.apple, ["python3", "scripts/publish-mac-release.py", "--publish-prepared",
                                            str(receipt_dir)], resource="corral-mac-publication", timeout=7200)
    receipt = json.loads((receipt_dir / "receipt.json").read_text())
    note(ctx.workspace, "mac", {"release": receipt.get("releaseURL"), "cask": receipt.get("homebrewCask")})
    return receipt.get("releaseURL") or "published"


class Skip(Exception):
    pass


PHASES = {name: globals()["phase_" + name.replace(".", "_")] for names in LANES.values() for name in names}


def run_lane(ctx: Context, lane: str) -> None:
    for name in LANES[lane]:
        phase = read_state(ctx.workspace)["phases"].get(name)
        if not phase or phase["status"] in {"succeeded", "skipped"}:
            continue
        update_phase(ctx.workspace, name, status="running", started=time.time(), finished=None, error=None)
        try:
            summary = PHASES[name](ctx)
        except Skip as skipped:
            update_phase(ctx.workspace, name, status="skipped", finished=time.time(), summary=str(skipped))
            continue
        except Failure as failure:
            update_phase(ctx.workspace, name, status="failed", finished=time.time(), error={
                "reason": str(failure), "log": str(failure.log) if failure.log else None,
                "excerpt": failure.excerpt, "hint": failure.hint})
            return
        except Exception as error:  # Unexpected tool errors are failures too; keep their type for diagnosis.
            update_phase(ctx.workspace, name, status="failed", finished=time.time(),
                         error={"reason": f"{type(error).__name__}: {error}"[:500]})
            return
        update_phase(ctx.workspace, name, status="succeeded", finished=time.time(), summary=summary)


def drive(workspace: Path, lane: str) -> None:
    with (workspace / f"driver-{lane}.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_lane(Context(workspace), lane)
    finish_if_done(workspace, lane)


def finish_if_done(workspace: Path, finished_lane: str) -> None:
    with locked(workspace):
        state = read_state(workspace)
        if any(lane_alive(state, lane) for lane in lanes_of(state) if lane != finished_lane):
            return
        open_phases = [n for n, p in state["phases"].items() if p["status"] not in {"succeeded", "skipped"}]
        state.update(status="failed" if open_phases else "succeeded", finished=time.time())
        write_state(workspace, state)
    if state["mode"] == "release":
        taskboard(state["id"], f"失败: {', '.join(open_phases)}; resume" if open_phases else None)


def refresh(workspace: Path, repo_name: str) -> None:
    """Take committed fixes into a pinned clone before anything from its lanes is published."""
    lanes = {"cli": {"cli"}, "apple": {"ios", "mac"}}[repo_name]
    with locked(workspace):
        state = read_state(workspace)
        phases = {n: p for n, p in state["phases"].items() if p["lane"] in lanes}
        if not phases:
            raise Failure(f"this release has no {repo_name} lanes")
        if any(lane_alive(state, lane) for lane in lanes):
            raise Failure(f"{repo_name} lanes are still running", hint="wait")
        if any(n in PUBLISHING and p["status"] == "succeeded" for n, p in phases.items()):
            raise Failure("part of this lane is already published; start a new release instead")
        clone = workspace / repo_name
        source = CLI if repo_name == "cli" else APPLE
        git(clone, "fetch", "-q", str(source), "main")
        if state["mode"] == "rehearsal":
            # The scratch clone carries its own uncommitted-upstream bump; rebuild it on the fix.
            git(clone, "reset", "-q", "--hard", "FETCH_HEAD")
            bump(clone, state["versions"], workspace, rehearse=True)
        else:
            git(clone, "merge", "-q", "--ff-only", "FETCH_HEAD")
        state["commits"][repo_name] = git(clone, "rev-parse", "HEAD")
        for phase in phases.values():
            if phase["status"] != "skipped":
                phase.update(status="pending", error=None, summary=None, started=None, finished=None)
        write_state(workspace, state)


# ---------- views ----------

def duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def status_view(state: dict) -> dict:
    now = time.time()
    phases, failed = {}, []
    for name, phase in state["phases"].items():
        label = phase["status"]
        if phase.get("started") is not None:
            label += " " + duration((phase.get("finished") or now) - phase["started"])
        if phase.get("note") and phase["status"] == "running":
            label += f" ({phase['note']})"
        phases[name] = label
        if phase["status"] == "failed":
            failed.append({"phase": name, **{k: v for k, v in (phase.get("error") or {}).items() if v}})
    status = state["status"]
    if status == "running" and not driver_alive(state):
        status = "interrupted"
    view = {"release": state["id"], "mode": state["mode"], "status": status,
            "versions": {c: v["to"] for c, v in state["versions"].items()},
            "elapsed": duration((state.get("finished") or now) - state["created"]), "phases": phases}
    if failed:
        view["failed"] = failed
    view["next"] = {"running": "wait", "interrupted": "resume", "failed": "fix the cause, then resume",
                    "succeeded": "summary"}[status]
    return view


def wait(state: dict, timeout: float) -> dict:
    workspace = Path(state["workspace"])
    failed_before = {n for n, p in state["phases"].items() if p["status"] == "failed"}
    deadline = time.monotonic() + timeout
    while True:
        state = read_state(workspace)
        failed = {n for n, p in state["phases"].items() if p["status"] == "failed"}
        if state["status"] != "running" or not driver_alive(state) or failed - failed_before \
                or time.monotonic() >= deadline:
            return status_view(state)
        time.sleep(5)


def summary(state: dict) -> dict:
    results = state.get("results", {})
    phases = state["phases"]
    out = {"release": state["id"], "status": state["status"], "elapsed": status_view(state)["elapsed"]}
    versions = {c: v["to"] for c, v in state["versions"].items()}
    links = {}
    if "cli" in versions:
        links["cli"] = phases.get("cli.publish", {}).get("summary")
    if "mac" in versions:
        links["mac"] = (results.get("mac") or {}).get("release")
    if "ios" in versions:
        links["ios"] = (results.get("ios") or {}).get("page_url")
    out["versions"], out["links"] = versions, {k: v for k, v in links.items() if v}
    out["installed"] = {n: phases[n].get("summary") for n in ("cli.install", "mac.install", "ios.deliver")
                        if phases.get(n, {}).get("status") == "succeeded"}
    limits = [f"{n} skipped: {p.get('summary', 'rehearsal')}" for n, p in phases.items() if p["status"] == "skipped"]
    limits += [f"{n} {p['status']}" for n, p in phases.items() if p["status"] not in {"succeeded", "skipped"}]
    if results.get("linux_known_failures"):
        limits.append(f"Linux known failures matched: {len(results['linux_known_failures'])}")
    if (results.get("ios") or {}).get("install", "").startswith("not installed"):
        limits.append("iPhone not installed directly: open the AppShelf link in Safari, sign in with the passkey "
                      "and tap the app to install")
    if results.get("mac_connection"):
        limits.append("Mac host connection " + results["mac_connection"])
    if "cli" in versions and state["mode"] == "release":
        runs = subprocess.run(["gh", "run", "list", "--repo", CLI_REPO, "--branch", "v" + versions["cli"],
                               "--json", "name,status,conclusion", "--limit", "5"], capture_output=True, text=True)
        with contextlib.suppress(ValueError):
            pending = [f"{r['name']}: {r['conclusion'] or r['status']}" for r in json.loads(runs.stdout)
                       if r.get("conclusion") != "success"]
            if pending:
                limits.append("GitHub Actions " + "; ".join(pending))
    out["limits"] = limits
    return out


# ---------- entry ----------

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    for name in ("start", "rehearse"):
        s = sub.add_parser(name)
        s.add_argument("--components", help="Comma-separated subset of cli,mac,ios (default: plan recommendation)")
        s.add_argument("--bump", choices=["patch", "minor", "major"])
    for name in ("wait", "status", "resume", "summary"):
        s = sub.add_parser(name)
        s.add_argument("--release", help="Release id (default: latest)")
        if name == "wait":
            s.add_argument("--timeout", type=float, default=WAIT_SECONDS)
        if name == "resume":
            s.add_argument("--refresh", choices=["cli", "apple"],
                           help="Fast-forward that pinned clone to its main checkout and rerun its lanes")
    drive_parser = sub.add_parser("_drive")
    drive_parser.add_argument("workspace", type=Path)
    drive_parser.add_argument("lane", choices=sorted(LANES))
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "_drive":
        drive(args.workspace, args.lane)
        return 0
    try:
        if args.command == "plan":
            data = plan()
        elif args.command in ("start", "rehearse"):
            data = start(args, rehearse=args.command == "rehearse")
        else:
            state = latest_state(args.release)
            if not state:
                raise Failure("no release found", hint="run plan, then start")
            if args.command == "status":
                data = status_view(state)
            elif args.command == "wait":
                data = wait(state, args.timeout)
            elif args.command == "summary":
                data = summary(state)
            else:
                workspace = Path(state["workspace"])
                if args.refresh:
                    refresh(workspace, args.refresh)
                with locked(workspace):
                    state = read_state(workspace)
                    stopped = [lane for lane in lanes_of(state) if not lane_alive(state, lane)
                               and any(p["lane"] == lane and p["status"] in {"failed", "running", "pending"}
                                       for p in state["phases"].values())]
                    for phase in state["phases"].values():
                        if phase["lane"] in stopped and phase["status"] in {"failed", "running"}:
                            phase.update(status="pending", error=None)
                    write_state(workspace, state)
                if not stopped:
                    raise Failure("nothing to resume: every unfinished lane is still running", hint="wait")
                if state["mode"] == "release":
                    taskboard(state["id"], f"进行中: {', '.join(state['components'])}")
                spawn_drivers(workspace, stopped)
                data = status_view(read_state(workspace))
    except Failure as failure:
        emit(error={"message": str(failure), **({"hint": failure.hint} if failure.hint else {}),
                    **({"log": str(failure.log)} if failure.log else {})})
        return 1
    emit(data)
    return 0 if data.get("status") not in {"failed", "interrupted"} else 1


if __name__ == "__main__":
    sys.exit(main())
