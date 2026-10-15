# Privacy

`corral` is designed as a local terminal utility for existing Claude Code, Codex CLI, OpenCode, Kimi Code CLI, and Cursor Agent CLI users.

## Data It Reads

- Claude Code history under `~/.claude/projects/`.
- Codex CLI history under `~/.codex/sessions/`.
- Codex session names from `~/.codex/session_index.jsonl` when present.
- Cursor Agent CLI history under `~/.cursor/chats/` (per-chat `meta.json` / `store.db`)
- OpenCode history from its SQLite database at `~/.local/share/opencode/opencode.db` (or the
  directory pointed to by `OPENCODE_DATA_DIR`), opened with a read-only connection (`mode=ro`).
  The tool never writes to this database.
- Kimi Code CLI history under `~/.kimi-code/sessions/` (per-session `state.json` metadata and the
  `agents/main/wire.jsonl` conversation log).
- Pi history under `~/.pi/agent/sessions/` (or `PI_CODING_AGENT_DIR`), one JSONL file per session.
- Cursor's user-level hook configuration at `~/.cursor/hooks.json`, solely to inspect and preserve
  existing entries while managing corral's own live-state observer entries.

The tool reads these files to build a recent-session list, extract a compact preview, and prepare native resume or cross-runtime handoff commands.

## Data It Writes

- Generated title cache under `~/.cache/corral/titles.json`.
- A lock file under `~/.cache/corral/titles.lock` while title generation is running.
- Sidebar memory under `~/.cache/corral/sidebar-layout.sqlite3`: session groups (which hosted
  sessions are shown side-by-side in the right pane), their generated fruit names, collapsed and
  pinned state, the last focused session, and whether the sidebar is hidden. Session keys and
  project paths only — no conversation content. Shared by every corral window on the machine.
  Older versions kept this in `~/.cache/corral/split-layout.json` and `~/.cache/corral/ui-prefs.json`;
  those files are imported once and then left untouched (never rewritten, renamed, or deleted).
- Update-check state under `~/.cache/corral/update.json` (which version you last dismissed, and on
  which day) — only written when you click "dismiss" on the update notification or run `corral update`.
- Content-free session attention state under `~/.cache/corral/session-attention.sqlite3`. It stores
  runtime/session identifiers, opaque activity/question tokens, timestamps, the current attention
  kind, and read baselines. It does not store prompts, answers, titles, tool output, or conversation text.
- Corral-managed Cursor observer entries in `~/.cursor/hooks.json`. The TUI installs or repairs these
  entries idempotently in the background; unrelated hook entries are preserved. Before changing an
  existing file, corral writes a user-only backup under
  `~/.cache/corral/cursor-hooks-backups/`, then replaces the config atomically. You can inspect,
  preview, repair, or remove this integration with `corral observer status cursor`,
  `corral observer install cursor --dry-run`, `corral observer install cursor`, and
  `corral observer uninstall cursor`. Uninstall removes only corral-managed entries.
- A bounded derived-performance database under `~/.cache/corral/performance-cache.sqlite3`. It may
  contain parsed session metadata and conversation preview text copied from history files that your
  OS user can already read. Entries are keyed by exact source-file signatures and rebuilt when those
  files change. The directory is user-only, the database is user-readable/writable, and nothing in it
  is uploaded. Inspect it with `corral cache status`, preview deletion with
  `corral cache clear --dry-run`, clear it with `corral cache clear`, or disable it with
  `CORRAL_CACHE=0`.
- A second derived cache under `~/.cache/corral/remote-transcripts.sqlite3` used by the phone remote
  service. It stores normalized chat messages (including tool summaries) so opening a session on the
  phone does not re-parse the entire history file. Same signature invalidation, same user-only
  directory, nothing uploaded. `CORRAL_CACHE=0` disables this on-disk cache too; the in-process
  copy used by a running remote service is not written to disk. `corral cache clear` also deletes
  this file.
  Note: since the full-text search feature (`Ctrl+F`), the TUI warms a search index in the
  background shortly after startup, which parses the conversation text of every scanned session
  rather than only the ones you open. This does not read anything your OS user could not already
  read and still uploads nothing, but it does mean the derived-performance database above fills up
  with conversation text sooner and more broadly than before. The search index itself lives only in
  memory and is never written to disk. `CORRAL_CACHE=0` still disables the on-disk part.
