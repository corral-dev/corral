# Changelog

Release notes are bilingual: English first, then complete Simplified Chinese.
Development adds entries under `## Unreleased`; at release time they become the
next version's section. A `Release: minor` or `Release: major` line requests a
larger version step; `Release: hold <reason>` keeps the changes unreleased until
the named acceptance is done.

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
