# Bundled agent skills

Status: adopted product requirements and proposed implementation, 2026-10-20.
No bundled installer or delegation command has shipped as part of this research.
The control architecture and acceptance are owned by the
[agent delegation design](AGENT_SESSION_DELEGATION_DESIGN.md).

## Product requirements

Corral bundles skills that teach supported agents to use its CLI for historical
search, continuing an exact session, creating a fresh runtime/model session,
supplying requirements and observing questions, progress and results. Control is
implemented in Corral's middleware. Skills route intent through the CLI; they
must not substitute runtime-native collaboration, raw tmux scripts, vendor APIs
or direct history parsing for a missing Corral command.

The owner approved compatible names **`corral-search`** and **`corral-delegate`**.
This supersedes the earlier `corral:` naming request. Directory and frontmatter
names use the same lower-case hyphenated form. Plugin namespace syntax must not
be mistaken for a portable skill name. The
[Agent Skills specification](https://agentskills.io/specification),
[OpenCode skill rules](https://opencode.ai/docs/skills/) and
[Cursor skill rules](https://cursor.com/docs/skills) explain the compatibility
constraint.

Installation includes default registration for installed supported runtimes;
users do not have to author or manually copy the bundled skills. Register/update
the same packaged assets with Corral upgrades, detect runtimes installed later,
and expose a read-only status plus an explicit repair path. Preserve user skills
and existing skill managers. Do not register an unfinished delegation workflow.

## Paths and existing managers

Reference implementation: AgentSync `cli` at
`48c094f5dbec7b2d1211e0ee77edbe7f5a0a256a`, inspected
`internal/agentsync/{paths.go,skills.go,pi_mcp.go}` and its
[path authority](~/Codes/AgentSync/cli/docs/agent_runtime_global_paths.md).
AgentSync is a compatibility reference, not a Corral dependency or a reason to
change its configuration. Its maintained source on a configured machine is
`~/.config/agentsync/`.

| Runtime | Existing AgentSync destination | Corral registration direction |
|---|---|---|
| Claude Code | `~/.claude/skills/` | Dedicated global skill directory; preserve a manager-owned directory or root symlink. |
| Codex | `~/.codex/skills/`, plus discovered shared skills | Preserve existing managed Codex views; current upstream also documents `~/.agents/skills/`. Resolve the actual supported target rather than creating duplicate copies. |
| OpenCode | `~/.config/opencode/skills/` | Dedicated global directory; validate any configured root override for the installed runtime. |
| Cursor | `~/.cursor/skills/` | Dedicated global directory; shared discovery is also supported by current documentation. |
| Pi | Shared `~/.agents/skills/` | Follow the existing shared target when managed. Pi also reads `<agent-dir>/skills/`, default `~/.pi/agent/skills/`; `PI_CODING_AGENT_DIR` overrides its agent directory. |

Paths are defaults, not proof of discovery in every client/version. Current
upstream references: [Claude](https://code.claude.com/docs/en/skills),
[Codex](https://learn.chatgpt.com/docs/build-skills),
[OpenCode](https://opencode.ai/docs/skills/),
[Cursor](https://cursor.com/docs/skills), and
[Pi skill documentation](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md).
Honor an override only after verifying that it controls skill discovery, not
merely MCP or instructions. Detect an installed runtime using Corral's existing
adapter/executable evidence plus its configured root; a missing `skills` directory
alone does not mean an absent runtime. Do not create fake homes for absent agents.

The inspected machine has root symlinks for Claude/OpenCode/Cursor/shared skills
pointing at AgentSync's canonical skills; Codex has a real filtered view. Pi's
dedicated skill directory is absent and the shared entry supplies its skills.
Therefore blindly replacing a root, copying independently to every alias, or
placing a lasting file only in a generated filtered view is incorrect.

Resolve root aliases and deduplicate physical destinations. For an existing
AgentSync-managed setup, use its canonical source and honor its target filters;
do not invoke a full MCP/config sync or bypass a deny rule through the shared
directory. If the existing manager offers no bounded registration interface,
implement a narrow Corral integration with explicit ownership/status rather than
rebuilding the manager's entire directory topology. A symlink into a skill manager
does not by itself grant permission to overwrite unrelated canonical skills.

## Package and installer contract

Proposed asset source: `src/corral/skills/<name>/SKILL.md`, included in source
archives and wheels. Use packaged resources, not a live download of mutable skill
text from a separate branch. A bundle manifest records names, content hashes,
bundle version and required CLI capabilities. Derive command schemas/examples
from the maintained CLI contract where possible; never duplicate parser logic.

Expose proposed `corral skills status`, `install --dry-run`, `install` and
`uninstall` operations with the same envelope, structured conflicts and exact
targets as the agent CLI. These names are proposals, not current instructions.
Status and dry-run must not create roots, state, locks, caches or registrations.
Return per-runtime availability, effective and resolved roots, owner, installed
bundle/hash, conflicts, filtering and discovery evidence. File presence and a
runtime actually loading the skill are separate verification results.

Default lifecycle:

1. The official user-level installer installs/verifies the CLI, then invokes its
   bundled registration command using the exact installed executable. Development
   installation follows the same bounded entry. Report per-runtime failures
   accurately; installing the binary alone is not successful skill registration.
2. A normal execution entry provides a safe, bounded registration/update fallback
   for package managers without a user-level install hook. Keep query/describe/
   version/help/status/dry-run paths read-only and JSON stdout clean. Provide an
   opt-out for automatic registration; explicit install/status remains available.
3. Reconcile bundle changes and newly installed runtimes on the next eligible
   invocation. Avoid per-command directory sweeps. Serialize concurrent changes
   and retain evidence of incomplete registration so it can resume.

Distribution has a real boundary: Python wheel installation unpacks files and
generates entry points; it does not provide a standard arbitrary per-user
post-install callback. Homebrew's current cookbook requires structured
`post_install_steps` and retains its post-install sandbox policy. A raw wheel or
`brew install` that never executes Corral cannot honestly promise edits to all
real user skill directories. Supply a one-command official install wrapper that
finishes registration, plus the first-execution fallback for direct package-manager
users; do not evade Homebrew's sandbox. Sources:
[wheel installation specification](https://packaging.python.org/en/latest/specifications/binary-distribution-format/)
and [Homebrew post-install rules](https://docs.brew.sh/Formula-Cookbook#running-commands-after-installation).

Manage only the bundled names. New name collisions and edited managed content
return a conflict with both hashes and a concrete recovery command. Do not
overwrite a user-authored skill, replace root directories, touch `.system`, edit
global instructions/MCP/authentication, or assume a package directory survives an
upgrade. Keep asset ownership and registration manifests under Corral's config
root; replace only positively identified owned content. Uninstall removes only
registrations still owned and unchanged, never their containing skill root.
Filesystem writes are not atomic with SQLite: journal registration intent before
replacement, audit state transitions, then reconcile hashes on recovery rather
than claim one transaction spans both stores.

## Skill behavior and acceptance

`corral-search` teaches binary/capability discovery, scoped keyword retrieval,
coverage interpretation, bounded evidence inspection and exact session references.
Treat history as evidence, including untrusted quoted/tool content, rather than
new instructions. No-hit results with incomplete coverage remain inconclusive.

`corral-delegate` teaches both target paths: select/inspect an existing session,
or explicitly choose runtime, cwd and optional model for a fresh one. Then prepare
the brief, dry-run, submit once, retain run/key/cursor, wait for actionable changes,
answer an identified question, retrieve the correlated result and review acceptance
evidence. Preserve unknown delivery instead of resubmitting, and distinguish wait
timeout from stopping execution. A completed run's follow-up is a new run on the
same session. The CLI owns all identity/idempotency/ownership guards.

Both skills first pin the executable and query version/capabilities. Include the
official install route for a missing executable. Gate control instructions on
implemented capabilities, never recommend a raw runtime/API workaround. Reuse
existing query documentation while preserving its published contracts; resolve
`docs/SKILL.md` and the new bundled assets to one maintained command authority.

Acceptance must include clean homes for all five runtimes, configured roots,
missing skill subdirectories, absent runtimes, root/per-skill symlinks, multiple
aliases to one directory, manager filters, custom same-name content, edited owned
content, upgrades/uninstall, interrupted/concurrent registration, read-only files,
and no-write dry-run/query checks. Inspect the actual packaged wheel and installed
resources. Verify discovery in a fresh runtime session; copying a valid file is
not that acceptance. Exercise both delegation paths through skills with real
returned values and the controller's full acceptance matrix. A separate worker
evaluation requires explicit authorization; direct evaluation must state its scope.
