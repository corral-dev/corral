# Corral Plugin Architecture & Core Decoupling Design

Status: **Approved (2026-11-27); Wave 0 (Core contracts & plugin runtime) and Wave 1 (`corral-workflow` extraction) implemented and verified on `main`. Wave 0 `remote/sessions.py` & `skill_bundle.py` cleanups and Wave 2 (`a2a`/`control`) await completion of concurrent Pi identity work.**

This document specifies how Corral decouples optional extension capabilities (`workflow`, `a2a`, and optionally single-session `run` delegation, plus existing external tools such as `flex`) into installable plugins while cleaning up core layering inversions.

Related authorities:
- [Corral workflow orchestration](CORRAL_WORKFLOW_DESIGN.md)
- [Agent session delegation](AGENT_SESSION_DELEGATION_DESIGN.md)
- [Bundled agent skills](BUNDLED_AGENT_SKILLS_DESIGN.md)
- [Activity card (agentflex)](SHAREABLE_WRAP_CARD_DESIGN.md)
- [Memory budgets](MEMORY_OPTIMIZATION_DESIGN.md)
- [Performance knowledge base](../PERFORMANCE_KNOWLEDGE_BASE.md)
- [Remote protocol](../REMOTE_KNOWLEDGE_BASE.md)

---

## 1. Problem Statement & Design Goals

### 1.1 Codebase Baseline (2026-11-27 Audit)

`cli/src/corral/` currently totals **88,604 lines of Python** across seven functional areas:

| Layer | Files | Lines | Share | Current Role |
|---|---:|---:|---:|---|
| Core (`runtime/`, `embed`, `keepalive`, `store`, `cache`, `attention`, `titles`, etc.) | 68 | 26,865 | 30.3% | Session scanning, tmux hosting, liveness, attention, titles |
| TUI (`ui/`, `cli.py`, `split_layout`, `i18n`, etc.) | 37 | 22,193 | 25.0% | Interactive terminal UI |
| Remote (`remote/`) | 30 | 21,359 | 24.1% | Encrypted LAN/relay daemon for Mac and iPhone clients |
| **Workflow (`workflow/`)** | **12** | **8,248** | **9.3%** | Multi-stage Python DAG orchestration, routing, circuit breaker, playbooks |
| **Control & A2A (`control*.py`)** | **4** | **3,625** | **4.1%** | Single-session delegation (`run *`) and peer A2A (`peers`, `send`, `a2a *`) |
| Agent Read API (`agent_api.py`) | 1 | 2,398 | 2.7% | Machine-readable queries plus shared command registry / `describe` |
| Utilities (`updater`, `tcc`, `shim`, `skill_bundle`, `flex_cmd`, `cursor_observer`) | 7 | 3,916 | 4.4% | Auxiliary CLI tooling (`flex_cmd.py` already delegates to an external binary) |

Key structural findings from the audit:
- **Apple clients (`apple/`) currently have zero `workflow` or `a2a` coupling** (Phase 2A Mac flowchart has not started), making this the lowest-cost window to establish plugin boundaries.
- **Reverse coupling from Core into extensions is small (< 10 call sites)**, concentrated in `bootstrap.py`, `agent_api.py`, `store.py`, `reclaim.py`, `models.py`, `transcript.py`, and `remote/sessions.py`.
- **`workflow/` is already ~95% decoupled from Corral Core** because its execution engine runs directly on `agent-session-control` + `sesskit`.
- **Three non-plugin layering inversions** have accumulated in Core/Remote/TUI (`Core -> remote.richmsg`, `remote -> ui`, `control_worker -> remote.sessions`) and should be resolved as Wave 0 prerequisites.

### 1.2 Design Goals

1. **Slim, fast Core:** Core owns session discovery (via SessKit), hosted terminal lifecycle, TUI, read-only Agent queries, and remote transport. Cold start for `corral --version` and fast subcommands stays in the 10–30 ms range (`bootstrap.py` never imports heavy libraries or plugins eagerly).
2. **User choice & opt-in footprint:** Users who only need session handoff/TUI do not pay installation size, dependency pinning, or Agent skill context for orchestration or A2A.
3. **Upgrade persistence across Homebrew, `install.sh`, and `uv`/`pipx`:** Running `brew upgrade x0c/tap/corral` wipes Homebrew's Cellar virtualenv; installed plugins and their state must live outside Homebrew/site-packages and survive upgrades intact.
4. **Strict acyclic dependency direction:** Core never imports any plugin. All Core-to-plugin interactions use small, versioned, fail-safe on-disk or local IPC contracts.
5. **Zero UX regression for installed plugins:** Once a plugin is installed, `corral workflow ...`, `corral a2a ...`, `corral send ...`, `corral describe ...`, and bundled agent skills behave seamlessly under the `corral` CLI contract (`agent-friendly-cli`).
6. **Polyglot readiness:** Plugins may be implemented in Python, Rust, Go, or any executable format, unifying existing `corral flex` (`agentflex`) with future extensions.

---

## 2. Wave 0 Prerequisites: Core Architecture Cleanup

Before moving extensions out of Core, fix the existing layering inversions and circular imports so every upper layer (`ui/`, `remote/`, plugins) depends strictly downward on Core.

### 2.1 Resolve 3 Layering Inversions

| Current Inversion | Exact Call Sites | Target Resolution |
|---|---|---|
| `Core -> remote.richmsg` | `attention_signals.py` (L274, L352, L545), `handoff_package.py` (L324) | Promote `RichReader` and async receipt classification to Core (`corral/rich_conversation.py`); `remote/richmsg.py` becomes a thin remote wire-projection adapter. |
| `Remote -> ui/` | `remote/sessions.py` (L2524, L3232, L3268, L3286, L3322, L3825) importing `ui.session_hud` and `ui.terminal_prompt_navigation` | Move pure data helpers (`is_injected_user_prompt`, `summarize_user_messages`) and tmux prompt navigation (`return_to_latest`, `restore_composer`) into Core (`corral/prompt_navigation.py`, `corral/session_summary.py`); both TUI and Remote import Core. |
| `Control -> remote.sessions.SessionHub` | `control_worker.py` (L1347–1365) calling `SessionHub.host_planned_session` and verified input injection | Extract headless hosting/readiness/input primitives out of `remote/sessions.py` into Core (`corral/hosted_control.py` built directly on `embed.py` + `hosted_lifecycle.py`). Remote and Control become peer consumers. |

