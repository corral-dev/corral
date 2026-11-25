# corral Reference

## Search, list, and export details

- **`--compact` vs `--fields`**: `--compact` uses unindented JSON and trims `list`/`search`/`show` to common fields (`ref`, `id`, `short_id`, `corral_id`, `runtime`, `title`, `status`, `live`, `keepalive`, `attention`, `mtime`, `cwd_display`, `resumable`, `resume_command`, `last_user`, `last_agent`). Note that `cwd`, `pid`, and `initiated_by` are omitted by `--compact` unless requested explicitly with `--fields cwd,pid,...` (unknown fields exit `2` and list valid names).
- **Zero-hit coverage**: A search result with `count=0` only proves no hit when `scanned > 0` and `potentially_limited == false` (`truncated_runtimes` empty, `failed_runtimes` empty, `history_read_failures` empty). `matched == 0` includes suggested `next_commands`.
- **`share` and `export` with `--out`**: Writing to a file defaults to **JSONL** (even if the filename ends in `.json`):
  - `corral share <ref> --out /tmp/t.jsonl`: line 1 is `{"type": "session_meta", ...}`, followed by one event per line (`user_message`, `assistant_message`, `thinking`, `tool_call`, `tool_result`). Pass `--json` (or `--format json`) to write a single JSON envelope instead.
  - `corral export --since 7d --out /tmp/week.jsonl`: one session object per line with `messages`.
- **`show` / `export` product boundaries**: `messages[]` contains text replies only (no tool calls/edits); `last_agent` may be empty on Cursor/Kimi; user-side `messages[]` can include runtime-injected prompts (e.g. plan attachments). Always verify real code changes via `corral share` or `git`.

## Delegation states, control, and recovery

- **Target `control` modes** (from `run submit --dry-run` or `show.control`):
  - `hosted`: Corral hosts the session in tmux; input goes to its terminal.
  - `resume`: Session is stopped; Corral resumes it in a hosted terminal (`resume_command` is unsafe when `keepalive: true`; always use `run submit`).
  - `external`: Another terminal outside Corral owns the session (`live: true, keepalive: false`). Wait — never force or resume manually.
- **`delivery` lifecycle**: `accepted` (queued locally) -> `dispatching` -> `input_submitted` -> `native_recorded`, or `rejected` (nothing sent; read `error`), or `unknown` (observe with `run status`/`events`; never resubmit).
- **`execution` states**: `queued`, `starting`, `running`, `waiting_input`, `completed`, `failed`, `interrupted`, `unknown`.
  - Check `blocked_by`, `queue_timeout`, `queue_remaining_s`, and `warnings` (`mixed_attribution_active`, `blocked_by_mixed_attribution`, `blocked_by_stalled_run`, `supervisor_not_running`) on queued runs.
  - If `supervisor.alive == false` on an unsettled run (or `ready: supervisor_lost`), run `corral run recover <run_id>` (never re-sends).
- **Idempotency & busy policies**:
  - Keep `run.run_id` and `$KEY`. Retrying `run submit` / `run create` with the same `$KEY` and spec returns `status: existing` without sending twice; changing the brief requires a new key.
  - Owner `run submit` defaults to `--on-busy queue` with `--queue-timeout 3600` (`0` = unlimited).
  - Peer A2A calls (`send`, or `run submit` from an active agent / `--from-agent`) forbid `--on-busy steer` (`steer_not_allowed`) and cap `--queue-timeout` at `1..120` seconds (`queue_timeout_clamped` warning if higher).

## Questions, steering, cancellation, and withdrawal

When `execution` is `waiting_input`, read `questions[]` from `run status`:

```bash
corral run answer <run_id> --request-id <questions[].request_id> \
  --answers-json '[{"question_id":"<id>","selected":["<option id>"]}]' \
  --if-version <run.version> --idempotency-key "$KEY-a1"
```

- Steer an active run (rejected while a question is pending): `corral run message <run_id> --prompt-file more.md --if-version <run.version> --idempotency-key "$KEY-m1"`
- Interrupt an active turn (cancels immediately if `accepted`/`queued`): `corral run interrupt <run_id> --if-version <run.version> --idempotency-key "$KEY-stop"`
- Withdraw a queued follow-up/control message before dispatch: `corral run withdraw <run_id> --message-id <msg_id> --if-version <run.version>`
- On `conflict`, re-read `corral run status <run_id>` and retry with the new `version`.
- `model_check: mismatch` on `run result` means the session ran a different model than `--model` requested.

## Peer A2A limits, chains, and circuit breakers

- Quotas: max 4 live A2A runs per sender and 4 per target (`capacity_exceeded` reports current occupancy); 12 messages per 10 minutes burst guard (`rate_limited`). `send` defaults to `--on-busy reject`; explicit `--on-busy queue` is bounded to `1..120` seconds.
- `--from-agent none` hides `[from <ref> · peer, not user]` behind `[from unknown · peer, not user]` while preserving all A2A quotas and queue caps.
- Rejected (`delivery: rejected`), interrupted, unknown, or answer-less `--ask` runs never wake the sender; inspect `run list --format tsv` or `run status <run_id>`.
- Reply chains default to 4 hops (`chain_budget_exhausted`); pass `--reply-to <run_id>` for an explicit parent or `--new-chain` for a consolidated independent brief.
- Circuit breakers & diagnostics: `corral a2a status`, `corral a2a ledger --hours 24`, `corral a2a pause` (blocks new peer sends and defers queued input/report-back without failing accepted work; does not block owner `run submit`/`create`), `corral a2a resume`.
