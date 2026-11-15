# Memory budgets and resource lifetime

## Goal and scope (owner authorization, 2026-10-25)

Optimize measured memory costs across the Python developer-host service, TUI, SessKit native-history readers and both Swift clients. Preserve complete native history, pending commands, drafts, reading position, native selection, terminal continuity, exact search semantics and cross-runtime handoff. The Go relay is already small and needs no speculative rewrite.

The coordinator owns integration, source review, full verification, installed acceptance and delivery; OpenCode workers may implement independently owned non-UI slices, while native/TUI interface implementation, pane presentation and visual acceptance stay with the primary model. Do not edit another task's files, change frame pacing, terminate agent sessions or discard foreign changes to improve a memory number.

## Measured baseline

A read-only audit of local CLI 0.24.353 / SessKit 0.2.19 and installed Mac 0.3.148 (152) collected 121 samples over 610.9 s: physical-footprint medians remote daemon 311 MiB, TUI 222 MiB, scanner 78 MiB, native Mac 279 MiB; concurrent total 905 MiB, sampled maximum 1081 MiB. Existing workloads changed during measurement, so these are not idle or before/after benchmarks.

Isolated no-tracing cold history reads requesting 80 messages measured 234 MiB for a 60.8 MiB Claude JSONL and 301 MiB for a 63.1 MiB Codex JSONL, from ~28 MiB import baselines; Pi's 38 MiB file reached 166 MiB. Releasing readers reduced traced live allocations to near zero even when process footprint stayed elevated — allocator high-water retention is not automatically an unreachable leak.

Prepared search snapshots retained ~17.6 MiB (567 sessions, depth 50) and 30.6 MiB (997 sessions, depth 200); available plain-conversation rows represent another potential 8.8/14.9 MiB, not a measured live duplicate in every process. A stalled-sink probe of the real Python relay writer retained ~4.2/17.0/33.9 MiB for 64/256/512 blocked 64 KiB sends with no admission ceiling. Two older Suzhou review supervisors each retained ~1020 MiB PSS plus 240 MiB swap while waiting five days for missing native history and an absent tmux socket (heap owner not established); Seoul's relay used ~18 MiB cgroup memory.

The native Mac breakdown included 173 MiB Malloc Small and 90 MiB IOSurface. `leaks` found zero unreachable allocations, which does not clear reachable retention; terminal buffer data was about 2 MiB, so cutting scrollback is not the first remedy. The physical iPhone app could not launch while locked, so no phone footprint or savings percentage is established.

## Ownership and contracts

### 1. Bounded delegation observation

Corral owns supervisor lifetime, not native history interpretation. An already submitted run that cannot bind native history must not keep a heavy observer alive indefinitely just because pane liveness is unknown. Use the existing bounded dispatch-observation budget for this unbound state: persist an honest recoverable `unknown` outcome with an evidence-specific error and exit the supervisor; apply equivalent bounded handling to a continuously unavailable bound reader where appropriate. Do not report completion or terminal death, replay input, erase the run, clear session ownership speculatively, or send an interrupt merely because a socket is absent.

`run recover` already permits unknown runs and must restart observation without resending the original brief; preserve append-only actor-attributed audit updates in the same transaction. Native history reappearing before the budget must bind normally, and a valid bound long-running agent is not subject to an arbitrary task-duration timeout. Test unbound, transient unknown, recovery, queued work and shutdown explicitly. Reconcile obsolete review runs only after source verification with exact run/process identity, current missing-target evidence and the maintained audited store/API — never broad-name process cleanup.

### 2. Ordered bounded transport

Corral's Python and Swift application queues need byte and count admission, not just socket write limits. One ordered writer owns each physical transport/lane; encryption counters and wire send ordering stay aligned. Queued plus in-flight work must have explicit limits, including one legal maximum-sized frame. An overloaded destination fails/rebinds independently; never silently drop encrypted frames or report an unsent command as delivered.

