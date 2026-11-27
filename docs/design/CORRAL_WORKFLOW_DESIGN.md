# Corral Workflow Orchestration Design

Status: **Phase 1 implemented and verified on `main` (`d084661`, `7765508`); Phase 2A/2B/3 pending; live dual-runtime playbook acceptance pending owner authorization.**
Items under §1 record adopted owner decisions; §§2–7 describe the implemented Phase 1 host engine, capability catalog, circuit-breaker, pure-CLI and session-hiding contracts; §8 specifies the upcoming Mac-first visual flowchart and remote protocol; §9 tracks delivery and verification status.

For single-session delegation primitives and host-neutral control boundaries, see [Agent session delegation](AGENT_SESSION_DELEGATION_DESIGN.md) and `~/Codes/AgentSessionControl/docs/CONTRACT.md`. For the adjacent human-driven idea canvas, see [Web Task Butler](WEB_TASK_BUTLER_DESIGN.md).

---

## 1. Adopted owner decisions (2026-11-27)

1. **Product entry and layering:** Ship as `corral workflow` (with short alias `corral wf`) inside Corral. Keep the orchestration engine decoupled from TUI/Apple presentation and consume `AgentSessionControl`'s `SessionController` contract rather than embedding orchestration policy into `AgentSessionControl` or duplicating session control.
2. **Python first-class orchestration language:** Workflow scripts are written in Python (top-level `await` supported) so execution requires no extra Node/Deno/Bun runtime beyond Corral's existing Python environment.
3. **Pure CLI over MCP:** Do not introduce MCP servers or per-step stdio MCP tools for workflow execution or structured output submission. All workflow authoring queries, worker structured-output submission, schema inspection, and status logging use lightweight Corral CLI commands following the `agent-friendly-cli` contract.
4. **High local concurrency:** Local execution must sustain high concurrency cleanly (real host capacity reaches roughly 30 concurrent local agents before saturating). Architecture must avoid artificial low single-digit hard caps while preventing keepalive-pool contention, supervisor process bloat, and remote-cache spikes.
5. **Main session list isolation + dedicated visual flowchart UI:** Child sessions spawned by a workflow must never appear in the main session list (TUI sidebar, Mac sidebar, iPhone list, or default `corral list`). Clients expose a dedicated **Workflows** entry with a visual flowchart view for inspecting progress, structured outputs, and intervening on blocked nodes.
6. **Mac-first visual rollout:** After the host engine and CLI loop are verified, build and polish the visual flowchart experience on the native Mac client (`CorralMac`) first, then propagate the shared data model and adapted views to iPhone/iPad and TUI.
7. **Capability-first workflow subjects with explicit target escape hatch:** Workflow scripts normally address **capability abstractions** (e.g., `ui-design`, `code-review`, `report-writing`, `cheap-fanout`, `deep-impl`) rather than hard-coding vendor runtime/model names, preserving Corral's runtime-agnostic portability. Explicit concrete `target` or `(runtime, model)` pinning remains supported as a transparent escape hatch for user-pinned steps or A/B benchmarks.
8. **Zero-config onboarding via Local Scan ∩ Cloud Capability Registry + Local CLI Overrides:** On a new machine, Corral automatically scans installed runtimes and locally available models (with automatic background refresh) and intersects that inventory with a cloud-maintained capability-to-runtime/model ranking and fallback map (with a bundled offline snapshot), overlaid by user-configured global metadata via an `agent-friendly-cli` surface.
9. **Unavailability detection, backoff, and safe degradation:** Explicitly design how unavailable runtimes or models are detected, circuit-broken, backed off, and degraded across fallback chains without silent policy violations or unsafe side-effect replay.

---

## 2. Layer ownership and boundaries

Keep dependency direction strictly acyclic:

| Layer | Owns | Does not own |
|---|---|---|
| `SessKit` | Native history parsing, incremental event readers, typed turn outcomes and native error evidence across runtimes. | Workflow state, session-list hiding, routing profiles. |
| `AgentSessionControl` | Host-neutral launch/resume plans, durable single-session `LocalController`, input ownership, best-effort single-create probing (`besteffort.py`). | Multi-step DAG scheduling, global capability catalog, shared circuit breaker, UI. |
| `corral workflow` (new module in `cli/`) | Python workflow runner, SQLite DAG/checkpoint/audit store, worker CLI (`finish`/`schema`/`log`), local runtime/model inventory scanner, cloud capability registry sync/cache, user metadata override CLI, shared circuit breaker & fallback router, isolated workflow keepalive lifecycle, worktree manager. | Re-parsing native JSONL/SQLite history; overriding human interactive session defaults (`resume`, `handoff`, blank creation, direct launch, `shim`, `titlegen`). |
| `corral remote` | Encrypted `workflow.*` snapshot/delta/control RPCs and Workflow-level aggregated notifications (Mac local + iPhone APNs). | Executing workflow scripts or pushing full child transcripts eagerly on list/graph open. |
| `CorralMac` / iOS / TUI | Dedicated Workflows entry, visual DAG flowchart (Mac first), collapsible fan-out clusters, lazy node inspector reusing existing chat/question/`SwiftTerm`/embedded terminal views. | Local agent hosting (Apple clients remain remote clients of the host). |

Relation to existing Corral surfaces:
- `corral run` remains the atomic single-session delegation surface.
- `corral peers` / `corral send` remains ad-hoc peer A2A Q&A between already-running sessions.
- `corral workflow` is the deterministic, checkpointed, repeatable multi-step and high-fan-out orchestration layer.
- `WEB_TASK_BUTLER` remains an independent human-facing free-form idea canvas; it may optionally consume workflow checkpoints later.

Scope reconciliation with Corral's model-selection policy:
- Existing human interactive paths (`resume`, `handoff`, blank creation, direct launch, `shim`, `titlegen`) remain untouched and continue inheriting each runtime's user defaults without any cloud or hard-coded ranking.
- The cloud capability ranking applies **exclusively** inside `corral workflow` capability resolution, is always intersected with the user's locally available runtimes/models, is always previewable before execution (`--dry-run`), and is always outranked by explicit user CLI overrides or explicit step pins.

---

## 3. Capability abstraction, local discovery, cloud registry, and routing metadata (`agent-friendly-cli`)

### 3.1 Capability abstraction vs concrete runtime/model

Workflow scripts declare **what capability a step requires** as the first-class subject, decoupling reusable workflows from any single machine's subscriptions or vendor lock-in:

