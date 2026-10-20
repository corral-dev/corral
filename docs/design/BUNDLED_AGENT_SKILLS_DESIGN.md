# Bundled agent skills

Status: implemented product requirements, 2026-10-20.
The packaged CLI and bundled registration share the command authority in `corral describe`.
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
and other tools' files. Do not register an unfinished delegation workflow.

## Paths

Owner correction (2026-10-20): **Corral's skill installation is unrelated to
AgentSync.** Corral does not read AgentSync configuration, canonical sources,
filters or policies, does not call it, and has no AgentSync-specific branch.
AgentSync was only a reference for where each runtime reads user skills. A skill
root is an ordinary directory to Corral, whoever else may also write into it.

| Runtime | User skill root Corral writes |
|---|---|
| Claude Code | `~/.claude/skills/` (`CLAUDE_CONFIG_DIR/skills` when set) |
| Codex | `~/.agents/skills/` when that directory exists (current documented user location, also read by Pi); otherwise `$CODEX_HOME/skills` (default `~/.codex/skills/`) |
| OpenCode | `$XDG_CONFIG_HOME/opencode/skills/` (default `~/.config/opencode/skills/`) |
| Cursor | `~/.cursor/skills/` |
| Pi | `~/.agents/skills/` when it exists; otherwise `$PI_CODING_AGENT_DIR/skills` (default `~/.pi/agent/skills/`) |

Rules:

- Only runtimes whose executable Corral's adapter finds are targeted; Corral never
  creates a home directory for an absent agent. A missing `skills` subdirectory
  inside an existing runtime home is created.
- Roots are resolved (`realpath`) and deduplicated, so one physical directory
  reached through several runtime roots or symlinks gets exactly one copy; this
  avoids duplicate-name discovery (Codex lists both copies of a duplicated name).
- Each skill is written as a real directory (no symlinks, no absolute home paths
  in content), so it stays valid wherever the directory is copied.
- Corral manages only `corral-search` and `corral-delegate`. Ownership is proven by
  the content hash recorded in `~/.config/corral/skills/registrations.json`; an
  existing same-name directory with unknown content is a conflict that is
  reported, never overwritten. Uninstall removes only unchanged owned copies.