### 2.2 Break Mutual Imports & Extract Shared CLI Contract

Current 2-node mutual imports in `cli/src/corral/`:
- `agent_api <-> control_cli`
- `agent_api <-> workflow.cli`
- `control <-> models`
- `models <-> titles`
- `ui.controllers.host_controller <-> ui.main_screen`

Concrete fixes:
1. Extract `corral/cli_contract.py` from `agent_api.py` (2,398 lines) to own `_bin()`, JSON envelope builders (`ok()` / `err()`), exit code constants, TSV formatter helpers, and the `CommandSpec` schema. This breaks both `agent_api <-> control_cli` and `agent_api <-> workflow.cli`.
2. Move the lightweight `[from <ref> · peer, not user]` header regex (`parse_agent_header`) into Core (`corral/origin_markers.py` or `agent_session_control.correlation`), breaking `models.py`/`transcript.py -> control.py`.

### 2.3 Incremental Tech-Debt Cleanup (Trackable Alongside Refactor)

These are not hard blockers for Wave 1 but directly address repository bloat and maintainability:
1. **Retire SessKit-duplicated legacy parsers in `remote/richmsg.py` (3,246 lines):** Remove `_parse_codex_legacy` (L1393), `_parse_claude_legacy` (L1526), `_parse_cursor_legacy` (L1722), and `_parse_pi_legacy` (L2631) now that `sesskit>=0.3.16` covers all 6 active runtimes; keeps SessKit as the single history-parsing authority.
2. **Decompose oversized files incrementally:**
   - `remote/sessions.py` (4,897 lines) → `SessionCatalog`, `TerminalStreamManager`, `InputDispatcher`, `LayoutSync`.
   - `embed.py` (2,730 lines) → `tmux_lifecycle.py`, `control_channel.py`, `input_injector.py`, `ansi_parser.py`.
   - `i18n.py` (2,472 lines / 98 KB Python literal dict) → lazy-loaded JSON locale bundles (`corral/locales/{en,zh}.json`) with optional plugin locale overlay.
3. **Relocate non-product prototypes out of `cli/`:** Archive or move `cli/web/ideas/` and `cli/spikes/web_butler/` to separate repositories so `Corral/cli` reflects shipping product code only.

---

## 3. Plugin Technical Architecture Selection

### 3.1 Evaluated Alternatives

| Option | Reference Ecosystem | Strengths | Disqualifying Tradeoffs for Corral |
|---|---|---|---|
| **A. In-process `pluggy` / `entry_points` in host venv** | `pytest`, `llm`, `datasette` | Direct Python object sharing; low code churn | **`brew upgrade` wipes the Homebrew Cellar venv and deletes all plugins**; PEP 668 blocks `pip install`; Python-only; dependency conflicts break Core |
| **B. In-process external plugin dir (`sys.path` injection)** | Custom Python loaders | Survives `brew upgrade` | Homebrew Python minor upgrades (e.g. 3.12→3.13) break compiled extensions in plugins; shared dependency conflicts remain; Python-only |
| **C. Permanent JSON-RPC daemon per plugin** | HashiCorp `go-plugin`, LSP | Strong isolation + bidirectional streaming | Disproportionate process/socket lifecycle complexity and idle RAM cost for short-lived CLI commands (conflicts with 16 GB Mac memory budget) |
| **D. Selected: `gh`-style External Executable Plugins + Declarative `plugin.json` + Versioned Local Contracts** | `gh extensions`, `kubectl` + `krew`, VS Code declarative manifests, `corral flex` | Survives `brew upgrade`; polyglot; isolated venv/binary per plugin; < 1 ms cold dispatch; zero Core reverse imports | Requires defining 1 manifest schema + 3 lightweight local contracts (specified in §§4–5 below) |

### 3.2 Selected Architecture Overview

```text
                    corral <subcommand> [args]
                                │
                      corral/bootstrap.py
                                │
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
   Built-in Core          index.json lookup     PATH fallback
 (TUI, list/search,     (~/.config/corral/     (corral-<cmd>,
  remote, plugin, ...)   plugins/index.json)    e.g. flex)
                                │
                       os.execv(entrypoint)
                                │
              ┌─────────────────┴─────────────────┐
              ▼                                   ▼
   plugins/workflow/bin/corral-workflow    plugins/a2a/bin/corral-a2a
   (isolated .venv: sesskit + ASC)         (isolated .venv: sesskit + ASC)
              │                                   │
              └──────────┬────────────────────────┘
                         ▼
       Versioned Local Contracts (~/.config/corral/state/)
       • hidden-sessions.sqlite3  ◄── read by Core store.py / agent_api.py
       • host-leases.sqlite3      ◄── read by Core reclaim.py
       • remote-events.sock       ◄── served by Core remote daemon
```

**Why Python plugins stay tiny without depending on `corral`:**
Because `sesskit` and `agent-session-control` are already standalone packages, `corral-workflow` and `corral-a2a` only need those libraries plus Core's local state contracts (and optional lightweight `corral-host-sdk` if shared helpers are needed). Plugin virtualenvs **never** need to install `corral`, `textual`, or the Rust `_native` wheel.

---

## 4. Storage Layout, Manifest Schema & Dispatch Contract

### 4.1 Directory Layout

All plugin artifacts live under the standard XDG config directory (`$XDG_CONFIG_HOME/corral` or `~/.config/corral`), completely decoupled from Homebrew Cellar and Python `site-packages`:

```text
~/.config/corral/
├── plugins/
│   ├── index.json                      # Compiled atomic index read on hot path (mode 0600)
│   ├── workflow/
│   │   ├── plugin.json                 # Authoritative manifest (schema_version: 1)
│   │   ├── describe.json               # Pre-generated COMMANDS contract for `corral describe`
│   │   ├── bin/
│   │   │   └── corral-workflow         # Executable entrypoint (+x)
│   │   ├── .venv/                      # Isolated Python venv (only for Python plugins)
│   │   └── skills/
│   │       └── corral-workflow/
│   │           ├── SKILL.md
│   │           └── references/
│   └── a2a/
│       ├── plugin.json
│       ├── describe.json
│       ├── bin/corral-a2a
│       ├── .venv/
│       └── skills/corral-a2a/
└── state/
    ├── hidden-sessions.sqlite3         # §5.1 Session visibility contract (WAL, 0600)
    ├── host-leases.sqlite3             # §5.2 Reclaim protection contract (WAL, 0600)
    └── remote-events.sock              # §5.3 Local notification socket (0600, daemon-owned)
```

