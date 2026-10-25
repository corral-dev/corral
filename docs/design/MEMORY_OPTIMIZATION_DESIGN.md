# Memory budgets and resource lifetime

## Goal and scope (owner authorization, 2026-10-25)

Optimize the measured memory costs across the Python developer-host service, TUI, SessKit native-history readers and both Swift clients. Preserve complete native history, pending commands, drafts, reading position, native selection, terminal continuity, exact search semantics and cross-runtime handoff. The Go relay is already small and does not need a speculative rewrite.

The coordinator owns integration, source review, full verification, installed acceptance and delivery. OpenCode workers may implement independently owned non-UI slices. Native/TUI interface implementation, pane presentation and visual acceptance remain with the primary model. Do not edit another task's files, change frame pacing, terminate agent sessions or discard foreign changes to improve a memory number.

## Measured baseline

A read-only audit of local CLI 0.24.353 / SessKit 0.2.19 and installed Mac 0.3.148 (152) collected 121 samples over 610.9 seconds. Physical footprint medians: remote daemon 311 MiB, TUI 222 MiB, scanner 78 MiB, native Mac 279 MiB; concurrent total 905 MiB, sampled maximum 1081 MiB. Existing workloads changed during measurement, so these are not idle or before/after benchmarks.

Fresh isolated no-tracing cold history reads requesting 80 messages measured process footprints of 234 MiB for a 60.8 MiB Claude JSONL and 301 MiB for a 63.1 MiB Codex JSONL, from approximately 28 MiB import baselines. Pi's 38 MiB file reached 166 MiB. Releasing readers reduced traced live allocations to near zero even when process footprint remained elevated; allocator high-water retention is not automatically an unreachable leak.

Prepared search snapshots for the current narrowed/preserved catalogs retained approximately 17.6 MiB (567 sessions, depth 50) and 30.6 MiB (997 sessions, depth 200). Available plain-conversation rows represent another potential 8.8/14.9 MiB, not a measured live duplicate in every process. A stalled-sink probe of the actual Python relay writer retained approximately 4.2/17.0/33.9 MiB for 64/256/512 blocked 64 KiB sends, with no admission ceiling.

Two older Suzhou review supervisors each retained approximately 1020 MiB PSS plus 240 MiB swap while waiting five days for missing native history and an absent tmux socket. Their specific heap allocation owner was not established. Seoul's relay used approximately 18 MiB cgroup memory.

The native Mac initial breakdown included 173 MiB Malloc Small and 90 MiB IOSurface. `leaks` found zero unreachable allocations; this does not clear reachable retention. Terminal buffer data accounted for about 2 MiB, so cutting scrollback is not the first remedy. The physical iPhone main app could not launch because it was locked; no phone footprint or savings percentage is established.

## Ownership and contracts

### 1. Bounded delegation observation

Corral owns supervisor lifetime, not native history interpretation. An already submitted run which cannot bind native history must not keep a heavy observer alive indefinitely solely because pane liveness is unknown. Use the existing bounded dispatch-observation budget for this unbound state; persist an honest recoverable `unknown` outcome with an evidence-specific error and exit the supervisor. Apply equivalent bounded handling to a continuously unavailable bound reader where appropriate. Do not report completion or terminal death, replay input, erase the run, clear session ownership speculatively, or send an interrupt merely because a socket is absent.

`run recover` already permits unknown runs and must restart observation without resending the original brief. Preserve append-only actor-attributed audit updates in the same transaction. Native history reappearing before the budget must bind normally. A valid bound long-running agent is not subject to an arbitrary task-duration timeout. Test unbound, transient unknown, recovery, queued work and shutdown explicitly.

Reconcile obsolete review runs only after source verification, using exact run/process identity, current missing-target evidence and the maintained audited store/API. Never apply broad-name process cleanup.

### 2. Ordered bounded transport

Corral's Python and Swift application queues need byte and count admission, not just socket write limits. One ordered writer owns each physical transport/lane; encryption counters and wire send ordering remain aligned. Queued plus in-flight work must have explicit limits, including a single legal maximum-sized frame. An overloaded destination fails/rebinds independently; never silently drop encrypted frames or report an unsent command as delivered.

Multicast event consumers must have bounded retention and an explicit gap/snapshot/resync path. A bare `bufferingNewest` replacement which silently loses conversation, list, receipt or terminal events is not acceptable. Recovery notifications must themselves be coalesced and bounded. Stop/deinit/connection replacement must release queued frames and complete pending outcomes exactly once. Normal local/LAN traffic must not reconnect merely because budgets are too small.