Multicast event consumers need bounded retention plus an explicit gap/snapshot/resync path; a bare `bufferingNewest` replacement that silently loses conversation, list, receipt or terminal events is unacceptable, and recovery notifications must themselves be coalesced and bounded. Stop/deinit/connection replacement must release queued frames and complete pending outcomes exactly once, and normal local/LAN traffic must not reconnect merely because budgets are too small. Reuse the current Go relay's per-destination byte queue and the existing Python/Swift ordered writer as references, adapting lifecycle and failure semantics rather than extracting product-independent code. No new service or shared framework.

### 3. Plain-conversation cache

Corral's `SessionStore` owns derived plain-conversation retention: deterministic byte-weighted LRU admission/eviction, and one-shot search preparation must not retain every full conversation. Preserve existing cache lookup/invalidation, unavailable-history behavior, short Chinese substring queries, role/timestamp snippets, exact search counts and standalone TUI operation; do not replace the search engine or require a daemon for the TUI.

Cache budgets are internal acceleration policy, not user settings. Oversized individual histories stay readable but must not become permanent unbounded entries. Tests cover hit recency, invalidation, concurrent loads, oversized bypass, removal and indexing behavior.

### 4. Remote normalized transcript cache

Corral owns product byte budgets across inactive transcripts, reader lifetimes, expensive read admission and incremental replay — the existing cap of 48 entries is not a byte budget. Protect actively watched/pending work, release unneeded heavyweight readers and make idle retention byte-aware, counting tool bodies and reader state, not only visible text.

Do not cut the open conversation's earlier pages, alter global message sequences or generations, discard pending tool correlations, or duplicate native parsers. An evicted derived cache must reconstruct correct history through SessKit while keeping persisted-cache acceleration, and global expensive-read admission must not queue unbounded work or let bulk history block input/control requests.

### 5. Native-history allocation

SessKit owns JSONL/SQLite interpretation and incremental readers. Eliminate redundant full-file byte/split-line/parsed-row buffers where the existing feeds consume records incrementally, preserving complete-file and active-branch semantics, partial trailing records, line evidence, generation/reset rules, page order, cross-page tool pairing, error evidence and no-change polling. Do not claim a genuinely bounded native tail if the implementation still materializes the generation.

Prefer a measured streaming improvement with unchanged public reader contracts over a speculative redesign; any retained-event/window change needs complete semantic parity and explicit page reconstruction. Publish a versioned artifact, update Corral's complete pin and invalidate derived data through the existing provider cohort before installed acceptance.

### 6. Native UI resources (primary-owned)

Shared Swift networking/cache changes are independently non-UI; native pane/view, image presentation, text/layout and switching behavior stay primary-owned. Evaluate aggregate image/cache cost and memory pressure with the existing AppKit/UIKit and NSCache APIs — per-loader soft cost limits and a four-dormant-pane count alone do not bound the application.

Evict only rebuildable resources under pressure, preserving drafts, reading state, native selection and terminal state. Do not use view teardown, frame throttling, shortened scrollback or a new UI state as a cosmetic remedy, and preserve the documented 400-message on-disk snapshot budget without applying it to an open conversation. Measure visible and dormant resource owners before claiming whole-client savings.

## Acceptance and delivery

1. Keep synthetic regression fixtures free of private history. Native-history probes emit counts, sizes and timings only; use read-only live sources and isolated writable caches.
2. Compare baseline/candidate on the same immutable data for cold read, warm no-change poll, append, history replacement, tool detail and backward paging, recording retained Python allocations, allocation peak, physical footprint and latency separately. Include all five active Corral runtimes while preserving SessKit's independent supported runtime contract.
3. Prove stalled socket/event consumers remain bounded and recover with no false delivery, ciphertext loss, missing terminal bytes or duplicate input; test reconnect and stop while queued work exists.
4. Verify TUI and both native clients on the real user path — builds/tests alone do not prove rendering or interaction. Report phone locking/unavailability separately and deliver an actual signed IPA through the documented shelf path when direct installation cannot run.
5. Run complete current-source/environment gates and inspect all warnings; commit and push only this task's verified changes, install reproducible committed artifacts, restart only owned/necessary services and measure installed behavior. Public releases use the release train, not manual tags.
6. Assess reductions from measured before/after evidence, not the sum of independent theoretical maxima, and retain explicit limits where a platform, real workload or allocation owner cannot be measured.

