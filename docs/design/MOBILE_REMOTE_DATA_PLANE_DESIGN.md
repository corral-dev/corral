# 手机远程会话数据面重设计

> 状态：2026-08-29 第一轮已落地（消息窗口、加密前 zlib 压缩、Codex 当前助手格式、详情返回栈）。第二轮已落地开发机规范化消息缓存：打开会话、翻页、提问列表和实时订阅共用同一份解析结果，Cursor 用户正文与本地扫描器同一套过滤。第三轮针对真机「列表/详情转圈超时」：经公网中继按手机同款请求实测，整表订阅 535 条约 13.5s、约 87MB 历史的详情在 20s 内无回包且中继心跳被掐断；已改为列表首包截断、解析时让出锁、心跳超时放宽、回包失败回错误。第四轮针对「进列表仍要转圈等网络」：手机先画出该开发机上次成功列表，刷新带版本号，未变则不重传窗口；开发机先截当前页再组包，避免为几百条闲置会话做完整摘要。第五轮已落地按序号断线续传：`session.watch` 可带 `after_seq`/`generation`，回包 `resume=replay|tail`；开发机对每条被看的会话保留有界增量缓冲（最近 200 条），代次一致且缺口仍在则只补更新，手机必须 merge、空 replay 不得清空。第六轮已落地 JSONL/Cursor 尾部偏移读取：缓存未命中时第一次打开只从文件末尾解析当前消息窗口，向前翻页再补读更早一块，`has_more` 在左侧还有未读字节时为真；文件变长仍从上次偏移增量读。**控制/数据通道拆分已落地（2026-08-30）**：hello 声明 `capabilities.planes`，新手机带 `want_data_plane` 后另开第二条 WebSocket 用一次性 `data_bind` 附着；旧手机不带该字段，仍单连接。数据面队列满只丢帧/拒历史页，不得踢控制面。验收必须以 `cli/scripts/phone_remote_acceptance.py`（或真机）走中继上的 `sessions.watch`，并**每个助手各打开一条有正文的详情**；`sessions.list --limit 5` 和本机 unittest 不算完成。只修 Codex 或只验本机、不重启用户正在连的那台开发机常驻进程，都会把「源码已改」误当成手机已修好。CLI **v0.24.150** 与 iOS **1.0.11**（构建号 12）已发布；本机与 suzhou 常驻远程已于 2026-08-30 17:05 左右换成该版；iPhone Max 已覆盖安装。2026-08-30 经当前公网中继跑 `phone_remote_acceptance.py`：整表首包 0.52s（80 条）、Cursor/Codex/Pi/OpenCode/Claude 详情均在 2s 内有正文、双连接竞速与空闲心跳通过；首包窗口里没有 Kimi 样本。真机换网/超大历史体感仍须用户点一次。

## 1. 适用范围

本文只管 iPhone 与开发机之间的远程会话列表、历史消息、实时消息、终端画面和重连数据面。配对凭据、安全边界、推送文案和桌面 TUI 仍以现有领域文档为准。

遇到以下现象时必须先读本文：手机会话列表或历史消息加载极慢、**每次进列表都转圈空等**、进详情后返回没反应、打开大历史时像卡死、**第一次打开仍把整份 JSONL 解析一遍**、换网后重复下载整段历史、实时画面被历史加载拖住、Cursor 把系统注入内容当成用户消息、Codex 缺少助手消息、**手机发得出话但对话不更新 / 再发提示会话已不在列表里**、**新开会话发了在吗 / 刚开的会话只有自己那句 / 终端里有字聊天没有**，或准备调整直连/中继/协议/缓存架构。

## 2. 调查裁定

### 2.0 First-principles network proposal (2026-09-10)

Status: **implementation authorized 2026-09-10** after user asked to complete the build. First-principles reasoning and section 7 defaults are the approved product contract. Older measurements below remain historical, not current production measurements.