Existing user data directories remain untouched for backward compatibility:
- Workflow store: `~/.config/corral/workflows/workflows.sqlite3`
- Control/A2A store: `~/.config/corral/control/runs.sqlite3`

### 4.2 Manifest Schema (`plugin.json`)

```json
{
  "schema_version": 1,
  "name": "workflow",
  "version": "0.1.0",
  "description": "Multi-stage Python workflow orchestration, capability routing, and playbooks.",
  "core_api": ">=1.1,<2.0",
  "runtime": "python-venv",
  "commands": ["workflow", "wf"],
  "entrypoint": "bin/corral-workflow",
  "rpc_args": ["--rpc-stdio"],
  "describe_file": "describe.json",
  "skills_dir": "skills",
  "session_hide_prefixes": ["[corral-wf:"],
  "remote_namespaces": ["workflow."],
  "enabled": true,
  "source": {
    "type": "github-release",
    "repo": "corral-dev/corral-plugin-workflow",
    "tag": "v0.1.0",
    "sha256": "..."
  }
}
```

Validation & security invariants:
- `name`: lowercase `[a-z0-9][a-z0-9-]{0,31}`.
- `entrypoint`: relative path inside the plugin directory; must resolve strictly within `~/.config/corral/plugins/<name>/` (no `..` escape, no world-writable binary).
- `commands`: non-empty list of top-level subcommands (`[a-z][a-z0-9-]*`). Two enabled plugins cannot claim the same command; `plugin install` / `enable` rejects conflicts with exit code `5` (`conflict`).
- **Reserved Core Commands (immutable, plugins may never shadow):**
  - Core CLI & query roots: `list`, `search`, `show`, `export`, `share`, `context`, `plan`, `describe`, `diagnose`, `cache`, `remote`, `login`, `logout`, `whoami`, `shim`, `observer`, `tcc`, `update`, `skills`, `plugin`, `internal`.
  - Active runtime direct-launch roots: `claude`, `codex`, `opencode`, `cursor`, `pi`, `antigravity` (plus dormant `kimi`).

### 4.3 Compiled Index (`index.json`) & Hot-Path Dispatch

To avoid walking plugin directories on every CLI invocation, `corral plugin install/upgrade/remove/enable/disable/doctor` atomically writes `~/.config/corral/plugins/index.json`:

```json
{
  "schema_version": 1,
  "generated_at": "2026-11-27T06:00:00Z",
  "commands": {
    "workflow": "workflow",
    "wf": "workflow"
  },
  "session_hide_prefixes": ["[corral-wf:"],
  "remote_namespaces": {
    "workflow.": "workflow"
  },
  "plugins": {
    "workflow": {
      "version": "0.1.0",
      "core_api": ">=1.1,<2.0",
      "entrypoint": "/abs/path/to/bin/corral-workflow",
      "rpc_args": ["--rpc-stdio"],
      "describe_file": "/abs/path/to/describe.json",
      "skills_dir": "/abs/path/to/skills",
      "commands": ["workflow", "wf"],
      "enabled": true
    }
  }
}
```

Hot-path routing in `corral/bootstrap.py`:
1. Built-in Core commands match first (zero filesystem access to `plugins/`).
2. On an unrecognized top-level token `cmd`:
   - Read `index.json` (single file read, < 1 ms). If `index.json` is missing or corrupt while `plugins/*/plugin.json` exists, rebuild lazily in memory and emit an `observe.event("plugin_index_fallback")`.
   - If `cmd` is mapped to an enabled plugin, inject the execution environment (§4.4) and `os.execv(entrypoint, [entrypoint, cmd, *rest])` (on non-POSIX fallback: `subprocess.call` and propagate exit code).
   - Otherwise, check `shutil.which(f"corral-{cmd}")` on `PATH` (supports standalone binaries like `corral-flex` or user scripts without explicit registration).
   - If nothing matches, fall through to `corral.cli` (preserves existing error/help behavior, plus a helpful hint if `cmd` is a known official plugin such as `workflow` or `a2a`: `"Plugin 'workflow' is not installed. Run: corral plugin install workflow"`).

### 4.4 Execution Environment & Subprocess Contract

When Core invokes a plugin executable, it passes:
- `argv`: `[entrypoint, <matched_command>, *remaining_args]` so multi-alias plugins (`workflow` / `wf`) know how they were invoked.
- Environment variables:
  - `CORRAL_BIN`: absolute path or invocation name of the host `corral` executable
  - `CORRAL_CORE_VERSION`: e.g. `1.1.0`
  - `CORRAL_PLUGIN_NAME`: manifest `name`
  - `CORRAL_PLUGIN_DIR`: absolute path to `~/.config/corral/plugins/<name>`
  - `CORRAL_CONFIG_DIR`: `~/.config/corral` (respecting `XDG_CONFIG_HOME`)
  - `CORRAL_STATE_DIR`: `~/.config/corral/state`
  - `CORRAL_CACHE_DIR`: resolved Corral cache dir (respecting `CORRAL_CACHE_DIR`)
  - `CORRAL_ACTIVE_RUNTIMES`: comma-separated allowlist (`claude,codex,opencode,cursor,pi,antigravity`)
  - `CORRAL_REMOTE_SOCKET`: `~/.config/corral/state/remote-events.sock`
- Exit codes follow the shared `agent-friendly-cli` contract (`0` ok, `2` usage, `3` not found, `5` conflict/state, `6` timeout, `1` generic error) and JSON commands emit `{"ok", "data", "error", "meta"}`.

---

## 5. Core Extension Contracts (Zero Reverse Imports)

Every existing reverse import from Core into `workflow/` or `control*.py` maps to one of five small, versioned contracts.

### 5.1 Generic Session Visibility Contract

Replaces hardcoded `corral.workflow.visibility` imports in `store.py` (L1418) and `agent_api.py` (L713, L917).

