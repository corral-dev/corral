# Changelog

Release notes are bilingual: English first, then complete Simplified Chinese.
Development adds entries under `## Unreleased`; at release time they become the
next version's section. A `Release: minor` or `Release: major` line requests a
larger version step; `Release: hold <reason>` keeps the changes unreleased until
the named acceptance is done.

## 1.0.0

### English

- Version 1.0.0 opens the CLI's stable 1.x line. Behavior is unchanged from 0.24.369: this release carries only the version bump.

### 简体中文

- 1.0.0 开启 CLI 的 1.x 稳定版本线。行为与 0.24.369 完全一致，本次发布只包含版本号跃升。

## 0.24.369

### English

- TUI session list & session API (via SessKit 0.3.8): sessions now expose cross-runtime agent loop counts (`loop_count: int | None`), rendered on Row 2 of each TUI session card as `↻ <count>` (`1.2k` at 1,000+) using three neutral foreground contrast tiers (`1-3`, `4-30`, `>30`) and hidden when zero or unavailable.
- Remote host & lifecycle: added `media.file` RPC and Antigravity `M-m` steering, reused the session ID as the tmux ident on native resume, stopped automatic background relaunches after reboot or crash, skipped missing tmux sockets in `busycheck`, cached remote LAN hints and throttled unchanged status snapshot writes in `RemoteDaemon`, and isolated `_list_tmux_sessions` under `CORRAL_ISOLATE_MANAGED_HOSTS=1`.

### 简体中文

- 终端界面会话列表与会话接口（依赖 SessKit 0.3.8）：会话现已提供跨运行时统一的 agent loop 轮次（`loop_count: int | None`），在 TUI 会话卡片第二行左侧以 `↻ <数字>`（1,000 起简写为 `1.2k`）配合三档中性前景透明度（`1-3`、`4-30`、`>30`）展示，为 0 或无数据时隐藏。
- 远程主机与生命周期：新增 `media.file` RPC 与 Antigravity `M-m` 引导发送；原生恢复时复用会话 ID 作为 tmux 标识；取消重启或崩溃后的后台自动拉起，并在 `busycheck` 中跳过不存在的 tmux socket；`RemoteDaemon` 缓存局域网提示地址并节流未变化的状态快照写入；`CORRAL_ISOLATE_MANAGED_HOSTS=1` 下同步隔离 `_list_tmux_sessions`。

## 0.24.368

### English

- Silent idle-session reclaim now uses a 60-minute idle threshold (down from 120 minutes) with a 5-minute check interval, keeps up to the 8 newest parked sessions resident under normal memory conditions without a per-pass retirement cap, and under armed memory pressure retires every eligible session idle for 60 minutes or more without stepping down below 60 minutes.

### 简体中文

- 静默自动清理闲置托管会话改为 60 分钟闲置门槛（原 120 分钟）、每 5 分钟巡检一次；内存正常时最多保留最新 8 个闲置会话常驻并取消每轮清理个数上限；系统真缺内存时保留 60 分钟门槛不下探，直接清理全部超过 60 分钟闲置的符合条件会话。

## 0.24.367

### English

- The Mac sidebar's pinned section no longer goes blank: the host's first-page window now keeps every session the desktop can render as pinned — independent pins plus every member of a pinned split (including a split pinned through one member), so a pinned Pi session beyond the 80-row window is no longer dropped from the Mac list. The phone's pinned section still counts independent pins only.

### 简体中文

- Mac 侧栏置顶区不再空白：开发机的首屏窗口现在保留桌面上所有能画成置顶的会话——独立置顶键，以及被置顶分屏组的全部成员（含成员独立钉提升出的整组置顶），落在 80 行外的置顶 Pi 会话不再从 Mac 列表里消失。手机置顶区仍然只按独立置顶判定。

## 0.24.366

### English