Authoritative specification: [section 7](#7-detailed-network-ux-design-proposal). Shipped behavior still follows earlier rounds until each delivery slice lands; negotiate new capabilities instead of silently reinterpreting v2 peers.

#### Invariants and ownership

- The development machine owns execution and canonical session history. The phone owns drafts, navigation, a bounded local view, and its last applied positions. A transport connection is replaceable; it is not the identity of a session or an operation.
- Reading previously synchronized content should require zero network round trips. Fresh remote state cannot be guaranteed while disconnected; retain content and expose freshness instead of presenting cached state as live.
- Separate snapshots, ordered changes, and commands. Snapshots can replace obsolete snapshots; ordered changes need gap recovery; commands require an explicit outcome and cannot be silently dropped or replayed as ordinary state.
- A connection to the relay proves only relay reachability. Readiness requires an authenticated response from the development machine and restoration of the relevant subscriptions.
- End-to-end encryption remains in place on every path. The relay forwards opaque payloads; cloud persistence would be a separate product decision with retention, revocation, expiry, and metadata implications.

#### Source findings and their limits

Source inspection of `remote/transport/relay.py:RelayClient`, `relay/internal/server/server.go:handleHostV2`, `relay/internal/hub/hub.go:FromHost` and `relay/internal/server/sink.go:Send` establishes that the phone's control/data sockets converge into one host-relay WebSocket. `FromHost` synchronously waits for a destination write before the host reader continues. Thus phone-side separation does not establish physical isolation across the relay route, and a slow destination can delay other channels. These are structural risks, not measured claims about today's dominant latency.

`RegisterHost` replaces the previous host registration. Adding another ordinary host connection would evict the first; a future split requires authenticated attachment of additional lanes to one host registration, bounded resources, and legacy capability negotiation.

#### Recommended design

1. **Measure the complete user operation.** Separate local feedback, path establishment/authentication, host queueing, parsing, transfer, phone decoding, and visible update. Use correlated operation identifiers and monotonic local durations; do not subtract unsynchronized device clocks or log conversation bodies. Compare median and p95 under idle and concurrent-history load.
2. **Make the local view independent of connection lifetime.** Reuse existing list snapshots and sequence recovery; verify a bounded recent conversation cache as well. Preserve drafts and scroll position. Resume only visible subscriptions first; fetch older content on demand. Do not reopen the currently disabled terminal feature.
3. **Give commands stable identities and explicit outcomes.** Retain an operation identity across transport retries, with durable host receipts and duplicate detection. Distinguish accepted, delivered to the assistant, and outcome unknown. A crash between external input injection and receipt recording prevents a blanket exactly-once guarantee; do not automatically replay an ambiguous input. Offline text remains a draft by default; stop/delete/key operations are not an automatic offline queue.
4. **Protect interaction across both network legs.** Keep small control/live updates separate from history and attachments through phone, relay, and host. Bound live batches and attachment chunks by bytes as well as counts. Use bounded per-destination queues with fair scheduling; a slow phone must not block the host reader. History can pause/cancel; ordered changes recover through cursors when a consumer falls behind. The relay must not decrypt content to choose priorities; use authenticated lane membership and per-lane quotas.
5. **Replace paths without replacing application state.** Retain concurrent LAN/relay establishment. Validate identity and restore the new path before retiring a still-working old path; if the old path is already dead, resume through a new one. Deduplicate updates during overlap and send each command on one active path. React promptly to foreground/network changes; use jittered backoff during sustained failure. Require sustained quality improvement before switching a healthy path to avoid oscillation.
6. **Choose relay placement from both endpoints.** Measure complete phone-relay-host request latency, loss, jitter, and success rate on actual Wi-Fi/cellular networks before selecting a primary and optional warm standby. Both endpoints must be registered/reachable through the selected relay; independent nearest-node selection is insufficient. Preserve pairing identity across failover and register trusted host keys consistently. Additional nodes improve availability but cannot execute work on a sleeping development machine.
7. **Treat background notifications as hints.** Restore from a saved cursor on foreground even if notifications were missed. Apple does not guarantee background notification delivery. Heartbeats cannot make an iOS app run indefinitely in the background or wake a sleeping host by themselves.

#### Decision order and acceptance

First measure and close local-view/command-outcome/recovery gaps; next eliminate cross-lane and cross-device blocking; then add a second relay only if route measurements or availability requirements justify it. QUIC or public-network hole punching are later transport experiments, not prerequisites. QUIC independent streams can reduce cross-stream loss blocking but still share congestion and require deployment/fallback work.

Proposed targets, not verified performance: p95 cached view <= 200 ms; local send feedback <= 100 ms; healthy-network host receipt <= 1 s; fresh tail <= 2 s; usable new network to resumed visible subscription <= 3 s. Measure assistant-output latency from data becoming readable on the host, separately from model generation time. During history/attachment load, target <= 100 ms additional control queueing. Validate on iPhone Max across LAN, relay-only, Wi-Fi/cellular switching, background return, host/relay restart, acknowledgement loss, and a slow second client. All six assistant types require content/recovery samples. Check no duplicate execution in tested fault windows and label ambiguous outcomes honestly.

#### External evidence

- [Tailscale connection types](https://tailscale.com/docs/reference/connection-types): reachability followed by path improvement. Reference source inspected in a shallow checkout: `wgengine/magicsock/endpoint.go`, `addrForSendLocked`, retains a trusted path and uses relay fallback/probing when trust expires. Borrow path validation and stability; do not copy packet duplication into side-effecting Corral commands or require users to install a VPN.
- [Apple background updates](https://developer.apple.com/documentation/usernotifications/pushing-background-updates-to-your-app): background delivery is not guaranteed.
- [QUIC RFC 9000](https://www.rfc-editor.org/rfc/rfc9000.html): independent streams and migration are transport capabilities, not application command receipts or state recovery.

### 2.1 速度问题不是单一的“中继延迟”

本机当前有 527 个会话，原始历史文件合计约 34.7 GB。冷启动扫描约 2.6 秒；最大的 Codex 历史文件约 91.6 MB，当前富消息解析约 0.9 秒。扫描完成后生成 100 条列表摘要约 3.1 毫秒，说明列表摘要本身不是主要瓶颈。

经新建中继连接的只读探针测得：列会话约 1.99 秒；列会话后再取一条会话的 5 条消息约 8.62 秒。这个数字包含新连接、握手、扫描/解析、加密解密和传输，不是固定网络 RTT；它说明“打开会话”的服务端读历史和大帧路径已经足以主导体感。

分页窗口只限制返回给手机的条数。缓存命中后打开、向上翻页、提问列表和实时订阅共用同一份规范化结果，不得再为了推进读取游标或生成首包把文件读第二遍。文件变长时只从上次偏移继续读。缓存未命中（文件被截断/替换、解析器版本变化）时，第一次打开只从 JSONL 末尾向前取完整行、解析足够填满当前消息窗口的记录（工具配对不完整允许再向前一块）；Cursor 按 rowid 取尾部 N 条。左侧还有未读字节时 `has_more` 必须为真。只有用户翻到文件头才允许读到 offset 0。截断/换文件仍整份重建并抬 generation。解析失败当作未命中或本轮无新消息，不得炸通道。

本轮对一份 87.4 MB 的 Codex 历史实测：首次订阅约 2.62 秒（完整解析一次，共 75 条规范化消息）；紧接着再取一页和提问列表均为 0.000 秒；新进程从磁盘缓存恢复约 0.004 秒。那是尾部读取落地前的缓存基线：该文件规范化后不足一页，尾部窗口仍可能扩到文件头。合成大 JSONL 的单测覆盖「几千行只解析尾部一页」。这不证明真机从点开到首屏已经达标。

### 2.2 当前运行中的服务可能不是当前工作树

本轮核对到的远程服务进程于 18:08:15 启动，而远程协议、会话和通道文件在 18:28–18:30 仍有修改时间。`corral --version` 显示源码入口，只能证明命令行入口指向源码，不能证明已经运行的常驻服务已经加载了这些修改。

以后凡是宣称“手机端已验证”，必须同时给出：常驻服务启动时间、实际加载版本/产物、手机客户端安装版本，以及一次真实的列表→打开历史→实时更新→断线恢复链路。只看编译结果、单元测试或 `corral --version` 不算完成。

### 2.3 内容错误是运行时解析与远程解析不一致

- Cursor 用户正文必须与本地扫描器共用同一套提取：只保留 `<user_query>` 里的真实问题，丢弃 `<user_info>`、规则和大段注入上下文。禁止在 iOS 里用固定字符串黑名单补救。冷缓存或未重启的常驻服务仍可能发出旧解析结果，真机验收前先核对服务是否已加载当前代码。
- Codex 的当前工作树已补上现代 `response_item` 中 `role=assistant` 的解析；当前本机对最大的 Codex 文件读到 64 条助手消息，定向远程测试 60 条通过。但这还没有证明真机当前安装包、当前常驻服务和当前手机界面已经共同使用这份修复。
- Pi 的桌面预览走扫描器，手机详情走另一张远程解析表。Pi 曾完全未登记，所以真机打开 Pi 会话会得到空聊天，而电脑上同一条会话有完整问答。禁止把它当成「这个 Pi 本来就没消息」或在手机端为 Pi 写特例过滤。补登记后必须抬高规范化缓存版本并重启常驻服务。
- 手机端对单条富消息采用尽量解码，异常项会被丢弃。它可以防止一条坏工具调用拖垮整页，但也会把服务端协议/语义不一致隐藏成“助手消息不见了”。服务端必须先输出稳定的规范化消息，客户端不能承担运行时格式识别。

### 2.4 现有网络结构把不该互相影响的工作串在一起

Historical pre-split findings follow. Later changes are recorded in section 4; current source-level relay limitations and the proposed next design are in section 2.0. Do not treat this historical description as the current implementation baseline.

当前一条设备通道同时承载请求响应、历史首包、实时消息、终端画面和输入。开发机端每条通道只有一个顺序工作的消息处理线程；一条慢的历史读取会占住该通道。通道队列达到上限时会直接关闭连接，而不是按消息重要性做背压。

手机端的收包、解密、JSON 解码和订阅分发仍在同一个主执行隔离域内。即使开发机把首包限制到较小窗口，大量事件或大工具载荷仍可能把“收到数据”与“画到屏上”绑在一起。

断线后客户端会重新建立 WebSocket、重新登记订阅并重新拉首包；协议没有用“历史版本 + 消息序号 + 确认位置”恢复事件流。因此换网或短暂掉线会把已经展示过的内容再次当成冷启动数据处理。

直连与中继现在采用各候选地址同时完成完整握手、先成功者胜出。它能缩短首次连接，但没有记录当前实际路径、没有把中继当作协商/升级旁路，也没有周期性验证能否从中继升级到直连。于是“能连上”与“走的是哪条路、为什么慢”无法从产品数据中区分。

## 3. 外部方案依据

本轮不以记忆或搜索摘要定方案，而是检索官方资料并拉取源码阅读：

- [Tailscale connection types](https://tailscale.com/docs/reference/connection-types)：直连、中继和同网设备中继都保持端到端加密，但性能不同；典型流程先通过中继建立可达性，再尝试直连，并持续重新检查；状态必须能显示当前实际连接类型。
- [Apple NWConnection](https://developer.apple.com/documentation/network/nwconnection) 与 [NWPathMonitor](https://developer.apple.com/documentation/network/nwpathmonitor)：连接状态、路径变化、可用性和连接建立/传输报告应进入可观测数据，不能只凭“网络变了”就猜测当前路径。
- [Shellular packages](https://github.com/shellular-org/packages) 与 [Shellular server](https://github.com/shellular-org/server)：实际源码中的 `TranscriptStore` 用持久缓存、消息位置、总数和 generation 支撑尾部读取与向前分页，重新打开会话不必等待完整历史回放；中继只转发端到端加密数据。
- [OpenCAN](https://github.com/botiverse/opencan)：实际实现把稳定会话身份、运行时身份、所有权和事件序号分开；守护进程持有生命周期和有限事件缓冲，重连用序号回放，而不是把一次连接当作会话本身。

这些项目的共同点不是某个特定传输协议，而是：状态在开发机持久化、历史按窗口读取、实时事件有序号、重连可恢复、当前连接路径可观测。不能只照搬它们的 UI 或把换成 QUIC 当成万能修复。

## 4. 目标架构

### 4.1 连接层：可达性、路径和数据传输分开

1. 开发机常驻连接只负责在线登记、候选地址、能力协商和最小心跳；中继继续只转发不可读的端到端密文。
2. 手机先建立可用路径，再记录 `direct` 或 `relay`、建立耗时、最近一次成功时间和失败原因。默认保留中继可用性；直连成功后周期性探测更优路径并平滑升级，直连失效则回落中继。
3. 首阶段继续使用现有 WebSocket/中继，先解决协议和调度问题；不把更换 QUIC 作为前置条件。若后续需要 QUIC，只能在已有的序号、确认、恢复和可观测契约上替换传输实现。
4. 历史/大画面不能与控制请求共用同一条有序字节流。首阶段使用两个物理数据连接：控制连接承载输入、短 RPC、心跳、状态和实时事件；数据连接承载历史页和终端画面。仅把两类消息标不同优先级但仍放在一条 TCP/WebSocket 上，不能真正消除大帧阻塞。**已落地**：`hello` 的 `capabilities` 增加 `"planes": ["control", "data"]`；客户端带 `want_data_plane: true` 时控制面响应带一次性 `data_bind`（绑定设备公钥，TTL ≤ 120 秒）。手机对同一主机再抢答一条 WebSocket，独立加密握手后 `hello`：`{"plane":"data","bind":"<token>","name":...}`。校验失败只关数据通道。旧客户端不带 `want_data_plane`，不发 token，仍单连接。数据面 `HostChannel` 队列满时丢弃过时终端帧或拒绝新的历史页，禁止因此 `close()` 控制面。

### 4.2 会话历史：开发机侧规范化缓存 + 位置分页

1. 目标：SessKit 的各运行时适配器把原生历史转换为统一的 `user` / `assistant` / `tool` 活动；Cursor 上下文过滤、Codex 现代消息识别等格式规则只在 SessKit 维护。列表预览、手机详情和导出消费同一结果。
2. 目标：SessKit 对 JSONL 使用文件偏移和文件签名增量读取，对 Cursor 使用数据库行位置。缓存未命中时首次打开只读尾部窗口，向前翻页从窗口左缘补读；文件轮换、截断或格式版本变化才重建必要范围。解析结果写入可失效的本机持久缓存，原始历史仍是权威来源。
3. 远程接口返回尾部窗口及 `from`、`to`、`total`、`generation`、`has_more`。向上翻页携带 `before`/`to` 位置；如果 generation 变化，客户端丢弃旧页游标并重新取当前尾部。服务端不得“先全量 parse 再 slice”来伪装分页。
4. 首次打开只取最近窗口；实时订阅首包与历史页共用同一份规范化缓存，不得为了“推进实时读取器”再完整读取一次。

### 4.2.1 Native history parsing and incremental cursor ownership

目标边界：SessKit owns native-format decoding, session metadata, and the incremental history cursor for all six runtimes. Corral consumes normalized conversation/activity records and owns hosting state, current process liveness, attention, phone message/tool-card projection, and the existing remote wire format. Corral must not keep a parallel parser or raw-file cursor for any runtime. The current Corral readers remain migration baseline until each replacement passes parity; do not switch a consumer to a full-history API where it currently uses bounded tail/page or incremental reads.

The authoritative model, compatibility, and verification contract is [SessKit's cross-runtime model evolution contract](../../../../SessKit/docs/CONTRACT.md#cross-runtime-model-evolution-contract). Keep history outcome separate from live/attention state; preserve structured native errors and tool result evidence; and keep the current v1 transcript and Corral/iOS wire behavior as compatibility projections during migration. Corral-only fields such as `keepalive_name`, `provisional`, and `attention_*` belong in Corral's typed session extension, not SessKit's shared session model.

Status (2026-09-30, SessKit 0.2.0): SessKit now provides typed activity for Claude, Codex, OpenCode, Cursor, and Pi (turns, error taxonomy, paired tool invocations, interaction requests), runtime adapters with capability flags (`get_adapter(...).capabilities`), and an incremental reader (`open_reader`) implemented for Pi only — on the largest local Pi history (14.4 MB) a no-change poll takes about 0.05 ms and an append poll parses only new lines, versus about 0.37 s for the current Corral `RichReader` poll. The Corral phone path has not switched yet: `remote/richmsg.py` still decodes Claude, Codex, Cursor, and Pi natively. Next consumer step is stage (2) below for Pi on top of `open_reader`; Claude/Codex/Cursor wait for their SessKit readers.

Migrate in bounded stages: (1) integrate SessKit's existing typed `SessionOutcome`, `AgentError`, and `ToolResultOutcome` into parser outputs without changing current schemas; design additive load-state and incremental-cursor APIs separately, since load-state is not implemented yet; (2) migrate Pi as the pilot, projecting SessKit's normalized events into the current phone `RichMessage`/tool-card shape; (3) migrate the remaining five runtime decoders and their incremental cursors into SessKit, one runtime at a time; (4) move Corral consumers to the compatibility projections and consider any wire/schema version only in a separate compatibility review. The Pi pilot must retain native message grouping so assistant text, tool calls, question options, and tool results reconstruct the same phone cards.

Each runtime migration must prove fixture and real-history semantic parity, including success, pending, abnormal/unknown endings, tool success/error, missing correlation evidence, valid empty history, and unavailable history. Preserve tail/page bounds and measure cold load plus warm incremental-tail latency and memory for each runtime before expanding past the Pi pilot. A warm poll must not reread/reparse the full history; cache invalidation and native tree reconstruction are SessKit-owned and must be measured. Record per-runtime baselines and report actual measurements; do not accept a latency or memory regression against that runtime's baseline. Full live-runtime and compatibility checks follow the linked SessKit contract.

#### Pi-to-phone compatibility boundary

**Current Corral behavior (verified in source):** `remote/richmsg.py` still decodes Pi JSONL itself through `_pi_build_messages` and `_parse_pi`. It selects the parent-linked active path, groups assistant text and tool calls into one `RichMessage`, attaches later results to the originating call, and re-emits the host message with its prior sequence when tool status changes. Empty assistant bodies with `errorMessage` become visible assistant text. Question cards are a Corral projection based on tool-name classification and recognized argument keys; any Pi question-shaped tool input used in fixtures is an extension fixture, not evidence that Pi's official native format defines questions or approvals.

The compatibility surface is the existing phone wire shape: message `seq`/`role`/`text`/`ts` and grouped tools (`id`, `name`, `kind`, `summary`, optional detail/output, question status/options/groups). iOS derives pending questions from running question tools, keeps multi-question groups separate, assigns `call_id:index` to grouped prompts, and submits `request_id` plus answers through `input.question`. A recent local SessKit history inventory (counts only; no conversation text retained) found structured question activity in Cursor (5 of 200 sessions, with `questions`/`title`/`id`/`prompt`/`options` fields), Codex (18 `request_user_input_async` calls), OpenCode (23 question calls), and Claude (7 `AskUserQuestion` calls); no Kimi history was present, and Pi structured user questions were not confirmed. Current phone extraction maps tool names through a shared finite classifier and only projects selected argument keys; `request_user_input_async` is currently absent from that map. Thus a migration cannot depend on tool-name classification alone: SessKit must surface normalized question/request identity, grouped prompt text and options, and supported selection/custom-answer semantics while preserving the native evidence. Treat these counts as a local sample, not runtime-wide prevalence. Pi `ask_parent` is an agent-to-parent coordination tool and must not be projected as a user question. The current parser exposes no separate approval-card kind; do not infer that permission approvals are already represented by question cards. Keep phone projection and wire shaping in Corral while moving native interpretation and cursor ownership to SessKit.

**Pi pilot acceptance (target, not yet established by this baseline):** use synthetic native-history fixtures to compare old and SessKit-backed `RichReader` wire output exactly for message/tool grouping, IDs, order/sequence, timestamps, summaries/details, pending and completed results, explicit and text-inferred errors, question extension fields, and empty-body agent errors. Include an active-leaf switch and verify that abandoned-branch content is not projected. Keep fixtures free of private history; label question-shaped records as Corral extensions. Separately measure cold history load and repeated hot polls on a real Pi history using output that contains only history byte size and elapsed time. A hot poll must process appended activity plus required branch changes without a full-history scan; branch reconstruction is allowed only when native tree topology changes. Record parse/read work and peak memory for the baseline and candidate, and require semantic equality with no p95 latency or memory regression before replacing the current reader. No real-history timing or old/new parity result is claimed here yet.

### 4.3 实时流：有序、可确认、可恢复

1. 每条会话事件带单调递增序号和历史 generation；事件批次同时受条数和字节上限约束。
2. 手机保存每个会话最后连续应用的序号，并周期性确认。重连时携带 `generation` 与 `after_seq`；开发机仍有缓冲就只回放缺口，缺口不存在或 generation 不同才返回一个新的尾部窗口。
   - 线上契约（已落地）：`session.watch` 的 `p` 可带 `after_seq`、`generation`（旧客户端不传 = 今天的尾部行为）。回包增加 `resume`：`"replay"` 只含 `seq > after_seq` 的连续更新，手机必须 merge；`"tail"` 为当前尾部窗口。generation 不同、缺口已滚出有界缓冲（每条被看会话最近 200 条增量）、或 `after_seq` 对不上时走 tail。空 replay 表示已追上，不是清空。实时事件仍走通道 `session:{key}`，继续带消息 seq。
   - 终端画面仍是「只保留最新帧」，不要给 screen 套同一套无损序号回放。
3. 历史消息必须无损、有序；终端画面属于“最新状态”，允许丢弃过时帧，只保留最新帧。两类数据不能共用同一个无限等待策略。
4. 会话编号在新开后可能从临时卡片换成正式编号。正在打开的详情必须继续用手机手里的旧编号发输入、收对话事件；开发机把旧编号解析到正式会话，并把历史读取切过去，事件仍推到原来的通道。停止、删除、恢复、置顶等写操作也必须改正式编号，不能写到已经退役的临时卡片。不能要求手机先知道新编号，也不能因此报「已经不在列表里」。**转正当时已经落在正式历史上的句子也必须当增量推出去。** 打开会话时的首包可以不走事件；已经在看的空订阅切到正式历史时，那些句子不再是「打开时的首包」。只换读取器、把首包吃进缓存、之后只 poll 新行，等于没修好「能收」。历史路径从空变成正式文件、键还没变时同样适用。回归必须断言旧通道收到这些事件，不能只断言缓存有正文。2026-08-31 真机：新开 Cursor 发「在吗」，自己的气泡有了，助手「在的，有什么需要帮忙的？」已在开发机历史里，手机一直看不到——正是这条缺口。禁止当成 0.24.156/157 已修过的长连接问题，禁止当成解析器读不到 protobuf。
5. 服务端发送队列必须有界并按优先级背压：控制和输入优先，实时事件可合并，画面丢旧保新，历史页暂停或取消。队列满不能无条件踢掉整条设备连接。
6. **读取游标只有一个所有者。** 后台轮询、打开会话、翻页、取提问选项，只要把规范化读取位置往前推，就必须同时写入缓存并往该会话所有订阅通道推增量。禁止「谁先 `poll()` 谁吃掉新行」——那会让正在看的对话永远等不到，只有退出重进（重新取尾部）才补上。
7. **`session:{key}` 事件 `kind`**：`delta` 为带 seq 的规范化消息批次（现有）；`echo` 为开发机刚收下的用户输入（`role=user`、`text`，不分配 seq，手机按正文与本地发送中气泡去重）；`attention` 为该会话当前关注状态（`working` / `waiting` / `unread` / `none`）。助手正在跑时面板必须显示「正在处理」，轮询周期应收紧；空闲再放回。
8. 手机点发送必须立刻画出自己的气泡（本地唯一负序号或本地标识），RPC 成功标已送达，失败标可重试。助手历史落地后的正式消息按「同角色 + 正文」吃掉对应本地气泡，禁止出现两条一样的。
9. **当前提问**只来自仍在等待、且后面没有新回复/新工具的那一次询问。一次询问里的多道题必须拆成多组选项，禁止摊成一份列表。过期问卷不得钉在时间线底部。

### 4.3.1 长连接保活（2026-08-30）

握手抢答超时（局域网约 2 秒、中继约 8 秒）**只约束建连竞速**，禁止写进已建立连接的 `URLSession` 资源存活上限。手机必须：定期应用层探活；记录最近一次收到开发机数据的时间，超过阈值判定断线并重连；前台返回时若已超过空闲阈值则探活或强制重选路，禁止因状态仍是「已连接」就什么都不做；数据通道断开后必须重新绑定，绑定失败要记日志，不能静默吞掉。开发机必须记录设备控制面 / 数据面的连上与断开及原因，`corral remote status` 的「当前在线」必须与真实订阅一致。

### 4.4 手机端：网络处理与界面更新解耦

1. 收包、解密、压缩解码、JSON 解码和序号整理放到独立 actor/任务；主执行隔离域只接收已经规范化、已按序整理的小批更新。**最小已落地**：`RemoteClient` 每条物理连接把 decrypt/unpack 放在连接对象上，主隔离域只分发已解码对象。
2. 历史页按窗口追加，实时事件按一帧/一批合并；禁止每一条消息都重建完整数组、完整排序和重复查询提问状态。
3. 重连期间保留最后一份可见历史与终端状态，显示连接状态但不清空内容；恢复成功后按游标合并，不能靠再次全量覆盖来“看起来恢复”。

### 4.5 会话列表：先画本地快照，版本未变不重传

列表体感慢的主因已经不是「行数太多画不动」，而是：**每次进入列表页都新建一份空状态，必须等握手 + 首包回来才有行**。首包截断到当前页（等待/置顶优先、闲置封顶）已经消掉整表 500+ 条塞满中继的问题；再堆滚动懒加载、字段精简、增量补丁或完整离线写入队列，解决不了「空屏等网络」。

本轮只做两件对得上的事：

1. **手机按开发机落下上次成功窗口**：进列表先画出这份快照，再在后台订阅/刷新。没有快照才转圈。刷新失败且本地有行时保留画面，只标明连接问题。切到另一台开发机必须换该机自己的快照，禁止把上一台的行留在这一台。
2. **列表窗口带版本号**：`sessions.watch` / `sessions.list` 可带上次版本。版本相同只回「未变」+ 版本号，不带会话数组。手机收到未变**禁止**当成空列表覆盖。开发机计算版本前先截当前页，只为窗口内的会话组摘要，不为整表闲置会话做完整打包。

搜索走已有的按关键词查询（不限于当前窗口），不要为此做滚动分页。闲置会话被截在窗口外时，用搜索找，不在首屏把几百条都拉下来。搜索命中只用于当前筛选画面，**禁止写回该开发机落盘的列表窗口**。

带条数上限、没有关键词的列会话仍是全表前 N 条（旧契约）；手机当前页窗口是默认不加条数上限的那条路。禁止为了「统一」把前者改成窗口。

本轮明确不做（不是「以后也不做」，是对不上当前瓶颈）：

- 列表滚动懒加载 / 游标翻页：截断已经让首包变小；缺的是进页立刻有行，不是再切一页闲置历史。
- 再砍一行里的预览字段：线路已有压缩，80 行不是 13 秒级瓶颈。
- 按单条补丁同步整表：有变化时重发当前窗口足够；版本未变才是重连和下拉刷新的热路径。
- 完整离线优先写入队列：列表是只读镜像，用户不能在断网时改开发机上的会话。
- 控制/数据通道拆分：那是详情和画面被大历史堵住的调度问题，不在列表首屏（拆分已落地，仍不要为了列表首屏再改连接模型）。历史尾部偏移读取已在详情路径落地，不要把它再当成列表待办。

同类产品（会话列表先读本地再同步）只借鉴「界面以本地为准、网络用来对齐」这一条，不引入独立同步协议。

## 5. 验收契约

代码、构建和单元测试只覆盖下限；本方案的完成条件是同一台真实 iPhone 上完成以下链路，并保存分段数据：

1. 冷启动：开发机已有大量历史时，先显示会话摘要，再按需显示当前会话尾部；记录路径、握手、服务端缓存命中/解析、传输字节、手机解码和首屏时间。**第二次及以后打开同一台开发机的列表：有上次快照就必须先出现行，不能再先转圈再出列表**；后台刷新版本未变则不应闪成空白。覆盖安装后启动常停在上次打开的对话：对话中间转圈是详情路径，**不能据此判定列表快照失败**；须从对话退回列表再进一次才算验了列表。
2. 大历史：至少覆盖 50 MB、100 MB 级历史；打开首屏不随总历史条数线性增长，向上翻页才读取更早窗口。
3. 内容语义：Cursor 的系统上下文、规则和用户资料不出现在用户气泡；真实用户问题保留且不重复；Codex 的现代助手消息、旧格式助手消息和工具结果都显示，**第一句不能是 AGENTS.md / 环境块 / 中断标记**；**Claude、Codex、Cursor、Kimi、OpenCode、Pi 各至少打开一条有正文的会话**（本机没有 Kimi 历史就去开发机抽，不要用「scan=0」跳过整类）。「对本仓库做 code review」这类真人可见提问必须留下。
4. 实时交互：打开历史时仍能发送输入、收到实时消息和终端最新画面；历史大页不能阻塞控制请求。
5. 断线恢复：直连、仅中继、Wi-Fi→蜂窝、短暂中继断开、开发机重启分别测试；恢复后不重复、不丢序，无法回放时明确重新取尾部。
6. 版本真实性：测试前核对常驻服务启动时间与实际加载产物；核对 iPhone 安装包版本。2026-08-29 21:15 物理机：`com.x0c.corral` **1.0.9（build 10）** 已覆盖安装并在跑；本机常驻服务 21:14:06 重启后中继在线、手机已连上。启动后画面停在一条对话（「修复 iOS 键盘与聊天交互」）中间转圈，背后列表已有行；**第二次进同一台开发机的列表是否立刻出字，仍需真人从对话退回列表再进一次**——装上 ≠ 列表快照链路已验收。

## 6. 禁止的误修

- 不要把 `session.watch` 的空 `resume=replay` 当成清空对话；那只表示断线期间没有新缺口。也不要把 `resume=tail` 且 messages 为空、本地已有内容的回包整表替换成空白。
- 不要只把客户端超时从 20 秒改大；这会把服务端全量读取和连接级阻塞隐藏得更久。
- 不要把握手抢答的 2 秒 / 8 秒写进已建立连接的资源存活上限，也不要因为状态仍是「已连接」就在回到前台时跳过探活。
- 不要让 `session.prompts` / 翻页 / 打开会话单独 `poll()` 把新消息吃进缓存却不推给订阅者。
- 不要用「让用户退出对话再点进去」冒充实时已修好。
- 不要只压缩整页 JSON；压缩能减少线路字节，不能替代缓存、按页读取、独立控制通道和断线恢复。
- 不要把「未变」回包解码成空会话数组再整表替换；那会把已经画出来的列表闪没。
- 不要为了列表首屏去上滚动分页、字段精简或完整离线写入队列；当前瓶颈是空状态等网络，不是行渲染或缺翻页。用户随口说的手段不是待办清单，先对上当前瓶颈再动手。
- 不要把覆盖安装后停在上次对话中间转圈写成列表快照失败；那是详情路径。
- 不要把搜索命中写回该开发机的列表快照；也不要把「带条数上限的列会话」改成手机当前页窗口。
- 不要把 Cursor 上下文过滤写在 iOS 的固定字符串黑名单里；运行时格式变化后会再次泄漏，且列表、导出和手机详情会不一致。
- 不要为了让一次 `session.watch` 返回“有数据”而重复全量读取；共享读取游标、缓存快照和历史页必须有明确所有权。
- 不要把“单元测试通过”“模拟器可运行”“真机能启动”写成远程会话链路已验证；必须有真实设备上的列表、历史、实时和恢复证据。

## 7. Detailed Network UX Design Proposal

Status: **approved for implementation, 2026-09-10**. This section expands section 2.0 into the build contract. It does not replace shipped v2 contracts for peers without the new capabilities, and it does not claim production performance until measured. Wire names and limits below are the initial negotiation surface; advertise them explicitly. Existing runtime adapters remain the source of normalized content.

### 7.0 First delivery slices (active)

Unified order (supersedes any conflicting lettering elsewhere in this section):

| Order | Slice | Scope | Exit gate | Production deploy |
|---|---|---|---|---|
| 0 | Baseline samples | Correlated timings on current builds (list first paint, open tail, send ack, concurrent history) | Reproducible before/after samples on this machine + relay path | n/a |
| 1 | Command outcomes | Host durable receipts + `command.status`; phone `want_command_receipts` + stable `command_id`; honest pending/accepted/unknown | Lost-ack / duplicate / restart tests; no silent double-send | Ship with host + phone |
| 2 | State continuity | Bounded phone conversation-tail cache; reopen without flash-empty; **extend existing `after_seq`/`generation` resume** (do not invent a parallel event cursor unless proven insufficient) | Reopen/background without empty flash or host mixing | Ship with phone |
| 3 | Relay non-blocking queues + optional host lane attach | Async per-destination queues; authenticated second host lane when negotiated | Unit/integration proves slow destination does not block host reader; attach does not evict primary | **Code may land; public relay upgrade waits for Slice 0 isolation samples** |
| 4 | Route write-ownership handoff | Validated overlap on Wi-Fi↔cellular; single active command writer | Real network change without duplicate ops | After 1–2 stable |
| 5 | Optional second relay | Provisioned standby | Measured need only | Later |

Attachments chunking and geographic redundancy stay after Slice 3. While Synchronizing: drafts remain editable; sends wait for Ready; stop/delete fail closed (no surprise queue). Unknown outcomes require an explicit user action (review / discard / send a **new** command id)—never auto-resend.

### 7.1 Goals, derivation, and scope

The phone controls work whose authoritative execution lives on another machine. Therefore:

1. Previously observed data can remain available without a connection; authoritative freshness cannot.
2. A remote operation needs communication and a host decision. Local feedback can be immediate, but cannot imply remote completion.
3. A broken connection cannot reveal whether the last command executed. Reconnection must reconcile outcomes, not infer them from socket state.
4. Every network hop has finite bandwidth and queues. Bulk traffic must be bounded and scheduled so it does not consume the interaction budget.
5. Changing a route must preserve host identity, session identity, command identity, and applied history position.

Scope: pairing continuity, list/recent conversation availability, live conversation updates, text/attachment submission, foreground recovery, host-relay transport, and optional relay failover. Existing disabled terminal UI stays disabled. Cloud execution, automatic host wakeup, cloud history storage, queued offline commands, VPN installation, and public NAT traversal are excluded from the first implementation.

### 7.2 Responsibilities and durable state

| Owner | Durable state | Transient state | Authority |
|---|---|---|---|
| Phone | Paired identity, per-host list/recent conversation cache, applied cursors, drafts, unresolved command identities | Route candidates, sockets, decoded update batches | Local navigation/drafts; never host execution |
| Development machine | Pairing ACL, canonical history, command receipts, stable history generations | Bounded change buffers, subscriptions, parse workers, lane queues | Authorization, execution, canonical session state |
| Relay | Existing registered host trust/account state | Host connections, attached lanes, device routes, bounded opaque queues | Routing only; cannot confirm command execution |

All phone cache keys include host public-key identity plus canonical session identity, not display names. Preserve the existing provisional-to-canonical session mapping. Resolve and validate the canonical target before accepting a command; record that target in its receipt so retries cannot retarget a different session.

Phone cache proposal: last successful list window plus recent conversation windows (all synchronized official messages per session, not only the last page), 100 MiB total and up to 50 recently viewed sessions, **400 official messages per session** before oldest-first trim, evicted by recency. Drafts and unresolved command metadata are separate from disposable history. Fetch only a bounded tail on a cold cache miss; Load earlier fills older pages that are then retained until trim/eviction. Store caches under platform file protection and exclude disposable conversation caches from device backup. Pair removal clears associated cached content and unresolved local operations; remote revocation prevents future access but cannot erase data already received by an offline phone.

Do not copy the whole development-machine history to the phone. Each view observes a local store; connection events update that store instead of replacing the view model with an empty one.

**Immutable history ≠ full phone mirror (2026-09-10 review; implemented 2026-09-10).** Host-owned transcripts are append-only / generation-replaced. Product rules:

1. A page the phone has already synchronized must not be re-fetched just because the session is idle, ended, or reopened — **zero network for content still in the local store**.
2. The phone still must not pretend to own the complete archive. Short conversations that fit one window leave `has_more=false` so Load earlier never appears. Long conversations show Load earlier only for content **outside** the local store.
3. Phone cache retains every synchronized official message for a session (including Load earlier pages), up to **400 official messages per session** and the existing 50-session / 100 MiB LRU. Over budget: drop the **oldest** official messages, set `has_more=true`, and recompute `oldest_seq` from what remains — never keep an expanded cursor with a truncated body.
4. On `session.watch` resume=`tail` with the **same history generation**, if local earlier pages **abut** the incoming window (`max(local earlier seq) + 1 == watch.oldest_seq`), merge and keep them. Generation change discards local official history. Non-abutting orphans are dropped to avoid timeline gaps.
5. Offline Load earlier that still needs host content fails closed with a visible connection error; already-cached earlier pages stay on screen.

### 7.3 Physical topology and relay attachment

```text
Phone local store and views
    | interaction: commands, receipts, bounded live updates
    | bulk: history, large message bodies, attachments
    v
LAN: independent authenticated interaction/bulk sockets directly to host
OR
Relay: phone interaction socket -> interaction route -> host interaction socket
       phone bulk socket        -> bulk route        -> host bulk socket
                                                     |
                                    host scheduler / readers / execution
```

Two physical lanes remain distinct across both legs of a relayed route. They still share the underlying link bandwidth; separation is not a reservation of bandwidth. Rate-limit bulk traffic against observed control delay. Within each lane, schedule devices fairly; one slow device cannot block the host receive loop.

The first host socket authenticates the existing host identity and creates a live registration generation. A second socket uses a new authenticated attachment flow rather than the current register-and-replace operation. Its signed assertion binds host identity, relay instance, registration generation, lane kind, timestamp, and nonce. The relay validates against its registered host key, limits attachments, and rejects replay or attachment to a retired generation. The bulk lane cannot replace the registration or become an independent host. Bind state is ephemeral to the relay process; relay restart requires fresh authentication and attachment.

The phone retains independent end-to-end handshakes for each physical lane and the existing host-issued data binding. The relay can see opaque lane/channel identifiers for scheduling, but not session names, message bodies, or operation types. A lane declaration grants a routing class, not unlimited priority: enforce rate and byte limits even on interaction lanes. Do not send unpaired bulk traffic before the host has authorized the attachment.

If the bulk lane fails, keep live conversation and commands working. Reattach it lazily when needed, and pause history/attachments meanwhile. If the interaction lane fails, retain the local view and reconcile commands before resuming writes. Host registration loss invalidates its attached lanes. Route capability negotiation determines whether full isolation is available; legacy operation is explicitly degraded, not falsely reported as isolated.

### 7.4 Connection state and route selection

Track transport status separately from data freshness:

| State | Meaning | User behavior |
|---|---|---|
| Disconnected | No verified route to host | Cached content and drafts remain available |
| Connecting | Candidate transports and host authentication in progress | Existing content stays visible |
| Synchronizing | Host authenticated; active subscriptions/outcomes being reconciled | Reads remain available; writes gated on host readiness |
| Ready | Host can accept commands; active view has a valid recovery boundary | Normal interaction |
| Degraded | Interaction works, bulk path unavailable or slow | Chat usable; history/attachment progress paused |

Each view additionally records its last successful synchronization time and whether a gap is being repaired. Relay registration, socket ping, and host application response are distinct evidence. Only an authenticated host application response refreshes host readiness; a relay-terminated ping cannot prove that the host is alive.

Establishment flow:

1. Show cached content immediately and build current LAN/relay candidates.
2. Race full host authentication, retaining existing LAN/relay timeout defaults initially. A TCP connection or relay greeting does not win the race.
3. Adopt the first authenticated usable interaction route; request active-view recovery and unresolved operation status without waiting for the bulk lane.
4. Establish bulk only when needed. Record actual route and setup duration for each lane.
5. On network change or foreground return, trigger an immediate health check/reselection rather than waiting out a previous failure backoff. Coalesce repeated network events.

Healthy-path improvement proposal: switch only after three successful probes over at least five seconds show both 25% and 30 ms improvement in authenticated end-to-end round-trip latency. Keep a 30-second switch cooldown. These provisional values do not delay replacement of a failed path. Probe while foreground and active; avoid a permanently connected second phone route solely for optimization.

For a still-working old route, authenticate the replacement, restore subscriptions to a captured boundary, atomically transfer write ownership, then close the old route. Before ownership transfer, outstanding old-route sends are classified as resolved or requiring status reconciliation. Deduplicate read overlap by history generation/event sequence. Never broadcast commands down both routes. An already dead route cannot be preserved; resume from durable local positions.

Background: persist local positions and unresolved operations, stop optional probes, and tolerate suspension. Foreground: revalidate host reachability and catch up even when no push was delivered. Continuous background sockets and guaranteed silent pushes are not assumptions.

### 7.5 Snapshot, event, and subscription recovery

Use separate identifiers for distinct facts:

| Identifier | Purpose |
|---|---|
| Host identity | Stable paired authority across routes and restarts |
| Host run identifier | Detect process restart and stale in-flight responses |
| Session identity / history generation | Stable conversation and history replacement boundary |
| Message identity | Update an existing streaming message without creating duplicates |
| Event sequence | Position in an ordered stream of mutations, including updates to an existing message |
| Subscription epoch | Reject responses belonging to an abandoned view/subscription |
| Command identity | Reconcile one requested side effect across retries |

Shipped resume already uses message `seq` plus history `generation` (`after_seq` on `session.watch`). **Default path: extend that contract.** Do not silently reinterpret `seq` as a different kind of cursor, and do not add a second parallel event cursor unless measurement shows streaming in-place edits cannot be recovered with generation + seq alone. Legacy clients keep current semantics.

Atomic snapshot-to-stream handoff on the host:

1. Under the subscription coordinator, capture a snapshot and its high-water event position and register subsequent delivery.
2. Return the snapshot through that position, followed by ordered events after it. Serialize network output without holding parse/index locks.
3. The phone applies events and advances the durable cursor in the same local transaction. Acknowledging an event before its state is stored is forbidden.
4. Resume with host/session identity, history generation, and last contiguous applied event position. Matching retained history returns only the gap; empty replay means caught up.
5. A missing event, expired buffer, or generation mismatch requests a bounded snapshot. Preserve visible old content until a valid replacement arrives and represent any discontinuity; do not fabricate complete history across a missing interval.

Candidate replay budget: 2 MiB or 2,000 events per active session, whichever comes first, with a 64 MiB host-wide ceiling and idle eviction. It is an accelerator, not canonical history. If the buffer is lost on host restart, return a bounded resynchronization snapshot. Persist stable message/generation identity when possible; explicitly reset the event-stream epoch when continuity cannot be established.

Prioritize the visible conversation, pending-operation reconciliation, and current list window. Delay other subscriptions. Leaving a page cancels its history requests and advances its subscription epoch, so late responses cannot replace the new page.

### 7.6 Command acceptance and outcome reconciliation

Proposed command envelope: unique command ID, canonical target, operation type, immutable payload digest, payload/attachment references, and a host-run-bound execution lease. Device identity comes from the authenticated channel. A request-response correlation ID is separate and may change during a status query.

Host state progression:

```text
unseen -> accepted -> dispatching -> delivered
             |             |
          rejected       unknown after crash / ambiguous adapter outcome
```

- `accepted`: authorization and target validated; immutable command and initial receipt committed durably. Only then may the host acknowledge acceptance.
- `dispatching`: persisted immediately before attempting the external side effect. Serialize conflicting commands per canonical session, while allowing independent sessions to proceed. A stop request may interrupt generation; it must not wait for model completion.
- `delivered`: the runtime adapter confirmed delivery, not completion of the assistant's work. Later assistant messages describe progress/outcome.
- `rejected`: no external side effect occurred; include a retryable/nonretryable reason.
- `unknown`: the host cannot prove whether the external side effect occurred. Never convert this automatically to rejected or replay it.

Persist receipts in a transactional host store with a unique key on authenticated device identity plus command ID. Same key and same digest returns the existing receipt; same key with different payload is rejected. Concurrent duplicates use the same atomic insert/claim. Status queries are read-only and can be retried.

Phone send flow:

1. Persist command identity and local outgoing bubble, then clear the composer. Initially show pending, not delivered.
2. Send once through the active interaction route. A valid accepted receipt changes the status to accepted by the development machine.
3. If a response is lost, query that command ID after reconnect. Do not generate a new ID automatically.
4. If the host confirms unseen and the original execution lease is still valid on the same host run, resend the same immutable command. Otherwise preserve it for explicit review.
5. After host restart, accepted-but-not-dispatched commands are rejected as interrupted; dispatching commands without a trustworthy adapter receipt become unknown. Delivered receipts remain queryable.

Proposed execution lease: 30 seconds on the host monotonic clock, bound to authenticated device, target, and host run. It limits when dispatch may begin, not model execution duration. Retries cannot refresh the old command's lease. Reject expired unseen submissions; status queries remain allowed. This prevents forgotten commands from executing much later without relying on synchronized phone/host clocks.

Keep resolved receipts for seven days. When capacity is exhausted, reject new commands before acceptance instead of evicting receipts that are still within their protection window. Receipt retention expiry is not proof of nonexecution; expired leases ensure old IDs cannot become executable again. Retain unknown receipts until reviewed, with admission limits so they cannot grow unbounded.

End-to-end exactly-once execution cannot be promised for terminal injection: a process may crash after injection and before recording delivery. Use runtime-provided idempotency/status when available. Otherwise expose ambiguity and require user review before a new command. Text equality alone is not a command identity; two intentionally identical messages must remain two messages. Match the final transcript to a command only with reliable correlation; legacy text matching remains a limited presentation fallback.

Offline text stays a draft. Stop/delete/key actions fail promptly when execution readiness is absent. This proposal does not add an automatic offline command queue.

### 7.7 Scheduling, framing, and bounded resources

Provisional engineering defaults, subject to measurement:

| Resource | Initial budget / behavior |
|---|---|
| Interaction frame | 32 KiB maximum payload; large bodies referenced through bulk |
| Live batch | Flush within 50 ms or 16 KiB; preserve event order |
| Bulk chunk | 64 KiB, up to four unacknowledged chunks per active transfer |
| History page | Up to 100 messages and 256 KiB encoded body; oversized message uses bounded body fetch |
| Relay destination queue | 256 KiB interaction, 512 KiB bulk, hard byte accounting |
| Relay host aggregate | 8 MiB queued; global limit with admission control |
| Host phone aggregate | 2 MiB queued per phone, 32 MiB globally |
| Transfers | One active history fetch and one attachment transfer per phone initially |

Application encoding, decoded lengths, frame lengths, and in-flight chunks all count toward limits. Compression cannot bypass decompressed-byte limits. Large command text uploads as a temporary body through bulk, then a small command references its verified content hash.

At the host, use bounded parse work outside the connection reader and scheduler. Prefer change-triggered processing for the active conversation with a bounded polling fallback; avoid polling every historical session at interactive frequency. Preserve one owner of each runtime read cursor and broadcast every consumed change.

At the relay, frame ingestion validates and enqueues quickly; independent writers drain per-destination queues. Do not hold the host receive loop while a phone write blocks. Use fair byte scheduling between phones within a lane. Prioritize receipts/health traffic over bounded live batches before encryption; the relay only uses the declared lane and quotas. Reserve scheduling opportunities for history so interaction traffic cannot starve it indefinitely.

Queue overflow must respect cryptographic ordering. Coalesce replaceable state before encryption at the host. The relay must not drop arbitrary ciphertext and continue a channel whose counters assume contiguous delivery. If a queue cannot accept reliable ciphertext, close/reset only that affected destination lane and let endpoints resume using a new handshake and cursor. Notify the host to stop producing for it; do not disconnect all phones. A reset must not use an overflowing queue to signal its own failure.

When interaction queueing rises beyond the provisional 100 ms budget, reduce bulk credit/rate. Queues must stay bounded through a slow second phone, bandwidth collapse, relay disconnect, and host overload. Metrics include queue bytes, oldest age, resets, refused work, and time blocked on socket writes.

### 7.8 Attachments and cancellation

Upload attachments before the side-effecting send. Assign a transfer ID scoped to the paired device and host, declare total size and content hash, upload bounded chunks, and query the host's confirmed contiguous offset after reconnect. The host enforces its existing media policy plus negotiated total-size/quota limits, writes into temporary storage, verifies the final hash, then returns a completed attachment reference.

Repeated completion for the same transfer is idempotent. An incomplete or mismatched attachment cannot be referenced by a command. Cancel removes only that transfer's temporary object; expiring unused uploads is safe after a proposed 30-minute idle TTL. Once a command has accepted an attachment, it is pinned until the command outcome is settled and then follows the existing product retention policy. Do not promise canceling a transfer will undo a command already delivered to an assistant.

### 7.9 Optional second relay

A second relay is a later availability feature, not required for the first two-lane implementation. The host maintains independently authenticated presence on primary and standby; standby bulk is lazy. Trusted host registrations must exist consistently on both nodes before failover. Do not use unauthenticated discovery or DNS change as authority to replace paired host keys.

Distribute the candidate set to already paired phones through an authenticated host response and persist it. A phone that never received the standby address cannot benefit during the primary's first outage; provisioning and upgrade acceptance must cover this case. A phone selects among relays on which this host is reachable, using full authenticated host round trips. Geography or phone-to-relay ping alone cannot select a winner.

Each relay is an independent forwarding route; avoid cross-relay message replication and inter-relay forwarding in this phase. Command deduplication remains on the host across both routes. Only one host route owns push emission for an event, and the phone deduplicates notification event identities. Existing pairing continues; no repeated scanning is needed.

Measure domestic Wi-Fi and actual cellular carriers to the existing node and candidate nodes before choosing locations. Test normal and busy periods. Changing a hostname alone is not latency optimization. Additional relay reachability does not make a sleeping or powered-off host executable.

### 7.10 User journeys and failure behavior

| Scenario | Immediate result | Recovery / success evidence |
|---|---|---|
| Reopen known conversation | Cached tail, draft, position visible | Apply only missing updates; freshness advances |
| First open without cache | Lightweight loading state with usable back navigation | Bounded valid tail replaces loading |
| Wi-Fi to cellular | Content remains; brief reconnect status | Authenticated new route and cursor catch-up |
| Send during link failure | Pending bubble remains | Query receipt; deliver, reject, or show unknown |
| History load while sending | Composer and live messages remain responsive | Bulk slows first; accepted receipt independent |
| Data lane failure | Conversation/control remain usable | Transfer/history resumes after independent rebind |
| Slow second phone | First phone continues | Only slow destination is throttled/reset |
| Host restart | Cached content remains, remote freshness unavailable | Reconcile receipts; replace invalid replay epoch |
| Host asleep | Clear unavailable status, drafts usable | Resume only when host actually responds |
| Missed push / long background | Previous content visible on return | Explicit catch-up without relying on notification |
| Pair revoked | Stop remote operations | Host denies old device on all routes |

Use the existing localized interface vocabulary and restrained inline status. Do not show transport internals, debugging controls, or new terminal entry points. Delivered-to-assistant and assistant-completed are distinct states.

### 7.11 Measurement and acceptance contract

Record a correlated timeline without conversation bodies: user action, local paint, transport ready, authenticated host ready, host queue start/end, parse completion, response received, decoded/applied, visible paint. Use monotonic durations measured on each endpoint and phone end-to-end elapsed time; do not subtract unsynchronized device clocks. Track path, cache-hit class, negotiated capabilities, payload bytes, and host/phone build identities.

Targets below apply to a defined healthy reference network (<= 150 ms measured end-to-end round trip, >= 5 Mbit/s usable throughput, no injected loss) and are proposals:

| Metric | Proposed p95 target | Boundary |
|---|---|---|
| Cached view visible | <= 200 ms | View request to usable cached paint |
| Local send feedback | <= 100 ms | Tap to pending bubble |
| Host acceptance | <= 1 s | Tap to durable host acceptance receipt; no attachment upload |
| Fresh current tail | <= 2 s | Open to valid current tail, report cache state separately |
| Active-session update | <= 500 ms | New source data observable on host to applied phone update; excludes model generation |
| Route recovery | <= 3 s | Replacement network usable to visible subscription caught up |
| Load isolation | <= 100 ms added control queueing | Concurrent bulk compared with idle baseline |

For latency percentiles, collect at least 100 operations per reference route/scenario where repeatable, and retain raw samples. For weaker networks, report achieved times rather than pretending the reference SLO still applies; require bounded memory, usable cached navigation, no silent content loss, and honest command outcomes.

Fault tests: cut before host acceptance; after acceptance before delivery; after delivery before receipt; during every attachment chunk boundary; during snapshot-to-stream handoff; during route ownership transfer. Restart host and relay separately, inject duplicate/out-of-order frames at the application test layer, expire replay buffers/leases/receipts, revoke pairing, and saturate a second phone's receive path. Assert either one evidenced delivery or an explicit unknown outcome, never blind re-execution.

End-to-end acceptance runs on iPhone Max through LAN and relay, real Wi-Fi/cellular transitions, background return, and each of Claude/Codex/Cursor/Kimi/OpenCode/Pi. Include 50 MiB and 100 MiB source histories and an oversized single message. Verify visible navigation/input while loading. Record installed phone build and running host/relay binaries; source tests alone do not close acceptance.

### 7.12 Compatibility, delivery slices, and rollback

| Slice | Deliverable | Exit gate |
|---|---|---|
| Baseline | Correlated timings and fault harness; no new transport | Reproducible bottleneck/latency samples |
| State continuity | Bounded phone cache, view/subscription epochs, explicit replay contract | Reopen/background/gap tests without flash-empty or host mixing |
| Command outcomes | Durable receipt store, stable IDs, leases, status reconciliation | Lost-ack/crash/duplicate tests with honest ambiguity |
| Full route isolation | Authenticated additional host lane, fair bounded queues/chunks | Slow-phone and bulk-load isolation through actual relay |
| Route replacement | Validated overlap, command write ownership, immediate network-event recovery | Real network changes without duplicate operations |
| Optional redundancy | Provisioned second relay and persisted candidates | Primary outage recovery without re-pairing |

Negotiate command receipts, event cursors, chunked bulk, and relay host lanes independently. Keep shipped v2 behavior for peers without a capability. New phone/old host must not claim durable delivery or safe automatic resend; old phone/new host continues its established protocol. Old relay permits existing single host socket only. Proposed incompatible relay framing gets a separately negotiated version/endpoint; do not reinterpret existing frame fields silently.

Upgrade order for full isolation: relay accepts old and new paths; host enables new attachment only when supported; phone enables matching capabilities last. Deploy incrementally by known host/device, retaining old registration paths and local identity. Rollback disables new negotiation, preserves receipt data and caches, and resets incompatible stream epochs explicitly. Unknown commands remain blocked for review even after rollback. A binary unable to read active command receipts cannot be used for an automatic rollback while unresolved operations exist.

### 7.13 Review decisions (approved)

Approved defaults: cached recent content available offline; offline text remains a draft; no automatic retry of ambiguous commands; no cloud history/command storage; existing WebSocket transport retained for the first release; first release targets one relay with complete lane isolation; second relay follows measured need. Cache and queue budgets are provisional engineering parameters, not user-facing settings.

Implementation notes locked with approval: place host receipts beside existing remote durable state under the Corral data directory; negotiate capability `command_receipts` (and later `host_lane_attach`) in hello; keep legacy `input.*` responses working when the capability is absent. Adapter delivery evidence may still be incomplete for some assistants — when an adapter cannot prove delivery, the receipt must stay `unknown` or `accepted`/`dispatching` honestly rather than inventing success. Latency targets and candidate relay nodes remain unmeasured proposals until instrumented.

### 7.14 Baseline samples (2026-09-10)

| Sample | Result | Implication |
|---|---|---|
| Host `command_receipts` unit suite | 15/15 pass (project venv) | Slice 1 host side is ready for phone opt-in |
| Relay `AttachHostLane` + queued sink tests | `go test` hub/protocol/server pass | Slice 3 code can stay in-repo; async queue removes reader-blocking structure |
| Microbench: enqueue 20 frames while 50 ms/frame consumer | ~0.02 ms to enqueue vs ~1 s if synchronous | Confirms Slice 3 queue value independent of geography |
| Device → public relay `wss://pickup-relay.caozc.top` | HTTP **503** on WebSocket (retry same) | Host still reports relay online — **do not upgrade public relay** until device path is healthy; treat host-online ≠ phone-reachable |
| Live LAN hello/list probe | Handshake frames OK; full RPC timed out on this probe identity | Need paired probe + restarted host process before claiming end-to-end UX numbers |

Phone hello must send `want_command_receipts: true` or receipted sends never enable. Extend `after_seq`/`generation`; do not add a second event cursor in Slice 2.

<!-- 该文档整理/压缩于 2026-09-05；详细方案增补于 2026-09-10 -->

## 8. Content-first history loading (2026-09-11)

### 8.1 User intent and scope

Existing mobile conversations must open on conversation text first. Tool call bodies are secondary and load when the user opens the existing activity sheet (or expands a card), not on first paint. Optional prefetch remains a later measurement-driven addition.

### 8.2 Implemented contract (shipped)

- History wire (`to_wire_dict` / `session.messages` / `session.watch`): tool rows are summaries with `has_detail`; `detail`/`output` omitted except question `detail` needed for live prompts.
- On-demand: `session.toolDetail` (`key`, `seq`, optional `tool_id`/`offset`/`limit`) returns bounded full tool bodies. Hello advertises `capabilities.tool_detail`.
- Host cold JSONL reads stop expanding the tail window once enough messages exist; they no longer keep doubling solely to pair historical tool results.
- Phone: activity sheet fetches details on open; reopen cache strips tool bodies so warm paint stays text-first; one activity card per host message seq.

### 8.3 User-visible loading contract

| Content | Initial conversation | Subsequent loading |
|---|---|---|
| Recent user/assistant text | First readable window; reuse local content immediately | Earlier text near the upper boundary or on explicit request |
| Tool activity | One stable summary per activity group: count, state, brief label, detail availability | Open existing large sheet to fetch a bounded list of tool summaries; load selected output separately |
| Active question or approval | Small authoritative current-state payload, high priority | Keep freshness independent of historical tool output; stale cached options are not actionable |
| Failure | Concise visible failure state/reason | Full logs on demand |
| Images/files/large code or diffs | Dimensions/type/size and bounded preview | Visible thumbnails first; original or full body on demand |
| All historical user prompts | No full index fetch needed for chat paint | Load when the existing prompt sheet opens; jump directly to the target context |

Proposed first text window: roughly 20–40 messages with a strict 64 KiB decoded-payload budget, adjusted after measurements. This refines the larger transport ceiling in section 7.7, not an assertion that a particular message count guarantees a fast screen. A single oversized body must expose a preview plus a retrievable continuation; never silently truncate conversation text. Preserve message order and indicate omitted earlier context. A tool-only active turn still displays activity rather than a false empty conversation.

Default to on-demand tool details. Optional prefetch begins only after readable text and current state are applied, for at most the visible/nearest activity group, with one cancellable request and a strict byte budget. Suspend it on weak/constrained networks, interaction backlog, page exit, or backgrounding. Do not crawl the entire history silently. Download completion must not expand a card or move the reading position.

### 8.4 Ownership and work outside the first-paint path

The host owns source history, normalization, stable message/tool identity, exact activity metadata when known, and source-offset/body references. Keep text/summary records separate from tool bodies in the persistent index. Build and extend the index once as source data arrives; share it across phone requests. Cold reads have byte/time/work limits and can return a smaller valid text window rather than waiting to fill a nominal count or complete remote tool pairs. Missing historical pairing must not invent success or drop user/assistant text. Giant JSONL records require incremental extraction or prebuilt offsets; stripping fields after full JSON parsing is insufficient.

The phone owns the visible text window, reading anchor, draft, small summary cache, and a separately bounded detail cache. Load only the visible working set off the UI execution path; incrementally persist changed records. Keep recent text available even if tool details are evicted. Cached historical status is not evidence that a tool is still running now. Details not cached while offline show an explicit unavailable state, while existing text stays readable.

Detail references bind authenticated host identity, canonical session, history generation, tool identity, and revision. Fetch by scoped opaque reference, never a client-supplied arbitrary filesystem path. Source replacement invalidates references explicitly. A detail request returns a complete bounded page, continuation, or honest unavailable/expired result; not a misleading empty success. Updates to a running tool refresh its summary revision without retransmitting unchanged output. Detail cache refresh must never replace newer live state. Snapshot-to-stream continuity and existing message cursors remain governed by section 7.5.

Prioritize current user actions/state and live text, then the requested text window, then explicitly opened details, then speculative prefetch. Reserve bounded service for requested history so it cannot starve. Keep source parsing locks, host queues, both network directions, and phone decoding bounded; separate phone sockets alone do not establish relay isolation. Leaving a page cancels obsolete work and rejects late replies. Preserve end-to-end encryption and current pairing boundaries.

### 8.5 Delivery order and acceptance proposal

1. Establish an installed-runtime baseline: distinguish first-ever phone open, warm reopen, host-cache miss, source replacement, and weak-network cases. Time source reads/parsing, encoding, queueing, wire transfer, phone decode/cache load, and first usable paint independently; never log conversation bodies.
2. Deliver one complete text-first path: lightweight summaries plus working on-demand detail retrieval, strict single-body bounds, bounded host cold reads, and split phone cache. Shipping summaries while detail retrieval is absent is not complete.
3. Optimize incremental persistence, changed-message rendering, earlier-page/context jumps, thumbnail loading, and bounded prefetch where measurements justify them. Existing Markdown limits/cache and history/replay machinery are prior work to verify, not new deliverables to claim.

Proposed targets reuse the healthy network conditions in section 7.11: p95 warm cached paint <=200 ms; fresh text <=2 s. Open detail shell immediately; aim for its first bounded content <=1 s on a warm connected host. Record misses separately. Holding recent text constant while increasing old tool output must not grow initial wire bytes or phone decode cost; a host without an index must demonstrate bounded initial work and explicit partial coverage rather than claiming complete history.

Acceptance on iPhone Max covers all six assistants, 50/100 MiB histories, few-text/many-tool sessions, an oversized single record, incomplete tool pairs, existing warm caches, offline reopen, host identity switches, source replacement, and page exit during a detail request. Confirm readable text, usable back/input, preserved scroll position/text selection, complete retrievable detail when source exists, honest missing-detail states, and no delayed active question/approval. Report first readable paint separately from background completion.

Apple references checked for this proposal: [scrollable stack performance](https://developer.apple.com/documentation/swiftui/creating-performant-scrollable-stacks/) and [SwiftUI performance diagnosis](https://developer.apple.com/documentation/Xcode/understanding-and-improving-swiftui-performance). Use measured main-thread/view costs; lazy view creation does not imply lazy data transfer. The budgets and tool-loading policy above are Corral proposals, not Apple requirements.
