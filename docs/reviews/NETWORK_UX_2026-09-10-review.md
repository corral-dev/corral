# Network UX Implementation Review

Scope: uncommitted CLI, iOS and relay changes against the [detailed design](../design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md#7-detailed-network-ux-design-proposal), while implementation was still changing. Baseline commits CLI `7a23c28`, iOS `87f0f8e`, relay `cd6f915`; not an approval of a release or an installed build.

Decision: **corrections applied; release candidate once the full CLI gate and the named regression tests stay green.** Four findings were present in the inspected source; this document modified no product source. Hashes in Source Snapshot are pre-fix. Public relay deploy stays deferred until the device path is healthy.

## Findings

### P1: Failed input injection is reported as delivered — corrected

Location: `cli/src/corral/remote/service.py` `_run_receipted_input`; underlying `cli/src/corral/embed.py` `_send` and `paste`.

The receipt wrapper treated a normal return from `SessionHub.send_text` as delivery evidence, but the helpers suppress OS errors/timeouts and ignore subprocess exit status, so a failed paste and a failed Enter both returned normally and produced a durable `delivered` receipt — a new false guarantee on old helper behavior. Reproduced with a fake pane lookup and `corral.embed.subprocess.run` patched to raise `OSError("injection failed")`: `ok: true`, `status: delivered`, no real pane or assistant touched.

Required: propagate explicit injection outcomes and declare delivery only with evidence; failures after a possible partial side effect stay unknown. Add failure/timeout/nonzero-exit tests through the real adapter boundary, not only the FakeHub success path.

### P1: A lost response destroys pending-command reconciliation — corrected

Location: `apple/iOS/Views/Chat/SessionDetailView.swift` `submitText` catch paths (also `submitPrompt`); `apple/iOS/Store/PendingCommandStore.swift` `updateDelivery`.

On any transport exception the view marked the command rejected and restored its text to the draft, and the pending store mapped rejection to failed and removed the durable record. If the host executed the command but its reply was lost, reconnection no longer queried its status, and the ordinary retry/send action allocated a new UUID, bypassing host deduplication and potentially executing the same instruction twice.

Required: distinguish authoritative host rejection from timeout/disconnection, keep the original command identity, mark ambiguous transport outcomes pending/needs-review and query after reconnect; never expose a retry that silently turns an unknown operation into a fresh one. Cover lost acceptance/delivery replies and app restart. Source-traced; no iPhone fault injection was run during review.

### P1: A valid large legacy frame disconnects the shared host — corrected

Location: `relay/internal/hub/queue.go` `queuedSink.Send`; `hub.go` interaction budget and primary lane routing.

The wire accepts frames up to 8 MiB, but the new primary destination queue closes its socket when queued bytes exceed 256 KiB, so even an empty queue cannot accept a single 300 KiB frame. Legacy clients and current image submission can still send an unchunked payload on the interaction connection, and closing that shared host socket disconnects all attached phones, not just the sender. The same mismatch exists between bulk's 512 KiB queue and unchunked larger responses.

Reproduced in an isolated copy with `TestReviewLegacyLargeFrameMustNotDropHost`: a legacy device sends a 300 KiB DATA payload through `FromDevice`, the shared host sink closes, and the function still returns nil.

Required: negotiate frame/chunk limits before enforcing incompatible budgets, preserve legal legacy traffic, and make overflow isolation match the intended scope. Test incompressible image payloads and large history replies with another phone active. A general fair-scheduler type does not establish fairness unless it actually schedules bytes.

### P2: Old bulk-connection cleanup closes its replacement — corrected

Location: `relay/internal/hub/hub.go` `AttachHostLane`/`DetachHostLane`; `internal/server/server.go` deferred detach in `handleHostAttachV2`.

Attaching a replacement closed the previous bulk connection, whose handler then ran deferred cleanup identified only by host ID, registration generation and lane name — values that also match the replacement — so cleanup removed and closed the new lane, repeatedly breaking bulk reconnection without touching the primary generation. Reproduced with `TestReviewOldBulkDetachMustNotCloseReplacement`: attach bulk A, replace with B under the same host generation, run A's detach, B closes.

Required: return an attachment identity/handle and detach only if it still owns the current lane; verify old-handler cleanup cannot remove a newer attachment, including during failed writes and replacement overlap.

## Corrections applied

Not a release approval; versions unchanged at CLI 0.24.171 / iOS 1.0.21.

1. **Failed injection reported as delivered** — `embed.paste` / `send_key` / `_send` return `bool` success; `SessionHub.send_text` raises `ActionError` on total failure and `PartialInjectionError` when paste succeeded but Enter failed; `_run_receipted_input` marks delivered only on proven success (`rejected` / `unknown` otherwise). Tests `test_embed_oserror_receipt_not_delivered`, `test_partial_paste_enter_marks_unknown` in `cli/tests/test_command_receipts.py`.
2. **Lost response destroys reconciliation** — transport/timeout failures map to `unknown` → `needsReview` and keep the durable `command_id`; authoritative `RemoteCallError` still rejects; needsReview retry resends the same id. Tests `PendingCommandStoreTests` (`testTransportFailureKeepsDurableRecordAsNeedsReview`, `testAuthoritativeRejectionRemovesDurableRecord`, `testNeedsReviewSurvivesAppRestartLoad`).
3. **Valid large legacy frame disconnects host** — interaction/bulk destination queue ceilings raised to `protocol.MaxFrameBytes` (8 MiB). Test `TestLegacyLargeFrameMustNotDropHost`; protocol note synced in `relay/docs/PROTOCOL_V2.md`.
4. **Old bulk detach closes replacement** — `AttachHostLane` returns a unique `attachID`; `DetachHostLane` only closes when it still matches. Test `TestOldBulkDetachMustNotCloseReplacement`.

## Check results and remaining areas

- Existing receipt suite: 15 tests passed with the project `.venv`, isolated temporary state and `PYTHONPATH=src`.
- Existing relay packages: `go test -race ./internal/hub ./internal/protocol ./internal/server` passed.
- Additional relay regression tests both failed as described, in `/tmp/corral-review-relay-20260910/internal/hub/review_regressions_test.go`; those temporary tests were not added to product source.
- Real adapter failure simulation reproduced false delivery using a fake subprocess failure and isolated state.
- The missing command ID on iOS keyboard input was corrected by the implementation agent during review and is excluded from open findings (it previously reproduced `usage_error: Missing command_id`).
- No iOS build, physical-device/network acceptance, live deployment verification or public relay change was performed. The implementation's taskboard still records integration work and a public-device-path 503 that was not independently re-probed here.
- Full completion still needs actual two-leg bulk routing (including phone lane selection), bounded host sends, recovery after bulk disconnect, command leases/retention, cache cursor consistency and installed-build acceptance — remaining review/acceptance areas, not additional reproduced findings.

## Source Snapshot

SHA-256 at final inspection, to distinguish subsequent corrections:

| File | Hash |
|---|---|
| CLI `remote/service.py` | `f84fb79e758a326712077e4327c0bcfac59ed4c1e2a717ac5adf8d0e6f08a26e` |
| iOS `SessionDetailView.swift` | `f714cb4e1c4a046ef145e6efcb824acdaff25cfcf7ec3e84b990541022c669c4` |
| iOS `PendingCommandStore.swift` | `4be56088fe0096c6bb8689906d2a2978c4a0099dac6dab2cbb83ca47abab2715` |
| Relay `hub.go` | `e8734e7da0c5c8ffbb86944406a0ce693cc2f7900118bbb17185b953a016fd77` |
| Relay `queue.go` | `c8037895e56c878f9ee3d9c92f3e85cf5b5df757ec9235d8d6de55cd3fa6f6b9` |

<!-- 该文档整理/压缩于 2026-11-15 -->