1. **Primary (recommended default) — Capability / Role Profile (`capability="ui-design"` or `profile="code-review"`):**
   - Portable across machines and users (a workflow authored on a Claude + Codex machine runs cleanly on an OpenCode + Cursor + Antigravity machine).
   - Survives model version churn and subscription changes without editing scripts.
   - Enables deterministic fallback chains and cross-model adversarial review (`exclude_runtimes=[impl.observed_runtime]`).
2. **Secondary (optional ad-hoc query) — Attribute Constraints (`require=["code-review"], prefer_cost="low"`):**
   - Useful when a workflow-authoring agent needs a one-off combination without creating a named profile.
3. **Escape hatch (explicit pin) — Concrete Target or `(runtime, model)` (`target="codex-default"` or `runtime="claude", model="opus"`):**
   - Supported for explicit user requests ("run this step on Codex"), A/B model comparison benchmarks, or runtime-specific workflows.
   - Still passes through circuit-breaker awareness and honest `requested` / `resolved` / `observed` provenance tracking.

### 3.2 Three-source resolution pipeline (zero-config on day 1)

On a fresh machine with zero manual setup, `corral workflow` computes the effective candidate chain for every capability through a deterministic four-stage pipeline:

```text
[1. Local Scanned Inventory]     ∩     [2. Cloud Capability Registry]
 (installed runtimes + locally          (capability -> ranked model families,
  available models + capabilities)       cost/speed tiers & fallback order)
                     \                 /
                      ▼  Intersection ▼
           [Effective Auto-Computed Candidate Chain]
                              │
                              ▼  Overlay (always wins)
           [3. Local User CLI Overrides (~/.config/corral/)]
            (custom rankings, custom capabilities, pins, disables,
             private/local models, per-target concurrency caps)
                              │
                              ▼  Live Health Filter
           [4. Shared Circuit Breaker & Concurrency Caps]
                              │
                              ▼
             [Selected Executable Target + Fallback Chain]
```

Exact precedence order (highest wins):
1. Explicit step/CLI pin (`runtime`/`model`/`target` or `resume --override-target`).
2. User-configured local `profile` / `target` overrides (via `corral workflow profile/target set`).
3. Intersection of Cloud Capability Registry rankings with Local Scanned Inventory.
4. Live circuit breaker (`closed` / `half_open` probe; skip `open`) and per-target `max_concurrency`.

### 3.3 Local runtime and model inventory scanner

Corral maintains an automatically refreshed local inventory inside the unified workflow SQLite store (`~/.config/corral/workflows/workflows.sqlite3`, schema v2, WAL + `IMMEDIATE`, `busy_timeout=30000`, `0o600`) describing what is actually installed and runnable on this machine:

- **Zero-quota local discovery only:** Background and automatic scans inspect executables in resolved `PATH`, cached CLI versions, runtime config/catalog files, and zero-cost local listing commands. **Scanners never issue paid model calls** to test quota or login; live billing/quota exhaustion is detected honestly during execution and fed into the circuit breaker (§4).
- **Per-runtime discovery boundary (active 6-runtime allowlist + optional LLM Gateway):**
  - `claude`: binary + version, `~/.claude/settings.json` default model, local auth presence hint, supported model override flags.
  - `codex`: binary + version, `~/.codex/config.toml` configured model/profile/provider, auth presence hint.
  - `opencode`: binary + version, local OpenCode provider/model config (or zero-cost `opencode models` where available); honestly records `supports_model_override` per installed version (for example, recording `false` on versions whose root interactive CLI rejects `--model` so only its active default model participates in intersection).
  - `cursor`: binary + version, local Cursor CLI config / default model, `supports_model_override`.
  - `pi`: binary + version, `~/.pi/agent/models.json` and `settings.json` full provider/model catalog.
  - `antigravity`: binary + version, `~/.gemini/antigravity-cli/settings.json` and local model metadata.
  - `gateway` (for lightweight `llm()` steps): if `~/.config/corral/llm-gateway.json` is configured, reads live `GET /v1/models` with short timeout and local cache, prioritizing curated gateway aliases (without provider `/` prefixes) so passthrough catalogs do not flood `workflow catalog`.
- **Automatic refresh policy (offline-first, non-blocking):**
  - First `corral workflow` run with an empty cache executes a fast initial scan.
  - Subsequent reads serve cached inventory immediately (frame-0 responsiveness) and trigger a background refresh when TTL expires (default 6 h) or when watched config files / binary `mtime` change.
  - Manual refresh via `corral workflow scan --refresh` (or `--dry-run` to preview detected changes).

### 3.4 Cloud-maintained capability registry

To give every new user high-quality routing out of the box, Corral maintains a public, versioned, static JSON registry mapping capabilities to canonical model families and runtime/model preferences:

- **Registry contents:**
  - `schema_version`, `registry_version`, `published_at`, `sha256`.
  - **Canonical capabilities (`capabilities`):** e.g., `ui-design` (visual aesthetics & UI implementation), `code-review` (adversarial defect detection), `report-writing` (clear structured prose & executive summaries), `deep-impl` (complex multi-file engineering), `debugging` (root-cause diagnosis), `cheap-fanout` (high-volume parallel audits/extraction), `fast-router` (low-latency planning/classification).
  - **Model normalization & baseline metadata (`models`):** Maps vendor/runtime-specific model identifiers and specific family substrings (e.g., `claude-opus-*`, `gpt-*-codex`, `gpt-6`, `deepseek-v4.1-flash`, `gemini-*-pro`) to canonical family IDs with baseline `cost_tier`, `speed_tier`, `strengths`, and `avoid_for`. Avoid overly broad bare substrings (`mini`, `pro`, `flash`) that falsely match unrelated model names (for example, `Gemini 4 Neon` matching `mini`).
  - **Ranked capability chains (`rankings`):** Ordered preference lists per capability plus deterministic tie-breakers when multiple local runtimes expose the same model family (e.g., first-party CLI preferred over generic wrapper unless user overrides).
- **Zero-telemetry, offline-first distribution:**
  - Strictly static read-only HTTP `GET` with `ETag` / `If-None-Match` conditional caching under `~/.cache/corral/workflow-registry.json` (default refresh TTL 24 h, short network timeout 3 s, background-only so execution never stalls on network issues). Close `urllib.error.HTTPError` responses (such as HTTP 304) in `finally` to prevent socket leaks.
  - **Never uploads local inventory, session data, or machine identifiers.**
  - **Bundled snapshot fallback:** Corral packages `src/corral/workflow/bundled_registry.json` inside the CLI distribution (explicitly included via `[tool.maturin] include = ["corral/workflow/*.json"]` in `pyproject.toml`) so air-gapped or offline installations still work out of the box.
  - **Self-hosting and opt-out:** `CORRAL_WORKFLOW_REGISTRY_URL` (or `corral workflow registry set-url <url>`) allows teams to point to an internal registry; `CORRAL_WORKFLOW_REGISTRY_OFFLINE=1` disables remote fetches completely.