- **Mechanism (dual-layer, fail-safe):**
  1. **Authoritative registry:** `~/.config/corral/state/hidden-sessions.sqlite3` (SQLite WAL, `busy_timeout=5000`, mode `0600`):
     ```sql
     CREATE TABLE IF NOT EXISTS hidden_sessions (
         session_key    TEXT PRIMARY KEY,   -- "<runtime>:<session_id>"
         keepalive_name TEXT,               -- optional hosted tmux name
         raw_id         TEXT,               -- optional raw runtime session id
         owner          TEXT NOT NULL,      -- plugin name, e.g. "workflow"
         group_id       TEXT,               -- e.g. wf_id
         created_at     REAL NOT NULL
     );
     CREATE INDEX IF NOT EXISTS idx_hidden_owner ON hidden_sessions(owner);
     CREATE INDEX IF NOT EXISTS idx_hidden_keepalive ON hidden_sessions(keepalive_name);
     ```
  2. **Cold-scan text markers:** Core reads `session_hide_prefixes` from `index.json` (plus built-in generic prefix `[corral-hidden:`) and checks `first_user_msg`, `title`, and `fallback_title` so hidden child sessions remain hidden even after database resets.
- **Core read rules:**
  - Read-only open (`?mode=ro`); if the file does not exist, return an empty set without creating directories or files.
  - Cache snapshot per scan cycle (never held stale across scans).
- **CLI compatibility:**
  - `corral list` and `corral search` expose `--include-hidden` (with `--include-workflow` retained as a deprecated compatibility alias) and return `"hidden_sessions": <int>` (plus `"hidden_workflow_sessions"` during the transition window if needed).
- **Plugin writes:** When `corral-workflow` registers a child session, it writes to both its own `workflows.sqlite3` and `hidden-sessions.sqlite3`.

### 5.2 Generic Host Lease Contract (Reclaim Protection)

Replaces `from corral.control_store import RunStore; delegated = RunStore().active_hosts()` in `reclaim.py` (L965–973).

- **Storage:** `~/.config/corral/state/host-leases.sqlite3` (SQLite WAL, `busy_timeout=5000`, mode `0600`):
  ```sql
  CREATE TABLE IF NOT EXISTS host_leases (
      owner          TEXT NOT NULL,      -- plugin name, e.g. "control", "workflow"
      lease_id       TEXT NOT NULL,      -- e.g. run_id or "<wf_id>:<step_id>"
      keepalive_name TEXT NOT NULL,      -- tmux session name to protect
      pid            INTEGER NOT NULL,   -- supervisor/owner process PID
      acquired_at    REAL NOT NULL,
      expires_at     REAL NOT NULL,      -- monotonic heartbeat deadline (Unix epoch)
      PRIMARY KEY (owner, lease_id)
  );
  CREATE INDEX IF NOT EXISTS idx_leases_keepalive ON host_leases(keepalive_name);
  ```
- **Reclaim semantics in `reclaim.py`:**
  - Missing DB file → empty active set (no plugins leasing hosts).
  - Unreadable/locked/corrupt DB file → treated as unreadable context source (`source="host_leases"`), which **blocks the reclaim pass** rather than risking live sessions (preserving existing fail-safe semantics in `reclaim.py`).
  - A lease is active when `expires_at > now` OR (`pid > 0` and local process `pid` is still alive and `now - expires_at < 60`, tolerating brief heartbeat jitter). Stale rows older than 24 h may be pruned only by writers, never during read-only probes.
- **Plugin write API:** Plugins write directly to `host-leases.sqlite3` using the documented schema (or call `corral internal lease acquire/renew/release` for non-SQLite languages).

### 5.3 Local Event Inbox for Remote Notifications

Replaces the 60-second polling loop in `remote/sessions.py` (L983–995, L1252–1254) that imported `corral.control_cli.a2a_chatter_alerts` and `corral.control_store.RunStore`.

- **Transport:** `corral remote` daemon opens a local Unix domain socket at `~/.config/corral/state/remote-events.sock` (mode `0600`). On macOS/Linux, the daemon validates peer UID == `os.getuid()` via `LOCAL_PEERCRED` / `SO_PEERCRED`.
- **Wire format:** Single newline-delimited JSON frame per event:
  ```json
  {
    "type": "plugin.alert",
    "plugin": "a2a",
    "dedup_key": "a2a:claude:abc->codex:def",
    "cooldown_seconds": 600,
    "title": "Agent chatter",
    "body": "claude:abc and codex:def exchanged 6 messages in 3m",
    "completion_id": "chatter:claude:abc->codex:def:1764222000"
  }
  ```
- **Behavior:** If `corral remote` is not running (socket absent or `ECONNREFUSED`), the plugin drops the push notification silently without failing the CLI operation. `PushNotifier` applies its existing dedup/cooldown/APNs pipeline generically for any `plugin.alert`.

### 5.4 On-Demand Remote RPC Bridge & Apple Client Capability Negotiation

Supports future client UI surfaces (such as the Mac-first Workflows visual flowchart in `CORRAL_WORKFLOW_DESIGN.md` §8) without bloating `remote/` or keeping idle plugin daemons in RAM:

- **Capability advertisement in `remote/service.py` `hello`:**
  ```json
  "capabilities": {
    "...existing core capabilities...",
    "plugins": {
      "workflow": {
        "version": "0.1.0",
        "namespaces": ["workflow."]
      }
    }
  }
  ```
- **Apple client behavior:** `CorralMac` and iOS inspect `hello.capabilities.plugins`. If `"workflow"` is absent, the Workflows navigation entry is hidden cleanly (no broken views, no error banners). Client-side plugin management (`install`/`enable`/`disable`/`remove`/`doctor`) is specified separately in **§11** (`CAPABILITY_PLUGIN_MANAGEMENT` + `plugin.*` RPCs).
- **Lazy RPC bridge in `remote/service.py`:**
  - When a paired client calls a method matching a registered `remote_namespaces` prefix (e.g. `workflow.list`, `workflow.graph`, `workflow.watch`), `remote/service.py` spawns `<entrypoint> --rpc-stdio` on demand and proxies JSON-RPC 2.0 frames over stdin/stdout (plus event push notifications back to subscribed clients).
  - **Memory governance:** If no client has active subscriptions or RPC traffic for **120 seconds**, `remote/service.py` sends `SIGTERM` (then `SIGKILL` after 5 s) to the bridge subprocess, keeping baseline daemon RSS unchanged when workflows are idle.

### 5.5 Aggregated `corral describe` & Per-Plugin Agent Skills

1. **`corral describe`:**
   - `corral describe` returns Core commands plus `"plugins": [{"name", "version", "commands", "description"}]` sourced from `index.json`.
   - `corral describe <cmd>` checks Core `COMMANDS` first, then looks up `index.json` and reads the plugin's `describe.json` (falling back to running `<entrypoint> describe --json` with a 3 s timeout if `describe.json` is missing).
