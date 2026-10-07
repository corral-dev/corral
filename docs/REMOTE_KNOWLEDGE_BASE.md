# 手机端远程接力（corral remote）

覆盖「手机连开发机看会话 / 输入 / 推送 / 配对 / 局域网直连 / **换网不可用与中继** / **开源默认中继 / 不要暴露维护者服务器 / 别人要用自己搭中继**」。

配套客户端：`../ios/`（见 `../ios/AGENTS.md`）。零知识中继：`../relay/`（开源自建看其 README）。维护者本人的多租户公网实例运维只写在私有 agentsync 基础设施知识库，**禁止**写进公开 GitHub 门面当默认地址。

## §0 目录索引

- [Task execution reliability](#task-execution-reliability)
- [开源中继硬规则](#开源中继硬规则2026-09-12-用户裁定-记牢)
- [产品边界](#产品边界)
- [Native agent questions: implemented paths](#native-agent-questions-implemented-paths-2026-09-29)
- [命令入口](#命令入口)
- [协议分层](#协议分层)
- [加密与身份](#加密与身份)
- [安全边界](#安全边界)
- [连接策略](#连接策略) · [Same-machine and LAN connections never drop](#same-machine-and-lan-connections-never-drop-owner-requirement-2026-10-05)
- [踩坑](#踩坑)
- [验证](#验证)

## Task execution reliability

Project-list responses must contain plain JSON strings for names and paths, including the no-project entry. Resolve lazy desktop localization before crossing the remote boundary. The new-session form must distinguish loading from failure, offer explicit retry, and disable manual creation until assistant and project options are valid; a failed project request must never silently create in the default directory.

- Execution state must agree across the host, mobile list, and open conversation. Opening or reconnecting during a running turn must immediately restore the current state; reading a conversation must not clear working. Process existence alone does not prove an active turn.
- State freshness (owner requirement 2026-10-05): a status change that is observable on the host — turn started/finished, question answered, process exited, session newly created in the TUI, provisional card becoming canonical — must reach every connected client (Mac, iPhone) within about 2 seconds. This budget must not depend on the full history-scan cadence and must not be relaxed by memory-pressure backoff; only the expensive full scan may slow down. See the pitfall row "Clients lag the TUI by tens of seconds".


Phone-submitted tasks must continue through normal Agent execution; delivery acknowledgement is not task completion. Automatic idle and capacity cleanup are disabled by default. Manual termination remains available. Automatic cleanup must never terminate an executing Agent. Input sent through the phone and output from detached terminals count as activity. If execution is interrupted or cannot resume, preserve the original task and surface failure rather than implying completion. Never blindly replay a delivered task because it may already have changed user data.

## 开源中继硬规则（2026-09-12 用户裁定 · 记牢）

1. **开源产品不提供、不暗示共用维护者的多租户中继。** 陌生人 clone / brew / 装 App 之后，默认只能走**局域网直连**；要换网/蜂窝可达，必须**自己部署** `corral-relay`（或显式配置自己的 `wss://`），再 `corral remote on --relay-url …` / 配对载荷里的 `r=`。
2. **禁止**在公开 GitHub 源码、README、安装说明、示例命令、iOS 内置常量里写入维护者个人中继域名，也禁止写成「默认公共中继」「开箱即用的共享中继」。
3. **禁止**把「维护者自己在用的多租户服务」当成开源用户的默认依赖——多租户账号登录（`corral login`）只服务**你自己部署的多租户中继**；单租户自建不需要 login。
4. 维护者本机已写入 `remote.json` 的中继地址可以继续用；那是**本机状态**，不是开源默认。新装 / 空状态：`relay_enabled=false`、`relay_url=""`，只开局域网。
5. 代码里若需识别「某个 URL 要走多租户 GitHub 登录」，只允许读环境变量允许名单（如 `CORRAL_PUBLIC_RELAY_URLS`），**默认名单为空**——不要把私人域名写死进仓库。
6. **Official paid builds (owner decision 2026-10-05; narrows rules 1 and 3 for these builds only).** The official App Store builds may offer an opt-in, maintainer-operated hosted relay as part of the paid subscription. Its endpoint and credentials reach the client and host through the paid account (private build configuration or server-provided after purchase), never through public source, README, install instructions, examples or built-in constants; rules 2 and 5 still apply. Open-source and self-built clients, the free tier and new CLI installs keep rules 1–4 unchanged: LAN only unless the user configures their own relay.

## 产品边界

- **Every Apple client (iPhone, iPad, Mac) is a remote client of the host** (owner decision 2026-10-01): it pairs, lists, reads and sends input; starting, forking or handing off a session is always a host request (`session.new` / `session.copy` / `session.handoff`). A Mac client never runs agents, tmux or SessKit locally. Adaptation plan: [iPad and Mac client adaptation](../../ios/docs/design/MULTIPLATFORM_CLIENT_DESIGN.md).
- 开发机远程服务是**开关**：`corral remote on` 打开、`corral remote off` 关掉，都幂等。打开后进程在**后台**常驻，命令立刻返回；不要把 `on` 当成前台守护进程，也不要因为命令马上结束就以为服务没起来——用 `corral remote status` 看是否 on。
- **开关必须记住**：`on` 把「想要开着」写入状态，并登记开机/登录自启（macOS LaunchAgent `com.x0c.corral.remote`，Linux `systemd --user` 的 `corral-remote.service`）；`off` 清掉记忆并撤销自启。重启、重新登录或进程崩溃后，只要上次是开，就必须自动回来——禁止要求用户每次开机再敲一次 `on`。排查「重启后手机立刻 failed / 本机远程 Status: off」先看开关记忆与自启是否在，不要先怪中继。**调度档（2026-09-30 裁定，已实施）：macOS 侧 plist 固定 `ProcessType=Interactive`，不用 Adaptive**——官方 `launchd.plist(5)`：不填则限 CPU 与 I/O 带宽；Adaptive 只按 XPC 连接活跃度在 Background/Interactive 间升降（https://github.com/apple-oss-distributions/launchd/blob/main/man/launchd.plist.5 ），而守护进程经 websocket/中继服务手机、无 XPC，只会永远钉在 Background；手机在等请求结果，属于响应性依赖，用 Interactive（与保活 server 的 `tmux_server.py` 同口径）。后台扫描线程照旧 `demote_background()` 让路。`enable()` 每次重写 plist 并 bootout/bootstrap，升级后对正在用的机器再执行一次 `corral remote on` 即换档重启；缺该键的旧 plist 会被 `is_installed()` 判为未登记以提示补 `on`。
- **配对与开关拆开**：二维码 / 手动配对码只由 `corral remote pair`（或 `pair --readonly`）输出。`on` / `off` / 再次 `on` **不得**顺带打开配对窗口。二维码仍是一次性、十分钟有效的 v2 载荷（含中继地址），不得为方便展示而弱化配对或换网可达性。
- `on` 已打开时再次执行：不重启、不刷码，只回报已打开（`--force` 才先停再开）。`off` 已关闭时再次执行也成功（幂等）。
- 需要前台占终端时（调试 / systemd `Type=simple`）才用 `corral remote on --foreground`；默认路径禁止占终端。
- `start` / `stop` 仍是 `on` / `off` 的兼容别名，行为已切到开关语义（后台、不刷码），禁止再按旧「前台 start + 顺带打码」理解。
- 手机与开发机的远程接力须按端到端链路持续优化：没有会话或画面变化时，不得反复编码、复制或广播同一份数据；有变化时仍须及时送达所有已订阅页面。性能改动不得把事件改回单消费者，也不得恢复手机改变开发机窗口尺寸的能力。
- 手机端远程数据必须遵守明确的数据边界：一次只传当前会话和当前页面需要的字段，不把无关会话、无关参与者、内部事件或无界历史塞进移动端首包；历史、增量和画面必须分别定义上限、游标/序号与重同步方式。
- 远程协议必须把「快照、增量、确认、重连」作为一套契约设计：每个订阅都能说明数据范围、版本、序号与是否完整；断线后按游标补增量，游标失效才请求受限快照，不能靠客户端猜测或把整份历史反复重传。
- 加密前允许对可压缩的结构化载荷做无损压缩：超过约 1 KB 才压缩，载荷头带标记与未压缩长度上限；密文、中继和解密边界不变，不能为了压缩泄露会话明文。开发机与手机必须使用同一套 raw DEFLATE（RFC 1951）。Apple Compression 的 `COMPRESSION_ZLIB` 名称容易误导——它实际输出的是裸 deflate，不是 zlib 包装；不要改成 `zlib.compress()` 的 RFC 1950 包装去“对齐名字”。
- 会话消息的语义只允许用户与助手进入聊天时间线；工具调用、服务事件、连接状态和未知角色必须有独立的数据类别，不能被当作普通聊天气泡，也不能因客户端静默丢弃而让助手正文消失。
- 手机发出的话必须立刻出现在正在看的对话里，不能等助手把这轮写进历史、也不能等开发机轮询到才出气泡。点发送后立刻清空输入框并画出自己的气泡（发送中 → 已送达 / 失败可重试）；开发机收下输入后向该会话通道广播回显，供本机去重和第二台手机同步。助手回复仍必须经实时订阅到达，禁止把「输入框空了」或「发送 RPC 成功」当成对话已跟上。
- **手机端发送永远按 steering，禁止排队成「等本轮结束后再接」的 follow-up。**【裁定·2026-09-11】用户从手机在会话中途发的话，必须插入当前正在跑的一轮（在安全边界转向或立刻打断并送入），不得落成 Cursor 终端里那种等本轮全部做完才处理的 follow-up 队列。桌面端自己在终端里按一次回车可以排队；手机路径不得复用该语义。实现落在开发机 `input.text`：对 Cursor 托管会话，粘贴正文并回车后**再补一次空回车**（Cursor CLI 把排队 follow-up 提升为当前轮 steering / interrupt-and-send 的官方键位）；仅当助手在等你回答选择题（`attention=waiting`）时仍只按一次回车，避免误提交空句。其它助手没有同一套 follow-up/steer 二分，保持粘贴 + 单次回车。排查「手机中途发消息却是 follow up 不是 steering / 要等助手跑完才接」先核本条与 `SessionHub.send_text`，不要去改手机 UI 文案。
- 手机与开发机之间的控制连接必须是真正的长连接：握手抢答的 2 秒 / 8 秒超时只约束建连，不得写进已建立 socket 的资源存活上限。手机必须主动探活，并在一段时间收不到开发机数据时自己判定断线、重连、重新登记订阅。状态显示「已连接」但对话不再更新，按断线处理，不要等系统 socket 报错。数据通道掉了必须自动接回；接不上时打开会话会变慢，但控制面仍须能推实时对话。
- 「退出这条会话再点进去，缺的内容就补上了」判定为**推送链路断了**，不是解析器没读到。重进走的是重新取尾部窗口。禁止把「让用户退回列表重进」当成修复，也禁止只修解析、不修保活。
- **Mac local notifications (2026-10-05).** The host publishes `kind=notification` events on the existing encrypted `sessions` subscription for waiting and terminal transitions, carrying `notification` (session payload) and `notification_kind` (`waiting`, `completed`, `aborted`). This is independent of phone APNs preferences and list pagination/version fingerprints; no extra scan/listener is introduced. Baseline histories are silent. Empty completion identity cannot produce a completion event. The Mac deduplicates locally and uses native UserNotifications; the phone keeps its APNs path. iPhone completion/abort preferences are visible, default on, and included in every `push.register`, so old host-side containment mutes do not remain invisible forever.

- **会话结束后的手机系统通知（2026-09-15）**：用户离开前台后，一轮正常结束或异常结束（额度用尽、限流、供应商报错等）都应发系统通知。触发与文案口径**基于 SessKit 的会话结束状态**（`status_tag` 已完成 / 已中断 + `last_agent_msg`；合同见 `~/Codes/SessKit/docs/CONTRACT.md`「Status tags and abnormal endings」，Corral 钉 SessKit ≥0.1.5）。开发机 `PushNotifier` 在 `SessionHub` 扫到标签跃迁时经中继加密投递；`kind` 为 `completed` / `aborted`（另保留既有 `waiting`）。禁止只看进程退出、侧栏关注圆点、或裸 `DONE`/`已完成`（假完成会把额度挂掉当成干完了）。成功与异常文案分开；异常带可读报错摘要。同一会话同类提醒两分钟节流。**短会话漏推（2026-09-15）**：服务刚启动或两次扫描之间，新会话首次出现就已是已完成/已中断时，启动基线本来不推——若历史 `mtime` 在约 5 分钟内仍须推（`v0.24.216+`）；更旧的存量终端态保持静默。扫描侧细则见 `SESSION_SCANNING_KNOWLEDGE_BASE.md` §6。
- 新开会话会先用一张临时卡片，助手落下第一句真实历史后再换成正式编号。电脑侧栏会跟着换；**手机仍停在旧编号的详情页时，必须继续能发、能收**。「能收」包括转正那一刻已经写进正式历史的第一句用户话和助手回复，不能只保证转正之后再增量出来的句子。禁止只换读取位置、把这些句子吃进缓存却不推到手机原来的通道。禁止把旧编号当成「已经不在列表里」——那不是配对错了，是编号转正后远程没跟上。**守护进程重启后旧临时编号仍须可用（2026-09-30 要求）：临时键 `runtime:<8位ident>` 与正式键 `runtime:<完整原生id>` 的映射只活在内存，转正迁移表随重启丢失；重启后必须经精确 `keepalive_name`（`corral-<runtime>-<ident>` 等全部前后缀）反查到已 `annotate` 贴名的正式会话，并支持原生 id 前缀/完整两种形态；命中多条或零条时仍报 `not_found`，禁止用 cwd/标题/任意旧卡兜底。**排查「新开会话发了在吗 / 刚开的会话只有自己那句 / 终端里有字聊天没有 / 发了消息对话不更新 / This session is no longer in the list」进下文踩坑。
- 助手一次抛出多道选择题时，手机必须按题分组：每道题自己的题干和选项，禁止把多道题的选项摊成一排。未作答且仍是最新动作时才显示；后面已经有新回复或新的工具动作，旧问卷必须从输入条上方拿走。对话在刷新但问卷还钉在底上、两道题合成 8 个按钮，按这条修，不要再当成「消息没推到」。
- **Native agent questions on the phone (2026-09-29 requirement):** Claude, Codex, and OpenCode built-in question requests must reach the same structured phone flow, including every question, its choices, whether multiple choices are allowed, and a free-form answer for each question. Keep the request's native identity and pending state. The user reviews all answers and submits once; selecting a choice must not immediately send its label as an ordinary chat turn or create a misleading user bubble. Route the completed answer to the matching pending native request, reject stale or unavailable requests without sending an unrelated message, and report delivery failure so answers remain editable. Ordinary chat steering remains a separate action. Preserve the current rule that only the latest unanswered request appears above the composer; completed or superseded requests disappear. Implementation and per-runtime verified paths: [Native agent questions: implemented paths](#native-agent-questions-implemented-paths-2026-09-29).

- 开发机跑 `corral remote` 常驻服务；手机只连这台服务，不直接扫各助手历史文件。
- 中继只做路由与代发推送，**看不到**会话明文；推送正文在手机本地用设备私钥解开。
- 手机与桌面共享同一个保活窗格时，**手机端禁止发 `screen.resize`**——否则会把电脑正在看的窗口挤窄。服务端即使收到也会以 `usage_error` 拒绝，**不会**改桌面窗口尺寸（不挂真实 resize 实现）。Mac 终端视图不走 `screen.*`：它用下面「桌面终端原始流」的 `terminal.*`，像 TUI 窗口一样按「宽取最宽、高取最高」参与定尺寸；手机仍永不改尺寸。
- 远程能力的组件（`cryptography` / `websockets` / `segno`）不进主安装包：首次执行会启动服务或配对的命令时，必须自动、幂等地补齐到 **当前 `corral` 命令实际使用的安装副本**。不得误装到系统 Python 后仍报缺依赖；只有网络或软件源不可用时才报清晰失败原因与可重试提示。只读状态查询不得为检查而改动安装环境。

## Native agent questions: implemented paths (2026-09-29)

The pre-fix phone path treated a selected choice as ordinary `input.text`: the label was pasted into the terminal and echoed as a user bubble, so a multi-question form was never finished and the answer looked like a new chat turn. It also offered no typed answer, and a single question's title showed the tool name (`request_user_input` / `AskUserQuestion`) because only multi-question groups kept their prompt text.

**Wire.** `session.prompts` rows keep legacy `id/name/summary/options` and add `request_id` (shared by every question of one request), `question_id`, `prompt`, `header`, `multi_select`, `allow_custom`, `is_secret`, `option_details[{id,label,description}]`, and `custom_needs_choice` (typed text is a note on a chosen option, not a standalone answer — Claude preview questions). Option `id` is the native position (it drives the digit keys). `input.question` takes `{key, request_id, answers:[{question_id, selected:[option_id], text}]}` and returns `{status: delivered|stale|unavailable, detail?}`. Every question needs a choice or text; unknown request/question/option ids are `stale`; nothing ever falls back to `input.text` or emits a chat echo. Per-question metadata lives in `ToolCall.questions_meta` (cached, bump `transcript_cache.PARSER_VERSION` when it changes) and is **not** on the history wire, so tool cards and the Pi migration baseline stay unchanged. Code: `src/corral/remote/questions.py`.

| Runtime | Pending source | Answer path (verified) |
|---|---|---|
| Claude Code 2.1.284 | `AskUserQuestion` tool_use in the transcript. **Claude keeps a pending AskUserQuestion out of the JSONL until it is answered** ([anthropics/claude-code#96795](https://github.com/anthropics/claude-code/issues/96795)); reproduced here — the phone never saw the question. Any PreToolUse hook matching the tool makes it persist within ~2 s, so every hosted/passthrough Claude launch adds `--settings` with a no-op `AskUserQuestion` hook (`ClaudeRuntime.hosted_args`; skipped when the user passes their own `--settings`, not added to `--print` continue plans). Sessions started before this change still hide the question until relaunch. | Keys into the hosted picker (tmux, live-tested): single choice = digit (moves to next tab; a lone question submits); multi choice = digits toggle, `Down`×N reaches "Type something", paste text (auto-checks), `Down` to Next, `Enter`; single + text = digit N+1, paste, `Enter` (choice + text is sent as `label — text`); ≥2 questions or any multi → review tab `1` = Submit. The tool_result arrives as one answer; no user message. **Preview questions (2.1.286, live-tested 2026-10-01):** when any option carries `preview`, Claude switches to the side-by-side picker, which has **no** "Type something" row: a digit only moves the highlight (it does not commit), `n` opens a per-option Notes field, and `Enter` commits the highlighted option with its notes (`annotations.notes` in the tool result). Plan: digit, then `n` + paste when text, then `Enter`. Text without a choice cannot be expressed natively, so the row carries `custom_needs_choice=true`; the phone labels the text as an optional note and requires a choice, and the host rejects text-only answers as `unavailable` (draft kept). Fault seen before the fix: typed text on a preview question submitted the first option and dropped the text. |
| Codex CLI 0.159.2（以本机实测为准，旧 0.158 行仅历史） | `request_user_input_async`：`event_msg` 的 `item_completed` 里 `item.type=AgentMessage`、`item.id=<call_id>`、`item.delivery=async`、`item.questions=[{title, options:null或[string]}]`、`item.phase=final_answer`；随后 `function_call_output` 回 `{"accepted":true}`，**`accepted` 不是已回答，不得据此清卡**。单题原生 id 为 `JSON.stringify(["request_user_input_async", <item id>, <index>])`（见 `tui/src/bottom_pane/async_questions/state.rs`）；30s 倒计时只管展示（`countdown`），v0.159.2 无过期移除代码，超时题目仍可正常回答；活轮结束清卡（`take_question_drafts`），已结束的测试轮不得再答。回答必须走原生 `<send_user_message_question_reply>[{questionItemId,question,answer}]</send_user_message_question_reply>` 信封（`tui/src/async_question_reply.rs`、`context-fragments/src/answered_question.rs`），作为上下文用户片段被接受；禁止发明普通聊天兜底。参考：[`request_user_input_async.rs @ rust-v0.159.2`](https://github.com/openai/codex/blob/rust-v0.159.2/codex-rs/core/src/tools/handlers/request_user_input_async.rs)。同步 `request_user_input`（Plan 模式）仍走旧 TUI 键路径。 | 同步沿用旧键路径；异步走原生信封经托管窗格投递（`questions._answer_async`，v0.24.242 已实现；已终轮/已答复报 `stale`，不得发无关文本）。 |
| Codex 异步结算规则（rust-v0.159.2 源码为准，2026-10-01 核对） | 官方口径（`tui/src/bottom_pane/async_questions/state.rs`、`chatwidget/input_submission.rs`、`chatwidget/protocol.rs`、`chatwidget/turn_runtime.rs`）：等答期间助手继续说话或调无关工具**不关面板**；用户提交正式 prompt（手机 steering 经 composer 粘贴+回车即此类）调 `clear_pending_questions`；回合正常结束/中断/失败走 `take_question_drafts` 清卡；30s 只是未展开面板上的倒计时展示（`countdown`），v0.159.2 没有任何过期移除代码，超时题目仍可经 `resolve_answers` 正常回答；`seen_ids`/`answered_ids` 保证 replay 不复活已答/已跳过的题；另一端已答经 `resolve_answers` 消卡。Corral 侧映射：commentary 与非提问工具不得清卡；用户正文（含原生信封回执）清卡；turn 级原生错误（assistant 卡带 `scope='turn'` 的 error，如 `turn_aborted`）清卡；**正常结束必须经原生 `task_complete`（带 `turn_id`）结算，不得靠终轮正文推测**。TUI 30s 倒计时无移除语义，解析器不做墙钟过期（源码已核对）；迟到但活轮内的回答仍可结算。 | —— |
| OpenCode 2.0.16 | The background service's pending form: `opencode api GET /api/session/<id>/form` (the CLI handles the service auth; raw HTTP gets 401). Only `metadata.kind == "question"` forms whose fields are all `string` / `multiselect`. | `opencode api POST /api/session/<id>/form/<formID>/reply -d '{"answer":{key: value \| [values]}}'`; custom text is accepted when `custom` is not false. Live-tested: TUI shows the answers and the turn continues. Routes: [session.ts @ v2.0.16](https://github.com/anomalyco/opencode/blob/v2.0.16/packages/protocol/src/groups/session.ts); answer schema `Form.Reply` in [form.ts @ v2.0.16](https://github.com/anomalyco/opencode/blob/v2.0.16/packages/schema/src/form.ts). |
| Cursor CLI / Pi / Kimi | Cursor's `AskQuestion` is "Tool not found" in the CLI; Pi has no native question tool; Kimi history is plain text on the phone. | Listed if present, `allow_custom=false`; `input.question` returns `unavailable` without sending anything. |

Key delivery guards: the picker footer (`Enter to select` / `Ready to submit your answers?` for Claude, `to submit answer` / `to submit all` footer for Codex — not "None of the above", which the answered history cell repeats) **and** the first question's prompt or first option must be on screen before any key is sent; success means the picker left the screen within ~4 s. Otherwise the result is `unavailable` and the phone keeps the draft.

**Phone.** One compact form above the composer; each question folds after a single choice and opens the next unanswered one; typed answer per question; one Submit, enabled only when every question is answered. Host `session.prompts` is authoritative once it has answered — message-derived prompts are only the offline/old-host fallback, otherwise every incoming message would reset drafts and flash a multi-question form down to its first question.

**Acceptance status (2026-09-29, v0.24.227).** Claude and OpenCode passed over the encrypted relay: `session.new` → `input.text` prompt → `session.prompts` → `input.question` → `delivered`, prompts empty, assistant continued, no extra user message. Codex passed on the real 0.158 TUI through `questions.answer` (typed-only, choice + note, digits) but not yet over the relay (account quota exhausted). The iOS 1.0.60 form has not been inspected on the phone yet. Restart the user's active remote service after parser changes.

**Confirmed defect (2026-10-01, v0.24.242 rejected):** the 3 async questions of the real fault transcript list correctly after the `accepted` receipt but vanish as soon as an assistant commentary message follows while the turn is still active — the generic later-text supersession rule in `richmsg.pending_prompts_from_messages` treats mid-turn commentary like a sync-blocking answer. Required correction: commentary and unrelated tool activity during the active turn must not settle a running `request_user_input_async`; settlement stays user text (steering / native envelope), turn-level native error, replacement request, or turn end. No-mock parser repro: `/tmp/corral-accept-20261001/accepted.jsonl` → 3 (correct), `/tmp/corral-accept-20261001/continued.jsonl` → 0 (must keep the same 3). Real-phone click acceptance stays OPEN (device screen black/locked at handoff; no simulator, no second driver app).

**Verified correction (2026-10-01, coordinator rejection of the first repair):**
normal completion is NOT indistinguishable from commentary — the rollout carries an
explicit native `task_complete` row with `turn_id` (see
`/tmp/corral-accept-20261001/real-normal-completed.jsonl`: 25 rows ending in
`task_complete`, no error). The first repair regressed it (v0.24.242 → 0 prompts,
repaired source → 3) because SessKit before 0.2.5 dropped that row whenever final
text existed (only the text card survived; bare completions already emitted a typed-only
`lifecycle`). SessKit 0.2.5 now always emits the turn-end `lifecycle`
marker (`text="task_complete"`, native `turn_id`, error when present) — the proven
lifecycle loss, application-neutral, mirroring the existing `turn_aborted` lifecycle;
Corral settles that turn's async tools on it, so an unanswered question from a
completed turn stays settled when a later turn begins. The earlier “known parser gap”
wording is withdrawn for completion/abort. Expiry needs no wall-clock gate: v0.159.2
has no expiry-removal code (countdown display only), so late answers on a live turn
still resolve via `resolve_answers`.

**Live TUI verification (2026-10-01, no quota):** fresh Codex 0.159.2 TUI against a local Responses-API stub (`model_catalog_json` advertises `request_user_input_async`, otherwise core rejects the call as `unsupported call`; `/models` alone is never fetched for custom providers). Receipts: 3 live prompts (`call_live1`) → stub commentary + `exec_command sleep` while the turn stayed active → still 3 → `input.question` path `delivered` (grouped choice + free text in one native envelope) → stub saw the envelope once, turn completed on final text → prompts empty; rollout shows the envelope as exactly one user message, no lone-label echo. Rig: stub+driver+report kept as a reproducible artifact (see acceptance report); isolated rollout files and the temp-dir trust entry were deleted afterwards. The encrypted-relay hop subsequently passed the gate below. Real-phone clicks remain unverified.

**Encrypted RPC gate (2026-10-01, no quota, phone locked):** isolated daemon from the
current tree (own state dir/port 8741, no autostart/switch changes) + temp full-access
probe identity (own key, revoked by deleting the isolated state dir; user pairings
untouched) + fresh Codex 0.159.2 TUI on the local stub. Full answer path over the
configured relay (single channel): `hello` → `pair` →
`sessions.list` (80, isolated session present) → `input.text` starts the test turn →
`session.prompts` 3 → commentary + `exec` tool activity while active → still 3 →
`input.question` `delivered` (grouped choice + free text, one native envelope) →
prompts 0 → normal `task_complete` → repeat `input.question` → `stale` with the
rollout keeping exactly one envelope user message and no lone-label echo →
`input.text` steering starts a new turn and is answered. Stub lessons recorded in
the rig: background title/memory turns must never consume ask rounds (exact-prompt
matching); every tool call needs a unique id and every fco exactly one terminal
response (no sleep chains — they self-perpetuate). Isolated state dir, rollouts,
pane, processes, and trust entries deleted afterwards; user daemon/pairings/sessions
untouched. Rig: `rpc-accept-rig/` next to the acceptance report.

**Independent installed acceptance (2026-10-01):** Corral 0.24.244 loads the
published SessKit 0.2.5 wheel from the actual pipx environment, without a source
override. Replaying the fault records returns 3 questions after acceptance, 3
after continued commentary, and 0 after either abort or normal completion. All
38 question tests execute and pass without skips, including completion followed
by a newer turn and stale submission without terminal injection. The complete
2097-test gate has a matching source/environment stamp. The active user daemon
started after both installed modules were updated and remains relay-connected;
temporary probes did not change the user's six pairings. Physical phone rendering
and taps remain open because the captured screen is black; backend and relay
acceptance do not establish mobile visual acceptance.

**Native reply display defect (2026-10-01, user phone screenshot):** a real custom answer (`other`) reached the agent, but the phone rendered the complete `<send_user_message_question_reply>` JSON envelope as an orange user bubble. Successful native delivery does not establish correct display. Requirement: treat a recognized native answer envelope as question-control content, keep its request settlement, and never expose its wrapper, serialized identifiers, or JSON as an ordinary chat bubble. Preserve the native rollout and ordinary user text, including text co-delivered outside the recognized envelope; do not hide arbitrary user messages merely because they mention the marker. Verify full history, tail open, incremental polling and page loading, plus the original request disappearing after native submission. Root cause: the typed user-message projector treated the native contextual fragment as an ordinary user bubble. Recognition and identity settlement must run on the complete native text before display clipping, and changed projection must invalidate persisted parsed messages. A recognized answer requires the official string identity, question and answer fields; malformed or incomplete lookalikes remain ordinary text. Coordinator verification rejected the first repair: a valid answer beyond the 40,000-character display cap still leaked, and an identity-only object was incorrectly hidden. Corrected runtime/phone evidence remains pending. The authoritative native shape is Codex 0.159.2 [`AnsweredQuestion`](https://github.com/openai/codex/blob/rust-v0.159.2/codex-rs/context-fragments/src/answered_question.rs): a `user.answered_question` contextual fragment with a user role and JSON answer array, distinct from ordinary composer text.

**How to verify without guessing:**

- **Phone-path probe needs full access.** The saved `scripts/phone_remote_acceptance.py` identity is paired read-only (`验收探针`), so `input.*`/`input.question` are rejected. Pair a temporary full probe with its own key file (`corral remote pair --json` → `pair` RPC with that code), drive `session.new` / `input.text` / `session.prompts` / `input.question` / `session.stop`, then `corral remote unpair <id>`. Improvement item: add a question case and a `--key-file` option to that script so this does not need a throwaway harness.
- **Codex picker without quota.** `request_user_input` only exists in Plan mode (`/plan`). Point a TUI at a local Responses-API stub: `codex -c 'model_providers.fake={name="fake",base_url="http://127.0.0.1:<port>/v1",env_key="FAKE_KEY",wire_api="responses"}' -c model_provider=fake -m fake`, where the stub streams `response.created` → `response.output_item.done` (a `function_call` named `request_user_input` with the questions JSON) → `response.completed`, and replies with a text message when the last input item is a `function_call_output`. The real overlay, rollout record and answer output are then exercised. Do not use the shared gateway for this: `openrouter-chat` costs ~125k input tokens per Codex turn and returned no tool-call item.
- **Claude transcript check.** Launch the TUI with and without the `--settings` hook and confirm the pending `tool_use` is (or is not) in the JSONL before answering; the file's own `timestamp` field is the call time, not the write time.

## 命令入口

| 命令 | 作用 |
|---|---|
| `corral login` / `logout` / `whoami` | 公共中继 GitHub 设备码登录（自建单租户不需要） |
| `corral remote on` | 打开常驻服务（后台：局域网 WebSocket + 可选连中继）；记住开关并登记开机自启；已打开则幂等回报。别名 `start` |
| `corral remote off` | 关掉常驻服务，清除开关记忆并撤销开机自启；已关闭则幂等回报。别名 `stop` |
| `corral remote pair` | 打开配对窗口，展示二维码 / `corral://pair?v=2...`（与开关无关） |
| `corral remote status` | 查看服务、开关记忆、开机自启、账号与已配对设备（人读状态为 on/off） |
| `corral remote rename NAME` / `--clear` | 改这台开发机在手机上显示的名字（无参报错；`--clear` 恢复系统默认名）；手机上单独改过名的不受影响 |
| `corral remote rotate-key` | 轮换 Ed25519 注册密钥；路由标识不变，手机不必重扫 |

入口挂在 `bootstrap.py` 的 `remote` 分支，不进 TUI、不碰 Agent 只读接口。

## 协议分层

产品名是 **Corral**。v2 的协议字符串、子协议（`corral.v2`）、请求头（`X-Corral-*`）、HKDF info（`corral/remote/v2 …`）、配对 scheme（`corral://pair`）一律用 `corral`，禁止再引入 `pickup`。旧环境变量、旧推送字段、`/v1` 的 `X-Pickup-*` 只留在存量兼容路径。

1. **中继层（明文）**：`[1 字节版本=2][1 字节类型][16 字节通道][载荷]`，子协议 `corral.v2`。路径 `/v2/host`、`/v2/device?host=<routing_id>`。中继只看版本、类型与通道做转发。`routing_id` 由开发机 X25519 公钥派生。开发机用 Ed25519 签名断言（`X-Corral-Auth`）鉴权，不再用 Bearer token。细则见 [relay/docs/PROTOCOL_V2.md](../../relay/docs/PROTOCOL_V2.md)。
2. **应用层（密文）**：载荷解密后是 JSON：`req` / `res` / `evt`。方法名在 `remote/protocol.py`（`M_*`）与 iOS `WireProtocol` 对齐。HKDF info 为 `corral/remote/v2 …`。

常用方法前缀：

- 只读：`sessions.*` / `session.messages` / `session.toolDetail` / `session.prompts` / `session.userPrompts` / `media.image` / `projects.list` / `runtimes.list` / `search`
- 订阅：`sessions.watch`、`session.watch`、`screen.watch`（事件通道 `sessions` / `session:<key>` / `screen:<key>`）
- 输入：`input.text` / `input.keys` / `input.image`
- 命令回执（可选能力）：`command.status`
- 会话动作：`session.new` / `session.stop` / `session.delete` / `session.markRead` …
- 配对与推送：`pair`、`push.register`
- 桌面客户端（Mac，能力 `desktop_layout`，2026-10-04）：`layout.watch` / `layout.unwatch`（通道 `layout`，快照 `{revision, groups:[{id,name,named,project,members,focus,collapsed,pinned,pinned_at}], pinned_sessions}`；`named=false` 时 `name` 是不展示的内部水果身份名，客户端按成员标题拼显示名，规则见 [终端界面知识库](TERMINAL_UI_KNOWLEDGE_BASE.md)「侧边栏会话组与置顶」）与 `layout.setGroup` / `layout.removeSession` / `layout.setFocus` / `layout.pin` / `layout.pinGroup` / `layout.collapse` / `layout.renameGroup {group_id, name}`（空名 = 恢复自动名）：读写的就是 TUI 的侧栏记忆库（`split_layout.SidebarLayoutDB`），Mac 分屏即 TUI 会话组，TUI 写入经每秒一次的版本号轮询推给 Mac（仅有桌面订阅时运行）。`layout.pin` 用 TUI 语义（组成员钉整组）；手机的 `session.pin` 与列表载荷不变、仍无分组概念。`search.fulltext {q, top}` 复用 TUI Ctrl+F 的对话正文索引，返回命中行与高亮区间（只读，`search` 仍只查标题/路径/最近一句）。
- 桌面终端原始流（Mac，能力 `terminal_stream`，2026-10-05；设计见 iOS 仓 `docs/design/MACOS_CLIENT_DESIGN.md` 的 Terminal view rebuild，实现 `remote/terminal_stream.py`）：`terminal.attach {key, cols, rows}` 订阅通道 `term:<key>`（走数据面），事件 `snapshot {seq, cols, rows, data}` / `output {seq, data}` / `ended {seq}`，`data` 为 base64 原始字节，`seq` 每流连续；客户端见缺口调 `terminal.resync` 拿新快照。字节来自开发机进程对该会话的 tmux 控制通道 `%output`（`embed.ControlChannel.on_data`），快照用同一通道的 `request_ordered` 取「状态 + capture-pane + 状态」并以 `output_seq` 为界拼接：界内输出已在快照里、界外才发，**不丢不重**；两次状态之间有新输出就重取。快照含最近 1500 行历史、可见屏、备用屏（`capture-pane -a` 取被保存的主屏）、光标、滚动区与光标键/小键盘/鼠标/光标形状模式；括号粘贴模式 tmux 不暴露，所以 Mac 粘贴仍走 `input.text submit:false`（tmux `paste-buffer -p` 按程序真实模式加括号）。尺寸：`attach`/`terminal.resize` 把该连接登记进 TUI 共用的 `host-viewers.sqlite3`（`viewer_id` 形如 `remote:<设备>:<连接>`，每秒续票），按「最宽观看方的宽 + 最高观看方的高」`resize-window` 后推新快照；只读设备只看不投票；断线/`terminal.detach` 撤票，最后一个观看方离开时关掉该会话的控制通道。`terminal.input {key, data}` 是原始字节（`send-keys -H`，单次 ≤16KB，限流 `TERMINAL_TYPING`，不走回执）；tmux 已代答程序的终端查询，客户端模拟器自己的应答必须丢弃。终端底色（2026-10-06）：`terminal.attach` 可带 `background` / `foreground`（`#rrggbb`），外观切换时另调 `terminal.theme {key, background, foreground}`；开发机把它们转成 OSC 11/10 应答经 `refresh-client -r`（与 TUI 的 `embed.report_theme` 同一路径，tmux ≥3.5）注入该 pane，并在控制通道重绑后重报，此后 agent 查背景色拿到的是 Mac 的颜色、按深/浅自动选主题。只读设备不报；已在运行且启动时已判定主题的 agent 要等它重查或重启；TUI 与 Mac 同看一格时后报者生效。旧开发机不认 `terminal.theme` 时客户端静默忽略。
- 项目终端（Mac 与 iPhone，能力 `project_shell`，2026-10-07；设计见 iOS 仓 `docs/design/PROJECT_TERMINAL_DESIGN.md`，实现 `remote/shell_terminal.py`）：`shell.list` → `{shells:[{key, project, name, cwd, command, busy, created}]}`（旧→新）；`shell.open {cwd, cols?, rows?}` 在**已存在**的文件夹（空为家目录）起账户登录 shell（`pw_shell -l`），回 `{shell}`；`shell.close {key}` 结束它。shell 托管在保活 tmux server 上、名字前缀 `corralsh-`（不以 `corral-` 开头，所以扫描、托管卡认领、静默回收和压力回收都看不到它），项目路径存在 tmux 会话选项 `@corral_project`，开发机不另存状态文件；远程服务重启、客户端断线都不结束 shell，只有 shell 里 `exit`、`shell.close` 或重启电脑。输出、输入、尺寸、配色复用上一条 `terminal.*`，键为 `shell:<8 位十六进制>`；粘贴同样走 `input.text submit:false`。**尺寸与助手窗格不同**：按「最近活跃的观看方」（最近一次 attach / resize / 打字的连接，tmux 3.1 起的默认 `window-size latest` 语义）定尺寸，该方离开后由剩下最近的一方接管；不进 TUI 的观看登记表。只读配对不能列、开、关、看或打字（带 `shell:` 键的任何调用都按只读拒绝）。`shell:` 键只被 `terminal.*`、`input.text`（粘贴）与 `shell.close` 接受，其余带 `key`/`keys` 的方法（会话、布局、置顶、已读、按键、图片）一律回 `usage_error`，所以 shell 永不进 TUI 布局与会话库（`service._reject_misplaced_shell_keys`）。列 shell 用 `\x1f`/`\x1e` 分隔，文件夹名含制表符或换行也不丢。上限 16 个；文件夹不存在回 `not_found`。TUI 右栏顶栏的「终端」格是 TUI 自己的 `corral-shell-*` 托管会话，与这里无关。
- 终端打字限流：`terminal.input`、`input.keys` 与 `submit: false` 的 `input.text` 走独立限流 `TERMINAL_TYPING`（1200 次/分），真正发送消息（`submit: true`）仍走 `INPUT_ACTIONS`（120 次/分）。协商了回执的连接上，`input.*` 每次都必须带 `command_id`，否则开发机回 `Missing command_id`。只有 `submit: true` 才广播 `echo` 用户气泡；未提交的终端打字不得出现在聊天与提问列表里。

成功返回形状（手机解码依赖这些字段，缺了会空白或静默失败）：

| 方法 | 成功 `d` |
|---|---|
| `input.text` / `input.keys` / `session.stop` / `session.delete` | 默认 `{"ok": true}`。协商了 `command_receipts` 后，`input.text` / `input.keys` 改为回执：`command_id` / `status`（`accepted`\|`dispatching`\|`delivered`\|`rejected`\|`unknown`）/ `host_run_id`，可选 `reason` / `retryable`。未协商时形状不变。 |
| `input.image` | 默认 `{"path": "<开发机落盘绝对路径>"}`。协商回执后额外带同上回执字段（仍含 `path` 当注入成功）。 |
| `command.status` | `{command_id, status, host_run_id, …}`；从未见过该 `command_id` 时 `status` 为 `"unseen"`（只读、可重试）。 |
| `session.new` / `resume` / `handoff` | `{"session": <SessionSummary>}`（含 `key` 等列表字段） |
| `session.markRead` | `{"attention": "none\|unread\|working\|waiting"}` |
| `projects.list` | `{"projects":[{"path","name","cwd","label","count","mtime"}, …]}`（`path`/`name` 给 iOS 新建页；`cwd`/`label` 与桌面项目列表同义） |
| `runtimes.list` | `{"runtimes":[{"id","name","available"}, …]}` |
| `hello` | 含 `paired` / `runtimes`（未配对为空）以及 **`relay_url` / `relay_enabled` / `local_enabled`**（未配对也返回；关中继时 `relay_url` 为空串）。另带稳定进程级 `host_run_id`。`capabilities` **增加** `"planes": ["control", "data"]`、`"command_receipts": true` 与 **`"tool_detail": true`**（历史线只带工具摘要，正文经 `session.toolDetail` 按需取；旧客户端忽略未知字段）。请求带 `"want_data_plane": true` 时额外给一次性 `"data_bind"`；带 `"want_command_receipts": true` 时本连接启用回执路径（`input.*` 须带 `command_id`，可选 `payload_digest` / `lease_sec`）。不带这些字段的旧客户端行为与今天完全一致。数据面第二条 WebSocket 独立握手后 `hello`：`{"plane":"data","bind":"<token>","name":...}`。令牌绑定设备公钥、TTL ≤ 120 秒、一次性；校验失败只关数据通道，不得踢控制面。 |
| `session.messages` / `session.watch` 首包 | `{"messages":[...], "oldest_seq", "newest_seq", "has_more", "generation", …}`。每条消息里的 `tools` **只含摘要**（`id`/`name`/`kind`/`summary`/`status`/`has_detail`；提问可另带 `options`/`questions`/`detail`），**不内嵌**工具 `output` 与普通工具 `detail`。 |
| `media.image` | Capability `"media_image": true`. Params: `key`, `seq` (official message seq), `ref` (exact image reference text from that message: path, `~/…`, relative-to-session-cwd, `file://`, or `http(s)` URL), optional `max_px` (64–4096, default 1080) and `quality` (30–95, default 70). Success: `{"mime", "data" (base64), "width", "height", "source_width", "source_height", "bytes", "source_bytes"}`. Errors: `not_found` (message not loaded, reference not in that message, or file/URL missing), `unavailable` (not an image, too large, or no encoder can fit the budget). Read-only pairings may call it. Details: [Message images](#message-images-and-provisional-working-2026-10-01). |
| `session.toolDetail` | `{"seq", "tools":[<含 detail/output 的完整工具>], "offset", "has_more", "total"}`；找不到消息时带诚实的 `unavailable`。参数：`key`、`seq`，可选 `tool_id` / `offset` / `limit`。 |
| `session.userPrompts` | Capability `"user_prompts": true`（2026-10-06：Your prompts 必须列整场会话，不随客户端已加载的历史窗口变化）。参数 `key`。回包 `{"prompts":[{"seq","role":"user","text","ts"}], "generation", "total"}`，旧→新，只含真人提问（去掉与 TUI 小窗同一套注入过滤），`seq` 与 `session.messages` 同一序号空间，客户端可按序号补页再跳转。主机把规范化消息缓存向前补满（分块读、块间释放读盘锁，结束才落盘一次），所以首调可能较慢、走数据面；单条正文截断（约 500 字，总量超 1 MiB 再缩到 160 字），客户端用已加载的完整正文覆盖。 |
| `sessions.list` / `sessions.watch` | `{"sessions":[...],"version":"<窗指纹>","unchanged":false,"has_more":bool,"total":int}`。请求可带 `since_version`；版本相同则 `unchanged=true` 且**不带** `sessions`。旧手机忽略多余字段仍读 `sessions`。**禁止**把未变回包当成空表覆盖。列表窗口与截断规则见 `docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md` §4.5 |

画面帧字段见 `remote/screen.py` 的 `to_dict()`：`cols/rows/full/lines/cursor/history/status`。`status` 取画面最后一行有内容的文本，供手机对话页做实时状态条（历史文件可能长时间不落盘）。

### Phone input delivery repair (2026-10-03 requirement)

A displayed failure cause is not a successful-send fix. Investigate the actual `Could not deliver input to the session` path with the active host process and hosted pane binding before attributing it to quota. A nonempty stored tmux name does not prove that pane still exists. Never paste into a different session as fallback. Native resume is allowed only for the same confirmed-dead conversation; do not restart a live user session to test delivery. Rejected versus partially delivered outcomes must remain distinct, with actionable host causes and explicit retry. Shared paste-buffer concurrency and stale bindings are investigation hypotheses until reproduced. Acceptance must send through the real remote dispatch into a disposable owned terminal and inspect the received text; no build/unit-test-only success claim. Provider acceptance and native-assistant continuation require separate evidence.

Verified implementation and acceptance (2026-10-04):

- A stale nonempty binding can reject a paste into a missing target. Recovery uses `embed.pane_liveness` (`alive/dead/unknown`): only an authoritative target-not-found response proves death. Timeouts, missing tmux, transport errors and unparseable failures are unknown, never grounds to clear a binding or resume.
- A certain failed injection can recover the same canonical conversation once. Recovery shares the restart lock and re-reads the binding under that lock, so concurrent requests do not launch duplicate processes. A live or unknown target is never restarted; a certain no-effect failure may retry once on the same binding. Native-resume failures retain their own causes.
- `InjectionResult` distinguishes success, certain failure and possible side effects. A set-buffer failure has no terminal side effect; paste-buffer/send-keys timeout or ambiguous failure becomes `PartialInjectionError` and receipt `unknown`, without automatic retry or resume. Text failure after any image path was pasted also stays partial; do not return success for an incomplete image turn.
- Concurrent pastes previously shared one buffer and could overwrite each other's text. Every call now owns a unique buffer, deleted after success or best-effort on failure. It cannot delete another call's buffer. Detailed internal causes map to localized human descriptions; raw codes and tmux stderr are not user-facing. Post-resume failure copy acknowledges that recovery happened.
- Independent acceptance: 264 focused tests pass; the full gate passes 2170 tests with complete coverage. `scripts/remote-send-acceptance.py` runs real service dispatch, hub, receipts and an owned shell pane. It verifies text plus Enter, duplicate command-ID suppression, rejection without injection, and an injected paste-buffer timeout remaining unknown with no retry/resume. Probes are cleaned up. This is terminal-delivery evidence, not native provider continuation or physical-phone acceptance.
- Version 0.24.252 is committed and installed. The old daemon that predated the source changes was replaced, and the new remote service is online. The dispatch acceptance passed again after installation. The historical screenshot's exact transient failure was not captured; a later read-only observation found that user's pane alive and idle. Neither that observation nor earlier quota wording establishes the historical cause.
- Scope: question replies retain no-resume semantics; standalone image input behavior is unchanged. The phone redesign and physical-device limits are owned by the [iOS UI knowledge base](../../ios/docs/UI_DESIGN_KNOWLEDGE_BASE.md#send-failure-feedback-and-session-restart-2026-10-02-requirement).

### Session restart and provisional retirement (2026-10-02)

Owner decisions (phone Restart menu; host implements, primary owns phone UI):

- **`session.restart` = remote access to the existing desktop advanced-restart, not a new feature.** Params `{key}` only; returns `{"session": SessionSummary}`, same shape as `resume`/`handoff`/`copy`. Server reuses the non-UI primitives behind TUI `_restart_hosted_session` / `_restart_and_focus` (authoritative behavior: [终端界面知识库](TERMINAL_UI_KNOWLEDGE_BASE.md) 高级操作): menu selection already counts as confirmation (2026-09-13, **no second confirmation**, so `session.restart` is NOT in the `confirm` gate); preflight the launch plan BEFORE stopping anything; kill the hosted process + close the control channel + forget liveness + wait for death; then native-resume the SAME conversation under the same identity (ident = session id, no copy/handoff/history rewrite). Title, history, project, session identity, and tmux group/layout membership are retained.
- Guards mirror the desktop action: `provisional` placeholder (no resumable history yet), shell panes, dormant (`kimi`) sources, and unknown runtimes are rejected with actionable localized errors; an ended session (no `keepalive_name`) falls back to native resume instead of failing. Readonly pairings get `unauthorized` (`session.restart` is not in `_READONLY_METHODS`; phone hides the menu). Rate limit: `SESSION_CREATE` bucket, same as resume/handoff/copy.
- Capability `hello.capabilities.session_restart=true` is advertised only with this implementation. Old hosts have no `session.restart` method (phone gets `usage_error` unknown-method and must keep the menu hidden); there is no silent fallback.
- **Receipts carry the human cause.** `rejected` / `unknown` receipts include a localized `detail` (the actual host error text) next to the machine `reason` code, plus a `message` alias with the same text for compatibility; `command.status` polling surfaces both. Verified: before this change only `reason` reached the wire while the text stayed in the host observe event. `unknown` stays uncertain (never auto-converted). Never infer a send error from earlier assistant transcript text (e.g. quota wording); only the receipt's own cause counts.
- **Provisional→formal retirement contract (evidence-backed).** Retirement changes the store signature (session key flips, except Pi `--session-id` where title/mtime/keeper fields flip), so the next refresh marks `changed` and the refresh loop pushes a full `sessions` snapshot to list watchers — manual phone refresh is never the repair path. An open conversation on a retired placeholder key keeps working via `resolve_session_key` + key migration, with metadata/live events patching the open detail. Verified by code + focused tests (see below); the 2026-10-02 incident (three stale Codex `Codex · 新会话` cards while the daemon scan already held formal history/titles) is NOT explained by the scan path — fresh `SessionHub.load()` resolves the same formal keys/titles/excerpts — so if the host demonstrably emitted the retirement snapshot and the phone still shows placeholders, the defect is in list subscription/version handling on the phone (primary investigates; no phone code here). Hypothesis, not yet confirmed against the live daemon log.

### Message images and provisional Working (2026-10-01)

Owner decisions; the phone-side presentation lives in the [iOS UI knowledge base](../../ios/docs/UI_DESIGN_KNOWLEDGE_BASE.md#conversation-polish-2026-10-01).

- **Images referenced by a message are previewed through the host, never sent raw.** `media.image` resolves a reference that literally appears in the text of the named message (containment guard: the paired device, including a read-only pairing, cannot use it as a general file reader). Local paths expand `~`, strip `file://`, and resolve relative paths against the session cwd; the target must be a regular image file (extension and decoded content). `http(s)` URLs are fetched by the host with a timeout and byte cap, so localhost URLs work and every preview gets the same compression. Output is downscaled to `max_px` on the long edge, EXIF-oriented, and re-encoded (JPEG; PNG when alpha must survive and fits) under a byte budget. Encoder order: Pillow (`corral[remote]` extra) → macOS `sips` → pass-through only when the original already fits the budget. Encoded previews are kept in a small in-memory LRU keyed by source identity and tier. The phone chooses `max_px`/`quality` from its network path; the host does not guess the phone's network.
- **Claude thinking text is shown as reply text (owner decision 2026-10-01).** Claude Code stores some of the assistant's visible progress notes as non-empty `thinking` blocks and renders them inline in its own window; the phone hid them, so whole notes the user had read in the desktop never reached the phone. For runtime `claude`, a SessKit `thinking` event with non-empty text projects exactly like `assistant_message` text (same card grouping, same clipping); empty/redacted thinking stays hidden. Other runtimes keep thinking hidden (Pi's thinking-only error card depends on it). Bump `transcript_cache.PARSER_VERSION` with this change.
- **Interrupted sessions never receive the recent-activity green fallback (2026-10-05).** Once native history classifies the current turn as aborted and authoritative attention is neither working nor waiting, a retained agent process and a recent history/title write must not imply execution. Unread results retain their red marker. A new authoritative working/waiting signal wins over an older aborted status so resumption remains visible. Marker changes must also publish metadata to open conversation subscriptions, even when the detail page has unsubscribed from the list and process/attention are unchanged. This shared host marker rule applies to the TUI, iPhone and Mac; the Apple chat footer follows that marker. Confirmed symptom: a Claude quota error had an ending record, but a later metadata write refreshed the three-minute recent tier and falsely restored Working. Verification: v0.24.259 passed all 2,233 complete-suite tests (2 existing conditional skips), including detail-only marker delivery, provisional-key migration, expiry, and resumed working. The installed host was checked through a real relay list subscription and one detail for each of the five supported runtimes: the reported Claude quota session returned aborted / attention none / empty marker while a running Codex still returned working. The maintained synthetic TUI capture showed no dot for the interrupted recent host. Apple screen acceptance remains unverified: macOS denied window capture, and the phone was being operated so no stable target-detail capture was obtained. These limits do not establish a successful visual result.
- **Turn-end state reaches every client within the state tick (owner requirement 2026-10-06).** Symptom: after an agent stopped, clients kept showing Working and completion notifications arrived late. Measured on the installed host (0.24.263, memory-pressured 16 GB Mac, swap 6.5/7 GB): nine Claude completion pushes were queued 15–70 s after the native final assistant event. Two causes: (1) `status_tag` / `completion_id` changed only on the full history scan, whose floor moves from 15 s to 60 s under memory pressure, and the shared scan worker backs off similarly; (2) the `recent` tier painted a finished hosted session green — and the Apple footer showed Working — for up to three minutes after its last write. Requirements: for every hot or recently written session (live, hosted, working/waiting), the 1 s state probe re-derives that one session's terminal state from its native history through the SessKit single-session refresh whenever the session's history evidence changed, and publishes status, completion identity and final text in the same tick, independent of memory pressure and the full-scan cadence. A fresher single-session result must not be regressed by an older full scan or shared snapshot. Completion notifications keep the existing `(session_key, completion_id)` dedupe and empty-identity safety gate. A completed current turn (`STATUS_DONE` with a nonempty native completion identity) suppresses the `recent` green fallback exactly as an aborted turn does; fresh authoritative working/waiting still wins, and unread keeps red. The rule lives on the host marker, so the TUI, iPhone and Mac follow it without client changes. This narrows the 2026-10-05 "Working follows the green dot" consequence (three-minute Working after the last activity); it does not change that the footer follows the marker. Implementation: SessKit single-session refresh (0.2.7) plus the store turn probe; the probe calls the hub (`turn_state_listener` → status/marker detection and a list snapshot) immediately, before the rest of the tick. A third cause surfaced in acceptance: Claude Code 2.1.291 writes a `prompt_snapshot` attachment after the final reply, which SessKit ≤0.2.7 read as continuation, so that finished turn had no status and no notification; fixed in SessKit 0.2.8. Verification (0.24.265 source, SessKit 0.2.8, real hosted Claude and Codex panes created through the hub on this memory-pressured Mac, notifications captured in-process with no relay/phone): Claude completion status and notification 1.2–3.7 s after the final reply record (first turn includes new-session arrival; 0.8–1.2 s on later turns); Codex 1.4–2.6 s after the history's last write; exactly one notification per turn; marker at completion was `unread` or none, never `recent`. Not verified here: APNs delivery to the phone and Apple visual acceptance of the footer.
- **Turn start and end are never held behind a scan (owner requirement 2026-10-07).** Symptom: after sending a prompt from the chat composer, Working did not appear immediately, the sidebar green dot appeared about ten seconds later, and Working lingered about ten seconds after the agent stopped. Root cause (measured on the installed host, 0.24.267, memory-pressured): the 1 s state probe replaced the whole attention-evidence cache with only its ~15 hot sessions, so every full refresh re-read all ~890 history tails (5–67 s under memory pressure); the probe ran on the same thread and waited (live daemon stack samples: one ~15 s and two ~2.5 s stalls in about a minute; real hosted Claude turns: working seen 20 s late or never, idle 6–16 s late). Pre-send idle state arriving late then cancelled the client's provisional Working after its 2 s grace. Requirements: (1) a partial probe updates only the evidence it inspected; full refreshes re-read only changed histories; (2) the state probe and publishing run independently of full scans, title polling and reclaim, so no scan duration delays dots, Working or notifications; scanned records must not regress probed attention or turn state when they are swapped in; (3) a successful chat submit (`input.text` with submit) marks the session working on the host immediately and publishes it (green dot and Working on every client, not just the sender), and that working state holds until native evidence shows the new turn (working/waiting evidence, or a new completion or abort), or until a bounded timeout with no history growth since the submit. The terminal UI follows rules (1) and (2) in its own process.
- **List row marker = TUI marker.** `sessions.list` / `sessions.watch` rows carry `marker` (`waiting` / `working` / `unread` / `recent` / empty) from `activity_board.resolve_active_marker`, the function behind the TUI dots and Active sessions; it is part of the list version fingerprint, and the refresh loop pushes a list snapshot when any marker flips (including a `recent` mark expiring with time). The phone must not derive dots from `live`.
- **Provisional Working after phone input.** After `input.text` with `submit` succeeds, the host immediately emits `{"kind":"attention","attention":"working","provisional":true,"live":true}` on `session:<key>` so every watching device shows Working without waiting for the agent's transcript to change. It does not alter the session's authoritative attention, list payload, or push decisions; the authoritative scanner-derived attention event supersedes it. Clients treat a provisional event as a short-lived hint that expires on its own if no confirmation arrives.

## 加密与身份

- 设备与开发机各有一把长期 X25519；配对后手机把开发机公钥存钥匙串，开发机记设备公钥。
- 会话通道用握手派生的双向密钥；推送另有密封盒，中继字段：
  - `corral`：base64 密文
  - `host_id`：明文开发机标识（**仅**用于手机取公钥；不含会话内容）
- 推送 APNs 需 `mutable-content`，以便通知服务扩展改写标题正文。
- **密钥确认**：握手 HELLO 之后，开发机必须等到对端发出第一条可解密密文才 `attach` 并写盘。仅重放公钥不能完成授权。
- 身份与状态落在状态目录（`CORRAL_STATE_DIR` / `XDG_STATE_HOME` / `~/.local/state/corral/remote`），不再放缓存目录；旧缓存路径会一次性迁过去。
- 命令回执落在同一远程状态目录下的 `command_receipts/`（按设备公钥 + `command_id`）；进程重启时 `accepted`→`rejected(interrupted)`，`dispatching`→`unknown`，且不会把 `unknown` 自动改成已送达或已拒绝。

## 安全边界

信任假设与硬约束（2026-08-08 审查后落地；**2026-08-30 对「手机走公网连开发机」整条线复审**，结论见本节后半）：

- **已配对手机 = 开发机上的完整代码执行权**（可向默认跳过权限审批的助手会话粘贴任意文本）。配对码是根凭据：一次性、十分钟过期、恒定时间比对。
- `corral remote unpair` 以磁盘为准；常驻服务会重读清单并在约两秒内踢掉已解绑连接。`touch_device` 不得用进程内陈旧快照整份覆盖把解绑写回去。
- 常驻服务必须**自持**上次加载状态文件的变更令牌再决定要不要重读——模块级全局令牌会被同进程的写盘更新，会导致「刚解绑却仍以为清单没变」。令牌取自纳秒 mtime；写盘后若令牌未前进（远程盘 / virtiofs 同刻写入常见）必须强制 `utime` 推进一步，否则同秒 unpair 会被漏掉。
- `corral remote status` 通过状态目录里的运行快照展示当前在线设备与最近远程操作（服务退出时清除）。
- `input.keys` 只接受 tmux 键名白名单；控制通道参数禁止换行 / 危险控制字节。
- 开发机侧通道数与建通道速率自设上限，不依赖中继记账。
- 局域网与中继均拒绝带 `Origin` 的浏览器跨站 WebSocket。
- 中继地址默认强制 `wss://`；明文 `ws://` 仅 `--insecure-relay`。
- `session.delete` / `session.stop` 必须带 `confirm: true`（手机端确认框之后再发）。
- `corral remote pair --readonly`：只能看会话 / 画面 / 搜索，不能输入或改会话。
- 新建会话的工作目录限定在已知项目或 `cwd_whitelist`。
- `session.new` / `session.resume` / `session.restart`（含接力、分叉）在托管后最多再等 1 秒：助手这期间非零退出就返回 `unavailable`，错误文案带退出码和助手打印的报错行（如缺依赖、参数不认），不登记托管；客户端原样展示。机制见 [内嵌实时终端知识库](EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md) §6「Startup failure must show the reason」。
- 限流错误码 `rate_limited`：配对尝试、输入、新建会话、推送登记、建通道。
- `corral remote rotate-key` 轮换中继 Ed25519 注册密钥；公开路由标识由 X25519 公钥派生，手机不必重扫。

### 2026-08-30：公网连接线复审与落地

审查范围：扫码配对、局域网直连、公网中继、推送密文、手机抢答选路。对照同类零知识中继（中继只转发密文、身份钉在扫码时的开发机公钥上）。**结论：会话正文在公网路上是加密的，中继即使被攻破也读不到对话；真正的风险在「可用性」和「根凭据怎么保管」，不是「中继偷看」。**

仍然成立、禁止为「看起来更安全」拆掉的：

- 手机用扫码得到的开发机长期公钥做握手，不信任中继宣布的身份。只重放公钥不能完成授权，必须等第一条可解密密文。
- 配对码约 80 bit、十分钟、空码拒绝、恒定时间比对；陌生设备只在配对窗口内能完成握手。
- 私钥落盘 0600；手机钥匙串 `AfterFirstUnlockThisDeviceOnly`，锁屏后推送扩展仍能解密封壳，但不进 iCloud。
- 手机系统传输安全只放行局域网明文，公网默认必须加密；开发机侧明文中继要显式 `--insecure-relay`。
- 浏览器跨站 WebSocket（带 Origin）局域网与中继都拒。
- 解绑以磁盘为准并踢连接；只读配对、破坏性操作二次确认、新建会话目录白名单仍有效。

Implemented relay security behavior (2026-08-30). Maintainer deployment history,
host names, and overwrite-install troubleshooting belong in the private
infrastructure runbook; they are not an open-source default deployment:

1. **单租户中继禁止离线换注册钥匙抢走坑位。** 第一次见到某路由标识仍按信任首次使用登记；之后换钥匙必须同时带旧钥匙签名的 `X-Corral-Prev-Auth`（与当前断言同一路由标识、同一时间戳、同一 nonce）。电脑执行 `rotate-key` 会把旧钥匙暂存在 `host.key.prev`，下次连上中继后删掉。丢了注册钥匙：操作者自己上中继删那条登记，不能靠「离线换一把」自动收回。多租户仍走账号登记，不走这条。
2. **配对码必须原子作废。** 读码与删窗口在同一把锁里；同时扫同一张码只允许一台成功。
3. **中继限流看真实来源。** 来自环回/私网反代时读 `X-Forwarded-For` 第一段，其它连接只用对端地址（防伪造）。另加按开发机维度的建连预算，避免反代后「整台共用一桶」被一个人打满。开发机自己的通道上限仍在。
4. **扫码必须明示「配成功等于把这台电脑交给这部手机」。** 电脑端打印二维码时、手机扫码页都要出现这句。只读配对维持原有只读说明。

仍不修、也不要当成漏洞去改的：

- 中继对手机接入仍只先验路由标识（未配对读不到、也控制不了）。大规模对公众开放前再加配对时签发的短时票据；见中继 README「当前尚未覆盖的边界」。
- 局域网口听在全部网卡：给 Tailscale / 同网直连用。有公网地址或端口转发时这个口会暴露到互联网；未配对仍要猜配对码。
- 每次 `corral remote pair` 都会重新打开十分钟配对窗口。终端回滚、SSH 录像、把配对链接发到聊天里，等于把根凭据交出去。`on` 本身不打开配对窗口。
- 中继能看见谁在连谁、每帧多大、何时连——零知识中继的固有元数据。
- 未做中继证书钉扎：证书体系被劫持时对方最多变成另一台中继。
- 已配对手机能向助手粘贴任意文本 = 完整执行权。丢失手机用 `corral remote unpair`。

## 连接策略

产品目标：手机扫码配对一次后，**任意能上网的网络**都应能连开发机（对标 shell-gate）；局域网只是更快路径，不能当唯一通路。

手机侧硬约束（**禁止退回串行死等**）：

1. **并发抢答（智能选路）**：全部局域网提示地址与中继候选**同时**发起，先握手成功者胜出并取消其余。不按蜂窝/Wi‑Fi 粗分——蜂窝上也可能经 VLAN / VPN / 组网直达开发机。
2. **每路独立超时**：局域网约 **2 秒**、中继约 8 秒（与 `ConnectCandidate.timeout` / 性能知识库一致）。死地址尽快让路，但不得把整次连接卡住到系统默认的几十秒。**禁止**把局域网超时收到亚秒（例如 0.4 秒）来「治 Connecting」——第一次系统本地网络权限/路由会来不及，且治不好真正的握手失败。**禁止**用整轮硬超时把「Connecting 挂死」改成「Connection failed」却不完成握手：那只是换文案，用户仍连不上。
3. **重连必须重新选路**：按「这台开发机」重新抢答，禁止复用上次成功的具体地址（否则出门后会死磕家里局域网）。
4. **换网立刻重赛**：路径变化时强制重新并行抢答，不必先空等旧链路探测，也不必等用户切前台。回家后若中继仍通，可在后台再赛一轮局域网并无感升级。
5. **局域网候选是多源、解耦的**：配对 `l=`、运行时 `hello.local_hints`、可选 Bonjour/mDNS（`_corral._tcp`）都只产出 `"ip:port"` 列表，抢答逻辑不耦合发现手段。缺 Bonjour / 禁组播的网络仍靠 `l=` + `hello` 兜底。手机端**不依赖** Multicast Networking 特权（该能力需苹果单独批 provisioning）；有本地网络权限 + `NSBonjourServices` 即可浏览，失败就静默降级。

### Same-machine and LAN connections never drop (owner requirement 2026-10-05)

A client on the host machine itself, or on the same LAN, must not show Connecting / Reconnecting / Connection failed during normal use. Transient path loss must recover before the user can notice. The 2026-10-05 host log showed 99 control-plane reconnects in 30 minutes from one Mac. Independent causes produced them. Each one is now a contract:

1. **Encrypted frames leave in counter order on both ends.** The counter is assigned at encryption time, so encryption and the hand-off to the socket are one ordered step. Client: one serial sender per plane (encrypt + enqueue atomically, a single loop awaits each `send`; a send that does not complete within the plane timeout fails the plane). Apple does not document ordering for concurrent `URLSessionWebSocketTask.send` calls, so the client never relies on it. Host: `HostChannel` hands the frame to its writer inside the same lock that assigned the counter. The writer schedules in FIFO order on the event loop. A relay data channel is pinned to the lane that carried its first frame. If that lane disappears, the channel is closed rather than continued on another lane out of order. Out-of-order frames are rejected as `加密帧顺序异常` and tear the channel down; that is the replay defence and stays strict.
2. **One control plane per client instance, not per device key.** Every request carries a per-process client-instance id (`ci`). The host supersedes an older control connection only when it has the same device key and the same instance id, or either side has no id (legacy clients). Different instances of the same key coexist; the installed Mac app and a Debug build no longer evict each other every 1–2 s. A device key holds at most 4 concurrent control planes; beyond that the oldest is superseded.
3. **Channel-open rate limits are per peer address, and loopback is exempt.** The previous single global bucket (16 opens/min for all LAN peers) was exhausted by one reconnect storm, because each race opens one socket per local hint for both planes. That then rejected the recovery attempts (`remote_local_rate_limited`). Brute-force protection stays on the handshake/pairing path.
4. **A Mac client whose host is the same machine also races loopback** (`127.0.0.1:<port>`). It is added only when one of the host's local hints is an address of this Mac. Loopback survives Wi-Fi IP changes, VPN/overlay churn, and interface sleep.
5. **The first reconnect after a drop is immediate**, and backoff stays short (cap 4 s) while local candidates exist. The old 1 → 2 → 4 → 8 s schedule turned one dropped frame into a 23-second Reconnecting banner.
6. **The host listens before its first scan.** The daemon opens the LAN listener and the relay at once. Requests that need session data wait for the first scan (`SessionHub.wait_ready`, up to 15 s), then return a retryable `unavailable`. Previously the scan ran first and took 39 s on a loaded machine; every client saw Connection failed after each host restart.
7. **A brief drop after a successful connection stays invisible.** The status bar keeps showing connected for 1.5 s (`TransientDropPolicy`); one-shot status syncs (return to foreground, path change) must not override a connected bar, or every app activation flashes Reconnecting. A request issued in that gap waits up to 3 s for the new connection. This only masks the reconnect that items 1–6 already make fast; it is not a fix for Connecting (see the pitfall row "一直 Connecting").

Acceptance: `events.log` shows no `加密帧顺序异常`, `remote_device_superseded`, or `remote_local_rate_limited` while the installed Mac app, a Debug Mac build, and the iPhone stay connected together for at least 10 minutes of normal use. The status bar never leaves the connected state.

关掉中继等于手机只能同网使用，**禁止当默认**（仅本机调试可显式 `--no-relay`）。设置里关掉「局域网优先」时只走中继。

开发机 `pair` 载荷里的 `l`（local hints）与 `r`（relay）都要填对：`local_port` 在状态里为 0 时仍须按默认端口（8737）写 `l=`，禁止因端口字段为 0 整段丢掉局域网地址。若旧配对二维码没有 `r=` / `l=`，手机可在后续 `hello` 里读到 `relay_url` / `relay_enabled` / `local_enabled` / `local_hints`（未配对也返回）并写回本地 Host 记录，无需重新扫码。**开源默认不内置任何共享中继**：新装 `relay_enabled=false`、无 `relay_url`，只靠局域网；换网必须自建中继并写入 `r=` / `--relay-url`（见文首硬规则）。

`corral remote status` 必须能区分「配置了中继地址」与「中继长连接真的在线」——看运行快照里的 `relay_online` / 人读输出的「中继：在线/离线」，不要只看 URL。局域网打开时还应能看到真实 `地址:端口`（不要只显示 “on”）。

## 踩坑

| 现象 | 原因 / 处理 |
|---|---|
| Clients lag the TUI by tens of seconds: ended sessions still show Working, the yellow waiting dot stays after answering, a TUI-started session or its first messages appear late on Mac/iPhone | **Not the network path.** Root cause (2026-10-05, measured): list state (`attention_kind`, `live`, hosted mark, provisional→canonical migration, new sessions) was recomputed only after `store.refresh()` in `SessionHub._refresh_loop`, throttled to a 15 s minimum gap and raised to 60 s by `pressure_cadence` whenever `reclaim.memory_pressure()` was true (true on this 16 GB Mac with ~4.3 GB swap; `events.log` showed a median 60 s scan gap while each scan took 15–25 ms). The shared scan index added its own staleness: the worker's churn backoff reached 60 s and kept republishing old `live`/size facts. Fix (CLI 0.24.257+, refined in 0.24.262): one loop thread runs `SessionStore.refresh_state` every second, never pressure-scaled — it re-stats hot (live/hosted/working/waiting or written within 30 s) Claude/Codex/Pi histories, drops `live` for exited pids, marks a history that grows after a not-live verdict as live, lists managed tmux panes every 2 s to adopt new panes and drop vanished hosted marks, and re-derives attention only for those sessions. Full merges apply the same disk corrections before attention, so a stale index cannot undo them. `HistoryWatcher.arrival_seq` (new `.jsonl` / `store.db`, not subagents) makes the worker parse on its next pass and makes the hub follow index publishes for 20 s (stamp taken before the scan). Measured through the configured relay with a read-only probe client against the restarted 0.24.262 host (23:46): turn end 1.8 s, process exit 2.1 s (Working gone; hosted mark 4.0 s), new hosted pane listed 3.1 s; earlier 0.24.257 runs 1.9–3.1 s / 4.4 s / 5.3–5.9 s. A brand-new session's first-turn Working still waits for canonical discovery: about 5–7 s host-side without foreign writers, 13.5 s over the relay while a pre-0.24.257 TUI was still running. The answered-question (yellow → cleared) transition uses the same history-growth path but was not measured separately. A desktop process still running pre-0.24.257 code can store a forced idle from its stale not-live verdict, delaying that first Working until the next history record; restart old TUI windows. `liveness._list_tmux_sessions` skips sockets whose `/tmp/tmux-<uid>/` file is absent (saves a ~30 ms failing spawn per listing). Regression: `tests/test_remote_state_latency.py` |
| 重启 / 关机后再开，手机连本机立刻 failed，`corral remote status` 为 off | **不是**中继坏了。旧版开关不持久，进程随关机消失且无 LaunchAgent。自 **0.24.173** 起：`on` 写入 `wanted` 并登记开机自启，`off` 才清除。升级后对正在用的机器再执行一次 `corral remote on` 以补登记。Linux 用户单元要在用户登录（或 `loginctl enable-linger`）后才会拉起 |
| 手机开的会话要半分钟才出现在电脑 TUI，且先是对话预览、再过一会才变成可交互画面 | **不是扫描「故意慢」**。远程守护与电脑 TUI 是两套进程；手机 `session.new` 只在守护进程里 `register_hosted_session`，TUI 原先只能等助手把历史写到磁盘再被扫到。Cursor/Codex 往往要等第一句才落盘，空会话可长时间侧栏空白；历史刚出现时 Cursor 正式 id 又常对不上 8 位托管 ident，`annotate` 一时贴不上名，右栏就先走静态预览（像「在别的窗口跑」），下一轮 pid 命中才变可交互。自 **0.24.173** 起：TUI 每轮合并扫描时认领保活 socket 里尚未挂到任何卡片的托管窗格，立刻插入带 `keepalive_name` 的占位卡（与本机新建同一条路径），历史到位后照旧退役。禁止再把「加大刷新」或「让用户退回重进」当成修法。回归：`test_foreign_tmux_host_is_adopted_as_interactive_provisional`、`test_foreign_adopted_provisional_retires_onto_real_history` |
| 刚开的会话发了「在吗」或第一句，自己的气泡有了，助手回复一直不出现；电脑终端里已经有字；长连接还活着 | **不是** 2026-08-30 那套「长连接假在线 / 游标被抢 / 没本地气泡」（CLI **0.24.156 / 0.24.157** 已修）。也不是解析器没字：2026-08-31 本机核对，开发机规范化结果里已经有用户「在吗」和助手「在的，有什么需要帮忙的？」。根因：新会话先以空历史被订阅，正式文件在转正时一次性出现；服务端把新历史整段读进缓存并推进读取位置，却没有把这些句子推到手机仍在听的旧通道；之后的增量轮询从文件末尾开始，永远是空。已有转正回归只断言缓存里有回复、没断言手机通道收到事件，所以会漏。路径从空变成正式文件、键还没变时同样适用。修法：已经在看的订阅切到正式历史时，把手机还没见过的句子当增量推到原来的通道。禁止当成 protobuf 没字去改解析器，禁止先改手机，禁止让用户退回重进。离开会话时才打出的「加密帧顺序异常」不是这条的原因。若再发送提示 `This session is no longer in the list`，才是旧键没解析到正式会话（自 CLI **0.24.154** 起跟随转正） |
| 手机发得出话、对话气泡不出现、看不到助手回复；退出重进又能补上 | **优先查长连接是否已死**：发送只是往窗格里粘贴，气泡要靠 `session:{key}` 实时事件或重进时的尾部窗口。2026-08-30 本机日志：手机 22:46 连上，22:49 数据通道掉且未接回，22:51 助手改成「等你回答」只能发推送，当时在线手机数为 0。根因叠了四层——(1) 已建立的 WebSocket 误用了抢答超时当资源存活上限；(2) 手机不主动探活，socket 没报错就一直显示已连接；(3) 开发机上 `session.prompts` / 增量读历史会把读取游标往前推，新消息进缓存却不推给正在看的订阅；(4) 手机发送没有本地气泡。修法：长连接保活 + 所有推进游标的读取都要推增量 + 发送立刻出气泡。禁止让用户退回重进当修复。刚开的新会话自己那句已经在、助手回复没有、电脑终端却有字——先看上一行，不要再按本行重做保活 |
| 电脑 Corral 里刚开的 Cursor 会话从侧栏消失（常见标题「手机 Corral 测试」） | **不是配对丢了。** 见扫描知识库「Cursor 会话不见了」：正式历史被停并删除后 Corral 扫不到。远程停止/删除只允许针对本次新建的测试会话；禁止把列表里其它正在跑的 Cursor 当成「转正后的新编号」清掉 |
| 换网后对话像重新加载整段历史、重连后聊天闪空 | 旧手机重连会再要一整段尾部窗口。新契约：`session.watch` 带已应用到的序号和历史代次；开发机只补缺口（`resume=replay`，空包不是清空），对不上才给尾部（`resume=tail`）。终端画面仍只留最新帧。契约见 `docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md` §4.3。自 CLI **0.24.150** / iOS **1.0.11** 起（本机与 suzhou 常驻远程已于 2026-08-30 17:05 左右换成该版）；旧客户端不带序号仍走整段尾部。真机换网体感仍须在手机上点一次确认 |
| 蜂窝下要等很久才连上 | 旧客户端串行先试局域网、无超时；不可达局域网会卡到系统默认约 60 秒。必须并发抢答 + 局域网 2 秒超时。重连若仍复用旧局域网地址也会同样慢 |
| 明明同 Wi-Fi，手机却一直显示中继 / 局域网探测无效 | **不是 Wi-Fi 问题。** 旧版 `remote.json` 里 `local_port` 恒为 0（只有 `on --port` 才写），`pair` 因此不写 `l=`，手机 `localHints` 为空，抢答退化成单路中继；`status` 的「LAN direct connect: on」只是开关记忆。另：有透明代理时 UDP 探针会给出 `198.18.0.0/15` fake-ip，旧实现会把它写进二维码。自 **0.24.207** 起：有效端口回落默认 8737、过滤 fake-ip/链路本地、`hello` 带 `local_hints`、可选 mDNS（`zeroconf`）广播。验收：`pair --json` 见 `l=192.168.…:8737`；同网状态条为局域网；出门蜂窝回落中继。旧配对无 `l=` 时连一次让 `hello` 写回即可，不必重扫 |
| 手机一直 Connecting / Connection failed；开发机 `Currently online: 0`；同 Wi-Fi 仍连不上 | **先分清三层，禁止把下一层当修好。** (1) `ios-deliver` 装上/启动成功 ≠ 已连接。(2) 局域网口 `ESTABLISHED`（如 `192.0.2.5→:8737`）只说明 TCP 通了，**不是**握手完成：开发机要等到第一条可解密帧才 `remote_channel_confirmed` / `online≥1`。(3) 本机用 `corral.v2` 打 `ws://<Wi-Fi>:8737` 若几十毫秒内收到 `DEVICE_OPEN`，开发机局域网服务是好的，不要先重启 daemon、不要怪中继。冷启动「回到前台」和会话列表会同时 `connect`：后一次拆前一次，前一次失败的 catch 再拆新连接，界面变成 Failed——必须单飞，过期一轮不得动当前 client；通道已握上后 `hello` 失败也不得打成 Failed。**验收（缺一不可）**：状态条离开 Connecting（局域网或中继文案）、`corral remote status` 当前在线 ≥ 1。禁止拿单测、装包、TCP、或「超时变成 Failed」交差。**【根因已定 · 2026-09-13，iOS 1.0.49 构建 59 + CLI 0.24.210 起】** 此前五轮补丁（Bonjour 抢先、按蜂窝关局域网、0.4s 超时、整轮 10s 超时、path 闸门、重叠 connect）全部无效，因为根因不在超时而在**连接泄漏**，两端各一半：(a) 手机端多路握手用 TaskGroup 等赢家，但 `URLSessionWebSocketTask.receive` 不响应 Swift Task 取消，group 退出又要等所有子任务结束——只要候选里有一条连不通的地址（ZeroTier `10.10.10.x`、旧 DHCP 地址），已握手成功的赢家会被拖到系统 TCP 超时（几十秒），界面一直 Connecting；输家 socket 在这期间不关，重连、过期一轮、`adopt` 直接覆盖旧 socket 又各漏一条。(b) 开发机把「WebSocket 握完 HELLO」当成一条通道，永不确认的僵尸也占名额（每主机 8 条），占满后真连接被拒——日志形态是连串 `remote_device_attached` / `remote_data_bind_issued` 后成簇迟到的 `remote_device_detached`。**修法（禁止再回到超时补丁）**：手机端超时/取消**立刻** `cancel(with:)` 关 socket（`HandshakeSocketLifecycle` 一次性状态机：open→adopted|closed，adopt 与 close 用锁互斥）；仲裁只等第一个赢家（`HandshakeArbiter`），输家立刻取消，迟到赢家与过期一轮的赢家一律 `Win.close()`；`adopt` 先拆旧 socket；`AppModel` 过期代次握手成功也 `disconnect()`。开发机端：握手后 **20 秒**（`UNCONFIRMED_TTL`）没收到可解密帧就关通道并给中继发 `DEVICE_CLOSE`（`remote_channel_unconfirmed_expired`）；同一客户端进程的新控制面确认后旧控制面被取代下线（`remote_device_superseded`；自 0.24.257 起按设备公钥 + 进程标识 `ci` 判定，同机多份 App 共存，见「Same-machine and LAN connections never drop」）。**验收实录**：开发机重启远程服务后 1 秒内手机 `remote_channel_confirmed` + 数据面确认，`Currently online: 1`，`lsof :8737` 仅 2 条来自手机（控制面 + 数据面），40 秒无重连。排查时先看这两个数字，再看 events.log 有没有 `unconfirmed_expired` / `superseded`。 |
| 本机以为中继「TLS/证书坏了」其实域名根本不存在 | For a user-configured relay, verify its hostname against authoritative DNS before diagnosing TLS. A local proxy's synthetic address does not prove the hostname exists. Relay configuration follows the open-source relay rules above; no maintainer server is a bundled default. |
| 手机一开终端，电脑窗口变窄 | 某处发了 `screen.resize`；手机端必须删掉这条调用；服务端应拒绝而非执行 |
| New Session shows a generic host error and an empty project picker | Host logs show `remote_response_send_failed` for `projects.list`: `_LocalizedLabel` cannot be JSON serialized. The no-directory bucket contains a lazy desktop label. Resolve labels to strings in `SessionHub.projects`, preserving desktop language switching. Regression must build the actual mixed known/unknown project catalog and serialize it through `protocol.dumps`, rather than mocking already-plain labels. Restart the active remote process after updating it. |
| 新建会话页项目列表空白 | `projects.list` 缺 `path`/`name`（旧版只有 `cwd`/`label`）；两端需同时认两套字段 |
| 发送失败但输入框已清空 | 客户端在 `try?` 后无条件清空草稿；应仅在成功时清空并展示服务端错误文案 |
| Mac terminal Restart returns to the ended prompt without starting the assistant | **2026-10-05 recovery requirement:** a stored hosted name is not proof of a running pane. `session.resume` must check tri-state pane liveness: reuse a live binding, explicitly reject an unknown result without spawning, and clear a certainly dead binding before native resume of the same conversation. Serialize resume and restart/recovery per canonical session key; nested recovery must not deadlock. Preserve history, title and layout. Observed evidence: the Mac cached this Claude conversation as hosted but not live; an independent scan found no live/hosted process. The old resume branch returned success without invoking the runtime or launcher whenever the old name remained. Mac uses `session.resume`; iPhone Restart uses `session.restart`, and sending to ended history uses the same resume/recovery backend. TUI recovery uses its own native-launch path and already clears stale bindings before native launch (`_restart_session_from_pane`); its ended-session regression cases remain in the full gate. Focused verification: 7 tests cover dead/live/unknown binding, start failure, concurrent resume and nested input recovery. A unique real tmux socket exercises Mac `session.resume` → `terminal.attach` → `terminal.input` and verifies streamed plus captured command output; the same dispatch fixture verifies phone dead-binding send/submission and explicit `session.restart` replacing its PID. The active host was also queried through its encrypted relay before replacement and independently returned hosted=true/live=false for the affected conversation. Delivery acceptance on CLI 0.24.261: the real affected conversation was resumed through the encrypted relay, retained its key, returned a 2548-byte terminal snapshot and independently scanned as live/hosted; a second resume reused the live binding. Complete gate: 2262 tests, coverage complete, two first-pass flakes passed isolated retry; clean install and all 12 real-tmux selftests passed. Physical iPhone clicks and the original Mac click remain unverified: the console was locked (`CGSSessionScreenIsLocked=Yes`) and window capture returned no image. Backend/stream verification is not a claim of physical-click acceptance. |
| 手机往已结束会话发消息红感叹号 / 回执 `unavailable` / 「快点动手实现」发不出 | 会话不在保活窗格里（`keepalive_name` 空），旧逻辑直接拒绝注入。自本修复起：`input.text` / `input.keys` / `input.image` 在注入前会先走原生恢复再粘贴（对齐电脑「回车重开」）。若仍失败：看回执 `reason`、该会话是否真能 resume、以及常驻远程是否已换新版。**不要**只当成中继超时 |
| 发给已结束会话的消息停在助手输入框里不提交 / 回执是已送达但助手没反应 / 唤醒后 Enter 被吞 | 2026-10-04 Mac 客户端验收实测（Codex，已结束会话）：`input.text` 先原生恢复再立刻粘贴 + 回车，助手还在启动（横幅、工具加载），回车被丢，正文留在输入框，回执仍是成功；之后在已就绪窗格补一次回车即正常回复。`send_turn` 的「等就绪」只认 Claude/Cursor 的 `→` 提示符，不能直接复用到 Codex。修法：仅在本次确实是唤醒（发送前无 `keepalive_name`）时，先等窗格画面连续约 1 秒不变（启动转圈会持续改画面；最多等 20 秒，超时照常注入、不报错），再粘贴回车；已在跑的会话不走这段、不加延迟。验证：单测覆盖唤醒必等、在跑不等、静止判定与超时不抛错；真实验收用 Mac/手机给一条已结束的一次性会话发消息，确认助手直接回复 |
| 手机详情顶栏显示 Ended / 已结束，但对话还在刷、电脑侧栏是执行中 | 常见不是 Cursor 判活假阴性。开发机 `corral list` 已是 `live=true` 时，根因是手机顶栏死守列表缓存的 `live`，进详情后列表 watch 常被卸掉，attention 事件又不带 live。修法：顶栏读打开中对话的 live；attention 事件带 `live`；live 翻转给已打开详情推 metadata。电脑「子代理跑、主会话已结束」另查扫描知识库 |
| Pi 会话有对话却看不到 Agent activity / 工具调用 | 旧远程把 Pi 挂在纯文本解析上，`supports_tool_calls` 也不含 pi。现已按活动分支解析 `toolCall` / `toolResult`；须抬高规范化缓存版本并 `corral remote off && on`。手机端活动卡本身不用改 |
| 置顶接口永远回未置顶 / 组内会话点置顶无效 | `session.pin` 必须读 `pinned_session_keys`（不是已废弃的 `pinned_sessions`）；组成员不能单独置顶，应改切 `pinned_group_ids`（与桌面侧栏一致）。列表载荷里组字段用 `group.id`（值取自 `SplitGroup.group_id`） |
| 手机删掉组内一条后，另一条仍挂着幽灵分组 | `session.delete` 成功后必须 `layout_db.remove_session`，不足两成员时解散组 |
| 会话列表整页空白 | 手机 `SessionSummary` 对 `id`/`short_id` 按 String、数值按 Double 解码；`session_payload` 必须先做类型收口，任一字段类型不符会让整份 `sessions.list` 解码失败（客户端 `catch` 后静默空白） |
| 推送仍是占位文案 | NSE 解不开：缺 `host_id`、钥匙串 access group 两边不一致、或主 App 未把开发机公钥写入共享组 |
| Swift 里写了 `$(AppIdentifierPrefix)...` 却永远对不上钥匙串 | 宏只在 entitlements 展开；源码必须写死 `TEAMID.com.x0c.corral` |
| 只装主包没装 `[remote]` | `corral remote` 导入失败；提示用户装可选依赖 |
| 还按旧习惯以为 `corral remote start` 会占住终端 / 会打二维码 | 服务已是开关：`on` 后台打开并立刻返回，二维码只走 `pair`。`start`/`stop` 只是别名。要用前台调试加 `--foreground` |
| 执行 `corral remote on` / `pair` 提示缺 `cryptography` / `websockets` / `segno`，或只显示手动配对码没有终端二维码 | 当前实际运行的 Corral 安装副本没有远程组件；打开服务或配对命令必须自动补齐。pipx 是隔离环境且默认不含 pip，必须走 `pipx inject corral …`，不能把包装到系统 Python；若自动补齐失败，才提示检查网络或软件源后重试 |
| 单租户中继上执行 `corral login` | 登录并不适用；客户端必须立即说明该中继无需账号并继续可用，不得向不存在的设备码入口发请求后抛 404。主域名若返回 404，说明公共多租户尚未部署；若要启用，必须先在服务器配置数据库、会话密钥和 GitHub OAuth 应用，禁止把单租户实例伪装成已隔离的公共服务 |
| 守护进程还是旧名 `pickup`（改名前起的），想换新名重启 | Restart with `corral remote off` then `corral remote on`. Identity and phone pairings remain in `~/.local/state/corral/remote/`; retain the configured relay URL. Maintainer-specific domain and account-routing operations belong in the private infrastructure runbook. |
| 新版 CLI 守护进程连中继报 `HTTP 404`（events.log `remote_relay_disconnected`） | Check whether the configured relay supports `/v2/host`; an older `/v1`-only binary needs an upgrade through that deployment's guide. Maintainer-specific hosts and service-unit names belong in the private infrastructure runbook. |
| 手机 App 突然连不上、守护进程状态一切正常 | 手机 App 已升 v2 协议（`/v2/device`，路由 id 由主机 X25519 公钥派生），而守护进程还是旧版只登记 v1（旧路由 id 是十六进制老格式）。把守护进程升到当前版本即恢复；配对按设备公钥绑定，手机无需重扫。端到端验证用 `corral remote pair --readonly --json` 拿 `--code` 交给 `relay/scripts/device_probe.py`（探针钥匙若重新生成过，旧配对作废须重新配对） |
| 空状态目录里 `corral remote` 测试或首次启动卡住 | `load_state` 持锁时会再进 `load_or_create_identity` / `host_key`；`config._lock` 必须是 `RLock`，改回普通 `Lock` 会在没有 `identity.key` 时死锁 |
| 事件只到一个界面 | 客户端事件流做成了单消费者；必须按通道多播 |
| 手机会话列表或打开历史极慢 / 转圈后「开发机响应超时」 | 不是单纯中继慢。2026-08-29 经公网中继、按手机同款请求实测：`sessions.watch` 整表 535 条约 13.5s（已接近手机 20s 超时），随后打开约 87MB 历史的 `session.watch` 在 20s 内无回包，中继因心跳未应答被掐断。根因是常驻进程把扫描/解析与心跳放在同一把解释器锁上，且把几百条闲置会话整表塞进首包。开发机必须：列表首包只带当前页（等待/置顶优先、闲置截断）、解析大文件时让出锁、心跳超时宽于一次冷解析、回包失败要回错误而不是默默断连接。详见 `docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md` §4.5。截断之后若**每次进列表仍先转圈**，是手机没落下该机上次窗口：必须先画出快照，刷新带版本号，未变不重传、也不得把未变回包当成空表。**禁止**只加大手机超时、只靠压缩、用滚动分页冒充首屏优化、或用 `sessions.list --limit 5` / 本机 unittest 冒充已验收 |
| 打开大历史第一次仍像卡死 / 详情把那条设备通道堵住 | 缓存未命中时禁止从 JSONL 文件头读到尾。第一次打开只从末尾向前取完整行，解析足够填满当前消息窗口的记录；工具配对不完整允许再向前一块。向前翻页从窗口左缘再补一块，不要为翻一页读完整文件。左侧还有未读字节时 `has_more` 必须为真。Cursor 按 rowid 取尾部，不要扫全表。文件变长仍从上次偏移增量读。解析/IO 失败降级为未命中或本轮无新消息，不得炸通道。改了读取语义必须抬规范化缓存版本并重启常驻服务。权威设计见 `docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md` §4.2 |
| 打开大历史时输入/心跳被堵住 | 历史页和终端帧必须走第二条数据面连接；控制面继续承载输入、短 RPC、列表事件、对话实时事件和心跳。旧手机不声明 `want_data_plane` 时仍单连接。数据面队列满只丢过时帧或拒绝新的历史页，禁止因此踢掉控制面。错误的 data_bind 只关数据通道。 |
| Cursor 用户气泡里出现整段系统上下文 | 远程富消息必须走与本地扫描器相同的 `user_query` 提取；不能在手机端用固定字符串过滤。未重启常驻服务时仍会发出旧解析结果 |
| Pi（或任意新助手）会话在手机上是空聊天，电脑预览却有对话 | 远程富消息有独立解析表，不会回落到桌面扫描器。Pi 曾完全未登记，打开详情只能拿到空窗口。补登记后必须抬高规范化缓存版本并重启常驻远程服务，否则会继续命中「空结果」缓存 |
| Codex 详情第一句是系统说明 / 打开像空白 | 首轮 `response_item` 常把 `# AGENTS.md instructions`、环境块写成 user；桌面扫描器会丢掉，旧远程解析会整段当人话。中断标记 `<turn_aborted>`、`<subagent_notification>`、`<user_action>` 同理。手机时间线若再抄桌面小窗黑名单，还会把「对本仓库做 code review」这种真人可见提问滤成空白。服务端丢掉高置信系统包装，真人提问必须留下 |
| Claude 详情多出一条「到点了」系统通知 | 到点任务通知挂在 user 轮次下，桌面预览按 `origin.kind` 丢掉，远程必须同样丢掉，不能当成用户气泡 |
| 电脑预览正常、手机某个助手仍是旧内容或空聊天 | **本机和开发机是两套常驻进程**。源码改了不等于手机已换新解析。2026-08-29 真机：开发机远程进程从 8 月 25 日起一直没重启，Pi/Codex 修复写进源码后手机仍走旧进程。修完必须对**用户正在连的那台**执行 `corral remote off && corral remote on`，并抬高规范化缓存版本 |
| 只抽了一条 Codex 就说「详情修好了」 | 六个助手历史格式不同，问题不会碰巧相同。验收必须每个助手各打开一条有最后一句的真实会话：首句不能是系统说明，列表有最后一句则详情不能空。`phone_remote_acceptance.py` 按助手抽样，禁止只验体积最大的那一条 |
| 同一连接第二次 `session.watch` 历史为空 | 连接级订阅已存在时 `_subscribe` 返回 false，旧实现直接回空列表；应走 `conversation_page`（与 `screen.watch`→`resync_screen` 同理），且不增加中枢订阅计数 |
| 聊天状态条与终端页叠订后第二帧空白 | 同连接重复 `screen.watch` 必须 `resync_screen`，不能只加订阅 |
| 手机上两台开发机点进去会话一模一样 | 不是身份撞车、也不是两台电脑共用历史。手机切机时先改「当前选中」再拿选中项判断要不要换连接，会继续拉上一台的列表。必须按「真正连着哪一台」决定重连。排查入口：`apple/docs/troubleshooting/2026-08-29-two-hosts-same-sessions.md` |

## 验证

New-session catalog repair acceptance (2026-09-30): after restarting the active host, an encrypted-relay client received 109 projects including the no-directory entry (0.875 s), the 80-session subscription window (0.929 s), one nonempty history from each of Claude, Codex, OpenCode, Kimi, Cursor and Pi (0.079–0.699 s), and a successful new-session response (0.153 s). Its empty synthetic session was stopped with `confirm=true`, and the temporary probe was unpaired. The real iPhone Max form also showed the restored project picker and opened a newly created Claude chat with the keyboard. The complete CLI gate passed 2063 tests with no first-pass failures. This check did not inject a network failure or test a network switch.

宣称「手机列表/详情已可用」时，必须同时给出：常驻服务启动时间、手机或探针走的是中继还是直连、以及下面这条**与手机同款**的路径。只跑编译、只跑 unittest、只跑 `sessions.list --limit 5` 都不算完成。

```bash
# 协议与加密单测（含在全量 ci-test）
env -u TEXTUAL_DISABLE_KITTY_KEY python3 scripts/ci-test.py

# Relay acceptance, only when a user-configured relay is enabled.
corral remote status   # Verify the configured relay is online.
corral remote pair --readonly --json   # v=2; r= is required for this relay case.
# Authenticate only when the configured relay requires an account.
# Full subscription + one detail per active runtime + timeout + idle heartbeat.
python3 scripts/phone_remote_acceptance.py \
  --relay wss://relay.example.com \
  --key <hello/pair 输出的公钥> \
  --code <只读配对码>
# 必须打用户正在连的那台开发机（本机和开发机公钥不同）。只抽一条 Codex 不算过。
# 2026-08-30 17:11: development host 0.24.150, through its configured relay:
# 整表首包 0.52s / 80 条；Cursor、Codex、Pi、OpenCode、Claude 详情均在 2s 内有正文；
# 双连接竞速与空闲 25s 心跳通过。首包窗口里没有 Kimi 样本，不能据此说 Kimi 详情已验。
# 可选：叠加蜂窝近似（额外往返 + 带宽上限）
#   --rtt-ms 80 --bytes-per-sec 50000
```

`relay/scripts/device_probe.py` 默认只拉 5 条摘要、不打开详情，只能证明「中继握手通了」，不能证明列表和详情能在手机超时前回来。

换网验收（对标 shell-gate，缺一不可）：

1. 开发机中继在线；手机用蜂窝或不在开发机局域网仍能连上并列出会话。
2. 同局域网时优先直连（可加速），失败须自动回落中继，**不要**要求用户再扫一张「外网码」。
3. 无手机时用 `cli/scripts/phone_remote_acceptance.py` 经当前配置的中继跑完整表订阅，并**每个助手各打开一条详情**；`device_probe.py` 只作握手对照。

<!-- 该文档整理/压缩于 2026-09-29 -->

## Rebuilt conversation sequences (2026-10-07)

A native-reader rebuild may assign different normalized sequence numbers than incremental projection (for example Codex tool hosts grouped differently across poll batches). These numbers are scoped to one projection generation. Rebuilt cards must replace the prior generation, never merge into its sequence slots. Otherwise newer user prompts overwrite earlier rows while an old assistant tail remains at larger sequence numbers: the reader appears to swallow prompts or move new prompts above old replies. Original history remains intact.

The reader signals a replacement when a previously assigned sequence changes message identity or disappears. SessionHub atomically replaces canonical history, increments generation, clears replay deltas and sends a bounded history-reset snapshot to active clients. Native clients apply it as a tail snapshot and preserve only their unconfirmed local echoes across generations. New uncached transcripts use a non-repeating generation seed so a host restart or parser-cache invalidation cannot be mistaken for the old generation. Status-only tool updates stay deltas. Invalidate old derived transcript caches containing mixed generations. TUI's direct history projection does not use this remote merge path.

Reproduction uses a temporary copy of the affected Codex history (original unchanged): incremental projection yielded 62 cards, rematerialization yielded 47, and overlapping sequence slots changed identity. The replacement signal fired and retained all 11 human prompts. Focused host regressions and 349 portable native tests pass. This confirms a code defect independently of which GUI build supplied the original screenshot.

Live-host acceptance: installed the committed 0.24.269 wheel and restarted the host. Reading the affected session over an existing paired, read-only encrypted local channel returned 55 cards instead of the old 60, preserved all 11 human prompts, and confirmed unique increasing sequences and chronological user timestamps. The new generation was distinct from the old host generation. Mac 0.3.39 (43) was inspected in the real conversation with all 11 prompts visible; iPhone 1.0.130 (140) installation was verified, but its chat screen was not navigated and AppShelf upload failed independently.
