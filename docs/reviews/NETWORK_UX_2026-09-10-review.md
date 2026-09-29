# Network UX Implementation Review

Scope: uncommitted CLI, iOS, and relay changes against the [detailed design](../design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md#7-detailed-network-ux-design-proposal). Implementation was still changing during this review. Baseline commits were CLI `7a23c28`, iOS `87f0f8e`, relay `cd6f915`; this is not approval of a release or an installed build.

Decision: **corrections applied; release candidate once the full CLI gate and the named regression tests stay green**. Four findings were in the inspected source at review time. No product source was modified by this review document itself. Corrections are recorded below (hashes in Source Snapshot are pre-fix). Public relay deploy remains deferred until the device path is healthy.

## Findings

### P1: Failed input injection is reported as delivered

Location: `cli/src/corral/remote/service.py`, `_run_receipted_input`, lines 831-845; underlying `cli/src/corral/embed.py`, `_send` and `paste`, lines 527-558.

The new receipt wrapper treats a normal return from `SessionHub.send_text` as delivery evidence. The real helpers suppress OS errors/timeouts and ignore subprocess exit status. Consequently a failed paste and failed Enter can both return normally and produce a durable `delivered` receipt. This is a new false guarantee even though the old helper behavior predates the change.

Reproduced with the existing receipt wire fixture, actual `SessionHub.send_text`, a fake pane lookup, and `corral.embed.subprocess.run` patched to raise `OSError("injection failed")`. Result: `ok: true`, `status: delivered`. No real pane or assistant was touched.

Required correction: propagate explicit injection outcomes; declare delivery only with evidence. Failures after a possible partial side effect remain unknown. Add failure/timeout/nonzero-exit tests through the real adapter boundary, not only the FakeHub success path.

### P1: A lost response destroys pending-command reconciliation

Location: `ios/Corral/Views/Chat/SessionDetailView.swift`, `submitText` catch paths, lines 291-304 (also `submitPrompt`); `ios/Corral/Store/PendingCommandStore.swift`, `updateDelivery`.

On any transport exception, the view marks the command rejected and restores its text to the draft. The pending store maps rejection to failed and removes the durable record. If the host executed the command but its reply was lost, reconnection no longer queries its status. Pressing the ordinary retry/send action allocates a new UUID, bypassing host deduplication and potentially executing the same instruction twice.

Required correction: distinguish an authoritative host rejection from timeout/disconnection. Keep the original command identity and mark ambiguous transport outcomes pending/needs-review; query it after reconnect. Do not expose an ordinary retry that silently turns an unknown operation into a fresh operation. Cover lost acceptance/delivery replies and app restart. This finding is source-traced; no iPhone fault-injection test was run during review.

### P1: A valid large legacy frame disconnects the shared host

Location: `relay/internal/hub/queue.go`, `queuedSink.Send`, lines 44-52; `hub.go` interaction budget and primary lane routing.

The existing wire accepts frames up to 8 MiB, but the new primary destination queue closes its underlying socket when queued bytes exceed 256 KiB. Even an empty queue cannot accept a single 300 KiB frame. Legacy clients and current image submission can still send an unchunked payload on the interaction connection; closing that shared host socket disconnects all attached phones, not merely the sender. The same mismatch exists between bulk's 512 KiB queue and unchunked larger responses.

Reproduced in an isolated copy with `TestReviewLegacyLargeFrameMustNotDropHost`: register a host, attach a legacy device, send a 300 KiB DATA payload through `FromDevice`; the shared host sink is closed. The function even returns nil after the reset.

Required correction: negotiate frame/chunk limits before enforcing incompatible budgets, preserve legal legacy traffic, and make overflow isolation match the intended scope. Test incompressible image payloads and large history replies with another phone active. A general fair-scheduler type does not establish fairness unless it actually schedules bytes.

### P2: Old bulk-connection cleanup closes its replacement

Location: `relay/internal/hub/hub.go`, `AttachHostLane` lines 166-171 and `DetachHostLane` lines 177-198; `internal/server/server.go`, deferred detach in `handleHostAttachV2`.

Attaching a replacement closes the previous bulk connection. Its handler then runs deferred cleanup identified only by host ID, registration generation, and lane name. Those values also match the replacement, so cleanup removes and closes the new lane. This can repeatedly break bulk reconnection without affecting the primary generation.

Reproduced in isolated copy with `TestReviewOldBulkDetachMustNotCloseReplacement`: attach bulk A, replace with B under the same host generation, run A's detach; B is closed.

Required correction: return an attachment identity/handle and detach only if it still owns the current lane. Verify old-handler cleanup cannot remove a newer attachment, including during failed writes and replacement overlap.

## Corrections applied

Source fixes for the four findings above (not a release approval; versions unchanged at CLI 0.24.171 / iOS 1.0.21):

1. **Failed input injection reported as delivered** — `embed.paste` / `send_key` / `_send` now return `bool` success; `SessionHub.send_text` raises `ActionError` on total failure and `PartialInjectionError` when paste succeeded but Enter failed; `_run_receipted_input` marks delivered only on proven success (`rejected` / `unknown` otherwise). Tests: `test_embed_oserror_receipt_not_delivered`, `test_partial_paste_enter_marks_unknown` in `cli/tests/test_command_receipts.py`.
2. **Lost response destroys pending reconciliation** — transport/timeout failures map to `unknown` → `needsReview` and keep the durable `command_id`; authoritative `RemoteCallError` still rejects; needsReview retry resends the same id. Tests: `PendingCommandStoreTests` (`testTransportFailureKeepsDurableRecordAsNeedsReview`, `testAuthoritativeRejectionRemovesDurableRecord`, `testNeedsReviewSurvivesAppRestartLoad`).
3. **Valid large legacy frame disconnects host** — interaction/bulk destination queue ceilings raised to `protocol.MaxFrameBytes` (8 MiB). Test: `TestLegacyLargeFrameMustNotDropHost`. Protocol note synced in `relay/docs/PROTOCOL_V2.md`.
4. **Old bulk detach closes replacement** — `AttachHostLane` returns a unique `attachID`; `DetachHostLane` only closes when it still matches. Test: `TestOldBulkDetachMustNotCloseReplacement`.


- Existing receipt suite: 15 tests passed using the project's `.venv`, isolated temporary state, and `PYTHONPATH=src`. System Python lacked `sesskit`; that environment error was resolved by using the project environment.
- Existing relay packages: `go test -race ./internal/hub ./internal/protocol ./internal/server` passed.
- Additional relay regression tests: both failed as described above, in `/tmp/corral-review-relay-20260910/internal/hub/review_regressions_test.go`. Temporary tests were not added to product source.
- Real adapter failure simulation: reproduced false delivery using fake subprocess failure and isolated state.
- The missing command ID on iOS keyboard input was corrected by the implementation agent during review and is excluded from open findings. It previously reproduced `usage_error: Missing command_id`.
- No iOS build, physical-device/network acceptance, live deployment verification, or public relay change was performed. The implementation's own current taskboard still records integration work and public-device-path 503; that 503 was not independently re-probed here.
- Full completion still needs actual two-leg bulk routing (including phone lane selection), bounded host sends, recovery after bulk disconnect, command leases/retention, cache cursor consistency, and installed-build acceptance. These are remaining review/acceptance areas, not additional independently reproduced findings in this report.

## Source Snapshot

SHA-256 at final inspection, to distinguish subsequent corrections:

| File | Hash |
|---|---|
| CLI `remote/service.py` | `f84fb79e758a326712077e4327c0bcfac59ed4c1e2a717ac5adf8d0e6f08a26e` |
| iOS `SessionDetailView.swift` | `f714cb4e1c4a046ef145e6efcb824acdaff25cfcf7ec3e84b990541022c669c4` |
| iOS `PendingCommandStore.swift` | `4be56088fe0096c6bb8689906d2a2978c4a0099dac6dab2cbb83ca47abab2715` |
| Relay `hub.go` | `e8734e7da0c5c8ffbb86944406a0ce693cc2f7900118bbb17185b953a016fd77` |
| Relay `queue.go` | `c8037895e56c878f9ee3d9c92f3e85cf5b5df757ec9235d8d6de55cd3fa6f6b9` |