- A hosted pane keeps one terminal stream per session: a provisional key and its canonical history now share the same raw stream, control callback and ordered fan-out, so a promoted or reconnected session no longer alternates between two widths (a real Antigravity pane flipped 77×61 and 85×61) or rebuilds its pane, and detaching one alias withdraws only that view.
- Remote terminals redraw immediately: an agent's 4–7 KB full-screen redraw no longer waits behind the 8 KiB coalescing window, taking publish p95 from 20.8 ms to 0.5 ms and raising renders per display frame to 0.81–0.86.
- More than eight warm terminal streams no longer drag the keepalive server down: streaming channels get their own 32-slot budget instead of evicting each other every second, reconnects back off from 2 s to 30 s, the launchd server keeps 16384/65536 file limits and a bounded log, and a lost tmux server restarts and re-queues its sessions by itself — no dialog, banner, badge or notification.
- Hosted-session lifecycles are now observable and self-healing: a seven-day journal records server start, exit and loss plus every hosted session that died, with a hosted inventory; a failing lifecycle probe counts as unknown instead of an error.
- Idle hosted sessions are retired on evidence and restored silently: a seven-signal state machine, a 30-minute absolute floor, 12/6 resident budgets on 16 GB, a durable retirement record, and a resume that Corral retries instead of handing the failure back.
- Session rows show the latest real reply instead of a superseded error: SessKit 0.3.6 stops an old Codex/Pi quota excerpt from owning the list preview. Antigravity sessions bounded to a hosted placeholder also recover their native history, because retained session IDs now augment discovery instead of replacing it — ending the persistent new-session title and the “no recorded history file path” handoff error.
- TUI remembers each session's Your prompts state: rebinding a pane and coming back restores it, the same session in another pane agrees, and Ctrl+G still clears every remembered choice.
- Peer messaging is bounded further: entries past their queue timeout no longer dispatch late, bypass paths are closed, and a double reply is suppressed.

### 简体中文

- 托管格每个会话只保留一条终端流：临时 key 与它对应的正式历史现在共用同一条原始流、同一个控制回调，事件按序分发到各个订阅通道，被提升或重连的会话不再在两个宽度间来回跳（实测 Antigravity 一格在 77×61 与 85×61 之间切换）或重建终端，摘掉一个别名也只撤掉那一个视图。
- 远端终端重绘即时下发：智能体 4–7 KB 的全屏重绘不再被 8 KiB 合并窗口挡住，发布延迟 p95 从 20.8 ms 降到 0.5 ms，每个显示帧能渲染 0.81–0.86 次。
- 超过 8 路暖流不再拖垮保活服务端：实时流通道改用自己的 32 个槽位预算，不再每秒互相驱逐；重连从 2 秒起指数退避、封顶 30 秒；launchd 服务端保持 16384/65536 文件句柄上限与有界日志；tmux 服务端丢失后自行重启并按清单重新排队会话，不弹对话框、横幅、角标或通知。
- 托管会话生命周期可观测且能自愈：七天事件日志记录服务端启动、退出、丢失以及每个死掉的托管会话，并维护托管清单；生命周期探针失败记为“未知”，不再当错误。
- 空闲托管会话按证据退休、静默恢复：七种状态的状态机、30 分钟硬底线、16 GB 下 12/6 常驻预算、必须落地退休记录，恢复失败由 Corral 重试，不甩给用户。
- 会话列表显示最新真实回复，不再被过期报错占据：SessKit 0.3.6 让被取代的 Codex/Pi 额度错误不再占用预览。被停在托管占位符上的 Antigravity 会话也能恢复原生历史：保留的会话 ID 改为补充而不是替代原生发现，新的会话标题一直不变、“The original session has no recorded history file path”接力报错随之消失。
- 终端界面按会话记住本会话提示词的开合：分屏换绑再换回能恢复，同一会话在另一格看到同样状态，Ctrl+G 仍然是全部清空。
- 助手互发消息进一步收紧：超过排队超时的消息不再延迟投递，绕过路径关闭，重复回执被抑制。

## 0.24.365

### English

