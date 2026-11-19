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

## Round 2: background daemon footprint & native scanner acceleration (2026-11-19)

### Measured baseline (Corral 1.0.0, SessKit 0.3.8 pure wheel, Python 3.14.10, macOS ARM64)

Live `vmmap --summary` physical-footprint sampling on the steady-state background pair (zero online remote clients) measured:

- `corral remote _serve`: `312.0–393.5 MiB` `phys_footprint` (peak `552.4–749.6 MiB`, RSS `~502 MiB`), dominated by `Malloc Small` (`139.8–182.3 MiB`), `Untagged` pymalloc arenas (`111.8–129.8 MiB`), and `Malloc Large (empty)` (`46.6–51.1 MiB`).
- `corral.scan_worker --limit 200`: `198.8–261.5 MiB` `phys_footprint` (peak `432.9–630.8 MiB`, RSS `~421 MiB`), dominated by `Malloc Small` (`124.7 MiB`), `Malloc Large (empty)` (`104.9 MiB`), and `Untagged` (`48.1 MiB`).
- Combined steady-state background pair: `573.5–627 MiB` `phys_footprint` (`>1.0–1.38 GiB` combined peak).

Isolated read-only probes against local native histories identified five primary root causes:

1. **SessKit Pi `_ENTRY_CACHE` retained full tool payloads and cached cold historical files**: `_compact_entry()` copied `message["content"]` verbatim for all roles (including multi-MB `toolResult` stdout, `toolCall` arguments, `thinking` blocks, and base64 images) while list-record building only needs user text, assistant text, and assistant-output presence. `scan_sessions(200)` also admitted every historical JSONL into the 64 MiB / 64-file cache and read whole files into memory before splitting lines. On local history, `pi.scan_sessions(200)` retained `91.8 MiB` traced allocations (`290.7 MiB` `phys_footprint`), dropping to `110.8 MiB` immediately after clearing `_ENTRY_CACHE`.
2. **macOS installed pure-Python SessKit instead of the published native `universal2` wheel**: `scripts/sesskit_dep.py` and `updater.py` pinned `py3-none-any.whl`, leaving `sesskit._native` unavailable (`native_available() == False`). Consequently `codex.scan_sessions(200)` fell back to pure-Python JSONL scanning and `count_loops_many`, raising `phys_footprint` from `23.4 MiB` to `151.5 MiB` and leaving `91.6 MiB` of `Malloc Large (empty)` deferred-reclaim pages.
3. **`corral remote _serve` fell back to full 6-runtime in-process scans on transient `keep_ids` misses**: Observed live in `events.log` (`scan_index_miss reason=keep_ids runtime=antigravity missing_count=1` → full `scan_all` inside `corral remote _serve`), permanently imprinting multi-runtime scan high-water memory onto the long-lived daemon.
4. **No idle reclamation for search index / zero-client remote state, and no high-water self-recycle for `scan_worker`**: `ConversationIndex` (`~26 MiB` traced / `~89 MiB` `phys_footprint` on 1,018 cached rows) stayed resident indefinitely after `_SEARCH_ACTIVE_WINDOW` (600 s) expired; idle `_transcripts` kept up to 64 MiB even with zero connected clients; and `scan_worker` retained cold-scan allocator high-water pages indefinitely (`malloc_zone_pressure_relief` does not force macOS `Deferred Reclaim` empty large regions back immediately).
5. **Cold-start attention inspection re-parsed ~1,000 unchanged dead histories inside `corral remote _serve` and TUI**: Even when `SessionStore.load()` consumed the shared `scan-index` in `0 ms` (`44.8 MiB`), `_reconcile_attention()` had only an in-memory evidence cache and called `inspect_session()` on every listed session. `_read_jsonl_tail()` read up to `8 MiB` per file and materialized up to `768` decoded dicts simultaneously (`codex` `+86.7 MiB`, `pi` `+47.4 MiB`, `antigravity` `+12.5 MiB`), spiking a fresh `corral remote _serve` from `44.8 MiB` straight to `225–236 MiB` before any client connected.

### Ownership and contracts (Round 2)

