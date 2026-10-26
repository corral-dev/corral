# Changelog

Release notes are bilingual: English first, then complete Simplified Chinese.
Development adds entries under `## 0.24.355`; at release time they become the
next version's section. A `Release: minor` or `Release: major` line requests a
larger version step; `Release: hold <reason>` keeps the changes unreleased until
the named acceptance is done.

## Unreleased

Release: hold installed Mac terminal-latest acceptance

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