- Added full Google Antigravity (`agy`) runtime support across the CLI, TUI, command shim, bundled skills, and mobile remote relay: session scanning, rich transcripts, live hosted panes, prompt navigation, `ask_question` status, and cross-assistant resume/handoff.
- Updated pinned dependencies (`sesskit 0.3.4` and `agent-session-control 0.1.2`) and Antigravity attention signals so background tasks, mid-turn tool errors, and native `ERROR_MESSAGE` stops emit accurate terminal-only completion notifications without false mid-turn or paused-session alerts.

### 简体中文

- 新增对 Google Antigravity（`agy`）运行时的完整支持，覆盖命令行、终端界面、命令拦截（shim）、内置技能与手机远程接力：包括会话扫描、结构化对话详情、内嵌托管终端、提示词跳转、`ask_question` 等待状态与跨助手接力恢复。
- 升级内置依赖（`sesskit 0.3.4` 与 `agent-session-control 0.1.2`）并完善 Antigravity 关注状态检测：后台任务、工具调用中间报错及原生 `ERROR_MESSAGE` 终止都能准确判定，仅在轮次真正结束时推送完成通知，消除中间误报。

## 0.24.364

### English

- Compact A2A envelopes preserve the correlation marker and historical attribution while removing the automatic-reply invitation. Replies now consume a parent grant once and stop at four hops (`chain_budget_exhausted`), including automatic report-back; use `--reply-to` or `--new-chain` for explicit control.
- Added chain/cost visibility: `run list` exposes parent/hop identity, `corral a2a status` shows active chains, and `corral a2a ledger` aggregates pair/hour queue time and token usage. The daemon can send a rate-limited encrypted Agent chatter alert when one pair becomes unusually active.

### 简体中文

- A2A 信封更精简，保留关联 marker 与历史消息归因，不再附带诱导自动回复的说明。每次回复只消费一次父消息授权，最多 4 跳（`chain_budget_exhausted`），自动回执也受同一约束；可用 `--reply-to` 或 `--new-chain` 显式控制。
- 新增链路与成本观测：`run list` 展示父消息和 hop，`corral a2a status` 展示活跃链，`corral a2a ledger` 按发送方/目标/小时汇总排队耗时与 token。单对助手消息异常密集时，开发机可通过现有加密推送通道发送带冷却的 Agent chatter 提醒。

## 0.24.363

### English

- Agent-to-agent messaging is bounded against loops and stale echoes: creation briefs stay owner input, explicit unattributed sends remain marked as peer messages, interrupted/unknown/empty outcomes no longer wake the sender, and queue expiry stays on the run. Each sender/target is limited to four live A2A runs, with one ordered queue owner per target and named occupancy on capacity errors.
- Added `corral a2a status`, `corral a2a pause` and `corral a2a resume` to inspect and stop peer traffic without failing accepted work or ordinary owner submissions. `corral send` now rejects a busy receiver by default; explicit queuing has a 120-second default budget.

### 简体中文

- 助手互发消息现在有明确边界，避免循环与陈旧回声：新建任务简报仍是机主输入，显式未署名发送也保留同伴消息标记；中断、未知、空答案和排队过期不再唤醒发送方。每个发送方/目标最多容纳 4 个未结束 A2A run，同一目标只有一个按序执行的队列 owner，容量错误会列出占用者。
- 新增 `corral a2a status`、`corral a2a pause`、`corral a2a resume`，可查看并暂停助手间流量，不会把已接受工作判失败，也不影响机主普通任务。`corral send` 默认在接收方忙时拒绝，显式排队默认最多等待 120 秒。

## 0.24.362

### English

- Agent-to-agent messaging: `corral peers` lists other running agents (same project first, with state and reachability), and `corral send <runtime:id> "msg"` delivers a one-step attributed message whose final answer is reported back to the sender; agent-sent messages are marked in show/export.
- TUI: Ctrl/Option-click a path in the right pane to reveal it in Finder (folders open, files selected); relative paths resolve against the hosted pane's directory.
- Focused terminal is more responsive: the focused pane renders at display cadence with faster input echo, and remote terminal output publishes immediately after idle and for large bursts.
- Pi waiting attention is reported correctly, and hosted sessions gain tmux extended-key (CSI-u) support on newer tmux.

