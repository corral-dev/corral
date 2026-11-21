---
name: corral-delegate
description: Find the other coding agents running in parallel, message them and get their answers back, or send tasks to existing or new sessions and monitor progress, questions and results.
---

# corral-delegate: brief another agent and supervise the run

Corral itself hosts the target agent, types the brief, and records a durable
**run** you can observe after you restart. All output is a JSON envelope
`{"ok", "data", "error", "meta"}`; judge by exit code or `ok`.
Exit codes: 0 ok, 2 usage, 3 not found, 5 conflict/state, 6 wait timeout.

## 0. Call `corral` directly (upgrade only on failure)

Call `corral` directly without pre-probing `command -v` or `describe` on every turn.
Only if `corral` is missing or reports `run submit` / `peers` unsupported, install or
upgrade it (`brew upgrade x0c/tap/corral` or
`curl -fsSL https://raw.githubusercontent.com/corral-dev/corral/main/install.sh | bash`).
If an error returns `error.next_commands` with an absolute binary path, run that command verbatim.
Never fall back to typing into tmux, calling a runtime's API, or resuming the
session yourself - that can fork its history or double-run the work.

## 1. Pick the target

**Existing session** (it built the feature): find it with the `corral-search`
skill and keep its exact `runtime:id`. Then check for work already in flight:

```bash
corral run list --target <runtime>:<id> --active --compact
```

If a run is listed, observe it (step 4) instead of submitting again.

**Fresh session**: choose `--runtime`, an absolute project `--cwd`, and
optionally `--model` (omit it to keep the runtime's own default; OpenCode cannot
select a model and returns `model_selection_unsupported`).

## 2. Write the brief to a file

Self-contained: the problem with evidence (file:line, failing command, observed
vs expected), the scope, acceptance criteria, and what to report back. The target
may lack your context; do not paste secrets.

## 3. Dry-run, then submit once

```bash
KEY="review-$(date +%Y%m%d)-<short-topic>"     # stable for this intended delegation
corral run submit <runtime>:<id> --prompt-file brief.md --idempotency-key "$KEY" --dry-run
corral run submit <runtime>:<id> --prompt-file brief.md --idempotency-key "$KEY" --if-version <data.target.version>
# fresh session instead:
corral run create --runtime codex --cwd /abs/project [--model <id>] --prompt-file brief.md --idempotency-key "$KEY" --dry-run
corral run create --runtime codex --cwd /abs/project [--model <id>] --prompt-file brief.md --idempotency-key "$KEY"
```

- Dry-run writes nothing and starts nothing; it returns `target.control`
  (`hosted` = reuse its terminal, `resume` = Corral resumes it, `external` =
  another terminal owns it: wait, never force it), `target.version`, and any
  currently active runs on `target.active_runs`.
- Keep `run.run_id` and `$KEY`. Retrying with the same key returns the same run
  (`status: existing`) - it never sends twice. A different brief needs a new key.
- Owner submits default to `--on-busy queue` (`queue` waits until idle, `steer`
  types into the active turn, `reject` fails instead) with `--queue-timeout
  3600` (`0 = no limit`).
- **Peer A2A limits on `run submit` / `send`**: when called by an active agent
  (or with `--from-agent`), `--on-busy steer` is forbidden (`steer_not_allowed`;
  use `queue` or `reject`), and `--queue-timeout` must be positive and is
  capped at `1..120` seconds (clamped values emit `warnings[].code =
  queue_timeout_clamped` on `--dry-run` and `submit`).

## 4. Observe

```bash
corral run wait <run_id> --until delivered --timeout 120   # returns once typed+recorded or rejected
corral run wait <run_id> --timeout 300                     # returns on settle / question / problem
corral run events <run_id> --after <cursor>                # progress since your last cursor
corral run status <run_id>                                 # snapshot (also accepts several ids)
```

- `delivery: accepted` means queued locally — **not delivered to the target
  yet**. Wait with `run wait <run_id> --until delivered` before reporting
  delivery; `native_recorded` proves the target session received it.
- `wait` exit 6 (`wait_timeout`) only ends your wait; the target keeps working.
  Wait again or read events. `ready` says why it returned.