- Share transcripts exported from the TUI Advanced menu under `~/.cache/corral/share/` (the same
  `corral.share/v1` JSON as `corral share`, including conversation text, thinking, and tool I/O).
  The directory follows `CORRAL_CACHE_DIR` / XDG like other cache files. Nothing in it is uploaded.
- Corral's bundled Pi identity extension under `~/.pi/agent/extensions/corral-session-identity/`,
  plus content-free process/session claims and single-writer locks under
  `~/.pi/agent/corral-session-identity/`. They contain local identifiers, paths, PIDs, timestamps,
  and protocol state only — never prompts, answers, tool data, credentials, or model settings. The
  extension makes no network requests.
- A Pi migration journal under `~/.cache/corral/pi-migration-v1.json`. On interactive startup,
  legacy `corral-*` / `pickup-*` session folders are checked. When a main session can be proven by
  exact header ID, the existing JSONL is copied byte-for-byte into Pi's default project folder so
  native `/resume` can see it. Existing targets are never overwritten, subagent files are not
  copied, active folders are deferred, and the original legacy folder is retained as a backup.

It does not write attention state into Claude Code, Codex CLI, OpenCode, Kimi Code CLI, Cursor, or Pi
conversation history. The Pi migration above creates a byte-identical copy but does not append to or
rewrite conversation content. The Cursor write described above changes only the user-level hook configuration.

## Network And Account Usage

The core scanner, TUI, preview screen, and JSON output do not make network requests by themselves.

**Client auto-update.** Each time the TUI starts, it makes one HTTPS request to the public GitHub API
(`https://api.github.com/repos/x0c/corral/releases/latest`) to check the latest published version
number. No session content, file paths, or any other local data is sent — only that one request to
that one endpoint. If your install can't be auto-upgraded (a source/dev checkout), this check is
skipped entirely and nothing is requested. If a newer version is found, a small notice appears in the
bottom-right corner; clicking it runs the same install command your install channel already uses
(`brew upgrade x0c/tap/corral` or `pip install --upgrade`), then offers to restart `corral`. You can also
trigger this manually any time with `corral update`, or dismiss the notice for the day.

Automatic title generation sends bounded session excerpts to the configured OpenAI-compatible gateway when its URL and key are configured (`~/.config/corral/llm-gateway.json` or the matching environment overrides). Without gateway configuration, Corral can instead launch an installed Claude Code, Codex, OpenCode, Cursor, or Pi runtime in non-interactive mode, using that runtime's existing account and model settings. This may consume the runtime account's quota and send the excerpts to its configured provider; "local Agent" describes the client process, not offline inference.

Title jobs run outside user projects in a private Corral-owned workspace under `~/.config/corral/title-worker/` (or `XDG_CONFIG_HOME`). Claude, Codex, and Pi use native no-session modes; OpenCode uses a private server/database, and Cursor uses a separate data directory. Temporary job data is removed afterward. Corral does not copy credentials or resume/rewrite user conversations. Incompatible or unauthenticated runtimes are skipped; if no channel works, readable local fallback titles remain.

Accepted titles remain stable. Failed or invalid requests have a six-hour cooldown. Missing-channel failures can retry when a usable channel appears; insufficient-context results retry when task input changes. All channels share the same host-side title cache and phone synchronization. Read-only JSON query commands never start title generation.

## Attention Status And Cursor Observer

The yellow/green/red attention dots are derived locally. Claude Code, Codex CLI, OpenCode, and Kimi
Code use explicit events in their existing local history. Cursor history is probed only when a session
is live or its relevant files changed, avoiding repeated database reads for cold sessions. Existing
history is treated as read on the first upgraded launch, so installing the feature does not create a
wall of unread alerts.

