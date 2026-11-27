"""Single-process asyncio Python workflow engine with 6 core primitives.

Compiles top-level-``await`` Python workflow scripts (supporting top-level
``return`` while preserving original line numbers), runs up to 30+ concurrent
steps bounded by global and per-target semaphores, persists DAG nodes/edges and
SQLite WAL checkpoints, enforces pure-CLI structured output submission via
``corral workflow finish``, and coordinates shared circuit-breaker degradation.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import random
import signal
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corral_workflow.errors import EXIT_ERROR, EXIT_TIMEOUT, EXIT_USAGE, WorkflowError
from corral_workflow.routing import (
    FAILURE_CALLER,
    FAILURE_UNCERTAIN,
    classify_failure,
    resolve_step_candidates,
)
from corral_workflow.scanner import ensure_inventory_fresh
from corral_workflow.schema import validate_json, validate_schema_definition
from corral_workflow.store import WorkflowStore
from corral_workflow.visibility import workflow_marker
from corral_workflow.worktree import (
    create_worktree,
    reset_worktree_clean,
    worktree_diff_summary,
)

DEFAULT_MAX_CONCURRENCY = 12
DEFAULT_MAX_TOTAL_STEPS = 200
DEFAULT_MAX_FINISH_NUDGES = 2


@dataclass
class StepResult:
    """Structured result returned by ``await agent(...)``, ``await llm(...)``, and ``await shell(...)``."""

    node_id: str
    step_key: str | None
    kind: str
    status: str
    value: Any = None
    ok: bool = True
    error: dict[str, Any] | None = None
    output: str = ""
    returncode: int | None = None
    requested: dict[str, Any] | None = None
    resolved: dict[str, Any] | None = None
    observed: dict[str, Any] | None = None
    session_ref: str | None = None
    worktree: str | None = None
    diff_summary: dict[str, Any] | None = None
    from_checkpoint: bool = False

    @property
    def observed_runtime(self) -> str | None:
        if isinstance(self.observed, dict) and self.observed.get("runtime"):
            return str(self.observed["runtime"])
        if isinstance(self.resolved, dict) and self.resolved.get("runtime"):
            return str(self.resolved["runtime"])
        return None


@dataclass
class BackendTurnOutcome:
    """Normalized turn outcome returned by an AgentBackend implementation."""

    state: str  # completed | failed | interrupted | waiting_input | unknown
    final_response: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    observed_runtime: str | None = None
    observed_model: str | None = None
    evidence_source: str = "backend"
    session_ref: str | None = None
    keepalive_name: str | None = None
    had_tool_activity: bool = False
    retry_after_s: float | None = None


@dataclass
class AgentSessionRef:
    """Active hosted session handle tracked by the engine."""

    session_id: str
    runtime: str
    session_ref: str | None = None
    keepalive_name: str | None = None
    model: str | None = None


class AgentBackend:
    """Abstract execution backend for ``agent()`` steps (real LocalController or test fake)."""

    async def create_session(
        self,
        *,
        runtime: str,
        cwd: str,
        model: str | None,
        wf_id: str,
        node_id: str,
        actor: str,
    ) -> AgentSessionRef:
        raise NotImplementedError

    async def run_turn(
        self,
        session: AgentSessionRef,
        *,
        prompt: str,
        request_id: str,
        timeout: float | None,
        actor: str,
    ) -> BackendTurnOutcome:
        raise NotImplementedError

    async def close_session(self, session: AgentSessionRef, *, actor: str) -> None:
        raise NotImplementedError


class LocalControllerBackend(AgentBackend):
    """Production backend over ``AgentSessionControl.LocalController`` with Corral adapter profiles.

    Uses ``spawn=False`` so all 20-30 concurrent workflow steps are supervised
    within the single workflow engine process rather than spawning 30 detached
    supervisor processes.
    """

    def __init__(self, store: WorkflowStore) -> None:
        self._store = store
        self._controller: Any = None

    def _get_controller(self) -> Any:
        if self._controller is None:
            from agent_session_control.controller import LocalController

            self._controller = LocalController(
                root=self._store.root / "control",
                namespace="workflow",
                runtime_profiles=None,
                approval_policy={"auto_approve": True},
                spawn=False,
            )
        return self._controller

    async def create_session(
        self,
        *,
        runtime: str,
        cwd: str,
        model: str | None,
        wf_id: str,
        node_id: str,
        actor: str,
    ) -> AgentSessionRef:
        from corral_workflow.state_bridge import acquire_host_lease

        controller = self._get_controller()
        handle = await controller.create(runtime, cwd, model=model, actor=actor)
        session_ref = f"{runtime}:{handle.native_id}" if getattr(handle, "native_id", None) else handle.id
        keepalive = getattr(handle, "instance_id", None) or getattr(handle, "id", None)
        if keepalive:
            acquire_host_lease(
                lease_id=f"{wf_id}:{node_id}",
                keepalive_name=str(keepalive),
                store_root=self._store.root,
            )
        return AgentSessionRef(
            session_id=handle.id,
            runtime=runtime,
            session_ref=session_ref,
            keepalive_name=keepalive,
            model=model,
        )

    async def run_turn(
        self,
        session: AgentSessionRef,
        *,
        prompt: str,
        request_id: str,
        timeout: float | None,
        actor: str,
    ) -> BackendTurnOutcome:
        from agent_session_control.errors import ControlError
        from agent_session_control.models import text_block

        controller = self._get_controller()
        try:
            run_id = await controller.start_turn(
                session.session_id,
                [text_block(prompt)],
                request_id=request_id,
                on_busy="queue",
                actor=actor,
            )
            result = await controller.wait(run_id, timeout=timeout)
        except ControlError as exc:
            return BackendTurnOutcome(
                state="failed" if exc.code != "wait_timeout" else "unknown",
                error_code=exc.code,
                error_message=exc.message,
                observed_runtime=session.runtime,
                session_ref=session.session_ref,
                keepalive_name=session.keepalive_name,
            )
        err = result.error if isinstance(result.error, dict) else {}
        return BackendTurnOutcome(
            state=result.state,
            final_response=result.final_response,
            error_code=err.get("code") if err else None,
            error_message=err.get("message") if err else None,
            observed_runtime=session.runtime,
            observed_model=session.model,
            evidence_source=result.evidence or "sesskit",
            session_ref=session.session_ref,
            keepalive_name=session.keepalive_name,
            had_tool_activity=result.state in {"completed", "failed", "interrupted"},
        )

    async def close_session(self, session: AgentSessionRef, *, actor: str) -> None:
        from corral_workflow.state_bridge import release_host_lease

        if session.keepalive_name:
            release_host_lease(keepalive_name=session.keepalive_name, store_root=self._store.root)
        if self._controller is None:
            return
        try:
            await self._controller.close_session(session.session_id, actor=actor)
        except Exception:
            pass


class LLMBackend:
    """Abstract lightweight LLM Gateway backend for ``llm()`` steps."""

    async def complete(
        self,
        *,
        prompt: str,
        model: str | None,
        schema: dict[str, Any] | None,
        timeout: float,
    ) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError


class GatewayLLMBackend(LLMBackend):
    """OpenAI-compatible LLM Gateway transport reusing Corral's ``titlegen`` config."""

    async def complete(
        self,
        *,
        prompt: str,
        model: str | None,
        schema: dict[str, Any] | None,
        timeout: float,
    ) -> tuple[str, dict[str, Any]]:
        return await asyncio.to_thread(self._complete_sync, prompt, model, schema, timeout)

    @staticmethod
    def _complete_sync(
        prompt: str,
        model: str | None,
        schema: dict[str, Any] | None,
        timeout: float,
    ) -> tuple[str, dict[str, Any]]:
        from corral_workflow import state_bridge

        url = state_bridge.gateway_url()
        key = state_bridge.gateway_key()
        if not url or not key:
            raise WorkflowError(
                "unavailable",
                "LLM gateway is not configured (set ~/.config/corral/llm-gateway.json or CORRAL_LLM_GATEWAY_URL/KEY).",
                EXIT_ERROR,
            )
        chosen_model = model or state_bridge.gateway_model()
        if not chosen_model:
            raise WorkflowError("unavailable", "No LLM gateway model alias available.", EXIT_ERROR)

        full_prompt = prompt
        if schema is not None:
            full_prompt = (
                f"{prompt}\n\nReturn ONLY a single valid JSON value matching this JSON Schema (no markdown fences):\n"
                f"{json.dumps(schema, ensure_ascii=False)}"
            )
        payload: dict[str, Any] = {
            "model": chosen_model,
            "messages": [{"role": "user", "content": full_prompt}],
            "temperature": 0,
            "max_tokens": 4096,
            "stream": False,
        }
        if isinstance(schema, dict) and schema.get("type") == "object":
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            f"{url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read(2_000_000).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            code = exc.code
            reason = exc.reason
            try:
                detail = exc.read(2000).decode("utf-8", errors="replace")
            except Exception:
                pass
            finally:
                exc.close()
            raise WorkflowError(
                f"http_{code}",
                f"LLM gateway HTTP {code}: {detail or reason}",
                EXIT_ERROR,
                context={"http_status": code, "retry_after": retry_after},
            ) from exc
        except (OSError, TimeoutError, ValueError, urllib.error.URLError) as exc:
            raise WorkflowError("transient", f"LLM gateway request failed: {exc}", EXIT_ERROR) from exc

        text = state_bridge.assistant_text(body)
        if text is None:
            raise WorkflowError("transient", "LLM gateway returned empty content.", EXIT_ERROR)
        observed_model = body.get("model") if isinstance(body, dict) else chosen_model
        return text, {
            "runtime": "gateway",
            "model_observed": observed_model or chosen_model,
            "evidence_source": "gateway",
        }