1. **SessKit Pi entry projection & streaming + native Rust loop streaming (`SessKit` v0.3.11)**:
   - `_compact_entry()` projects only list-builder fields: precomputed user text, assistant text, and assistant-output flag; drops raw `content` from all cached messages (especially `toolResult` / `toolCall` / `thinking`). Both raw entries (`read_entries()`) and compacted entries remain 100% semantically identical in `_build_session_info_from_entries()` and `count_loops_from_branch()`.
   - Full reads in `_read_incremental_entries()` stream line-by-line instead of reading whole-file bytes + splitting all lines in memory.
   - Tightened defaults (`_ENTRY_CACHE_MAX_BYTES = 8 MiB`, `_ENTRY_CACHE_MAX_FILES = 16`); `scan_sessions()` only admits recently modified files (within 15 minutes) or already-cached files, while `refresh_session()` continues caching hot polled sessions.
   - `crates/sesskit-stats/src/loops.rs` streams JSONL files line-by-line via `BufReader` (64 KiB buffer, shrinking buffers > 256 KiB after oversized lines) instead of calling `std::fs::read()` across all Rayon threads (which previously caused `414.4 MiB Malloc Large (empty)` / `455.9 MiB` footprint on `codex.scan_sessions(200)`), and `crates/sesskit-stats/src/opencode_list.rs` borrows SQLite column `&str` directly via `row.get_ref()`.
   - `src/sesskit/parsers/claude.py` (and `crates/sesskit-stats/src/claude.rs`) preserves non-empty `<command-args>` inside leading `<command-name>/...</command-name>` wrappers without `<command-message>` (fixing a regression from `d7d6c77` while keeping isolated `<command-args>` in prose untouched and emitting no spurious `SkillInvocation`).
2. **Platform-aware SessKit native wheel pin (`Corral/cli`)**:
   - `scripts/sesskit_dep.py`, `src/corral/updater.py`, `pyproject.toml` (`[tool.uv.sources]` with `sys_platform` markers), `install.sh`, and Homebrew resource generation select the pinned macOS `universal2` native wheel on `darwin` and the pinned `py3-none-any` wheel elsewhere, with SHA-256 verification for every published artifact.
3. **Scanner isolation & worker self-recycling (`Corral/cli`)**:
   - When `scan_worker` is active and `scan_index.try_consume()` misses only on `keep_ids`, `SessionStore.refresh()` briefly waits (up to ~3 s) for `scan_worker` (which already watches `keep_ids` changes) to republish. If a subset of runtimes still misses (e.g. in-memory `_revealed_search_keys`), `SessionStore` / `RuntimeRegistry` consumes the covered runtimes from the fresh shared snapshot and scans only the missing runtime(s) locally rather than triggering a full 6-runtime scan.
   - `corral.scan_worker` measures its own `phys_footprint` (`proc_pid_rusage` v4 on macOS, `/proc/self/statm` fallback on Linux). After publishing a fresh snapshot, if uptime ≥ 120 s and `phys_footprint` exceeds the configured ceiling (`128 MiB` default, `CORRAL_SCAN_WORKER_MAX_MB` / `SC_SCAN_WORKER_MAX_MB`, `0` disables), it logs `scan_worker_memory_recycle`, removes its heartbeat file so the next refresh cleanly respawns a fresh worker while hitting the just-published snapshot, and exits cleanly.
4. **Remote idle reclamation (`Corral/cli`)**:
   - `ConversationIndex` and `SearchCatalog` expose explicit `clear()` methods. When `_SEARCH_ACTIVE_WINDOW` (600 s) elapses without search demand, `SessionHub` evicts both and resets demand state so the next search rebuilds lazily from SQLite cache.
   - When zero remote clients have active watchers (`sessions`, `conversations`, `terminals`, `screens`) continuously for 5 minutes, `SessionHub` releases all idle transcript readers (bypassing the 4 MiB floor), trims `_transcripts` to `8 MiB` / 4 entries, clears `store.conversations`, and runs `gc.collect()`. Actively watched sessions and open conversations remain untouched.
5. **Persisted non-live attention evidence cache & streaming JSONL tail inspection (`Corral/cli`)**:
   - `AttentionStore` (`session-attention.sqlite3`) persists an `attention_evidence_cache` table keyed by `(runtime_id, session_id)` and a versioned SHA-256 of `SessionStore`'s exact `evidence_signature` (source, path, mtime, file_mtime, size_bytes, live=False, runtime-specific signature). `SessionStore._reconcile_attention()` reuses matching persisted evidence for non-live sessions on cold startup/restart without re-reading unchanged histories; live sessions and any changed signature always call `inspect_session()` directly.
   - `attention_signals.py` streams tail JSONL entries one decoded dict at a time (`_iter_jsonl_tail()`) and drops the raw tail buffer before iterating so cold misses never retain 768 decoded multi-MB dicts simultaneously.

### Measured results (Round 2, 2026-11-19, Corral 1.0.0 + SessKit 0.3.11 native `universal2`, Python 3.14.10, macOS ARM64)

**Isolated real-history probes** (each measured in a fresh Python process against local histories; not summed as whole-process savings):

