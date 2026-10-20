#!/usr/bin/env python3
"""Opt-in native Pi selection checkpoints; never count a proposal as execution.

Without --live this validates and prints the plan, without invoking a model or
creating artifacts. Live cases have fresh native contexts and read-only tools.
Full execution acceptance remains a separate, explicitly scoped fixture.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT = (
    "This is an initial investigation checkpoint. Perform useful read-only discovery, "
    "then report the next action. Do not modify project files, send messages or launch "
    "work during this checkpoint. Execution tools will be available in the next phase."
)


def selected_skills(stdout: str, skills_root: Path) -> list[str]:
    """Count only successful native read-tool results for an exact skill file."""
    pending = {}
    selected = []
    paths = {str((skills_root / n / "SKILL.md").resolve()): n
             for n in ("corral-search", "corral-delegate")}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if event.get("type") == "tool_execution_start" and event.get("toolName") == "read":
            path = str(Path(event.get("args", {}).get("path", "")).expanduser().resolve())
            if path in paths:
                pending[event.get("toolCallId")] = paths[path]
        if event.get("type") == "tool_execution_end" and not event.get("isError"):
            name = pending.pop(event.get("toolCallId"), None)
            if name and name not in selected:
                selected.append(name)
    return selected


def run_case(case: dict, args, binary: str) -> dict:
    case_dir = args.artifacts / case["id"]
    case_dir.mkdir(parents=True)
    project = case_dir / "project"
    project.mkdir()
    (project / "billing_rules.py").write_text(
        "RETRIES = 3\ndef apply_discount(total, percent):\n    return total - percent\n")
    (project / "README.md").write_text("A fixture may recieve input.\n")
    argv = [binary, "--approve", "--print", "--mode", "json", "--no-session",
            "--thinking", "low", "--no-extensions", "--no-mcp",
            "--no-context-files", "--no-prompt-templates", "--no-themes", "--no-skills",
            "--tools", "read,grep,find,ls", "--append-system-prompt", CHECKPOINT]
    if args.model:
        argv.extend(["--model", args.model])
    for extension in args.extensions:
        argv.extend(["--extension", extension])
    for name in ("corral-search", "corral-delegate"):
        argv.extend(["--skill", str(args.skills_root / name)])
    argv.extend(["--", case["prompt"]])
    started = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=project, capture_output=True, text=True,
                                timeout=args.timeout, env={**os.environ, "CORRAL_SKILLS_AUTO": "0"})
        stdout, stderr, code = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        stdout = stdout.decode() if isinstance(stdout, bytes) else stdout
        stderr = stderr.decode() if isinstance(stderr, bytes) else stderr
        code = 124
    (case_dir / "native.jsonl").write_text(stdout)
    (case_dir / "stderr.log").write_text(stderr)
    observed = selected_skills(stdout, args.skills_root)
    expected = case["expected"]
    completed, native_errors, observed_models = False, [], set()
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        message = event.get("message", {})
        if event.get("type") == "message_end" and message.get("role") == "assistant":
            observed_models.add(f"{message.get('provider')}/{message.get('model')}")
            if message.get("stopReason") in {"error", "aborted"}:
                native_errors.append(message.get("errorMessage") or message["stopReason"])
            elif message.get("stopReason") in {"stop", "end_turn"}:
                completed = True
    valid = code == 0 and completed and not native_errors
    passed = valid and (bool(set(expected) & set(observed)) if expected else not observed)
    record = {"id": case["id"], "expected_any": expected, "selected": observed,
              "passed": passed, "valid": valid, "native_errors": native_errors,
              "observed_models": sorted(observed_models), "exit_code": code,
              "seconds": round(time.monotonic() - started, 2)}
    (case_dir / "result.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record), flush=True)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Invoke configured Pi/model; may incur usage")
    parser.add_argument("--skills-root", type=Path, default=ROOT / "src/corral/skills")
    parser.add_argument("--cases", type=Path, default=ROOT / "scripts/fixtures/skill_trigger_cases.json")
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--model", help="Inherit the configured runtime default when omitted")
    parser.add_argument("--extension", action="append", default=[], dest="extensions",
                        help="Explicit provider extension required by the configured model")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=100)
    parser.add_argument("--case", action="append", dest="case_ids")
    args = parser.parse_args()
    args.skills_root = args.skills_root.resolve()
    cases = json.loads(args.cases.read_text())
    if args.case_ids:
        cases = [c for c in cases if c["id"] in args.case_ids]
    if not cases or len({c["id"] for c in cases}) != len(cases):
        parser.error("Cases must be nonempty and have unique IDs")
    for case in cases:
        if "corral" in case["prompt"].lower():
            parser.error("Business prompts must not name Corral or its skills")
    if not args.live:
        print(json.dumps({"live": False, "cases": len(cases), "model": args.model,
                          "skills_root": str(args.skills_root), "writes": False}))
        return 0
    binary = shutil.which("pi")
    if not binary:
        parser.error("Pi must be installed and configured before --live")
    if args.artifacts is None:
        home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        args.artifacts = home / "corral/development/skill-triggers" / str(time.time_ns())
    args.artifacts.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 4))) as pool:
        records = list(pool.map(lambda c: run_case(c, args, binary), cases))
    summary = {"model": args.model, "runtime": subprocess.check_output([binary, "--version"], text=True).strip(),
               "skills_root": str(args.skills_root),
               "skill_sha256": {n: hashlib.sha256((args.skills_root / n / "SKILL.md").read_bytes()).hexdigest()
                                for n in ("corral-search", "corral-delegate")},
               "cases": records,
               "passed": sum(r["passed"] for r in records), "total": len(records),
               "scope": "Successful native skill reads during read-only checkpoints; no execution claim"}
    (args.artifacts / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"passed": summary["passed"], "total": len(records),
                      "artifacts": str(args.artifacts)}))
    return 0 if all(r["passed"] for r in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