Paths are defaults, not proof of discovery in every client/version. Upstream
references: [Claude](https://code.claude.com/docs/en/skills),
[Codex](https://learn.chatgpt.com/docs/build-skills),
[OpenCode](https://opencode.ai/docs/skills/),
[Cursor](https://cursor.com/docs/skills), and
[Pi](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/skills.md).

## Package and installer contract

Asset source: `src/corral/skills/<name>/SKILL.md`, included in source
archives and wheels. Use packaged resources, not a live download of mutable skill
text from a separate branch. A bundle manifest records names, content hashes,
bundle version and required CLI capabilities. Derive command schemas/examples
from the maintained CLI contract where possible; never duplicate parser logic.

Expose `corral skills status`, `install --dry-run`, `install` and
`uninstall` operations with the same envelope, structured conflicts and exact
targets as the agent CLI. These commands are implemented.
Status and dry-run must not create roots, state, locks, caches or registrations.
Return per-runtime availability, effective and resolved roots, owner, installed
bundle/hash, conflicts and discovery evidence. File presence and a
runtime actually loading the skill are separate verification results.

Default lifecycle:

1. The official user-level installer installs/verifies the CLI, then invokes its
   bundled registration command using the exact installed executable. Development
   installation follows the same bounded entry. Report per-runtime failures
   accurately; installing the binary alone is not successful skill registration.
2. An interactive startup provides a safe, bounded registration/update fallback
   for package managers without a user-level install hook. Keep query/describe/
   version/help/status/dry-run paths read-only and JSON stdout clean. Provide an
   opt-out for automatic registration; explicit install/status remains available.
3. Reconcile bundle changes and newly installed runtimes on the next eligible
   invocation. Avoid per-command directory sweeps. Serialize concurrent changes
   and retain evidence of incomplete registration so it can resume.

### Registration timing

| Entry or installation channel | Registration/update behavior |
|---|---|
| Official `install.sh` and `scripts/dev-install.sh` | Invoke `skills install` through the exact installed executable at the end; inspect per-runtime results |
| Homebrew, raw pip or pipx package installation/upgrade | Unpack bundled assets; registration is reconciled on the next eligible interactive Corral startup, with both stdin and stdout attached to a TTY |
| Top-level version/help, agent query/describe/status/dry-run commands and `update` | No automatic registration; these entries do not count as that interactive startup |
| `corral skills status` | Read-only registration/conflict inspection |
| `corral skills install` | Explicit registration/repair using the installed bundle; respects ownership conflicts |
| `CORRAL_SKILLS_AUTO=0` | Disable automatic startup registration; explicit install/status remain available |

Only detected installed runtimes are targeted. A successful package upgrade alone
therefore does not establish updated registrations through every channel. Open a
fresh agent session to verify discovery of the updated descriptions; an already
running agent is not evidence that new metadata has loaded.

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
aliases to one directory, custom same-name content, edited owned
content, upgrades/uninstall, interrupted/concurrent registration, read-only files,
and no-write dry-run/query checks. Inspect the actual packaged wheel and installed
resources. Verify discovery in a fresh runtime session; copying a valid file is
not that acceptance. Exercise both delegation paths through skills with real
returned values and the controller's full acceptance matrix. A separate worker
evaluation requires explicit authorization; direct evaluation must state its scope.


## Ownership and recovery corrections (2026-10-20)

An identical SKILL.md without a prior registration is a name conflict, not proof
of ownership. Never adopt or delete that directory. Managed updates replace only
the registered SKILL.md, preserving additional user files. Uninstall deletes only
an unchanged registered file and removes the directory only if empty; per-skill
and file symlinks are conflicts. Serialize registration changes, journal intended
hashes before writes, and reconcile interrupted writes by those hashes.

Official and development installation invoke registration through the exact
installed binary. Interactive startup is the bounded fallback; version/help/query/
status/dry-run remain read-only. Newly installed runtimes and incomplete attempts
are reconciled on later eligible starts, including when the bundle version did
not change.


## Current description wording (owner correction, 2026-10-20)

Keep each description to a concise statement of its core capability: searching and
reading other agents' local conversations, or assigning tasks and monitoring their
progress, questions and results. Keep detailed workflow scenarios and boundaries
in the skill body instead of extending the frontmatter into a trigger checklist.
The earlier selection measurements below apply to their recorded descriptions;
they do not establish the simplified wording's selection reliability.

## Earlier implicit selection requirements (2026-10-20)

The owner approved strengthening the descriptions and evaluating automatic
selection without naming Corral or either skill in the task. Search must cover
review/bug triage where the original development context can prevent expensive
rediscovery, and recovery of prior implementation decisions. Delegation must
cover returning a contextual repair to its original session, starting a fresh
agent for an independent task, and supervising work already handed off.
A caller need not already have decided to reuse an agent before discovering
these capabilities. Small self-contained changes, general explanations and
ordinary Git/source searches must not acquire an unnecessary delegation step.

Description selection does not authorize actions outside the user's task or
turn every bug into mandatory delegation. Keep the existing exact-target,
idempotency, uncertainty and acceptance boundaries in the body. Preserve generic
five-runtime portability; do not add a vendor-specific invocation requirement.

The shared [skill selection/evidence method](~/.config/agentsync/docs/SKILLS_GUIDE.md#321-implicit-selection-and-execution-evidence)
owns evaluation design and claim limits. Prior Corral repair acceptance explicitly
named the tool and skills, so it established operational usability only. Corral
additionally requires unnamed-tool original-owner and fresh-task workflows, with
isolated fixtures and bounded execution. The observations below complement the
CLI suite; they do not establish all-model reliability.

Reproduce selection checkpoints with `python3 scripts/evaluate_skill_triggers.py`.
The default only validates/prints the plan and makes no writes or model calls.
Opt in with `--live --artifacts <new-directory> [--model <configured-model>]`;
use `--extension <provider-extension>` only when that provider needs an explicit
extension. Automatic extension/MCP/context discovery is disabled so unrelated
agent orchestration cannot participate. Pi's own skill loader supplies the names
and descriptions from the two packaged skills; no description is pasted into the
business prompt. The helper requires successful native assistant completion and
successful SKILL.md read results for expected selections. It rejects native
quota/auth errors even when Pi exits zero.

<!-- 该文档整理/压缩于 2026-10-20 -->


### Selection and execution evidence

Pi 1.3.0 on `codex-1/gpt-6.1-sol` evaluated 20 fresh read-only contexts against
the previous descriptions and 20 against the revised descriptions. Each group
passed all 10 positive and 10 negative cases with actual successful skill reads
as the trigger evidence. All native assistant completions were checked for errors.
The previous wording also passed this sample: this is coverage evidence, not a
measured trigger-rate improvement. The corpus includes Chinese/English requests,
review-context routing, prior decisions/commands/errors, fresh workers and active
supervision, with ordinary edits/source/Git queries as negatives.

The contexts exposed only the two candidate skills through Pi's native skill
loader and disabled unrelated extensions/MCP/context files. The business prompts
named neither Corral nor a skill. This controlled selection test does not establish
reliability across every skill catalog, model or runtime, nor a statistical
trigger rate from one run per case. Additional independently worded cases and
repeated native runs are the appropriate next measurement when usage exposes gaps.

A separate execution probe gave only the billing defect and the value of the
original developer's context. The fresh reviewer chose both skills, searched and
inspected the stopped owner `pi:fe0dfd88`, submitted run
`run_e69728c3670c4534b78f3e2ec77551ba`, waited for its result, and independently
passed the two required assertions plus five regression tests. The owner made
the repair; the reviewer did not replace the owner with a local implementation.
The initial task did not name Corral, a skill, a session reference or CLI commands.

The default evaluator plan and both skill frontmatters validate. The final
2677-test/Ruff gate passed in 107.6 seconds with two environment skips, no
failures or retries; the unchanged product source/environment stamp was checked
separately. Background workflow receipts report documentation drift from the
concurrent documentation pass; they do not verify those later document edits.
Source and test files remained unchanged throughout the final gate. Navigation,
links and current wording were reread separately.

The complete diagnostic log retains existing Textual asyncio timing notices.
They originate in the unchanged UI fixture/dependency lane, whose assertions all
passed. Each observed origin is assessed below; no diagnostic or threshold was
suppressed. These notices do not prove production latency. Timing under runner
contention remains a test-environment limitation; investigate independently if a
repeated user-visible stall accompanies an origin.

| Coroutine origin | Count | Seconds | Assessment |
|---|---:|---:|---|
| `App.run_test.<locals>.run_app` | 1 | 0.111–0.111 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `AppThemeTests.test_clicking_close_on_unfocused_pane_does_not_switch_to_it` | 1 | 0.107–0.107 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `AppThemeTests.test_closing_focused_split_pane_stays_on_remaining_not_closed` | 1 | 0.102–0.102 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `AppThemeTests.test_closing_middle_focused_pane_keeps_left_neighbor` | 1 | 0.151–0.151 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `AppThemeTests.test_closing_one_split_pane_keeps_sibling_widget` | 1 | 0.108–0.108 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `AppThemeTests.test_closing_unfocused_split_pane_keeps_remaining_split` | 1 | 0.178–0.178 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `AppThemeTests.test_focusing_split_pane_highlights_matching_sidebar_session` | 1 | 0.108–0.108 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `AppThemeTests.test_group_pane_close_preserves_sidebar_scroll` | 5 | 0.122–0.181 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `AppThemeTests.test_pending_selection_follow_after_close_does_not_open_closed_session` | 1 | 0.119–0.119 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `AppThemeTests.test_pi_provisional_refresh_keeps_split_group_without_duplicate` | 1 | 0.104–0.104 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `AppThemeTests.test_reconcile_split_keys_after_provisional_becomes_real` | 1 | 0.114–0.114 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `AppThemeTests.test_stale_focus_notify_after_close_is_ignored` | 1 | 0.114–0.114 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `MessagePump._process_messages` | 59 | 0.101–0.260 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `SessionHudPlacementTests.test_every_live_pane_draws_its_own_hud` | 1 | 0.132–0.132 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `SessionHudPlacementTests.test_hud_background_follows_pane_chrome` | 1 | 0.151–0.151 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `SidebarResizeTests.test_narrow_screen_caps_width_for_right_pane` | 1 | 0.128–0.128 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `SidebarSplitHighlightTests.test_selecting_group_card_highlights_whole_group` | 1 | 0.180–0.180 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `Timer._run_timer` | 10 | 0.109–0.132 | Existing fixture/dependency scheduling; assertions passed; retain notice. |
| `UpdateCheckWorkerTests.test_staged_spec_reaches_restart_request` | 1 | 0.129–0.129 | Existing fixture/dependency scheduling; assertions passed; retain notice. |

The fresh independent-task probe also selected `corral-delegate` without a tool
name in its task, created `pi:11b899f0` on the requested model, and produced its
report. This uncovered the trailing-marker/400-character correlation defect
recorded in the [delegation authority](AGENT_SESSION_DELEGATION_DESIGN.md#long-brief-correlation-after-trailing-markers-2026-10-20).
After fixing the shared matcher, explicit monitoring recovery settled the same
native turn as completed/correlated with model evidence, without sending the
brief again. Regression coverage includes a long Chinese brief, a quoted other
run marker before the actual trailing marker, and legacy prefix compatibility.


The caller's independent fresh-session probe finished successfully, confirmed
the report exists and returned the correlated result after recovery. Native
transcripts show the reviewer selected search/delegation and the fresh-task
caller selected delegation through successful skill reads before CLI execution.
The private acceptance root retains native JSONL, CLI receipts, report and
assertion output; no real user session was edited.


### Installed delivery (v0.24.340)

The release packages were built from committed `cd38c87`; all six downloaded
packages match `SHA256SUMS` and the built bytes. The five platform wheels and
source archive are publicly available. Both local pipx and Homebrew installations
report 0.24.340 with current registered skills. Installed skill files match the
packaged/source hashes; the registered-copy positive and negative checkpoints
both passed.

A further native-discovery check used Pi's normal 20-skill catalog without
`--skill` or `--no-skills`: the unnamed review task read both search/delegation,
the independent-task request read delegation, and the spelling edit read neither.
All three completed on GPT-6.1 Sol without native errors. This adds normal-catalog
evidence to the controlled corpus; it still does not guarantee all-model behavior.

The installed CLI created and completed a new 1308-character brief as
`run_2058f1f6cbd04e678758553698caf908`, returning the exact expected response
with `native_recorded` delivery and correlated attribution, without recovery or
retyping. The Homebrew installation retrieved that same result. The isolated
Linux x86_64 wheel imported its native extension and correctly matched a long
trailing marker. All task-created tmux servers/sockets were stopped/removed;
native histories, reports and receipts remain for inspection.

macOS build and clean installation emitted no warnings. Linux Zig builds retain
the two previously assessed musl compiler-family probes for
`x86_64-linux-musl-gcc` and `aarch64-linux-musl-gcc`: the explicit Zig builds
succeeded, but musl/ARM execution was not measured here. This is the same build
environment limitation recorded in the delegation authority. Homebrew's
unrelated tap-trust advisory remains unchanged. A transient HTTP/2 clone failure
interrupted automatic tap closeout; the maintained formula generator completed
through HTTP/1.1, and the live tap plus local upgrade were verified separately.

Release: <https://github.com/corral-dev/corral/releases/tag/v0.24.340>.