Reuse the current Go relay's per-destination byte queue and existing Python/Swift ordered writer as implementation references; adapt lifecycle and failure semantics, not product-independent code extraction. No new service or shared framework is required.

### 3. Plain-conversation cache

Corral's `SessionStore` owns derived plain-conversation retention. Add deterministic byte-weighted LRU admission/eviction and avoid making one-shot search preparation retain every full conversation. Preserve existing cache lookup/invalidation, unavailable-history behavior, short Chinese substring queries, role/timestamp snippets, exact search counts and standalone TUI operation. Do not replace the existing search engine or introduce a required daemon for the TUI.

Cache budgets are internal acceleration policy, not new user settings. Oversized individual histories remain readable but must not become permanent unbounded cache entries. Tests must cover hit recency, invalidation, concurrent loads, oversized bypass, removal and indexing behavior.

### 4. Remote normalized transcript cache

Corral owns product byte budgets across inactive transcripts, reader lifetimes, expensive read admission and incremental replay. The existing count cap of 48 is not a byte budget. Protect actively watched/pending work, release unneeded heavyweight readers, and make idle cache retention byte-aware. Budget accounting must include tool bodies and reader state, not only visible text.

Do not cut the open conversation's earlier pages, alter global message sequences or generations, discard pending tool correlations, or duplicate native parsers. An evicted derived cache must reconstruct correct history through SessKit and preserve persisted-cache acceleration. Global expensive-read admission must not queue unbounded work or let bulk history block input/control requests.

### 5. Native-history allocation

SessKit owns JSONL/SQLite interpretation and incremental readers. Eliminate redundant full-file byte/split-line/parsed-row buffers where the existing feeds can consume records incrementally. Preserve complete-file and active-branch semantics, partial trailing records, line evidence, generation/reset rules, page order, cross-page tool pairing, error evidence and no-change polling. Do not claim a genuinely bounded native tail if the implementation still materializes the generation.

Prefer a measured streaming allocation improvement with unchanged public reader contracts before a speculative redesign. Any further retained-event/window change must have complete semantic parity and explicit page reconstruction. Publish a versioned artifact, update Corral's complete pin and invalidate derived data through the existing provider cohort before installed acceptance.

### 6. Native UI resources (primary-owned)

Shared Swift networking/cache changes are independently non-UI. Native pane/view, image presentation, text/layout and switching behavior stay primary-owned. Evaluate aggregate image/cache cost and memory pressure with the existing AppKit/UIKit and NSCache APIs. Per-loader soft cost limits and a four-dormant-pane count alone do not bound the application.

Evict only rebuildable resources under pressure, preserving drafts, reading state, native selection and terminal state. Do not use view teardown, frame throttling, shortened scrollback or a new UI state as a cosmetic memory remedy. Preserve the documented 400-message on-disk snapshot budget without applying it to an open conversation. Measure visible and dormant resource owners before claiming whole-client savings.

## Acceptance and delivery

1. Keep synthetic regression fixtures free of private history. Native-history probes emit counts, sizes and timings only; use read-only live sources and isolated writable caches.
2. Compare baseline/candidate on the same immutable data for cold read, warm no-change poll, append, history replacement, tool detail and backward paging. Record retained Python allocations, allocation peak, physical footprint and latency separately. Include all five active Corral runtimes; preserve SessKit's independent supported runtime contract.
3. Prove stalled socket/event consumers remain bounded and recover with no false delivery, ciphertext loss, missing terminal bytes or duplicate input. Test reconnect and stop while queued work exists.
4. Verify TUI and both native clients on the real user path. Builds/tests alone do not prove rendering or interaction. Phone locking/unavailability must be reported separately; deliver an actual signed IPA through the documented shelf path when direct installation cannot run.
5. Run complete current-source/environment gates and inspect all warnings. Commit and push only this task's verified changes, install reproducible committed artifacts, restart only owned/necessary services, and measure installed behavior. Public releases use the release train, not manual tags.
6. Assess reductions from measured before/after evidence, not the sum of independent theoretical maxima. Retain explicit limits if a platform, real workload or allocation owner cannot be measured.

## Reference sources

- Apple footprint and VM accounting: <https://developer.apple.com/videos/play/wwdc2022/10106/>
- Apple allocation generations and retaining graphs: <https://developer.apple.com/documentation/xcode/gathering-information-about-memory-use>
- Python allocation tracing: <https://docs.python.org/3/library/tracemalloc.html>
- websockets memory/backpressure: <https://websockets.readthedocs.io/en/stable/topics/memory.html>
- AsyncStream unbounded policy: <https://developer.apple.com/documentation/swift/asyncstream/continuation/bufferingpolicy/unbounded>
- NSCache advisory limits: <https://developer.apple.com/documentation/foundation/nscache/totalcostlimit>