2. **Per-plugin `skill_bundle.py`:**
   - Core's bundled `SKILL.md` (`corral`) shrinks to Core capabilities only (search/inspect/share/handoff).
   - Each plugin ships its own skill folder under `skills/` (e.g. `corral-workflow`, `corral-a2a`), tracked by owner in `~/.config/corral/skills/registrations.json`:
     - `corral plugin install <name>` installs the plugin's skill(s) into every installed agent runtime using the existing hash-protected ownership rules (never overwriting user-edited copies).
     - `corral plugin remove <name>` uninstalls unchanged copies belonging to that plugin.
     - `corral skills status/install/uninstall` operates across Core + all enabled plugins.

---

## 6. Plugin Lifecycle Management (`corral plugin …`)

All lifecycle commands follow `agent-friendly-cli` (`--json`, `--format tsv`, `--dry-run`, structured errors with `next_commands`):

| Command | Behavior |
|---|---|
| `corral plugin list [--json \| --format tsv]` | Lists installed plugins (`name`, `version`, `enabled`, `commands`, `runtime`, `status`) plus discovered `PATH` `corral-*` executables. |
| `corral plugin install <spec> [--version V] [--editable PATH] [--dry-run]` | Installs an official plugin by short name (`workflow`, `a2a`), GitHub Release URL/repo, or local checkout (`--editable` for development). Validates `core_api`, provisions isolated `.venv` (if `python-venv`), verifies SHA256, atomically activates, rebuilds `index.json`, and registers plugin skills. |
| `corral plugin upgrade [<name> \| --all]` | Upgrades plugin(s) in staging directories and swaps atomically; rolls back on verification failure. |
| `corral plugin remove <name> [--purge-state]` | Unregisters plugin skills, removes `~/.config/corral/plugins/<name>`, rebuilds `index.json`. Keeps user databases (`workflows.sqlite3`, `runs.sqlite3`) unless `--purge-state` is explicitly passed. |
| `corral plugin enable / disable <name>` | Toggles `enabled` in `plugin.json`, rebuilds `index.json`, and syncs skills without deleting binaries/venvs. |
| `corral plugin doctor [--fix]` | Checks manifest validity, `core_api` compatibility, entrypoint executability, `.venv` Python interpreter health (detects broken symlinks after OS/Homebrew Python upgrades and can recreate `.venv` with `--fix`), index consistency, and skill manifest integrity. |

### 6.1 Python Plugin Environment Provisioning

For `runtime: "python-venv"` plugins:
1. Prefer `uv venv` + `uv pip install` when `uv` is on `PATH` (fast, deterministic); fall back to `python3 -m venv` + `pip`.
2. Pin `sesskit` and `agent-session-control` inside the plugin's own wheel metadata / release bundle so installing a plugin never mutates Core's environment.
3. Because `brew upgrade` may bump Homebrew's Python minor version (breaking venv symlinks if the venv pointed into Cellar), `corral plugin install` resolves a stable Python interpreter path (`/opt/homebrew/opt/python@3.x/bin/python3` or `uv`-managed Python when available) and `corral plugin doctor --fix` repairs broken venvs in one command without losing plugin config or state.

---

## 7. Phased Migration Plan

### Wave 0 — Core Decoupling & Plugin Runtime Foundation
- Fix the 3 layer inversions (§2.1): `rich_conversation.py`, `prompt_navigation.py` / `session_summary.py`, `hosted_control.py`.
- Extract `cli_contract.py` and `origin_markers.py` (§2.2).
- Implement `corral/plugins.py` (manifest loader, `index.json` compiler, `bootstrap.py` dispatch, `corral plugin` CLI).
- Implement Core readers for `hidden-sessions.sqlite3` (§5.1) and `host-leases.sqlite3` (§5.2), plus `remote-events.sock` (§5.3) and `hello.capabilities.plugins` (§5.4).
- Extend `skill_bundle.py` for multi-owner plugin skills (§5.5).
- **Exit gate:** Full `ci-test.py` green; zero behavioral change to existing commands; synthetic sample plugin passes install/dispatch/describe/hide/lease/remove tests.

