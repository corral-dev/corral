# corral 性能知识库

## 什么时候读

改、评审、优化或排查启动、会话扫描、对话预览、内嵌终端渲染、**侧边栏列表重建与分屏加格**、缓存、原生扩展、安装包或发布流水线时先读本文；**排查「电脑忙时 corral 卡、自身占用却不高」「corral 内新开任何助手都慢、外面直接启动秒开」「新开会话要等半分钟 / 内存被托管会话占满 / swap 爆 / 整机 load 两百多」「自身 CPU 占用过高 / 风扇狂转 / 两个窗口特别吃 CPU」「Cursor 进程过多 / 活动监视器一堆 agent / cursor-agent」「同类会话管理 / 内嵌终端 TUI 的性能坑」「打开大历史第一次解析整份 JSONL / 详情把通道堵住」时也读**（见「系统高负载下的调度优先级」、「托管子进程被限流」、「Slow new sessions / laggy UI = machine out of memory」、「自身占用过高」与「同类应用踩坑地图」）。**性能优化动手前先做一轮外部调研**（同类 TUI / 终端工具的公开优化经验），再结合本地计时拆解，不要只靠本地 profile 闭门造车（机主 2026-08-17 纠正；本地计时的做法见「新开分屏（加格）链路」节）。各助手历史语义仍以 `SESSION_SCANNING_KNOWLEDGE_BASE.md` 为准，终端交互语义仍以 `EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md` 为准。

## §0 目录索引