## Reference sources

- Apple footprint and VM accounting: <https://developer.apple.com/videos/play/wwdc2022/10106/>
- Apple allocation generations and retaining graphs: <https://developer.apple.com/documentation/xcode/gathering-information-about-memory-use>
- Python allocation tracing: <https://docs.python.org/3/library/tracemalloc.html>
- websockets memory/backpressure: <https://websockets.readthedocs.io/en/stable/topics/memory.html>
- AsyncStream unbounded policy: <https://developer.apple.com/documentation/swift/asyncstream/continuation/bufferingpolicy/unbounded>
- NSCache advisory limits: <https://developer.apple.com/documentation/foundation/nscache/totalcostlimit>

Private diagnostic artifacts and repeatable probes: `~/.config/corral/development/memory-audit-20261025/` — diagnostic evidence, not a product dependency.

## Implementation findings (2026-10-25)

Numbers below are controlled isolated probes or fixtures recorded in the slice reports under the private audit directory, not live whole-process savings.

### Bounded delegation observation

An already submitted run with no usable native history now settles a recoverable `execution=unknown` after 150 s of continuous missing/unreadable history and exits. Any successful bind or readable poll restarts the budget, so a valid long-running agent is never cut by task duration. Delivery, host, session ownership, native id, messages and questions stay untouched; nothing is resent or interrupted. Tests cover unbound, bound-but-unreadable, transient recovery, positive terminal death, external settlement, recovery without replay and a 900 s readable task. The two obsolete review supervisors were allocation-profiled by lifetime only, not by retained owner; ending them released process memory with no per-iteration leak claimed.

### Ordered bounded transport

One ordered `LaneWriter` per physical socket; per-channel 8 MiB/128 frames, per-lane 12 MiB/256 frames, per-transport 32 MiB/512 frames, counting queued and in-flight work. A stalled sink receiving 512 × 64 KiB frames now retains 12.31 MiB with one pending send task instead of 33.90 MiB with 512 tasks; overflow resets only the affected channel or lane and the client rebinds. `HostChannel` inbound admission is byte-aware and counts the processing frame; data-plane overflow resets that plane while the control connection survives. Encrypted frames are never dropped from a live counter sequence.

### Plain-conversation cache

`SessionStore.conversations` is an 8 MiB byte-weighted LRU with single-flight reads and removal fencing. One-shot search preparation reads through `get_conversation_for_index()`, which reuses a valid entry without promoting it and never admits a fresh full conversation. A 997-row real catalog measured 14.9 MiB traced retention unbounded versus 7.2 MiB admitted; oversized individual histories remain complete but uncached. Search text, counts, Chinese substring semantics and standalone TUI operation are unchanged.

### Remote transcript cache

Inactive transcripts now have a 64 MiB byte budget beside the 48-entry cap. Watched entries and the single newest unprotected entry are protected; eviction drops the whole reconstructible record. When the last watcher leaves and the estimate exceeds 4 MiB, heavyweight reader/full state is released while cursors, generations, pending tool pairing and projected messages stay valid. A 24 × 4,000-message fixture measured 96.8 → 32.4 MiB traced current (process footprint 175.5 → 86.4 MiB); five live runtimes reopen without duplicate messages. History/image/search reads share one process-wide 8-slot admission with immediate retryable refusal, while metadata, input and terminal control stay outside it. An open conversation is never clipped.

### Native-history allocation

SessKit reads JSONL/Pi cold builds in 1 MiB batches and feeds parsed rows in bounded batches instead of holding whole-file bytes, every split line and all parsed rows together. On the largest real samples, cold open plus an 80-event page reduced traced peak from 195.7/238.4/125.4 MiB to 36.3/54.8/63.9 MiB (Claude/Codex/Pi) and process footprint from 223.2/291.6/160.1 MiB to 70.6/107.7/125.6 MiB. Materialized events and Pi's native entry tree remain retained by contract; no bounded-tail claim is made, and a single huge line still peaks with the line.

