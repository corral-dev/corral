"""Comprehensive tests for ``corral workflow`` (Phase 1):

- Pure-Python JSON Schema validation with JSONPath errors
- Zero-quota local inventory scanner + cloud capability registry ETag sync & intersection
- Agent-friendly CLI metadata contract (--version optimistic locks, --dry-run, audit, conflicts)
- Shared circuit breaker, 5-class failure degradation, exclude_runtimes & dirty-workspace safety
- Python 6-primitive engine (phase/agent/llm/shell/parallel/pipeline), 25-way concurrency & checkpoint resume
- Pure-CLI ``corral workflow finish`` handshake & bounded missing-submission nudges
- Git worktree isolation without mutating dirty main workspace
- Main session list hiding across ``corral list``, ``corral search``, and ``SessionStore``
"""

from __future__ import annotations

import asyncio
import gc
import http.server
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from corral_workflow import cli as wf_cli
from corral_workflow.engine import (
    AgentBackend,
    AgentSessionRef,
    BackendTurnOutcome,
    LLMBackend,
    WorkflowEngine,
    analyze_script_dry_run,
    render_graph_ascii,
)
from corral_workflow.errors import EXIT_CONFLICT, EXIT_OK, EXIT_USAGE, WorkflowError
from corral_workflow.registry import load_active_registry, sync_registry
from corral_workflow.routing import (
    FAILURE_CALLER,
    FAILURE_DETERMINISTIC,
    FAILURE_HARD_QUOTA,
    FAILURE_TRANSIENT,
    FAILURE_UNCERTAIN,
    build_catalog,
    classify_failure,
    resolve_step_candidates,
)
from corral_workflow.scanner import scan_local_inventory
from corral_workflow.schema import validate_json
from corral_workflow.store import WorkflowStore
from corral_workflow.visibility import is_workflow_session, workflow_marker


def _run_cli(argv: list[str], *, root: Path, extra_env: dict[str, str] | None = None) -> tuple[int, dict]:
    buf = io.StringIO()
    env_patch = {"CORRAL_WORKFLOW_ROOT": str(root)}
    if extra_env:
        env_patch.update(extra_env)
    with mock.patch.dict(os.environ, env_patch, clear=False), redirect_stdout(buf):
        code = wf_cli.main(["--root", str(root), *argv])
    raw = buf.getvalue().strip()
    return code, json.loads(raw) if raw else {}


class FakeAgentBackend(AgentBackend):
    """Deterministic async fake backend simulating multi-runtime workers & pure-CLI finish."""

    def __init__(self, store: WorkflowStore, handler) -> None:
        self.store = store
        self.handler = handler
        self.created: list[dict] = []
        self.closed: list[str] = []
        self._counter = 0

    async def create_session(self, *, runtime, cwd, model, wf_id, node_id, actor) -> AgentSessionRef:
        self._counter += 1
        sid = f"sess-{self._counter}"
        ref = f"{runtime}:native-{self._counter}"
        self.created.append({"runtime": runtime, "cwd": cwd, "model": model, "wf_id": wf_id, "node_id": node_id})
        return AgentSessionRef(
            session_id=sid, runtime=runtime, session_ref=ref, keepalive_name=f"corral-wf-{sid}", model=model
        )

    async def run_turn(self, session, *, prompt, request_id, timeout, actor) -> BackendTurnOutcome:
        return await self.handler(self.store, session, prompt, request_id)

    async def close_session(self, session, *, actor) -> None:
        self.closed.append(session.session_id)


class FakeLLMBackend(LLMBackend):
    def __init__(self, fn) -> None:
        self.fn = fn
        self.calls: list[dict] = []

    async def complete(self, *, prompt, model, schema, timeout):
        self.calls.append({"prompt": prompt, "model": model})
        return self.fn(prompt, model, schema)


class SchemaValidatorTests(unittest.TestCase):
    def test_valid_and_invalid_json_paths(self) -> None:
        schema = {
            "type": "object",
            "required": ["approved", "blockers", "score"],
            "additionalProperties": False,
            "properties": {
                "approved": {"type": "boolean"},
                "blockers": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                "score": {"type": "integer", "minimum": 0, "maximum": 100},
            },
        }
        self.assertEqual(validate_json({"approved": True, "blockers": ["bug"], "score": 90}, schema), [])
        # Booleans must never pass integer checks in Python
        errs = validate_json({"approved": "yes", "blockers": ["ok", 42], "score": True, "extra": 1}, schema)
        paths = {e["path"] for e in errs}
        self.assertIn("$.approved", paths)
        self.assertIn("$.blockers[1]", paths)
        self.assertIn("$.score", paths)
        self.assertIn("$.extra", paths)


class ScannerRegistryAndMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "wf"
        self.cache = Path(self.tmp.name) / "cache"
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir(parents=True)
        self.store = WorkflowStore(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _populate_fake_home(self) -> None:
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"model": "claude-opus-4"}), encoding="utf-8")
        (self.home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")

        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".codex" / "config.toml").write_text('model = "gpt-5-codex"\n', encoding="utf-8")
        (self.home / ".codex" / "auth.json").write_text("{}", encoding="utf-8")

        (self.home / ".config" / "opencode").mkdir(parents=True)
        (self.home / ".config" / "opencode" / "opencode.json").write_text(
            json.dumps({"model": "deepseek-v4-flash", "provider": {"local": {}}}),
            encoding="utf-8",
        )

        (self.home / ".pi" / "agent").mkdir(parents=True)
        (self.home / ".pi" / "agent" / "settings.json").write_text(
            json.dumps({"defaultProvider": "subswap", "defaultModel": "deepseek-v4.1-flash"}),
            encoding="utf-8",
        )

    def test_zero_config_scan_intersects_bundled_cloud_registry(self) -> None:
        self._populate_fake_home()
        installed = {
            "claude": "/usr/local/bin/claude",
            "codex": "/usr/local/bin/codex",
            "opencode": "/usr/local/bin/opencode",
            "pi": "/usr/local/bin/pi",
        }
        with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": str(self.cache)}, clear=False):
            report = scan_local_inventory(
                self.store,
                home_dir=self.home,
                which_fn=lambda exe: installed.get(exe),
                version_fn=lambda exe: f"{Path(exe).name} 1.0.0",
                gateway_models_fn=lambda: (["budget-chat"], "http://127.0.0.1:4000/v1"),
            )
            self.assertGreaterEqual(report["targets_discovered"], 5)
            cat = build_catalog(self.store, explain=True)
            profiles = {p["id"]: p for p in cat["profiles"]}
            # UI design prefers Claude Opus first on this machine
            self.assertEqual(profiles["ui-design"]["candidates"][0], "claude-claude-opus-4")
            # Code review prefers Codex GPT first on this machine
            self.assertEqual(profiles["code-review"]["candidates"][0], "codex-gpt-5-codex")
            # Cheap fan-out prefers DeepSeek Flash (Pi/OpenCode) and avoids Claude Opus
            self.assertIn("cheap-fanout", profiles)
            self.assertNotIn("claude-claude-opus-4", profiles["cheap-fanout"]["candidates"])
            # Fast router prefers gateway budget-chat first
            self.assertEqual(profiles["fast-router"]["candidates"][0], "gw-budget-chat")

    def test_cloud_registry_etag_sync_and_304(self) -> None:
        manifest = load_active_registry(self.store)["manifest"]
        manifest["registry_version"] = "2026.11.27-cloud-test"
        body_bytes = json.dumps(manifest).encode("utf-8")
        etag = '"v-test-etag"'

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(body_bytes)

            def log_message(self, format, *args):  # noqa: A002
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{port}/registry.json"
            with mock.patch.dict(
                os.environ,
                {"CORRAL_CACHE_DIR": str(self.cache), "CORRAL_WORKFLOW_REGISTRY_URL": url},
                clear=False,
            ):
                res1 = sync_registry(self.store, force=True)
                self.assertEqual(res1["status"], "updated")
                self.assertEqual(res1["registry_version"], "2026.11.27-cloud-test")
                # Force expired cache check -> should hit 304 Not Modified cleanly
                cache_file = self.cache / "workflow-registry.json"
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                cached["fetched_at"] = 0
                cache_file.write_text(json.dumps(cached), encoding="utf-8")
                res2 = sync_registry(self.store, force=False)
                self.assertEqual(res2["status"], "not_modified")
        finally:
            server.shutdown()
            server.server_close()

    def test_metadata_cli_version_optimistic_lock_dry_run_and_referential_integrity(self) -> None:
        # 1. Dry-run create leaves store absent
        code, env = _run_cli(
            [
                "target",
                "set",
                "ui-claude",
                "--runtime",
                "claude",
                "--model",
                "claude-opus-4",
                "--cost",
                "high",
                "--strengths",
                "ui-design,aesthetics",
                "--dry-run",
            ],
            root=self.root,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(env["meta"]["dry_run"])
        self.assertFalse(self.store.exists())

        # 2. Real create
        code, env = _run_cli(
            [
                "target",
                "set",
                "ui-claude",
                "--runtime",
                "claude",
                "--model",
                "claude-opus-4",
                "--cost",
                "high",
                "--strengths",
                "ui-design,aesthetics",
                "--max-concurrency",
                "4",
            ],
            root=self.root,
        )
        self.assertEqual(code, EXIT_OK)
        v1 = env["data"]["target"]["version"]
        self.assertEqual(len(v1), 12)

        # 3. Updating without --version fails with exit 2 (confirmation_required)
        code, env = _run_cli(["target", "set", "ui-claude", "--runtime", "claude", "--notes", "no-ver"], root=self.root)
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(env["error"]["code"], "confirmation_required")

        # 4. Updating with wrong --version fails with exit 5 (conflict)
        code, env = _run_cli(
            ["target", "set", "ui-claude", "--runtime", "claude", "--notes", "bad", "--version", "deadbeef0000"],
            root=self.root,
        )
        self.assertEqual(code, EXIT_CONFLICT)
        self.assertEqual(env["error"]["code"], "conflict")

        # 5. Updating with exact --version succeeds and rotates version
        code, env = _run_cli(
            ["target", "set", "ui-claude", "--runtime", "claude", "--notes", "great taste", "--version", v1],
            root=self.root,
        )
        self.assertEqual(code, EXIT_OK)
        v2 = env["data"]["target"]["version"]
        self.assertNotEqual(v1, v2)

        # 6. Create profile referencing ui-claude; deleting ui-claude without --force conflicts
        code, env = _run_cli(
            ["profile", "set", "my-ui", "--candidates", "ui-claude", "--description", "UI work"], root=self.root
        )
        self.assertEqual(code, EXIT_OK)
        pver = env["data"]["profile"]["version"]

        code, env = _run_cli(["target", "delete", "ui-claude", "--version", v2], root=self.root)
        self.assertEqual(code, EXIT_CONFLICT)
        self.assertIn("my-ui", env["error"]["context"]["referencing_profiles"])

        # 7. Export and dry-run import into a fresh root
        export_path = Path(self.tmp.name) / "export.json"
        code, env = _run_cli(["meta", "export", "--out", str(export_path)], root=self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(env["data"]["targets"], 1)
        self.assertEqual(env["data"]["profiles"], 1)

        root2 = Path(self.tmp.name) / "wf2"
        code, env = _run_cli(["meta", "import", "--file", str(export_path), "--dry-run"], root=root2)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(env["data"]["targets_created"], ["ui-claude"])
        self.assertFalse(WorkflowStore(root2).exists())

        # Reset/delete profile
        code, env = _run_cli(["profile", "delete", "my-ui", "--version", pver], root=self.root)
        self.assertEqual(code, EXIT_OK)


class CircuitBreakerAndDegradationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "wf"
        self.store = WorkflowStore(self.root)
        self.store.upsert_user_target(
            {
                "id": "claude-primary",
                "origin": "user_custom",
                "kind": "runtime",
                "runtime": "claude",
                "model": "claude-opus-4",
                "strengths": ["code-review"],
                "enabled": True,
            },
            expected_version=None,
            actor="test",
        )
        self.store.upsert_user_target(
            {
                "id": "codex-backup",
                "origin": "user_custom",
                "kind": "runtime",
                "runtime": "codex",
                "model": "gpt-5-codex",
                "strengths": ["code-review"],
                "enabled": True,
            },
            expected_version=None,
            actor="test",
        )
        self.store.upsert_user_profile(
            {
                "id": "code-review",
                "origin": "user_override",
                "description": "Review",
                "candidates": ["claude-primary", "codex-backup"],
                "degradation_policy": "pause_on_exhaust",
            },
            expected_version=None,
            actor="test",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_failure_classification_matrix(self) -> None:
        self.assertEqual(classify_failure("usage_error")["failure_class"], FAILURE_CALLER)
        self.assertEqual(classify_failure("executable_missing")["failure_class"], FAILURE_DETERMINISTIC)
        self.assertEqual(classify_failure(None, "429 AccountQuotaExceeded")["failure_class"], FAILURE_HARD_QUOTA)
        self.assertEqual(
            classify_failure(None, "HTTP 503 overloaded_error", http_status=503)["failure_class"], FAILURE_TRANSIENT
        )
        self.assertEqual(
            classify_failure(None, "unknown", execution_state="unknown")["failure_class"], FAILURE_UNCERTAIN
        )

    def test_shared_circuit_breaker_and_cross_model_degradation(self) -> None:
        # 10 parallel steps hit code-review; claude-primary fails with hard quota on the first call,
        # tripping the shared circuit breaker so remaining siblings degrade straight to codex-backup.
        claude_hits = 0

        async def handler(
            store: WorkflowStore, session: AgentSessionRef, prompt: str, request_id: str
        ) -> BackendTurnOutcome:
            nonlocal claude_hits
            if session.runtime == "claude":
                claude_hits += 1
                return BackendTurnOutcome(
                    state="failed",
                    error_code="quota_exhausted",
                    error_message="429 AccountQuotaExceeded",
                    observed_runtime="claude",
                )
            # Codex commits valid structured output via pure-CLI finish contract
            wf_id, step_id = _extract_marker(prompt)
            store.commit_step_output(wf_id, step_id, {"ok": True, "reviewer": session.runtime}, actor="worker")
            return BackendTurnOutcome(
                state="completed",
                final_response="done",
                observed_runtime="codex",
                observed_model="gpt-5-codex",
                session_ref=session.session_ref,
            )

        script = """
phase("Parallel Review")
items = list(range(10))
results = await parallel(
    items,
    lambda i: agent(
        f"Review file {i}",
        capability="code-review",
        read_only=True,
        step_key=f"rev:{i}",
        schema={
            "type": "object",
            "required": ["ok", "reviewer"],
            "properties": {"ok": {"type": "boolean"}, "reviewer": {"type": "string"}},
        },
    ),
    max_concurrency=10,
    label="10-way Review",
)
return [r.value for r in results]
"""
        backend = FakeAgentBackend(self.store, handler)
        engine = WorkflowEngine(self.store, backend=backend, sleep_fn=_no_sleep)
        run = asyncio.run(engine.run_script(script, name="quota-degrade", max_concurrency=10))
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(run["result"]), 10)
        self.assertTrue(all(item["reviewer"] == "codex" for item in run["result"]))
        # Shared circuit breaker tripped on hard quota
        health = self.store.get_target_health("claude-primary")
        self.assertEqual(health["state"], "open")
        self.assertEqual(health["failure_class"], FAILURE_HARD_QUOTA)
        # All 10 single-shot sessions were closed cleanly to keep active panes bounded
        self.assertEqual(len(backend.closed), len(backend.created))

        # Resetting health via CLI requires --version
        code, env = _run_cli(["health", "--reset", "claude-primary", "--version", health["version"]], root=self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.store.get_target_health("claude-primary")["state"], "closed")

    def test_exclude_runtimes_enforces_cross_model_and_fails_closed(self) -> None:
        # Exclude claude -> resolves to codex-backup
        res = resolve_step_candidates(self.store, capability="code-review", exclude_runtimes=["claude"])
        self.assertEqual([c["target_id"] for c in res["candidates"]], ["codex-backup"])
        # Exclude both -> returns empty candidates (never silently substitutes unqualified runtime)
        res2 = resolve_step_candidates(self.store, capability="code-review", exclude_runtimes=["claude", "codex"])
        self.assertEqual(res2["candidates"], [])

    def test_mutating_inherit_workspace_refuses_blind_fallback(self) -> None:
        async def handler(store, session, prompt, request_id):
            return BackendTurnOutcome(
                state="failed",
                error_code="quota_exhausted",
                error_message="429 AccountQuotaExceeded",
                observed_runtime=session.runtime,
                had_tool_activity=True,
            )

        script = 'await agent("Edit shared files", capability="code-review", workspace="inherit", read_only=False)'
        engine = WorkflowEngine(self.store, backend=FakeAgentBackend(self.store, handler), sleep_fn=_no_sleep)
        with self.assertRaises(WorkflowError) as ctx:
            asyncio.run(engine.run_script(script, name="dirty-guard"))
        self.assertEqual(ctx.exception.code, "degradation_blocked_dirty_workspace")


class EnginePrimitivesFinishCheckpointAndWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "wf"
        self.store = WorkflowStore(self.root)
        self.store.upsert_user_target(
            {
                "id": "codex-main",
                "origin": "user_custom",
                "kind": "runtime",
                "runtime": "codex",
                "model": "gpt-5-codex",
                "strengths": ["deep-impl", "code-review", "cheap-fanout"],
                "enabled": True,
            },
            expected_version=None,
            actor="test",
        )
        self.store.upsert_user_target(
            {
                "id": "claude-rev",
                "origin": "user_custom",
                "kind": "runtime",
                "runtime": "claude",
                "model": "claude-opus-4",
                "strengths": ["code-review"],
                "enabled": True,
            },
            expected_version=None,
            actor="test",
        )
        self.store.upsert_user_target(
            {
                "id": "gw-fast",
                "origin": "user_custom",
                "kind": "gateway",
                "runtime": None,
                "model": "budget-chat",
                "strengths": ["fast-router"],
                "enabled": True,
            },
            expected_version=None,
            actor="test",
        )
        self.store.upsert_user_profile(
            {
                "id": "deep-impl",
                "origin": "user_override",
                "candidates": ["codex-main"],
                "degradation_policy": "strict",
            },
            expected_version=None,
            actor="test",
        )
        self.store.upsert_user_profile(
            {
                "id": "code-review",
                "origin": "user_override",
                "candidates": ["codex-main", "claude-rev"],
                "degradation_policy": "strict",
            },
            expected_version=None,
            actor="test",
        )
        self.store.upsert_user_profile(
            {
                "id": "cheap-fanout",
                "origin": "user_override",
                "candidates": ["codex-main"],
                "degradation_policy": "strict",
            },
            expected_version=None,
            actor="test",
        )
        self.store.upsert_user_profile(
            {"id": "fast-router", "origin": "user_override", "candidates": ["gw-fast"], "degradation_policy": "strict"},
            expected_version=None,
            actor="test",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pure_cli_finish_validation_nudge_and_25_way_parallel_with_resume(self) -> None:
        attempts_by_key: dict[str, int] = {}
        fail_once = {"flag": True}

        async def handler(
            store: WorkflowStore, session: AgentSessionRef, prompt: str, request_id: str
        ) -> BackendTurnOutcome:
            wf_id, step_id = _extract_marker(prompt)
            node = store.get_node(wf_id, step_id)
            key = node["step_key"] or step_id
            attempts_by_key[key] = attempts_by_key.get(key, 0) + 1

            # Step 'step-nudge' forgets to call finish on turn 1, then calls real CLI finish on nudge!
            if key == "step-nudge" and "nudge" not in request_id:
                return BackendTurnOutcome(
                    state="completed",
                    final_response="forgot finish",
                    observed_runtime=session.runtime,
                    session_ref=session.session_ref,
                )

            if key == "step-fail-first" and fail_once["flag"]:
                fail_once["flag"] = False
                return BackendTurnOutcome(
                    state="failed",
                    error_code="usage_error",
                    error_message="boom first run",
                    observed_runtime=session.runtime,
                )

            # Exercise the REAL pure-CLI `corral workflow finish` validation path!
            bad_code, bad_env = _run_cli(
                ["finish", "--wf", wf_id, "--step", step_id, "--json", '{"ok": "not-bool"}'], root=self.root
            )
            if node["schema"] and "ok" in (node["schema"].get("properties") or {}):
                assert bad_code == EXIT_USAGE
                assert bad_env["error"]["code"] == "schema_validation_failed"
            ok_code, _ = _run_cli(
                ["finish", "--wf", wf_id, "--step", step_id, "--json", json.dumps({"ok": True, "key": key})],
                root=self.root,
            )
            assert ok_code == EXIT_OK
            _run_cli(["log", "--wf", wf_id, "--step", step_id, f"finished {key}"], root=self.root)
            return BackendTurnOutcome(
                state="completed",
                final_response="submitted",
                observed_runtime=session.runtime,
                session_ref=session.session_ref,
            )

        script = """
phase("1. Plan via LLM + Shell")
plan_schema = {
    "type": "object",
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {"type": "integer"}}},
}
plan = await llm("plan", capability="fast-router", step_key="plan", schema=plan_schema)
sh = await shell("echo hello-workflow", step_key="echo-step")

phase("2. 25-Way Parallel Fan-Out")
schema = {
    "type": "object",
    "required": ["ok", "key"],
    "properties": {"ok": {"type": "boolean"}, "key": {"type": "string"}},
}
fan = await parallel(
    plan["items"],
    lambda i: agent(f"Audit {i}", capability="cheap-fanout", read_only=True, step_key=f"item:{i}", schema=schema),
    max_concurrency=25,
    on_error="collect",
    label="25-Way Audit",
)

phase("3. Nudge + Fail-Then-Resume")
nudged = await agent("Need nudge", capability="deep-impl", read_only=True, step_key="step-nudge", schema=schema)
flaky = await agent("Flaky step", capability="deep-impl", read_only=True, step_key="step-fail-first", schema=schema)

phase("4. Cross-Model Review")
rev = await agent(
    "Review",
    capability="code-review",
    exclude_runtimes=[flaky.observed_runtime],
    read_only=True,
    step_key="step-review",
    schema=schema,
)
return {"shell": sh.output, "fan_count": len(fan), "nudged": nudged.value, "reviewer": rev.observed_runtime}
"""
        script_file = Path(self.tmp.name) / "flow.py"
        script_file.write_text(script, encoding="utf-8")

        # Dry-run verifies syntax & resolution without writing workflow runs
        dry = analyze_script_dry_run(self.store, script, filename=str(script_file))
        self.assertTrue(dry["syntax_ok"])
        self.assertEqual(len(dry["phases_detected"]), 4)
        self.assertEqual(self.store.list_workflow_runs(), [])

        llm_backend = FakeLLMBackend(
            lambda p, m, s: (
                json.dumps({"items": list(range(25))}),
                {"runtime": "gateway", "model_observed": "budget-chat"},
            )
        )
        engine = WorkflowEngine(
            self.store, backend=FakeAgentBackend(self.store, handler), llm_backend=llm_backend, sleep_fn=_no_sleep
        )

        # First run fails on step-fail-first AFTER completing all 25 parallel items and step-nudge
        with self.assertRaises(WorkflowError) as ctx:
            asyncio.run(engine.run_script(script, name="big-flow", script_path=str(script_file), max_concurrency=25))
        wf_id = ctx.exception.context["wf_id"]
        self.assertEqual(attempts_by_key["item:0"], 1)
        self.assertEqual(attempts_by_key["item:24"], 1)
        self.assertEqual(attempts_by_key["step-nudge"], 2)  # initial turn + 1 nudge

        # Resume skips ALL 25 completed parallel items + plan + shell + step-nudge!
        resumed = asyncio.run(
            engine.run_script(
                script, name="big-flow", script_path=str(script_file), max_concurrency=25, wf_id=wf_id, resume=True
            )
        )
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["result"]["fan_count"], 25)
        self.assertEqual(resumed["result"]["reviewer"], "claude")  # excluded codex -> resolved to claude
        self.assertEqual(attempts_by_key["item:0"], 1)  # NOT re-executed on resume
        self.assertEqual(attempts_by_key["item:24"], 1)  # NOT re-executed on resume
        self.assertEqual(len(llm_backend.calls), 1)  # LLM checkpoint reused

        # ASCII graph & events stream work cleanly
        graph = self.store.get_graph(wf_id)
        ascii_tree = render_graph_ascii(graph)
        self.assertIn("25-Way Audit", ascii_tree)
        events = self.store.list_events(wf_id, after=0)
        self.assertGreater(len(events["events"]), 25)

    def test_git_worktree_isolation_preserves_dirty_main_workspace(self) -> None:
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "app.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
        # Simulate user's uncommitted work in main tree
        (repo / "user_draft.txt").write_text("do-not-touch\n", encoding="utf-8")

        async def handler(store, session, prompt, request_id):
            # Agent edits file inside its isolated worktree cwd
            created_info = [c for c in backend.created if c["runtime"] == session.runtime][-1]
            wt_path = Path(created_info["cwd"])
            (wt_path / "app.txt").write_text("changed-in-worktree\n", encoding="utf-8")
            wf_id, step_id = _extract_marker(prompt)
            store.commit_step_output(wf_id, step_id, {"edited": True}, actor="worker")
            return BackendTurnOutcome(
                state="completed",
                final_response="ok",
                observed_runtime=session.runtime,
                session_ref=session.session_ref,
            )

        backend = FakeAgentBackend(self.store, handler)
        engine = WorkflowEngine(self.store, backend=backend, sleep_fn=_no_sleep)
        script = (
            'res = await agent("Modify app.txt", capability="deep-impl", '
            'workspace="worktree", schema={"type": "object", "required": ["edited"]})\n'
            'return {"wt": res.worktree, "dirty": res.diff_summary["dirty"]}'
        )
        run = asyncio.run(engine.run_script(script, name="wt-test", cwd=repo))
        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["result"]["dirty"])
        # Main repo stayed 100% untouched!
        self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "base\n")
        self.assertEqual((repo / "user_draft.txt").read_text(encoding="utf-8"), "do-not-touch\n")

    def test_stateful_multi_turn_agent_handle_and_pipeline(self) -> None:
        turns_seen: list[str] = []

        async def handler(store, session, prompt, request_id):
            turns_seen.append(session.session_id)
            wf_id, step_id = _extract_marker(prompt)
            store.commit_step_output(wf_id, step_id, {"turn": len(turns_seen)}, actor="worker")
            return BackendTurnOutcome(
                state="completed",
                final_response="ok",
                observed_runtime=session.runtime,
                session_ref=session.session_ref,
            )

        script = """
reviewer = agent("Standby reviewer", capability="code-review", read_only=True, role="Reviewer")
t1 = await reviewer("First check", schema={"type": "object", "required": ["turn"]})
t2 = await reviewer("Second check", schema={"type": "object", "required": ["turn"]})
await reviewer.close()
pipe = await pipeline([1, 2, 3], lambda x: x * 2, lambda x: x + 1, label="Math Pipe")
return {"t1": t1.value["turn"], "t2": t2.value["turn"], "pipe": pipe}
"""
        backend = FakeAgentBackend(self.store, handler)
        engine = WorkflowEngine(self.store, backend=backend, sleep_fn=_no_sleep)
        run = asyncio.run(engine.run_script(script, name="stateful-and-pipe"))
        self.assertEqual(run["result"], {"t1": 1, "t2": 2, "pipe": [3, 5, 7]})
        # Both turns reused the EXACT same underlying session!
        self.assertEqual(turns_seen[0], turns_seen[1])
        self.assertEqual(len(backend.created), 1)
        self.assertEqual(len(backend.closed), 1)


class SessionListIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "wf"
        self.store = WorkflowStore(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_workflow_sessions_hidden_from_list_search_and_store(self) -> None:
        self.store.register_workflow_session(
            wf_id="wf_abc123",
            node_id="step-0001",
            phase="phase-1",
            session_ref="codex:wf-child-1",
            keepalive_name="corral-wf-child-1",
        )
        normal = {
            "source": "codex",
            "id": "human-1",
            "title": "Normal chat",
            "first_user_msg": "Fix login",
            "mtime": 200.0,
            "cwd": "/tmp/p",
        }
        wf_by_db = {
            "source": "codex",
            "id": "wf-child-1",
            "title": "Child 1",
            "first_user_msg": "Audit file 1",
            "mtime": 199.0,
            "cwd": "/tmp/p",
        }
        wf_by_marker = {
            "source": "claude",
            "id": "wf-child-2",
            "title": "Child 2",
            "first_user_msg": f"{workflow_marker('wf_abc123', 'step-0002')}\nAudit file 2",
            "mtime": 198.0,
            "cwd": "/tmp/p",
        }

        with mock.patch.dict(os.environ, {"CORRAL_WORKFLOW_ROOT": str(self.root)}, clear=False):
            self.assertFalse(is_workflow_session(normal))
            self.assertTrue(is_workflow_session(wf_by_db))
            self.assertTrue(is_workflow_session(wf_by_marker))

            # Verify generic state-contract DB (hidden-sessions.sqlite3) was populated too.
            import sqlite3

            from corral_workflow.state_bridge import resolve_state_dir

            hidden_db = resolve_state_dir(self.root) / "hidden-sessions.sqlite3"
            self.assertTrue(hidden_db.is_file())
            with sqlite3.connect(hidden_db) as conn:
                rows = {r[0] for r in conn.execute("SELECT session_key FROM hidden_sessions").fetchall()}
            self.assertIn("codex:wf-child-1", rows)
            self.assertIn("corral-wf-child-1", rows)


class CancelResumeDryRunAndWarningRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "wf"
        self.store = WorkflowStore(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cli_cancel_stops_separate_process_and_prevents_second_step_and_overwrite(self) -> None:
        marker = Path(self.tmp.name) / "should_not_exist.marker"
        script_file = Path(self.tmp.name) / "cancel_flow.py"
        script_file.write_text(
            f"""
phase("Step 1")
await shell("sleep 4", step_key="s1")
phase("Step 2")
await shell("touch {marker}", step_key="s2")
return {{"done": True}}
""",
            encoding="utf-8",
        )
        wf_id = "wf_cancel_reg_01"
        plugin_src = str(Path(__file__).resolve().parents[1] / "src")
        env = {
            **os.environ,
            "PYTHONPATH": plugin_src + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
            "CORRAL_WORKFLOW_ROOT": str(self.root),
        }
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "corral_workflow",
                "--root",
                str(self.root),
                "run",
                str(script_file),
                "--wf-id",
                wf_id,
                "--json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            # Wait until workflow is running step s1 and has registered its live owner_pid
            started_s1 = False
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if self.store.exists():
                    try:
                        graph = self.store.get_graph(wf_id)
                    except WorkflowError:
                        graph = None
                    if graph and graph["workflow"].get("owner_pid") == proc.pid:
                        s1_nodes = [n for n in graph["nodes"] if n.get("step_key") == "s1" and n["status"] == "running"]
                        if s1_nodes:
                            started_s1 = True
                            break
                time.sleep(0.05)
            self.assertTrue(started_s1, "Separate workflow CLI process never entered running s1 state")

            t0 = time.monotonic()
            code, cancel_env = _run_cli(["cancel", wf_id, "--wait", "5"], root=self.root)
            cancel_elapsed = time.monotonic() - t0
            self.assertEqual(code, EXIT_OK)
            self.assertTrue(cancel_env["data"]["cancel_confirmed"])
            self.assertEqual(cancel_env["data"]["workflow"]["status"], "cancelled")
            self.assertLess(cancel_elapsed, 3.0, "Cancel should terminate sleep 4 promptly instead of waiting it out")

            proc.wait(timeout=5.0)
            self.assertNotEqual(proc.returncode, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3.0)
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

        # Step 2 must NEVER have run, and workflow status must stay cancelled
        self.assertFalse(marker.exists(), "Second step ran despite cancellation!")
        final_graph = self.store.get_graph(wf_id)
        self.assertEqual(final_graph["workflow"]["status"], "cancelled")
        self.assertIsNone(final_graph["workflow"]["owner_pid"])
        s2_nodes = [n for n in final_graph["nodes"] if n.get("step_key") == "s2"]
        self.assertEqual(s2_nodes, [])

        # A late completion write must never overwrite a cancelled workflow back to completed
        guarded = self.store.update_workflow_run(
            wf_id,
            status="completed",
            result={"late": True},
            finished=True,
            actor="late-writer",
        )
        self.assertEqual(guarded["status"], "cancelled")
        self.assertIsNone(guarded["result"])

    def test_resume_restores_cwd_guards_script_change_and_invalidates_changed_checkpoints(self) -> None:
        work_dir = (Path(self.tmp.name) / "target_workdir").resolve()
        caller_dir = (Path(self.tmp.name) / "caller_workdir").resolve()
        work_dir.mkdir()
        caller_dir.mkdir()

        script_file = Path(self.tmp.name) / "resume_flow.py"
        script_v1 = """
s1 = await shell("echo old && pwd", step_key="s1")
s2 = await shell(f"echo downstream:{s1.output.splitlines()[0]} && pwd", step_key="s2")
return {
    "s1": s1.output,
    "s1_ckpt": s1.from_checkpoint,
    "s2": s2.output,
    "s2_ckpt": s2.from_checkpoint,
    "cwd": CWD,
}
"""
        script_file.write_text(script_v1, encoding="utf-8")

        orig_cwd = os.getcwd()
        try:
            os.chdir(caller_dir)
            code, run_env = _run_cli(["run", str(script_file), "--cwd", str(work_dir)], root=self.root)
            self.assertEqual(code, EXIT_OK)
            wf_id = run_env["data"]["workflow"]["wf_id"]
            res1 = run_env["data"]["workflow"]["result"]
            self.assertEqual(res1["cwd"], str(work_dir))
            self.assertEqual(res1["s1"].splitlines(), ["old", str(work_dir)])
            self.assertEqual(res1["s2"].splitlines(), ["downstream:old", str(work_dir)])
            self.assertFalse(res1["s1_ckpt"])
            self.assertEqual(run_env["data"]["workflow"]["cwd"], str(work_dir))

            # Modify disk script so same step_key s1 outputs "new"
            script_v2 = script_v1.replace("echo old && pwd", "echo new && pwd")
            script_file.write_text(script_v2, encoding="utf-8")

            # Bare resume refuses silent stale-checkpoint reuse with EXIT_CONFLICT
            code_conflict, conflict_env = _run_cli(["resume", wf_id], root=self.root)
            self.assertEqual(code_conflict, EXIT_CONFLICT)
            self.assertEqual(conflict_env["error"]["code"], "script_changed")

            # Explicit --use-saved-script replays original snapshot in original work_dir
            code_saved, saved_env = _run_cli(["resume", wf_id, "--use-saved-script"], root=self.root)
            self.assertEqual(code_saved, EXIT_OK)
            res_saved = saved_env["data"]["workflow"]["result"]
            self.assertEqual(res_saved["cwd"], str(work_dir))
            self.assertTrue(res_saved["s1_ckpt"])
            self.assertEqual(res_saved["s1"].splitlines(), ["old", str(work_dir)])

            # Explicit --allow-script-change restores work_dir and recomputes s1 + downstream s2
            code_changed, changed_env = _run_cli(["resume", wf_id, "--allow-script-change"], root=self.root)
            self.assertEqual(code_changed, EXIT_OK)
            res_changed = changed_env["data"]["workflow"]["result"]
            self.assertEqual(res_changed["cwd"], str(work_dir))
            self.assertFalse(res_changed["s1_ckpt"])
            self.assertFalse(res_changed["s2_ckpt"])
            self.assertEqual(res_changed["s1"].splitlines(), ["new", str(work_dir)])
            self.assertEqual(res_changed["s2"].splitlines(), ["downstream:new", str(work_dir)])
        finally:
            os.chdir(orig_cwd)

    def test_dry_run_does_not_mutate_expired_open_circuit_breaker(self) -> None:
        self.store.upsert_user_target(
            {
                "id": "pi-default",
                "origin": "user_custom",
                "kind": "runtime",
                "runtime": "pi",
                "model": None,
                "strengths": ["cheap-fanout"],
                "enabled": True,
            },
            expected_version=None,
            actor="test",
        )
        # Put pi-default into an expired open circuit state
        expired_now = time.time() - 120.0
        self.store.record_target_failure(
            "pi-default",
            failure_class=FAILURE_HARD_QUOTA,
            reason="429 AccountQuotaExceeded",
            open_immediately=True,
            cooldown_seconds=60.0,
            now=expired_now,
        )
        before = self.store.get_target_health("pi-default")
        self.assertEqual(before["state"], "open")
        self.assertIsNone(before["half_open_owner"])

        script_file = Path(self.tmp.name) / "dry_circuit.py"
        script_file.write_text('await agent("Check", target="pi-default", read_only=True)\n', encoding="utf-8")

        # CLI run --dry-run previews pi-default without mutating target_health
        code, dry_env = _run_cli(["run", str(script_file), "--dry-run"], root=self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(dry_env["meta"]["dry_run"])
        self.assertEqual(dry_env["data"]["resolved_preview"][0]["candidates"], ["pi-default"])

        after = self.store.get_target_health("pi-default")
        self.assertEqual(after["state"], "open")
        self.assertIsNone(after["half_open_owner"])
        self.assertEqual(after["version"], before["version"])
        self.assertEqual(after["updated_at"], before["updated_at"])

        # Subsequent real execution owner claims half_open cleanly (no false probe_in_flight)
        real_res = resolve_step_candidates(
            self.store,
            target="pi-default",
            require_kind="runtime",
            probe_owner="wf_real:step-0001",
            claim_probes=True,
        )
        self.assertEqual([c["target_id"] for c in real_res["candidates"]], ["pi-default"])
        self.assertTrue(real_res["candidates"][0]["half_open_probe"])
        claimed = self.store.get_target_health("pi-default")
        self.assertEqual(claimed["state"], "half_open")
        self.assertEqual(claimed["half_open_owner"], "wf_real:step-0001")

        # Another concurrent owner sees circuit_half_open_probe_in_flight
        other_res = resolve_step_candidates(
            self.store,
            target="pi-default",
            require_kind="runtime",
            probe_owner="wf_other:step-0002",
            claim_probes=True,
        )
        self.assertEqual(other_res["candidates"], [])
        self.assertEqual(other_res["skipped"][0]["reason"], "circuit_half_open_probe_in_flight")

        # Releasing the probe restores expired open state cleanly
        self.store.release_half_open_probe("pi-default", owner="wf_real:step-0001")
        released = self.store.get_target_health("pi-default")
        self.assertEqual(released["state"], "open")
        self.assertIsNone(released["half_open_owner"])

    def test_cloud_registry_304_emits_no_resource_warning_and_v1_schema_migrates(self) -> None:
        cache = Path(self.tmp.name) / "cache"
        manifest = load_active_registry(self.store)["manifest"]
        manifest["registry_version"] = "2026.11.27-no-warn"
        body_bytes = json.dumps(manifest).encode("utf-8")
        etag = '"v-no-warn"'

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("ETag", etag)
                self.end_headers()
                self.wfile.write(body_bytes)

            def log_message(self, format, *args):  # noqa: A002
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{port}/registry.json"
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                with mock.patch.dict(
                    os.environ,
                    {"CORRAL_CACHE_DIR": str(cache), "CORRAL_WORKFLOW_REGISTRY_URL": url},
                    clear=False,
                ):
                    sync_registry(self.store, force=True)
                    cache_file = cache / "workflow-registry.json"
                    cached = json.loads(cache_file.read_text(encoding="utf-8"))
                    cached["fetched_at"] = 0
                    cache_file.write_text(json.dumps(cached), encoding="utf-8")
                    res = sync_registry(self.store, force=False)
                    self.assertEqual(res["status"], "not_modified")
                gc.collect()
            resource_warnings = [w for w in caught if issubclass(w.category, ResourceWarning)]
            self.assertEqual(resource_warnings, [])
        finally:
            server.shutdown()
            server.server_close()

        # Verify v1 SQLite database upgrades idempotently to v2 without losing existing runs
        v1_root = Path(self.tmp.name) / "v1_wf"
        v1_root.mkdir()
        v1_db = v1_root / "workflows.sqlite3"
        conn = sqlite3.connect(v1_db)
        conn.executescript(
            """
            CREATE TABLE workflow_runs (
                wf_id TEXT PRIMARY KEY, name TEXT NOT NULL, script_path TEXT, script_sha256 TEXT,
                args_json TEXT NOT NULL, status TEXT NOT NULL, active_phase TEXT,
                max_concurrency INTEGER NOT NULL, result_json TEXT, error_json TEXT,
                created_at REAL NOT NULL, started_at REAL, finished_at REAL, updated_at REAL NOT NULL
            );
            CREATE TABLE workflow_nodes (
                wf_id TEXT NOT NULL, node_id TEXT NOT NULL, step_key TEXT, parent_id TEXT,
                node_type TEXT NOT NULL, kind TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL,
                requested_json TEXT, resolved_json TEXT, observed_json TEXT, schema_json TEXT,
                output_json TEXT, output_committed INTEGER NOT NULL DEFAULT 0, session_ref TEXT,
                keepalive_name TEXT, worktree_path TEXT, attempt INTEGER NOT NULL DEFAULT 0,
                progress_note TEXT, error_json TEXT, started_at REAL, finished_at REAL,
                duration_ms INTEGER, updated_at REAL NOT NULL, PRIMARY KEY (wf_id, node_id)
            );
            CREATE TABLE workflow_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, wf_id TEXT NOT NULL, kind TEXT NOT NULL,
                node_id TEXT, payload_json TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE audit_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL,
                target_id TEXT NOT NULL, before_version TEXT, after_version TEXT,
                detail_json TEXT, timestamp REAL NOT NULL, timestamp_utc TEXT NOT NULL
            );
            INSERT INTO workflow_runs VALUES
                ('wf_legacy', 'legacy', NULL, NULL, '{}', 'completed', NULL, 4, NULL, NULL, 1.0, 1.0, 2.0, 2.0);
            PRAGMA user_version=1;
            """
        )
        conn.commit()
        conn.close()

        v1_store = WorkflowStore(v1_root)
        # Read-only open works on v1 without crashing
        legacy = v1_store.get_workflow_run("wf_legacy")
        self.assertEqual(legacy["status"], "completed")
        self.assertIsNone(legacy["cwd"])
        # Write open upgrades schema to v2 cleanly
        v1_store.create_workflow_run(
            wf_id="wf_new",
            name="new",
            script_path=None,
            script_sha256="abc",
            script_source="return 1",
            cwd="/tmp",
            owner_pid=os.getpid(),
            args={},
            max_concurrency=4,
            actor="test",
        )
        upgraded = v1_store.get_workflow_run("wf_new")
        self.assertEqual(upgraded["cwd"], "/tmp")
        self.assertTrue(upgraded["has_script_snapshot"])


class PlaybookLibraryOptimizationAndThinSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "wf"
        self.proj = (Path(self.tmp.name) / "proj").resolve()
        self.proj.mkdir()
        self.store = WorkflowStore(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_playbook_save_list_show_script_export_optimization_signals_and_thin_skill(self) -> None:
        # 1. Empty library reports count=0 honestly (never fabricates built-in playbooks)
        code, env = _run_cli(["playbook", "list", "--cwd", str(self.proj)], root=self.root)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(env["data"]["count"], 0)

        # 2. Run a standalone script twice (one completed, one failed) to generate optimization signals
        script_v1 = '''"""Cross-check repository status and echo goal."""
WORKFLOW_META = {
    "description": "Audit repo status and print goal",
    "tags": ["audit", "review"],
    "args": {"goal": "Target audit goal"},
}
phase("1. Inspect")
s1 = await shell(f"echo goal:{args.get('goal', 'none')}", step_key="inspect")
if args.get("fail"):
    raise RuntimeError("simulated failure")
return {"out": s1.output}
'''
        src_file = Path(self.tmp.name) / "draft.py"
        src_file.write_text(script_v1, encoding="utf-8")

        code_r1, env_r1 = _run_cli(
            ["run", str(src_file), "--name", "repo-audit", "--cwd", str(self.proj), "--arg", "goal=auth"],
            root=self.root,
        )
        self.assertEqual(code_r1, EXIT_OK)
        wf1 = env_r1["data"]["workflow"]["wf_id"]

        # Historical script snapshot export works and writes to --out
        exported_py = Path(self.tmp.name) / "exported.py"
        code_sc, env_sc = _run_cli(["script", wf1, "--out", str(exported_py)], root=self.root)
        self.assertEqual(code_sc, EXIT_OK)
        self.assertEqual(env_sc["data"]["source"], script_v1)
        self.assertEqual(exported_py.read_text(encoding="utf-8"), script_v1)

        # Promote run snapshot directly to project playbook (dry-run first -> no disk write)
        code_dry, env_dry = _run_cli(
            [
                "playbook", "save", "repo-audit", "--from-run", wf1,
                "--scope", "project", "--cwd", str(self.proj), "--dry-run",
            ],
            root=self.root,
        )
        self.assertEqual(code_dry, EXIT_OK)
        self.assertTrue(env_dry["meta"]["dry_run"])
        self.assertFalse((self.proj / ".corral" / "workflows" / "repo-audit.py").exists())

        code_save, env_save = _run_cli(
            ["playbook", "save", "repo-audit", "--from-run", wf1, "--scope", "project", "--cwd", str(self.proj)],
            root=self.root,
        )
        self.assertEqual(code_save, EXIT_OK)
        v1 = env_save["data"]["playbook"]["version"]
        self.assertEqual(env_save["data"]["playbook"]["tags"], ["audit", "review"])
        self.assertEqual(env_save["data"]["playbook"]["description"], "Audit repo status and print goal")

        # Overwriting without --version fails with EXIT_USAGE (confirmation_required)
        code_nover, env_nover = _run_cli(
            ["playbook", "save", "repo-audit", "--file", str(src_file), "--scope", "project", "--cwd", str(self.proj)],
            root=self.root,
        )
        self.assertEqual(code_nover, EXIT_USAGE)
        self.assertEqual(env_nover["error"]["code"], "confirmation_required")

        # Run by playbook name directly (resolved from --cwd/.corral/workflows/)
        code_pb_run, env_pb_run = _run_cli(
            ["run", "repo-audit", "--cwd", str(self.proj), "--arg", "goal=billing"],
            root=self.root,
        )
        self.assertEqual(code_pb_run, EXIT_OK)
        self.assertEqual(env_pb_run["data"]["workflow"]["result"]["out"], "goal:billing")

        # playbook list TSV & show include aggregated optimization signals
        code_list, env_list = _run_cli(["playbook", "list", "--cwd", str(self.proj)], root=self.root)
        self.assertEqual(code_list, EXIT_OK)
        self.assertEqual(env_list["data"]["count"], 1)
        self.assertEqual(env_list["data"]["playbooks"][0]["runs_count"], 2)
        self.assertEqual(env_list["data"]["playbooks"][0]["success_rate"], 1.0)

        code_show, env_show = _run_cli(["playbook", "show", "repo-audit", "--cwd", str(self.proj)], root=self.root)
        self.assertEqual(code_show, EXIT_OK)
        signals = env_show["data"]["playbook"]["optimization_signals"]
        self.assertEqual(signals["runs_analyzed"], 2)
        self.assertEqual(signals["completed"], 2)
        self.assertEqual(signals["slowest_steps"][0]["step"], "inspect")

        # 3. Opt-in thin skill export (project scope by default -> <proj>/.agents/skills/corral-wf-repo-audit/SKILL.md)
        code_ex_dry, env_ex_dry = _run_cli(
            ["playbook", "export-skill", "repo-audit", "--scope", "project", "--cwd", str(self.proj), "--dry-run"],
            root=self.root,
        )
        self.assertEqual(code_ex_dry, EXIT_OK)
        skill_md = self.proj / ".agents" / "skills" / "corral-wf-repo-audit" / "SKILL.md"
        self.assertFalse(skill_md.exists())

        code_ex, env_ex = _run_cli(
            ["playbook", "export-skill", "repo-audit", "--scope", "project", "--cwd", str(self.proj)],
            root=self.root,
        )
        self.assertEqual(code_ex, EXIT_OK)
        self.assertTrue(skill_md.is_file())
        skill_text = skill_md.read_text(encoding="utf-8")
        self.assertLessEqual(len(skill_text.splitlines()), 25)
        self.assertIn("name: corral-wf-repo-audit", skill_text)
        self.assertIn("--dry-run", skill_text)
        self.assertIn("playbook show repo-audit", skill_text)

        # User manual edit protects skill from silent overwrite or deletion (EXIT_CONFLICT)
        skill_md.write_text(skill_text + "\n# user note\n", encoding="utf-8")
        code_conf, _ = _run_cli(
            ["playbook", "unexport-skill", "repo-audit", "--scope", "project", "--cwd", str(self.proj)],
            root=self.root,
        )
        self.assertEqual(code_conf, EXIT_CONFLICT)

        # Restore original exported content -> clean unexport removes file & empty dir
        skill_md.write_text(skill_text, encoding="utf-8")
        code_unex, _ = _run_cli(
            ["playbook", "unexport-skill", "repo-audit", "--scope", "project", "--cwd", str(self.proj)],
            root=self.root,
        )
        self.assertEqual(code_unex, EXIT_OK)
        self.assertFalse(skill_md.exists())
        self.assertFalse(skill_md.parent.exists())

        # Delete playbook requires exact --version
        code_del_bad, _ = _run_cli(
            [
                "playbook", "delete", "repo-audit", "--scope", "project",
                "--cwd", str(self.proj), "--version", "deadbeef0000",
            ],
            root=self.root,
        )
        self.assertEqual(code_del_bad, EXIT_CONFLICT)
        code_del_ok, _ = _run_cli(
            ["playbook", "delete", "repo-audit", "--scope", "project", "--cwd", str(self.proj), "--version", v1],
            root=self.root,
        )
        self.assertEqual(code_del_ok, EXIT_OK)
        self.assertFalse((self.proj / ".corral" / "workflows" / "repo-audit.py").exists())


async def _no_sleep(_seconds: float) -> None:
    return None


def _extract_marker(prompt: str) -> tuple[str, str]:
    from corral_workflow.visibility import parse_workflow_marker

    parsed = parse_workflow_marker(prompt)
    assert parsed is not None, f"Missing workflow marker in prompt: {prompt!r}"
    return parsed


if __name__ == "__main__":
    unittest.main()
