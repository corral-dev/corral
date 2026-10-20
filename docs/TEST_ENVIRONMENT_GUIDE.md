# Test environment and behavioral acceptance

This guide defines how Corral's reusable UI acceptance run stays isolated from
developer data and live hosted sessions. The owner-facing entry is
`python3 scripts/acceptance.py`; it is a test helper, not a product command or
replacement test framework.

## Durable requirements

- Before opening the UI, use a disposable fixture root for Corral cache/state,
  session data and artifacts, and set managed-host discovery to isolation mode.
  Never enumerate, attach to, stop, or otherwise modify the user's real hosted
  sessions.
- Every real-terminal scenario uses a unique tmux socket and session names.
  Start a synthetic, deterministic command on that socket and verify it is
  alive and its captured screen contains the expected fixture marker before
  treating any UI assertion as meaningful. Cleanup must run on success, failure,
  and interruption without touching a different socket.
- Generate fixture timestamps from the current run or pass an explicit clock to
  pure date-bucket helpers. Do not use fixed historical dates that silently
  move into older/collapsed buckets over time. Acceptance fixtures use
  `live=False` and recent relative mtimes so the sidebar buckets stay visible
  throughout the run.
- The headless UI probe routes real capture, liveness, control-channel, and
  resize operations for its synthetic pane names to the unique socket. It first
  verifies each backend's live process and screen marker with raw tmux calls.
  Every other name retains the original resolver, so user-hosted sessions are
  never redirected. Both UI and terminal sides use synthetic markers and never
  read real session history.
- Exercise closing a focused and an unfocused member in a real multi-pane group,
  including 3→2 and 2→1 transitions. Validate the mounted viewport and fixture
  count first; record that the click was accepted, the scroll position before
  and after plus representative scroll samples, the surviving selection, and
  that the closed member's synthetic backend remains alive.
- Capture synthetic-only before/after evidence with the maintained screenshot
  renderer. Do not capture user conversations or use ad-hoc operating-system
  screenshots. Keep output artifacts outside the repository by default.
- Emit a concise JSON result with a stable failure category (`environment/setup`,
  `product assertion`, or `timeout`), elapsed time, fixture/precondition facts,
  actual interaction outcomes, and full log/screenshot paths. Keep routine
  stdout quiet; write complete diagnostics to a run log. Exit non-zero on any
  failed precondition or assertion.
- Keep tests for environment/helper behavior independent of the user's sessions
  and resources. Reuse existing isolation switches and maintained fixture
  capture paths where appropriate; do not import a large test module as an
  undocumented public fixture API.