### Wave 1 — Extract `corral-workflow` (Pilot Plugin: 8,248 lines + 1,398 test lines)
- Move `cli/src/corral/workflow/` and `cli/tests/test_workflow.py` into the `workflow` plugin package.
- Replace the 4 thin `corral.*` imports (`ACTIVE_RUNTIME_IDS`, `default_registry`, `host_runtime_profiles`, `session_key`, `_bin`) with `agent_session_control` + `sesskit` + Core local contracts (`hidden-sessions.sqlite3`, `host-leases.sqlite3`).
- Ship `plugins/workflow/skills/corral-workflow/SKILL.md` (moved out of Core's `SKILL.md`) and generated `describe.json`.
- Remove `src/corral/workflow/` and hardcoded `workflow` branches from `bootstrap.py`, `store.py`, and `agent_api.py`.
- **Exit gate:** Core passes full suite with `workflow` uninstalled; installing `corral-workflow` passes all existing workflow tests and preserves `~/.config/corral/workflows/workflows.sqlite3`.

### Wave 2 — Extract `corral-a2a` (and `run` Delegation per Decision #1: 3,625 lines + 3,156 test lines)
- Move `peers`, `send`, `a2a *` (and `run *` if bundled into one `control` / `a2a` plugin) out of Core.
- Switch active-host protection to `host-leases.sqlite3` (§5.2) and chatter alerts to `remote-events.sock` (§5.3).
- Remove hardcoded `a2a_chatter_alerts` polling from `remote/sessions.py` and `RunStore` import from `reclaim.py`.
- If `run *` moves out with `a2a`, remove `agent-session-control` from Core `pyproject.toml`, `install.sh`, `scripts/control_dep.py`, and `homebrew_formula.py` (reducing Core install surface).
- **Exit gate:** Core passes full suite without the plugin; installing the plugin restores full `run`/`peers`/`send`/`a2a` parity and reads existing `~/.config/corral/control/runs.sqlite3`.

### Wave 3 — Unify `flex` & Ongoing Modularity
- Register `corral-flex` (`agentflex`) via the external executable path so `corral plugin list` shows it cleanly alongside managed plugins.
- Execute remaining non-blocking god-file splits (`remote/sessions.py`, `remote/richmsg.py` legacy cleanup, `embed.py`, `i18n.py`) as separate focused PRs.
- When Mac Workflows UI (Phase 2A) begins, wire it exclusively through the §5.4 `hello.capabilities.plugins.workflow` + on-demand RPC bridge.

---

## 8. Security, Performance & Compatibility Guarantees

| Dimension | Contract |
|---|---|
| **Cold-start latency** | Built-in Core commands read **zero** plugin files. Unknown commands read one `index.json` (< 1 ms) and `os.execv` directly. |
| **Memory budget** | No permanent plugin daemons. Optional remote RPC bridge (`--rpc-stdio`) spawns lazily on first client request and auto-exits after 120 s idle. |
| **Filesystem security** | `~/.config/corral/plugins/` and `state/` created with `0700`; manifests/indexes/SQLite/sockets `0600`. Entrypoints verified to reside inside their plugin directory and not be group/world-writable. |
| **Supply chain** | Official plugin releases verified by SHA256 before activation; staging + atomic swap prevents half-installed plugins. |
| **Data preservation** | Existing `workflows.sqlite3` and `runs.sqlite3` paths and schemas stay unchanged; `corral plugin remove` never deletes user data unless `--purge-state` is passed. |
| **Fail-safe degradation** | Corrupt `index.json` falls back to manifest scan; unreadable `host-leases.sqlite3` blocks silent reclaim; offline `remote-events.sock` never fails CLI commands. |

---

## 9. Verification & Acceptance Plan

Every wave requires automated + real-path verification before shipping:

1. **Core isolation & contract suite (`tests/test_plugins.py`):**
   - Manifest validation, path-traversal rejection, reserved-command shadowing rejection, command-collision rejection.
   - `bootstrap.py` dispatch preserving stdout/stderr, stdin TTY, signals, and exit codes.
   - `hidden-sessions.sqlite3` + prefix hiding across `SessionStore` (TUI), `agent_api list/search`, and `remote/sessions.py`.
   - `host-leases.sqlite3` protecting live hosted tmux sessions during `reclaim.py` passes, including fail-closed behavior on unreadable DB.
   - `remote-events.sock` UID verification and `PushNotifier` dedup delivery.
   - Per-plugin `skill_bundle.py` install/uninstall parity without overwriting user-modified skills.
2. **Cold-start & scan regression benchmarks:**
   - Measure `corral --version`, `corral list --limit 50`, and `corral describe` with 0 plugins vs 5 installed plugins; confirm no measurable regression on Core commands.
3. **Homebrew upgrade persistence simulation:**
   - Install Core into temp venv A → install `corral-workflow` into isolated `XDG_CONFIG_HOME` → delete venv A completely and install fresh Core into venv B → verify `corral workflow --help`, `corral describe workflow`, and existing `workflows.sqlite3` runs work without reinstalling plugins.
4. **Cross-end checks (TUI + Mac + iPhone):**
   - Verify workflow child sessions remain hidden in TUI sidebar, `CorralMac` sidebar, and iPhone list after Wave 1 extraction.
   - Verify `hello.capabilities.plugins` reflects installed/removed plugins accurately on live `corral remote` reload.

---

## 10. Adopted Owner Decisions (2026-11-27)

1. **Wave 2 scope (`run *` + `a2a *`) — Option 1A adopted:** Extract both single-session delegation (`run *`) and peer A2A (`peers`, `send`, `a2a *`) together into one control plugin (`corral-a2a` / `corral-control`), allowing `agent-session-control` to be removed from Core's mandatory dependencies, `install.sh`, and Homebrew formula.
2. **Official plugin repository topology — Option 2A adopted initially:** Host official plugins under `cli/plugins/<name>/` inside the `Corral/cli` repository (independent `pyproject.toml` and wheel/release artifacts, zero Python import coupling with `src/corral/`), keeping cross-contract iteration fast while remaining ready to split into separate repositories later.
3. **Upgrade experience — Option 3A adopted:** Pure opt-in; invoking an uninstalled plugin command (such as `corral workflow ...`) prints a structured error and exact `corral plugin install <name>` command while preserving existing `workflows.sqlite3` / `runs.sqlite3` files untouched.

### Delivery Status

- **Wave 0 (Core plugin runtime & contracts):** Implemented in `src/corral/cli_contract.py`, `src/corral/state_contracts.py`, `src/corral/plugins.py`, `bootstrap.py`, `store.py`, `reclaim.py`, `agent_api.py`, and `remote/service.py`. (`remote/sessions.py` and `skill_bundle.py` edits deferred to Wave 2 to avoid colliding with concurrent Pi identity work.)
- **Wave 1 (`corral-workflow` extraction):** Implemented in `cli/plugins/workflow/` (`corral-plugin-workflow`, zero `corral` import dependencies); `src/corral/workflow/` and `tests/test_workflow.py` removed from Core.
- **Wave 1B (Client plugin management UI & `plugin.*` remote protocol, §11):** Proposed (`remote/service.py` `plugin.*` RPCs, Mac `Settings` Plugins tab, iOS `HostPluginsView`, TUI `PluginsModal`).
- **Wave 2 (`corral-a2a` / `control`, `remote-events.sock`, multi-owner `skill_bundle.py`):** Pending.
- **Wave 3 (`flex` unification & remaining god-file splits):** Pending.

---

## 11. Client-Side Plugin Management UI & Remote Protocol

Clients (native Mac, iPhone/iPad, and TUI) must let users inspect, enable/disable, install, upgrade, repair, and remove plugins without dropping into a shell, while keeping plugin storage strictly owned by each development host.

### 11.1 Architectural Principles (Host-Scoped, Offline-First, Native Controls)

1. **Host-scoped ownership:** Plugins live on each paired development host (`~/.config/corral/plugins/`), never inside Apple app bundles. Multi-host users manage plugins per selected host.
2. **Offline-first frame 0 (Stale-While-Revalidate):** Mac and iPhone hydrate the last known plugin snapshot synchronously from per-host local cache (`HostPluginCache` under `~/.config/corral-mac/` on macOS, Application Support on iOS) on frame 0—never blocking launch or tab switching with full-screen spinners.
3. **Calm, self-healing state rules:** Never render transient background labels (`Syncing…`, `Saved`, momentary reconnect warnings) or redundant subtitles restating self-evident titles. Surface errors only when an operation fails or host connection cannot recover, always paired with a manual `Retry` action.
4. **Native platform surfaces only:**
   - **macOS (`CorralMac`):** 4th native `Settings`-scene toolbar tab (`TabView` + grouped `Form`, conforming to `~/Codes/_standards/workspace-docs/swift-docs/macos-settings-visual-spec.md`).
   - **iPhone / iPad:** Native grouped `Form` pushed via `NavigationLink` inside `HostSettingsView` (portrait-only on iPhone; adaptive on iPad / iPhone Duo).
   - **TUI:** Lightweight `PluginsModal` over the single-screen workspace (preserving the sidebar + split-pane architecture; no second top-level screen).

### 11.2 Remote Wire Protocol (`corral.v2` Encrypted Control Lane)

#### Capability & Channel Constants (`cli/src/corral/remote/protocol.py`)

- `CAPABILITY_PLUGIN_MANAGEMENT = "plugin_management"` (`bool` in `hello.capabilities`): indicates the host supports `plugin.*` management RPCs and `CH_PLUGINS` subscriptions, orthogonal to `hello.capabilities.plugins` (which lists enabled remote plugin namespaces such as `"workflow."`).
- `CH_PLUGINS = "plugins"`: multicast pub/sub channel delivering snapshot updates and async operation progress to every connected client.
- Live control-plane event `capabilities.changed`: emitted whenever a plugin is enabled, disabled, installed, upgraded, or removed so clients can show or hide plugin-gated UI (such as a future **Workflows** view) without reconnecting.
  ```json
  {
    "t": "evt",
    "c": "capabilities",
    "d": {
      "capabilities": {
        "plugins": {
          "workflow": { "version": "0.1.0", "namespaces": ["workflow."] }
        }
      }
    }
  }
  ```

#### RPC Methods

All requests share the existing `corral.v2` request/response envelope (`{"t":"req","id":...,"m":...,"p":...}`):

| Method | Request Params | Response / Async Behavior | Notes |
|---|---|---|---|
| `plugin.list` | `{}` | `{version, healthy, plugins, catalog, path_plugins}` | Fast read from `index.json` + `OFFICIAL_PLUGINS` metadata; never executes plugin binaries. |
| `plugin.watch` | `{}` | Subscribes `CH_PLUGINS` and returns initial `plugin.list` snapshot. | Mirrors `quotas.watch` lifecycle. |
| `plugin.unwatch` | `{}` | Unsubscribes `CH_PLUGINS`. | Called when closing Settings / leaving `HostPluginsView`. |
| `plugin.set_enabled` | `{name, enabled}` | `{plugin, capabilities_plugins}` | Atomic toggle (~5 ms); broadcasts `CH_PLUGINS` snapshot + `capabilities.changed`. |
| `plugin.install` | `{spec, version?}` | `{op_id, name, state: "running"}` | Runs async in background thread so venv creation never stalls the control lane; broadcasts stage progress on `CH_PLUGINS`, serialized per plugin name (`E_CONFLICT` on duplicate in-flight op). |
| `plugin.upgrade` | `{name?, all?}` | `{op_id, name?, state: "running"}` | Same async `op_id` lifecycle; rolls back atomically on failure. |
| `plugin.remove` | `{name, purge_state?: false}` | `{removed: name, capabilities_plugins}` | Removes plugin directory and rebuilds `index.json`; preserves SQLite user databases unless `purge_state: true` is explicitly confirmed. |
| `plugin.doctor` | `{fix?: false}` | `{healthy, checks, repaired}` | `fix: true` repairs broken `.venv` / `index.json` and broadcasts refreshed snapshot. |

#### Snapshot & Progress Payloads

```json
{
  "version": "1764223000.12",
  "healthy": true,
  "plugins": [
    {
      "name": "workflow",
      "version": "0.1.0",
      "description": "Multi-stage Python workflow orchestration, capability routing, and playbooks.",
      "enabled": true,
      "commands": ["workflow", "wf"],
      "runtime": "python-venv",
      "status": "ready",
      "issue": null,
      "update_available": null,
      "remote_namespaces": ["workflow."],
      "operation": null
    }
  ],
  "catalog": [
    {
      "name": "a2a",
      "latest_version": "0.1.0",
      "description": "Peer session messaging and multi-agent A2A guardrails.",
      "installed": false,
      "commands": ["peers", "send", "a2a"]
    }
  ],
  "path_plugins": [
    { "command": "flex", "entrypoint": "/opt/homebrew/bin/corral-flex" }
  ]
}
```

Async `plugin.install` / `plugin.upgrade` updates push `operation: {"op_id": "...", "kind": "install", "stage": "downloading" | "provisioning" | "verifying" | "ready" | "failed", "error": null | {"code", "message"}}` on `CH_PLUGINS`, followed by a final settled snapshot and `capabilities.changed` on success.

#### Remote Security Boundary

1. **Access level:** Read-only paired devices (`connection.access != "full"`) may only call `plugin.list`, `plugin.watch`, `plugin.unwatch`, and `plugin.doctor` with `fix: false`; all mutating RPCs return `E_FORBIDDEN`.
2. **Verified catalog default:** Over remote RPC, `plugin.install` only accepts known verified `OFFICIAL_PLUGINS` specs (SHA256-verified releases or configured local source when explicitly permitted). Installing arbitrary local paths or unverified URLs over remote is rejected by default (`CORRAL_REMOTE_ALLOW_UNVERIFIED_PLUGINS=0`) and remains available locally via CLI (`corral plugin install <path-or-url>`).

### 11.3 Shared Apple Data Layer (`apple/Shared/`)

Modeled directly on `Shared/UI/Quotas/HostQuotaFeed.swift` and `Shared/Core/Network/RemoteClient.swift`:

1. **`Shared/Core/Plugins/PluginModels.swift`:**
   - `Codable`, `Sendable`, `Equatable` models: `HostPluginItem`, `CatalogPluginItem`, `PathPluginItem`, `PluginOperationState`, `HostPluginsSnapshot`, and `HostPluginCache` (per-host JSON cache).
2. **`Shared/Core/Network/RemoteClient+Plugins.swift`:**
   - Extend `HostCapabilities` with `pluginManagement: Bool` and `plugins: [String: RemotePluginCapability]`.
   - Subscribe to `capabilities.changed` to update `hostCapabilities.plugins` live.
   - Typed async RPC helpers: `pluginsWatch()`, `pluginsUnwatch()`, `pluginsList()`, `pluginSetEnabled(name:enabled:)`, `pluginInstall(spec:version:)`, `pluginUpgrade(name:all:)`, `pluginRemove(name:purgeState:)`, `pluginDoctor(fix:)`.
3. **`Shared/UI/Plugins/HostPluginFeed.swift`:**
   - `@MainActor @Observable final class HostPluginFeed` (same lifecycle pattern as `HostQuotaFeed`):
     - `loadCacheIfNeeded()` hydrates frame 0 synchronously.
     - `activate(appModel:)` connects if needed, checks `hostCapabilities.pluginManagement` (`.hostUnsupported` when older CLI is connected), subscribes `WireProtocol.chPlugins`, and fetches initial `pluginsWatch()`.
     - Optimistic `setEnabled(_:enabled:)` updates local UI immediately and rolls back with inline error only if the RPC fails.
     - Tracks active `op_id` per plugin so concurrent installs/upgrades stay isolated per row.

### 11.4 Native macOS Settings Tab (`apple/macOS/Settings/MacSettingsView.swift`)

Follows `macos-settings-visual-spec.md` strictly:

- **Tab entry:** Add `case plugins` to `MacSettingsView.Pane` (`@AppStorage("corral.mac.settingsPane")`):
  `Tab("settings.plugins", systemImage: "puzzlepiece.extension", value: Pane.plugins)`.
- **Window geometry:** Shares fixed width `480 pt` and `.fixedSize(horizontal: false, vertical: true)` with sibling tabs; outer `Form` uses `.formStyle(.grouped)` and `.toggleStyle(.switch)` with no outer scrollbar (if installed + catalog rows exceed max height, only the plugin collection scrolls cleanly).
- **Multi-host selector:** Shown at top of the pane **only when `appModel.hosts.count > 1`** (omitted for single-host setups to avoid dead interaction cost); defaults to the currently connected host.
- **Installed section:** Equal-height `LabeledContent` rows:
  - Leading: plugin title (`Text`) + secondary version (`caption`), plus an inline `exclamationmark.triangle.fill` badge and `Repair` action **only** when `status != "ready"`. Supplementary description sits in `.help(...)`, never a multi-line wall of text.
  - Trailing: native `.switch` `Toggle` + compact `Menu` (`ellipsis.circle`) with `Upgrade` (shown only when `update_available != nil`), `Commands` (inspects command aliases), and destructive `Remove…` (native confirmation dialog with an explicit unchecked opt-in `Toggle` for *"Also delete plugin data"* / `purge_state`).
- **Available section** (hidden when all catalog plugins are already installed):
  - Leading: plugin title + concise 1-line summary.
  - Trailing: native `Install` button (switches inline to a small `ProgressView()` while `operation?.isRunning == true`, never disabling unrelated rows or tabs).
- **Empty / unsupported / error states:** Older host without `plugin_management` shows a single-line hint to update `corral` on that machine; unrecovered failure shows error text + manual `Retry` button. Zero orphan empty-label button rows.

### 11.5 Native iOS / iPad Page (`apple/iOS/Views/Hosts/`)

1. **Entry in `HostSettingsView.swift`:**
   - Add a `NavigationLink` row (`Label("settings.plugins", systemImage: "puzzlepiece.extension")`) with trailing count summary hydrated from `HostPluginCache` on frame 0 (hidden gracefully when `hostCapabilities` explicitly reports `pluginManagement == false` after handshake).
2. **`HostPluginsView.swift`:**
   - Native grouped `Form` with `.navigationTitle("settings.plugins")` and `.refreshable` pull-to-refresh.
   - Portrait-only on iPhone; adaptive push navigation on iPad / iPhone Duo.
   - **Installed section:** Native `Toggle` switches (`HapticFeedback.shared.emit(.committed, scope: hapticScope)` on confirmed change), inline `Repair` button when unhealthy, swipe/menu `Remove…` confirmation dialog (preserving SQLite state unless user toggles purge).
   - **Available section:** `Install` button transitioning to inline `ProgressView` during active `op_id`.
   - **Localization:** All new UI copy uses semantic keys in `apple/Shared/Resources/Localizable.xcstrings` (`en` + `zh-Hans`), validated by `scripts/check_localizations.py`.

### 11.6 TUI Management Modal (`cli/src/corral/ui/plugins_modal.py`)

Preserves Corral's single-screen TUI architecture (left sidebar + right split panes; no second top-level screen):

- Opened from the footer/command bar or shortcut (e.g. `P` / top menu item) as a dismissible `ModalScreen` (`Esc` closes immediately), modeled on `ui/search_modal.py`.
- Displays **Installed** and **Available** plugins directly from `corral.plugins.list_plugins()` + `OFFICIAL_PLUGINS`:
  - `Space` / `Enter`: toggle `enable` / `disable`
  - `i`: install selected catalog plugin
  - `u`: upgrade selected plugin
  - `x`: remove (with inline `[y/N]` confirmation; keeps state by default)
  - `d`: run `doctor --fix`
- Runs mutating `install` / `upgrade` / `doctor` calls in a background Textual worker (`@work(thread=True)`) so live embedded tmux panes keep streaming without UI stalls.

### 11.7 Cross-End Acceptance Checklist (Wave 1B)

1. **Host daemon (`cli/tests/test_remote_plugins.py`):**
   - `plugin.list` / `plugin.watch` / `plugin.set_enabled` / async `plugin.install` / `plugin.remove` / `plugin.doctor` over encrypted `RemoteService` frames.
   - `capabilities.changed` multicast delivery on toggle/install/remove.
   - `E_FORBIDDEN` for read-only device access; rejection of unverified non-catalog remote install specs.
2. **macOS (`CorralMac`):**
   - `macos-window-screenshot` captures of `MacSettingsView` `Plugins` tab in Dark/EN, Light/ZH, and 100%/150% zoom: scroll-free outer form, aligned trailing switches, zero stray focus rings.
   - Live toggle updates `hello.capabilities.plugins` without reconnect.
3. **iOS / iPad:**
   - `python3 scripts/check_localizations.py` passes with zero missing keys.
   - `ios-sim` / `iPhone Max` (plus AppShelf upload fallback) verification of `HostSettingsView` → `HostPluginsView`: frame-0 cached render, pull-to-refresh, toggle/install/remove, and haptic feedback.
4. **TUI:**
   - Isolated Textual Pilot test opening `PluginsModal`, toggling/listing plugins, and closing with `Esc` without interrupting active embedded panes.