Cursor's live turn boundaries are delivered to a short-lived local corral hook process. It uses only
the hook event name, conversation/session identifier, and generation identifier, then records the
local receipt time needed to update attention state; prompt and response bodies are neither stored in
the attention database nor logged by the hook. Malformed input, configuration errors, permission errors, or local database
failures are fail-open: the hook exits successfully and never blocks Cursor from continuing.

Attention state never triggers network requests, sounds, system notifications, or remote telemetry.
The observer management commands support JSON output; install/uninstall also support strict
`--dry-run`, which makes no configuration, backup, or directory changes.

When you resume or hand off a session, the selected runtime process takes over the terminal. From that point on, Claude Code or Codex CLI behaves according to its own configuration.

## Keep-Alive (Background tmux)

By default, sessions started or resumed from the TUI are wrapped in a dedicated background `tmux`
server (socket name `corral-keepalive`) so the underlying process survives an SSH disconnect. This changes
what stays running after `corral` exits:

- The wrapped runtime process (and everything it does) keeps running in the background until it exits
  on its own, is manually closed (`x` in the TUI), or is auto-reaped after being idle (default 2h, see)
  `CORRAL_KEEPALIVE_IDLE_HOURS`, legacy name `SC_KEEPALIVE_IDLE_HOURS`).
- To detect which sessions are already backgrounded, `corral` reads the local process table (`ps -eo
  pid,ppid`) and lists the tmux server's own sessions (`tmux -L corral-keepalive list-sessions`). This is
  local process metadata, not file content, and is not written anywhere.
- On a machine shared with other local users, anyone able to run commands as your OS user (or root) can
  attach to `tmux -L corral-keepalive` and see the live terminal content of a backgrounded session — the
  same exposure any tmux session already has under your account; `corral` does not add encryption or
  access control on top of it.
- Disable entirely with `corral --no-keepalive` for one run, or `CORRAL_KEEPALIVE=0` (legacy
  `SC_KEEPALIVE=0`) permanently. The full-screen attach form is skipped when `corral` is already
  running inside `tmux`/`screen`; embedded panes don't attach and are unaffected.

## Cross-Runtime Handoff

For handoff between runtimes, the tool passes the original history location (a file path, or a
SQLite database path plus session ID for OpenCode) and a short format hint to the target runtime.
It does not copy the full conversation into command-line arguments and does not modify the source
session.

The target runtime may choose to read that local history after it starts.

## Phone Remote (`corral remote`)

Optional phone relay (`pip install 'corral[remote]'`) lets a paired phone list sessions, watch
live terminal frames, send input, and start or stop hosted sessions on this machine.
Turn the host service on/off with `corral remote on` / `corral remote off` (background daemon);
print a pairing QR with `corral remote pair`.

- **What leaves the machine.** After pairing, session titles, project paths, conversation text,
  and live terminal frames are end-to-end encrypted to the phone. The optional public relay only
  forwards ciphertext and opaque routing identifiers; it cannot read prompts or code. Push
  notification bodies are sealed the same way and decrypted on-device by the notification service
  extension.
- **What stays local.** Long-term identity keys and the paired-device list live under the OS state
  directory (`~/.local/state/corral/remote/`, overridable with `CORRAL_STATE_DIR`). They are not
  uploaded. Older installs that kept these under `~/.cache/corral/remote/` are migrated once.
- **Trust boundary.** A paired phone can drive the same hosted agent sessions you run locally
  (including launches that skip permission prompts). Treat the pairing QR code as a root
  credential: one-time, ten-minute expiry. Use `corral remote pair --readonly` for view-only
  access, and `corral remote unpair` to revoke a lost phone (the daemon drops that device within
  a few seconds).
- **Network defaults.** The host dials out to a `wss://` relay; LAN direct is plaintext `ws://`
  under the same application-layer encryption. Cleartext relays require an explicit
  `--insecure-relay` flag.

## Repository Hygiene

Do not commit real session history, generated caches, logs, tokens, API keys, or local environment files. The project `.gitignore` excludes common local artifacts, but contributors should still review changes before publishing.