The private diagnostic artifacts and repeatable probes are under `~/.config/corral/development/memory-audit-20261025/`. These paths are diagnostic evidence, not a product dependency. Per-slice implementation and acceptance findings belong below as they are established.

## Implementation findings (2026-10-25)

All numbers below are controlled isolated probes or fixtures recorded in the slice reports under the private audit directory; they are not live whole-process savings. Installed acceptance and before/after sampling are recorded separately at delivery.

### Bounded delegation observation

An already submitted run with no usable native history now settles a recoverable `execution=unknown` after 150 s of continuous missing/unreadable history and exits. Any successful bind or readable poll restarts the budget, so a valid long-running agent is never cut by task duration. Delivery, host, session ownership, native id, messages and questions stay untouched; nothing is resent or interrupted. Tests cover unbound, bound-but-unreadable, transient recovery, positive terminal death, external settlement, recovery without replay and a 900 s readable task. The two obsolete review supervisors were allocation-profiled only by lifetime, not by retained owner; ending them releases their process memory but no per-iteration leak is claimed.

### Ordered bounded transport

One ordered `LaneWriter` per physical socket; per-channel 8 MiB/128 frames, per-lane 12 MiB/256 frames, per-transport 32 MiB/512 frames, all counting queued and in-flight work. A stalled sink receiving 512 × 64 KiB frames now retains 12.31 MiB with one pending send task instead of 33.90 MiB with 512 tasks; overflow resets only the affected channel or lane and the client rebinds. `HostChannel` inbound admission is byte-aware and counts the processing frame; data-plane overflow resets that plane while the control connection survives. Encrypted frames are never dropped from a live counter sequence.

### Plain-conversation cache

`SessionStore.conversations` is an 8 MiB byte-weighted LRU with single-flight reads and removal fencing. One-shot search preparation reads through `get_conversation_for_index()`, which reuses a valid entry without promoting it and never admits a fresh full conversation. A 997-row real catalog measured 14.9 MiB traced retention unbounded versus 7.2 MiB admitted; oversized individual histories remain complete but uncached. Search text, counts, Chinese substring semantics and standalone TUI operation are unchanged.

### Remote transcript cache

Inactive transcripts now have a 64 MiB byte budget beside the 48-entry cap. Watched entries and the single newest unprotected entry are protected; eviction drops the whole reconstructible record. When the last watcher leaves and the estimate exceeds 4 MiB, heavyweight reader/full state is released while cursors, generations, pending tool pairing and projected messages stay valid. A 24 × 4,000-message fixture measured 96.8 → 32.4 MiB traced current (process footprint 175.5 → 86.4 MiB); five live runtimes reopen without duplicate messages. History/image/search reads also share one process-wide 8-slot admission with immediate retryable refusal; metadata, input and terminal control stay outside it. An open conversation is never clipped.

### Native-history allocation

SessKit reads JSONL/Pi cold builds in 1 MiB batches and feeds parsed rows in bounded batches instead of holding whole-file bytes, every split line and all parsed rows together. On the largest real samples, cold open plus an 80-event page reduced traced peak from 195.7/238.4/125.4 MiB to 36.3/54.8/63.9 MiB (Claude/Codex/Pi) and process footprint from 223.2/291.6/160.1 MiB to 70.6/107.7/125.6 MiB. Materialized events and Pi's native entry tree remain retained by contract; no bounded-tail claim is made, and a single huge line still peaks with the line.

### Native client resources

Mac warm panes are costed by their whole retained payload (text, full tool bodies, question projections and per-message objects): at most 4 MiB per pane, 12 MiB for all dormant panes, four panes maximum. Image previews use one app-wide 48 MiB cache instead of a 48 MiB limit per conversation; Markdown, image-reference and list-preview caches have byte costs. System memory pressure releases only those caches and dormant panes; the open pane keeps subscriptions, drafts, reading position, native selection and terminal state. The 400-message on-disk snapshot budget is unchanged and is not applied to an open conversation.

### Retained and deliberately unchanged

- Retained allocator high-water is not a leak: releasing readers drops traced reachable allocations sharply while process footprint can stay elevated.
- The Go relay remains small (about 18 MiB cgroup memory) with 8 MiB per-destination queues; no speculative rewrite.
- The prepared search snapshot keeps original-case lines plus a lowercase blob (17–30 MiB at depth 50–200). Dropping role/time evidence or replacing the substring engine is not justified by this measurement.
- Watched conversations and the single newest oversized transcript can exceed the soft budgets by design; several simultaneously watched huge sessions are the documented residual case.
