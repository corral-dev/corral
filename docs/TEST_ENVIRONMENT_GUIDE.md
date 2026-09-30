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
  move into older/collapsed buckets over time.
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

## Running the isolated acceptance

Run `python3 scripts/acceptance.py` from `cli/`. The command owns its temporary
directories, isolated tmux socket, synthetic terminal process, screenshots,
and cleanup. It does not replace the complete UI/tmux suite or release checks;
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
