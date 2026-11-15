# Mobile session actions: fork + handoff

## 1. Scope (locked)

Covers **fork (`session.copy`) + handoff** from the iOS session page top-right menu. In scope: `session.copy` (new) and `session.handoff` (already server-side). Out of scope: export, restart, a new native-resume protocol, new i18n keys, version bump, release — export/restart must not be smuggled into this change. On success the client **navigates to the new session** (the fork, not the source).

Availability (client-side): **unavailable** (no tmux, runtime missing, fork/clone failed) hides the fork entry; a **readonly pairing** hides the whole action sheet (the server also rejects).

## 2. Protocol: `session.copy`

- Method `session.copy`, params `{key}` only, returns `{"session": SessionSummary}` — same shape as `session.resume` / `session.handoff`.
- Not in `_READONLY_METHODS`: a readonly device gets `unauthorized` (`remote.err.readonly`); iOS hides the sheet so the error is never shown.
- No `confirm` (non-destructive creation; confirm is only for stop/delete). Rate limit: `SESSION_CREATE` bucket, same as resume/handoff (`remote.err.action_rate_limited` on excess).
- Error codes, same vocabulary as handoff: `usage_error` (missing key); `not_found` (session gone via `require_session`, placeholder migration included); `unavailable` (tmux missing → `remote.err.tmux_missing_handoff`, runtime not installed, fork plan missing, clone failed — the original `LaunchError` message passes through `remote.err.handoff_failed`, no fabricated paths); `rate_limited`; `unauthorized`.

## 3. Server reuse chain (no forks of the fork logic)

`SessionHub.copy_session(key)`:

1. `require_session(key)` — resolves placeholder old keys via key migration.
2. `title = store.get_title(session)`; keep `session.get("cwd")` for the new card.
3. `request = registry.prepare_copy_request(session, title)` — official fork first (Claude/Codex/OpenCode/Pi), else disk clone with new identity + fork suffix. Title, suffix and not-installed errors live there.
4. `plan = registry.build_launch_plan(request)` — the same fork/resume plans the TUI uses for Ctrl+T fork; do not reimplement per-runtime branches.
5. `_host(plan, request.target_runtime_id, request.title, session.get("cwd"))` — same tmux hosting and provisional placeholder card as new/handoff.

Error mapping mirrors `handoff_session`: missing tmux → `unavailable`, `LaunchError` → `unavailable` reusing the `handoff_failed` template (existing i18n key, no new strings). `LaunchRequest(copy_session=True)` stays same-assistant only (`registry` enforces `copy_same_assistant`).

## 4. iOS wiring contract

Call `session.copy` with `{key}` and expect `{"session": SessionSummary}`. On `ok`, open the returned session key (watch + messages) while keeping the source open. On `unavailable`, hide the fork entry and surface the server message once. On `unauthorized`, the sheet should already be hidden; treat it as read-only state.

Kimi note: forks of Kimi sessions resume natively in the new identity, and a Kimi fork created from an interactive pane runs interactively — do not document it as "runs once and exits" (that applies only to Kimi cross-runtime handoff prompts).

## 5. Acceptance

1. `session.copy` on a Claude history fixture returns a fork launch plan (`--fork-session`) through `prepare_copy_request` + `build_launch_plan`.
2. `copy_session` with tmux unavailable returns `unavailable` with no host attempt.
3. Unpaired `session.copy` is rejected; `M_SESSION_COPY` is listed alongside `session.new/resume/handoff` in the unpaired-rejection test.
4. Readonly pairing rejects `session.copy` with `unauthorized`.
5. `python3 -m unittest test_remote_actions test_remote_sessions test_runtime` green; `ruff check` on touched files green.
6. No changes outside the six allowed files; no exports/restarts/i18n/version changes.

## 6. Follow-up doc patch (REMOTE_KNOWLEDGE_BASE.md, not this round)

`rename` work was editing `REMOTE_KNOWLEDGE_BASE.md`, so that round could not touch it. Add later under the method table (`session.new`/`resume`/`handoff` row): `` `session.copy` → `{"session": <SessionSummary>}` (same-assistant clone: official fork else disk clone; readonly hidden, `SESSION_CREATE` limit, no confirm) ``.

<!-- 该文档整理/压缩于 2026-11-15 -->