### 简体中文

- 助手互发消息：`corral peers` 列出其他运行中的助手（同项目优先，带状态与可达性），`corral send <运行时:id> "消息"` 一步送达带署名消息，对方最终答复会自动回传给发送方；show/export 会标出助手发送的消息。
- TUI：右栏按住 Ctrl/Option 点路径可在 Finder 中定位（文件夹打开，文件选中）；相对路径按所在托管格目录解析。
- 聚焦终端更跟手：聚焦格按显示节奏渲染、输入回显更快，远端终端输出在空闲后首包与大包直接下发不再固定等待。
- Pi 等待回答的关注提示已修正；新版 tmux 下托管会话支持扩展按键（CSI-u）。

## 0.24.361

### English

- Adding a split pane is instant: the new pane appears immediately with its title bar and a `Starting…` hint, then tmux hosting, the control channel and the first frame arrive in the background. The pane converts in place when the session is ready and disappears if hosting fails, instead of nothing happening until the agent was ready.
- Pane chrome is now a single bottom strip: the session title moved below the terminal, and the hint row only appears when there is something to say (restart, input not receiving keystrokes), instead of always showing an empty bar.
- New Session can open any folder through More → Open Folder…

### 简体中文

- 加分屏不再等托管：新格子立刻出现，带标题栏与「正在启动…」提示，tmux 建窗、控制通道与首帧随后在后台完成；会话就绪时就地转正，启动失败则自动收起。以前要等助手起好才有任何反应。
- 分屏底栏合并成一条：会话标题移到终端下方，提示行只在有内容时出现（重启、输入不生效），不再常驻一条空白行。
- 新建会话可从「更多 → 打开文件夹…」选任意目录。

## 0.24.360

### English

- Session titles now start with a task emoji (🔧 fix, ✨ feature, 📝 docs, 🚀 release…), chosen for the task domain so sessions are easier to scan and remember. Existing titles stay unchanged.
- New `corral flex` command launches the shareable agentflex activity card (animated, with PNG export), replacing the retired Python `corral wrap`.
- Remote image-only turns now submit correctly, and freshly created OpenCode sessions report input readiness instead of timing out.

### 简体中文

- 会话标题现在以任务 emoji 开头（🔧修复、✨功能、📝文档、🚀发布……），按任务领域选择，扫列表时更好认、更好记；已有标题保持不变。
- 新增 `corral flex` 命令，打开可分享的 agentflex 动态卡片（带动画，支持 PNG 导出），替代已退役的 Python 版 `corral wrap`。
- 远程纯图片消息现在可正常发出，新建的 OpenCode 会话能正确上报输入就绪，不再超时。

## 0.24.359

### English

- Deleting an OpenCode session now removes both copies created by its storage migration, so the session no longer resurfaces on the next scan.
- Fixed Homebrew installs for this release line: the bottled formula now vendors the agent-session-control dependency, so `brew install` and `brew upgrade corral` verify cleanly again.

### 简体中文

- 删除 OpenCode 会话时会同时清除其存储迁移产生的两份拷贝，会话不会在下次扫描时重新冒出来。
- 修复本版本线的 Homebrew 安装：预编译配方现在内置 agent-session-control 依赖，`brew install` 与 `brew upgrade corral` 可正常通过校验。

## 0.24.358

### English

- Remote typing stays responsive while the session list loads: list and watch snapshots no longer block keystrokes on the encrypted channel, so Mac and iPhone terminal input is not stuck behind a slow list refresh.
- The TUI filter now cycles Focus, Active and All, matching the Apple clients.
- Retry on an ended or stopped session resumes it first, so the terminal shows a fresh snapshot instead of a blank page.
- OpenCode upgrades no longer break session forking: version-aware resume, plus more robust image pasting.
- Session titles prefer the agent-native title over the empty-history placeholder.