- [什么时候读](#什么时候读)
- [系统高负载下的调度优先级](#系统高负载下的调度优先级为什么自己不重却卡)
- [托管子进程被限流](#托管子进程被限流老保活-server-的子女只分到约-2-cpu2026-09-29)
- [Slow new sessions / laggy UI = machine out of memory](#slow-new-sessions--laggy-ui--machine-out-of-memory-2026-09-29-diagnosis)
- [同类应用踩坑地图](#同类应用踩坑地图会话管理--内嵌终端-tui)
- [性能架构](#性能架构)
- [切换选中会话时的右栏更新](#切换选中会话时的右栏更新)
- [开屏首卡响应](#开屏首卡响应2026-08-17-第二轮修复快照秒开--首铺分片)
- [全文搜索索引](#全文搜索索引)
- [派生缓存边界](#派生缓存边界)
- [原生扩展与分发](#原生扩展与分发)
- [测量与验收](#测量与验收)

## 系统高负载下的调度优先级（为什么「自己不重却卡」）

用户常见体感：电脑 CPU 已经被浏览器 / IDE 打满时，corral 占用并不高，界面却开始掉帧、按键迟钝。根因通常不是业务逻辑变慢，而是**调度等级偏低**：

- **macOS**：未标注 QoS 的线程落在 Default（图形 App 主线程才是 User Interactive）。系统忙时会优先给更高档的进程 CPU，CLI TUI 可被饿死。业界同构事故：键盘重映射工具 kanata 在编译打满 CPU 时按键处理被饿 100–275ms，系统误判长按并自动连发；修法是把处理线程抬到 `QOS_CLASS_USER_INTERACTIVE`（[kanata#2040](https://github.com/jtroo/kanata/pull/2040)）。Apple 官方也写明：界面相关工作要用 User Interactive，否则界面会像冻住（[Energy Efficiency Guide](https://developer.apple.com/library/archive/documentation/Performance/Conceptual/power_efficiency_guidelines_osx/PrioritizeWorkAtTheTaskLevel.html)）。
- **Apple Silicon**：QoS 还会影响更偏向性能核还是能效核。Background 档会被钉在能效核；交互档优先性能核。别把「慢」一两个原因混为一谈——单线程算法慢 ≠ 被钉到能效核（见 Eclectic Light 对命令行工具与 QoS 的辨析）。
- **对策**（`schedprio.py`，v0.24.72 起进入 TUI 时生效）：启动时先撤销遗留的 macOS 后台让位标记，再把主线程提到 User Interactive；抓帧 / 控制通道读 / 鼠标发送线程使用 User Initiated。Linux 尽力 `nice(-5)`，Windows 尽力抬到 Above Normal。调用失败一律忽略，不得挡启动。
- **前台会话不得被后台治理误伤**：corral 正在展示或接收输入的界面及其托管助手属于用户正在等待结果的工作，绝不能被本机性能治理工具标记为后台让位；工具必须识别并拒绝此类目标。排查“列表不慢、但首帧或输入很卡”时，先检查会话及其运行时是否被后台降级，再归因到扫描或 Cursor 重绘。
- **不要**给标题生成守护进程、纯扫描后台也抬到 Interactive——那些可以让路；只保「用户正在看的界面」。远程守护的刷新线程用 `demote_background()`（Utility / nice+5）。
- **优先级反转**：界面线程若同步等更低 QoS 的辅助进程（如未抬档的 tmux 子进程），高负载下仍可能一起卡。macOS 对 Mach IPC 有 QoS override，但对「fork 出去的普通 tmux 客户端」不保证同等提权——因此热路径应走常驻控制通道，并给喂画面的线程也抬档。
- 这解决的是**被别人抢走时间片**，不是替代抓帧节流 / 原生解析等业务侧优化。若空闲时也卡，仍按本文其它节与下方踩坑地图排查。

### 托管子进程被限流（老保活 server 的子女只分到约 2% CPU，2026-09-29）

上一节是 corral 自己被饿死；这里是反过来的镜像：**保活 tmux 里生出来的助手进程被限流**。症状是 corral 内新建 / 恢复**任何**助手都慢（重初始化的白屏等首帧）、外面直接启动秒开；`new-session` 本身也变慢（10 倍），与具体助手无关。

- 实测（同机同分钟，`nice` 均为 0）：`perl -e 'while(1){$x++}'` 在保活 pane 里 10 秒只拿到 2.0% CPU（0.10s），在全新 tmux server 里拿到 63.8%（5.91s）；Mach 优先级 17 vs 23。`opencode --auto` 子进程同样：保活侧 +6s 时 2.5–2.9% CPU、RSS 仅约 69MB、无子进程、无连接，还在 bootstrapping 里饿着（`ps` 状态 `Rs+`），全新侧 8.5%、149MB、已建连接。`new-session` 406ms vs 41ms。
- 机理：连续跑了 2 天的老 server（`tmux -L corral-keepalive` 常驻、无可见窗口）被系统降了重要性等级，fork 出来的子进程继承限流；新 server 无此问题。整机 overload 会进一步放大：当场 load 230、swap 5/6GB 用掉，opencode 这类重初始化（bun + 12 插件 + 6 MCP + 插件对账的 npm/pnpm/bun/yarn/vp 外部探针）2 秒的活被拖成 20–35 秒白屏。
- **Mechanism correction (2026-09-29, measured): it is the launch context, not the server's age.** The keepalive tmux server daemonizes and keeps the scheduling class of whatever started it, and every hosted agent inherits it. `ps -o pri` / `proc_pidinfo` flags on the live machine: iTerm-started processes are PRI 31 with the APPLICATION flag; the TUI running under the `shell-gate-ttyd` web terminal (a launchd job), the `com.x0c.corral.remote` daemon, the keepalive server and all hosted agents are PRI 20 without it. A new TUI opened from iTerm is PRI 31, but its hosted agents still land in the old PRI 20 server. Same perl busy loop (6 s, load ~200): launched from an app-tree shell 48–64% CPU; from a default launchd job 14% (thread QoS user-interactive before spawn 30%, `taskpolicy -B -p` 31%, `posix_spawnattr_set_qos_class_np` 18%, all still PRI 20); from a launchd job with `ProcessType=Interactive` 74–86% (PRI 31). A fresh server looked healthy earlier only because it was started from an app-tree shell. So: whoever first starts the server (web terminal, remote daemon, or iTerm) fixes the priority of every hosted agent until the server exits. **Decision (owner, 2026-09-29: "fix it completely"):** on macOS the keepalive server is always started as its own launchd job with `ProcessType=Interactive` (`tmux -D` in the foreground, so the job *is* the server), so its class never depends on who started it; secrets never go into the plist — the launcher's environment is seeded into the server with `set-environment -g` after start. The already-running clamped server is restarted once (sessions end, history stays, native resume brings them back), and the ShellGate ttyd web-terminal job also gets `ProcessType=Interactive` so a Corral window opened there is not clamped either. Contract and details: `MAINTAINER_GUIDE.md`「会话保活」, bullet "Keepalive server scheduling class". Implemented (`tmux_server.py`); check a live machine with `corral diagnose` → `keepalive_server.clamped`. Userland QoS calls cannot lift a launchd-job tree to app class. Official basis: an unspecified `ProcessType` (= `Standard`) gets "light resource limits … throttling its CPU usage and I/O bandwidth", `Interactive` gets app limits, i.e. none ([launchd.plist(5)](https://keith.github.io/xcode-man-pages/launchd.plist.5.html)); the clamp covers the whole tree and its low-priority I/O makes swapped-out pages crawl under memory pressure ([omnipus#880](https://github.com/elicify-ai/omnipus/issues/880)); a web terminal hit the same clamp on every TUI inside it ([web-terminal#9](https://github.com/code-yeongyu/web-terminal/issues/9)).
- 判别：怀疑这条时跑死循环 A/B——两边各一个 `perl` 忙循环 10 秒，对 `ps` 的 %CPU。两边接近 → 不是这条，按扫描 / 抓帧查；保活侧低一个数量级 → 就是这条，不要先查扫描或重装助手。
- 处置（从轻到重）：先降负载（TUI 里结束不看的托管会话，结束进程不删历史），再用同一组 A/B 复测——负载下来后恢复 → 只是 overload 放大；依然被限 → 续期 server：`tmux -L corral-keepalive kill-server` 一次性杀掉全部托管进程（历史都在，可原生恢复），下次进 corral 自动建新 server。该操作不可逆（进行中任务丢失），必须机主明确授权。2026-09-29 现场：已清 21 个闲置 >6h 会话（56→35），机主裁定其余暂时不动、opencode service 暂不重启。
- opencode 专项：它的首帧慢另有一半是自身插件对账（`opencode --auto --print-logs` 可见 `spawning process npm/pnpm/bun/yarn/vp` + 多次 `plugin reconciliation`）；怀疑时先裸测 `npm list -g --depth=0` 计时（现场阵发性挂起 4 分钟 vs 正常 2.7 秒），不要先改 embed。12 插件 + 6 MCP 属于偏重配置。
- 禁止的误修：不要把这条当成 fork 风暴去拆保活 / 通道（症状相似、根因相反：这里是子进程拿不到 CPU，不是 corral fork 太多）；不要靠 renice 去动 server（机制是重要性继承不是 nice，动 server 影响面大）；核对只用 A/B 复测说话。
- **手机远程守护进程自己的调度档（2026-09-30，已实施）：** `com.x0c.corral.remote` 一直没有 `ProcessType`，即 launchd 默认 clamp（限 CPU + 低优先级 I/O；内存压力下换页更慢，手机请求跟着慢）。本机实测守护进程 PRI 20、NI 10。**Decision: `ProcessType=Interactive`，不用 Adaptive。** 官方 `launchd.plist(5)` 原文：不填则 "the system will apply light resource limits to the job, throttling its CPU usage and I/O bandwidth"；"Adaptive jobs move between the Background and Interactive classifications based on activity over XPC connections"；"Interactive jobs run with the same resource limitations as apps, that is to say, none … this key should only be used if an app's ability to be responsive depends on it, and cannot be made Adaptive"（https://github.com/apple-oss-distributions/launchd/blob/main/man/launchd.plist.5 ，镜像 https://manp.gs/mac/5/launchd.plist ，指引见 https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html ）。远程守护经 websocket/中继服务手机、无 XPC 连接，Adaptive 永远升不上去，只能 Interactive；手机在等列表/详情/输入回显，属于响应性依赖。后台扫描线程照旧 `demote_background()`（线程级 QoS Utility + nice，进程档 Interactive 不覆盖它），`schedprio.py` 无需改。实现：`remote/autostart.py` 的 plist 生成固定写 `ProcessType=Interactive`；`is_installed()` 把缺该键的旧 plist 判为未登记，下次 `corral remote on`（运行中也一样，`enable()` 每次重写 plist 并 bootout/bootstrap）即换新档重启。`bootout` 是异步的，`enable()` 会等旧 job 卸载可见后再 bootstrap，失败则 2 秒后重试一次才回落 `load`（2026-09-30 本机实录：不带等待的直接 bootstrap 会撞 teardown 而两边都失败、守护进程离线约 20 分钟直到手动 bootstrap）。升级后对正在用的机器再执行一次 `corral remote on`。验收：`launchctl print`（`spawn type = interactive`）+ `ps -o pri= -p <daemon pid>` 脱离 20（本机 20→37），`corral remote status` 正常。

### Slow new sessions / laggy UI = machine out of memory (2026-09-29 diagnosis)

Symptom: starting a new hosted session in Corral (any runtime) takes about 30 s before the agent TUI shows in the right pane, while the same agent started in a plain terminal tab is instant. Diagnosed on the owner's 16 GiB Mac, 2026-09-29 roughly 22:20–23:00 local, load average 180–245.

**Relation to the previous section.** That section shows children of an old, long-running `corral-keepalive` server getting ~2% CPU. This one is a separate, compounding cause: the whole machine ran out of memory and Corral's hosted tree is its largest resident. Both can hold at once, and the previous section's A/B does not measure memory pressure and is itself distorted by it. Caveat on the timings below: the startup probes (E2) ran on a *fresh* scratch tmux server, which the previous section shows is not throttled — they prove hosting and agent startup are cheap on a healthy server, not that the old server's children were healthy.

Verified evidence (single snapshots; reproduce each with the command shown):

- **E1 — hosting is not the wait.** `host_session` events: 920 / 498 / 192 (pi) / 1053 ms. Reproduce: `grep '"host_session"' ~/.cache/corral/events.log`.
- **E2 — agents paint fast on a fresh server, even at load ~229.** First non-blank pane capture in a detached session using the same `keepalive.tmux.conf`, no client: claude 1.13 s, codex 0.88 s, opencode 4.66 s; session creation 0.11–0.21 s (30–60 ms for a bare `new-session` on a fresh socket). Reproduce: `tmux -L <scratch> new-session -d -x 96 -y 40 -c <dir> -- <agent cmd>`, poll `tmux -L <scratch> capture-pane -p` every 100 ms until non-blank, then kill only your own scratch server.
- **E3 — the real keepalive server was not busy.** 2167 of 2550 samples in `select`; `list-sessions` took 0.6 / 1.2 / 1.2 s in the first probe and 0.01–0.26 s minutes later (tracks system load, not server work). Reproduce: `sample <tmux server pid> 3`; `time tmux -L corral-keepalive list-sessions`.
- **E4 — the Corral UI process was starved.** In a 6 s native sample the main thread had 4074 samples: 3973 (97%) inside asyncio task steps, about 96 (2%) idle in kqueue; a large share of the busy samples were `take_gil` waits (another thread holds the GIL). All threads together executed only ~2.5 s of Python in those 6 s. Threads present: 4 `_capture_loop`, 5 control-channel `_read_loop`, history watcher, 6 pool workers. Reproduce: `sample <corral TUI pid> 6 -file /tmp/x.txt`, then read "Call graph" for the main thread and "Sort by top of stack".
- **E5 — the UI's own timings blew up.** `list_rebuild` 6975 ms (in_place, normally 4–6 ms), 18274 ms (splice), 47994 ms (full); TUI `scan_all` reason=refresh (280 sessions) outliers of 31–404 s (83713, 112470, 59004, 31048, 38041, 64061, 404545 ms) against a typical 4–8 s. Reproduce: events.log entries with `duration_ms > 3000`.
- **E6 — constant cold rescans.** The remote daemon (817 sessions) rescans about every 20 s taking 3–9 s, with `cache_hit=false` and `shared_index=false` on nearly every line, because ~30 agents keep writing history. Same log.
- **E7 — the machine is out of memory.** `sysctl vm.swapusage`: total 10240M, used 9115M (the previous section recorded 5/6 GB earlier the same day; macOS swap size is dynamic). Cumulative Swapins 21.6M, Swapouts 23.7M, Decompressions 1.23 billion, Pageins 132M. Second `top -l 2` sample: 33–37% user, 62–66% sys, 0.0% idle; 968 processes, 7711 threads, 86 running; load 229 / 221 / 198; `kernel_task` ~185% CPU, `fseventsd` ~31%, WindowServer ~37%, iTerm2 ~24%. Reproduce: `sysctl vm.swapusage`; `vm_stat`; `top -l 2 -n 0 -s 2 | grep '^CPU usage'` — read the SECOND sample, the first is cumulative since boot.
- **E8 — memory demand including compressed and swapped pages, by group** (`top -stats mem,cmprs`, MB, compressed part in brackets): opencode.exe 19 procs 7877 (6233); node 111 procs 4361 (3898); codex 18 procs 3960 (3468); Chrome 25 procs 2553 (2164); claude.exe 7 procs 1444 (1222). `ps` RSS hides this (a codex showing ~106 MB RSS). Reproduce: `top -l 1 -n 400 -o mem -stats pid,command,mem,cmprs`, aggregate by command.
- **E9 — Corral's share.** Descendants of the keepalive tmux server: 207 processes, 13.3 GiB of ~30.8 GiB total demand across 961 processes on 16 GiB of RAM (opencode 4.61, codex 3.88, MCP `server-memory` 1.58, node 1.42, claude 1.13, node_repl 0.36 GiB). About 30 hosted sessions; tmux `window_activity` idle times ranged 0–180 min (an imperfect signal, see `MAINTAINER_GUIDE.md`). Process counts at that moment: 72 node, 42 node_repl, 35 codebase-memory-mcp, 33 server-memory. Reproduce: walk the process tree from the tmux server pid (`ps -Ao pid,ppid,comm`) and sum `top`'s mem per pid.
- **E10 — a large consumer outside Corral's tree.** An orphan `opencode.exe serve --service` (ppid 1) at 160–240% CPU, ~1.0–1.2 GB RSS, 19 children, ~395 open files, next to `fseventsd` at ~31%: the largest single CPU consumer. It was NOT stopped (owner decision needed). Reproduce: `ps -Ao pid,ppid,etime,%cpu,command | grep 'opencode.exe serve'`.

**Why "outside Corral is fast, inside is slow".** Outside, it is one fresh small process with nothing else to wait for (~1 s first paint even at load ~229, E2). Inside, hosting is under 1.1 s (E1); the rest of the ~30 s is the wait on the Corral UI process (E4–E5): a long-lived Python process with one GIL shared by the UI loop, 4 capture threads, 5 control channels, a history watcher and 6 scan workers, whose pages compete for RAM under swap thrash, so each step of the new-session path (`_on_embed_hosted`, list rebuild, first frame) waits on page-ins and on the GIL. Corral also causes much of the pressure: it keeps every hosted agent and its MCP helpers resident (E9, ~43% of memory demand), and before this decision nothing reclaimed them automatically. Status: the statement about the UI process is inferred from system counters and event timings (E3–E7), not from Python-level stacks (see Unverified).

**Ruled out.** Title daemon: in a 60 s window there were 2 spawns (both from the remote daemon, ~24 s apart), each alive ~1.2 s and 0.3 s CPU; a spawn that loses the lock exits in 0.28–0.36 s wall (0.14 s user), about 1% of a core. An earlier burst of ~6 TUI-side spawns in 30 s was transient (pending titles). tmux server busy: no (E3). Reclaim on the host path: at diagnosis time `keepalive.reap_pressure` was a no-op at the default cap of 0.

**Run these first next time** (cheap, in order; under thrash every timing is inflated, so re-measure after load drops): 1. `sysctl vm.swapusage` — used above ~70% of total with Swapouts still climbing means thrash. 2. `top -l 2 -n 0 -s 2 | grep '^CPU usage'`, second sample — idle near 0 with sys above 50% means kernel-bound. 3. Memory by group including compressed, and the hosted tree's share (E8–E9). 4. events.log outliers (E5–E6). 5. Only then the throttle A/B (item 6) and per-thread sampling.

**Unverified / limits.**
- No Python-level stack of the live TUI was obtained: `sample` gives native frames only; `sys.remote_exec` raised `PermissionError: Cannot get task port` (needs root or the `com.apple.system-task-ports` entitlement); `py-spy dump` needs sudo (password required). Which Python task holds the main thread is unknown.
- The decisive A/B is pending: does freeing memory (stopping idle hosted sessions) make `list_rebuild` and host-to-first-frame fast again? Until measured, memory exhaustion is the best-supported explanation, not a proven one; the throttled-children mechanism (previous section) may be a co-cause.
- E2 does not cover the old server's throttling. Whether hosted OpenCode sessions depend on the orphan `opencode serve --service` is unknown. All numbers are single snapshots; `top` mem includes compressed memory and is approximate.

**Mitigation and backlog.** Mitigation: silent automatic reclaim of inactive hosted sessions — contract recorded 2026-09-29 in the "Silent automatic reclaim" bullet of `MAINTAINER_GUIDE.md`「会话保活」; confirm `reclaim.py` and its background tick exist in the installed version before relying on it. Since 2026-09-30 the idle verdict additionally treats a hosted session with pending background work or live non-helper descendant processes as busy (`src/corral/busycheck.py`, shared by reclaim, keepalive reaping and the keepalive-server migration script): one `ps` snapshot plus one `tmux list-panes` per pass/tick, transcript tail-scan only for sessions that would otherwise read as idle. Measured 2026-09-30 on this machine: ~44 ms per tick (one `ps -eo pid,ppid,command` ≈ 43 ms + one `list-panes` ≈ 6 ms; `tests/test_busycheck.py::CostTests` on a private 3-session socket, bound 5000 ms). Never on a UI thread; the reclaim pass already runs off-thread at most once per minute machine-wide. Backlog, NOT implemented: (a) pressure-adaptive backoff for the ~20 s cold rescans in both the TUI and the remote daemon (E5–E6), without lengthening `REFRESH_MIN_GAP` globally (existing rule); (b) move scanning and frame parsing out of the UI process's GIL (E4); (c) the per-session MCP fan-out (`server-memory`, `codebase-memory-mcp`, `node_repl` per hosted agent, E9) lives in the owner's global agentsync `mcp.json`, not in Corral, so lazy or shared servers is a configuration decision; (d) a UI memory-pressure indicator is not wanted for reclaim (owner decision: silent, no notification) — any passive indicator needs a new owner decision.

### Stop redundant full rescans (perf-B, 2026-09-30; instrumented on the owner's machine)

Root causes (measured, not guessed): (1) `cache_hit=false` on nearly every pass because the OpenCode list signature contains the `-wal` file mtime at microsecond precision, which changes on every SQLite write while agents run — so `last_scan_cache_hit_all` is always false and every TUI/remote refresh pays a full `_merge_scanned` instead of `_merge_live_state`. SessKit owns that signature; Corral must not re-implement it. (2) `shared_index=false` on every pass because `try_consume` requires the consumer's whole `keep_ids` set to be present in the published buckets, while `remembered_ids_by_runtime()` on this machine returns 420 ids (mostly stale pins/groups pointing at long-gone sessions) that no limit-50 scan can cover — verified by `try_consume` MISS on a 1.5 s-old same-limit index; the remote daemon (limit 200) additionally can never consume the TUI's limit-50 index. (3) Disk churn: `scan-index.json` (~420 KB) is rewritten by every `scan_all` (~3 s TUI + ~15 s remote), `sidebar-snapshot.json` (~423 KB) on most refreshes, stale `scan-index.json.tmp.*` files linger after killed publishes, and `performance-cache.sqlite3` (56 MB, 32773 `session_meta` rows) is never pruned for stale rows while its WAL grows.

Contracts (acceleration only; any error = miss, `CORRAL_CACHE=0` bypasses everything): (a) the shared index records which keep ids its own scan could not cover (`uncoverable_keep`); a consumer hits iff its keep ids minus that set are all present — same-sidebar processes then hit (a process also consumes its own publish, so a lone TUI scans locally only about every 12 s instead of every 3 s), newly pinned sessions still force a local scan; (b) tried and reverted: letting `SessionStore.refresh` take the light `_merge_live_state` path on stable session-key sets despite a signature miss — three existing `TuiLayoutTests` fail because light merge skips provisional retirement, force-ended suppression and hosted rebinding; teaching light merge those transitions is future work, not this task; (c) per-process index publishes are throttled to at most one per 5 s with identical result keys (still inside the 12 s consume TTL), stale `tmp.*` files are cleaned best-effort on publish, and the sidebar snapshot is rewritten only when session keys change or 60 s elapsed; (d) `PerformanceCache.prune` also drops bounded batches of stale `session_meta` rows (superseded parser version or vanished path); (e) under `reclaim.memory_pressure()` both refresh loops back off without changing the normal cadence — TUI reconcile 60 s→180 s with min gap 3 s→12 s, remote reconcile/min-gap 15 s→60 s — evaluated once per pass (two `sysctl` forks, never on the UI thread); FS-event wakes are preserved, only the polling floor moves. Prohibited: lengthening `REFRESH_MIN_GAP` globally, putting WAL mtimes back into any list signature, or holding a scan snapshot across scans.

### Take history scanning off the TUI process (perf-C, 2026-09-30)

Decision: a dedicated scanner subprocess (`src/corral/scan_worker.py`, run as `sys.executable -m corral.scan_worker`) owns parsing history; the TUI process normally only consumes the shared index and falls back to an in-process scan when the index is missing/stale/unusable. Rationale: `scan_all` + merge run in threads of the same Python process as Textual rendering, so every full parse (1.8–2.1 s for ~227 sessions at limit 50 on this machine, 2026-09-30) steals GIL time from the UI; `try_consume` on a fresh index costs ~3 ms.

Contracts (acceleration only; same bypasses as above, plus `CORRAL_ISOLATE_MANAGED_HOSTS=1` disables everything so tests never spawn workers): (a) the worker scans at `max(tui_limit, 200)` so one publisher covers both the TUI (limit 50) and the phone remote daemon (limit 200) — a limit-50 publisher could never serve a limit-200 consumer (`published_limit < limit` is a miss); both consumers narrow via `_narrow_bucket`. When the remote daemon runs, the worker's `scan_all(prefer_shared=True)` mostly consumes the daemon's publishes instead of parsing, and vice versa — a single scanner does the work instead of two. (b) The worker is a per-user singleton: an `flock`'d lock file plus a `scan-worker.json` heartbeat (pid + time); a second TUI window reuses the live worker instead of spawning another. It calls `demote_background()` on start (background scans must never take Interactive QoS) and exits when its parent pid changes (orphan/backstop) — the spawning store never kills foreign live sessions, only its own worker child. (c) `SessionStore.refresh` keeps its 12 s forced local scan only while no worker heartbeat is fresh; with a live worker the forced-local rule becomes staleness-based (shared hit → merge, shared miss → local scan, which also republishes). New-session latency is bounded by worker cadence (~2 s) + TUI gap, not by the old 12 s local-scan floor. (d) The worker reads sidebar keep ids from the same layout DB as the TUI, so `uncoverable_keep` semantics are unchanged; it performs no `annotate`/liveness calls (no tmux forks from the worker — merging/annotating stays in the consumer). Prohibited: spawning the worker on any UI thread tick without rate-limiting (respawn at most once per 30 s per process), giving the worker Interactive priority, or letting the TUI skip its local-scan fallback when the index is stale (a dead worker must silently degrade to pre-C behavior, never to a frozen list).

### Scan-worker churn backoff (perf-H, 2026-09-30; instrumented on the owner's machine)

Root cause (measured, not guessed): the worker polls `scan_all(prefer_shared=True)` every 2 s, but `try_consume` misses whenever the index is older than the 12 s TTL — so under OpenCode WAL churn the worker pays a full local re-parse about every 12 s even though intermediate passes shared-hit in ~9 ms. Steady-state TTL-miss passes cost 0.6–1.4 s wall (fixture: OpenCode-only re-parse ~0.6–1.2 s at limit 200 with 36 keep ids; live runs add 0.5–1.2 s flap re-parses of claude/codex when their pid/file signatures move), which is ~17–21 s CPU per 90 s (~20% of one core). Lengthening the poll interval alone cannot fix TTL-driven parses; per-runtime signature skip (`registry.scan_all`) already fires — the remaining cost is the changed runtimes themselves.

Decision: the worker backs off full parses while consecutive parses show an unchanged per-runtime session-id fingerprint (message/mtime churn, no arrivals/departures), and keep-alive republishes the previous buckets so consumers never see a stale index. Contracts (acceleration only; same bypasses as perf-C): (a) parse gap ramps 2 s (floor, idle or fingerprint changed) up to 24 s under sustained churn (60 s under `reclaim.memory_pressure()`); a new/removed session id or a changed keep-ids input resets to the floor immediately. Idle→arrival latency is unchanged (~2–5 s); arrivals during sustained churn wait at most ~24 s + TUI gap. (b) Between parses the worker republishes its last buckets at most every 8 s (above the 5 s identical-keys publish throttle, inside the 12 s consume TTL), so `try_consume` keeps hitting and the TUI never routinely falls back to local scans; `DEFAULT_TTL_SECONDS` is unchanged. A parse pass first re-stamps the current buckets when the index is already older than 8 s, so consumers hold on across a multi-second parse under load instead of aging past the TTL mid-parse. The loop reuses one registry for all passes (its per-runtime signature cache then actually skips unchanged runtimes — a fresh registry per pass would re-parse everything every time); parse passes force `prefer_shared=False` because our own keep-alive republishes would otherwise shared-hit forever on stale buckets. (c) A keep-alive republish never overwrites a fresher foreign publish (the remote daemon's): when the index `published_at` is newer than the worker's last parse, the worker adopts the foreign buckets/fingerprint instead. (d) The loop wakes on `HistoryWatcher` FS events with a 2 s floor between passes (no busy loop under constant churn); without a native watcher backend it degrades to the same timed cadence. Prohibited: serving buckets older than the backoff cap, throttling the first parse after worker start, or skipping the parse when keep ids changed (a newly pinned session must still force coverage).

## 同类应用踩坑地图（会话管理 / 内嵌终端 TUI）

与 corral 同形态的产品（多会话列表 + 内嵌实时终端 + 常驻 tmux）在公开仓库里反复踩过这些坑。评审新优化或排查「卡 / 烫 / 风暴」时按图索骥；**已在 corral 落地的用「已做」标出，其余是警戒线**。

| 坑类 | 典型症状 | 业界证据 / 教训 | corral 现状 |
|------|----------|-----------------|-------------|
| 调度档偏低 | 系统忙时掉帧、自身 CPU 不高 | kanata 抬 QoS；Apple QoS 指南 | **已做** `schedprio`（v0.24.72） |
| 每会话 fork 风暴 | 自身 100%+ CPU，大量短命 `tmux` 子进程 | [agent-deck#1728](https://github.com/asheshgoplani/agent-deck/issues/1728)：~60 会话扫状态时 `capture-pane`/`show-environment` 狂 fork；修复含**否定缓存**、超时、合并刷新 | **已做** 控制通道优先 + 存活证据缓存 + 通道池 LRU；禁止在热路径无 `max_age` 判活 |
| 控制客户端过多 | 启动风暴、tmux 服务端背压、交互抢焦 | agent-deck：多实例 × 全会话常驻 `tmux -C` 会挤爆单线程服务端；改为「焦点 / 最近查看」小 LRU 才挂活管 | **已做** `_MAX_CHANNELS`（须 > `MAX_PANES`）；多开窗口时仍要注意别再开「全会话挂管」 |
| 无截止的周期命令 | 系统卡死后客户端挂死，恢复后越扫越疯 | agent-deck：每个节奏性 `tmux` 调用必须带 deadline，否则卡住的客户端占满 CPU 且无法自愈 | 热路径多有 timeout；**新增周期轮询时必须带超时，禁止裸 `subprocess` 无限等** |
| 刷新积压不合并 | 卡顿结束后突然狂刷 | agent-deck#1728 假设：stall 期间定时器积压，恢复后背靠背跑完 | 选择跟随有节流；**新定时器必须「超时则丢旧 tick」，禁止串行还债** |
| tmux 服务端 livelock | 整个 socket 无响应，要 `kill -9` | [tmux#5024](https://github.com/tmux/tmux/issues/5024)：控制模式高压 + 宽字符/emoji 可卡在重绘；Unicode 重的 pane 更危险 | 少开多余 `-C` 客户端；升级本机 tmux；异常时查是否服务端 100% 而非 corral |
| Textual / Python GIL | 界面冻、后台其实在算 | Textual 官方：CPU 活必须 `@work(thread=True)`，UI 更新走 `call_from_thread`；后台线程过重仍会抢 GIL | 抓帧已在后台线程；**禁止**在消息处理里同步重解析 / 全表刷新 |
| 把后台活抬到 Interactive | 耗电、挤掉真正交互 | Apple：只有真正交互才用最高档 | 标题守护 / 全量扫描保持默认或更低 |

### 手机远程画面无变化路径（2026-08-27）

手机订阅终端画面仍需按固定节奏向 tmux 取快照，才能在各助手未提供可靠事件通知时保持实时性；但相同原文、尺寸、光标、历史位置的连续快照不得再进入字符网格解析和逐行差分。`SessionHub` 在抓到原文后先比较这份完整画面状态，相同则直接跳过；画面内容或任一展示元数据变化才解析并交给 `ScreenEncoder`。这不会减少抓 tmux 的频率，却移除了空闲时反复创建单元格、编码行与计算状态行的 CPU/GIL 开销。

`ScreenEncoder` 的“无行变化”不等于“无画面变化”：光标位置、可见性或历史长度变化时，必须发一份空行的增量帧，让手机更新交互态；只有内容和元数据都相同才不发。不得为了这项优化合并订阅者、丢掉慢订阅者的最新变化，或调用 resize。回归：`tests.test_remote_screen`、`SessionHubPayloadTests.test_unchanged_capture_skips_screen_parsing_and_encoding`。

### 手机远程会话历史（2026-08-29）

打开手机上的会话详情时，开发机侧的瓶颈不只是富消息解析。2026-08-29 经公网中继按手机同款请求实测：本机生成 535 条列表摘要约 2ms、约 368KB；同一条 `sessions.watch` 经中继约 13.5s；随后对约 87MB 历史发 `session.watch`，20s 内无回包，中继报心跳超时。列表摘要编码不是主因；主因是常驻进程扫描/解析占住解释器锁导致心跳无法应答，以及把整表闲置会话塞进首包。

分页窗口只减少线路字节；若每次 `session.watch` / `session.messages` / 提问列表都从文件头 `read_all`，大 Codex 历史会被完整解析两到三遍。正确路径：按历史文件签名保存规范化消息（进程内 + `remote-transcripts.sqlite3`）；缓存未命中时第一次打开只从 JSONL 末尾解析当前消息窗口（Cursor 按 rowid 取尾部），向前翻页再补读更早一块，不要为翻一页读完整文件；打开、翻页、提问和实时订阅共用同一把读取器和同一份消息列表；文件变长只从上次偏移继续。改了规范化结果形状或读取语义必须抬 `transcript_cache.PARSER_VERSION`。列表首包截成等待/置顶 + 有限闲置；解析 JSONL 时按块让出解释器锁。禁止为了“推进实时游标”再读一遍，也禁止用 `sessions.list --limit 5` 或本机 unittest 冒充手机路径已通。验收脚本：`scripts/phone_remote_acceptance.py`。架构与禁止的误修见 `docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md`。

**排查分诊（先问「空闲也卡还是只忙时卡」；占用高则先问「几个窗口、侧栏是否在跟着跑着的助手变」）**：

0. **先分清是哪条 Python，不要把别的进程算成界面。** 活动监视器里「Python / corral」常见四类：交互界面（命令行就是 `corral`）、手机远程守护（`remote _serve`，通常 0–3%）、标题补全（`--generate-titles`，一阵）、以及仓库里卡住的 `ci-test.py`（可打满一两核、跑十几小时）。2026-09-14 本机把核打满的是另一路 Codex 留下的界面测试，不是调度窗。
1. **只在电脑忙时卡、corral 自身占用低** → 先信调度档（本机可看线程 QoS；已装 ≥0.24.72 仍如此则查是否卡在等 tmux / 磁盘）。
2. **自身 CPU 高、风扇转、两个界面窗口同时开着特别明显** → 先看事件日志里的全量重扫频率与耗时（见下节「自身占用过高」），不要一上来当成 fork 风暴或抓帧死循环。
3. **空闲也卡、日志里重扫并不密** → 查 fork 数（是否狂出 `tmux`）、控制客户端个数、抓帧是否退回外部 fork、Textual 主线程是否被同步活堵住。
4. **连 `tmux ls` 都卡住** → 怀疑 tmux 服务端本身（livelock），别只在 corral 里加日志。
5. **活动监视器里 Cursor / agent / cursor-agent 进程很多** → 先分清图形版和命令行助手。图形版没开时，这些几乎全是调度界面里打开过、还挂在保活里的 Cursor 命令行会话：**一张卡一只顶层进程，不是泄漏、也不是同一条聊天开了两份**。侧栏显示「就绪」的会话进程也还活着；空闲回收默认约 2 小时，**另有托管软上限 10**（历史：旧版 `keepalive.py` 的自动回收自 2026-09-14 起默认关闭，`IDLE_HOURS=0`、`MAX_SESSIONS=0`，「2 小时 / 上限 10」是更早的默认值。**自 2026-09-29 起默认开启的是静默精确回收（`reclaim.py`）**：闲置 120 分钟（内存吃紧时 10 分钟），且非执行中 / 非等待回答 / 无观看方 / 未置顶才结束，无任何通知，契约见 `MAINTAINER_GUIDE.md`「会话保活」节的「Silent automatic reclaim」条；旧的 `IDLE_HOURS` / `MAX_SESSIONS` 仍只在显式设置时才生效）：以下「超限会关掉闲置 >10 分钟且非「执行中」的卡（进界面 / 新开托管时顺带跑）」仅描述该显式软上限开启时的行为。刚开过不久、或仍在执行中的不会被压关，因此仍可能暂时多于 10。真正打满核的通常只有仍在「执行中」的那几只。问「有没有超限」只数保活托管会话，不要拿活动监视器进程数去比 10，也**不要对真实保活跑回收来“验证”**。不要为了消掉进程个数去关图形版 Cursor、杀 ChatGPT 名下的 node，或当成 fork 风暴去拆保活。要马上减进程：在调度界面结束不需要的托管会话（结束进程不删历史）；不要从活动监视器按短名一把杀光。权威机制见 `docs/MAINTAINER_GUIDE.md`「会话保活」回收段。
6. **corral 内新开 / 恢复任何助手都慢、外面直接启动秒开** → 先跑上节（托管子进程被限流）的死循环 A/B；保活侧低一个数量级就是 server 限流，按那节处置，不要先查扫描或重装助手。
7. **New session takes ~30 s to show in the right pane (a session opened outside Corral is instant), whole machine at load 100+, or swap nearly full** → run the memory checks first (`sysctl vm.swapusage`, second `top -l 2` sample, memory including compressed by process group) per the section "Slow new sessions / laggy UI = machine out of memory", *before* the throttle A/B in item 6: under swap thrash every timing, including that A/B, is amplified and can mislead. Silent reclaim of inactive hosted sessions (contract in `MAINTAINER_GUIDE.md`「会话保活」) is the mitigation; do not run reclaim against the real keepalive socket just to "verify".

更细的控制通道协议与「禁止主线程调 tmux」见 `EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md`。

### 重启托管后的退避（2026-09-26）

杀旧起新会让 Pi 的 pid 快照变化、扫描签名必穿，紧接着的全量重扫（秒级，握住 GIL）会和首帧抓取抢时间片。托管成功后（新建/重启都算）把 `_refresh_cooldown_until` 推后 `REFRESH_HOST_COOLDOWN`（6s）：刷新循环到点后跳过这一轮扫描，让首帧先上屏；FS 事件仍保留，下一轮（≥3s 后）自然补扫。这不是拉长间隔（`REFRESH_MIN_GAP` 不动），属于「超时丢旧 tick」。禁止把 `REFRESH_MIN_GAP` 改大来达到同样效果——那会拖慢所有正常刷新。

### 自身占用过高（2026-08-30 本机实测）

用户说法：「corral 自己不该吃这么多 CPU / 风扇在转 / 开着两个窗口更烫」。这和上一节「自己不重却卡」是反面：这里占用是真的高。

2026-08-30 17:32 本机（v0.24.151）连续采样约 10 秒：两个交互窗口瞬时合计经常 40–80%（单窗口尖峰到 80%+），十分钟量级累计也是每窗口约 20% 核。手机远程守护进程多数时候接近 0，偶尔扫一轮才冒几秒。原生屏幕解析已启用，不是「退回纯 Python 解析」造成的。

当场对照事件日志（约 8 分钟窗口）：全量重扫 250 次、侧栏原地刷新 234 次。其中约 200 条深度的是界面窗口、约 560 条深度的是远程守护。单次界面重扫常见 0.7–2.7 秒，偶发 6.8 秒。根因不是 tmux 短命子进程风暴（控制通道都在、子进程占用接近 0），而是下面三条叠在一起：

1. **只要有助手还在写历史，后台重扫就不会拉长间隔。** 界面最短隔 3 秒扫一轮；连续几轮「集合没变」才会退避到最长 10 秒。判定「变了」包含运行中标记、托管名、修改时间和首尾消息——正在跑的 Cursor/Claude 几乎每轮都在改这些字段，于是永远停在 3 秒一扫，退避形同虚设。远程守护已经改成 15 秒一扫（3 秒全量会把中继心跳拖死），界面窗口没有跟。
2. **每开一扇窗口就独立再扫一遍同一批历史。** 两扇窗口 = 两倍磁盘和解析；日志里会看到几乎同时到达的成对重扫。窗口之间不共享这一轮的扫描结果。
3. **右栏正在跟的实时画面是第二开销，不是主因。** 有控制通道时画面事件最多约 25 帧/秒；2026-07-19 已确认：若每帧再查一次光标/尺寸，常驻就能到 40%+，所以状态查询已降到 5 次/秒。本次采样里抓帧变慢（≥100ms）只有零星几条，且多发生在两扇窗口同时盯同一条会话、宽度不一致的时候（现场有一条实际 165 列、期望 82 列的尺寸漂移）。禁止把「占用高」修成再给抓帧加一层中间态过滤——Cursor 长对话整屏重画那条已经验证无效，见 `EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md`。

**排查时先做：**

- 数交互窗口个数；两扇以上先按「重复全量扫」而不是「单窗口死循环」。
- 读 `~/.cache/corral/events.log` 最近几分钟：`scan_all` 是否约每 3 秒一条、`duration_ms` 是否经常 ≥1000、两扇窗口是否成对出现。`session_count` 约 200 = 界面窗口，明显更大（现场 560）= 远程守护。
- **`cache_hit=true` 却从来没有 `reason=refresh_live`**：不是「没升到 0.24.185」。比较把占位卡算进内存键时，侧栏只要还有未转正托管占位，轻量合并永远进不去。**v0.24.212 起比较忽略占位键**；完整合并仍每 12s 兜底转正。升级后仍全是 `refresh` 才再查旧进程 / 两扇窗 / 远程同盘。
- `ps` 看短命 `tmux` 是否在狂出：没有的话不要走 fork 风暴那条。
- 调用栈：主线程大量解释器求值 + 周期性垃圾回收、若干 `_capture_loop` 线程大多在等，符合「后台重扫在算、抓帧不是主因」。**v0.24.153 之后这条不再够**：先看 `/proc/<pid>/io` 的 `rchar` 速率和主线程 `%CPU`，抓帧把数据灌进 Textual 时主线程会自己到 20–40%。
- `ps -L`：主线程（tid=pid）高、抓帧线程在 `pipe_read`/`futex` → 贵的是把画面画上屏，不是 tmux fork。

**禁止的误修：**

- 不要为了降占用把「修改时间 / 运行中 / 首尾消息」从「是否变化」签名里拿掉——那会让侧栏的运行中标记和相对时间冻住（已有真实 bug）。
- 不要把界面重扫改成远程那样的 15 秒而不先交代侧栏新鲜度代价（新会话、已结束、关注圆点会最多晚一轮才出现）。
- 不要给标题生成或全量扫描再抬调度档。

要降占用，优先让「助手还在跑」时不必每 3 秒付一次完整扫描代价（廉价签名跳过、多窗口共用一轮结果、或把「只有相对时间变了」从完整重扫里拆出去），而不是先砍实时画面帧率。

**已落地（v0.24.153）**：嵌套历史改为逐文件 `stat` + pid 快照做 `scan_signature`（含 Cursor/Kimi），签名未变的运行时跳过完整扫描（含 macOS `lsof`）；`live_processes` 在 Darwin 上一次合并查询 cwd，并按 pid 集合缓存 cwd / 命令行 / 环境。不要退回祖先目录 mtime 或逐 pid `lsof`。未变化窗口的重扫应从秒级降到几十毫秒量级。**列表级 Cursor WAL 仍会每轮打穿签名 → 见下方 v0.24.185**；在那之前 cwd/`store.db`/命令行探测已不再对每个 agent 各 fork 一次。

**已落地（v0.24.185 / SessKit 0.1.2，2026-09-12 本机复现）**：用户可见症状是 TUI 卡死或按键极慢，事件日志里 `scan_all` 约每 3–4 秒一条、界面 `session_count≈178`、远程≈617，单次重扫 P50 约 0.5s、尖峰可到十几秒甚至远程 60s+，进程瞬时 70%+ CPU。根因仍是「助手在写时签名永远变 → 全量重扫 + 整表合并」叠远程同盘争用，不是抓帧死循环。

1. **Cursor 列表级 `scan_signature` 不再包含 `store.db-wal`**（正文缓存的 `extra_version` 仍带 WAL）。流式写入只动 WAL 时复用上一轮 `scan_sessions`；`store.db`/meta 真正 checkpoint 或进程启停仍会失效。**禁止**为「预览要看见 WAL」把 `-wal` 加回列表签名——预览/对话缓存与列表签名是两套版本键。
2. **macOS `ps -axo` 在约 1s 内跨 agent/pi 签名复用**；同一轮 `_merge_scanned` / `_merge_live_state` 内 `tmux list-sessions` 只列一次（`liveness.tmux_list_wave`），禁止跨刷新 TTL（会串单测 mock）。
3. **`SessionStore.refresh`：签名全命中且距上次完整合并 <12s 时走 `_merge_live_state`**（只刷新托管标注与关注圆点，不整表替换）；事件里 `reason=refresh_live`、`cache_hit=true`。完整合并仍兜住新会话与集合变化。
4. **轻量合并前必须 `_memory_keys_match_scan(scanned)`**：内存会话键集合与本轮扫描键不一致时强制完整 `_merge_scanned`。乐观删除先清空内存、磁盘删除失败后若仍走 live merge，侧栏会空着不回来（2026-09-12 修 live 路径时踩过）。禁止「签名命中就永远不看 scanned」。**比较必须忽略 `provisional` 占位**（v0.24.212）：未转正托管卡是故意多出来的键，算进去会让 `refresh_live` 永不出现。

**修完后验收**：必须完全退出再开 TUI（旧进程仍跑旧代码）。再读 `~/.cache/corral/events.log`：助手流式写入期间应出现 `reason=refresh_live` + `cache_hit=true`；若仍全是 `reason=refresh` 且每 3–4s 一条、`duration_ms` 经常数百毫秒以上，先核版本号与 SessKit ≥0.1.2，再查是否两扇窗口 / `remote on` 同盘重复扫。

**已落地（v0.24.186，2026-09-12）——跨进程共享扫描索引 + 首帧优先 + 远程降档**：

用户体感「启动白屏好几秒、会话中又卡」在 16GB 忙机上复现：`load` 一次 28s、首铺 `list_rebuild` full 3.5s、界面与 `remote _serve` 各扫一遍。阶段 0 拆解（负载已回落时）：冷 `scan_all(50)` ≈540ms（OpenCode/Claude 等并行），签名全命中仍 ≈100ms，`annotate` ≈70ms——所以「共享索引跳过整段扫描」比「把轮询间隔拉长」更符合体验优先。

1. **`scan_index`（`~/.cache/corral/scan-index.json`）**：任一方完成本地 `scan_all` 后原子发布；另一方在 TTL（默认 12s）内若发布方 `limit` 覆盖自己的需求且置顶/组成员 id 都在索引里，直接消费（可按更小 limit 收窄）。事件字段 `shared_index=true`。`CORRAL_CACHE=0` 或 **`CORRAL_ISOLATE_MANAGED_HOSTS=1`（单测隔离）全禁用**——后者禁止把开发机真实索引泄漏进 mock 扫描。
2. **`SessionStore.refresh` 每 `_FULL_MERGE_INTERVAL`（12s）强制本地扫一轮**（`prefer_shared=False`），避免只喝共享索引时新会话永远不出现；间隔内可喝共享或本机签名缓存。
3. **启动首帧优先**：有侧栏快照时 `main()` **不**立即起 `load` 线程（`store._load_deferred`），等 Textual 首帧 `call_after_refresh` 后再扫——消除「六个解析线程与首铺抢 GIL」的白屏。无快照仍与 OSC 探测并行开扫。
4. **远程刷新线程 `demote_background()`**（macOS Utility QoS + nice+5）：纯扫描后台给前台 TUI 让路；不得用于界面主线程。

**已落地（v0.24.187，2026-09-12）——变化驱动刷新 + 诚实新鲜度**：

阶段 1 仍会在「助手持续写历史」时按最短间隔付扫描代价。阶段 2 把定时全量改成「有变化再扫」：

1. **`history_watch`**：监视各助手历史根目录；Darwin 用 FSEvents（ctypes）、Linux 用 inotify（ctypes），失败则退回慢速 reconcile。突发写入 debounce ≈0.35s。**`CORRAL_ISOLATE_MANAGED_HOSTS=1` / `CORRAL_CACHE=0` 时默认根目录为空**（单测不得盯开发机真实历史，否则 FS 事件会提前吃掉 mock `scan_sessions` side_effect）。
2. **TUI**：后台重扫最短间隔仍 3s（防抖 thrash）；**空闲最多睡约 60s** 才 reconcile；有 FS 事件立刻醒。无原生监视时 reconcile 退回 10s。
3. **远程**：同样事件唤醒（会话落盘可早于 15s 醒来）；完整扫描最短间隔与空闲 reconcile 仍为 **15s**（不低于旧节奏，保证手机关注态 / 列表不过久冻住）；标题缓存同周期轻量轮询。
4. **诚实提示**：筛选框在列表距上次成功扫描 ≥10s 时显示「N 秒前更新」（`filter.placeholder_*_stale`）；`SessionStore.last_refresh_at` / `refresh_age_seconds()`。

**仍未完（阶段 3）**：SessKit 增量尾读 JSONL/DB，让「有写入」时也不必整 runtime 重解析——空闲成本才能真正趋近 0。

**禁止的误修**：不要为了降占用把刷新固定改成 15s（牺牲空闲新鲜度）；不要砍实时画面帧率；不要把列表签名再塞回 WAL；不要在无 FS 监视的平台上把 reconcile 留在 60s 却不显示陈旧提示。

### 2026-08-31 suzhou：扫描降下来之后，吃核的变成实时抓帧

网页终端觉得卡、把锅甩给 corral「自己太吃 CPU」时用这次的数。现场 v0.24.157、4 核、约 8 路 Cursor agent 在跑、TUI 开着 3 格实时画面；`openconductor` 已停。不是泄漏、不是 fork 风暴、原生 `_native.abi3.so` 已加载。

| 项 | 当场的数 | 判读 |
|---|---|---|
| TUI 进程 | 刚开 34 秒就到 ~37%；稳态主线程 17–33% | 贵的是把画面画上屏 |
| `/proc/<pid>/io` `rchar` | **~24 MB/s** | `capture-pane -e` 把正在刷的 Cursor 屏灌进来（`MIN_CAPTURE_INTERVAL=0.04`，上限约 25fps） |
| `scan_all`（界面，`session_count≈292`） | 每 3–4s 一轮；近 10 分钟 P50 **242ms** / P95 1319ms / 尖峰 **4224ms** | 约 **10% 核**；尖峰会握住 GIL，整机都顿 |
| 侧栏 `list_rebuild` | 144 次扫描里 128 次重建 | 退避到 10s **从未生效**：Cursor WAL/mtime/首尾消息每轮都在变 |
| `corral remote on`（后台常驻） | 另一次 `session_count≈578`、约 15s 一轮，P50 275ms | 独立再扫一遍同一批历史，约 **2% 核** |
| `capture_slow` | 10 分钟 60 条，P50 164ms，最大 1.3s | 多格同时刷 + `host_size_drift` |
| Cursor 历史体量 | `~/.cursor/chats` 30 个工作区 / **518** 条会话 | WAL 一写，签名未命中就得重扫该运行时 |

所以 **v0.24.153 之后主因换了**：扫描不再是 0.7–2.7s 那种大头，但 Cursor 一写 WAL 仍每 3 秒付一次完整扫描；叠加 3 格 25fps 抓帧，单窗口就能到 30–40% 核。这会把苏州 PSI `some avg300` 顶过 20%，网页终端按键回显跟着排队（见 shell-gate 的回显排查记录）。

马上减占用：在调度界面结束不看的托管会话（结束进程不删历史）；少开几格正在刷屏的实时画面。不要为了这次去关图形版 Cursor，也不要给抓帧再加一层中间态过滤。

还没做、且值得做的（不要先砍帧率）：变化驱动的增量索引（阶段 2）。跨进程共用扫描结果已在 v0.24.186 以 `scan_index` 落地（见上节「已落地」）；列表级 WAL 容错与签名命中后的轻量合并已在 v0.24.185 / SessKit 0.1.2 落地。

### 高输出时的画面降载原则（2026-08-31）

“不要先砍帧率”指的是不要把所有画面一刀切降速，拿丢失输入回显的体验掩盖扫描问题；它不等于在高输出时继续无条件生产每一帧。现场的 `rchar` 和主线程占用表明，持续刷屏时真正必须减少的是进入“抓取 → 解析 → 上屏”的帧数。

- 现有控制通道已经把 `%output` 合并成唤醒信号，右栏也按行局部刷新；不得再加“两帧相同才显示”之类的中间态过滤——Cursor 每次重绘的中间屏本身会稳定停留，已验证无效。
- 正确的画面优化是**背压优先的自适应取样**：用户刚输入、正在拖动或切换会话时保持当前即时反馈；助手持续刷屏而界面尚未消费上一帧时，只保留最新画面，跳过不会被用户看见的中间抓取、解析和上屏。本次实现后，`EmbedPane` 对有控制通道的纯自动输出最多每 **100ms** 取一次完整画面，且主线程只允许一项待回写；新快照到来时旧回写会被跳过而非继续排队。键入、粘贴、切换和回滚会打开 250ms 的即时窗口，仍按 40ms 最小间隔抓取；**焦点不在本格时改为最多每 250ms 一帧**（v0.24.212，分屏多路刷屏时降占用，持焦格不受影响）。最终静止帧在下一采样点补上。
- 扫描优化与画面优化并行但不互相替代：先把“仅运行中会话变了”的轻量路径从完整历史扫描中拆出，定期完整核对继续兜住外部新会话、结束状态和关注变化；随后让终端界面与远程守护复用同一轮结果，避免两个进程重复读同一批历史。
- 验收必须同时记录主线程占用、`rchar`、抓帧/上屏次数和输入回显；只看总 CPU 下降不够，不能用漏画最终状态、输入迟滞或侧栏状态延后冒充优化成功。

外部对照：Textual 要求耗时工作离开界面线程，并将界面更新回到其消息循环；tmux 控制模式提供输出通知和流控，但通知本身不是完整屏幕状态，因此仍须保留快照作为权威画面。参考 [Textual Workers](https://textual.textualize.io/guide/workers/) 与 [tmux Control Mode](https://github.com/tmux/tmux/wiki/Control-Mode)；已拉取并阅读 agent-deck 的控制通道实现，其“输出事件只作唤醒、通过控制通道取快照”的模式与 corral 当前设计一致，不应重复造一套平行通道。

### 失焦窗口抓帧降速（2026-09-30）

每多开一扇窗口就多一套抓帧循环（每可见托管格最高约 25fps 事件唤醒 + 自动输出 10fps 取样）；用户没在看的窗口仍按全速刷新，3–4 格时 `capture_slow` 100–700ms 并伴随 `host_size_drift`。**窗口失焦（`MainScreen._app_focused=false`，经 `app_focus` 跟踪）时，该窗口全部 `EmbedPane` 按 `UNFOCUSED_WINDOW_CAPTURE_INTERVAL`（1.0s，即 ≤1fps，低于任务上限 2fps）抓帧；重新聚焦经 `set_window_focused(True)` 打开 250ms 即时窗口并唤醒抓帧线程，立刻恢复全速。** 收回格池的闲置格（`session_name=None`）本来就不抓帧，不在此列。**硬约束**：只降取样间隔，不丢最终帧（抓帧永远取 tmux 最新缓冲，聚焦后立即补抓）、不改托管窗尺寸（`desired_host_size` 登记与 heal 照旧，失焦窗口仍是观看方）、不碰手机镜像（`SessionHub` 独立抓帧）与关注已读判定（`_attention_read` 在失焦时本来就暂停）。回归：`test_unfocused_window_uses_1s_capture_interval*`、`test_window_refocus_requests_immediate_capture`。

## 性能架构

corral 的热路径分为四层：

1. 轻量入口只处理版本、缓存维护、只读 Agent 命令和更新命令；只有进入交互界面时才加载 Textual 与完整界面模块。
2. Claude、Codex、Kimi、Cursor 的历史元数据按源文件精确签名保存为本地派生缓存；OpenCode 继续使用自身 SQLite 查询与注册表内存签名。所有运行时仍并行扫描，缓存写入在一次扫描结束后批量提交。
3. 完整对话先查进程内缓存，再查本地派生缓存；只有源文件签名变化才重新解析。TUI 与 Agent 深度查询共用这一份结果。
4. ANSI 屏幕解析进入 Rust 原生扩展。屏幕解析在释放 Python 全局锁后完成，并直接返回合并后的行文本、样式区间和指纹，避免为每个终端格创建 Python 对象。扩展不可用或显式关闭时自动走语义相同的 Python 参考实现。**JSON 解码一律走标准库 `json`，不进原生扩展**——原因见下面「Rust 的适用边界」。

静态对话预览还缓存完整布局结果；滚动只切可见窗口，不再对每一可见行重复排版整篇对话。实时终端继续按行指纹比较，只重建和刷新变化行。

## 切换选中会话时的右栏更新

侧边栏换一个**活跃**会话，右栏要跟着换实时画面。2026-07-26 在 suzhou 用真实托管会话逐段实测（Pilot 驱动真实事件循环，非估算），一次切换端到端约 170–200ms，构成：

| 阶段 | 实测 | 是否卡住主线程 |
|---|---|---|
| 事件派发 + 活跃判定 | ~32ms（峰值 45ms） | 是 |
| 排队等主线程空闲 | ~6ms | — |
| 右栏整排拆掉重建 | ~30ms | — |
| 重建后铺静态回退内容 | ~55ms | — |
| 开控制通道 + resize | ~14ms | 是 |
| 首帧抓取与渲染 | ~30–60ms | — |

同机单次调用基线：`has-session` fork 约 4.8ms；`capture` 走 fork 约 5.3ms、走已建好的控制通道只要 **0.4ms**；首次建通道约 18ms。**结论是开销几乎全在「判活的 fork」和「整排重建」上，不在画面本身。**

已落地的两项（v0.24.16）：

- **存活证据缓存**（`embed.note_alive` / `is_alive(name, max_age=...)`）：抓帧、状态查询、开通道、创建托管成功都算一次「确认它还活着」并打时间戳；界面层判活（`MainScreen._session_is_active` / `_is_session_active`，TTL `_ALIVE_EVIDENCE_TTL`=3s）先读证据，命中就不 fork。右栏在显示的会话每轮抓帧都会刷新证据，所以这条路几乎永远命中。**判定「会话是否已结束」一律不传 `max_age`**（`EmbedPane._capture_loop` 的三次失败确认），缓存只能加速「确认活着」，不能替代宣告死亡。实测主线程阻塞中位 18.2ms → 9.3ms，命中缓存时 0.6ms。
- **选择跟随节流**（`MainScreen._schedule_follow_selection`，窗口 `_FOLLOW_THROTTLE`=120ms）：leading-edge + trailing，单次方向键零额外延迟，连按时窗口内只保留最后一次。**不能改成纯 debounce**——那会给「按一下」也加上固定延迟，单步反而更迟钝。后台重扫和搜索框过滤后的刷新也走这条节流（它们同样会整排重建右栏）。实测积压 5 次高亮：跟随 6 次 → 2 次，主线程累计阻塞 5.7ms → 0.7ms。定时器延迟下限必须 > 0，Textual 的 `Timer` 用间隔做除法，`interval=0` 会在停表时抛 `ZeroDivisionError` 把屏幕卸载流程带崩。回归：`test_rapid_highlights_are_throttled_but_still_settle`（既断言合并，也断言停下来一定收敛到最后一项）、`test_embed.py` 的三条存活证据用例。

v0.24.17 又补了三项，把上面表里「整排重建 + 重铺回退 + 重开通道」那三段基本消掉：

- **格子就地改绑，不再整排重建**（`PaneCell.rebind` / `SplitPaneArea._mount_panes_async`）：新旧格数相同就复用现有格子，只有多出来的才新挂、超出的才卸。`EmbedPane.focus_session` 本来就支持切换会话（提升抓帧代次、拦住旧回调），不需要靠销毁控件来换会话。**`cell_id` 必须沿用旧的**——格子里 `EmbedPane` 的 DOM id 是 compose 时按它生成的。关格回调也必须改成「按此刻绑着的 spec」解析（`PaneCell._close_self`），构造时闭包捕获的那一个在改绑后就过期了，会关错会话。
- **按会话缓存最后一屏**（`embed_pane._screen_cache`，上限曾为 6）：切走时把网格存起来，切回来先摆上去、后台抓帧几毫秒后用新帧覆盖。恢复必须走 `_sync_strips` 而不是直接赋 `_grid`——`render_line` 的实时分支只认 `_strips`，只设网格会渲染成整片空白。会话确认结束时必须 `forget_cached_screen`，否则再选中它会先摆一屏「像还在跑」的旧画面。
- **控制通道池加 LRU 上限**（`embed._MAX_CHANNELS`=8，须严格大于 `MAX_PANES`）：格子不再卸载，也就不再顺手关掉自己的通道；没有上限的话在侧边栏一路翻下去会攒出几十个 `tmux -C attach` 子进程。淘汰按最久未用，正在显示的格子每轮抓帧都会经 `_active_channel` 续期，天然不会被淘汰。

A/B 实测（同一进程内把挂载协程换回旧实现对照，n=6，口径「按下方向键 → 新画面出现在屏上」）：

| | 右栏换好 | 画面就绪 |
|---|---|---|
| 改动前（整排重建） | 24.9ms | **80.2ms** |
| 改动后（第一次看这个会话） | 32.5ms | **37.1ms** |
| 改动后（切回看过的会话） | 17.0ms | **17.3ms** |

「右栏换好」在冷缓存下反而略高，是因为改绑把 `focus_session`（开通道 / resize）搬进了挂载协程内同步做完，旧实现是挂完再 `call_after_refresh` 补——所以只看这一列会误判，以「画面就绪」为准。

### 分组切换丝滑化（接续）

跨**会话组**切换时身份必变，走改绑而非 inplace；若屏缓存未命中，旧逻辑会同步铺 Markdown 对话回退，观感就像 runtime 整窗重载。另外浏览已有组时误走 `set_group` 会抬 `updated_at` 并整表写盘，堵主线程。

已落地：

- **浏览已有组只 `set_focus`**（`layout_controller._show_session_group`）：目标 keys 与 store 里该组成员一致时走 `_persist_split_focus()`；只有组合真的变了（加格/关格/多选开屏等）才 `_persist_split_composition()` → `set_group`。禁止浏览路径抬 `updated_at`。
- **固定格池**（`SplitPaneArea`）：首次挂满 `MAX_PANES` 个 `PaneCell`，多余格 `-spare` 隐藏；跨组 2↔4 只 rebind/显隐，关格 `park()` 回收进池，不 `remove`。`cells()` / `hosted_identity()` / `ordered_session_keys()` 只报绑定中的可见格。可见最左格用 `-leading` 去左边距（闲置格仍占 DOM，不能靠 `:first-child`）。
- **格数改变时按最终尺寸立即重设，且绝不铺旧宽画面**：单格切多格、或多格切单格时，`rebind` 发生的瞬间旧格仍保留旧宽度；读取它来 resize 会先写错尺寸，随后布局完成又按新宽度写一次。反过来只等布局后的 200ms 防抖，虽避开错误 resize，却会把旧的半宽屏缓存拉伸到新单格左侧。分栏区须根据最终格数和间距预先算出每格内容尺寸，立即 resize；这次格数变化同时清空当前/缓存的旧尺寸画面，首帧只接收新尺寸内容。布局随后发来的相同尺寸回报必须去重，不能在 200ms 后再 resize 或冻结抓帧。**只有格数不变的普通会话切换**才允许复用屏缓存并按当前格即时同步，不能为消除跳变而给每次切换都增加等待。
- **屏缓存扩到 `MAX_PANES * 4`（16）**：覆盖约四个最近分组。冷切换默认空白画布等首帧（`focus_session` 不跑 Markdown 回退）；`detail_until_frame=True` 保留旧回退行为给测试/特例。跟随稳定后后台 `prefetch_cached_screen` 预抓当前组缺缓存的托管帧。**预抓必须先 `parse_screen_rows` 再入缓存**：`embed.capture` 返回的是 ANSI 原文，直接塞进 `_screen_cache` 会在恢复时对字符串逐字符 `_row_to_strip`，真机直接 `AttributeError: 'str' object has no attribute 'wide_cont'` 崩掉（v0.24.61）。`_cache_screen` / `_take_cached_screen` 也要拒绝非行网格脏数据。
- **格池已满时同步改绑**（`_schedule_mount`）：无需新建控件时直接 `_apply_pane_bindings`，少一帧旧画面停顿。

回归：`test_browsing_existing_groups_persists_focus_not_composition`、`test_pane_count_change_reuses_pool_without_remount`、`test_pane_count_change_resizes_once_at_final_layout_size`、`test_two_to_one_discards_half_width_screen_before_first_frame`、`test_cold_hosted_switch_skips_markdown_fallback`。

### 新开分屏（加格）链路（2026-08-17 本轮实测与修复，200 卡规模）

用 Pilot 驱动真实事件循环 + 真实 tmux 托管会话分段计时，加一格的主线程构成与修复后数值：

| 环节 | 修复前 | 修复后 | 手段 |
|---|---|---|---|
| 改绑/开通道 `_apply_pane_bindings` | 12~43ms | 12~20ms（真实终端更低：新格通道由后台 `host_session` 预开，主线程是池命中 ~0ms） | 未动；勿把 `open_channel` 搬主线程外（测试同步断言多、真实收益小） |
| 记忆库写 `persist_split_composition` | 8~18ms | ~1ms | `SidebarLayoutDB` 常驻连接（见下） |
| 列表重建（第 2 格：独立卡→两人组） | **全量重建 951~1079ms，主线程冻结** | splice 27~45ms | 区段 splice（见下） |
| 列表重建（首格/新独立卡置顶） | 360~766ms | 13~45ms | 条纹相位锚定改段尾（见下） |
| 端到端（点击→新格首帧） | 43~155ms（第 2 格另计上面 1 秒冻结） | 57~134ms | 合计 |

三条硬约束（写反了都不报错，只是回到秒级卡顿）：

1. **区段 splice**（`session_list._region_splice` + `_splice_region`）：公共前后缀夹出唯一变化区段，区段外行原样保留，只删/插中间；单行插删是特例，「独立卡→会话组（同位置删 1 插 3）」必须命中它而不是退回全量重建。超过 `_MAX_SPLICE_REGION`（当前 8）或整体换血/重排（一行没保留）仍走全量。固定头（＋新建/看板）不在时（clear() 之后）禁止走 splice，只能全量回补。回归：`test_region_splice_matches_single_and_local_region_changes`、`test_rebuild_falls_back_to_full_rebuild_when_session_set_changes`。
2. **条纹相位锚段尾**（`_assign_block_stripes`）：每次类变更（`set_class`）都触发一次 Textual 全量样式重匹配，200 卡全翻 ≈ 0.7 秒。锚段尾后段首插块零翻转；改动条纹相关代码前先想清楚哪些操作会翻转多少块。区段 splice 后必须 `_apply_stripes(rows)`（奇偶可能翻转）。
3. **`SidebarLayoutDB` 常驻连接**：读写都持实例锁，连接缓存出错就丢弃重开（自愈）；禁止改回每次 `connect+PRAGMA+建表+迁移探测+close`（单次写 8~18ms，且 `read_revision` 是每秒轮询路径）。多窗口互斥仍由 `BEGIN IMMEDIATE` 保证。

## 开屏首卡响应（2026-08-17 第二轮修复：快照秒开 + 首铺分片）

用户可感：启动白屏停在「Pick a session or tap a runtime above」约 2 秒。真实拆解（218 卡规模、真机实测）：

| 阶段 | 修复前 | 修复后 | 手段 |
|---|---|---|---|
| 首帧内容 | 空骨架+提示，卡片要等扫描 | 首帧直接带卡（快照秒开） | `SessionStore._save_sidebar_snapshot` / `hydrate_from_snapshot`（SWR） |
| 首次铺表 | 全量一次性挂载 218 卡，主线程冻结 0.8~1.9 秒 | rebuild 返回时只挂首批 40 行（~几十 ms），尾部空闲帧分批补齐 | `_MOUNT_CHUNK` / `_begin_tail_mount` / `_mount_tail_batch` |
| 扫描完成后的收敛 | （旧版即在此刻全量铺表） | 原地更新 5~20ms | 复用已有 in_place/splice 路径 |

机制与硬约束：

1. **快照（stale-while-revalidate）**：扫描完成后把合并后的会话桶与 `_order` 写进 `~/.cache/corral/sidebar-snapshot.json`（遵循 `CORRAL_CACHE_DIR`，`CORRAL_CACHE=0` 全禁用，原子写）；启动时同步读快照填入 store（~几十 ms，218 卡约 270KB），标记 `hydrated`（≠ `loaded`）。**必须在后台加载线程启动前 hydrate**，否则会被真扫描的合并覆盖。快照只存展示元数据与顺序，不存 hosted/占位等进程内运行时态；运行状态/标题可能滞后一两秒，真扫描经原地更新收敛（实测 10~20ms）。`loaded` 语义不变：空态提示、启动分屏恢复仍等真扫描。
2. **首铺分片**：全量重建同步只挂前 `_MOUNT_CHUNK`（40）行，尾部每 `_TAIL_MOUNT_INTERVAL`（10ms，**必须 > 0**，Textual Timer 间隔做除法）挂一批，批次间可交互。作废机制：`_rebuild_seq` 递增即作废旧尾（rebuild 入口已递增；`clear()` 不走 rebuild，自己手动作废）。分片批必须持 `_rebuild_lock`（与 rebuild 同闸门，防两条消息泵交错），持锁后再验 token。批后幂等重贴分屏标与斑马纹。分片中途身份比对只看已挂前缀，新重建自然走全量再分片，正确性不变。
3. 回归：`SidebarSnapshotTests`（roundtrip/收敛/幂等/损坏降级）、`MainScreenNavigationTests.test_full_rebuild_mounts_first_chunk_and_fills_tail_in_frames`（首批/作废/补齐/条纹一致）。observe `list_rebuild` 新增 `chunked` 字段。

边界（未做，已评估）：敲命令到首帧之间还有 ~0.5s（Python 导入）+ OSC 探测 ≤0.25s，与提示窗口无关；直启子命令路径是同步全扫后进 TUI（另一条流）。**有快照时全量 `load` 推迟到首帧之后**（v0.24.186，`_load_deferred`），忙机上避免与首铺抢锁。Textual 官方 `Reveal` 每 20ms 只挂 1 个（218 卡要 4 秒+），节奏不可用，故自实现按批分片。启动首建 200 卡全量重建的 ~0.6 秒冻结已由本轮分片挂载消除（observe `chunked=True`，首帧只挂首批）。

## 全文搜索索引

`search.ConversationIndex` 是全文搜索弹窗（`Ctrl+F`）的内存索引。它**不自己读历史文件**，一律经 `SessionStore.get_conversation()` 拿正文，因此天然复用进程内 dict 缓存和 SQLite 派生缓存里的对话；新增的磁盘读取量为零。

关键结论：正文体量远小于历史文件体量，别被 JSONL 的大小吓退。本机实测（默认 `limit=50` 共 168 个会话；`limit=200` 共 461 个会话）：

| 指标 | 168 个会话 | 461 个会话 |
|---|---|---|
| 原始历史文件合计 | 725 MB | 1.2 GB |
| 提取出的对话正文合计 | 约 97 万字符 | 约 538 万字符 |
| 建索引（正文已在派生缓存里） | 234 ms | 603 ms |
| 签名全命中的增量刷新 | 0.5 ms | 1.2 ms |
| 查询：`tmux`（窄） | 5 ms | 11 ms |
| 查询：`的` / `a`（最坏，几乎全命中） | 21～30 ms | 29～35 ms |
| 索引常驻内存 | 6.8 MB | 37.8 MB |

由此定下的约定：

- **不引入倒排索引 / FTS5 / 外部搜索库。** 语料量级下朴素子串匹配就是毫秒级，额外索引结构只会增加维护面。SQLite FTS5 的 trigram 分词器对中文尤其不划算——1～2 个字的查询（中文最常见的查询长度）根本索引不到，还得再挂 `LIKE` 兜底。
- **`search()` 必须先判定+排序、再只对要展示的前 `top` 条提取命中行。** 命中行提取（逐行 lower + 定位 + 开窗）是整个查询里最贵的一步，对着几百条命中全做一遍会把界面线程实打实卡住：461 个会话搜单字母实测 305～441 ms，改成只算前 60 条后降到 35 ms。排序键只依赖会话时间、不依赖命中行，所以先排后截不改变前 `top` 条的内容。`SearchOutcome.total` 仍是命中总数，状态行据此如实告诉用户「还有多少条没显示」，不做静默截断。
- **`_clean()` 用 `str.translate` + 懒查表，不要写回逐字符 `unicodedata.category()` 循环。** 建索引原本 90% 的时间花在那个循环上（461 个会话 1289 ms）；查表后整轮建索引降了一半以上。两种写法在 8672 条真实消息上逐条比对过，替换结果完全等价。
- **索引构建必须在后台线程**（`MainScreen._warm_search_index`，`@work(thread=True)`），且**要等首屏画完再开始**（`_schedule_search_index_warm`，延后 `_SEARCH_INDEX_WARM_DELAY`）。后台线程也受 GIL 影响：解析正文期间界面每帧多滞后 4～5 ms（p95 9～14 ms），直接在首屏那一秒开跑实测让首次出卡片慢了 110～165 ms，而首屏目标本来就只有 1 秒。
- **按会话签名增量重建**：签名取扫描结果里的 `path` / `size_bytes` / `file_mtime`，不额外 `stat`（真正读取时 `get_conversation` 自己会校验文件签名）。增量刷新只要 0.5～1.2 ms，所以**每次打开弹窗都要刷一遍**——否则首屏预热之后新产生的会话和新追加的消息永远搜不到（这是最容易漏的一条：索引建好后不再刷新，corral 开着不动几小时就搜不到当天的新会话）。
- `refresh()` 内部持锁串行，预热与弹窗侧的刷新同时触发也不会把同一批会话解析两遍。
- 搜索结果只带会话键和正文命中，展示用的标题 / 时间 / 运行中状态由调用方从当前 `store` 快照取；索引里不存展示态，避免建索引那一刻的旧标题被钉死。
- **内存**：索引把正文存两份（原始大小写的行 + 小写 blob）。默认规模下 6.8 MB，不值得为省这点内存改成「只存 blob、命中再切行」——那样会丢掉角色和时间戳，命中时还得回头重读对话。另外 `_build_entry` 走 `store.get_conversation`，会把全部会话的对话灌进 `store.conversations`（该 dict 无淘汰），预热后实测净增约 10 MB；会话数量级再上一个台阶时，要先给这个 dict 加淘汰，而不是先动索引。
- **JSON 解析一律用标准库**，不要试图为建索引再引入原生 JSON 加速，原因见下面「Rust 的适用边界」里的实测记录。

## 派生缓存边界

- 默认位置：`~/.cache/corral/performance-cache.sqlite3`；遵循 `XDG_CACHE_HOME`，也可用 `CORRAL_CACHE_DIR` 改目录。
- **库路径在每次连接时按当前环境解析，不在导入时锁定。** 2026-10-05 事故：进程级单例 `_CACHE` 在 `import corral.cache` 那一刻就把路径定成真实 `~/.cache/corral`，而 `tests/test_ui.py` 先 `from corral import …` 再设 `CORRAL_CACHE_DIR`，隔离形同虚设；界面测试 mock 的「测试问题 / 测试回复」被写进真实缓存，键是本机托管窗格里真实 Claude 会话的 key 与文件签名，签名一直有效，于是侧栏标题正确、预览格子却是假对话（Enter restart 的已结束会话最明显）。修法：未显式传路径时 `path` 属性每次取 `cache_path()`，路径变了就丢弃本线程旧连接重连。残留脏行用 `corral cache clear` 或删除 payload 恰为夹具的行清理。
- 默认上限 256 MiB；可用 `CORRAL_CACHE_MAX_MB` 调整，最小 16 MiB。超过上限时优先淘汰完整对话，元数据保留以保障启动速度。
- 文件签名包含设备、inode、字节数和纳秒修改时间；Codex 额外包含标题索引签名，Cursor 额外包含提示历史和正文数据库签名。任一输入变化都视为未命中。
- 缓存目录权限为当前用户独占，数据库为当前用户读写。内容只来自用户本来可读的本机会话历史，不上传、不进入项目日志。
- 数据库损坏、锁竞争、只读文件系统或原生扩展缺失都必须降级为未命中，不能阻断原始历史读取。
- `CORRAL_CACHE=0` 可完全关闭；`corral cache status` 查看状态，`corral cache clear --dry-run` 预览，`corral cache clear` 幂等清空。

### 暖缓存扫描的两处开销（v0.24.22 修，别改回去）

暖缓存下扫描已经不是解析瓶颈，而是**缓存访问本身的重复开销**。profile 曾显示一次 Codex 扫描里 `posix.mkdir` / `posix.chmod` 各被调用约 950 次、SQLite `execute` 约 950 次——都不是在读历史，是在重复做无用功。

- **目录准备只在新建连接时做。** `PerformanceCache._connect` 原先每次进入都 `mkdir` + `chmod` 一遍父目录，哪怕线程本地连接早就建好；一次 Codex 扫描白做约 1900 次系统调用。现在连接已存在就直接复用（仅 `create=True` 的热路径；`create=False` 的 `status`/`clear` 保持原样，它们还要靠 `path.exists()` 判断库在不在）。
- **一轮扫描内每个运行时的元数据只查一次库。** 扫描要在上千个候选文件里筛出最近几十条（大量候选会被子代理线程、空会话、目录已删等规则滤掉），逐条查库意味着 Codex 一次扫描发起约 950 次独立查询。`begin_scan()` / `end_scan()` 圈出一轮扫描，期间 `get_session` 走按运行时一次性读入的快照。`registry.scan_all` 和 `agent_api._scan_runtimes` 两个并发扫描入口都要成对调用，`end_scan` 必须放在 `finally` 里。

两条硬约束：

1. **快照严格限定在一轮扫描内。** 做成长期缓存会让同一进程里后续扫描看不到本轮新写入的会话。不在扫描期间的调用方（`store` / `titles`）继续走逐条查询，行为不变。
2. **payload 解码必须保持惰性。** 快照装着该运行时的全部条目（Codex 2686 条 / 2.3 MB），本轮只用得到其中一小部分；建快照时就解码等于白做大量无用功，收益会被吃光。只有签名与解析器版本都校验通过才 `json.loads`。

### 缓存版本绑定 provider 合约（2026-10-01 consumer-upgrade，别改回去）

`session_meta` / `conversation` 的 `parser_version` = Corral `_PARSER_VERSION` +
`sesskit-<本进程实际安装版>`（`cache.provider_cohort()`，单一起源），host-tag
（SessKit 的 `host_cache_tag`）追加在后。SessKit 一升级，cohort 就变：旧行读
直接未命中走新鲜解析，冷刷的 `prune_stale_sessions` 按 cohort 前缀清掉旧合约
行。注意两处曾经写反的版本比较（2026-10-01 修好）：

- **purge 必须按前缀比，不能按裸 `_PARSER_VERSION` 精确比。** host-tagged 行的
  版本是 `cohort + tag`，精确比较会把当期有效行在每次冷刷都删掉；现在只删
  `parser_version NOT LIKE <cohort>%` 的行（LIKE 转义后比较），当期裸行与
  host-tagged 行都保留。读路径仍是精确比较（`_decode_session_row` 与
  `get_conversation`），正确性不依赖 purge。
- **共享快照与 worker 心跳同样带 cohort。** `scan-index.json` 的
  `provider_cohort` 对不上（或缺失）就直接不消费，回退本地扫描；
  `scan-worker.json` 心跳缺 cohort/对不上，本进程 `is_active()` 即为假——
  旧合约的常驻 worker 永不被新合约复用，只等它退出后新 worker 占到单例槽
  （期间消费者走本地扫描，不杀进程、不抢锁）。

实测（同一进程内把行为还原成改动前做 A/B，n=15，本机 168 个会话）：`scan_all` 暖缓存中位 **251.9 ms → 203.3 ms**（−19%），最快 218.4 ms → 175.8 ms。验收差分：走快照与 `CORRAL_CACHE=0` 现解析，5 个运行时的扫描结果逐字段完全一致。

## 原生扩展与分发

### Rust 的适用边界

**原生加速只用在「大量输入压缩成少量结果」的场景**，目前仅终端画面解析（一屏带 ANSI 转义的文本 → 若干紧凑行元组，实测约 27 倍）。

**不要往原生层加 JSON 解析。** 曾经有过一个 serde_json + PyO3 的 `loads`，实测比标准库 C 实现的 json **慢 2.4～2.5 倍**（400 条 payload / 4.8 MB：标准库 19 ms、原生 46 ms），已于 v0.24.22 连同 `serde_json` 依赖一起移除。根因是产出物本身就是一大棵 Python 对象树：Rust 侧要先解析成中间对象树、再逐节点转成 Python 对象，同一份数据构建两遍，还要先 `str.encode('utf-8')`；标准库那条路是 C 直接建 Python 对象。判断新的加速候选时按这条准则看**产出物形状**，不要按「这活是不是 CPU 密集」下判断。

**不要把扫描内核改写成 Rust。** 2026-07-29 专门评估过一次（本机真实数据：Claude 历史 633 MB、Codex 3.2 GB、2124 个 Codex 会话文件），结论是不划算，三条理由按重要性排：

1. **启动耗时的大头 Rust 够不着。** 实测 546 ms = 加载 corral 自身 117 ms + 加载 Textual 198 ms + 扫描 232 ms。前两项合计 315 ms（58%）是 Python 与第三方界面库的导入成本，任何 Rust 重写都动不了，收益上限就摆在那。
2. **日常路径不是解析瓶颈。** 暖缓存下扫描卡在缓存访问的重复开销上（见「派生缓存边界」那两条），Rust 帮不上忙，改架构才有用。
3. **扫描器的产出天然是 Python 字典**，正好落在原生加速「输」的那一侧（同上面 JSON 的根因）。理论上可以写一个「给定文件路径直接返回一小段元数据、全程不建 Python 对象树」的提取器绕开这点，但要把 5 种助手的历史格式怪癖（子代理线程识别、`stop_reason` 与正文无关、`origin.kind` 区分真人与系统事件、`payload` 值可能是 JSON null、标题生成噪音会话过滤）在 Rust 里重写一遍并长期与 Python 参考实现做差分维护，而它**只对「第一次启动」有效**——派生缓存已经把这变成一次性成本。

重新评估的条件：首次扫描成为真实用户投诉点，或派生缓存机制被取消。

顺带记一条被否决的微优化：`scan/codex.py` 的 `_read_session_head` 把文件头最多 30 行逐行完整解析（真实行平均 7.4 KB），加子串预筛可省 28%，**但不能做**——该函数的调用方 `_build_session_info` 会对**每一条**头部条目取 `_entry_time()` 来推 `event_time`，预筛掉的行会改变会话时间进而改变列表排序。抽查 300 个真实文件，300 个都会被改变。

- 原生扩展使用稳定的 Python 3.10 ABI，一个平台产物覆盖该平台的 Python 3.10 及以上版本。
- 正式发布必须构建 macOS 通用轮子，以及 glibc/musl 的 Linux x86_64、aarch64 轮子，并附源代码包和校验和。
- 一键安装脚本按操作系统、CPU 架构和 Linux libc 直接选择预编译轮子；找不到匹配产物时才退回源码安装。项目支持范围仍是 macOS 与 Linux，不声明 Windows 支持（阻碍面与档位见 [WINDOWS_COMPATIBILITY_DESIGN.md](design/WINDOWS_COMPATIBILITY_DESIGN.md)）。
- Homebrew 源码配方构建时必须声明 Maturin 与 Rust 构建依赖，并在隔离环境中生成轮子，不能继续调用旧的纯 Python 安装入口。
- `CORRAL_NATIVE=0` 可强制走 Python 回退，用于差分测试和故障隔离；正常用户不需要设置。

## 测量与验收

仓库的 `scripts/benchmark.py` 只输出计时与数量，不输出真实会话正文。性能改动至少记录：

```bash
python3 scripts/benchmark.py
CORRAL_NATIVE=0 python3 scripts/benchmark.py
python3 -c "import time; from corral.runtime import default_registry; r

<!-- 该文档整理/压缩于 2026-09-29 -->