- Real-terminal `selftest.sh` must use the same selected Python interpreter for
  its `sys.path` capture and for every isolated TUI launch inside tmux. The
  script replaces `PATH` inside the fixture environment, so a bare `python3`
  there can resolve to a different installation than the one that produced
  `PYTHONPATH`; versioned standard-library entries (for example a 3.12
  `lib/python3.12` tree) loaded by another interpreter (for example 3.14)
  abort before startup with `AssertionError: SRE module mismatch`.
  `PYTHONPATH` entries apply to every interpreter on the machine (see
  <https://docs.python.org/3/library/sys_path_init.html> and
  <https://docs.python.org/3/using/cmdline.html#envvar-PYTHONPATH>), so the
  only safe shape is one absolute interpreter path, shell-quoted, reused for
  all three launches (main/direct/cursor). Do not hardcode a
  machine-specific interpreter or reintroduce bare `python3` in a tmux
  launch line.

## Resize-hold fixture ownership
A synthetic session name and a prefilled live grid are unit-test inputs, not a
running tmux host. Stop, wake and join the pane's capture worker before injecting
that state into a resize-hold test. Otherwise a slow runner can accumulate three
`capture=None` misses, correctly mark the nonexistent host dead, and cancel the
pending resize before the hold assertion. The same failure is reproducible with
a longer debounce and shorter polling interval; it is not evidence that a real
live host loses its hold. Keep the real Textual timer, resize-call assertion,
changing-frame rejection, minimum hold and stable-frame release assertions.
Separate capture-worker and real-tmux tests retain their existing coverage.

## Pilot drag-test coordinates

Textual `Pilot.mouse_down` / `mouse_up` each post a leading `MouseMove`
before the button event, and `widget + offset` coordinates resolve against
the widget's **current** region. While dragging a handle the region moves
with the width, so offsets relative to the handle double-count (verified
2026-10-15: `hover(handle, offset=(12, 0))` lands at handle-origin + 12,
but a trailing `mouse_up(handle, offset=(12, 0))` re-resolves against the
moved handle and drags 12 more). Write drag tests with screen-absolute
targets (`pilot.hover(None, offset=(x, y))`) and release with
`pilot.mouse_up(handle)` (offset `(0, 0)` makes the precursor move a
no-op). This is harness geometry, not product behavior: real terminals
send absolute coordinates. Regression: `SidebarResizeTests`.

## Running the isolated acceptance

Run from `cli/` (verified 2026-09-30, exit 0 in ~8s):

```bash
python3 scripts/acceptance.py --json --artifacts-dir <dir> --timeout 240
python3 scripts/acceptance.py --dry-run --json
```

Exit codes: 0 success, 1 setup/product-assertion failure, 2 usage, 6 timeout.
`--dry-run` is fully read-only (no tmux server, no temp dirs, no UI).

What the entry actually does:

- Starts 3 synthetic backends (`printf` marker + `sleep`) on a unique
  `corral-accept-<pid>-<rand>` socket and waits for the marker via raw
  `capture-pane` before touching the UI.
- Routes this run's session names to that socket through the maintained
  `tmux_argv_for_session` / `socket_for_session` hooks, so `embed.capture`,
  control channels, resize, and `liveness.is_alive` execute against real tmux.
  Nothing is mocked in the terminal path; all other names fall through to the
  original resolvers. Fixtures stay `live=False` with relative mtimes.
- Mounts a 3-pane group, waits until every pane grid renders the real marker,
  then runs s1 (unfocused 3→2), s2 (focused 3→2), s3 (unfocused 2→1) with real
  `_PaneClose` clicks, per-scenario before/after screenshots taken immediately
  around each click, and 3 post-click scroll samples that must all equal the
  pre-click offset or the scenario fails.
- Proves cleanup in JSON: server stopped and zero socket files, else failure.

It does not replace the complete UI/tmux suite or release checks;
it is a repeatable focused behavioral probe that can run separately from those
shared-socket checks.

Inspect the JSON summary for all preconditions and close scenarios. A green
result requires a live synthetic terminal with a verified screen marker, a
mounted scrollable sidebar, successful actual close-button clicks, the expected
remaining group members and selection, unchanged backend liveness, and readable
before/after screenshots. On failure, use the reported category and full log
path before deciding whether the issue is setup, timeout, or product behavior.

## Evidence boundary

Artifacts may contain only the synthetic fixture sessions and terminal output.
The runner must never read real session history. A successful focused run does
not stand in for mandatory complete verification or release acceptance; the
serialized integrator records those separate results and may rerun this entry
when shared UI/tmux jobs are idle.

## State-probe independence under a blocked scan

The state loop regression must prove at least four additional probes while the full-scan worker is still blocked, using a condition/event to observe probe progress with a bounded deadline. A fixed 0.3-second sleep does not establish that independence: the 2026-10-16 macOS/Python 3.10 cloud run and its isolated retry both observed exactly three probes and failed the `> 3` threshold, while the local full suite passed. Preserve the probe-count threshold, blocked scan, production loop and cleanup; synchronize on the observed progress instead of treating runner scheduling as product cadence. Runtime latency requirements remain separate and must not be relaxed by this fixture repair.

## Prompt navigation acceptance

Exercise the actual Your prompts row action in both a hosted native runtime and a static ended-session preview. Repeated short prompts and assistant quotations must resolve by message identity, not the first matching string. Keep the TUI/Mac floating card expanded after selection. Inspect pressed and released frames (including wrapped rows) in light/dark themes. For native-runtime navigation, submit the next synthetic message through the production input path and count exactly one matching native user record; draft preservation alone cannot detect accepted search modes that still own Enter. Include legacy Find with zero results as well as matching results. Concurrent probes must have independent native session IDs and history files, in addition to separate tmux sockets; shared Codex history can correctly produce its “open in another app” lock instead of testing navigation.

Synthetic uniform histories missed every real-structure failure found on 2026-10-19: tool output quoting the conversation, injected user-role context, long duplicate prompts, compaction and Claude render lag. Prompt-navigation acceptance must also run copies of real owner sessions in isolated runtime homes, across the session sizes, prompt counts and composer states (empty, draft, working) in use. The current private harness and its isolation recipe are described in [Current navigation design](EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md#current-navigation-design-2026-10-19-no-native-find).


## Watcher debounce contract

A fixed number of file writes does not establish a fixed number of native file
event batches or callbacks. The parallel gate observed three callbacks from the
five-write fixture without controlling notification arrival times.
Test the debounce boundary with an injected monotonic clock and explicit event
signals; retain the separate native file-write wakeup test for backend coverage.
Do not increase callback limits, enlarge sleeps or suppress diagnostics to make
this assertion pass. Temporary acceptance sockets must be removed after their
own server is confirmed stopped: `tmux kill-server` alone leaves a socket path,
which the existing dry-run no-stray assertion correctly reports.

Apple documents non-deterministic notification latency and event coalescing in
[Using the File System Events API](https://developer.apple.com/library/archive/documentation/Darwin/Conceptual/FSEvents_ProgGuide/UsingtheFSEventsFramework/UsingtheFSEventsFramework.html).
