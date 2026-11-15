# Hosted session lifecycle: retire when idle, resume losslessly

Status: **design for owner review**. No implementation in this document and none shipped under it. Behaviour is specified here; the SwiftUI/TUI surface of §8 stays with the owner's own agent.

Registration note: this file is not yet linked from the root `AGENTS.md` navigation. Root edits belong to the `agents-md-edit` skill, which this task did not have; the owner's next documentation pass should add one summary line under the design-document links.

## 0. Why this exists (measured, 2026-11-15)

One `tmux -D -L corral-keepalive` server, launchd-parented, hosted 43 agent sessions (33 pi, 9 antigravity) on a MacBook Pro 18,3: Apple M1 Pro, 8 cores (6 performance + 2 efficiency), 16 GB unified memory. 34 of those sessions sat at 0.0 % CPU for hours. Their RSS was ~15 MB each; their `phys_footprint` was 100-275 MB each. Aggregate ~4.2 GB — most of the 3.0 GB of swap that was in use, plus compressor. One parked `pi` measured RSS 13 MB / `phys_footprint` 129 MB.

The gap between RSS and footprint is not a measurement error: `phys_footprint` is the ledger that includes compressed and swapped-out pages, RSS is "pages in RAM right now". The kernel formula is `phys_footprint = (internal - alternate_accounting) + (internal_compressed - alternate_accounting_compressed) + iokit_mapped + purgeable_nonvolatile + purgeable_nonvolatile_compressed + page_table` ([kernel ledger breakdown](https://www.bazhenov.me/posts/activity-monitor-anatomy), [footprint(1) usage](https://www.unixtutorial.org/commands/footprint)). An idle agent that has been paged out therefore looks tiny to `ps` and enormous to the memory system.

Consequences observed in the same window: load average 57 rising to 133, 0 % idle CPU, 3.0 GB of swap used, jetsam activity killing `ReportCrashService`, and the tmux server disappearing between 12:07:39 and 12:07:51, taking every session and five in-flight subagents with it. A second full loss at 12:42 (followed by another `exit(1)` server loss at 18:25 on the same host). Subsequent system-log correlation (`launchd`, `runningboardd`, `corral remote _serve` stack dumps) showed that both the 12:07 (PID 5141) and 18:25 (PID 1773) losses were **tmux internal `exit(1)`** rather than jetsam kills — triggered while an un-restarted `corral remote _serve` process evicted `embed._channels` (`_MAX_CHANNELS = 8`) against the Mac client's 16 open terminal streams in a tight 1s loop (~30 `tmux -C attach` cycles/s), under launchd's default 256-fd soft limit (`12:41:52` PID 13263 was Mac Resource Monitor `SIGKILL` before reboot). Meanwhile Corral recorded `remote_push_queued kind=aborted` for four sessions at 12:09 and **zero** `keepalive`/`server_` lifecycle events in `~/.cache/corral/events.log`, so the death was initially undiagnosable afterwards.

The owner's decision: the fix belongs in Corral. Sessions are retired automatically when idle, resumed losslessly when needed, and a session in use is never touched.

### 0.1 What this design must not do

- Not a new service, daemon, sidecar, CLI or binary. The mechanism lands in the existing modules and the existing background tick.
- Not a change to SessKit's parsing or to the relay protocol.
- Not a scrollback cut, a frame-rate throttle or a scan cadence change dressed up as a memory fix.
- Never a user-visible notification on the normal path. The existing silent contract ([`MAINTAINER_GUIDE.md`](../MAINTAINER_GUIDE.md) "Silent automatic reclaim") stands.

## 1. What already exists (read from source, 2026-11-15)

Do not rebuild any of this.

| Mechanism | Where | What it already does |
|---|---|---|
| Hosting wrapper | `keepalive.wrap_plan` / `hosted_identity` | `new-session -A -s corral-<runtime>-<ident8>` on socket `corral-keepalive`, per-runtime identity injection (Pi claim env, Codex app-server proxy, OpenCode pre-reserved `ses_` id) |
| Server scheduling | `tmux_server.ensure_server` | macOS launchd job `com.x0c.corral.keepalive.<socket>`, `ProcessType=Interactive`, `tmux -D`, `KeepAlive=false`; `server_report()` feeds `corral diagnose` |
| Idle verdict | `reclaim.evaluate` / `decide` | Pure-data protections: history unknown, caller protect, grace 30 min, viewed, foreign client, pinned, attention busy, background busy, `status_tag` not terminal, tmux timestamps unknown, idle below threshold |
| Evidence gathering | `reclaim.probe_hosted` / `_build_context` | One `list-sessions` + one `list-clients` per socket; attention DB, viewer registry, pins, delegation leases, background verdict; **any unreadable source makes the whole pass a no-op** |
| Background work | `busycheck.BusyChecker` | One `ps -eo pid,ppid,command` + one `list-panes` per pass; non-agent non-helper descendant ⇒ busy; Claude transcript enqueue-without-remove ⇒ busy |
| Pressure | `reclaim.memory_pressure` | Darwin: in-process `sysctlbyname` for `vm.swapusage` (used/total ≥ 0.6) **or** `kern.memorystatus_vm_pressure_level` ≥ 2; Linux: PSI `avg60` / `MemAvailable` |
| Kill | `reclaim.apply` → `keepalive.kill` | Audit event first, exact `kill-session -t =name`, in-process caches dropped via `liveness.forget_alive` + `embed.close_channel` |
| Audit | `observe.event("reclaim", …)` | One line per session with every evidence field; `events.log` rotates at 256 KiB into a single `.1` |
| Resume | `runtime/*.build_resume_plan`, `SessionHub.resume_session` | TUI Enter-to-restart, phone `session.resume`, `_keepalive_name(resume_if_needed=True)` |
| Liveness | `liveness.annotate`, `embed.pane_liveness` | Re-binds `keepalive_name` to panes; tri-state `alive` / `dead` / `unknown`, only authoritative target-not-found is `dead` |

Two structural facts drive the rest of this document.

1. **Reclaim produces a verdict, not a state.** Nothing writes down that a session is parked, that Corral retired it, or why it stopped. A retired session is indistinguishable from a crashed one, in the list, in the pane and in the log.
2. **Retirement is the only destructive lever, and it is all-or-nothing.** There is no budget, no floor below the age threshold, no per-session cooldown and no resume grace. A pass either finds provably idle sessions or does nothing at all.

## 2. State machine

### 2.1 States

`parked` is the only state Corral may retire from. Everything else is either work in progress, user intent, or unknown.

| State | Meaning | Observed today by | Fidelity |
|---|---|---|---|
| `working` | A turn is being produced: model streaming or tool executing | `attention.phase == "working"` from SessKit history evidence or observer events (Cursor hooks, Pi `agentPhase`); `busycheck` non-helper descendant | Good. Absence of evidence is not idleness, but presence is reliable |
| `waiting_on_tool` | A tool call is issued and not yet returned | **Not a state today.** Partially covered: `status_tag == pending` blocks retirement; `busycheck` sees live descendants; `attention_signals` already tracks unpaired tool ids per runtime but discards them into `phase` | Needs a name and a per-runtime reader (§3.4) |
| `waiting_on_user` | A structured question is open and the agent cannot proceed | `attention.phase == "waiting"` (yellow dot). Per owner ruling 2026-11-09 this is *only* structured questions (`_QUESTION_TOOLS`), not "the prompt is empty" | Good for the narrow meaning; the broad meaning ("agent finished, human has not replied") is `parked` |
| `parked` | Turn finished, process alive, nobody is using it | Today: the `evaluate` "inactive" verdict — history `status_tag` in {done, aborted} **and** old `session_activity` **and** old history time **and** no background work. Never recorded as a state | The predicate exists; the state does not |
| `retired` | Ended by Corral because it was parked; history intact; resumable | **Does not exist.** After `kill-session` the session is simply unhosted, and the next scan clears `keepalive_name` | Needs a durable record (§7.3) |
| `died` | Ended by anything else: crash, `kill-server`, jetsam, server loss, machine reboot | `live=false`, no `keepalive_name`; `embed.pane_liveness == "dead"`; nothing records the cause | Needs a cause and an actor |
| `unknown` | A required probe failed for this session | Existing contract: `busycheck` returns `background_unknown`, `memory_pressure` returns False, unreadable protection source blocks the pass | Correct today; must be preserved for every new signal |

Mapping to existing Corral vocabulary, so no parallel taxonomy appears in the UI:

- `live` (SessKit) = "an associated process was observed"; it stays true in `parked` and false in `retired`/`died`.
- `status_tag` (SessKit) = persisted turn outcome; it stays `✅已完成` in `parked`, `retired` and most `died`.
- `hosted` = `bool(keepalive_name)`; true in `working`/`waiting_*`/`parked`, false in `retired`/`died`.
- `attention.phase` = work evidence only; never a lifetime state.
- `pane_liveness` = the authoritative death check for a *named* pane.

### 2.2 Transitions

```
                     ┌──────────────────────── resume (native) ────────────────────────┐
                     │                                                                  │
  new/handoff ──▶ working ⇄ waiting_on_tool ⇄ waiting_on_user ──▶ parked ──▶ retired ──┘
                     │              │                 │              │
                     └──────────────┴─────────────────┴──────────────┴──▶ died (crash/server loss)
```

| From | To | Trigger | Guard |
|---|---|---|---|
| `working` / `waiting_*` | `parked` | Turn reaches a terminal record and no background work remains | `status_tag` ∈ {done, aborted}; no non-helper descendant; no pending transcript marker; attention phase not working/waiting |
| `parked` | `working` | New input or resumed turn | Any fresh history/mtime/session_activity resets the idle clock |
| `parked` | `retired` | Policy verdict (§4) | Every invariant in §5, audit written first |
| `parked` | `died` | Pane or server vanishes | `pane_liveness == dead` or server pid changed |
| `retired` / `died` | `working` | Resume | History must exist and the resume plan must build; identity verified after start |
| any | `unknown` | A required probe fails | Blocks destructive action, never blocks display |

Illegal transitions that must be impossible by construction: `working → retired` (INV-1/2), `waiting_on_user → retired` (INV-3), `retired → retired` within the cooldown (INV-13).

## 3. Signals

Each signal is listed with how it is read, what it proves, and how it lies. A signal that "can lie" is not useless — it is a signal whose *absence* may not be trusted in the same way as its presence. Corral's existing rule — presence blocks, absence needs corroboration — is correct and must be extended, not replaced.

### 3.1 S1 — cumulative CPU-time delta

**Read.** Extend the single per-pass `ps -eo pid,ppid,command` in `busycheck.snapshot_ps` to `ps -eo pid,ppid,time,command` (one fork, no extra cost), and take the delta against the previous pass's value for the same pane tree, keyed by `(tmux session name, pane pid, process start time)`. `time` is cumulative CPU consumed by the process; it is not `%cpu`.

**Proves.** Zero delta over a full interval proves the tree consumed no CPU during that interval. In the measured incident every one of the 34 parked sessions read 0.0 %, and their CPU-time delta would have been exactly 0.

**Lies.**

- `ps %cpu` must never be used for this. It is a decaying average over up to a minute (BSD `ps(1)`: "a decaying average over up to a minute of previous usage"), so a session that worked hard ten minutes ago still reads green, and a session that worked for 200 ms reads red. Midnight-to-midnight spikes and slow leaks are both invisible.
- Zero CPU does **not** prove parked. A blocked network read, an approval prompt, or a rate-limit backoff all consume zero CPU while a turn is open. S1 can only ever *veto* activity, never establish idleness alone.
- CPUs are asymmetric: 6P + 2E. A task pinned to an efficiency core can be running flat out at a tiny fraction of a percent of "the machine". Use the delta as a boolean, never as a percentage of capacity.
- A respawned process resets its CPU time. Compare start time first; a changed start time means "unknown", not "zero delta".
- Long intervals wrap and the printed format varies (`[[dd-]hh:]mm:ss` vs `hh:mm:ss`); parse defensively and treat a parse failure as unknown.
- CPU burned by the tmux *server* (redraw of 43 panes) belongs to the server, not to any session. Do not attribute it.

### 3.2 S2 — live child process tree

**Read.** Existing `busycheck.BusyChecker.process_busy_names`, one `ps` + one `list-panes` per pass. Non-agent, non-helper descendant ⇒ busy.

**Proves.** A live non-helper descendant means work that the agent is waiting on, or a shell the user started inside the pane. Both must survive.

**Lies.**

- Orphans that reparent to `init` escape the tree by design. A backgrounded `npm test` that outlives its parent is invisible.
- The helper allowlist (`_HELPER_ARGS`) is a name heuristic in both directions: an MCP server doing real work reads as helper, and a new helper name reads as busy forever. The second direction is safe; the first is a real hole for hosts whose tool work happens inside an MCP server.
- A child that starts and exits between two snapshots is never seen. At a 60 s tick this is a genuine miss; it does not matter for *retirement* (the work is already done) but it does matter for "was there work at all".
- A shared server (OpenCode's account-wide `opencode serve`) is not a descendant of the pane at all. See §6.4.
- `D`-state processes consume no CPU and still block. Treat as busy.

### 3.3 S3 — tmux pane / session activity

**Read.** `#{session_activity}` and `#{window_activity}` already in `reclaim._LIST_FORMAT`.

**Proves.** `window_activity` is the last time the pane did I/O; `session_activity` is the session-level timestamp. Per the tmux formats table both are "time of … last activity" ([tmux(1) formats](https://man7.org/linux/man-pages/man1/tmux.1.html)).

**Lies.**

- Idle agent TUIs repaint continuously, so `window_activity` is always fresh for a finished session. Measured on this repo's own machine: Codex sessions finished 24-34 h earlier showed pane output 0.1-1 min old; with output gating, 0 of 39 were reclaimable. This is why `reclaim.WINDOW_OUTPUT_GATES_HISTORY = False`; that flag must stay False for history-backed sessions.
- **Corral's own write counts as activity.** `send-keys` (remote inject, paste injection, mouse sequence, resize) advances window activity. A monitoring loop that pokes a parked agent therefore makes it look busy forever — the exact failure recorded upstream as "last_active inaccurate for parked agents: `#{window_activity}` counts gc's own wake send-keys as activity" ([gascity#3049](https://github.com/gastownhall/gascity/issues/3049)). Corral does not currently poke agents, but the remote input path does.
- `session_activity` is not documented to advance on agent work. Measurement on this repo's machine: a detached test continued producing output and received `send-keys` while `session_activity` stayed unchanged and `window_activity` advanced ([`MAINTAINER_GUIDE.md`](../MAINTAINER_GUIDE.md) "Misleading idle timestamp"). Its trigger set is undocumented; it is a weak signal that may only *delay*, never *permit*.
- Timestamps are integer seconds; wall-clock jumps (NTP, sleep/wake) produce negative ages. Treat negative as fresh (the existing code already does via `min(ages)` plus the freshness comparison).
- Attach/detach touches session activity, so the moment a user detaches, the session looks freshly active and retirement is delayed by a full threshold. Safe direction; keep it.

### 3.4 S4 — transcript mtime and tail content

**Read.** `History.active_at` = max of the session dict's `event_time`, `mtime`, `file_mtime`; `History.status_tag` from SessKit; `busycheck.claude_pending_background` for the Claude tail.

**Proves.** A turn is over when the transcript's last record is terminal (`✅已完成`, `⚠️已中断`). A tool call is pending when the tail has a `tool_use` without its `tool_result`, an enqueue without a remove, or a "moved to the background" without a completion.

**Lies.**

- An open turn can write nothing for a long time (long tool, slow model). Old mtime with a live turn is the normal case, not an anomaly; it is why S1/S2 exist.
- Metadata-only writes move mtime without a turn: Pi identity claims, `agentPhase`, title-side effects. The current `max(event_time, mtime, file_mtime)` makes a claim write look like fresh work, which errs protective. Keep.
- `status_tag` is a localized display string (`✅已完成` / `⏳待回复` / `⚠️已中断`, `sesskit/titles.py`), not a machine enum. Comparing it means importing presentation. SessKit already maps it to English in `sesskit/cli.py`; the retirement record and the audit must carry the machine form, and a future SessKit rename must not silently flip the verdict. A tag outside the known set is `unknown` and blocks.
- Tail windows miss older markers. `busycheck` documents a 256 KiB tail for histories over 8 MiB and a partition-based drop of a cut first line; a pending task older than the window reads as idle. The process tree is the stated backstop; for runtimes where it is not (§6.4) this is an accepted miss that must appear in the audit.
- Only Claude has a transcript marker check today. Pi, Codex, OpenCode and Antigravity rely on the process tree. That is a **known incompleteness**, not a design property.

### 3.5 S5 — Corral's own attention and busy signals

**Read.** `AttentionStore.busy_pairs()` (phases working + waiting; `None` when the store is unreadable), `embed.live_host_viewer_names()` (fresh rows only, `_HOST_VIEW_STALE_SECONDS = 8.0`, pid-alive check), `split_layout.pinned_keys_effective()`, `RunStore.active_hosts()` (unsettled delegation leases).

**Proves.** The user's intent: they are watching it, they pinned it, or a delegation run depends on it. Presence is authoritative.

**Lies.**

- Every one of these is local derived state. A stale row can keep a session alive forever: a `phase=working` row whose agent died mid-turn stays until a scan reconciles it; an `agentPhase=working` claim from the Pi extension is only refreshed on `/reload` for an already-running pane.
- Viewer liveness is a heartbeat plus a pid check, so a recycled pid can keep a dead window's row fresh.
- A readonly phone client never resumes and never registers a viewer, so it correctly does not protect a session — but that also means the phone can be looking at a conversation while Corral retires its *pane*. The conversation is history and stays readable, so this is the intended behaviour; §8 states it.
- Delegation leases protect by host name and resolve through `keepalive._ident_matches_session_id`; a claim that has drifted off its ident prefix silently stops protecting.

### 3.6 S6 — system memory pressure

**Read.** Existing `reclaim.memory_pressure()`: in-process `sysctlbyname("kern.memorystatus_vm_pressure_level")` (a 4- or 8-byte integer) and `sysctlbyname("vm.swapusage")` (a 32-byte `struct xsw_usage`, decoded as total/available/used), so a tick costs zero forks. Linux: PSI `some avg60` and `MemAvailable`.

**Proves.** The system is short of memory *now* — for the level; the swap ratio proves only that memory *was* short.

**Lies.**

- The level is discrete with no time window: 1 normal, 2 warn, 4 critical (userspace values; XNU collapses five internal levels into these three) ([newosxbook](https://newosxbook.com/articles/MemoryPressure.html), [psutil#2725](https://github.com/giampaolo/psutil/issues/2725)). Two reads in a row are the only smoothing available; one read is a coin flip at a boundary.
- **Swap used/total is sticky and must not be read as pressure.** macOS swaps pages out that have not been touched for a long time even when memory is comfortable, and it does not compact swap when pressure clears ([swap behaviour](https://mole.fit/blog/mac-memory-pressure-explained)). Total size is dynamic. The current `_SWAP_RATIO = 0.6` OR'd with the level therefore arms the 10-minute threshold for hours after recovery — a real defect: a 4 GB swap file at 2.5 GB used, on a machine that has been calm for an hour, still counts as "pressure".
- Pressure is machine-wide. Chrome or Xcode can create it. Retiring Corral's own parked sessions is still the right response; escalating beyond `parked` never is.
- Any probe failure returns False (existing). That is correct for "do not lower the threshold", but it must not be read as "the machine is fine" anywhere else.

### 3.7 S7 — hosted-set integrity (new, required for §7)

**Read.** Compare `tmux list-sessions` against the recorded inventory (§7.3); `tmux_server.server_pid`; `embed.pane_liveness`. Together they distinguish "the session ended" from "the server that owned it ended".

**Proves.** A server whose pid changed means every session under the old pid is `died`, regardless of what individual panes report afterwards.

**Lies.** A socket file can exist without a connectable server (`tmux_server.server_running` already probes by connecting, not by stat). `pane_liveness` timeouts are `unknown`, never death.

## 4. Policy for 16 GB / 8 cores

### 4.1 Measured basis

Parked footprint measured on this machine: median ~124 MB per parked session (4.2 GB / 34), range 100-275 MB. 12 parked sessions ≈ 1.5 GB, ~10 % of RAM — the share that may sit idle. The machine ran a 4 GB swap file with 3.0 GB in use when the server died.

### 4.2 Thresholds

| Knob | Normal | Under pressure | Why |
|---|---|---|---|
| Idle threshold before `parked → retired` | 120 min (`DEFAULT_IDLE_MINUTES`) | 10 min (`DEFAULT_PRESSURE_IDLE_MINUTES`) | Unchanged from the shipped contract; re-measured against the incident, where the 34 parked sessions had been idle for hours |
| Absolute floor, any reason | 30 min | 30 min | A session younger than this cannot have been "forgotten"; below it the only thing retirement frees is work the user may return to in the next breath |
| Grace after creation | 30 min (`GRACE_SECONDS`) | 30 min | Unchanged |
| Grace after resume | 10 min (**new**) | 10 min | A resumed pane spends its first minutes re-reading history and can look idle; retiring it again is the worst possible behaviour |
| Per-session cooldown | 30 min (**new**) | 30 min | Anti-flap: a session oscillating parked/working must not be retired repeatedly |
| Resident parked budget | 12 sessions (**new**) | 6 | ~1.5 GB at the measured median; under pressure halve it |
| Max retirements per pass | 3 (`MAX_PER_PASS`) | 6 | Pressure must be able to drain faster, but still bounded so one tick cannot end a working day |
| Pass interval, machine-wide | 60 s (`_MIN_INTERVAL_SECONDS`) | 60 s | Unchanged; the cross-process `flock` slot already guarantees one pass |
| Young-process guard | 120 s (`_MIN_PROCESS_AGE_SECONDS`) | same | Unchanged: opening the TUI must never kill anything |
| Pressure arming | 2 consecutive true reads ≥ 60 s apart (**new**) | — | Kills the one-sample blip |
| Pressure disarming | pressure false for 10 min (**new**) | — | Kills the sticky-swap false arm |
| Swap ratio | informational only (**change**) | — | Not a pressure trigger; retained in the audit |

The budget is a **count**, not a byte budget, and that is deliberate. `phys_footprint` is the honest number but costs one fork per pid (`footprint -p <pid> -f bytes`), i.e. 1-2 s for 43 sessions — unusable every 60 s. Summing RSS from the `ps` snapshot already taken would be exact and cheap but understates by roughly an order of magnitude on this machine (13 MB RSS vs 129 MB footprint for the same parked process), which would make the budget meaningless. Count is exact, free, stable, and directly proportional to the thing being controlled. Footprint is used where it is cheap and where it matters: acceptance (§10) and the per-candidate tiebreak below.

### 4.3 Decision order (every pass)

1. Read all protection sources. Any unreadable source ⇒ no-op for the whole pass (existing, unchanged).
2. Pressure = armed per §4.2 (two reads + 10-minute disarm).
3. For each hosted session, `evaluate` as today, plus: resume grace, per-session cooldown, `waiting_on_tool` (§3.4), and S1's cumulative-CPU delta.
4. `decide` sorts by longest idle, then by largest last-measured footprint (only computed for the eligible candidates, at most `MAX_PER_PASS + 4` forks), and caps per pass.
5. `apply` audits first, verifies pane identity, kills, records the retirement.

### 4.4 Budget exceeded but nothing eligible

This is the normal position during the incident: 43 sessions, most of them pinned to a live pane or owned by a user who was actually working. The ladder, in order, and it stops at the first step that frees the budget:

1. **Tighten this tick's effective threshold** toward the 30-minute floor, in steps (120 → 90 → 60 → 45 → 30 min), one step per pass. Not silent-arbitrary: every step is recorded in the pass event with the budget arithmetic.
2. **Lower the pressure threshold** 10 → 5 min, only if pressure is armed. Never below the resume grace.
3. **Do nothing destructive.** Record one `reclaim_blocked` event per pass carrying the reason histogram (`attached_client: 12, busy: 9, viewed: 3, grace: 2`) and the resident count. Do not kill anything outside `parked`.
4. **Reduce Corral's own cost** through the already-shipped pressure-adaptive backoff for scans and frame pacing ([`PERFORMANCE_KNOWLEDGE_BASE.md`](../PERFORMANCE_KNOWLEDGE_BASE.md) perf-B item (e)). This is the only other lever the owner has authorised.

Step 3 must be loud in the log and silent in the UI. The incident's root failure was not the wrong threshold; it was that nobody could tell afterwards what had been happening.

### 4.5 Graceful degradation

| Failure | Behaviour |
|---|---|
| tmux missing / unusable | No retirement at all (existing) |
| `ps` unusable | Busy verdict unknown ⇒ whole pass no-op (existing) |
| Attention DB, layout DB, viewer registry or run store unreadable | Whole pass no-op (existing) |
| Footprint probe fails | Budget falls back to count-only; `budget_source: count` in the audit |
| Pressure probe fails | No pressure (existing) ⇒ the 10/5-minute thresholds stay unarmed; nothing else changes |
| Lifecycle log unwritable | **No retirement** (§5 INV-9). This is a change from today, where `_audit` swallows the error and the kill proceeds |
| `kill-session` fails | `reclaim_failed` recorded, not counted, not retried in the same pass (existing) |
| Clock jumps backwards | Negative ages read as fresh (existing) |

## 5. Safety invariants

Numbered so tests can name them. Each is stated as a property of `evaluate`/`decide`/`apply` on plain data, so all but INV-6, INV-7 and INV-9 can be unit-tested on plain data; those three need one real-tmux test on a private socket, in the style `test_reclaim.py` already uses.

1. **INV-1 — no live child work.** Never retire a hosted session while any non-helper descendant of its pane pid is alive. Unknown (`ps` unavailable, pane pid missing from the snapshot) is busy.
2. **INV-2 — no pending tool call.** Never retire while a tool call is open: `status_tag == pending`, an unpaired `tool_use` in the tail, an enqueue without a remove, or a background move without a completion. Unknown is busy. For Codex this is stricter, not optional (see §6.3).
3. **INV-3 — never the session the user is using.** Any fresh `live_host_viewer_names` entry, any unsettled `RunStore.active_hosts` lease, any non-control-mode attached client, and any pinned key blocks retirement.
4. **INV-4 — never below a floor.** No retirement before the 30-minute absolute floor, the 30-minute creation grace, the 10-minute resume grace, or the 30-minute per-session cooldown, whatever the budget or pressure says.
5. **INV-5 — always leave a resumable record.** Retirement must not touch any history file, and must write a retirement record (§7.3) that outlives the Corral process. A retirement whose record cannot be written does not happen.
6. **INV-6 — identity before the kill.** The pane pid and server pid observed at decision time are re-read immediately before `kill-session`. A mismatch cancels the retirement and records `reclaim_skipped: pane_replaced`. `kill-session` keeps its exact-match `=` target.
7. **INV-7 — idempotent under a resume race.** If the user resumes between decision and kill, the outcome is exactly one live pane, one agent process, and no lost message. Mechanically: a per-session advisory lock (a file under the cache dir, `flock`) held across *re-check protections → kill → record*; `resume` takes the same lock. Whoever loses waits and re-reads, never retries blindly.
8. **INV-8 — never from a UI thread or a startup path.** Only the existing background ticks call it; `maybe_reclaim` must not appear in `host_controller`, `embed` or `cli` (an existing test already guards the source).
9. **INV-9 — no retirement without a durable audit.** The audit write is verified (append + `fsync` or a size check) before the kill.
10. **INV-10 — bounded and non-retrying.** At most `max_per_pass` per pass; a failed kill is recorded and not retried in the same pass.
11. **INV-11 — never during a server-wide operation.** While a keepalive migration or server restart is in progress (the existing migration script's lock file), no retirement.
12. **INV-12 — unknown is never idle, at every level.** Per-session and per-pass. This is the existing contract and it must survive every addition above.
13. **INV-13 — one retirement per session per cooldown.** A retire record must not be producible twice for the same session key inside 30 minutes.
14. **INV-14 — the retirable set is closed under the recorded evidence.** Every field the verdict used must appear in the record, so a disputed retirement can be re-decided from the log alone. This is what the 12:07 incident lacked.
15. **INV-15 — a resume that can fail is a retirement that may not happen.** Eligibility requires a resume guaranteed by construction (§6.5), not a resume that is attempted and hoped for. When a resume does fail unexpectedly, Corral absorbs it: it retries itself under the INV-7 lock and never renders the failure as an action the user must take. No retry button, no "please try again", no repair instructions.

## 6. Retire and resume, per runtime

### 6.1 Common destruction

What is destroyed is the same for all runtimes: the tmux session (its window, pane, pty and `history-limit 10000` scrollback), the agent process, its child helper processes, and every byte of state that lived only in that process's memory. What persists is the native history plus whatever the tool wrote to disk.

### 6.2 Per runtime

| Runtime | Destroyed | Persists on disk | Resume path | Not recoverable |
|---|---|---|---|---|
| **pi** | tmux session; `pi` process; its extension-hosted MCP children; pane scrollback; composer contents | `~/.pi/agent/sessions/<cwd-slug>/<session-id>.jsonl` — a JSONL tree, header line + entries with `id`/`parentId` ([Pi session format](https://pi.dev/docs/latest/session-format)) | `pi --session <path>` (`PiRuntime.build_resume_plan`), cwd from history; interactively `/resume` or `pi -c` | The in-flight tool call (the loop is gone; the tree may keep a `tool_use` with no result), unsent composer text, the visible screen and scrollback, and any shell state outside the agent |
| **agy (Antigravity)** | Same; plus the per-conversation lock file it held under the Antigravity app-data root | The conversation store addressed by the conversation id that `liveness` reads back from the pane argv | `agy --conversation <id>` (`AntigravityRuntime.build_resume_plan`). Official: `-c/--continue` resolves the last conversation for the workspace from `last_conversations.json` and verifies it server-side; `--conversation <id>` loads a specific one ([Antigravity resume](https://antigravity.google/docs/cli/commands/resume)) | Same as pi. Extra hazard: if the id no longer resolves, `agy -c` **silently starts a fresh session for that workspace** (same doc). The retirement record must therefore store the exact id, and resume must verify identity after start through the existing claim/binding path rather than assume it landed |
| **codex** | tmux session; the `corral.codex_proxy` wrapper and the app-server it spawned; thread state held in that server | `~/.codex/sessions/**/*.jsonl` rollout (thread id, items, tool records) | `codex resume <thread-id>` (`CodexRuntime.build_resume_plan`, wrapped for hosted identity); a new app-server comes up with the new pane | Same as pi, **plus a documented failure mode**: resuming a thread interrupted while a tool call was still running can leave the thread stuck at `Working` until the JSONL is repaired by hand ([codex#12382](https://github.com/openai/codex/issues/12382)). This is why INV-2 is non-negotiable for Codex and why a Codex session must never be retired on a timestamp alone |
| **opencode** | tmux session; the pane's client process | The session store (`ses_…` id, messages) owned by the OpenCode background service | `opencode -s <ses_id>` (`OpenCodeRuntime.build_resume_plan`); new hosted panes pre-reserve the id via `opencode_identity.bind_hosted_ident` | Same as pi. **Extra, and the highest-risk case:** OpenCode by default discovers or starts one shared background server for the account, and that server owns sessions, tools and execution ([OpenCode CLI](https://opencode.ai/docs/cli)). Killing the pane can therefore leave the *session still executing*; and conversely a busy shared server makes every pane look idle by tree inspection. See §6.4 |
| claude (for completeness) | Same | `~/.claude/projects/**/<id>.jsonl`, possibly with a `continued-in` forward pointer | `--resume <chain-latest>`; the runtime resolves the forward pointer before building the plan | Same; additionally the resume must resolve continuation, or a second live copy of the same agent runs (the 2026-09-30 incident in [`SESSION_SCANNING_KNOWLEDGE_BASE.md`](../SESSION_SCANNING_KNOWLEDGE_BASE.md)) |
| cursor | Same | `store.db` history + user hook events | `cursor-agent --resume <id>` | Same |

### 6.3 Codex: the resume hazard is a retirement constraint

Codex is the one runtime where a *correct* retirement can still produce a broken session, because the failure happens at resume time in the agent, not at retire time in Corral. That makes INV-2 stricter here than anywhere else: a Codex session must never be retired on a timestamp, only on terminal history evidence plus a clean process tree plus a clean transcript tail. If those three are not all present, it stays resident. This is a deliberate asymmetry: a few hundred MB against a thread that needs manual JSONL repair.

### 6.4 OpenCode shared server: a blocking open question

Two facts collide. (a) The default OpenCode topology is one account-wide background server, so an agent turn may be executing in a process that is not a descendant of the pane. (b) INV-1 and INV-2 are evaluated against the pane tree and the transcript.

Three options, none of them free:

- **A. Treat a reachable shared `opencode serve` as busy** for every OpenCode session that shares it. Safest; makes OpenCode sessions effectively non-retirable while the service runs, which for this owner's machine is most of the time.
- **B. Retire the pane and accept that the session may keep working**, then reconcile the result on next scan. Preserves memory savings; breaks "retire means the work stopped" and can produce an agent that finishes a task nobody is watching and cannot be re-attached to (no pane, no terminal).
- **C. Detect per-session execution state from the store** (the `session_message` tail already used by `attention_signals._inspect_opencode_v2_tail`: `finish=tool-calls` + live ⇒ working). Most correct; needs a per-runtime *session-level* liveness probe that does not exist today.

Recommendation: **A** until **C** exists, and record `opencode_shared_server: true` in every OpenCode verdict so the limitation is visible in the audit rather than implied by silence. This is owner decision D5.

### 6.5 Resume failure is not a user task (owner rule, 2026-11-15)

The owner's words: *nothing is allowed to fail, and a failure is never handed back to him to deal with.* Two consequences, both binding on this design.

**Resume reliability decides eligibility.** A session may be retired only if bringing it back is guaranteed by construction: native history present and readable, a resume path verified for that runtime on this machine, and no known runtime defect in the way. Codex is the live counter-example (§6.3): a thread interrupted mid-tool-call can return stuck at `Working` and need hand repair of the JSONL ([codex#12382](https://github.com/openai/codex/issues/12382)). So a Codex session is retired only under the full INV-2 evidence, and where that evidence is missing it stays resident. Occupying memory is the cheaper error; an unrestorable session is not.

**Failure is Corral's to absorb.** "Retry" is never a user-facing action and never a button:

- A resume that fails is retried by Corral itself under the same per-session `flock` INV-7 defines. Bounded attempts, backoff between them, every attempt and its cause recorded.
- The retry sequence is invisible by construction: the user sees one pending indicator for the whole sequence, never a failure followed by a recovery, never two spinners, never a state that flips back and forth.
- A failure is reported only once it is established as unresolvable — the runtime reports the record gone, or every bounded attempt returned the same deterministic error. The report states the fact, keeps the transcript reachable (names its path or offers to open it), and asks the user to do nothing.
- The audit carries `resume_retry` and `resume_failed_terminal` events, so an unresolvable failure is diagnosable from the log alone and never needs the owner to investigate.

What this rules out, explicitly: a toast that says the restart failed, a dialog offering "retry", copy that blames the session, and any path where the user must notice a failure in order for the product to recover.

## 7. Server loss as a first-class case

### 7.1 What went wrong at 12:07

`events.log` had zero lifecycle events, so after the fact there was no way to tell whether the server was killed, crashed, was jetsammed, or was restarted by launchd. `tmux_server.plist_payload` sets `KeepAlive=false`, so a dead server stays dead until the next `new-session`; that is the right choice for a server full of agents, but it also means nobody restarts it and nobody notices. And `observe._MAX_LOG_BYTES = 256 KiB` with a single `.1` generation means a burst of scan events can rotate a lifecycle record out within minutes — writing the record into `events.log` alone would have failed again.

### 7.2 What Corral must record

Four events, in a dedicated append-only file, `~/.cache/corral/hosted-lifecycle.log`, JSONL, retained 7 days and pruned by whole day when the file passes 2 MiB, and never rotated into the 256 KiB `events.log` budget:

| Event | Fields |
|---|---|
| `keepalive_server_started` | socket, pid, launchd label, `interactive_job`, priority, plist digest, tmux version, config digest, whether `ensure_server` started it or found it running |
| `keepalive_server_exit` | socket, pid, uptime, `reason` (`corral:kill-server` / `corral:migration` / `signal:SIGKILL` / `exit-empty` / `unknown`), `initiated_by` (requesting Corral pid or actor) |
| `keepalive_server_lost` | socket, last known pid, last heartbeat, `recorded_exit: true|false`, and the survival evidence gathered at the next start: free/compressed pages and swap from the last pass, whether any `ReportCrashService`/`kernel` report appeared in `/Library/Logs/DiagnosticReports` inside the window (one directory listing, no `log show`), plus a bounded tail of `keepalive-server.log` when non-empty |
| `hosted_session_died` | name, runtime, ident, last known state, last state timestamp, `cause` (`server_lost` / `pane_died` / `unknown`), and the resume inputs needed to bring it back |

Plus a continuous **inventory** `~/.cache/corral/hosted-inventory.json`, rewritten at most once per 60 s and on every host/stop: `{server_pid, server_started_at, sessions: {name: {runtime, ident, session_id, history_path, cwd, state, state_at}}}`. It is cheap: the pass already lists sessions, and the file is a few kilobytes. It is what makes "who was on the server when it died" answerable at the next start.

### 7.3 Detecting the loss

- **Corral-initiated.** Every Corral path that ends the server writes `keepalive_server_exit` **before** acting, so its absence is itself evidence.
- **Crash / jetsam / signal.** Detected either at the next `ensure_server` or by the background lifecycle pass (`reclaim._run_pass`, even when `probe_hosted()` returns `[]` because no session has restarted yet): the recorded `server_pid` is gone, no exit record exists ⇒ `keepalive_server_lost` with `recorded_exit: false`, then one `hosted_session_died cause=server_lost` per inventory entry.
- **Individual pane death.** `pane_liveness == dead` during a pass ⇒ `hosted_session_died cause=pane_died`. Today this is inferred silently by the next scan; it must be recorded.

### 7.4 What happens on restart

1. `ensure_server` starts the server as today (under launchd `SoftResourceLimits.NumberOfFiles = 16384`, `HardResourceLimits.NumberOfFiles = 65536`, and `WorkingDirectory = cache_dir()`, pruning `keepalive-server.log` above 1 MiB) and records `keepalive_server_started`.
2. The loss is reconciled against the inventory: every session on the dead server is marked `died`, with the resume inputs preserved. Nothing is deleted.
3. **Nothing auto-resumes** by default. Relaunching 43 agents because the machine came back is how the incident would repeat. Sessions appear as ended cards; the user resumes what they need, and the resume path is the ordinary one.
4. If the owner wants an opt-in convenience, it must be a separate, explicit setting with a hard cap (for example "resume at most N sessions that were `working` at loss"), never a default. Owner decision D3.
5. **Superseded 2026-11-15 (owner ruling).** Steps 3 and 4 describe the pre-ruling design and no longer hold. The owner's rule: the user's attention belongs to the sessions, not to the tool; anything the tool can heal, it heals silently. Since a server loss may never be announced, a silent loss with no recovery is not an option either — it is the worst one. Server loss now self-heals without asking and without telling. See §7.6 for the mechanism and its cap.

### 7.6 Silent self-healing (owner ruling, 2026-11-15)

The owner's rule, in substance: *the user's attention is on the sessions, not on the tool; whatever the tool can heal, it heals transparently and never interrupts work.* Applied to server loss, that forbids every notification path and simultaneously forbids silence without recovery.

So a lost server is healed without a word:

1. **Detect and restart.** Corral's supervisor (`SessionHub` background loop + `reclaim`) detects an unrecorded `server_lost` (`recorded_exit: false`), restarts the tmux server on the same socket with `keepalive.ensure_server()` (never touching a server already alive), and queues lost sessions from inventory. No dialog, no banner, no toast, no notification, no badge, no line the user must read.
2. **Restore, in order, under a rate cap.** Sessions that were `working` at the moment of loss are resumed first — those absences are noticed within seconds — then `waiting_on_user`, then remaining hosted sessions. Restores go through `retirement.restore_session`: same lock, same retries, same silence. The cap is a **rate**, not a total: at most 3 restores in flight (`RESTORE_IN_FLIGHT_MAX = 3`), the next starting as the previous completes/reaches readiness. Sessions from a Corral-recorded exit (`recorded_exit: true`, `reclaim`, or `kill-session`) are never auto-restored.
3. **Keep the place.** A restored session appears in the same row, same position, same title, same conversation. The only difference may be its first frame arriving a second later.
4. **A session that cannot be restored silently is not restored silently.** If its history is gone or the runtime reports the thread unusable, its row keeps the transcript and its state reads as ended. That is a fact the user meets when he clicks it — not an interruption — and §6.5 forbids turning it into a task for him.
5. **If healing itself keeps failing** — the server cannot stay up — that is the one case where the tool is genuinely broken rather than healing, and it may say so in the surface that is already open. It still may not pop anything over the user's work.

The measure of success: the owner never learns that the server died unless he asks. Everything needed afterwards is in §7.2's journal, which he can read when he cares.

### 7.5 The observability gap, stated plainly

Today: no lifecycle events, no inventory, no cause, and a log budget that would evict them. After this design: an append-only lifecycle file whose retention is larger than one bad hour, an inventory that answers "who was there", a recorded exit when Corral ends the server, and `recorded_exit: false` as an explicit finding when it cannot. What Corral still cannot know is *why* a third-party kill happened (jetsam vs. `kill -9` from a shell vs. a panic). The design says so rather than guessing, and it captures the one cheap corroborating fact (the crash-report directory listing).

## 8. Surfacing (behaviour only; no SwiftUI here)

### 8.1 Session list (TUI and both native clients)

**The owner's daily surface is the native Mac client, not the TUI** (clarified 2026-11-15); the TUI and the remote payload follow the same rules, but any UI work lands in the client and belongs to the primary model, not to a delegated worker.

A retired session is an ordinary ended card: `hosted=false`, `live=false`, history intact, same id, same title, same last message. Its rows must not gain a fifth dot: `activity_board` fixes four markers (`waiting`, `working`, `unread`, `recent`) and a retired session must resolve to none of them, exactly like a session with terminal native evidence today.

**Nothing on the surface says a session was retired (owner ruling 2026-11-15).** The list itself must not sort, group, filter, grey out, badge or annotate differently — retirement is not an event, and the owner's attention belongs to the sessions, not the tool. That supersedes the earlier D4 copy change ("ended because it was idle — Enter to resume"): there is no explanatory copy, anywhere.

The only visible consequence is the one the owner causes himself, when he opens the session:

- while the resume runs, the content area shows one indeterminate progress indicator and **no text**;
- the retry sequence of §6.5 is invisible — one indicator for the whole sequence, never a failure followed by a recovery;
- a failure that is established as unresolvable keeps the transcript visible and states the fact without asking him to act, retry or repair (§6.5).

### 8.2 Activity board / Attention filter

Must behave as ended: excluded from `recent` (the `resolve_active_marker` terminal-evidence guard already suppresses `recent` for `STATUS_DONE` + non-empty `completion_id`; the retirement record joins that guard rather than adding a branch), absent from Attention after the 30-minute window, never unread.

### 8.3 Remote client

- Payload: `retired_at` plus reason may be added to `session_payload` **for diagnostics only**; no client may render explanatory copy from it (owner ruling: a retired session looks like any other ended session until the owner opens it).
- Watching a screen: `session.watch` on a retired session must keep returning the existing `unavailable` / `remote.err.no_live_screen`. Do not invent a new error.
- Resume: reuse `session.resume` unchanged. `terminal_attach(vote=True)` already native-resumes through `_keepalive_name(resume_if_needed=True)`; the phone's chat input path already resumes through the same call. A readonly pairing never resumes (unchanged) and therefore sees the conversation, not a terminal.
- Push: none. The silent contract stands, and the existing `remote_push_queued` abort path must not gain a "session retired" reason.

### 8.4 The resume trigger, stated once

There is exactly one resume path for all surfaces: `SessionHub.resume_session(key)` → `registry.build_resume_plan(session)` → `keepalive.wrap_plan` → `new-session -A`. Retirement adds nothing to it. What retirement adds is the *precondition*: the session must still be resumable, which is what INV-5 guarantees.

## 9. Rollout and revert

### 9.1 Default state

| Lever | Default | Env |
|---|---|---|
| Retirement on/off | On (unchanged) | `CORRAL_RECLAIM=0` disables everything |
| Age thresholds | 120 / 10 min (unchanged) | `CORRAL_RECLAIM_IDLE_MINUTES`, `CORRAL_RECLAIM_PRESSURE_IDLE_MINUTES` |
| Absolute floor | 30 min (new constant) | none: a fixed invariant, not a product setting; tests may inject it |
| Resident parked budget | 12 (new) | `CORRAL_RECLAIM_BUDGET=0` disables only the budget lever, keeps age-based retirement |
| Resume grace / cooldown | 10 / 30 min (new) | not configurable; constants |
| Pressure hysteresis | on (new) | `CORRAL_RECLAIM_PRESSURE_SAMPLES`, `CORRAL_RECLAIM_PRESSURE_DISARM_MINUTES` |
| Dry run | off | `CORRAL_RECLAIM_DRY_RUN=1` emits `reclaim_would` with full evidence and kills nothing |
| Lifecycle log | on | shares `CORRAL_CACHE_DIR` |

All knobs go through `legacy_names.getenv` / `env_is_disabled`, so the `SC_` and `PICKUP_` legacy prefixes keep working, as every other knob in this module does.

### 9.2 Revert

Revert is `CORRAL_RECLAIM=0`, or a downgrade to the previous version. Nothing needs migrating: the retirement record, the inventory and the lifecycle log are additive, read-only to every existing consumer, and ignored when the feature is off. Reverting never strands a session: disabled retirement means the session is simply not stopped.

### 9.3 Evidence that justifies tightening or loosening

| Observation | Reading | Action |
|---|---|---|
| `reclaim_would` entries whose session was resumed within 24 h and whose new turn opened with "where were we" / recovery language | Retirement is costing real work | Raise the idle threshold or the floor; investigate the missing `waiting_on_tool` evidence |
| `reclaim_blocked` dominated by `attached_client` while pressure is sustained | Panes are being left attached by a tool, not a person | Investigate the attach source; do not lower thresholds |
| Resident count above budget for > 30 min with pressure level 2 | Budget too generous for this workload | Lower the budget (12 → 8) |
| Pressure armed but resident footprint well under budget | Thresholds are too aggressive | Raise the pressure-idle threshold 10 → 20 min |
| Any `hosted_session_died cause=server_lost` | Server stability problem, not a retirement problem | Investigate separately; retirement is not the remedy |
| Swap > 80 % with pressure level 1 | The sticky-swap trap | Confirm the ratio is not being used as a trigger; no action on thresholds |

## 10. Acceptance

### 10.1 Proving memory actually returns

Numbers are per-session and aggregate, before and after, on the same workload, with the machine otherwise quiet. `footprint` is the primary metric because it is the one that counted in the incident.

| Metric | Command | Expected |
|---|---|---|
| Per-session physical footprint | `footprint -p <pid> -f bytes` (or `footprint -p <pid> -w`) before retirement; 0 / absent after | The retired process and all its children are gone; the summed last-measured `phys_footprint` matches the footprint the machine reports as released, within compressor lag |
| Swap | `sysctl vm.swapusage` before/after | Used volume falls. It may not fall to zero: macOS does not compact eagerly, so a flat used value with falling compression is a pass |
| Compressor | `vm_stat` → "Pages occupied by compressor" × page size, before/after, plus "Pages stored in compressor" | Falls; this is where the parked sessions actually lived |
| Paging activity | `vm_stat` "Pageins", "Pageouts", "Swapins", "Swapouts" deltas over a fixed 10-minute window | Falls toward idle |
| Pressure level | `sysctl kern.memorystatus_vm_pressure_level` | Returns to 1 |
| Corroboration only | `vm_stat` free/active/inactive; `top -l 2 -n 0 -s 2` second sample idle %; load average | Improves; never used as the pass criterion |

The measurement must be taken **before** the pass and **after** the machine has had time to reclaim — the release is visible in the compressor counters, not instantly in swap. A test that reads swap two seconds after the kill and declares failure is measuring the wrong thing.

### 10.2 Proving no work was lost

1. **History unchanged.** For every retired session, the history file's size and sha256 are identical before and after. Any diff is a defect.
2. **Card identity unchanged.** Same key, same id, same title, same last message id and count, after the next scan.
3. **Resume restores the same conversation.** Resume each retired session and compare `load_conversation` message count and last message id to the pre-retirement values; the terminal must come back, not a "session not found".
4. **Nothing was pending.** For each retirement, the audit's INV-2 evidence must show a terminal `status_tag` and zero pending markers. A retirement with `evidence.pending_task_ids` non-empty is a test failure.
5. **Race.** On a private socket: start a session, park it, then retire and resume within the same second from two threads. Assert exactly one pane, one live process, no duplicate history, and that the loser of the lock re-read rather than retried.
6. **Budget path.** Construct 15 parked synthetic sessions on a private socket with a budget of 3; assert the pass retires the 3 longest idle and that the next passes converge without oscillation.
7. **Server-loss replay.** On a private socket with 20 sessions: `kill -9` the server, restart, assert one `keepalive_server_lost` with `recorded_exit: false`, 20 `hosted_session_died cause=server_lost`, 20 intact histories, and 20 resumable records. This is the 12:07 case made testable.
8. **Living-session path.** With a real session doing work (a tool that sleeps), assert zero retirements across 10 passes and that `reclaim_blocked` names `busy` or `background_busy`.

## 11. Decisions the owner must make

| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | May budget pressure pull a session below the 120-minute threshold, and how far? | (a) never — budget only orders candidates that already passed 120 min; (b) down to a 30-minute floor; (c) down to 10 min | (b), with the 30-minute floor as an invariant rather than a knob |
| D2 | Is a durable audit a precondition for retirement? | (a) yes — no record, no kill (INV-9); (b) keep today's swallow-and-kill | (a). The incident's cost was diagnosis, and this is the cheapest insurance |
| D3 | After a server loss, does anything auto-resume? | (a) nothing; (b) opt-in capped resume of sessions that were `working` | (a) by default, (b) only as an explicit setting |
| D4 | Is a retired card visually distinguishable from a crashed one? | (a) identical, existing "Enter restart" copy; (b) different copy in the pane/detail, same list row | (b) |
| D5 | OpenCode's shared background server | (a) treat a reachable shared server as busy (sessions effectively non-retirable); (b) retire the pane and accept that work may continue; (c) build a session-level execution probe first | (a) now, (c) as the follow-up |

## 12. What this design does not know

Stated so an implementer does not invent it:

- **Which protection actually fires, and how often.** No pass ever recorded a reason histogram, so the relative weight of `working`/`waiting` versus `attached_client` versus `grace` on this machine is unmeasured. §4.4 step 3 exists partly to produce that data.
- **Whether any of the 34 parked sessions at 12:07 were resumable in practice.** The histories survived, but no resume was attempted, and three runtimes have known resume hazards (§6). The first rollout should resume a sample and verify content, not just process liveness.
- **The per-runtime footprint of a *freshly resumed* session** versus a long-parked one. Resuming 12 sessions at once concentrates the cost again; the budget should probably count resuming sessions as parked for its first minutes. Not measured, so not specified as a number.
- **Whether `session_activity` advances on agent work at all.** It did not in one controlled test; the trigger set is undocumented. Treat as a one-way signal until measured on each tmux version in use.
- **The exact jetsam threshold for the tmux server on this hardware.** The incident shows it was reached; no band or limit is configured or recorded by Corral, and `kern.memorystatus_vm_pressure_level` does not expose it.

## References

- Apple memory pressure: [DispatchSourceMemoryPressure](https://developer.apple.com/documentation/dispatch/dispatchsourcememorypressure), [dispatch_source_memorypressure_flags_t](https://developer.apple.com/documentation/dispatch/dispatch_source_memorypressure_flags_t) (NORMAL / WARN / CRITICAL), [sysctl levels 1 / 2 / 4](https://newosxbook.com/articles/MemoryPressure.html), [userspace mapping and no-root readability](https://github.com/giampaolo/psutil/issues/2725)
- `phys_footprint` versus RSS: [ledger formula](https://www.bazhenov.me/posts/activity-monitor-anatomy), [footprint(1)](https://www.unixtutorial.org/commands/footprint), [top MEM is footprint](https://github.com/elastic/integrations/issues/20924)
- Swap and compressor: [why swap stays used](https://mole.fit/blog/mac-memory-pressure-explained), [swap totals and page size](https://eclecticlight.co/2022/12/02/tracking-swap-space-is-it-wearing-out-your-ssd)
- CPU time: [BSD `ps` %CPU is a decaying average](https://stackoverflow.com/questions/71865175/how-to-get-the-cpu-of-a-process-in-freebsd), [`cputime` is cumulative](https://jihoony.gitbook.io/developers-notes/developer/c-c++/calculate-cpu-usage-on-linux-as-top)
- tmux: [formats table](https://man7.org/linux/man-pages/man1/tmux.1.html), [window activity counts foreign send-keys](https://github.com/gastownhall/gascity/issues/3049)
- Runtimes: [Pi session format](https://pi.dev/docs/latest/session-format), [Antigravity resume](https://antigravity.google/docs/cli/commands/resume), [Codex resume stuck on Working after an interrupted turn](https://github.com/openai/codex/issues/12382), [OpenCode shared background server](https://opencode.ai/docs/cli)
- Repo: [`MAINTAINER_GUIDE.md`](../MAINTAINER_GUIDE.md) (silent reclaim contract, misleading idle timestamp, background-work rule), [`PERFORMANCE_KNOWLEDGE_BASE.md`](../PERFORMANCE_KNOWLEDGE_BASE.md) (E1-E10 incident measurements, perf-B backoff), [`SESSION_SCANNING_KNOWLEDGE_BASE.md`](../SESSION_SCANNING_KNOWLEDGE_BASE.md) (status versus liveness, Claude continuation hazard), [`REMOTE_KNOWLEDGE_BASE.md`](../REMOTE_KNOWLEDGE_BASE.md) (resume and watch semantics), [`MEMORY_OPTIMIZATION_DESIGN.md`](MEMORY_OPTIMIZATION_DESIGN.md) (footprint-versus-allocation method), [`apple/docs/MAC_PERF_SAMPLING_20261031.md`](../../../apple/docs/MAC_PERF_SAMPLING_20261031.md) (footprint sampling discipline), [`docs/2026-08-16-多助手会话专项汇报.md`](../../../docs/2026-08-16-多助手会话专项汇报.md) (historical, not an authority)

## 13. Owner rulings recorded after the first draft (2026-11-15)

These supersede any conflicting sentence above; where the two disagree, this section wins.

| Ruling | Consequence |
|---|---|
| The owner's attention belongs to the sessions, not the tool. Anything the tool can heal, it heals silently (§7.6). | No prompt, toast, banner, badge or notification for retirement, resume, server loss, or a successful self-heal. Silence + no recovery is the worst outcome and is forbidden. |
| "Nothing is allowed to fail" — a resume failure is never handed back to the owner (§6.5, INV-15). | Resume reliability is a *precondition* for retirement. A failed resume is retried by Corral under the INV-7 lock; no retry button, no repair instructions. |
| No explanation of a retirement on any surface (§8.1). | D4's copy change is withdrawn. The list row does not change at all; the only visible thing is a spinner while the owner's own resume runs. |
| The owner uses the native Mac client, not the TUI (2026-11-15). | UI work targets the client and stays with the primary model. The TUI and remote payload follow the same behavioural rules. |
| Swap size is not a lever (see the host's performance governance guide). | Retirement exists to lower the working set; "add more swap" is never the answer to memory pressure. |