- `pi.scan_sessions(limit=200)`: `290.7 MiB` steady (`345.0 MiB` peak, `91.8 MiB` traced retained, `_ENTRY_CACHE` 28 files / `64.9 MiB`) → **`30.0 MiB` steady & peak** (`0.98–1.02 MiB` traced retained, 200/200 differential parity, 0 mismatches).
- `codex.scan_sessions(limit=200)`: pure-Python `145.9–151.5 MiB` / `4,844 ms`; unstreamed native `std::fs::read` in Rayon `455.9 MiB` (`414.4 MiB Malloc Large (empty)`) / `572 ms` → **streamed native `146.5 MiB` / `864 ms`** (`count_loops_many` alone `63.7 MiB`, 5.6× faster than pure Python without the +310 MiB Rayon buffer regression).
- `opencode.scan_sessions(limit=200)`: pure-Python `228.7 MiB` / `5,975 ms`, owned-String native `173.0 MiB` → **borrowed-`&str` native `145.3 MiB`**.
- `SessionStore.load()` (`limit=200`, `shared_index=True`, 1,017 sessions): `202.4 MiB` / `~2,000 ms` before → **`100.1 MiB` / `1,669 ms`** on first cold start (empty attention evidence cache, streaming `_iter_jsonl_tail`) → **`54.6 MiB` / `279 ms`** on warm restarts (persisted `attention_evidence_cache`).
- First-screen `registry.scan_all(50)` on installed pipx Python: **`11.3 ms`** (`shared_index=True`, 251 sessions).

**Live whole-process `vmmap --summary` on the installed background pair** (zero online remote clients, Relay online, LAN + mDNS on, 1,017 sessions, 4+ minutes post-restart; `tmux -L corral-keepalive` untouched):

| Process | Before steady `phys_footprint` | After steady `phys_footprint` | Before peak | After peak |
|---|---|---|---|---|
| `corral remote _serve` | `312.0–393.5 MiB` | **`72.0 MiB`** (`-77%` to `-82%`) | `549.5–749.6 MiB` | **`123.8 MiB`** (`-77%` to `-83%`) |
| `corral.scan_worker` | `198.8–261.5 MiB` | **`82.8 MiB`** (`-58%` to `-68%`) | `432.9–630.8 MiB` | **`203.9 MiB`** (`-53%` to `-68%`) |
| **Combined pair** | **`573.5–627.0 MiB`** | **`154.8 MiB`** (`-73%` to `-75%`) | **`>1.0–1.38 GiB`** | **`327.7 MiB`** (`-67%` to `-76%`) |

**Verification & residual limits:**

- `SessKit` v0.3.11 (`36bdb15`, tag `v0.3.11`): Ruff clean, `cargo test --locked` + `sesskit-stats` 25/25 + `cargo clippy --all-targets -- -D warnings` (0 warnings), `publish.py check-dist` passed on both `universal2` and `py3-none-any` wheels + `tar.gz`, pytest native `1,250 passed, 4 skipped, 54 subtests passed` and pure `1,210 passed, 44 skipped, 54 subtests passed`.
- `Corral/cli`: Ruff clean, full `scripts/ci-test.py` first-pass green **`3,460 / 3,460`** (3 skipped, 0 failures, 0 retries, 0 warnings, `coverage_ok=True`).
- Residual peaks: cold scanning multi-GB native histories (e.g. `~1.8 GiB` `opencode.db`, Codex sessions with multi-MB screenshot lines) still briefly peaks `scan_worker` around `~204 MiB` before macOS reclaims deferred-reclaim pages (and `scan_worker` self-recycles if steady footprint stays `≥ 128 MiB` after 120 s uptime). Actively watched multi-MB conversations remain bounded by the existing 64 MiB active transcript cap and trim down to 8 MiB / 4 entries after 5 minutes with zero connected clients.
- Note on `malloc_zone_pressure_relief(NULL, 0)`: measured on macOS ARM64; does not reclaim `Malloc Large (empty)` deferred-reclaim regions immediately, confirming process isolation + self-recycling in `scan_worker` as the reliable containment mechanism.

### Round 2 (P2): Native Apple clients (`Corral/apple`, 2026-11-19)

Adopted architecture contracts (detailed client rules and diagnostics policy live in `apple/docs/PERFORMANCE_KNOWLEDGE_BASE.md`):