### 3.5 Local user override model (`targets` and `profiles`)

Users can inspect, customize, or completely replace cloud rankings via two clean local override layers (persisted in SQLite with same-transaction append-only audit rows):

#### Targets (`target`) — concrete runtime/model entries

Each target has a semantic ID (preferred over UUIDs per `agent-friendly-cli`), e.g., `claude-opus`, `codex-default`, `opencode-default`, `pi-deepseek`, `antigravity-pro`, `gw-flash`. Auto-scanned targets are materialized automatically with `origin="scanned"`; user edits overlay fields with `origin="user_override"` so future scans update discovered facts without clobbering user preferences.

| Field | Type / Values | Purpose |
|---|---|---|
| `id` | `string` (slug, unique) | Semantic handle referenced by profiles and scripts. |
| `origin` | `scanned \| user_override \| user_custom` | Separates auto-discovered entries from user overrides/custom targets. |
| `kind` | `runtime \| gateway` | `runtime` executes full coding sessions via `AgentSessionControl`; `gateway` executes lightweight `llm()` calls. |
| `runtime` | `claude \| codex \| opencode \| cursor \| pi \| antigravity \| null` | Active Corral allowlist only (never exposes dormant runtimes). |
| `model` | `string \| null` | Explicit per-launch model override; `null` inherits the runtime's user default. Validated against `supports_model_override`. |
| `cost_tier` | `free \| low \| medium \| high` | Stable English enum for cost-aware fan-out decisions. |
| `speed_tier` | `fast \| medium \| slow` | Latency tier for routing interactive vs batch steps. |
| `strengths` | `list[string]` | User-extensible capability tags merged over cloud baseline tags. |
| `avoid_for` | `list[string]` | Optional negative guidance (e.g., `["cheap-fanout"]` for expensive flagship quotas). |
| `max_concurrency` | `int \| null` | Optional per-target concurrency cap (e.g., cap `claude-opus` at 4 while allowing `pi-deepseek` at 24 within a 30-agent workflow). |
| `enabled` | `bool` | User toggle to take a target offline without deleting its metadata. |
| `notes` | `string` | Concise human/agent guidance explaining taste, quirks, or subscription boundaries. |
| `version` | `string` (12-char SHA-256) | Deterministic optimistic-lock fingerprint over canonical JSON of mutable user fields (excludes volatile scan timestamps and runtime health metrics). |

#### Role profiles (`profile`) — semantic capability bindings

Every built-in cloud capability automatically appears as a resolved profile (`origin="cloud_derived"`), intersected with this machine's scanned inventory. Users may override any built-in profile's candidate order or define custom profiles (`origin="user_override"` / `"user_custom"`).

| Field | Type / Values | Purpose |
|---|---|---|
| `id` | `string` (slug, unique) | Capability / role name (e.g., `ui-design`, `code-review`, `report-writing`, `cheap-fanout`, `deep-impl`). |
| `origin` | `cloud_derived \| user_override \| user_custom` | Makes it explicit whether the chain came from cloud intersection or local customization. |
| `description` | `string` | When a workflow-authoring agent should pick this capability/profile. |
| `candidates` | `list[string]` (ordered target IDs) | Effective ordered primary → fallback target chain on this machine. |
| `degradation_policy` | `strict \| pause_on_exhaust \| best_effort` | `strict`: fail step when all candidates are unavailable; `pause_on_exhaust` (default): checkpoint and pause step as `blocked_no_healthy_target`; `best_effort`: allow falling back to another enabled target matching required strengths. |
| `version` | `string` (12-char SHA-256) | Optimistic-lock fingerprint over canonical mutable fields. |

### 3.6 `agent-friendly-cli` command surface and contract

Every command under `corral workflow` adheres to the P0/P1/Dual-Audience contract:

| Command | Risk | Audience & Behavior |
|---|---|---|
| `corral workflow catalog [--for <capability\|tag>] [--available-only] [--explain] [--format json\|tsv] [--compact] [--fields ...]` | `read` | Single-call progressive-disclosure read for workflow-authoring agents: returns effective capabilities/profiles on this machine, resolved candidate chains, strengths, cost/speed tiers, `origin` (`cloud_derived`/`user_override`), and live circuit health in ~150–300 tokens. `--explain` shows why each target ranked where it did (scan + cloud + override). |
| `corral workflow scan [--refresh] [--runtime R] [--dry-run] [--format json\|tsv]` | `read` / `write_local` | Inspects or refreshes the local runtime & available-model inventory (zero paid quota). Returns `scanned_runtimes`, discovered models, `supports_model_override`, and per-runtime read failures explicitly. |
| `corral workflow registry status \| sync [--force] \| set-url <url> --version <v> [--dry-run]` | `read` / `write_local` | Shows active registry version/source (`cloud_cached` vs `bundled`), triggers conditional ETag refresh, or configures a custom self-hosted registry URL. |
| `corral workflow target list [--runtime R] [--tag T] [--enabled-only] [--format json\|tsv]` | `read` | Lists scanned and user-configured targets with `origin`, `version` fingerprints, coverage metadata, and health summary. |
| `corral workflow target get <id>` | `read` | Full target definition plus `version` token, scan evidence, and current circuit state. |
| `corral workflow target set <id> --runtime <r> [--model M] [--cost ...] [--speed ...] [--strengths a,b] [--avoid c] [--max-concurrency N] [--notes ...] [--enabled\|--disabled] [--version <v>] [--dry-run]` | `write_local` | Declarative upsert. Creating a new custom ID requires no `--version`; modifying an existing target requires `--version <v>` (read-before-write optimistic lock). Echoes updated object + new `version`. |
| `corral workflow target delete <id> --version <v> [--dry-run]` | `write_local` | Deletes a user custom target or clears user overrides back to scanned defaults; fails with `conflict` (exit 5) if still referenced by a user profile unless `--force` is explicit, listing referencing profiles in `error.context`. |
| `corral workflow profile list \| get <id> \| set <id> --candidates t1,t2,... [--description ...] [--degradation ...] [--version <v>] [--dry-run] \| reset <id> --version <v> \| delete <id> --version <v>` | `read` / `write_local` | Same declarative upsert, `--version` guard, `--dry-run` parity, and audit logging. `reset` reverts a customized built-in capability back to the cloud-derived intersection. Validates all candidate target IDs exist before committing. |
| `corral workflow meta export --out <path>` / `import --file <path> [--dry-run]` | `read` / `write_local` | Exports/imports only user overrides and custom profiles/targets (never volatile scan caches); `--dry-run` reports exact create/update/unchanged diff without writing. |
| `corral workflow health [--target <id>] [--probe] [--reset <id> --version <v>]` | `read` / `write_local` | Shows binary presence, recent failure reasons, circuit state, and cooldown expiry. Default is strictly read-only and never spends model quota. |

