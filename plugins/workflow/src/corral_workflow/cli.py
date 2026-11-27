"""Agent-friendly CLI surface for ``corral workflow`` (and alias ``corral wf``).

Covers:
- Worker pure-CLI structured output contract: ``finish``, ``schema``, ``log``.
- Discovery, cloud registry & global metadata: ``catalog``, ``scan``,
  ``registry status|sync|set-url``, ``target list|get|set|delete``,
  ``profile list|get|set|reset|delete``, ``health``, ``meta export|import``.
- Workflow orchestration: ``run``, ``list``, ``status``, ``graph``, ``events``,
  ``resume``, ``cancel``.

Dual audience: readable tables on TTY (respecting ``NO_COLOR`` / ``--no-color``),
uniform ``{ok, data, error, meta}`` JSON envelope on ``--json`` or non-TTY.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from corral_workflow.engine import (
    DEFAULT_MAX_CONCURRENCY,
    WorkflowEngine,
    analyze_script_dry_run,
    render_graph_ascii,
)
from corral_workflow.errors import (
    EXIT_CONFLICT,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    WorkflowError,
    ok_envelope,
)
from corral_workflow.playbooks import (
    delete_playbook,
    discover_playbooks,
    export_playbook_skill,
    save_playbook,
    show_playbook,
    unexport_playbook_skill,
)
from corral_workflow.registry import (
    configured_registry_url,
    load_active_registry,
    registry_url_version,
    set_registry_url,
    sync_registry,
)
from corral_workflow.routing import (
    COST_TIERS,
    DEGRADATION_POLICIES,
    SPEED_TIERS,
    build_catalog,
    build_effective_profiles,
    build_effective_targets,
)
from corral_workflow.scanner import ensure_inventory_fresh, scan_local_inventory
from corral_workflow.schema import validate_json
from corral_workflow.state_bridge import ACTIVE_RUNTIME_IDS, caller_identity, corral_bin
from corral_workflow.store import WorkflowStore, _pid_alive, utc_iso

# Single-source metadata exposed via ``corral describe workflow ...``.
WORKFLOW_COMMANDS: list[dict[str, Any]] = [
    {
        "name": "workflow catalog",
        "help": "Progressive-disclosure capability catalog (Local Scan ∩ Cloud Registry + User Overrides + Health)",
        "risk": "read",
        "examples": ["corral workflow catalog --compact", "corral workflow catalog --for ui-design --explain"],
    },
    {
        "name": "workflow scan",
        "help": "Zero-quota scan of locally installed runtimes and available models",
        "risk": "write_local",
        "examples": ["corral workflow scan --dry-run", "corral workflow scan --refresh"],
    },
    {
        "name": "workflow registry",
        "help": "Inspect, sync (ETag) or configure the static cloud capability registry URL",
        "risk": "read / write_local",
        "examples": ["corral workflow registry status", "corral workflow registry sync --force"],
    },
    {
        "name": "workflow target",
        "help": "List, inspect, declaratively set or delete concrete runtime/model targets (--version guarded)",
        "risk": "read / write_local",
        "examples": [
            "corral workflow target list --format tsv",
            (
                "corral workflow target set claude-opus --runtime claude "
                "--model claude-opus-4 --cost high --strengths ui-design,report-writing"
            ),
        ],
    },
    {
        "name": "workflow profile",
        "help": "List, inspect, override, reset or delete semantic capability profiles and ordered fallback chains",
        "risk": "read / write_local",
        "examples": [
            "corral workflow profile list --format tsv",
            "corral workflow profile set ui-design --candidates claude-opus,antigravity-default --version <ver>",
        ],
    },
    {
        "name": "workflow health",
        "help": "Inspect shared circuit-breaker states and cooldowns, or reset a target circuit with --version",
        "risk": "read / write_local",
        "examples": ["corral workflow health", "corral workflow health --reset claude-default --version <ver>"],
    },
    {
        "name": "workflow meta",
        "help": "Export or import portable user metadata overrides (JSON)",
        "risk": "read / write_local",
        "examples": [
            "corral workflow meta export --out catalog.json",
            "corral workflow meta import --file catalog.json --dry-run",
        ],
    },
    {
        "name": "workflow run",
        "help": "Execute or --dry-run a Python workflow script (--arg, --max-concurrency, --detach)",
        "risk": "write_local",
        "examples": [
            "corral workflow run flow.py --arg goal='audit auth' --dry-run",
            "corral workflow run flow.py --max-concurrency 20",
        ],
    },
    {
        "name": "workflow list",
        "help": "List workflow runs and progress counts",
        "risk": "read",
        "examples": ["corral workflow list --format tsv"],
    },
    {
        "name": "workflow status",
        "help": "Inspect workflow run status, phase and node counts",
        "risk": "read",
        "examples": ["corral workflow status <wf_id>"],
    },
    {
        "name": "workflow graph",
        "help": "Inspect execution DAG nodes and edges (--format json or ascii)",
        "risk": "read",
        "examples": ["corral workflow graph <wf_id> --format ascii"],
    },
    {
        "name": "workflow events",
        "help": "Cursor-based workflow event stream",
        "risk": "read",
        "examples": ["corral workflow events <wf_id> --after 0"],
    },
    {
        "name": "workflow resume",
        "help": "Resume a paused/failed workflow from SQLite checkpoints (skips completed steps)",
        "risk": "write_local",
        "examples": [
            "corral workflow resume <wf_id>",
            "corral workflow resume <wf_id> --from-step impl:auth --override-target impl:auth=codex-default",
        ],
    },
    {
        "name": "workflow cancel",
        "help": "Cancel an active workflow run and stop in-flight work (--wait, --force)",
        "risk": "write_local",
        "examples": ["corral workflow cancel <wf_id>", "corral workflow cancel <wf_id> --wait 5"],
    },
    {
        "name": "workflow script",
        "help": "Export the saved script snapshot from a historical workflow run",
        "risk": "read",
        "examples": ["corral workflow script <wf_id>", "corral workflow script <wf_id> --out flow.py"],
    },
    {
        "name": "workflow playbook",
        "help": (
            "List, inspect (with optimization signals), save, delete, "
            "or opt-in export thin skills for reusable playbooks"
        ),
        "risk": "read / write_local",
        "examples": [
            "corral workflow playbook list --format tsv",
            "corral workflow playbook show <name>",
            "corral workflow playbook save <name> --from-run <wf_id> --scope project",
            "corral workflow playbook export-skill <name> --scope project --dry-run",
        ],
    },
    {
        "name": "workflow finish",
        "help": "Worker CLI: validate structured JSON against step schema and commit output",
        "risk": "write_local",
        "examples": ["corral workflow finish --wf <wf_id> --step <step_id> --json-file result.json"],
    },
    {
        "name": "workflow schema",
        "help": "Worker CLI: print the registered JSON Schema for a workflow step",
        "risk": "read",
        "examples": ["corral workflow schema --wf <wf_id> --step <step_id>"],
    },
    {
        "name": "workflow log",
        "help": "Worker CLI: emit a concise progress note onto the flowchart node card",
        "risk": "write_local",
        "examples": ["corral workflow log --wf <wf_id> --step <step_id> 'Scanned 18/24 routes'"],
    },
]


class _ArgParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - exercised via dispatch
        raise WorkflowError("usage_error", message, EXIT_USAGE, hint=f"Run '{self.prog} --help' for valid syntax.")


def _corral_bin() -> str:
    return corral_bin()


def _actor() -> str:
    return str(caller_identity()["actor"] or "local-user")


def _parse_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_kv_args(items: list[str] | None) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for raw in items or []:
        if "=" not in raw:
            raise WorkflowError("usage_error", f"Invalid --arg '{raw}': expected key=value.", EXIT_USAGE)
        key, val = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise WorkflowError("usage_error", f"Invalid --arg '{raw}': empty key.", EXIT_USAGE)
        try:
            parsed[key] = json.loads(val)
        except ValueError:
            parsed[key] = val
    return parsed


def _emit(
    payload: dict[str, Any],
    *,
    json_mode: bool,
    compact: bool,
    tsv_lines: list[str] | None = None,
    human_text: str | None = None,
) -> None:
    if tsv_lines is not None and payload.get("ok"):
        sys.stdout.write("\n".join(tsv_lines) + ("\n" if tsv_lines else ""))
        return
    if not json_mode and human_text is not None and payload.get("ok") and sys.stdout.isatty():
        sys.stdout.write(human_text.rstrip("\n") + "\n")
        return
    if compact:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    else:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _build_parser(prog: str = "corral workflow") -> _ArgParser:
    parser = _ArgParser(
        prog=prog,
        description="Python multi-agent workflow orchestration, capability catalog and pure-CLI step contract.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--root", dest="root", help="Override workflow state root directory")
    parser.add_argument("--json", action="store_true", dest="json_mode", help="Force JSON envelope output")
    parser.add_argument("--compact", action="store_true", help="Compact single-line JSON output")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color")

    sub = parser.add_subparsers(dest="command")

    # -- catalog --
    p_cat = sub.add_parser(
        "catalog", help="Read effective capability catalog (Local Scan ∩ Cloud Registry + Overrides)"
    )
    p_cat.add_argument("--for", dest="for_capability", help="Filter by capability/profile or strength tag")
    p_cat.add_argument("--available-only", action="store_true", help="Show only enabled locally available targets")
    p_cat.add_argument("--explain", action="store_true", help="Include ranking provenance per candidate")
    p_cat.add_argument("--no-auto-scan", action="store_true", help="Skip cold-start auto scan")
    p_cat.add_argument("--format", choices=["json", "tsv"], default=None)
    p_cat.add_argument("--json", action="store_true", dest="json_mode")
    p_cat.add_argument("--compact", action="store_true")

    # -- scan --
    p_scan = sub.add_parser("scan", help="Scan locally installed runtimes and available models (zero paid quota)")
    p_scan.add_argument("--refresh", action="store_true", help="Force immediate rescan")
    p_scan.add_argument("--runtime", choices=sorted(ACTIVE_RUNTIME_IDS), help="Scan a single runtime")
    p_scan.add_argument("--no-gateway", action="store_true", help="Skip LLM Gateway /v1/models check")
    p_scan.add_argument("--dry-run", action="store_true", help="Preview scan without writing SQLite inventory")
    p_scan.add_argument("--format", choices=["json", "tsv"], default=None)
    p_scan.add_argument("--json", action="store_true", dest="json_mode")
    p_scan.add_argument("--compact", action="store_true")

    # -- registry --
    p_reg = sub.add_parser("registry", help="Inspect, sync or configure the cloud capability registry")
    reg_sub = p_reg.add_subparsers(dest="registry_cmd")
    p_reg_st = reg_sub.add_parser("status", help="Show active registry version, source and URL")
    p_reg_st.add_argument("--json", action="store_true", dest="json_mode")
    p_reg_st.add_argument("--compact", action="store_true")
    p_reg_sync = reg_sub.add_parser("sync", help="Fetch latest cloud capability manifest via ETag")
    p_reg_sync.add_argument("--force", action="store_true")
    p_reg_sync.add_argument("--json", action="store_true", dest="json_mode")
    p_reg_sync.add_argument("--compact", action="store_true")
    p_reg_url = reg_sub.add_parser("set-url", help="Configure a custom static registry URL")
    p_reg_url.add_argument("url")
    p_reg_url.add_argument(
        "--version", dest="expected_version", help="Current URL version token from 'registry status'"
    )
    p_reg_url.add_argument("--dry-run", action="store_true")
    p_reg_url.add_argument("--json", action="store_true", dest="json_mode")
    p_reg_url.add_argument("--compact", action="store_true")

    # -- target --
    p_tgt = sub.add_parser("target", help="Manage concrete runtime/model targets and user overrides")
    tgt_sub = p_tgt.add_subparsers(dest="target_cmd")
    p_tl = tgt_sub.add_parser("list", help="List scanned and user-configured targets")
    p_tl.add_argument("--runtime", choices=sorted(ACTIVE_RUNTIME_IDS))
    p_tl.add_argument("--tag", help="Filter by strength tag")
    p_tl.add_argument("--enabled-only", action="store_true")
    p_tl.add_argument("--format", choices=["json", "tsv"], default=None)
    p_tl.add_argument("--json", action="store_true", dest="json_mode")
    p_tl.add_argument("--compact", action="store_true")

    p_tg = tgt_sub.add_parser("get", help="Get one target with its version fingerprint and health")
    p_tg.add_argument("id")
    p_tg.add_argument("--json", action="store_true", dest="json_mode")
    p_tg.add_argument("--compact", action="store_true")

    p_ts = tgt_sub.add_parser("set", help="Declaratively create or override a target")
    p_ts.add_argument("id")
    p_ts.add_argument("--kind", choices=["runtime", "gateway"], default="runtime")
    p_ts.add_argument("--runtime", choices=sorted(ACTIVE_RUNTIME_IDS), default=None)
    p_ts.add_argument("--model", default=None)
    p_ts.add_argument("--cost", dest="cost_tier", choices=COST_TIERS, default=None)
    p_ts.add_argument("--speed", dest="speed_tier", choices=SPEED_TIERS, default=None)
    p_ts.add_argument("--strengths", default=None, help="Comma-separated capability tags")
    p_ts.add_argument("--avoid", dest="avoid_for", default=None, help="Comma-separated capabilities to avoid")
    p_ts.add_argument("--max-concurrency", type=int, default=None)
    p_ts.add_argument("--notes", default=None)
    grp_en = p_ts.add_mutually_exclusive_group()
    grp_en.add_argument("--enabled", dest="enabled", action="store_true", default=None)
    grp_en.add_argument("--disabled", dest="enabled", action="store_false")
    p_ts.add_argument("--version", dest="expected_version", default=None)
    p_ts.add_argument("--dry-run", action="store_true")
    p_ts.add_argument("--json", action="store_true", dest="json_mode")
    p_ts.add_argument("--compact", action="store_true")

    p_td = tgt_sub.add_parser("delete", help="Delete a user target or revert override")
    p_td.add_argument("id")
    p_td.add_argument("--version", dest="expected_version", default=None)
    p_td.add_argument("--force", action="store_true")
    p_td.add_argument("--dry-run", action="store_true")
    p_td.add_argument("--json", action="store_true", dest="json_mode")
    p_td.add_argument("--compact", action="store_true")

    # -- profile --
    p_prof = sub.add_parser("profile", help="Manage semantic capability profiles and ordered fallback chains")
    prof_sub = p_prof.add_subparsers(dest="profile_cmd")
    p_pl = prof_sub.add_parser("list", help="List cloud-derived and user-customized profiles")
    p_pl.add_argument("--format", choices=["json", "tsv"], default=None)
    p_pl.add_argument("--json", action="store_true", dest="json_mode")
    p_pl.add_argument("--compact", action="store_true")

    p_pg = prof_sub.add_parser("get", help="Inspect one profile with its effective candidate chain")
    p_pg.add_argument("id")
    p_pg.add_argument("--json", action="store_true", dest="json_mode")
    p_pg.add_argument("--compact", action="store_true")

    p_ps = prof_sub.add_parser("set", help="Declaratively create or override a capability profile")
    p_ps.add_argument("id")
    p_ps.add_argument("--candidates", required=True, help="Ordered comma-separated target IDs")
    p_ps.add_argument("--description", default="")
    p_ps.add_argument(
        "--degradation", dest="degradation_policy", choices=DEGRADATION_POLICIES, default="pause_on_exhaust"
    )
    p_ps.add_argument("--version", dest="expected_version", default=None)
    p_ps.add_argument("--dry-run", action="store_true")
    p_ps.add_argument("--json", action="store_true", dest="json_mode")
    p_ps.add_argument("--compact", action="store_true")

    p_pr = prof_sub.add_parser("reset", help="Reset a customized built-in capability back to cloud-derived ranking")
    p_pr.add_argument("id")
    p_pr.add_argument("--version", dest="expected_version", default=None)
    p_pr.add_argument("--dry-run", action="store_true")
    p_pr.add_argument("--json", action="store_true", dest="json_mode")
    p_pr.add_argument("--compact", action="store_true")

    p_pd = prof_sub.add_parser("delete", help="Delete a custom user profile")
    p_pd.add_argument("id")
    p_pd.add_argument("--version", dest="expected_version", default=None)
    p_pd.add_argument("--dry-run", action="store_true")
    p_pd.add_argument("--json", action="store_true", dest="json_mode")
    p_pd.add_argument("--compact", action="store_true")

    # -- health --
    p_hlt = sub.add_parser("health", help="Inspect circuit breaker states or reset a target circuit")
    p_hlt.add_argument("--target", dest="target_id", default=None)
    p_hlt.add_argument("--reset", dest="reset_id", default=None, help="Reset circuit breaker for target ID")
    p_hlt.add_argument("--version", dest="expected_version", default=None)
    p_hlt.add_argument("--dry-run", action="store_true")
    p_hlt.add_argument("--format", choices=["json", "tsv"], default=None)
    p_hlt.add_argument("--json", action="store_true", dest="json_mode")
    p_hlt.add_argument("--compact", action="store_true")

    # -- meta export/import --
    p_meta = sub.add_parser("meta", help="Export or import portable user metadata overrides")
    meta_sub = p_meta.add_subparsers(dest="meta_cmd")
    p_me = meta_sub.add_parser("export", help="Export user targets and profiles to JSON")
    p_me.add_argument("--out", required=True)
    p_me.add_argument("--json", action="store_true", dest="json_mode")
    p_me.add_argument("--compact", action="store_true")
    p_mi = meta_sub.add_parser("import", help="Import user targets and profiles from JSON")
    p_mi.add_argument("--file", dest="in_file", required=True)
    p_mi.add_argument("--dry-run", action="store_true")
    p_mi.add_argument("--json", action="store_true", dest="json_mode")
    p_mi.add_argument("--compact", action="store_true")

    # -- run --
    p_run = sub.add_parser("run", help="Run or --dry-run a Python workflow script")
    p_run.add_argument("script", help="Path to Python workflow script or saved playbook name")
    p_run.add_argument(
        "--arg", dest="arg_items", action="append", default=[], help="Pass key=value into script args dict"
    )
    p_run.add_argument("--cwd", default=None, help="Working directory for the workflow")
    p_run.add_argument("--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY)
    p_run.add_argument("--name", default=None, help="Optional display name for this workflow run")
    p_run.add_argument(
        "--dry-run", action="store_true", help="Validate syntax and preview capability resolution without running"
    )
    p_run.add_argument(
        "--detach", action="store_true", help="Launch workflow in background and return wf_id immediately"
    )
    p_run.add_argument("--wf-id", dest="wf_id", default=None, help=argparse.SUPPRESS)
    p_run.add_argument("--json", action="store_true", dest="json_mode")
    p_run.add_argument("--compact", action="store_true")

    # -- list / status / graph / events / resume / cancel --
    p_list = sub.add_parser("list", help="List workflow runs")
    p_list.add_argument(
        "--status", choices=["running", "waiting_input", "blocked", "completed", "failed", "cancelled"], default=None
    )
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--format", choices=["json", "tsv"], default=None)
    p_list.add_argument("--json", action="store_true", dest="json_mode")
    p_list.add_argument("--compact", action="store_true")

    p_st = sub.add_parser("status", help="Show workflow status and node counts")
    p_st.add_argument("wf_id")
    p_st.add_argument("--json", action="store_true", dest="json_mode")
    p_st.add_argument("--compact", action="store_true")

    p_gr = sub.add_parser("graph", help="Show execution DAG nodes and edges")
    p_gr.add_argument("wf_id")
    p_gr.add_argument("--format", choices=["json", "ascii"], default=None)
    p_gr.add_argument("--json", action="store_true", dest="json_mode")
    p_gr.add_argument("--compact", action="store_true")

    p_ev = sub.add_parser("events", help="Read workflow event stream after cursor")
    p_ev.add_argument("wf_id")
    p_ev.add_argument("--after", type=int, default=0)
    p_ev.add_argument("--limit", type=int, default=100)
    p_ev.add_argument("--json", action="store_true", dest="json_mode")
    p_ev.add_argument("--compact", action="store_true")

    p_res = sub.add_parser("resume", help="Resume a workflow from persisted SQLite checkpoints")
    p_res.add_argument("wf_id")
    p_res.add_argument(
        "--from-step",
        dest="from_step",
        default=None,
        help="Invalidate this step_key and downstream nodes before resuming",
    )
    p_res.add_argument(
        "--override-target",
        dest="override_items",
        action="append",
        default=[],
        help="Override step target as step_key=target_id",
    )
    script_grp = p_res.add_mutually_exclusive_group()
    script_grp.add_argument(
        "--use-saved-script",
        action="store_true",
        dest="use_saved_script",
        help="Resume using the script snapshot saved when the workflow started",
    )
    script_grp.add_argument(
        "--allow-script-change",
        action="store_true",
        dest="allow_script_change",
        help="Allow resuming with a modified script on disk (recomputes changed and downstream steps)",
    )
    p_res.add_argument("--dry-run", action="store_true")
    p_res.add_argument("--json", action="store_true", dest="json_mode")
    p_res.add_argument("--compact", action="store_true")

    p_can = sub.add_parser("cancel", help="Cancel an active workflow run")
    p_can.add_argument("wf_id")
    p_can.add_argument(
        "--wait",
        type=float,
        default=5.0,
        help="Seconds to wait for the live workflow process to stop cleanly (default: 5.0)",
    )
    p_can.add_argument(
        "--force",
        action="store_true",
        help="Send SIGKILL if the live workflow process does not exit within --wait",
    )
    p_can.add_argument("--dry-run", action="store_true")
    p_can.add_argument("--json", action="store_true", dest="json_mode")
    p_can.add_argument("--compact", action="store_true")

    # -- Historical script export --
    p_scr = sub.add_parser("script", help="Export the saved script snapshot from a historical workflow run")
    p_scr.add_argument("wf_id")
    p_scr.add_argument("--out", dest="out_file", default=None, help="Write script snapshot to this file")
    p_scr.add_argument("--json", action="store_true", dest="json_mode")
    p_scr.add_argument("--compact", action="store_true")

    # -- Playbook library & opt-in thin skill export --
    p_pb = sub.add_parser(
        "playbook", help="Discover, inspect, save, delete, or export thin skills for workflow playbooks"
    )
    pb_sub = p_pb.add_subparsers(dest="playbook_cmd")

    p_pbl = pb_sub.add_parser("list", help="List saved playbooks across project and global scopes")
    p_pbl.add_argument("--scope", choices=["all", "project", "global"], default="all")
    p_pbl.add_argument("--tag", default=None)
    p_pbl.add_argument("--cwd", default=None)
    p_pbl.add_argument("--format", choices=["json", "tsv"], default=None)
    p_pbl.add_argument("--json", action="store_true", dest="json_mode")
    p_pbl.add_argument("--compact", action="store_true")

    p_pbs = pb_sub.add_parser("show", help="Show playbook metadata, source, version, and optimization signals")
    p_pbs.add_argument("name")
    p_pbs.add_argument("--scope", choices=["project", "global"], default=None)
    p_pbs.add_argument("--cwd", default=None)
    p_pbs.add_argument("--no-source", action="store_true", dest="no_source")
    p_pbs.add_argument("--json", action="store_true", dest="json_mode")
    p_pbs.add_argument("--compact", action="store_true")

    p_pbv = pb_sub.add_parser("save", help="Save a validated workflow script or historical run snapshot as a playbook")
    p_pbv.add_argument("name")
    pb_src = p_pbv.add_mutually_exclusive_group(required=True)
    pb_src.add_argument("--file", dest="in_file", default=None, help="Source .py file to save")
    pb_src.add_argument("--from-run", dest="from_run", default=None, help="Promote saved script from workflow run ID")
    p_pbv.add_argument("--scope", choices=["project", "global"], default="project")
    p_pbv.add_argument("--cwd", default=None)
    p_pbv.add_argument("--version", default=None, help="Required when overwriting an existing playbook")
    p_pbv.add_argument("--dry-run", action="store_true")
    p_pbv.add_argument("--json", action="store_true", dest="json_mode")
    p_pbv.add_argument("--compact", action="store_true")

    p_pbd = pb_sub.add_parser("delete", help="Delete a saved playbook (--version required)")
    p_pbd.add_argument("name")
    p_pbd.add_argument("--scope", choices=["project", "global"], default=None)
    p_pbd.add_argument("--cwd", default=None)
    p_pbd.add_argument("--version", default=None)
    p_pbd.add_argument("--dry-run", action="store_true")
    p_pbd.add_argument("--json", action="store_true", dest="json_mode")
    p_pbd.add_argument("--compact", action="store_true")

    p_pbe = pb_sub.add_parser(
        "export-skill",
        help="Opt-in export of a thin pointer SKILL.md for a mature, high-frequency playbook",
    )
    p_pbe.add_argument("name")
    p_pbe.add_argument("--scope", choices=["project", "global"], default="project")
    p_pbe.add_argument("--dest", default=None, help="Explicit parent skills directory override")
    p_pbe.add_argument("--cwd", default=None)
    p_pbe.add_argument("--skill-name", dest="skill_name", default=None)
    p_pbe.add_argument("--dry-run", action="store_true")
    p_pbe.add_argument("--json", action="store_true", dest="json_mode")
    p_pbe.add_argument("--compact", action="store_true")

    p_pbu = pb_sub.add_parser("unexport-skill", help="Remove a Corral-exported thin pointer SKILL.md")
    p_pbu.add_argument("name")
    p_pbu.add_argument("--scope", choices=["project", "global"], default="project")
    p_pbu.add_argument("--dest", default=None, help="Explicit parent skills directory override")
    p_pbu.add_argument("--cwd", default=None)
    p_pbu.add_argument("--skill-name", dest="skill_name", default=None)
    p_pbu.add_argument("--dry-run", action="store_true")
    p_pbu.add_argument("--json", action="store_true", dest="json_mode")
    p_pbu.add_argument("--compact", action="store_true")

    # -- Worker pure-CLI commands: finish / schema / log --
    p_fin = sub.add_parser("finish", help="Worker CLI: validate and commit structured JSON output for a workflow step")
    p_fin.add_argument("--wf", dest="wf_id", default=None)
    p_fin.add_argument("--step", dest="step_id", default=None)
    src_grp = p_fin.add_mutually_exclusive_group(required=True)
    src_grp.add_argument("--json-file", dest="json_file", default=None)
    src_grp.add_argument("--json", dest="json_inline", default=None)
    src_grp.add_argument("--stdin", action="store_true", dest="from_stdin")
    p_fin.add_argument("--compact", action="store_true")

    p_sch = sub.add_parser("schema", help="Worker CLI: print the expected JSON Schema for a workflow step")
    p_sch.add_argument("--wf", dest="wf_id", default=None)
    p_sch.add_argument("--step", dest="step_id", default=None)
    p_sch.add_argument("--json", action="store_true", dest="json_mode")
    p_sch.add_argument("--compact", action="store_true")

    p_log = sub.add_parser("log", help="Worker CLI: record a concise progress update onto the flowchart node")
    p_log.add_argument("--wf", dest="wf_id", default=None)
    p_log.add_argument("--step", dest="step_id", default=None)
    p_log.add_argument("message")
    p_log.add_argument("--json", action="store_true", dest="json_mode")
    p_log.add_argument("--compact", action="store_true")

    return parser


def _resolve_script_path(raw: str, store: WorkflowStore, *, cwd: str | Path | None = None) -> Path:
    direct = Path(raw).expanduser()
    if direct.is_file():
        return direct.resolve()
    search_roots = []
    if cwd:
        search_roots.append(Path(cwd).expanduser().resolve())
    search_roots.append(Path.cwd())
    candidates: list[Path] = []
    for base in search_roots:
        candidates.append(base / ".corral" / "workflows" / f"{raw}.py")
        candidates.append(base / ".corral" / "workflows" / raw)
    candidates.extend(
        [
            store.root / "playbooks" / f"{raw}.py",
            store.root / "playbooks" / raw,
        ]
    )
    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    raise WorkflowError(
        "not_found",
        f"Workflow script or playbook not found: {raw}",
        EXIT_NOT_FOUND,
        hint="Pass a valid .py file path or run 'corral workflow playbook list' to see saved playbooks.",
    )


def _resolve_worker_ids(args: argparse.Namespace) -> tuple[str, str]:
    wf_id = args.wf_id or os.environ.get("CORRAL_WF_ID", "").strip()
    step_id = args.step_id or os.environ.get("CORRAL_WF_STEP_ID", "").strip()
    if not wf_id or not step_id:
        raise WorkflowError(
            "usage_error",
            "Missing workflow/step identity: pass --wf and --step "
            "(or run inside a workflow step with CORRAL_WF_ID/CORRAL_WF_STEP_ID).",
            EXIT_USAGE,
        )
    return wf_id, step_id


def _dispatch_parsed(
    args: argparse.Namespace, store: WorkflowStore
) -> tuple[dict[str, Any], list[str] | None, str | None]:
    cmd = args.command
    actor = _actor()
    cbin = _corral_bin()

    if not cmd:
        raise WorkflowError(
            "usage_error",
            "Missing workflow subcommand.",
            EXIT_USAGE,
            hint=(
                "Use catalog, scan, registry, target, profile, health, meta, "
                "run, list, status, graph, events, resume, cancel, finish, schema or log."
            ),
            next_commands=[f"{cbin} workflow catalog", f"{cbin} workflow --help"],
        )

    if cmd == "catalog":
        if not args.no_auto_scan:
            ensure_inventory_fresh(store, probe_gateway=True)
        data = build_catalog(
            store,
            for_capability=args.for_capability,
            available_only=args.available_only,
            explain=args.explain,
        )
        tsv = None
        if args.format == "tsv":
            tsv = ["# profile\torigin\tcandidates\thealthy\tdegradation"]
            for p in data["profiles"]:
                tsv.append(
                    f"{p['id']}\t{p['origin']}\t{','.join(p['candidates'])}\t{','.join(p['healthy_candidates'])}\t{p['degradation_policy']}"
                )
        human_lines = [
            (
                f"Registry: {data['registry']['registry_version']} ({data['registry']['source']})  |  "
                f"Profiles: {data['profiles_count']}  |  Targets: {data['targets_count']}"
            ),
            "",
        ]
        for p in data["profiles"]:
            chain = " -> ".join(p["candidates"]) or "(no local match)"
            human_lines.append(f"  {p['id']:<16} [{p['origin']:<13}]  {chain}")
        return ok_envelope(data), tsv, "\n".join(human_lines)

    if cmd == "scan":
        data = scan_local_inventory(
            store,
            runtimes=[args.runtime] if args.runtime else None,
            dry_run=args.dry_run,
            probe_gateway=not args.no_gateway,
            actor=actor,
        )
        tsv = None
        if args.format == "tsv":
            tsv = ["# target_id\tkind\truntime\tmodel\tfamily\toverride\tauth"]
            for t in data["targets"]:
                tsv.append(
                    f"{t['id']}\t{t['kind']}\t{t.get('runtime') or '-'}\t{t.get('model') or 'default'}\t"
                    f"{t.get('family') or '-'}\t{t['supports_model_override']}\t{t['auth_hint']}"
                )
        human_lines = [f"Scanned {len(data['runtimes'])} runtimes ({data['targets_discovered']} targets discovered):"]
        for rt in data["runtimes"]:
            mark = "✓" if rt["available"] else "✗"
            ver = rt.get("cli_version") or ""
            human_lines.append(
                f"  {mark} {rt['runtime']:<12} models={rt['models_found']} "
                f"override={rt['supports_model_override']} {ver}"
            )
        return ok_envelope(data, meta={"dry_run": args.dry_run}), tsv, "\n".join(human_lines)

    if cmd == "registry":
        sub_cmd = args.registry_cmd
        if sub_cmd == "status":
            reg = load_active_registry(store)
            url, url_source = configured_registry_url(store)
            data = {
                "source": reg["source"],
                "registry_version": reg["registry_version"],
                "published_at": reg.get("published_at"),
                "sha256": reg.get("sha256"),
                "fetched_at": reg.get("fetched_at"),
                "url": url,
                "url_source": url_source,
                "version": registry_url_version(store),
                "capabilities": sorted((reg["manifest"].get("capabilities") or {}).keys()),
            }
            return (
                ok_envelope(data),
                None,
                f"Registry {data['registry_version']} ({data['source']}) url={url} version={data['version']}",
            )
        if sub_cmd == "sync":
            synced = sync_registry(store, force=args.force)
            out = {k: v for k, v in synced.items() if k != "manifest"}
            return ok_envelope(out), None, f"Registry sync: {out['status']} (version={out['registry_version']})"
        if sub_cmd == "set-url":
            res = set_registry_url(
                store,
                args.url,
                expected_version=args.expected_version,
                dry_run=args.dry_run,
                actor=actor,
            )
            return (
                ok_envelope(res, meta={"dry_run": args.dry_run}),
                None,
                f"Registry URL -> {res['url']} (version={res['version']})",
            )
        raise WorkflowError("usage_error", "Use 'registry status', 'registry sync', or 'registry set-url'.", EXIT_USAGE)

    if cmd == "target":
        sub_cmd = args.target_cmd
        if sub_cmd == "list":
            targets = build_effective_targets(store)
            items = list(targets.values())
            if args.runtime:
                items = [t for t in items if t.get("runtime") == args.runtime]
            if args.tag:
                needle = args.tag.strip().lower()
                items = [t for t in items if any(needle == s.lower() for s in t.get("strengths") or [])]
            if args.enabled_only:
                items = [t for t in items if t.get("enabled")]
            tsv = None
            if args.format == "tsv":
                tsv = ["# id\torigin\tkind\truntime\tmodel\tcost\tspeed\thealth\tversion\tstrengths"]
                for t in items:
                    tsv.append(
                        f"{t['id']}\t{t['origin']}\t{t['kind']}\t{t.get('runtime') or '-'}\t"
                        f"{t.get('model') or 'default'}\t{t['cost_tier']}\t{t['speed_tier']}\t"
                        f"{t['health']['state']}\t{t['version']}\t{','.join(t['strengths'])}"
                    )
            return ok_envelope({"count": len(items), "targets": items}), tsv, None

        if sub_cmd == "get":
            targets = build_effective_targets(store)
            if args.id not in targets:
                raise WorkflowError(
                    "not_found",
                    f"Target '{args.id}' not found.",
                    EXIT_NOT_FOUND,
                    context={"available_targets": sorted(targets)},
                    next_commands=[f"{cbin} workflow target list --format tsv"],
                )
            return ok_envelope({"target": targets[args.id]}), None, None

        if sub_cmd == "set":
            existing_effective = build_effective_targets(store).get(args.id)
            existing_user = store.get_user_target(args.id)
            if existing_effective is None and args.kind == "runtime" and not args.runtime:
                raise WorkflowError("usage_error", "Creating a new runtime target requires --runtime.", EXIT_USAGE)
            runtime_val = (
                args.runtime
                if args.runtime is not None
                else (existing_effective.get("runtime") if existing_effective else None)
            )
            model_val = (
                args.model
                if args.model is not None
                else (existing_effective.get("model") if existing_effective else None)
            )
            if (
                model_val
                and runtime_val == "opencode"
                and existing_effective
                and not existing_effective.get("supports_model_override", True)
            ):
                raise WorkflowError(
                    "model_selection_unsupported",
                    "OpenCode hosted CLI rejects explicit --model selection; omit --model to inherit runtime default.",
                    EXIT_USAGE,
                )
            origin = (
                "user_override" if (existing_effective and existing_effective["origin"] == "scanned") else "user_custom"
            )
            record = {
                "id": args.id,
                "origin": origin,
                "kind": args.kind or (existing_effective["kind"] if existing_effective else "runtime"),
                "runtime": runtime_val,
                "model": model_val,
                "cost_tier": args.cost_tier or (existing_effective["cost_tier"] if existing_effective else "medium"),
                "speed_tier": args.speed_tier or (existing_effective["speed_tier"] if existing_effective else "medium"),
                "strengths": _parse_csv(args.strengths)
                if args.strengths is not None
                else (list(existing_effective["strengths"]) if existing_effective else []),
                "avoid_for": _parse_csv(args.avoid_for)
                if args.avoid_for is not None
                else (list(existing_effective["avoid_for"]) if existing_effective else []),
                "max_concurrency": args.max_concurrency
                if args.max_concurrency is not None
                else (existing_effective["max_concurrency"] if existing_effective else None),
                "enabled": args.enabled
                if args.enabled is not None
                else (existing_effective["enabled"] if existing_effective else True),
                "notes": args.notes
                if args.notes is not None
                else (existing_effective["notes"] if existing_effective else ""),
            }
            if args.dry_run:
                if existing_user is not None and args.expected_version != existing_user["version"]:
                    if not args.expected_version:
                        raise WorkflowError(
                            "confirmation_required",
                            (
                                f"Target '{args.id}' already exists; "
                                f"pass --version {existing_user['version']} to update it."
                            ),
                            EXIT_USAGE,
                            next_commands=[f"{cbin} workflow target get {args.id}"],
                        )
                    raise WorkflowError(
                        "conflict",
                        f"Version mismatch for target '{args.id}'.",
                        EXIT_CONFLICT,
                        next_commands=[f"{cbin} workflow target get {args.id}"],
                    )
                return (
                    ok_envelope({"status": "planned", "target": record, "dry_run": True}, meta={"dry_run": True}),
                    None,
                    None,
                )
            status, saved = store.upsert_user_target(record, expected_version=args.expected_version, actor=actor)
            return ok_envelope({"status": status, "target": saved}), None, None

        if sub_cmd == "delete":
            if args.dry_run:
                usr = store.get_user_target(args.id)
                if usr is None:
                    raise WorkflowError("not_found", f"User target '{args.id}' not found.", EXIT_NOT_FOUND)
                return (
                    ok_envelope({"status": "planned_delete", "target": usr, "dry_run": True}, meta={"dry_run": True}),
                    None,
                    None,
                )
            removed = store.delete_user_target(
                args.id, expected_version=args.expected_version, force=args.force, actor=actor
            )
            return ok_envelope({"status": "deleted", "target": removed}), None, None

        raise WorkflowError("usage_error", "Use 'target list|get|set|delete'.", EXIT_USAGE)

    if cmd == "profile":
        sub_cmd = args.profile_cmd
        if sub_cmd == "list":
            profiles = build_effective_profiles(store)
            items = list(profiles.values())
            tsv = None
            if args.format == "tsv":
                tsv = ["# id\torigin\tdegradation\tversion\tcandidates"]
                for p in items:
                    tsv.append(
                        f"{p['id']}\t{p['origin']}\t{p['degradation_policy']}\t{p['version']}\t{','.join(p['candidates'])}"
                    )
            return ok_envelope({"count": len(items), "profiles": items}), tsv, None

        if sub_cmd == "get":
            profiles = build_effective_profiles(store)
            if args.id not in profiles:
                raise WorkflowError(
                    "not_found",
                    f"Profile '{args.id}' not found.",
                    EXIT_NOT_FOUND,
                    context={"available_profiles": sorted(profiles)},
                    next_commands=[f"{cbin} workflow profile list --format tsv"],
                )
            return ok_envelope({"profile": profiles[args.id]}), None, None

        if sub_cmd == "set":
            cands = _parse_csv(args.candidates)
            if not cands:
                raise WorkflowError("usage_error", "--candidates must specify at least one target ID.", EXIT_USAGE)
            known_targets = build_effective_targets(store)
            missing = [tid for tid in cands if tid not in known_targets]
            if missing:
                raise WorkflowError(
                    "not_found",
                    f"Unknown candidate target(s): {', '.join(missing)}.",
                    EXIT_NOT_FOUND,
                    hint="Create or scan those targets before referencing them in a profile.",
                    context={"missing_targets": missing, "available_targets": sorted(known_targets)},
                    next_commands=[f"{cbin} workflow target list --format tsv"],
                )
            existing_eff = build_effective_profiles(store).get(args.id)
            origin = "user_override" if (existing_eff and existing_eff["origin"] == "cloud_derived") else "user_custom"
            record = {
                "id": args.id,
                "origin": origin,
                "description": args.description or (existing_eff["description"] if existing_eff else ""),
                "candidates": cands,
                "degradation_policy": args.degradation_policy,
            }
            if args.dry_run:
                return (
                    ok_envelope({"status": "planned", "profile": record, "dry_run": True}, meta={"dry_run": True}),
                    None,
                    None,
                )
            status, saved = store.upsert_user_profile(record, expected_version=args.expected_version, actor=actor)
            return ok_envelope({"status": status, "profile": saved}), None, None

        if sub_cmd in {"reset", "delete"}:
            if args.dry_run:
                usr = store.get_user_profile(args.id)
                if usr is None:
                    raise WorkflowError("not_found", f"User profile '{args.id}' not found.", EXIT_NOT_FOUND)
                return (
                    ok_envelope(
                        {"status": f"planned_{sub_cmd}", "profile": usr, "dry_run": True}, meta={"dry_run": True}
                    ),
                    None,
                    None,
                )
            removed = store.delete_user_profile(
                args.id,
                expected_version=args.expected_version,
                actor=actor,
                action=f"profile.{sub_cmd}",
            )
            return ok_envelope({"status": sub_cmd, "profile": removed}), None, None

        raise WorkflowError("usage_error", "Use 'profile list|get|set|reset|delete'.", EXIT_USAGE)

    if cmd == "health":
        if args.reset_id:
            if args.dry_run:
                cur = store.get_target_health(args.reset_id)
                return (
                    ok_envelope({"status": "planned_reset", "health": cur, "dry_run": True}, meta={"dry_run": True}),
                    None,
                    None,
                )
            updated = store.reset_target_health(args.reset_id, expected_version=args.expected_version, actor=actor)
            return ok_envelope({"status": "reset", "health": updated}), None, None
        targets = build_effective_targets(store)
        ids = (
            [args.target_id]
            if args.target_id
            else sorted(set(targets) | {h["target_id"] for h in store.list_target_health()})
        )
        rows = [store.get_target_health(tid) for tid in ids]
        tsv = None
        if args.format == "tsv":
            tsv = ["# target_id\tstate\tconsecutive_failures\tcooldown_remaining_s\tfailure_class\tversion\treason"]
            for h in rows:
                tsv.append(
                    f"{h['target_id']}\t{h['state']}\t{h['consecutive_failures']}\t{h['cooldown_remaining_s']}\t"
                    f"{h.get('failure_class') or '-'}\t{h['version']}\t{h.get('reason') or '-'}"
                )
        return ok_envelope({"count": len(rows), "targets": rows}), tsv, None

    if cmd == "meta":
        if args.meta_cmd == "export":
            payload = {
                "schema_version": 1,
                "exported_at_utc": utc_iso(),
                "targets": store.list_user_targets(),
                "profiles": store.list_user_profiles(),
            }
            out_path = Path(args.out).expanduser().resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return (
                ok_envelope(
                    {
                        "output_path": str(out_path),
                        "targets": len(payload["targets"]),
                        "profiles": len(payload["profiles"]),
                    }
                ),
                None,
                None,
            )
        if args.meta_cmd == "import":
            in_path = Path(args.in_file).expanduser().resolve()
            if not in_path.is_file():
                raise WorkflowError("not_found", f"Import file not found: {in_path}", EXIT_NOT_FOUND)
            try:
                doc = json.loads(in_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise WorkflowError("usage_error", f"Invalid JSON in {in_path}: {exc}", EXIT_USAGE) from exc
            t_list = doc.get("targets") if isinstance(doc, dict) else None
            p_list = doc.get("profiles") if isinstance(doc, dict) else None
            if not isinstance(t_list, list) or not isinstance(p_list, list):
                raise WorkflowError(
                    "usage_error", "Import JSON must contain 'targets' and 'profiles' arrays.", EXIT_USAGE
                )
            diff = {"targets_created": [], "targets_updated": [], "profiles_created": [], "profiles_updated": []}
            for item in t_list:
                existing = store.get_user_target(item["id"])
                if args.dry_run:
                    (diff["targets_updated"] if existing else diff["targets_created"]).append(item["id"])
                else:
                    st, _ = store.upsert_user_target(
                        item, expected_version=None, actor=actor, require_version_if_exists=False
                    )
                    (diff["targets_updated"] if st == "updated" else diff["targets_created"]).append(item["id"])
            for item in p_list:
                existing = store.get_user_profile(item["id"])
                if args.dry_run:
                    (diff["profiles_updated"] if existing else diff["profiles_created"]).append(item["id"])
                else:
                    st, _ = store.upsert_user_profile(
                        item, expected_version=None, actor=actor, require_version_if_exists=False
                    )
                    (diff["profiles_updated"] if st == "updated" else diff["profiles_created"]).append(item["id"])
            return ok_envelope({"dry_run": args.dry_run, **diff}, meta={"dry_run": args.dry_run}), None, None
        raise WorkflowError("usage_error", "Use 'meta export --out <file>' or 'meta import --file <file>'.", EXIT_USAGE)

    if cmd == "run":
        script_file = _resolve_script_path(args.script, store, cwd=args.cwd)
        source = script_file.read_text(encoding="utf-8")
        wf_args = _parse_kv_args(args.arg_items)
        wf_name = args.name or script_file.stem
        if args.dry_run:
            analysis = analyze_script_dry_run(store, source, filename=str(script_file))
            return (
                ok_envelope(
                    {
                        "dry_run": True,
                        "name": wf_name,
                        "script_path": str(script_file),
                        "args": wf_args,
                        "max_concurrency": args.max_concurrency,
                        **analysis,
                    },
                    meta={"dry_run": True},
                ),
                None,
                None,
            )
        if args.detach:
            import uuid

            wf_id = args.wf_id or f"wf_{uuid.uuid4().hex[:12]}"
            logs_dir = store.root / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            argv = [
                sys.executable,
                "-m",
                "corral",
                "workflow",
                "--root",
                str(store.root),
                "run",
                str(script_file),
                "--wf-id",
                wf_id,
                "--max-concurrency",
                str(args.max_concurrency),
                "--name",
                wf_name,
                "--json",
            ]
            if args.cwd:
                argv.extend(["--cwd", args.cwd])
            for kv in args.arg_items:
                argv.extend(["--arg", kv])
            # The editable plugin launcher adds its package root only to the parent's sys.path.
            env = os.environ.copy()
            package_root = str(Path(__file__).resolve().parents[1])
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = os.pathsep.join((package_root, existing)) if existing else package_root
            with open(logs_dir / f"{wf_id}.log", "ab") as log_fp:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fp,
                    stderr=subprocess.STDOUT,
                    cwd=args.cwd or os.getcwd(),
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                )
            return (
                ok_envelope(
                    {
                        "wf_id": wf_id,
                        "name": wf_name,
                        "status": "starting",
                        "pid": proc.pid,
                        "log_path": str(logs_dir / f"{wf_id}.log"),
                    }
                ),
                None,
                None,
            )

        engine = WorkflowEngine(store, actor=actor, corral_bin=cbin, auto_scan=True)
        run_rec = asyncio.run(
            engine.run_script(
                source,
                name=wf_name,
                script_path=str(script_file),
                args=wf_args,
                cwd=args.cwd,
                max_concurrency=args.max_concurrency,
                wf_id=args.wf_id,
            )
        )
        return ok_envelope({"workflow": run_rec}), None, None

    if cmd == "list":
        runs = store.list_workflow_runs(status=args.status, limit=args.limit)
        tsv = None
        if args.format == "tsv":
            tsv = ["# wf_id\tstatus\tname\tphase\tdone/total\telapsed_ms"]
            for r in runs:
                c = r["counts"]
                tsv.append(
                    f"{r['wf_id']}\t{r['status']}\t{r['name']}\t{r.get('active_phase') or '-'}\t"
                    f"{c['completed']}/{c['total']}\t{r.get('elapsed_ms') or 0}"
                )
        return ok_envelope({"count": len(runs), "workflows": runs}), tsv, None

    if cmd == "status":
        run_rec = store.get_workflow_run(args.wf_id)
        return ok_envelope({"workflow": run_rec}), None, None

    if cmd == "graph":
        graph = store.get_graph(args.wf_id)
        ascii_text = render_graph_ascii(graph)
        if args.format == "ascii":
            return ok_envelope(graph), ascii_text.splitlines(), ascii_text
        return ok_envelope(graph), None, ascii_text

    if cmd == "events":
        ev = store.list_events(args.wf_id, after=args.after, limit=args.limit)
        return ok_envelope(ev), None, None

    if cmd == "resume":
        run_rec = store.get_workflow_run(args.wf_id)
        script_path = run_rec.get("script_path")
        saved_source = store.get_workflow_script_source(args.wf_id)
        saved_sha = run_rec.get("script_sha256")
        resume_cwd = run_rec.get("cwd")
        disk_exists = bool(script_path and Path(script_path).is_file())
        disk_source = Path(script_path).read_text(encoding="utf-8") if disk_exists and script_path else None
        disk_sha = (
            hashlib.sha256(disk_source.encode("utf-8")).hexdigest()[:16] if disk_source is not None else None
        )
        script_changed = bool(saved_sha and disk_sha and disk_sha != saved_sha)

        if args.use_saved_script:
            if saved_source is None:
                raise WorkflowError(
                    "not_found",
                    f"No saved script snapshot exists for workflow '{args.wf_id}'.",
                    EXIT_NOT_FOUND,
                )
            source = saved_source
        elif not disk_exists:
            if saved_source is not None:
                raise WorkflowError(
                    "not_found",
                    f"Original script file for '{args.wf_id}' is missing: {script_path}",
                    EXIT_NOT_FOUND,
                    hint="Re-run with --use-saved-script to resume from the saved script snapshot.",
                    next_commands=[f"{cbin} workflow resume {args.wf_id} --use-saved-script"],
                )
            raise WorkflowError(
                "not_found", f"Original script file for '{args.wf_id}' is missing: {script_path}", EXIT_NOT_FOUND
            )
        elif script_changed and not args.allow_script_change:
            raise WorkflowError(
                "script_changed",
                (
                    f"Workflow script '{script_path}' changed since run '{args.wf_id}' started "
                    f"(saved {saved_sha}, current {disk_sha})."
                ),
                EXIT_CONFLICT,
                hint=(
                    "Pass --use-saved-script to resume the original snapshot, or "
                    "--allow-script-change to recompute changed and downstream steps."
                ),
                next_commands=[
                    f"{cbin} workflow resume {args.wf_id} --use-saved-script",
                    f"{cbin} workflow resume {args.wf_id} --allow-script-change",
                ],
                context={
                    "wf_id": args.wf_id,
                    "script_path": script_path,
                    "saved_sha256": saved_sha,
                    "current_sha256": disk_sha,
                },
            )
        else:
            assert disk_source is not None
            source = (
                disk_source
                if args.allow_script_change
                else (saved_source if saved_source is not None else disk_source)
            )

        overrides = _parse_kv_args(args.override_items)
        if args.dry_run:
            return (
                ok_envelope(
                    {
                        "dry_run": True,
                        "wf_id": args.wf_id,
                        "cwd": resume_cwd,
                        "script_changed": script_changed,
                        "script_source": "saved_snapshot" if args.use_saved_script else "disk",
                        "from_step": args.from_step,
                        "override_targets": overrides,
                    },
                    meta={"dry_run": True},
                ),
                None,
                None,
            )
        if args.from_step:
            store.clear_checkpoints_from(args.wf_id, args.from_step, actor=actor)
        engine = WorkflowEngine(store, actor=actor, corral_bin=cbin, auto_scan=True)
        resumed = asyncio.run(
            engine.run_script(
                source,
                name=run_rec["name"],
                script_path=script_path,
                args=run_rec["args"],
                cwd=resume_cwd,
                max_concurrency=run_rec["max_concurrency"],
                wf_id=args.wf_id,
                resume=True,
                override_targets={k: str(v) for k, v in overrides.items()},
                update_script_snapshot=bool(args.allow_script_change),
            )
        )
        return ok_envelope({"workflow": resumed}), None, None

    if cmd == "cancel":
        run_rec = store.get_workflow_run(args.wf_id)
        if args.dry_run:
            return (
                ok_envelope(
                    {
                        "dry_run": True,
                        "wf_id": args.wf_id,
                        "current_status": run_rec["status"],
                        "owner_pid": run_rec.get("owner_pid"),
                        "would_cancel": run_rec["status"] in {"running", "cancelling"},
                    },
                    meta={"dry_run": True},
                ),
                None,
                None,
            )
        res = store.request_cancel_workflow(args.wf_id, actor=actor)
        wf_rec = res["workflow"]
        owner_pid = res.get("owner_pid")
        if not res["already_terminal"] and res["owner_alive"] and owner_pid:
            try:
                os.kill(owner_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass
            deadline = time.monotonic() + max(0.0, float(args.wait))
            while time.monotonic() < deadline:
                wf_rec = store.get_workflow_run(args.wf_id)
                if wf_rec["status"] in {"cancelled", "completed", "failed"}:
                    break
                if not _pid_alive(owner_pid):
                    wf_rec = store.finalize_cancelled_workflow(args.wf_id, actor=actor)
                    break
                time.sleep(0.05)
            else:
                wf_rec = store.get_workflow_run(args.wf_id)
                if wf_rec["status"] not in {"cancelled", "completed", "failed"}:
                    if not _pid_alive(owner_pid):
                        wf_rec = store.finalize_cancelled_workflow(args.wf_id, actor=actor)
                    elif args.force:
                        try:
                            os.kill(owner_pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
                        kill_deadline = time.monotonic() + 1.0
                        while time.monotonic() < kill_deadline and _pid_alive(owner_pid):
                            time.sleep(0.05)
                        if not _pid_alive(owner_pid):
                            wf_rec = store.finalize_cancelled_workflow(args.wf_id, actor=actor)
        return (
            ok_envelope(
                {
                    "workflow": wf_rec,
                    "cancel_confirmed": wf_rec["status"] == "cancelled",
                    "already_terminal": res["already_terminal"],
                }
            ),
            None,
            None,
        )

    if cmd == "script":
        run_rec = store.get_workflow_run(args.wf_id)
        saved_source = store.get_workflow_script_source(args.wf_id)
        if saved_source is None:
            script_path = run_rec.get("script_path")
            if script_path and Path(script_path).is_file():
                saved_source = Path(script_path).read_text(encoding="utf-8")
            else:
                raise WorkflowError(
                    "not_found",
                    f"No saved script snapshot exists for workflow '{args.wf_id}'.",
                    EXIT_NOT_FOUND,
                )
        out_path: str | None = None
        if args.out_file:
            target_out = Path(args.out_file).expanduser().resolve()
            target_out.parent.mkdir(parents=True, exist_ok=True)
            target_out.write_text(saved_source, encoding="utf-8")
            out_path = str(target_out)
        data = {
            "wf_id": args.wf_id,
            "name": run_rec["name"],
            "status": run_rec["status"],
            "cwd": run_rec.get("cwd"),
            "script_path": run_rec.get("script_path"),
            "script_sha256": run_rec.get("script_sha256"),
            "args": run_rec.get("args"),
            "output_path": out_path,
            "source": saved_source,
        }
        return ok_envelope(data), None, saved_source

    if cmd == "playbook":
        pb_cmd = args.playbook_cmd
        if pb_cmd == "list":
            items = discover_playbooks(store, cwd=args.cwd, scope=args.scope, tag=args.tag)
            tsv = None
            if args.format == "tsv":
                tsv = ["# name\tscope\tversion\truns\tsuccess_rate\tavg_ms\tdescription"]
                for p in items:
                    sr = f"{p['success_rate']:.2f}" if p["success_rate"] is not None else "-"
                    avg = str(p["avg_elapsed_ms"]) if p["avg_elapsed_ms"] is not None else "-"
                    tsv.append(
                        f"{p['name']}\t{p['scope']}\t{p['version']}\t{p['runs_count']}\t{sr}\t{avg}\t{p['description']}"
                    )
            return ok_envelope({"count": len(items), "playbooks": items}), tsv, None
        if pb_cmd == "show":
            data = show_playbook(
                store,
                args.name,
                cwd=args.cwd,
                scope=args.scope,
                include_source=not args.no_source,
            )
            return ok_envelope({"playbook": data}), None, data.get("source")
        if pb_cmd == "save":
            if args.in_file:
                src_path = Path(args.in_file).expanduser().resolve()
                if not src_path.is_file():
                    raise WorkflowError("not_found", f"Source script file not found: {src_path}", EXIT_NOT_FOUND)
                src_text = src_path.read_text(encoding="utf-8")
            else:
                assert args.from_run is not None
                _ = store.get_workflow_run(args.from_run)
                src_text = store.get_workflow_script_source(args.from_run)
                if src_text is None:
                    raise WorkflowError(
                        "not_found",
                        f"No saved script snapshot exists for workflow '{args.from_run}'.",
                        EXIT_NOT_FOUND,
                    )
            res = save_playbook(
                store,
                args.name,
                source=src_text,
                scope=args.scope,
                cwd=args.cwd,
                expected_version=args.version,
                dry_run=args.dry_run,
                actor=actor,
            )
            return ok_envelope(res, meta={"dry_run": args.dry_run}), None, None
        if pb_cmd == "delete":
            res = delete_playbook(
                store,
                args.name,
                scope=args.scope,
                cwd=args.cwd,
                expected_version=args.version,
                dry_run=args.dry_run,
                actor=actor,
            )
            return ok_envelope(res, meta={"dry_run": args.dry_run}), None, None
        if pb_cmd == "export-skill":
            res = export_playbook_skill(
                store,
                args.name,
                scope=args.scope,
                dest=args.dest,
                cwd=args.cwd,
                skill_name=args.skill_name,
                dry_run=args.dry_run,
                actor=actor,
                corral_bin=cbin,
            )
            return ok_envelope(res, meta={"dry_run": args.dry_run}), None, None
        if pb_cmd == "unexport-skill":
            res = unexport_playbook_skill(
                store,
                args.name,
                scope=args.scope,
                dest=args.dest,
                cwd=args.cwd,
                skill_name=args.skill_name,
                dry_run=args.dry_run,
                actor=actor,
            )
            return ok_envelope(res, meta={"dry_run": args.dry_run}), None, None
        raise WorkflowError(
            "usage_error",
            "Use 'playbook list|show|save|delete|export-skill|unexport-skill'.",
            EXIT_USAGE,
            next_commands=[f"{cbin} workflow playbook list --format tsv"],
        )

    if cmd == "finish":
        wf_id, step_id = _resolve_worker_ids(args)
        node = store.get_node(wf_id, step_id)
        if args.json_file:
            path = Path(args.json_file).expanduser()
            try:
                raw_text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise WorkflowError("usage_error", f"Cannot read --json-file: {exc}", EXIT_USAGE) from exc
        elif args.from_stdin:
            raw_text = sys.stdin.read()
        else:
            raw_text = args.json_inline or ""
        try:
            payload = json.loads(raw_text)
        except ValueError as exc:
            raise WorkflowError(
                "schema_validation_failed",
                f"Invalid JSON payload: {exc}",
                EXIT_USAGE,
                hint="Fix JSON syntax and re-run 'corral workflow finish'.",
            ) from exc
        schema = node.get("schema")
        if isinstance(schema, dict):
            violations = validate_json(payload, schema)
            if violations:
                first = violations[0]
                raise WorkflowError(
                    "schema_validation_failed",
                    (
                        f"Output failed JSON Schema validation ({len(violations)} error(s)): "
                        f"{first['path']}: {first['message']}"
                    ),
                    EXIT_USAGE,
                    hint=(
                        "Fix the fields listed in error.context.violations and re-run "
                        "'corral workflow finish' in this same turn."
                    ),
                    context={"wf_id": wf_id, "step_id": step_id, "violations": violations},
                )
        saved = store.commit_step_output(wf_id, step_id, payload, actor=actor)
        return (
            ok_envelope({"wf_id": wf_id, "step_id": step_id, "committed": True, "output": saved["output"]}),
            None,
            None,
        )

    if cmd == "schema":
        wf_id, step_id = _resolve_worker_ids(args)
        node = store.get_node(wf_id, step_id)
        return ok_envelope({"wf_id": wf_id, "step_id": step_id, "schema": node.get("schema")}), None, None

    if cmd == "log":
        wf_id, step_id = _resolve_worker_ids(args)
        saved = store.record_step_log(wf_id, step_id, args.message)
        return ok_envelope({"wf_id": wf_id, "step_id": step_id, "progress_note": saved["progress_note"]}), None, None

    raise WorkflowError("usage_error", f"Unknown workflow command: {cmd}", EXIT_USAGE)


def main(argv: list[str], *, prog: str = "corral workflow") -> int:
    """Entry point called lazily by ``corral.bootstrap`` for ``workflow`` / ``wf``."""
    parser = _build_parser(prog=prog)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_OK
    except WorkflowError as exc:
        _emit(exc.to_envelope(), json_mode=True, compact=False)
        return exc.exit_code

    json_mode = bool(getattr(args, "json_mode", False)) or not sys.stdout.isatty()
    compact = bool(getattr(args, "compact", False))
    store = WorkflowStore(getattr(args, "root", None))
    try:
        payload, tsv_lines, human_text = _dispatch_parsed(args, store)
    except WorkflowError as exc:
        _emit(exc.to_envelope(), json_mode=True, compact=compact)
        return exc.exit_code
    except Exception as exc:
        err = WorkflowError("internal_error", f"{type(exc).__name__}: {exc}", EXIT_ERROR)
        _emit(err.to_envelope(), json_mode=True, compact=compact)
        return err.exit_code

    _emit(payload, json_mode=json_mode, compact=compact, tsv_lines=tsv_lines, human_text=human_text)
    return EXIT_OK


def entrypoint(argv: list[str] | None = None) -> int:
    """Console script entrypoint for standalone ``corral-workflow`` execution."""
    raw = list(sys.argv[1:] if argv is None else argv)
    prog = "corral workflow"
    if raw and raw[0] in {"workflow", "wf"}:
        prog = f"corral {raw[0]}"
        raw = raw[1:]
    elif raw[:1] == ["describe"]:
        print(json.dumps({"commands": WORKFLOW_COMMANDS}, ensure_ascii=False, indent=2))
        return EXIT_OK
    return main(raw, prog=prog)