1. **Mac terminal AppKit backing store & hidden Metal reclamation (`CorralMac`)**:
   - `StreamTerminalView` opts into layer-update mode (`wantsUpdateLayer = isUsingMetalRenderer || isHiddenOrHasHiddenAncestor || window == nil`) and clears `layer?.contents = nil` in `updateLayer()` and `releaseBackingStoreIfIdle()`, avoiding redundant Retina `NSViewBackingLayerContents` `IOSurface` allocations while `CAMetalLayer` renders (keeping `wantsUpdateLayer == false` for visible CoreGraphics fallback).
   - `TerminalHostView` enables Metal before `isHidden = false` on unhide and releases `MetalTerminalRenderer` on hidden terminals after a 10 s grace delay (or immediately under `ClientMemoryPressure`), while keeping `MacTerminalSessions` 16-terminal retention, 10,000-line scrollback, live terminal streams, and `showsRestartPrompt` intact. Deleted sessions (`MacWorkspace.applySessionAction(.delete, ...)`) and host switches explicitly close associated terminals and evict warm panes.
2. **Shared diagram, media & WebSocket memory hygiene (`Corral/apple/Shared`)**:
   - `ChatDiagramDrawing` drops retained raw `png: Data?` after raster/disk persistence, accounts decoded pixel bytes via `cgImage.bytesPerRow * cgImage.height` inside a 32 MiB / 48-entry `NSCache`, and registers `purgeMemory()` with `ClientMemoryPressure` on macOS as well as iOS.
   - `ChatImageLoader` caps `memory.countLimit = 96` and uses header-only validation (`CGImageSourceGetCount`) on raw disk bytes (`fetchRawBytes`) for Quick Look `.full` exports so full 2560px RGBA bitmaps are not decoded into `Self.memory` (preserving `#if DEBUG` `corral-preview://` fallback).
   - `ChatFileLoader.fetchAndCache` streams downloaded chunks directly to disk via `FileHandle` with incremental `SHA256` verification rather than buffering whole files in `Data`.
   - `RemoteWebSocketSession.makeConfiguration()` disables `urlCache`, `httpCookieStorage`, `httpShouldSetCookies`, and local request caching for encrypted WebSocket transports.
3. **Mac conversation text & table teardown (`CorralMac`)**:
   - `MacSingleTextEngine` bounds cached layout widths to 12; `MacPaneSurfaceCache` invokes `releaseDetachedViews()` on suspended warm panes and memory pressure to detach offscreen `NSTextView` instances without clearing active selection or hover-retained `MacConversationTable.Coordinator.cells`; `Coordinator.detach()` clears `measurer.rootView`, `rows`, `indexByID`, and `rowContent`.
4. **Measured results & delivery verification (2026-11-19)**:
   - **Isolated 16-terminal A/B probe** (892×1200@2x, 2,500 CJK+truecolor lines each, 4 visible + 12 hidden): unmodified `TerminalView` `479.4 MiB` steady (`IOSurface 266.5 MiB`, `IOAccelerator graphics 26.8 MiB`) → `wantsUpdateLayer` + clearing `layer?.contents` + hidden `setUseMetal(false)`: **`222.5 MiB`** (`-53.6%`), `IOSurface = 66.6 MiB` (`-75.0%`), `IOAccelerator (graphics) = 9.3 MiB` (`-65.1%`); production `StreamTerminalView` 16-terminal probe passed (`reveal = 2.22 ms`, `liveFeed = true`, `IOAccelerator` graphics regions 36 → 12 / `864 KiB` → `288 KiB` dirty).
   - **Checks & builds**: `check_localizations.py` (644 keys), `swift test` (521/521, 0 warnings), `test_mac_session_switch.py`, `test_mac_chat_stream.py`, `test_mac_text_selection.py` (including `releaseDetachedViews()` leak/selection test), `test_mac_lifecycle.py`, `test_mac_terminal_selection.py` (5/5 including `verifyBackingStorePolicy()`), `CorralMac` Debug build + universal (`arm64 x86_64`) Release archive (0 new warnings), `iPhone Duo` simulator `scripts/ios_sim.sh test` (`CorralTests` 617/622 passed, 1 skipped non-loopback check, 4 documented hardware-keyboard timeouts in `ChatKeyboardLayoutTests`; `CorralUITests` 2/2 passed; 0 new warnings), and generic iOS Release build (0 new warnings).
   - **Delivery & live acceptance**: Developer ID Release `/Applications/Corral.app` (`1.0.0` build `193`, commit `bd762b8`, `codesign --verify --deep --strict`) verified healthy (`client_diag.py status --active`: `ready local data=True`; idle `vmmap --summary`: `78.7 MiB` steady, `79.4 MiB` peak, `IOSurface 384 KiB`; fresh `4448×2532` Retina window capture inspected clean). Signed iOS Release (`1.0.221` build `235`) delivered to AppShelf (`ReleaseID 506`, HTTP `201`) via the documented `seoul` fallback (`iPhone Max` unavailable over CoreDevice; direct home TLS hit curl exit 35).

<!-- 该文档整理/压缩于 2026-11-15 -->