Mandatory `agent-friendly-cli` invariants:
- **Envelope & exit codes:** Uniform `{"ok", "data", "error", "meta"}` on stdout for `--json` or non-TTY (including failures); progress/logs to stderr only. Exit codes match Corral's published contract (`0` ok, `1` generic error, `2` usage/schema/missing-version error, `3` not found, `4` auth/permission, `5` conflict/ambiguous/state, `6` timeout).
- **Human default:** Readable tables on TTY honoring `NO_COLOR`, `TERM=dumb`, and `--no-color`; non-TTY automatically emits JSON without requiring `--non-interactive`.
- **Self-healing errors:** Every error provides stable `error.code`, human `error.message`, `error.hint`, `error.retryable`, `error.context` (e.g., `available_targets`, `similar_ids`, `referencing_profiles`, `missing_runtimes`), and concrete copy-runnable `error.next_commands` preserving the active executable path.
- **Single-source `describe`:** `corral describe workflow ...` shares definitions directly with the parser, exposing exact flags, allowed enums, `risk`, preconditions, and 1–2 runnable examples.
- **Same-transaction audit:** Every metadata write records `(actor, action, target_id, before_version, after_version, timestamp_utc)` in an append-only SQLite audit table in the same transaction.
- **Zero secrets in catalog:** Neither the local inventory nor the cloud registry nor user overrides ever store API keys, tokens, or credentials.

---

## 4. Unavailability detection, circuit breaking, and safe degradation

At 20–30 concurrent steps, naive per-step retry loops cause cascading rate-limit storms and wasted startup overhead. Corral Workflow builds on `AgentSessionControl`'s `besteffort.py` failure-classification philosophy and adds a **shared cross-step circuit breaker** plus **side-effect-aware degradation rules**.

### 4.1 Four-layer failure classification

Every failure observed during preflight, launch, input submission, or turn execution is classified before deciding retry vs backoff vs fallback vs pause:

| Class | Signals & Evidence | Action |
|---|---|---|
| **1. Caller / Config Error** | `usage_error`, invalid JSON Schema, missing `cwd`, `not_authorized`, `idempotency_conflict`. | **Abort step immediately.** Retrying or switching targets cannot fix caller bugs. |
| **2. Deterministic Target Unavailable** | Binary missing from `PATH`, `unsupported`, `model_selection_unsupported` (e.g., OpenCode `--model`), `enabled=false`, missing login / expired auth detected via native startup/turn error. | **0 retries on this target.** Trip target circuit to `open` (long cooldown / until `health reset`), immediately advance to next fallback candidate. |
| **3. Hard Quota / Billing / Rolling Cap** | `429 AccountQuotaExceeded`, billing depleted, 5-hour/weekly rolling window cap surfaced by SessKit normalized error or `llm()` gateway status. | **0 retries on this target.** Close failed pane cleanly, immediately trip shared target circuit to `open` (e.g., 15 min default or parsed `Reset-At` / `Retry-After` timestamp) so sibling parallel workers stop slamming the exhausted provider, and advance to next fallback candidate. |
| **4. Transient Overload / Glitch** | HTTP `5xx`, provider `overloaded_error`, short-window transient rate limit with small `Retry-After`, transient tmux/startup race. | **Bounded jittered exponential backoff** on the same target (default max 2 attempts, base 1 s, cap 15 s, respecting `Retry-After` when bounded). If still failing or consecutive failures across sibling workers hit threshold (default 3), trip shared circuit to short `open` (60 s) and fall back. |
| **5. Uncertain / Mixed / Stalled Execution** | `delivery=unknown`, `execution=unknown`, unbound history timeout (150 s), `mixed_attribution`, `waiting_input`. | **Never blindly replay or auto-fallback.** `waiting_input` pauses the node and triggers a Workflow-level notification; uncertain outcomes preserve state honestly and require explicit recovery (`resume` or script-defined read-only retry opt-in). |

### 4.2 Shared cross-step circuit breaker (`target_health` table)

Persisted in `workflows.sqlite3` so all concurrent steps in one workflow — and concurrent workflows on the same host — share immediate health awareness:

- **`closed`**: Healthy; requests admitted up to the target's `max_concurrency` semaphore.
- **`open(opened_at, cooldown_until, reason, failure_class)`**: Incoming steps bypass this target with zero launch cost and resolve directly to the next healthy candidate in the profile chain.
- **`half_open`**: Once `now >= cooldown_until`, admit **exactly one** probe step (guarded by a single-flight lock in SQLite via `try_claim_half_open_probe`). Read-only preview resolution (`resolve_step_candidates(..., claim_probes=False)`, including `corral workflow run --dry-run`) **never** claims a `half_open` slot or mutates `target_health`. Execution claims the probe slot just-in-time right before attempting that candidate and releases it (`release_half_open_probe`) if cancelled before settling. If the probe succeeds, close the circuit and reset failure counters; if it fails with class 2–4, re-open with exponential cooldown backoff (e.g., 60 s → 5 min → 15 min → 60 min cap).
- **Manual recovery**: `corral workflow health --reset <target_id> --version <v>` resets a circuit immediately after the user re-authenticates or tops up quota.

### 4.3 Side-effect-safe degradation (coding vs read-only steps)

Falling back mid-step is fundamentally different for stateless queries versus repository-mutating coding agents:

1. **Read-only / pure steps** (`llm()`, read-only `agent(..., read_only=True)` reviews/audits, or pre-delivery launch failures where zero edits occurred): Safe to fail over immediately to the next candidate in the profile chain.
2. **Mutating step inside `workspace="worktree"`**: When a worker fails mid-turn (for example, hits quota halfway through editing files), the engine captures `git status` / `git diff` in that isolated worktree and applies the step's explicit `on_partial_fallback` mode:
   - `"clean_retry"` (default): Reset the disposable worktree to its base `HEAD` and run the fallback target from a clean slate — zero half-baked edits carried over.
   - `"handoff"`: Preserve the partial worktree diff and pass a compact handoff summary + failure reason to the fallback target to finish the remaining work.
