# Agent workflow

## Requirements (approved 2026-10-06)

Corral development should start from discoverable, task-specific entry points. Reuse `dev_env.py`, `ci-test.py` and its source/environment stamp, `acceptance.py`, Apple `client_diag.py`, and `ios-deliver`; do not introduce a second product implementation or test-pass cache.

Use a dependency-free maintainer workflow entry for read-only readiness, process-owned exclusive resources, durable background execution, verified sequential delivery steps and task checkpoints. Runtime state and full logs belong under `XDG_CONFIG_HOME/corral/development` (default `~/.config/corral/development`), isolated by checkout and task. Never store secrets in plans, argv or checkpoints; credentials remain in existing tools and Keychain.

Resource ownership lasts exactly as long as the executing process tree. File existence and broad process-name matching never establish ownership. Busy resources return their owner without starting the command; a killed owner must not leave a permanent reservation. Do not remove another task's lock file or stop its process. Locks coordinate concrete resources, not permission to publish finished work.

A saved operation records repository/source identity, command plan, per-step terminal results, verification, and full-log location. Resume only the same immutable plan and source. Successful verified steps are not replayed; uncertain side effects must not be automatically replayed. A failed step can be retried only if its plan explicitly declares it retry-safe. Verification commands are separate from execution; zero exit proves only command execution unless the declared real-path verification succeeds. Source drift invalidates the saved operation; it never authorizes hiding or moving workspace changes.

Checkpoints preserve the goal, next action and evidence references without rewriting TASKBOARD history. Recovery rereads current source/version and compares evidence; another task's publication is not proof of this task's behavioral acceptance.

Before Apple UI work, inspect the installed client, device reachability and capture prerequisites. Record logic, screenshots and real interaction separately. Do not drive the pointer, boot a simulator, or bypass locked-device/permission limits. Existing product delivery rules still apply to the whole workspace.

Document each product decision once. Navigation summarizes subjects; symptom detail and historical failed remedies live in reachable routing/troubleshooting documents. Keep managed inherited blocks generated from the product root, never edited or deleted in place.

CLI release finalization must build from committed HEAD matching the requested tag, explicitly maintain GitHub latest, and verify latest/tap through the authenticated CLI. Raw public HTTP or malformed final summary output must not turn a completed upload into an ambiguous release failure.

## Implementation references

- Python advisory process locks: https://docs.python.org/3/library/fcntl.html
- Detached execution, argv and process groups: https://docs.python.org/3/library/subprocess.html
- Atomic receipt replacement: https://docs.python.org/3/library/os.html#os.replace

## Start with the matching task

| Task | First evidence | Existing authority |
|---|---|---|
| CLI investigation | `doctor --repo <cli checkout>`, active Corral version and diagnostic output | DEVELOPMENT_ENVIRONMENT_GUIDE.md and OBSERVABILITY_KNOWLEDGE_BASE.md |
| CLI change | Environment readiness, matching domain routing, task ownership | AGENT_TASK_ROUTING_GUIDE.md and TEST_ENVIRONMENT_GUIDE.md |
| Apple change | Toolchain/diagnostic evidence, device and capture capability | Apple AGENTS.md, client_diag.py, ios-deliver doctor and the window-screenshot skill |
| Delivery | Committed whole workspace, full checks, active artifact identity | MAINTAINER_GUIDE.md; Apple signing/device guides |
| Interrupted task | Exact saved run/checkpoint, current source and active version | This guide; TASKBOARD is only the active ownership view |