### 简体中文

- 远端打字不再被会话列表加载卡住：列表与订阅快照不再占用加密通道的串行处理，按键输入不会排在慢列表后面，Mac 与 iPhone 终端输入更跟手。
- TUI 筛选现在按"关注 → 活跃 → 全部"循环，与苹果客户端一致。
- 对已结束或已停止的会话点重试会先恢复会话，终端显示最新快照而不是空白页。
- OpenCode 升级不再破坏会话分叉：按版本自适应恢复，图片粘贴更稳。
- 会话标题优先采用助手原生标题，不再被空历史占位符盖掉。

## 0.24.357

### English

- Corrected Quick Start space usage: show more recent combinations across two rows as terminal width allows, instead of stretching buttons. Removed the five-combination cap; existing session history supplies additional choices, and dismissed choices stay hidden.

### 简体中文

- 纠正快速开始的空间利用：根据终端宽度在两行内显示更多最近组合，而不是拉长按钮。取消固定五个的限制，从已有会话历史补充选项，移除的组合不会重新冒出。

## 0.24.356

### English

- Fixed Your prompts → latest in native terminal mode: return the agent-owned viewport to live output without losing the draft or interrupting the running task. Guard stale navigation replies and require write permission for the host action.
- Quick Start choices now fill each wrapped row, including the last row, while adapting to narrow and wide terminals.

### 简体中文

- 修复原生终端模式下「提问 → 最新」：将助手自己的滚动位置带回实时输出，保留草稿且不打断正在执行的任务；过期导航响应不再覆盖新操作，宿主动作也会检查写入权限。
- 快速开始选项会填满每一行，包括换行后的最后一行，同时适配窄屏与宽屏终端。

## 0.24.355

### English

- The Your prompts card's last row now returns the terminal pane's agent-owned viewport to live output through a guarded `terminal.latest` host action, so fullscreen agents that are scrolled back actually show their newest output again; the draft, running work and Chat/Terminal mode are preserved. Older hosts keep the previous local-buffer fallback, and Chat mode keeps its existing latest behaviour.
- The quick-filter switch is now Focus (关注): it lists sessions that need you or are worth keeping in view — waiting, working or recently active sessions, unread results from the last 7 days, and sessions touched in the last 30 minutes.
- Full-text search now finds matches in history beyond the recently loaded window.
- The Your prompts card's last row returns the conversation to its newest message, including live sessions.
- Idle CPU and memory are lower: the host service skips redundant state work, list loads reuse the shared scan index, and inactive transcripts, readers and outbound frames are bounded.
- Handoffs write a verbatim package under the Corral cache with every user message, recent conversation and tool activity, the in-flight call and the source Git state, without calling a model.
- Updated the pinned SessKit reader, terminal classification and warm-scan caching.

### 简体中文

- 提问卡片的最后一行现在通过宿主侧受控的 `terminal.latest` 动作，把终端分屏里助手自己的滚动位置带回最新输出；此前只清 Corral 外层缓冲，全屏助手仍停在旧消息。草稿、进行中的工作和聊天/终端模式都保留；旧宿主继续使用本地缓冲回退，聊天模式维持原有回到底部行为。
- 快捷筛选开关改为「关注」：列出需要你处理或值得留在视野里的会话——等待、运行中或刚有活动的会话、最近 7 天的未读结果，以及 30 分钟内碰过的会话。
- 会话全文搜索现在能命中最近加载范围之外的历史。
- 提问卡片最后一行可让会话回到最新消息，运行中的会话也可用。
- 降低空闲 CPU 与内存：宿主服务跳过重复状态计算，列表加载复用共享扫描索引，并限制非活跃对话、读取器和外发帧的保留量。
- 交接会在 Corral 缓存下写出逐字交接包：全部用户消息、最近的对话与工具活动、进行中的调用和源仓库 Git 状态，不调用模型。
- 更新 SessKit 读取器、终端识别与热扫描缓存。