3. **Mutating step inside `workspace="inherit"` (shared working tree)**: If the first turn already ran tools before failing, the engine **refuses silent replay** and pauses the step as `degradation_blocked_dirty_workspace` so the user or orchestrator inspects the workspace first.

### 4.4 Honest provenance and anti-quality-cliff guarantee

- Every step persists three separate fields (preserving Corral's existing honest-model rule):
  - `requested`: `{profile, target, runtime, model, exclude_runtimes}`
  - `resolved`: `{target, runtime, model, fallback_rank, fallback_reason, attempts[]}`
  - `observed`: `{runtime, model_observed, evidence_source}` (from SessKit / gateway response)
- **No silent downgrade outside user policy:** If every candidate in a profile's `candidates` list is open/unavailable and `degradation_policy` is `pause_on_exhaust` (default), the engine checkpoints the workflow and pauses that branch (`blocked_no_healthy_target`) instead of silently substituting an unsuitable low-quality model. The user can wait for recovery and run `corral workflow resume <wf_id>`, or explicitly override with `corral workflow resume <wf_id> --override-target <step_id>=<target_id>`.

---

## 5. Pure-CLI structured output contract (no MCP)

Every active runtime in Corral natively executes shell commands. Replacing MCP with lightweight CLI subcommands removes all per-worker stdio server processes (crucial at 30 concurrency) and eliminates per-runtime MCP compatibility gaps.

### 5.1 Worker-facing CLI commands

When launching an `agent()` step, the engine injects minimal non-secret environment variables (`CORRAL_WF_ID`, `CORRAL_WF_STEP_ID`, `CORRAL_WF_OUT_DIR`) and appends a compact submission contract to the step brief.

| Command | Behavior |
|---|---|
| `corral workflow finish [--wf ID] [--step ID] (--json-file <path> \| --json '<str>' \| --stdin)` | Reads the step's registered JSON Schema from SQLite and validates synchronously. **Valid (exit 0):** atomically persists payload + audit row, marks output committed. **Invalid (exit 2):** prints structured `error.code="schema_validation_failed"` with exact JSON-path errors (`$.blockers[0]: expected string, got int`) so the worker fixes and resubmits **in the same turn**. |
| `corral workflow schema [--wf ID] [--step ID]` | Prints the exact JSON Schema for the current step on demand, avoiding prompt bloat for large schemas. |
| `corral workflow log [--wf ID] [--step ID] "<message>"` | Records a concise live progress line shown on the flowchart node card (`"Audited 18/24 routes"`). |

### 5.2 Missing-submission guard

If a worker's turn settles (`execution=completed`) without calling `corral workflow finish`:
- When `schema` is required: the engine sends up to `max_finish_nudges` (default 2) bounded follow-up messages via `run message` stating the exact missing submission command and output path. If still absent, the step settles as `failed (missing_structured_output)`.
- When `schema` is `{"type": "string"}` and `allow_text_fallback=True`: the engine may explicitly accept the correlated final assistant response.

---

## 6. Python orchestration engine and 30-concurrency governance

### 6.1 Python primitives (minimal 6-primitive surface)

Workflow scripts run in a dedicated Python subprocess with top-level `await`, `args` dict (validated against optional script metadata), and six primitives:

```python
phase("1. Plan")
plan = await llm(
    f"Split work for: {args['goal']}",
    capability="fast-router",
    schema=PLAN_SCHEMA,
    step_key="plan",
)

phase("2. Parallel Implementation")
impls = await parallel(
    plan["tasks"],
    lambda t: agent(
        t["brief"],
        capability="deep-impl",
        workspace="worktree",
        role=f"Impl {t['id']}",
        step_key=f"impl:{t['id']}",
        schema=IMPL_SCHEMA,
    ),
    max_concurrency=16,
    on_error="collect",
    label="Implement Modules",
)

phase("3. Deterministic Gate + Cross-Model Review")
async def check_and_review(impl):
    if impl.error:
        return impl
    test = await shell("pytest -q", cwd=impl.worktree, role=f"Test {impl.step_key}")
    review = await agent(
        f"Review raw diff and test output only:\n{test.output}",
        capability="code-review",
        exclude_runtimes=[impl.observed_runtime],  # enforce cross-model review
        cwd=impl.worktree,
        read_only=True,
        role=f"Review {impl.step_key}",
        step_key=f"review:{impl.step_key}",
        schema=REVIEW_SCHEMA,
    )
    return {"impl": impl.value, "test_ok": test.ok, "review": review.value}

final = await parallel(impls, check_and_review, max_concurrency=12, on_error="collect")
return final
```

Key primitive capabilities:
- **`agent(...)`**: Selects its subject via `capability=` (alias `profile=`) by default, or explicit `target=` / `(runtime=, model=)` escape hatch. Supports both single-shot `await agent(...)` (auto-closes its tmux pane immediately after output commit to recycle host slots) and stateful multi-turn handles (`r = agent(...); await r("turn 1"); await r("turn 2"); await r.close()`).
- **`llm(...)`**: Direct OpenAI-compatible gateway call with schema enforcement (zero CLI startup cost, ideal for classification, planning, deduplication, and summarization).
- **`shell(...)`**: Deterministic command step (build, lint, tests, git diff), rendered as a first-class node in the flowchart.
- **`parallel(items, fn, *, max_concurrency, on_error="fail_fast"|"collect", label)`**: Synchronization barrier rendered as a collapsible fan-out cluster; `on_error="collect"` prevents one failed item from cancelling 29 healthy sibling steps.
- **`pipeline(items, *stages, max_concurrency, label)`**: Streams each item independently through multi-stage chains without a global stage barrier.
- **`phase(name)`**: Groups downstream nodes and fan-out clusters into named visual stages.

### 6.2 30-concurrency resource architecture

Informed by the local measurements in [MEMORY_OPTIMIZATION_DESIGN.md](MEMORY_OPTIMIZATION_DESIGN.md):

1. **Single-process `asyncio` scheduler:** One `WorkflowEngine` process drives `AgentSessionControl.LocalController` across all active steps instead of spawning 30 heavyweight per-run supervisor Python processes.
2. **Isolated workflow keepalive lifecycle:** Workflow child sessions carry an explicit `workflow_managed` lease and do not count against or trigger human `CORRAL_KEEPALIVE_MAX_SESSIONS` pressure reclamation. One-shot `agent()` panes close immediately on step completion while leaving native history permanently readable via SessKit, so live tmux pane count equals *active* concurrency rather than cumulative step count.
3. **Configurable concurrency + per-target caps:** Global workflow default `max_concurrency=12` (CLI `--max-concurrency` and per-call `parallel(..., max_concurrency=...)` configurable up to 30+ according to host capacity), intersected with per-target `max_concurrency` caps from the metadata catalog.
4. **Loop-of-Loops guardrails:** Default `max_retries_per_step=2`, `max_total_steps=200` (configurable), and explicit `step_timeout` stop runaway bugs without throttling legitimate wide fan-outs.
5. **SQLite WAL checkpoints, input binding, and `resume` safety:** `workflow_runs` persists original `cwd`, `script_source` snapshot, `script_sha256`, `owner_pid`, and `cancel_requested_at`. Every completed `shell` / `llm` / `agent` `step_key` persists a deterministic 16-char `input_hash` (binding command/prompt, `cwd`, capability/target/runtime/model, override target, schema, workspace, and `exclude_runtimes`), validated output, provenance, and `session_ref` in `~/.config/corral/workflows/workflows.sqlite3`.
   - `corral workflow resume <wf_id>` restores original `cwd`, detects disk-script drift against `script_sha256`, and fails with `EXIT_CONFLICT` (`script_changed`) unless `--use-saved-script` (replay saved snapshot) or `--allow-script-change` (execute modified script and automatically cascade-invalidate changed + downstream checkpoints) is explicitly passed.
   - Checkpoints are reused only when `input_hash` matches and no upstream step was invalidated; `--from-step <step_key>` or `--override-target` invalidates the affected step and downstream nodes.
6. **Real cancellation propagation and late-overwrite protection:** `corral workflow cancel <wf_id> [--wait S] [--force]` transitions live runs to `cancelling`, sends `SIGTERM` to `owner_pid`, and waits up to `--wait` (default 5 s) for `status == "cancelled"` (`cancel_confirmed`). The engine listens on both `SIGTERM` and a 100 ms SQLite cancel watcher, runs `shell()` in its own session (`start_new_session=True`) with process-group teardown (`os.killpg` `SIGTERM -> SIGKILL`), closes active agent sessions, cancels parallel child tasks, and guards both `update_workflow_run` and `upsert_node` inside SQLite transactions so late `completed` writes can never overwrite a cancelled run.

### 6.3 Orchestrator CLI commands

```bash
corral workflow run <script.py | playbook> [--cwd <dir>] [--arg k=v ...] [--max-concurrency N] [--dry-run] [--detach]
corral workflow list [--status running|cancelling|waiting|blocked|failed|completed|cancelled] [--format json|tsv]
corral workflow status <wf_id> [--compact]
corral workflow graph <wf_id> [--format json|ascii]
corral workflow events <wf_id> [--after <cursor>] [--follow]
corral workflow script <wf_id> [--out <path>]
corral workflow resume <wf_id> [--from-step <step_key>] [--override-target <step>=<target>] [--use-saved-script | --allow-script-change] [--dry-run]
corral workflow cancel <wf_id> [--wait <sec>] [--force] [--dry-run]
corral workflow playbook list [--scope all|project|global] [--tag <tag>] [--cwd <dir>] [--format json|tsv]
corral workflow playbook show <name> [--scope project|global] [--cwd <dir>] [--no-source]
corral workflow playbook save <name> (--file <path> | --from-run <wf_id>) [--scope project|global] [--cwd <dir>] [--version <v>] [--dry-run]
corral workflow playbook delete <name> [--scope project|global] [--cwd <dir>] --version <v> [--dry-run]
corral workflow playbook export-skill <name> [--scope project|global] [--dest <dir>] [--cwd <dir>] [--skill-name <slug>] [--dry-run]
corral workflow playbook unexport-skill <name> [--scope project|global] [--dest <dir>] [--cwd <dir>] [--skill-name <slug>] [--dry-run]
```

- `--dry-run` on `run` parses the script, validates `args` schema, checks referenced profiles/targets against the catalog and current circuit health, and reports a static plan/warnings without launching any runtime or mutating SQLite (including expired `open` circuit-breaker states).
- `--detach` spawns `sys.executable -m corral workflow --root <root> run ...` in a new session (`start_new_session=True`, logs under `<root>/logs/<wf_id>.log`).

### 6.4 Playbook library, iteration loop, and opt-in thin skill export (owner approved 2026-11-27)

To make existing workflows discoverable, reusable, and continuously optimizable across all 6 runtimes without causing always-on context bloat:

1. **Default tier — Playbook library (zero always-on skill pollution):**
   - Playbooks live in two scopes: project (`<cwd>/.corral/workflows/<name>.py`, higher precedence) and global (`~/.config/corral/workflows/playbooks/<name>.py`). Empty libraries report `count: 0` honestly; never assume built-in playbooks exist unless explicitly installed.
   - Each `.py` playbook may declare a module docstring and/or literal `WORKFLOW_META = {"description": "...", "tags": [...], "args": {...}}`, parsed purely via Python `ast` without executing code.
   - `playbook save` validates syntax and slug rules, enforces read-before-write `--version` (12-char SHA-256 of bytes) when overwriting an existing playbook, writes atomically, and records an audit row.
   - `playbook show <name>` returns metadata, source, `version`, and **Optimization Signals** aggregated from `workflows.sqlite3` (`workflow_runs` + `workflow_nodes`): historical run count, success rate, average elapsed time, slowest steps, degraded/fallback steps, and retry/failure hotspots so agents optimize from measured execution evidence.
   - `corral workflow script <wf_id>` exports the saved `script_source` snapshot from any historical run for inspection, diffing, or promotion via `playbook save --from-run <wf_id>`.
2. **Opt-in promoted tier — Thin pointer Skills (`playbook export-skill` / `unexport-skill`):**
   - Do **not** auto-generate a separate Skill for every workflow (prevents prompt bloat and cross-routing interference).
   - For the small set of mature, high-frequency workflows worth direct natural-language recall across runtimes, users explicitly run `playbook export-skill <name> [--scope project|global]`.
   - Default `--scope project` writes `<cwd>/.agents/skills/corral-wf-<name>/SKILL.md` so project-specific workflows stay scoped to that repository. `--scope global` writes to the shared user skills root (or explicit `--dest`).
   - Generated thin skills (~15–20 lines) contain only routing metadata and pointers to `corral workflow playbook show <name>` and `corral workflow run <name> --dry-run` (requiring explicit user authorization before executing workers) — never duplicating script logic so subsequent script iterations stay fresh. Ownership hashes are recorded in SQLite so `export-skill` / `unexport-skill` refuse to overwrite or delete user-modified files (`EXIT_CONFLICT`).

### 6.5 Cross-runtime agent discovery (owner correction 2026-11-27)

Workflow discovery and guidance use the same portable Corral skill and CLI across runtimes. Do not add runtime-specific extensions, hooks, or system-prompt injection for this purpose. The existing Pi session-identity extension remains responsible for identity and session integration only; its normal installer upgrades the extension without the Workflow prompt section. Registering the shared skill does not install or update runtime extensions.

The `description` in `src/corral/skills/corral/SKILL.md` advertises Workflow and playbook reuse in runtimes that expose skill metadata. The skill body owns when to use Corral, how to find and preview a workflow, and when to require explicit authorization before launching workers. Keep this behavior in the shared skill rather than per-runtime copies. A normal review or a plain parallel request must not launch a workflow.

Verify an upgrade from the installed Pi extension and inspect a fresh default session: the shared skill remains discoverable, the extension adds no Workflow prompt section, and the CLI still supports read-only discovery and preview. Verify actual tool selection separately for each runtime claimed to support the shared path.

---

## 7. Main session list isolation and aggregated notifications

A workflow launching 20–100 child sessions must never pollute the user's daily session list or flood their devices with 30 separate completion notifications.

1. **Dual provenance binding:**
   - Primary source of truth: `workflows.sqlite3` maps every workflow-owned `keepalive_name` and canonical `runtime:native_id` to `(workflow_id, step_id, phase)`.
   - Redundant cold-scan fallback: the initial step prompt envelope embeds a compact `wf:<wf_id>:<step_id>` marker so cold scans without cache still classify workflow-owned sessions accurately.
2. **Presentation filtering:**
   - **TUI sidebar, Mac sidebar, iPhone/iPad session list:** Hide all workflow-owned sessions by default. Child sessions are opened by clicking their node inside the **Workflows** visual flowchart view.
   - **`corral list` / `corral search`:** Exclude workflow-owned sessions by default (reporting `hidden_workflow_sessions` in coverage metadata); `--include-workflow` opts in explicitly.
   - **`corral show` / `corral share`:** Continue resolving any exact `runtime:id` directly for inspection and auditing.
3. **Aggregated Workflow notifications:**
   - Suppress per-child-session Mac local notifications and iPhone APNs completion pushes for workflow-owned sessions.
   - Emit **Workflow-level** notifications only on actionable transitions:
     - `workflow.waiting_input` (names the workflow, phase, step role, and question summary; tapping opens that node directly)
     - `workflow.blocked` (e.g., all fallback targets exhausted or dirty-workspace degradation guard hit)
     - `workflow.failed` (terminal failure with failing step summary)
     - `workflow.completed` (overall summary, e.g., `24/25 succeeded, 1 collected error`, elapsed time)

---

## 8. Visual flowchart architecture (Mac-first) and remote protocol

### 8.1 Remote wire protocol (`workflow.*`)

Extend `corral remote` over the existing encrypted long-lived transport, preserving [REMOTE_KNOWLEDGE_BASE.md](../REMOTE_KNOWLEDGE_BASE.md) invariants (snapshot + cursor deltas, bounded payload size, ~2 s state freshness, lazy detail loading):

- `workflow.list` → compact list of workflow runs (`wf_id`, `name`, `status`, `active_phase`, `counts{total,queued,running,waiting,blocked,done,failed}`, `started_at`, `elapsed_ms`).
- `workflow.subscribe(wf_id, after_seq?)` → `workflow.graph.snapshot` followed by `workflow.graph.delta` events (`node.upsert`, `group.update`, `edge.add`, `workflow.status`).
  - Graph payloads carry **topology and live status metadata only** (no full transcripts, no large JSON outputs, no terminal buffers).
  - Each leaf node includes `step_id`, `kind`, `role`, `phase_id`, `group_id`, `status`, `requested_profile`, `resolved_target`, `observed_runtime`, `degraded` (bool + reason), `attempt`, `duration_ms`, `progress_note`, `session_ref`.
- `workflow.step.detail(wf_id, step_id)` → lazy fetch on node click: prompt brief, validated JSON output (with bounded preview + spill/full fetch), attempt/fallback log, worktree diff stat, error envelope.
- **Reuse existing live session streams on demand:** When the user opens a node's **Conversation** or **Live Terminal** tab, the client subscribes to `session_ref` through the existing `session.subscribe` / `input.question` / `terminal.*` (Mac `SwiftTerm`) / `screen.*` channels — zero duplicated chat or terminal plumbing.
- `workflow.control(wf_id, action, params)` → `cancel`, `resume`, `resume_from_step`, `override_target`.

### 8.2 Phase-2A (Mac first): `CorralMac` visual flowchart

Target platform: macOS 26, Swift 6, `@Observable` (per `apple/AGENTS.md`).

- **Top-level navigation:** Native segmented control in the sidebar/toolbar switching between **Sessions** and **Workflows** (no proactive secondary pop-up windows; keeps the main Sessions list untouched).
- **Split workspace:**
  - **Left sidebar:** Workflow runs list (status indicator, title, compact progress bar, elapsed time, status filter).
  - **Center canvas (zoomable/pannable DAG):**
    - Deterministic layered layout (top-to-bottom or left-to-right Phases → step cards / fan-out clusters → barrier edges).
    - **Hybrid rendering for 120 Hz performance:** All directed edges (orthogonal/Bézier curves + subtle active-edge flow indicator) rendered in a single immediate-mode `SwiftUI.Canvas`; visible node cards rendered as lightweight native SwiftUI views inside a transformed `ZStack` with viewport culling and native macOS trackpad pan/zoom handling (`NSEvent` scroll-wheel / magnify bridge without coordinate-space drift).
    - **Collapsible Fan-Out Clusters (essential for 20–30 concurrent steps):** A `parallel()` or `pipeline()` batch renders by default as a clean compound cluster card showing aggregate counts (`18 done · 9 running · 1 waiting · 2 queued`) plus a compact color-coded status matrix sorted with `waiting_input` and `failed` items first. Clicking expands the cluster inline or drills into its nodes, keeping the macro flowchart legible at 30+ workers.
    - **Node visual anatomy:** Role title, runtime brand mark + resolved target/model badge (plus subtle fallback pill `↪ codex` if degraded), live status dot (reusing Corral attention semantics: amber waiting, green running, red failed, neutral done), elapsed time, and 1-line live `progress_note`.
  - **Right Inspector Drawer (lazy, opens on node selection):**
    - **Summary & Output tab:** Formatted structured JSON result, fallback/attempt timeline (showing requested vs resolved vs observed target), worktree diff summary, and node actions (`Resume from here`, `Retry with target...`).
    - **Conversation / Terminal tab:** Embeds `CorralMac`'s existing chat view, native question answer form, and `SwiftTerm` live terminal toggle for that node's `session_ref`.
- **Global metadata quick view (optional follow-up):** Read-only health/catalog popover showing active targets and open circuit breakers (mutations remain CLI-first until UI editing is explicitly requested).

### 8.3 Phase-2B (follow-up): iPhone / iPad and TUI

- **iPad (`apple/Shared/`):** Reuses the Mac zoomable DAG canvas + slide-over Inspector.
- **iPhone (portrait-only per owner rule):** Renders a **Vertical Flow Timeline** with collapsible fan-out cluster cards and a top **"Needs Attention"** strip surfacing `waiting_input` / `blocked` / `failed` nodes so the user can answer a blocking question in one tap using the existing iPhone chat/question screen, plus an optional pinch-zoomable overview map.
- **TUI:** Dedicated `Workflows` view (hotkey toggle) with a collapsible Unicode tree/stage rail on the left and the existing `EmbedPane` / JSON output inspector on the right.

---

## 9. Phased delivery and verification gates

| Phase | Scope | Verification Gate |
|---|---|---|
| **Phase 1: Inventory + Registry + Engine + Pure CLI** | **Implemented (`d084661`, `7765508`):** Local runtime/model inventory scanner (`workflow scan`), cloud capability registry sync + bundled snapshot (`workflow registry`), user override & catalog CLI (`workflow target/profile/catalog/health/meta`, `agent-friendly-cli` compliant), shared SQLite circuit breaker & read-only dry-run router, `WorkflowEngine` (single-process `asyncio`, Python 6 primitives, SQLite WAL v2 input-bound checkpoints, `cwd`/script-snapshot `resume`, process-group `cancel` propagation & late-overwrite guard), `workflow finish/schema/log` Worker CLI, git `worktree` isolation, main session list hiding, `corral workflow` Companion Skill. | 1. **Passed (`7765508`):** `compileall`, `ruff check src tests`, `tests/test_workflow.py` (16/16 with `-W default -W error::ResourceWarning`), full `ci-test.py` (3,575 tests, zero failures).<br>2. **Passed:** Clean-machine zero-config scan/catalog intersection, user override precedence, read-only `--dry-run` circuit-breaker invariance, separate-process `run --detach` + `cancel` teardown (0.2 s), `--cwd` + `--use-saved-script` / `--allow-script-change` cascade invalidation, 25-way simulated concurrency, worktree dirty-tree preservation, and main-session-list hiding.<br>3. **Pending owner authorization:** Live dual-runtime cross-model review playbook acceptance (sync updated bundled `corral` skill into active runtimes first, then verify end-to-end on two real runtimes with real model calls). |
| **Phase 2A: Remote protocol + `CorralMac` visual flowchart** | `corral remote` `workflow.*` RPCs & aggregated notifications; `CorralMac` **Sessions \| Workflows** switch, hybrid `Canvas`+`ZStack` DAG canvas, collapsible 30-node fan-out clusters, lazy Inspector reusing chat/question/`SwiftTerm`. | 1. Live `CorralMac` build installed and visually inspected via `macos-window-screenshot` across idle, 20-concurrent running cluster, degraded-fallback badge, `waiting_input`, and failed states.<br>2. Verified ~2 s delta freshness and bounded payload size.<br>3. Verified main Mac Sessions list contains zero workflow child sessions. |
| **Phase 2B: iPhone / iPad + TUI views** | Shared `apple/Shared/` workflow store extended to iPhone portrait vertical flow timeline + APNs workflow notifications; TUI `Workflows` view. | Real-device iPhone install (`ios-deliver`) + real TUI visual inspection; zero workflow sessions leaking into main lists across all three clients. |
| **Phase 3: First-class Worktree automation & Built-in Playbooks** | Automated `workspace="worktree"` creation, dirty-tree protection, diff/merge helpers, and 3–4 curated cross-runtime playbooks (cross-model PR review, bulk audit + disprove, TDD repair loop, multi-angle RCA). | Parallel mutating worktree workflow leaves main working tree untouched; fresh independent agent discovers catalog + runs workflow using only CLI `--help` / Skill. |

---

## 10. References

### Local authorities
- [Agent session delegation](AGENT_SESSION_DELEGATION_DESIGN.md)
- [Memory budgets](MEMORY_OPTIMIZATION_DESIGN.md)
- [Remote protocol](../REMOTE_KNOWLEDGE_BASE.md)
- [Agent CLI skill](../SKILL.md)
- [Web Task Butler](WEB_TASK_BUTLER_DESIGN.md)
- `~/Codes/AgentSessionControl/docs/DESIGN.md`, `~/Codes/AgentSessionControl/docs/CONTRACT.md`, `~/Codes/AgentSessionControl/src/agent_session_control/besteffort.py`

### External research consulted (2026-11-27)
- Claude Code Dynamic Workflows (`agent`/`parallel`/`pipeline`/`schema`, `ultracode`) and programmatic tool calling: [Claude Code headless docs](https://code.claude.com/docs/en/headless), [Anthropic engineering](https://www.anthropic.com/engineering/writing-tools-for-agents)
- Capability-based routing, circuit breakers, and 429/quota fallback chains: [LiteLLM](https://docs.litellm.ai/), [Bifrost / Maxim AI](https://www.getmaxim.ai/), [Cloudflare Agents / Code Mode](https://developers.cloudflare.com/agents/)
- Durable local/SQLite execution and dynamic execution-graph visualization (LangGraph Studio, Mastra, DBOS, Temporal): [LangGraph](https://www.langchain.com/langgraph), [Mastra](https://mastra.ai/), [DBOS](https://www.dbos.dev/), [Temporal](https://temporal.io/)
- Agent-friendly CLI standards: [clig.dev](https://clig.dev/), [Writing effective tools for AI agents (Anthropic)](https://www.anthropic.com/engineering/writing-tools-for-agents)
