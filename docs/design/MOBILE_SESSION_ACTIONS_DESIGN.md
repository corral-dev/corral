# Mobile session actions: copy + handoff

## 1. Scope (locked)

- This document covers **copy + handoff** from the iOS session page top-right menu.
- **In scope:** `session.copy` (new) and `session.handoff` (already exists server-side).
- **Out of scope:** export, restart, new native-resume protocol, new i18n keys,
  version bump, release. Export/restart must not be smuggled into this change.
- On success the client **navigates to the new session** (the copy, not the source).
- Availability rules (client-side):
  - **unavailable** (no tmux, runtime missing, fork/clone failed) → hide the copy entry.
  - **readonly pairing** → hide the whole action sheet (server also rejects).

## 2. Protocol: `session.copy`

- Method: `session.copy`. Params: `{key}` only. Returns `{"session": SessionSummary}`,
  same shape as `session.resume` / `session.handoff`.
- Not in `_READONLY_METHODS`: a readonly device gets `unauthorized`
  (`remote.err.readonly`); iOS hides the sheet so the user never sees the error.
- No `confirm` (non-destructive creation; confirm is only for stop/delete).
- Rate limit: `SESSION_CREATE` bucket, same as resume/handoff
  (`remote.err.action_rate_limited` on excess).
- Error codes (same vocabulary as handoff):
  - `usage_error` — missing key.
  - `not_found` — session gone (via `require_session`, placeholder migration included).
  - `unavailable` — tmux missing (`remote.err.tmux_missing_handoff`), runtime not
    installed, fork plan missing, or clone failed. The original `LaunchError`
    message is passed through `remote.err.handoff_failed`; no paths are fabricated.
  - `rate_limited`, `unauthorized` — as above.

## 3. Server reuse chain (no forks of the fork logic)

`SessionHub.copy_session(key)`:

1. `require_session(key)` — resolves placeholder old keys via key migration.
2. `title = store.get_title(session)`; keep `session.get("cwd")` for the new card.
3. `request = registry.prepare_copy_request(session, title)` — official fork first
   (Claude/Codex/OpenCode/Pi), else disk clone with new identity + copy suffix.
   Title, suffix, and not-installed errors all live there.
4. `plan = registry.build_launch_plan(request)` — same fork/resume plans the TUI
   uses for Ctrl+T copy; do not reimplement per-runtime branches.
5. `_host(plan, request.target_runtime_id, request.title, session.get("cwd"))` —
   same tmux hosting + provisional placeholder card as new/handoff.

Error mapping mirrors `handoff_session`: missing tmux → `unavailable`, `LaunchError`
→ `unavailable` reusing the `handoff_failed` message template (existing i18n key;
no new strings). `LaunchRequest(copy_session=True)` stays same-assistant only
(`registry` enforces `copy_same_assistant`).

## 4. iOS wiring contract

- Call `session.copy` with `{key}`; expect `{"session": SessionSummary}`.
- On `ok`: open the returned session key (watch + messages), keep the source open too.
- On `unavailable`: hide the copy entry and surface the server message once.
- On `unauthorized`: the sheet should already be hidden; treat as read-only state.
- Kimi note: copies of Kimi sessions resume natively in the new identity; a Kimi
  copy created from an interactive pane runs interactively — do not document it as
  "runs once and exits" (that only applies to Kimi cross-runtime handoff prompts).

## 5. Acceptance

1. `session.copy` on a Claude history (fixture) returns a fork launch plan
   (`--fork-session`) via `prepare_copy_request` + `build_launch_plan`.
2. `copy_session` with tmux unavailable returns `unavailable` (no host attempt).
3. Unpaired `session.copy` is rejected; `M_SESSION_COPY` listed alongside
   `session.new/resume/handoff` in the unpaired-rejection test.
4. Readonly pairing rejects `session.copy` with `unauthorized`.
5. `python3 -m unittest test_remote_actions test_remote_sessions test_runtime` green;
   `ruff check` on touched files green.
6. No changes outside the six allowed files; no exports/restarts/i18n/version changes.

## 6. Follow-up doc patch (REMOTE_KNOWLEDGE_BASE.md, not this round)

`rename` work is currently editing `REMOTE_KNOWLEDGE_BASE.md`, so this round must
not touch it. One line to add later under the method table (`session.new` /
`resume` / `handoff` row): `` `session.copy` → `{"session": <SessionSummary>}`
(same-assistant clone: official fork else disk clone; readonly hidden,
`SESSION_CREATE` limit, no confirm) ``.

<!-- 该文档整理/压缩于 2026-09-29 -->
