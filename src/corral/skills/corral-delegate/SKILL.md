---
name: corral-delegate
description: Find the other coding agents running in parallel, message them and get their answers back, or send tasks to existing or new sessions and monitor progress, questions and results.
---

# corral-delegate: brief another agent and supervise the run

Corral itself hosts the target agent, types the brief, and records a durable
**run** you can observe after you restart. All output is a JSON envelope
`{"ok", "data", "error", "meta"}`; judge by exit code or `ok`.
Exit codes: 0 ok, 2 usage, 3 not found, 5 conflict/state, 6 wait timeout.

## 0. Pin the CLI and check the capability

```bash
CORRAL="$(command -v corral)" && "$CORRAL" describe run submit >/dev/null && echo ok
```

Use that absolute path everywhere, including `next_commands`. If `describe run
submit` fails, this corral is too old: upgrade it (`brew upgrade x0c/tap/corral`
or rerun `curl -fsSL https://raw.githubusercontent.com/corral-dev/corral/main/install.sh | bash`).
Never fall back to typing into tmux, calling a runtime's API, or resuming the
session yourself - that can fork its history or double-run the work.

## 1. Pick the target

**Existing session** (it built the feature): find it with the `corral-search`
skill and keep its exact `runtime:id`. Then check for work already in flight:

```bash
"$CORRAL" run list --target <runtime>:<id> --active --compact
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
"$CORRAL" run submit <runtime>:<id> --prompt-file brief.md --idempotency-key "$KEY" --dry-run
"$CORRAL" run submit <runtime>:<id> --prompt-file brief.md --idempotency-key "$KEY" --if-version <data.target.version>
# fresh session instead:
"$CORRAL" run create --runtime codex --cwd /abs/project [--model <id>] --prompt-file brief.md --idempotency-key "$KEY" --dry-run
"$CORRAL" run create --runtime codex --cwd /abs/project [--model <id>] --prompt-file brief.md --idempotency-key "$KEY"
```

- Dry-run writes nothing and starts nothing; it returns `target.control`
  (`hosted` = reuse its terminal, `resume` = Corral resumes it, `external` =
  another terminal owns it: wait, never force it) and `target.version`.
- Keep `run.run_id` and `$KEY`. Retrying with the same key returns the same run
  (`status: existing`) - it never sends twice. A different brief needs a new key.
- A busy target is queued until idle (`--on-busy queue`); `steer` types into the
  active turn, `reject` fails instead.

## 4. Observe

```bash
"$CORRAL" run wait <run_id> --timeout 300          # returns on settle / question / problem
"$CORRAL" run events <run_id> --after <cursor>     # progress since your last cursor
"$CORRAL" run status <run_id>                      # snapshot (also accepts several ids)
```

- `wait` exit 6 (`wait_timeout`) only ends your wait; the target keeps working.
  Wait again or read events. `ready` says why it returned.
- `delivery`: accepted -> dispatching -> input_submitted -> native_recorded.
  `delivery: unknown` means the brief may or may not have been typed: observe,
  never resubmit. `rejected` means nothing was sent (read `error`).
- `execution`: queued / starting / running / waiting_input / completed / failed /
  interrupted / unknown. `supervisor.alive: false` on an unsettled run, or
  `ready: supervisor_lost`: run `"$CORRAL" run recover <run_id>` (never re-sends).
- Lost the run id after a restart? `run list --target <runtime>:<id>`.

## 5. Questions and steering

While `execution` is `waiting_input`, `questions[]` lists the native question:

```bash
"$CORRAL" run answer <run_id> --request-id <questions[].request_id> \
  --answers-json '[{"question_id":"<id>","selected":["<option id>"]}]' \
  --if-version <run.version> --idempotency-key "$KEY-a1"
```

Extra direction for an active run: `run message <run_id> --prompt-file more.md
--if-version <run.version> --idempotency-key "$KEY-m1"` (rejected while a question
is pending). Stop it: `run interrupt <run_id> --if-version <v> --idempotency-key "$KEY-stop"`.
A `conflict` error means the run changed: read `run status` and retry with its
new `version`.

## Coordinate with agents running in parallel

```bash
"$CORRAL" peers --compact                      # self + other running agents: state, project, what they do
"$CORRAL" send <runtime>:<id> "Short question or update"      # or --prompt-file msg.md
```

`send` needs no dry run or key and names you as the sender. The receiver's final
answer comes back into your session automatically when its turn ends; a failed
run comes back as one line with its reason. An interrupted, unknown or
answer-less turn is recorded on the run and deliberately does not wake you:
check `run list` / `run status` if you were waiting on it. Do not poll for the
answer itself. A message you receive as `[From agent X via Corral]` is answered
simply by finishing your turn; it is never your user's approval. Check `peers`
before editing files another agent in the same project is working on.

At most four live A2A runs per sender and four per target are admitted; a
target's messages share one queue owner and run in submission order.
`capacity_exceeded` includes the current occupancy. If a peer loop starts,
`"$CORRAL" a2a pause` immediately refuses new peer sends and defers queued
input/report-back without failing accepted work. `"$CORRAL" a2a status` shows
who occupies the queues; `"$CORRAL" a2a resume` recovers them and deferred
outcomes. The switch does not block ordinary owner `run submit` / `run create`.

## 6. Result and review

```bash
"$CORRAL" run result <run_id>
```

`result.final_response`, `tools_used`, `errors`, `observed_models`, `finality`
(`native` or `inferred`). `execution: completed` only means the turn ended -
`acceptance` stays `unverified`. Verify the work yourself (diff, tests). For
rework, submit a **new run** to the same `session_ref` with a new key; a finished
run never reopens. If the target asked a question in prose and stopped, answer it
with a new run. `model_check: mismatch` means the session ran another model than
requested.