### Native client resources

Mac warm panes are costed by their whole retained payload (text, full tool bodies, question projections, per-message objects): at most 4 MiB per pane, 12 MiB for all dormant panes, four panes maximum. Image previews use one app-wide 48 MiB cache instead of a 48 MiB limit per conversation; Markdown, image-reference and list-preview caches carry byte costs. System memory pressure releases only those caches and dormant panes, while the open pane keeps subscriptions, drafts, reading position, native selection and terminal state. The 400-message on-disk snapshot budget is unchanged and not applied to an open conversation.

### Retained and deliberately unchanged

- Retained allocator high-water is not a leak: releasing readers drops traced reachable allocations sharply while process footprint can stay elevated.
- The Go relay remains small (~18 MiB cgroup memory) with 8 MiB per-destination queues; no speculative rewrite.
- The prepared search snapshot keeps original-case lines plus a lowercase blob (17–30 MiB at depth 50–200); dropping role/time evidence or replacing the substring engine is not justified by this measurement.
- Watched conversations and the single newest oversized transcript can exceed the soft budgets by design; several simultaneously watched huge sessions are the documented residual case.

## Delivery acceptance (2026-10-25)

- CLI source gate: 2,899 tests, 0 failures, 2 skips, coverage complete. Seven first-pass UI/remote tests passed on the isolated retry and were classified as the known load-sensitive set, not regressions.
- SessKit 0.2.21 published (wheel `9e54d47f…`, sdist `c33a954a…`, downloaded back and digest-verified) and pinned in every install path; the checkout `.venv` and pipx venv both report 0.2.21. SessKit gate: 626 passed, ruff clean.
- Portable Apple Core: 448 tests, 0 failures. Mac universal Release signed with the existing Developer ID and installed at `/Applications/Corral.app`; executable hash matches the export, one GUI copy reports local transport ready with the data plane attached. A concurrent release-prep task then installed its later 0.3.149 (153) integration build over it; the running binary contains `ClientMemoryPressure`/`PlaneOutbox`/`eventGap`, so the slice is present, but final installed visual/footprint acceptance belongs to that later build. iOS Release built and signed; direct install failed because the phone was locked (CoreDevice 10003/12040), so the real IPA was uploaded to AppShelf (release 343, `https://apps.caozc.com/a/corral`) — shelf success is not phone installation.
- Mac rendering exercise used the existing Debug inspection harness: 24 real pane cases at 1/2/4 panes all rendered non-blank with no overflow/overlap, and an in-app pane capture was inspected. Desktop window capture remained unavailable to this execution host (Screen Recording permission), so no OS window screenshot is claimed.
- Installed footprint (physical, `rusage_info_v4`): Mac 184.4 MiB median over 60 s with six live subscriptions (the audit's comparable median was 279.0 MiB, not a controlled same-workload comparison); the same build with no detail selected measured 72.2 MiB. Remote daemon 288–412 MiB during its first seven minutes while scanning and serving ten hosted sessions; scanner 168–183 MiB; the still-open TUI (old 0.2.20 code) 298–328 MiB. A later 60 s sample of the integrated 0.3.149 build with four subscriptions measured 224.0 MiB median, the restarted daemon 318.9 MiB, the scanner 190.3 MiB. These installed numbers do not establish a whole-process reduction; bounded retention is established by the isolated probes and fixtures above.
- The open TUI process predates the 0.2.21 provider cohort and keeps working with full-scan fallback until relaunched; no user TUI or agent session was killed. The remote service and scanner were restarted onto the new code; clients reconnected over the local transport.
- Two obsolete review supervisors on the Suzhou host were reconciled after exact process/run identity, missing native history, empty review project directories and the absent keepalive socket were re-verified: SIGTERM to PIDs 277898/280535 released 2.26 GiB of used RAM and 487 MiB of swap. Their run rows remain untouched/recoverable; no database or other process was edited.

<!-- 该文档整理/压缩于 2026-11-15 -->