The entry is the source script `cli/scripts/agent_workflow.py`, requiring Python 3.10+ on macOS/Linux and Git. It imports no Corral runtime or third-party package. Pin its absolute path for the session. An Apple-only checkout can use a [maintainer-tool artifact](https://github.com/x0c/corral/releases/latest/download/corral-maintainer-tools.tar.gz) containing this script; the installed apps have no CLI dependency. Self-description (`describe`) comes from the actual argument parser. For terminal users it prints short readable results; non-TTY and `--json` output use `{ok,data,error,meta}`. Failure codes: 1 failed/unknown execution, 2 invalid usage, 3 missing record, 5 resource/identity conflict. Full command output goes to private local logs, never routine stdout.

From `cli/`:

```bash
python3 scripts/agent_workflow.py describe
python3 scripts/agent_workflow.py doctor --repo . --json
python3 scripts/agent_workflow.py doctor --repo ../apple --json
python3 scripts/agent_workflow.py resources --resource corral-mac-acceptance
python3 scripts/agent_workflow.py run --repo . --resource corral-cli-checks --dry-run -- .venv/bin/python scripts/test_agent_workflow.py
python3 scripts/agent_workflow.py run --repo . --resource corral-cli-checks --background -- .venv/bin/python scripts/test_agent_workflow.py
python3 scripts/agent_workflow.py status --repo . --run <returned-run-id>
python3 scripts/agent_workflow.py checkpoint --repo . --task <task-name> --goal "Finish verification" --next "Inspect the saved operation receipt" --evidence <log-or-run-reference>
python3 scripts/agent_workflow.py checkpoint --repo . --task <task-name>
```

Doctor aggregates existing environment/client/delivery doctors; its success means diagnostics were collected, not that delivery or UI acceptance passed. Device unlock and capture permissions must be checked through the platform authorities before a UI scenario. It never starts a simulator, installs dependencies, pairs a device or repairs configuration.

Use `corral-cli-checks` for the complete CLI suite that may share terminal resources, `corral-apple-build` for shared Xcode/Swift build directories, and `corral-mac-acceptance` for window scenarios or performance sampling. Isolated fixture tests can remain independent. Wrap the entire foreground scenario through cleanup, not merely an `open` command which launches a detached app. Locks only coordinate participating processes; they do not seize existing apps, legacy shell locks or other tasks. Never use the resource entry as a release-order gate.

## Sequential delivery and recovery

A plan is a local JSON file containing only a `steps` array. Each step has a unique name, an argv array, optional `verify_argv`, optional resource, timeout (seconds, default 1800) and `retry_safe` (default false). Store plans outside the repository; they may contain local artifact paths but never secrets. The immutable command plan is copied and hashed in the receipt before execution. The environment identity includes this Python interpreter/version resolved executable identities and checkout virtual-environment package metadata; repository content/commit drift is rejected. This is operation recovery, not a second validation cache: complete-test reuse remains exclusively owned by `ci_stamp.py`.

```json
{
  "steps": [
    {"name": "localization", "argv": ["python3", "scripts/check_localizations.py"], "retry_safe": true},
    {"name": "client-tool-tests", "argv": ["python3", "scripts/test_client_diag.py"], "retry_safe": true},
    {"name": "core-tests", "argv": ["swift", "test"], "resource": "corral-apple-build", "retry_safe": true}
  ]
}
```

Run that plan against the Apple checkout, not the CLI checkout:

```bash
python3 scripts/agent_workflow.py run --repo ../apple --plan <plan-path> --dry-run
python3 scripts/agent_workflow.py run --repo ../apple --plan <plan-path> --background
python3 scripts/agent_workflow.py run --repo ../apple --resume <run-id> --background
```

For actual publication/install plans add `--require-clean`, keep exact tags/artifact paths in argv, and supply the existing tool's real status/verification command as `verify_argv`. CLI publication still calls `bash scripts/publish-release.sh <tag>`; mobile delivery still calls `ios-deliver` and queries its separate install/upload outcomes. Do not wrap an asynchronous submission as a verified completed install: use the tool's terminal receipt. The workflow does not invent signing, credentials, Forgejo upload or device APIs.

Resume skips completed steps, retries failed execution only when `retry_safe:true`, and refuses active/unknown operations, changed plans/source/tools, or an executed step whose verifier failed. For a failed verifier, inspect the target and reconcile with its owning tool instead of replaying the write. Timeout/interruption preserves an unknown result and log path. If the original source changed, create a new operation after checking current state; do not restore or hide another task's files to match a receipt. Record standalone logic, installed behavior, screenshots and physical interaction as separate evidence.

## Agent task prompt

Use this short task scaffold with the actual task, not another global policy block:

> Identify the affected component and read its matching domain authority. Probe readiness using the maintained entry. Record the adopted requirement before implementation. Use existing preparation, acceptance and delivery tools; keep resource ownership around the complete scenario. Classify failures from evidence before retrying. Save a checkpoint containing the next action and evidence when waiting or handing off. On recovery compare current source, operation and installed version. Deliver the full releasable workspace and report separately what was verified and what remains physically unverified.

## Maintenance acceptance

`python3 scripts/test_agent_workflow.py` invokes fresh CLI processes against disposable Git repositories. It checks truly read-only dry runs, argv safety, background handoff, exclusive ownership and automatic release, replay boundaries, verification failure, timeout, concurrent resume, source drift, committed-delivery gates and checkpoint recovery. No real session, device, app or user data is modified. These lifecycle checks complement the existing full suite; they never replace required real product behavior acceptance.

A future workflow improvement is accepted using actual task receipts: setup time, failed/redundant calls, recovery steps and complete user-behavior evidence. No speedup percentage is inferred from tool-call counts alone. Code-graph discovery currently reports a daemon connection failure; full-index refresh through `codebase-memory-mcp cli index_repository --repo-path <checkout> --mode full` must succeed before relying on graph output. Use source/text inspection only while the documented refresh fails; diagnose the shared daemon through the global MCP guide without changing unrelated configuration.

## Remaining environment improvements

Adopt the maintained resource names in new task prompts and legacy check/acceptance launchers. Advisory ownership cannot coordinate commands that bypass the entry. Source/test edits during full-suite discovery can produce mixed test coverage; the existing CI runner rejects that snapshot and the operation must be rerun against current source. Never manufacture a success stamp from the individual shard results. For asynchronous fixture behavior, wait for the observable result instead of assuming a fixed short sleep is sufficient under build/test contention. The code-graph daemon connection remains a separate shared-tooling issue.