def _step_input_hash(payload: dict[str, Any]) -> str:
    """Deterministic 16-char SHA-256 fingerprint binding a checkpoint to its actual step inputs."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate an async subprocess and its entire process group cleanly."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=0.5)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        pass


def _extract_json_value(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            cleaned = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        start_candidates = [idx for idx in (cleaned.find("{"), cleaned.find("[")) if idx >= 0]
        if start_candidates:
            start = min(start_candidates)
            end = max(cleaned.rfind("}"), cleaned.rfind("]"))
            if end > start:
                return json.loads(cleaned[start : end + 1])
        raise


def compile_workflow_script(source: str, filename: str = "<workflow>") -> Any:
    """Wrap a top-level-``await`` Python script into an async function preserving line numbers."""
    try:
        tree = ast.parse(source, filename=filename, mode="exec")
    except SyntaxError as exc:
        raise WorkflowError(
            "syntax_error",
            f"Workflow script syntax error at {filename}:{exc.lineno}: {exc.msg}",
            EXIT_USAGE,
            context={"filename": filename, "lineno": exc.lineno, "offset": exc.offset},
        ) from exc

    fn_def = ast.AsyncFunctionDef(
        name="__corral_workflow_main__",
        args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
        body=tree.body or [ast.Return(value=ast.Constant(value=None))],
        decorator_list=[],
        returns=None,
        type_comment=None,
    )
    ast.fix_missing_locations(fn_def)
    # Restore original statement line numbers so runtime tracebacks match the source file.
    wrapper = ast.Module(body=[fn_def], type_ignores=[])
    return compile(wrapper, filename, "exec")


def analyze_script_dry_run(
    store: WorkflowStore,
    source: str,
    *,
    filename: str = "<workflow>",
) -> dict[str, Any]:
    """Read-only static inspection of a workflow script for ``--dry-run``."""
    _ = compile_workflow_script(source, filename=filename)
    tree = ast.parse(source, filename=filename, mode="exec")
    phases: list[str] = []
    calls: list[dict[str, Any]] = []
    warnings: list[str] = []

    def _const_str(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        fname = node.func.id
        if fname == "phase" and node.args:
            name = _const_str(node.args[0])
            if name:
                phases.append(name)
        elif fname in {"agent", "llm", "shell", "parallel", "pipeline"}:
            kw = {k.arg: _const_str(k.value) for k in node.keywords if k.arg}
            entry: dict[str, Any] = {"primitive": fname, "lineno": node.lineno}
            for key in (
                "capability",
                "profile",
                "target",
                "runtime",
                "model",
                "workspace",
                "role",
                "label",
                "step_key",
            ):
                if kw.get(key):
                    entry[key] = kw[key]
            calls.append(entry)

    calls.sort(key=lambda item: item["lineno"])
    resolved_preview: list[dict[str, Any]] = []
    for item in calls:
        if item["primitive"] not in {"agent", "llm"}:
            continue
        cap = item.get("capability") or item.get("profile")
        tgt = item.get("target")
        rt = item.get("runtime")
        if not (cap or tgt or rt):
            continue
        try:
            res = resolve_step_candidates(
                store,
                capability=cap,
                target=tgt,
                runtime=rt,
                model=item.get("model"),
                require_kind="gateway" if item["primitive"] == "llm" else "runtime",
            )
            cands = [c["target_id"] for c in res["candidates"]]
            resolved_preview.append(
                {
                    "lineno": item["lineno"],
                    "primitive": item["primitive"],
                    "requested": cap or tgt or rt,
                    "candidates": cands,
                    "skipped": res["skipped"],
                }
            )
            if not cands:
                warnings.append(f"Line {item['lineno']}: no healthy local target resolved for '{cap or tgt or rt}'.")
        except WorkflowError as exc:
            warnings.append(f"Line {item['lineno']}: {exc.message}")

    return {
        "syntax_ok": True,
        "filename": filename,
        "phases_detected": phases,
        "primitive_calls": calls,
        "resolved_preview": resolved_preview,
        "warnings": warnings,
    }


def render_graph_ascii(graph: dict[str, Any]) -> str:
    """Compact Unicode flowchart tree for ``corral workflow graph --format ascii``."""
    wf = graph["workflow"]
    nodes = graph["nodes"]
    status_icon = {
        "completed": "✓",
        "running": "●",
        "waiting_input": "?",
        "blocked": "⏸",
        "failed": "✗",
        "cancelled": "⊘",
        "queued": "○",
    }
    lines = [
        f"Workflow {wf['wf_id']} ({wf['name']})  [{wf['status']}]",
    ]
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    for node in nodes:
        by_parent.setdefault(node.get("parent_id"), []).append(node)

    def _emit(parent: str | None, prefix: str) -> None:
        children = by_parent.get(parent, [])
        for idx, node in enumerate(children):
            last = idx == len(children) - 1
            branch = "└─ " if last else "├─ "
            icon = status_icon.get(node["status"], "·")
            resolved = node.get("resolved") or {}
            target_tag = ""
            if resolved.get("target_id"):
                arrow = "↪ " if resolved.get("degraded") else ""
                target_tag = f"  [{arrow}{resolved['target_id']}]"
            note = f" — {node['progress_note']}" if node.get("progress_note") else ""
            lines.append(f"{prefix}{branch}{icon} {node['role']} ({node['kind']}){target_tag}{note}")
            ext = "   " if last else "│  "
            _emit(node["node_id"], prefix + ext)

    _emit(None, "")
    return "\n".join(lines)


class _StatefulAgentHandle:
    """Multi-turn agent handle created via ``a = agent(...)`` and invoked via ``await a(prompt)``."""

    def __init__(self, engine: WorkflowEngine, spec: dict[str, Any]) -> None:
        self._engine = engine
        self._spec = spec
        self._session: AgentSessionRef | None = None
        self._resolved: dict[str, Any] | None = None
        self._worktree_info: dict[str, Any] | None = None
        self._turn_index = 0
        self._closed = False
        self._awaited_once = False
        self._single_shot_result: StepResult | None = None
        self._turn_lock = asyncio.Lock()

    def __await__(self):
        return self._run_single_shot().__await__()

    async def _run_single_shot(self) -> StepResult:
        if self._awaited_once and self._single_shot_result is not None:
            return self._single_shot_result
        self._awaited_once = True
        try:
            self._single_shot_result = await self._engine._execute_agent_step(self._spec)
            return self._single_shot_result
        finally:
            self._closed = True

    async def __call__(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        *,
        role: str | None = None,
        step_key: str | None = None,
    ) -> StepResult:
        if self._closed:
            raise WorkflowError("usage_error", "Cannot invoke a closed stateful agent handle.", EXIT_USAGE)
        async with self._turn_lock:
            self._turn_index += 1
            base_prompt = self._spec.get("prompt")
            combined = f"{base_prompt}\n\n{prompt}" if (self._turn_index == 1 and base_prompt) else prompt
            turn_spec = {
                **self._spec,
                "prompt": combined,
                "schema": schema if schema is not None else self._spec.get("schema"),
                "role": role or f"{self._spec.get('role') or 'Agent'} (turn {self._turn_index})",
                "step_key": step_key
                or (f"{self._spec['step_key']}:turn-{self._turn_index}" if self._spec.get("step_key") else None),
            }
            result, session, resolved, wt = await self._engine._execute_agent_turn_stateful(
                turn_spec,
                existing_session=self._session,
                existing_resolved=self._resolved,
                existing_worktree=self._worktree_info,
            )
            self._session = session
            self._resolved = resolved
            self._worktree_info = wt
            return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._session is not None:
            await self._engine._close_session_safe(self._session)
            self._session = None


@dataclass
class WorkflowRunContext:
    wf_id: str
    cwd: Path
    max_concurrency: int
    max_total_steps: int
    override_targets: dict[str, str] = field(default_factory=dict)
    current_phase_id: str | None = None
    last_top_node_id: str | None = None
    step_counter: int = 0
    cancelled: bool = False
    checkpoint_invalidated: bool = False


class WorkflowEngine:
    """Executes Python workflow scripts with durability, concurrency bounds and degradation."""

    def __init__(
        self,
        store: WorkflowStore,
        *,
        backend: AgentBackend | None = None,
        llm_backend: LLMBackend | None = None,
        actor: str = "local-user",
        corral_bin: str = "corral",
        auto_scan: bool = False,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self._backend = backend or LocalControllerBackend(store)
        self._llm = llm_backend or GatewayLLMBackend()
        self._actor = actor
        self._corral_bin = corral_bin
        self._auto_scan = auto_scan
        self._sleep = sleep_fn or asyncio.sleep
        self._global_sem: asyncio.Semaphore | None = None
        self._target_sems: dict[str, asyncio.Semaphore] = {}
        self._ctx: WorkflowRunContext | None = None
        self._open_handles: list[_StatefulAgentHandle] = []
        self._active_sessions: list[AgentSessionRef] = []

    def _check_cancelled(self) -> None:
        if self._ctx is None:
            return
        if self._ctx.cancelled or self.store.is_cancel_requested(self._ctx.wf_id):
            self._ctx.cancelled = True
            raise asyncio.CancelledError("Workflow cancelled")

    def _track_session(self, session: AgentSessionRef) -> None:
        if session not in self._active_sessions:
            self._active_sessions.append(session)

    async def _close_session_safe(self, session: AgentSessionRef | None) -> None:
        if session is None:
            return
        if session in self._active_sessions:
            self._active_sessions.remove(session)
        try:
            await self._backend.close_session(session, actor=self._actor)
        except Exception:
            pass

    def _lookup_valid_checkpoint(
        self,
        wf_id: str,
        step_key: str | None,
        input_hash: str,
    ) -> dict[str, Any] | None:
        """Return a completed checkpoint only when inputs match and no upstream step was invalidated."""
        assert self._ctx is not None
        if not step_key:
            return None
        ckpt = self.store.find_completed_checkpoint(wf_id, step_key)
        if ckpt is None:
            return None
        if not self._ctx.checkpoint_invalidated and ckpt.get("input_hash") == input_hash:
            return ckpt
        # Input changed (or an upstream step was already invalidated): cascade-clear from here.
        self._ctx.checkpoint_invalidated = True
        self.store.clear_checkpoints_from(wf_id, step_key, actor=self._actor, ignore_missing=True)
        return None

    def _next_node_id(self, prefix: str) -> str:
        assert self._ctx is not None
        self._ctx.step_counter += 1
        if self._ctx.step_counter > self._ctx.max_total_steps:
            raise WorkflowError(
                "step_budget_exceeded",
                f"Workflow exceeded max_total_steps ({self._ctx.max_total_steps}).",
                EXIT_ERROR,
                hint="Increase max_total_steps or inspect the script for runaway loops.",
            )
        return f"{prefix}-{self._ctx.step_counter:04d}"

    def _target_semaphore(self, target_id: str, limit: int | None) -> asyncio.Semaphore | None:
        if not limit or limit <= 0:
            return None
        sem = self._target_sems.get(target_id)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            self._target_sems[target_id] = sem
        return sem

    def _build_worker_prompt(
        self,
        *,
        wf_id: str,
        node_id: str,
        prompt: str,
        schema: dict[str, Any] | None,
        out_file: Path,
    ) -> str:
        root_flag = f" --root {self.store.root}" if os.environ.get("CORRAL_WORKFLOW_ROOT") else ""
        finish_cmd = (
            f"{self._corral_bin} workflow finish{root_flag} --wf {wf_id} --step {node_id} --json-file {out_file}"
        )
        parts = [workflow_marker(wf_id, node_id), "", prompt.strip()]
        if schema is not None:
            parts.extend(
                [
                    "",
                    "---",
                    "Structured output requirement (pure CLI — do not skip):",
                    f"1. Write a valid JSON file to `{out_file}` matching the schema below.",
                    (
                        f"2. Run `{finish_cmd}` to validate and commit your output "
                        "(exit 0 = accepted; exit 2 = schema error to fix immediately in this turn)."
                    ),
                    f"JSON Schema: {json.dumps(schema, ensure_ascii=False)}",
                ]
            )
        return "\n".join(parts)

    async def run_script(
        self,
        source: str,
        *,
        name: str,
        script_path: str | None = None,
        args: dict[str, Any] | None = None,
        cwd: str | Path | None = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_total_steps: int = DEFAULT_MAX_TOTAL_STEPS,
        wf_id: str | None = None,
        resume: bool = False,
        override_targets: dict[str, str] | None = None,
        update_script_snapshot: bool = False,
    ) -> dict[str, Any]:
        if self._auto_scan:
            ensure_inventory_fresh(self.store)

        compiled = compile_workflow_script(source, filename=script_path or f"<workflow:{name}>")
        script_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        run_id = wf_id or f"wf_{uuid.uuid4().hex[:12]}"

        resolved_cwd_input = cwd
        if resume and resolved_cwd_input is None:
            existing_run = self.store.get_workflow_run(run_id)
            if existing_run.get("cwd"):
                resolved_cwd_input = existing_run["cwd"]
        work_cwd = Path(resolved_cwd_input or os.getcwd()).expanduser().resolve()
        owner_pid = os.getpid()

        if resume:
            self.store.set_workflow_owner(
                run_id,
                owner_pid,
                clear_cancel=True,
                script_source=source if update_script_snapshot else None,
                script_sha256=script_sha if update_script_snapshot else None,
                actor=self._actor,
            )
        else:
            self.store.create_workflow_run(
                wf_id=run_id,
                name=name,
                script_path=script_path,
                script_sha256=script_sha,
                script_source=source,
                cwd=str(work_cwd),
                owner_pid=owner_pid,
                args=dict(args or {}),
                max_concurrency=max(1, int(max_concurrency)),
                actor=self._actor,
            )

        self._ctx = WorkflowRunContext(
            wf_id=run_id,
            cwd=work_cwd,
            max_concurrency=max(1, int(max_concurrency)),
            max_total_steps=max(1, int(max_total_steps)),
            override_targets=dict(override_targets or {}),
        )
        self._global_sem = asyncio.Semaphore(self._ctx.max_concurrency)
        self._target_sems = {}
        self._open_handles = []
        self._active_sessions = []

        env_globals = self._build_script_globals(args=dict(args or {}), cwd=work_cwd, wf_id=run_id)
        loop = asyncio.get_running_loop()
        main_task: asyncio.Task[Any] | None = None
        watcher_task: asyncio.Task[None] | None = None
        sigterm_installed = False

        def _trigger_cancel() -> None:
            if self._ctx is not None:
                self._ctx.cancelled = True
            if main_task is not None and not main_task.done():
                main_task.cancel()

        async def _watch_cancel() -> None:
            while main_task is not None and not main_task.done():
                if self.store.is_cancel_requested(run_id):
                    _trigger_cancel()
                    return
                await asyncio.sleep(0.1)

        try:
            try:
                loop.add_signal_handler(signal.SIGTERM, _trigger_cancel)
                sigterm_installed = True
            except (NotImplementedError, RuntimeError, ValueError):
                sigterm_installed = False

            exec(compiled, env_globals)
            main_task = asyncio.create_task(env_globals["__corral_workflow_main__"]())
            watcher_task = asyncio.create_task(_watch_cancel())
            result = await main_task
            self._check_cancelled()
            if self._ctx.current_phase_id:
                self.store.upsert_node(run_id, self._ctx.current_phase_id, status="completed", finished_at=time.time())
            run_record = self.store.update_workflow_run(
                run_id,
                status="completed",
                result=result,
                error=None,
                finished=True,
                actor=self._actor,
            )
            if run_record["status"] == "cancelled":
                self.store.mark_running_nodes_cancelled(run_id)
                raise WorkflowError(
                    "cancelled",
                    "Workflow was cancelled.",
                    EXIT_ERROR,
                    context={"wf_id": run_id, "workflow_status": "cancelled"},
                )
            return run_record
        except WorkflowError as exc:
            if self._ctx.cancelled or self.store.is_cancel_requested(run_id):
                self.store.mark_running_nodes_cancelled(run_id)
                self.store.update_workflow_run(
                    run_id,
                    status="cancelled",
                    error={"code": "cancelled", "message": "Workflow was cancelled."},
                    finished=True,
                    actor=self._actor,
                )
                raise WorkflowError(
                    "cancelled",
                    "Workflow was cancelled.",
                    EXIT_ERROR,
                    context={"wf_id": run_id, "workflow_status": "cancelled"},
                ) from exc
            status = (
                "blocked"
                if exc.code in {"blocked_no_healthy_target", "degradation_blocked_dirty_workspace", "waiting_input"}
                else "failed"
            )
            if self._ctx.current_phase_id:
                self.store.upsert_node(run_id, self._ctx.current_phase_id, status=status, finished_at=time.time())
            run_record = self.store.update_workflow_run(
                run_id,
                status=status,
                error=exc.to_error_dict(),
                finished=True,
                actor=self._actor,
            )
            raise WorkflowError(
                exc.code,
                exc.message,
                exc.exit_code,
                hint=exc.hint,
                next_commands=exc.next_commands,
                retryable=exc.retryable,
                context={**(exc.context or {}), "wf_id": run_id, "workflow_status": run_record["status"]},
            ) from exc
        except asyncio.CancelledError as exc:
            self._ctx.cancelled = True
            self.store.mark_running_nodes_cancelled(run_id)
            self.store.update_workflow_run(
                run_id,
                status="cancelled",
                error={"code": "cancelled", "message": "Workflow was cancelled."},
                finished=True,
                actor=self._actor,
            )
            raise WorkflowError(
                "cancelled",
                "Workflow was cancelled.",
                EXIT_ERROR,
                context={"wf_id": run_id, "workflow_status": "cancelled"},
            ) from exc
        except Exception as exc:
            self.store.update_workflow_run(
                run_id,
                status="failed",
                error={"code": "script_error", "message": f"{type(exc).__name__}: {exc}"},
                finished=True,
                actor=self._actor,
            )
            raise WorkflowError(
                "script_error",
                f"Workflow script raised {type(exc).__name__}: {exc}",
                EXIT_ERROR,
                context={"wf_id": run_id},
            ) from exc
        finally:
            if watcher_task is not None and not watcher_task.done():
                watcher_task.cancel()
                try:
                    await watcher_task
                except asyncio.CancelledError:
                    pass
            if sigterm_installed:
                try:
                    loop.remove_signal_handler(signal.SIGTERM)
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
            for handle in list(self._open_handles):
                await handle.close()
            self._open_handles.clear()
            for sess in list(self._active_sessions):
                await self._close_session_safe(sess)
            self._active_sessions.clear()
            try:
                self.store.update_workflow_run(run_id, clear_owner=True)
            except Exception:
                pass

    def _build_script_globals(self, *, args: dict[str, Any], cwd: Path, wf_id: str) -> dict[str, Any]:
        engine = self

        def phase(name: str) -> str:
            assert engine._ctx is not None
            engine._check_cancelled()
            pid = engine._next_node_id("phase")
            engine.store.upsert_node(
                wf_id,
                pid,
                node_type="phase",
                kind="phase",
                role=name,
                status="running",
                started_at=time.time(),
            )
            if engine._ctx.current_phase_id:
                engine.store.upsert_node(
                    wf_id, engine._ctx.current_phase_id, status="completed", finished_at=time.time()
                )
                engine.store.add_edge(wf_id, engine._ctx.current_phase_id, pid, "sequence")
            engine._ctx.current_phase_id = pid
            engine._ctx.last_top_node_id = pid
            engine.store.update_workflow_run(wf_id, active_phase=name)
            return pid

        def agent(
            prompt: str | None = None,
            *,
            capability: str | None = None,
            profile: str | None = None,
            target: str | None = None,
            runtime: str | None = None,
            model: str | None = None,
            cwd: str | Path | None = None,
            workspace: str = "inherit",
            schema: dict[str, Any] | None = None,
            role: str | None = None,
            step_key: str | None = None,
            exclude_runtimes: tuple[str, ...] | list[str] = (),
            read_only: bool = False,
            retries: int = 2,
            timeout: float | None = None,
            allow_text_fallback: bool = False,
            on_partial_fallback: str = "clean_retry",
        ) -> _StatefulAgentHandle:
            if workspace not in {"inherit", "worktree"}:
                raise WorkflowError("usage_error", f"Unsupported workspace mode: {workspace!r}", EXIT_USAGE)
            if schema is not None:
                errs = validate_schema_definition(schema)
                if errs:
                    raise WorkflowError("usage_error", f"Invalid step schema: {'; '.join(errs)}", EXIT_USAGE)
            handle = _StatefulAgentHandle(
                engine,
                {
                    "prompt": prompt,
                    "capability": capability,
                    "profile": profile,
                    "target": target,
                    "runtime": runtime,
                    "model": model,
                    "cwd": str(Path(cwd).expanduser().resolve())
                    if cwd
                    else str(engine._ctx.cwd if engine._ctx else Path.cwd()),
                    "workspace": workspace,
                    "schema": schema,
                    "role": role,
                    "step_key": step_key,
                    "exclude_runtimes": tuple(exclude_runtimes),
                    "read_only": read_only,
                    "retries": max(0, int(retries)),
                    "timeout": timeout,
                    "allow_text_fallback": allow_text_fallback,
                    "on_partial_fallback": on_partial_fallback,
                },
            )
            engine._open_handles.append(handle)
            return handle

        async def llm(
            prompt: str,
            *,
            capability: str | None = None,
            profile: str | None = None,
            target: str | None = None,
            model: str | None = None,
            schema: dict[str, Any] | None = None,
            role: str | None = None,
            step_key: str | None = None,
            retries: int = 2,
            timeout: float = 60.0,
        ) -> Any:
            return await engine._execute_llm_step(
                prompt=prompt,
                capability=capability,
                profile=profile,
                target=target,
                model=model,
                schema=schema,
                role=role,
                step_key=step_key,
                retries=max(0, int(retries)),
                timeout=timeout,
            )

        async def shell(
            cmd: str | list[str],
            *,
            cwd: str | Path | None = None,
            role: str | None = None,
            step_key: str | None = None,
            check: bool = False,
            timeout: float | None = 120.0,
        ) -> StepResult:
            return await engine._execute_shell_step(
                cmd=cmd,
                cwd=str(Path(cwd).expanduser().resolve())
                if cwd
                else str(engine._ctx.cwd if engine._ctx else Path.cwd()),
                role=role,
                step_key=step_key,
                check=check,
                timeout=timeout,
            )

        async def parallel(
            items: list[Any],
            fn: Callable[[Any], Any],
            *,
            max_concurrency: int | None = None,
            on_error: str = "fail_fast",
            label: str | None = None,
        ) -> list[Any]:
            return await engine._execute_parallel(
                items,
                fn,
                max_concurrency=max_concurrency,
                on_error=on_error,
                label=label or f"Parallel ({len(items)})",
            )

        async def pipeline(
            items: list[Any],
            *stages: Callable[[Any], Any],
            max_concurrency: int | None = None,
            on_error: str = "fail_fast",
            label: str | None = None,
        ) -> list[Any]:
            return await engine._execute_pipeline(
                items,
                list(stages),
                max_concurrency=max_concurrency,
                on_error=on_error,
                label=label or f"Pipeline ({len(items)} x {len(stages)})",
            )

        def log(*parts: Any) -> None:
            assert engine._ctx is not None
            msg = " ".join(str(p) for p in parts)
            target_node = engine._ctx.current_phase_id
            if target_node:
                engine.store.record_step_log(wf_id, target_node, msg)

        return {
            "__builtins__": __builtins__,
            "args": args,
            "CWD": str(cwd),
            "WORKFLOW_ID": wf_id,
            "StepResult": StepResult,
            "WorkflowError": WorkflowError,
            "phase": phase,
            "agent": agent,
            "llm": llm,
            "shell": shell,
            "parallel": parallel,
            "pipeline": pipeline,
            "log": log,
        }

    # -- Step execution primitives ---------------------------------------------

    async def _execute_shell_step(
        self,
        *,
        cmd: str | list[str],
        cwd: str,
        role: str | None,
        step_key: str | None,
        check: bool,
        timeout: float | None,
    ) -> StepResult:
        assert self._ctx is not None
        self._check_cancelled()
        wf_id = self._ctx.wf_id
        input_hash = _step_input_hash({"kind": "shell", "cmd": cmd, "cwd": cwd, "check": check})
        ckpt = self._lookup_valid_checkpoint(wf_id, step_key, input_hash)
        if ckpt is not None:
            val = ckpt.get("output") or {}
            return StepResult(
                node_id=ckpt["node_id"],
                step_key=step_key,
                kind="shell",
                status="completed",
                value=val,
                ok=bool(val.get("returncode", 0) == 0),
                output=str(val.get("output") or ""),
                returncode=val.get("returncode"),
                from_checkpoint=True,
            )

        node_id = self._next_node_id("shell")
        label = role or (cmd if isinstance(cmd, str) else " ".join(cmd))[:64]
        started = time.time()
        self.store.upsert_node(
            wf_id,
            node_id,
            step_key=step_key,
            input_hash=input_hash,
            parent_id=self._ctx.current_phase_id,
            node_type="step",
            kind="shell",
            role=label,
            status="running",
            attempt=1,
            started_at=started,
        )
        proc: asyncio.subprocess.Process | None = None
        try:
            if isinstance(cmd, str):
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.CancelledError:
            if proc is not None:
                await _terminate_process_group(proc)
            finished = time.time()
            self.store.upsert_node(
                wf_id,
                node_id,
                status="cancelled",
                error={"code": "cancelled", "message": "Shell step was cancelled."},
                finished_at=finished,
                duration_ms=int((finished - started) * 1000),
            )
            raise
        except asyncio.TimeoutError as exc:
            if proc is not None:
                await _terminate_process_group(proc)
            finished = time.time()
            err = {"code": "timeout", "message": f"Shell step timed out after {timeout}s"}
            self.store.upsert_node(
                wf_id,
                node_id,
                status="failed",
                error=err,
                finished_at=finished,
                duration_ms=int((finished - started) * 1000),
            )
            raise WorkflowError("timeout", err["message"], EXIT_TIMEOUT) from exc

        self._check_cancelled()
        stdout_text = raw_out.decode("utf-8", errors="replace") if raw_out else ""
        stderr_text = raw_err.decode("utf-8", errors="replace") if raw_err else ""
        combined = (stdout_text + (("\n" + stderr_text) if stderr_text else "")).strip()
        finished = time.time()
        rc = proc.returncode if proc.returncode is not None else -1
        ok = rc == 0
        payload = {"returncode": rc, "output": combined[:64_000]}
        self.store.upsert_node(
            wf_id,
            node_id,
            input_hash=input_hash,
            status="completed" if (ok or not check) else "failed",
            output=payload,
            output_committed=True,
            error=None if (ok or not check) else {"code": "shell_failed", "message": f"exit {rc}"},
            finished_at=finished,
            duration_ms=int((finished - started) * 1000),
        )
        if check and not ok:
            raise WorkflowError(
                "shell_failed",
                f"Shell step '{label}' exited {rc}: {combined[:300]}",
                EXIT_ERROR,
            )
        return StepResult(
            node_id=node_id,
            step_key=step_key,
            kind="shell",
            status="completed" if ok else "failed",
            value=payload,
            ok=ok,
            output=combined,
            returncode=rc,
        )

    async def _execute_llm_step(
        self,
        *,
        prompt: str,
        capability: str | None,
        profile: str | None,
        target: str | None,
        model: str | None,
        schema: dict[str, Any] | None,
        role: str | None,
        step_key: str | None,
        retries: int,
        timeout: float,
    ) -> Any:
        assert self._ctx is not None
        self._check_cancelled()
        wf_id = self._ctx.wf_id
        override = self._ctx.override_targets.get(step_key or "")
        effective_target = override or target
        input_hash = _step_input_hash(
            {
                "kind": "llm",
                "prompt": prompt,
                "capability": capability,
                "profile": profile,
                "target": effective_target,
                "model": model,
                "schema": schema,
            }
        )
        ckpt = self._lookup_valid_checkpoint(wf_id, step_key, input_hash)
        if ckpt is not None:
            return ckpt.get("output")

        node_id = self._next_node_id("llm")
        started = time.time()
        probe_owner = f"{wf_id}:{node_id}"

        resolution = resolve_step_candidates(
            self.store,
            capability=capability if not effective_target else None,
            profile=profile if not effective_target else None,
            target=effective_target,
            model=model,
            require_kind="gateway" if (capability or profile or effective_target) else None,
            probe_owner=probe_owner,
            claim_probes=False,
        )
        candidates = resolution["candidates"]
        if not candidates and model is not None:
            candidates = [
                {"target_id": f"gw:{model}", "kind": "gateway", "model": model, "fallback_rank": 0, "degraded": False}
            ]
        elif not candidates and not (capability or profile or effective_target):
            candidates = [
                {"target_id": "gw:default", "kind": "gateway", "model": None, "fallback_rank": 0, "degraded": False}
            ]

        self.store.upsert_node(
            wf_id,
            node_id,
            step_key=step_key,
            input_hash=input_hash,
            parent_id=self._ctx.current_phase_id,
            node_type="step",
            kind="llm",
            role=role or (capability or profile or "LLM"),
            status="running",
            requested=resolution["requested"],
            schema=schema,
            started_at=started,
        )

        if not candidates:
            target_label = capability or profile or effective_target
            err = {
                "code": "blocked_no_healthy_target",
                "message": f"No healthy LLM gateway target available for '{target_label}'.",
                "skipped": resolution["skipped"],
            }
            self.store.upsert_node(wf_id, node_id, status="blocked", error=err, finished_at=time.time())
            raise WorkflowError("blocked_no_healthy_target", err["message"], EXIT_ERROR, context=err)

        attempts_log: list[dict[str, Any]] = []
        assert self._global_sem is not None
        async with self._global_sem:
            for cand in candidates:
                self._check_cancelled()
                tid = cand["target_id"]
                claimed_probe = False
                if cand.get("half_open_probe") and not tid.startswith("gw:"):
                    if not self.store.try_claim_half_open_probe(tid, owner=probe_owner):
                        resolution["skipped"].append(
                            {"target_id": tid, "reason": "circuit_half_open_probe_in_flight"}
                        )
                        continue
                    claimed_probe = True
                tsem = self._target_semaphore(tid, cand.get("max_concurrency"))
                for attempt_idx in range(1, retries + 2):
                    self._check_cancelled()
                    if attempt_idx > 1:
                        delay = min(1.0 * (2 ** (attempt_idx - 2)), 15.0) * (0.8 + 0.4 * random.random())
                        await self._sleep(delay)
                    try:
                        if tsem is not None:
                            async with tsem:
                                raw_text, observed = await self._llm.complete(
                                    prompt=prompt,
                                    model=cand.get("model") or model,
                                    schema=schema,
                                    timeout=timeout,
                                )
                        else:
                            raw_text, observed = await self._llm.complete(
                                prompt=prompt,
                                model=cand.get("model") or model,
                                schema=schema,
                                timeout=timeout,
                            )
                        self._check_cancelled()
                        parsed = _extract_json_value(raw_text) if schema is not None else raw_text
                        if schema is not None:
                            schema_errs = validate_json(parsed, schema)
                            if schema_errs:
                                raise WorkflowError(
                                    "schema_validation_failed",
                                    (
                                        f"LLM output failed schema validation: "
                                        f"{schema_errs[0]['path']}: {schema_errs[0]['message']}"
                                    ),
                                    EXIT_USAGE,
                                )
                        if not tid.startswith("gw:"):
                            self.store.record_target_success(tid)
                        finished = time.time()
                        self.store.upsert_node(
                            wf_id,
                            node_id,
                            input_hash=input_hash,
                            status="completed",
                            resolved={**cand, "attempts": attempts_log},
                            observed=observed,
                            output=parsed,
                            output_committed=True,
                            attempt=attempt_idx,
                            error=None,
                            finished_at=finished,
                            duration_ms=int((finished - started) * 1000),
                        )
                        return parsed
                    except asyncio.CancelledError:
                        if claimed_probe:
                            self.store.release_half_open_probe(tid, owner=probe_owner)
                        self.store.upsert_node(
                            wf_id,
                            node_id,
                            status="cancelled",
                            error={"code": "cancelled", "message": "LLM step was cancelled."},
                            finished_at=time.time(),
                        )
                        raise
                    except (WorkflowError, ValueError) as exc:
                        code = exc.code if isinstance(exc, WorkflowError) else "schema_validation_failed"
                        msg = exc.message if isinstance(exc, WorkflowError) else str(exc)
                        ctx = exc.context if isinstance(exc, WorkflowError) and exc.context else {}
                        http_status = ctx.get("http_status")
                        cls = classify_failure(code, msg, http_status=http_status)
                        attempts_log.append(
                            {
                                "target_id": tid,
                                "attempt": attempt_idx,
                                "code": code,
                                "failure_class": cls["failure_class"],
                                "message": msg[:200],
                            }
                        )
                        if cls["abort"]:
                            self.store.upsert_node(
                                wf_id,
                                node_id,
                                status="failed",
                                error={"code": code, "message": msg, "attempts": attempts_log},
                                finished_at=time.time(),
                            )
                            raise WorkflowError(code, msg, EXIT_USAGE, context={"attempts": attempts_log}) from exc
                        if not tid.startswith("gw:"):
                            self.store.record_target_failure(
                                tid,
                                failure_class=cls["failure_class"],
                                reason=msg,
                                open_immediately=cls["open_immediately"],
                                cooldown_seconds=cls["cooldown_seconds"],
                            )
                        if not cls["retry_same_target"]:
                            break

        err = {
            "code": "blocked_no_healthy_target",
            "message": "All LLM candidates exhausted.",
            "attempts": attempts_log,
        }
        self.store.upsert_node(wf_id, node_id, status="blocked", error=err, finished_at=time.time())
        raise WorkflowError("blocked_no_healthy_target", err["message"], EXIT_ERROR, context=err)

    async def _execute_agent_step(self, spec: dict[str, Any]) -> StepResult:
        result, session, _, _ = await self._execute_agent_turn_stateful(
            spec,
            existing_session=None,
            existing_resolved=None,
            existing_worktree=None,
            close_on_finish=True,
        )
        if session is not None:
            await self._close_session_safe(session)
        return result

    async def _execute_agent_turn_stateful(
        self,
        spec: dict[str, Any],
        *,
        existing_session: AgentSessionRef | None,
        existing_resolved: dict[str, Any] | None,
        existing_worktree: dict[str, Any] | None,
        close_on_finish: bool = False,
    ) -> tuple[StepResult, AgentSessionRef | None, dict[str, Any] | None, dict[str, Any] | None]:
        assert self._ctx is not None
        self._check_cancelled()
        wf_id = self._ctx.wf_id
        step_key = spec.get("step_key")
        override = self._ctx.override_targets.get(step_key or "")
        effective_target = override or spec.get("target")
        schema = spec.get("schema")
        input_hash = _step_input_hash(
            {
                "kind": "agent",
                "prompt": spec.get("prompt") or "",
                "capability": spec.get("capability"),
                "profile": spec.get("profile"),
                "target": effective_target,
                "runtime": spec.get("runtime"),
                "model": spec.get("model"),
                "cwd": spec.get("cwd"),
                "workspace": spec.get("workspace"),
                "schema": schema,
                "exclude_runtimes": list(spec.get("exclude_runtimes") or ()),
                "read_only": bool(spec.get("read_only")),
            }
        )

        # Checkpoint hit on resume (only when inputs match and no upstream step was invalidated)
        if step_key and existing_session is None:
            ckpt = self._lookup_valid_checkpoint(wf_id, step_key, input_hash)
            if ckpt is not None:
                return (
                    StepResult(
                        node_id=ckpt["node_id"],
                        step_key=step_key,
                        kind="agent",
                        status="completed",
                        value=ckpt.get("output"),
                        ok=True,
                        requested=ckpt.get("requested"),
                        resolved=ckpt.get("resolved"),
                        observed=ckpt.get("observed"),
                        session_ref=ckpt.get("session_ref"),
                        worktree=ckpt.get("worktree_path"),
                        from_checkpoint=True,
                    ),
                    None,
                    ckpt.get("resolved"),
                    {"worktree_path": ckpt.get("worktree_path")} if ckpt.get("worktree_path") else None,
                )

        node_id = self._next_node_id("step")
        started = time.time()
        probe_owner = f"{wf_id}:{node_id}"
        prompt = spec.get("prompt") or ""
        if not prompt.strip():
            raise WorkflowError("usage_error", "agent() step requires a non-empty prompt.", EXIT_USAGE)

        if existing_session is not None and existing_resolved is not None:
            resolution = {
                "requested": existing_resolved,
                "degradation_policy": "strict",
                "candidates": [existing_resolved],
                "skipped": [],
            }
        else:
            resolution = resolve_step_candidates(
                self.store,
                capability=spec.get("capability") if not effective_target else None,
                profile=spec.get("profile") if not effective_target else None,
                target=effective_target,
                runtime=spec.get("runtime") if not effective_target else None,
                model=spec.get("model"),
                require_kind="runtime",
                exclude_runtimes=spec.get("exclude_runtimes") or (),
                probe_owner=probe_owner,
                claim_probes=False,
            )

        role = spec.get("role") or (prompt.splitlines()[0][:64] if prompt else node_id)
        self.store.upsert_node(
            wf_id,
            node_id,
            step_key=step_key,
            input_hash=input_hash,
            parent_id=self._ctx.current_phase_id,
            node_type="step",
            kind="agent",
            role=role,
            status="queued",
            requested=resolution["requested"],
            schema=schema,
            started_at=started,
        )

        candidates = resolution["candidates"]
        if not candidates:
            err = {
                "code": "blocked_no_healthy_target",
                "message": f"No healthy runtime target available for step '{role}'.",
                "skipped": resolution["skipped"],
                "degradation_policy": resolution["degradation_policy"],
            }
            self.store.upsert_node(wf_id, node_id, status="blocked", error=err, finished_at=time.time())
            raise WorkflowError(
                "blocked_no_healthy_target",
                err["message"],
                EXIT_ERROR,
                hint=(
                    f"Inspect health with '{self._corral_bin} workflow health' or "
                    f"resume with --override-target {step_key or node_id}=<target>."
                ),
                next_commands=[f"{self._corral_bin} workflow health", f"{self._corral_bin} workflow resume {wf_id}"],
                context=err,
            )

        worktree_info = existing_worktree
        exec_cwd = spec["cwd"]
        if spec.get("workspace") == "worktree" and worktree_info is None:
            wt_dir = self.store.root / "worktrees" / wf_id / node_id
            worktree_info = create_worktree(exec_cwd, wt_dir)
            exec_cwd = worktree_info["worktree_path"]
        elif worktree_info is not None:
            exec_cwd = worktree_info["worktree_path"]

        out_dir = self.store.root / "runs" / wf_id / "steps" / node_id
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        out_file = out_dir / "result.json"
        wired_prompt = self._build_worker_prompt(
            wf_id=wf_id,
            node_id=node_id,
            prompt=prompt,
            schema=schema,
            out_file=out_file,
        )

        attempts_log: list[dict[str, Any]] = []
        retries = int(spec.get("retries", 2))
        assert self._global_sem is not None

        async with self._global_sem:
            for cand in candidates:
                self._check_cancelled()
                tid = cand["target_id"]
                claimed_probe = False
                if cand.get("half_open_probe") and not tid.startswith("pin:"):
                    if not self.store.try_claim_half_open_probe(tid, owner=probe_owner):
                        resolution["skipped"].append(
                            {"target_id": tid, "reason": "circuit_half_open_probe_in_flight"}
                        )
                        continue
                    claimed_probe = True
                tsem = self._target_semaphore(tid, cand.get("max_concurrency"))
                session = existing_session
                for attempt_idx in range(1, retries + 2):
                    self._check_cancelled()
                    if attempt_idx > 1:
                        delay = min(1.0 * (2 ** (attempt_idx - 2)), 15.0) * (0.8 + 0.4 * random.random())
                        await self._sleep(delay)

                    self.store.upsert_node(
                        wf_id,
                        node_id,
                        status="running",
                        resolved={**cand, "attempts": attempts_log},
                        worktree_path=worktree_info["worktree_path"] if worktree_info else None,
                        attempt=len(attempts_log) + 1,
                    )

                    async def _run_once(
                        cur_cand: dict[str, Any],
                        cur_session: AgentSessionRef | None,
                        attempt_no: int,
                        cur_prompt: str,
                    ):
                        created_here = False
                        sess = cur_session
                        if sess is None:
                            sess = await self._backend.create_session(
                                runtime=cur_cand["runtime"],
                                cwd=exec_cwd,
                                model=cur_cand.get("model"),
                                wf_id=wf_id,
                                node_id=node_id,
                                actor=self._actor,
                            )
                            created_here = True
                            self._track_session(sess)
                            self.store.upsert_node(
                                wf_id,
                                node_id,
                                status="running",
                                session_ref=sess.session_ref,
                                keepalive_name=sess.keepalive_name,
                            )
                        outcome = await self._backend.run_turn(
                            sess,
                            prompt=cur_prompt,
                            request_id=f"{wf_id}:{node_id}:a{attempt_no}",
                            timeout=spec.get("timeout"),
                            actor=self._actor,
                        )
                        return sess, created_here, outcome

                    try:
                        if tsem is not None:
                            async with tsem:
                                session, created_here, outcome = await _run_once(
                                    cand, session, len(attempts_log) + 1, wired_prompt
                                )
                        else:
                            session, created_here, outcome = await _run_once(
                                cand, session, len(attempts_log) + 1, wired_prompt
                            )
                    except asyncio.CancelledError:
                        if claimed_probe:
                            self.store.release_half_open_probe(tid, owner=probe_owner)
                        if session is not None:
                            await self._close_session_safe(session)
                            session = None
                        self.store.upsert_node(
                            wf_id,
                            node_id,
                            status="cancelled",
                            error={"code": "cancelled", "message": "Agent step was cancelled."},
                            finished_at=time.time(),
                        )
                        raise
                    except Exception as exc:
                        code = getattr(exc, "code", "create_failed")
                        msg = getattr(exc, "message", str(exc))
                        outcome = BackendTurnOutcome(
                            state="failed", error_code=code, error_message=msg, observed_runtime=cand["runtime"]
                        )
                        created_here = False

                    self._check_cancelled()

                    # Handle waiting_input cleanly (no retry, surface blocked)
                    if outcome.state == "waiting_input":
                        err = {
                            "code": "waiting_input",
                            "message": outcome.error_message or "Step is waiting for user input.",
                        }
                        self.store.upsert_node(wf_id, node_id, status="waiting_input", error=err)
                        raise WorkflowError(
                            "waiting_input",
                            err["message"],
                            EXIT_ERROR,
                            context={"node_id": node_id, "session_ref": outcome.session_ref},
                        )

                    # Handle completed turn + pure-CLI finish handshake
                    if outcome.state == "completed":
                        value: Any = outcome.final_response
                        if schema is not None:
                            node_now = self.store.get_node(wf_id, node_id)
                            nudges = 0
                            while (
                                not node_now["output_committed"]
                                and nudges < DEFAULT_MAX_FINISH_NUDGES
                                and session is not None
                            ):
                                self._check_cancelled()
                                nudges += 1
                                root_flag = (
                                    f" --root {self.store.root}" if os.environ.get("CORRAL_WORKFLOW_ROOT") else ""
                                )
                                nudge_cmd = (
                                    f"{self._corral_bin} workflow finish{root_flag} "
                                    f"--wf {wf_id} --step {node_id} --json-file {out_file}"
                                )
                                nudge_prompt = (
                                    f"{workflow_marker(wf_id, node_id)}\n\n"
                                    f"Your previous turn finished without committing structured output. "
                                    f"Write valid JSON to `{out_file}` and run `{nudge_cmd}` now."
                                )
                                nudge_outcome = await self._backend.run_turn(
                                    session,
                                    prompt=nudge_prompt,
                                    request_id=f"{wf_id}:{node_id}:nudge{nudges}",
                                    timeout=spec.get("timeout"),
                                    actor=self._actor,
                                )
                                self._check_cancelled()
                                node_now = self.store.get_node(wf_id, node_id)
                                if nudge_outcome.state != "completed":
                                    break
                            if node_now["output_committed"]:
                                value = node_now["output"]
                            elif (
                                schema.get("type") == "string"
                                and spec.get("allow_text_fallback")
                                and isinstance(outcome.final_response, str)
                            ):
                                value = outcome.final_response
                                self.store.commit_step_output(wf_id, node_id, value, actor=self._actor)
                            else:
                                err = {
                                    "code": "missing_structured_output",
                                    "message": (
                                        f"Step '{role}' completed without submitting schema-validated output "
                                        "via 'corral workflow finish'."
                                    ),
                                }
                                if close_on_finish and session is not None:
                                    await self._close_session_safe(session)
                                    session = None
                                self.store.upsert_node(
                                    wf_id, node_id, status="failed", error=err, finished_at=time.time()
                                )
                                raise WorkflowError(
                                    "missing_structured_output",
                                    err["message"],
                                    EXIT_ERROR,
                                    context={"node_id": node_id},
                                )

                        if not tid.startswith("pin:"):
                            self.store.record_target_success(tid)
                        diff_stat = (
                            worktree_diff_summary(
                                worktree_info["worktree_path"], base_commit=worktree_info["base_commit"]
                            )
                            if worktree_info
                            else None
                        )
                        observed = {
                            "runtime": outcome.observed_runtime or cand["runtime"],
                            "model_observed": outcome.observed_model or cand.get("model"),
                            "evidence_source": outcome.evidence_source,
                        }
                        finished = time.time()
                        self.store.upsert_node(
                            wf_id,
                            node_id,
                            input_hash=input_hash,
                            status="completed",
                            resolved={**cand, "attempts": attempts_log},
                            observed=observed,
                            output=value,
                            output_committed=True,
                            session_ref=outcome.session_ref or (session.session_ref if session else None),
                            keepalive_name=outcome.keepalive_name or (session.keepalive_name if session else None),
                            error=None,
                            finished_at=finished,
                            duration_ms=int((finished - started) * 1000),
                        )
                        if close_on_finish and session is not None:
                            await self._close_session_safe(session)
                            session = None
                        return (
                            StepResult(
                                node_id=node_id,
                                step_key=step_key,
                                kind="agent",
                                status="completed",
                                value=value,
                                ok=True,
                                requested=resolution["requested"],
                                resolved={**cand, "attempts": attempts_log},
                                observed=observed,
                                session_ref=outcome.session_ref,
                                worktree=worktree_info["worktree_path"] if worktree_info else None,
                                diff_summary=diff_stat,
                            ),
                            session,
                            cand,
                            worktree_info,
                        )

                    # Failure classification & side-effect-safe degradation
                    cls = classify_failure(
                        outcome.error_code,
                        outcome.error_message,
                        execution_state=outcome.state,
                        retry_after_s=outcome.retry_after_s,
                    )
                    attempts_log.append(
                        {
                            "target_id": tid,
                            "runtime": cand["runtime"],
                            "attempt": attempt_idx,
                            "state": outcome.state,
                            "code": outcome.error_code,
                            "failure_class": cls["failure_class"],
                            "message": (outcome.error_message or "")[:240],
                        }
                    )
                    if created_here and session is not None:
                        await self._close_session_safe(session)
                        session = None

                    if cls["failure_class"] == FAILURE_UNCERTAIN:
                        err = {
                            "code": "execution_unknown",
                            "message": outcome.error_message or "Execution state is unknown; refusing blind replay.",
                            "attempts": attempts_log,
                        }
                        self.store.upsert_node(wf_id, node_id, status="blocked", error=err, finished_at=time.time())
                        raise WorkflowError("execution_unknown", err["message"], EXIT_ERROR, context=err)

                    if cls["failure_class"] == FAILURE_CALLER or cls["abort"]:
                        err = {
                            "code": outcome.error_code or "usage_error",
                            "message": outcome.error_message or "Caller error",
                            "attempts": attempts_log,
                        }
                        self.store.upsert_node(wf_id, node_id, status="failed", error=err, finished_at=time.time())
                        raise WorkflowError(err["code"], err["message"], EXIT_USAGE, context=err)

                    if not tid.startswith("pin:"):
                        self.store.record_target_failure(
                            tid,
                            failure_class=cls["failure_class"],
                            reason=outcome.error_message or outcome.error_code or "failed",
                            open_immediately=cls["open_immediately"],
                            cooldown_seconds=cls["cooldown_seconds"],
                        )

                    # Side-effect guard before fallback/retry on mutating steps
                    if not spec.get("read_only") and outcome.had_tool_activity:
                        if worktree_info is not None:
                            if spec.get("on_partial_fallback", "clean_retry") == "clean_retry":
                                reset_worktree_clean(
                                    worktree_info["worktree_path"], base_commit=worktree_info["base_commit"]
                                )
                            else:
                                diff_info = worktree_diff_summary(
                                    worktree_info["worktree_path"], base_commit=worktree_info["base_commit"]
                                )
                                wired_prompt = (
                                    f"{wired_prompt}\n\n[Fallback Handoff] Previous attempt failed "
                                    f"({outcome.error_code}); existing worktree diff:\n{diff_info['diff_stat']}"
                                )
                        else:
                            err = {
                                "code": "degradation_blocked_dirty_workspace",
                                "message": (
                                    "Step failed after running tools inside shared workspace='inherit'; "
                                    "refusing silent replay on dirty files."
                                ),
                                "attempts": attempts_log,
                            }
                            self.store.upsert_node(wf_id, node_id, status="blocked", error=err, finished_at=time.time())
                            raise WorkflowError(
                                "degradation_blocked_dirty_workspace", err["message"], EXIT_ERROR, context=err
                            )

                    if not cls["retry_same_target"]:
                        break  # immediately fall back to next candidate in profile chain

        err = {
            "code": "blocked_no_healthy_target",
            "message": f"All candidate targets failed for step '{role}'.",
            "attempts": attempts_log,
            "skipped": resolution["skipped"],
        }
        status = "failed" if resolution["degradation_policy"] == "strict" else "blocked"
        self.store.upsert_node(wf_id, node_id, status=status, error=err, finished_at=time.time())
        raise WorkflowError(
            "blocked_no_healthy_target",
            err["message"],
            EXIT_ERROR,
            hint=f"Run '{self._corral_bin} workflow health' or resume with --override-target.",
            next_commands=[f"{self._corral_bin} workflow health", f"{self._corral_bin} workflow resume {wf_id}"],
            context=err,
        )

    async def _execute_parallel(
        self,
        items: list[Any],
        fn: Callable[[Any], Any],
        *,
        max_concurrency: int | None,
        on_error: str,
        label: str,
    ) -> list[Any]:
        assert self._ctx is not None
        self._check_cancelled()
        if on_error not in {"fail_fast", "collect"}:
            raise WorkflowError(
                "usage_error", f"parallel() on_error must be 'fail_fast' or 'collect', got {on_error!r}.", EXIT_USAGE
            )
        wf_id = self._ctx.wf_id
        group_id = self._next_node_id("group")
        parent_before = self._ctx.current_phase_id
        started = time.time()
        self.store.upsert_node(
            wf_id,
            group_id,
            parent_id=parent_before,
            node_type="group",
            kind="parallel",
            role=label,
            status="running",
            started_at=started,
        )
        local_limit = max(1, int(max_concurrency)) if max_concurrency else self._ctx.max_concurrency
        local_sem = asyncio.Semaphore(local_limit)

        async def _run_item(idx: int, item: Any) -> Any:
            async with local_sem:
                self._check_cancelled()
                try:
                    res = fn(item)
                    if hasattr(res, "__await__"):
                        return await res
                    return res
                except Exception as exc:
                    if on_error == "collect":
                        err_dict = (
                            exc.to_error_dict()
                            if isinstance(exc, WorkflowError)
                            else {"code": "error", "message": str(exc)}
                        )
                        return StepResult(
                            node_id=f"{group_id}:item-{idx}",
                            step_key=None,
                            kind="parallel_item",
                            status="failed",
                            value=None,
                            ok=False,
                            error=err_dict,
                        )
                    raise

        prev_phase = self._ctx.current_phase_id
        self._ctx.current_phase_id = group_id
        tasks: list[asyncio.Task[Any]] = []
        try:
            tasks = [asyncio.create_task(_run_item(i, item)) for i, item in enumerate(items)]
            if on_error == "fail_fast":
                try:
                    results = await asyncio.gather(*tasks)
                except asyncio.CancelledError:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    self.store.upsert_node(wf_id, group_id, status="cancelled", finished_at=time.time())
                    raise
                except Exception:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    self.store.upsert_node(wf_id, group_id, status="failed", finished_at=time.time())
                    raise
            else:
                try:
                    results = await asyncio.gather(*tasks)
                except asyncio.CancelledError:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    self.store.upsert_node(wf_id, group_id, status="cancelled", finished_at=time.time())
                    raise
            failed_count = sum(1 for r in results if isinstance(r, StepResult) and not r.ok)
            finished = time.time()
            self.store.upsert_node(
                wf_id,
                group_id,
                status="completed",
                progress_note=f"{len(results) - failed_count}/{len(results)} succeeded",
                finished_at=finished,
                duration_ms=int((finished - started) * 1000),
            )
            return list(results)
        finally:
            self._ctx.current_phase_id = prev_phase

    async def _execute_pipeline(
        self,
        items: list[Any],
        stages: list[Callable[[Any], Any]],
        *,
        max_concurrency: int | None,
        on_error: str,
        label: str,
    ) -> list[Any]:
        async def _chain(item: Any) -> Any:
            current = item
            for stage in stages:
                res = stage(current)
                current = await res if hasattr(res, "__await__") else res
            return current

        return await self._execute_parallel(
            items,
            _chain,
            max_concurrency=max_concurrency,
            on_error=on_error,
            label=label,
        )