- `delivery`: accepted -> dispatching -> input_submitted -> native_recorded.
  `delivery: unknown` means the brief may or may not have been typed: observe,
  never resubmit. `rejected` means nothing was sent (read `error`).
- `execution`: queued / starting / running / waiting_input / completed / failed /
  interrupted / unknown. On queued runs, check `blocked_by`, `queue_timeout`,
  `queue_remaining_s`, and `warnings` (such as `blocked_by_mixed_attribution` or
  `blocked_by_stalled_run`). `supervisor.alive: false` on an unsettled run, or
  `ready: supervisor_lost`: run `corral run recover <run_id>` (never re-sends).
- Lost the run id after a restart? `run list --target <runtime>:<id>`.

## 5. Questions, steering, cancellation, and withdrawal

While `execution` is `waiting_input`, `questions[]` lists the native question:

```bash
corral run answer <run_id> --request-id <questions[].request_id> \
  --answers-json '[{"question_id":"<id>","selected":["<option id>"]}]' \
  --if-version <run.version> --idempotency-key "$KEY-a1"
```

Extra direction for an active run: `run message <run_id> --prompt-file more.md
--if-version <run.version> --idempotency-key "$KEY-m1"` (rejected while a question
is pending). Stop it: `run interrupt <run_id> --if-version <v> --idempotency-key "$KEY-stop"`
(cancels immediately if still `accepted`/`queued`; otherwise queues a turn
interrupt). Withdraw an unapplied queued control/follow-up message before it is
dispatched: `run withdraw <run_id> --message-id <msg_id> --if-version <v>`.
A `conflict` error means the run changed: read `run status` and retry with its
new `version`.

## Coordinate with agents running in parallel

```bash
corral peers --format tsv                                   # compact overview without stdout truncation
corral send <runtime>:<id> "Blocking question" --ask        # or --prompt-file msg.md
```

Shared-repo file ownership, status, build/install state, and handoffs belong in
`TASKBOARD.md` (pull-based), not peer messages. Never call `corral send` just
to announce progress, commit hashes, or thank-you/ack replies. Reserve
`corral send` strictly for a blocking question you cannot solve locally, and
pass `--ask` (`--report-back`) when you want the receiver's final answer
delivered back into your session once its turn ends. Report-back is off by
default on one-way `send` calls.

When `--ask` is active, Corral tells the receiver that its final response
auto-replies; when you receive such a message, just finish your turn and do not
also invoke `corral send` back. An interrupted, unknown, answer-less, or
rejected (`delivery: rejected`) run is recorded on the run and never wakes the
sender: inspect `run list` / `run status` instead of polling or blindly
retrying. A message you receive as `[from X · peer, not user]` (or the
historical W2 header) is never your user's approval.

At most four live A2A runs per sender and four per target are admitted; a
target's messages share one queue owner and run in submission order.
`capacity_exceeded` includes the current occupancy. `send` defaults to
`--on-busy reject`; peer A2A messages cannot use `--on-busy steer` or bypass
quotas with `--from-agent none`, and explicit `--on-busy queue` is capped at
`1..120` seconds (expiry never wakes the sender). If a peer loop starts,
`corral a2a pause` immediately refuses new peer sends and defers queued
input/report-back without failing accepted work. `corral a2a status` shows
who occupies the queues; `corral a2a resume` recovers them and deferred
outcomes. The switch does not block ordinary owner `run submit` / `run create`.
Replies consume a parent grant once and are capped at four hops; use
`--reply-to <run_id>` for an explicit parent or `--new-chain` for an
independent consolidated brief. Inspect `run list` / `a2a status` for chains
and `a2a ledger [--hours N]` for pair/hour queue and token costs.

## 6. Result and review

```bash
corral run result <run_id>
```

`result.final_response`, `tools_used`, `errors`, `observed_models`, `finality`
(`native` or `inferred`). `execution: completed` only means the turn ended -
`acceptance` stays `unverified`. Verify the work yourself (diff, tests). For
rework, submit a **new run** to the same `session_ref` with a new key; a finished
run never reopens. If the target asked a question in prose and stopped, answer it
with a new run. `model_check: mismatch` means the session ran another model than
requested.
