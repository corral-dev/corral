# 手机端远程接力（corral remote）

覆盖「手机连开发机看会话 / 输入 / 推送 / 配对 / 局域网直连 / **换网不可用与中继** / **开源默认中继 / 不要暴露维护者服务器 / 别人要用自己搭中继**」。

配套客户端：`../ios/`（见 `../ios/AGENTS.md`）。零知识中继：`../relay/`（开源自建看其 README）。维护者本人的多租户公网实例运维只写在私有 agentsync 基础设施知识库，**禁止**写进公开 GitHub 门面当默认地址。

## Task execution reliability

- Execution state must agree across the host, mobile list, and open conversation. Opening or reconnecting during a running turn must immediately restore the current state; reading a conversation must not clear working. Process existence alone does not prove an active turn.


Phone-submitted tasks must continue through normal Agent execution; delivery acknowledgement is not task completion. Automatic idle and capacity cleanup are disabled by default. Manual termination remains available. Automatic cleanup must never terminate an executing Agent. Input sent through the phone and output from detached terminals count as activity. If execution is interrupted or cannot resume, preserve the original task and surface failure rather than implying completion. Never blindly replay a delivered task because it may already have changed user data.

## 开源中继硬规则（2026-09-12 用户裁定 · 记牢）

1. **开源产品不提供、不暗示共用维护者的多租户中继。** 陌生人 clone / brew / 装 App 之后，默认只能走**局域网直连**；要换网/蜂窝可达，必须**自己部署** `corral-relay`（或显式配置自己的 `wss://`），再 `corral remote on --relay-url …` / 配对载荷里的 `r=`。
2. **禁止**在公开 GitHub 源码、README、安装说明、示例命令、iOS 内置常量里写入维护者个人中继域名，也禁止写成「默认公共中继」「开箱即用的共享中继」。
3. **禁止**把「维护者自己在用的多租户服务」当成开源用户的默认依赖——多租户账号登录（`corral login`）只服务**你自己部署的多租户中继**；单租户自建不需要 login。
4. 维护者本机已写入 `remote.json` 的中继地址可以继续用；那是**本机状态**，不是开源默认。新装 / 空状态：`relay_enabled=false`、`relay_url=""`，只开局域网。
5. 代码里若需识别「某个 URL 要走多租户 GitHub 登录」，只允许读环境变量允许名单（如 `CORRAL_PUBLIC_RELAY_URLS`），**默认名单为空**——不要把私人域名写死进仓库。

## 产品边界

- 开发机远程服务是**开关**：`corral remote on` 打开、`corral remote off` 关掉，都幂等。打开后进程在**后台**常驻，命令立刻返回；不要把 `on` 当成前台守护进程，也不要因为命令马上结束就以为服务没起来——用 `corral remote status` 看是否 on。
- **开关必须记住**：`on` 把「想要开着」写入状态，并登记开机/登录自启（macOS LaunchAgent `com.x0c.corral.remote`，Linux `systemd --user` 的 `corral-remote.service`）；`off` 清掉记忆并撤销自启。重启、重新登录或进程崩溃后，只要上次是开，就必须自动回来——禁止要求用户每次开机再敲一次 `on`。排查「重启后手机立刻 failed / 本机远程 Status: off」先看开关记忆与自启是否在，不要先怪中继。
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
- **会话结束后的手机系统通知（2026-09-15）**：用户离开前台后，一轮正常结束或异常结束（额度用尽、限流、供应商报错等）都应发系统通知。触发与文案口径**基于 SessKit 的会话结束状态**（`status_tag` 已完成 / 已中断 + `last_agent_msg`；合同见 `~/Codes/SessKit/docs/CONTRACT.md`「Status tags and abnormal endings」，Corral 钉 SessKit ≥0.1.5）。开发机 `PushNotifier` 在 `SessionHub` 扫到标签跃迁时经中继加密投递；`kind` 为 `completed` / `aborted`（另保留既有 `waiting`）。禁止只看进程退出、侧栏关注圆点、或裸 `DONE`/`已完成`（假完成会把额度挂掉当成干完了）。成功与异常文案分开；异常带可读报错摘要。同一会话同类提醒两分钟节流。**短会话漏推（2026-09-15）**：服务刚启动或两次扫描之间，新会话首次出现就已是已完成/已中断时，启动基线本来不推——若历史 `mtime` 在约 5 分钟内仍须推（`v0.24.216+`）；更旧的存量终端态保持静默。扫描侧细则见 `SESSION_SCANNING_KNOWLEDGE_BASE.md` §6。
- 新开会话会先用一张临时卡片，助手落下第一句真实历史后再换成正式编号。电脑侧栏会跟着换；**手机仍停在旧编号的详情页时，必须继续能发、能收**。「能收」包括转正那一刻已经写进正式历史的第一句用户话和助手回复，不能只保证转正之后再增量出来的句子。禁止只换读取位置、把这些句子吃进缓存却不推到手机原来的通道。禁止把旧编号当成「已经不在列表里」——那不是配对错了，是编号转正后远程没跟上。排查「新开会话发了在吗 / 刚开的会话只有自己那句 / 终端里有字聊天没有 / 发了消息对话不更新 / This session is no longer in the list」进下文踩坑。
- 助手一次抛出多道选择题时，手机必须按题分组：每道题自己的题干和选项，禁止把多道题的选项摊成一排。未作答且仍是最新动作时才显示；后面已经有新回复或新的工具动作，旧问卷必须从输入条上方拿走。对话在刷新但问卷还钉在底上、两道题合成 8 个按钮，按这条修，不要再当成「消息没推到」。
- **Native agent questions on the phone (2026-09-29 requirement):** Claude, Codex, and OpenCode built-in question requests must reach the same structured phone flow, including every question, its choices, whether multiple choices are allowed, and a free-form answer for each question. Keep the request's native identity and pending state. The user reviews all answers and submits once; selecting a choice must not immediately send its label as an ordinary chat turn or create a misleading user bubble. Route the completed answer to the matching pending native request, reject stale or unavailable requests without sending an unrelated message, and report delivery failure so answers remain editable. Ordinary chat steering remains a separate action. Preserve the current rule that only the latest unanswered request appears above the composer; completed or superseded requests disappear. Implementation and per-runtime verified paths: [Native agent questions: implemented paths](#native-agent-questions-implemented-paths-2026-09-29).

- 开发机跑 `corral remote` 常驻服务；手机只连这台服务，不直接扫各助手历史文件。
- 中继只做路由与代发推送，**看不到**会话明文；推送正文在手机本地用设备私钥解开。
- 手机与桌面共享同一个保活窗格时，**手机端禁止发 `screen.resize`**——否则会把电脑正在看的窗口挤窄。服务端即使收到也会以 `usage_error` 拒绝，**不会**改桌面窗口尺寸（不挂真实 resize 实现）。
- 远程能力的组件（`cryptography` / `websockets` / `segno`）不进主安装包：首次执行会启动服务或配对的命令时，必须自动、幂等地补齐到 **当前 `corral` 命令实际使用的安装副本**。不得误装到系统 Python 后仍报缺依赖；只有网络或软件源不可用时才报清晰失败原因与可重试提示。只读状态查询不得为检查而改动安装环境。

## Native agent questions: implemented paths (2026-09-29)

The pre-fix phone path treated a selected choice as ordinary `input.text`: the label was pasted into the terminal and echoed as a user bubble, so a multi-question form was never finished and the answer looked like a new chat turn. It also offered no typed answer, and a single question's title showed the tool name (`request_user_input` / `AskUserQuestion`) because only multi-question groups kept their prompt text.

**Wire.** `session.prompts` rows keep legacy `id/name/summary/options` and add `request_id` (shared by every question of one request), `question_id`, `prompt`, `header`, `multi_select`, `allow_custom`, `is_secret`, `option_details[{id,label,description}]`. Option `id` is the native position (it drives the digit keys). `input.question` takes `{key, request_id, answers:[{question_id, selected:[option_id], text}]}` and returns `{status: delivered|stale|unavailable, detail?}`. Every question needs a choice or text; unknown request/question/option ids are `stale`; nothing ever falls back to `input.text` or emits a chat echo. Per-question metadata lives in `ToolCall.questions_meta` (cached, bump `transcript_cache.PARSER_VERSION` when it changes) and is **not** on the history wire, so tool cards and the Pi migration baseline stay unchanged. Code: `src/corral/remote/questions.py`.

| Runtime | Pending source | Answer path (verified) |
|---|---|---|
| Claude Code 2.1.284 | `AskUserQuestion` tool_use in the transcript. **Claude keeps a pending AskUserQuestion out of the JSONL until it is answered** ([anthropics/claude-code#96795](https://github.com/anthropics/claude-code/issues/96795)); reproduced here — the phone never saw the question. Any PreToolUse hook matching the tool makes it persist within ~2 s, so every hosted/passthrough Claude launch adds `--settings` with a no-op `AskUserQuestion` hook (`ClaudeRuntime.hosted_args`; skipped when the user passes their own `--settings`, not added to `--print` continue plans). Sessions started before this change still hide the question until relaunch. | Keys into the hosted picker (tmux, live-tested): single choice = digit (moves to next tab; a lone question submits); multi choice = digits toggle, `Down`×N reaches "Type something", paste text (auto-checks), `Down` to Next, `Enter`; single + text = digit N+1, paste, `Enter` (choice + text is sent as `label — text`); ≥2 questions or any multi → review tab `1` = Submit. The tool_result arrives as one answer; no user message. |
| Codex CLI 0.158 | `request_user_input` (Plan mode) function_call in the rollout. | Keys, from the version's `tui/src/bottom_pane/request_user_input` source: digit commits and advances/submits; typed-only = `Down`×N to "None of the above" (always present: core sets `is_other`), `Enter` opens notes, paste, `Enter`; choice + note = `Down`×k, `Tab`, paste, `Enter` (native `user_note`). Codex has no multi-select. `request_user_input_async` (non-blocking panel; replies are user messages and the panel clears at turn end) is not answered from the phone. |
| OpenCode 2.0.16 | The background service's pending form: `opencode api GET /api/session/<id>/form` (the CLI handles the service auth; raw HTTP gets 401). Only `metadata.kind == "question"` forms whose fields are all `string` / `multiselect`. | `opencode api POST /api/session/<id>/form/<formID>/reply -d '{"answer":{key: value \| [values]}}'`; custom text is accepted when `custom` is not false. Live-tested: TUI shows the answers and the turn continues. Routes: [session.ts @ v2.0.16](https://github.com/anomalyco/opencode/blob/v2.0.16/packages/protocol/src/groups/session.ts); answer schema `Form.Reply` in [form.ts @ v2.0.16](https://github.com/anomalyco/opencode/blob/v2.0.16/packages/schema/src/form.ts). |
| Cursor CLI / Pi / Kimi | Cursor's `AskQuestion` is "Tool not found" in the CLI; Pi has no native question tool; Kimi history is plain text on the phone. | Listed if present, `allow_custom=false`; `input.question` returns `unavailable` without sending anything. |

Key delivery guards: the picker footer (`Enter to select` / `Ready to submit your answers?` for Claude, `to submit answer` / `to submit all` footer for Codex — not "None of the above", which the answered history cell repeats) **and** the first question's prompt or first option must be on screen before any key is sent; success means the picker left the screen within ~4 s. Otherwise the result is `unavailable` and the phone keeps the draft.

**Phone.** One compact form above the composer; each question folds after a single choice and opens the next unanswered one; typed answer per question; one Submit, enabled only when every question is answered. Host `session.prompts` is authoritative once it has answered — message-derived prompts are only the offline/old-host fallback, otherwise every incoming message would reset drafts and flash a multi-question form down to its first question.

Acceptance still requires a real pending question per runtime answered from the phone over the encrypted path. Restart the user's active remote service after parser changes.

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

- 只读：`sessions.*` / `session.messages` / `session.toolDetail` / `session.prompts` / `projects.list` / `runtimes.list` / `search`
- 订阅：`sessions.watch`、`session.watch`、`screen.watch`（事件通道 `sessions` / `session:<key>` / `screen:<key>`）
- 输入：`input.text` / `input.keys` / `input.image`
- 命令回执（可选能力）：`command.status`
- 会话动作：`session.new` / `session.stop` / `session.delete` / `session.markRead` …
- 配对与推送：`pair`、`push.register`

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
| `session.toolDetail` | `{"seq", "tools":[<含 detail/output 的完整工具>], "offset", "has_more", "total"}`；找不到消息时带诚实的 `unavailable`。参数：`key`、`seq`，可选 `tool_id` / `offset` / `limit`。 |
| `sessions.list` / `sessions.watch` | `{"sessions":[...],"version":"<窗指纹>","unchanged":false,"has_more":bool,"total":int}`。请求可带 `since_version`；版本相同则 `unchanged=true` 且**不带** `sessions`。旧手机忽略多余字段仍读 `sessions`。**禁止**把未变回包当成空表覆盖。列表窗口与截断规则见 `docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md` §4.5 |

画面帧字段见 `remote/screen.py` 的 `to_dict()`：`cols/rows/full/lines/cursor/history/status`。`status` 取画面最后一行有内容的文本，供手机对话页做实时状态条（历史文件可能长时间不落盘）。

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

已落地（2026-08-30 当晚；不要退回去）。个人公网中继已于当日 18:33 换上含第 1、3 条的二进制（覆盖安装与「误把 Mac 程序拷上去服务起不来」见基础设施知识库 `corral-relay.caozc.top` 节）：

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

关掉中继等于手机只能同网使用，**禁止当默认**（仅本机调试可显式 `--no-relay`）。设置里关掉「局域网优先」时只走中继。

开发机 `pair` 载荷里的 `l`（local hints）与 `r`（relay）都要填对：`local_port` 在状态里为 0 时仍须按默认端口（8737）写 `l=`，禁止因端口字段为 0 整段丢掉局域网地址。若旧配对二维码没有 `r=` / `l=`，手机可在后续 `hello` 里读到 `relay_url` / `relay_enabled` / `local_enabled` / `local_hints`（未配对也返回）并写回本地 Host 记录，无需重新扫码。**开源默认不内置任何共享中继**：新装 `relay_enabled=false`、无 `relay_url`，只靠局域网；换网必须自建中继并写入 `r=` / `--relay-url`（见文首硬规则）。

`corral remote status` 必须能区分「配置了中继地址」与「中继长连接真的在线」——看运行快照里的 `relay_online` / 人读输出的「中继：在线/离线」，不要只看 URL。局域网打开时还应能看到真实 `地址:端口`（不要只显示 “on”）。

## 踩坑

| 现象 | 原因 / 处理 |
|---|---|
| 重启 / 关机后再开，手机连本机立刻 failed，`corral remote status` 为 off | **不是**中继坏了。旧版开关不持久，进程随关机消失且无 LaunchAgent。自 **0.24.173** 起：`on` 写入 `wanted` 并登记开机自启，`off` 才清除。升级后对正在用的机器再执行一次 `corral remote on` 以补登记。Linux 用户单元要在用户登录（或 `loginctl enable-linger`）后才会拉起 |
| 手机开的会话要半分钟才出现在电脑 TUI，且先是对话预览、再过一会才变成可交互画面 | **不是扫描「故意慢」**。远程守护与电脑 TUI 是两套进程；手机 `session.new` 只在守护进程里 `register_hosted_session`，TUI 原先只能等助手把历史写到磁盘再被扫到。Cursor/Codex 往往要等第一句才落盘，空会话可长时间侧栏空白；历史刚出现时 Cursor 正式 id 又常对不上 8 位托管 ident，`annotate` 一时贴不上名，右栏就先走静态预览（像「在别的窗口跑」），下一轮 pid 命中才变可交互。自 **0.24.173** 起：TUI 每轮合并扫描时认领保活 socket 里尚未挂到任何卡片的托管窗格，立刻插入带 `keepalive_name` 的占位卡（与本机新建同一条路径），历史到位后照旧退役。禁止再把「加大刷新」或「让用户退回重进」当成修法。回归：`test_foreign_tmux_host_is_adopted_as_interactive_provisional`、`test_foreign_adopted_provisional_retires_onto_real_history` |
| 刚开的会话发了「在吗」或第一句，自己的气泡有了，助手回复一直不出现；电脑终端里已经有字；长连接还活着 | **不是** 2026-08-30 那套「长连接假在线 / 游标被抢 / 没本地气泡」（CLI **0.24.156 / 0.24.157** 已修）。也不是解析器没字：2026-08-31 本机核对，开发机规范化结果里已经有用户「在吗」和助手「在的，有什么需要帮忙的？」。根因：新会话先以空历史被订阅，正式文件在转正时一次性出现；服务端把新历史整段读进缓存并推进读取位置，却没有把这些句子推到手机仍在听的旧通道；之后的增量轮询从文件末尾开始，永远是空。已有转正回归只断言缓存里有回复、没断言手机通道收到事件，所以会漏。路径从空变成正式文件、键还没变时同样适用。修法：已经在看的订阅切到正式历史时，把手机还没见过的句子当增量推到原来的通道。禁止当成 protobuf 没字去改解析器，禁止先改手机，禁止让用户退回重进。离开会话时才打出的「加密帧顺序异常」不是这条的原因。若再发送提示 `This session is no longer in the list`，才是旧键没解析到正式会话（自 CLI **0.24.154** 起跟随转正） |
| 手机发得出话、对话气泡不出现、看不到助手回复；退出重进又能补上 | **优先查长连接是否已死**：发送只是往窗格里粘贴，气泡要靠 `session:{key}` 实时事件或重进时的尾部窗口。2026-08-30 本机日志：手机 22:46 连上，22:49 数据通道掉且未接回，22:51 助手改成「等你回答」只能发推送，当时在线手机数为 0。根因叠了四层——(1) 已建立的 WebSocket 误用了抢答超时当资源存活上限；(2) 手机不主动探活，socket 没报错就一直显示已连接；(3) 开发机上 `session.prompts` / 增量读历史会把读取游标往前推，新消息进缓存却不推给正在看的订阅；(4) 手机发送没有本地气泡。修法：长连接保活 + 所有推进游标的读取都要推增量 + 发送立刻出气泡。禁止让用户退回重进当修复。刚开的新会话自己那句已经在、助手回复没有、电脑终端却有字——先看上一行，不要再按本行重做保活 |
| 电脑 Corral 里刚开的 Cursor 会话从侧栏消失（常见标题「手机 Corral 测试」） | **不是配对丢了。** 见扫描知识库「Cursor 会话不见了」：正式历史被停并删除后 Corral 扫不到。远程停止/删除只允许针对本次新建的测试会话；禁止把列表里其它正在跑的 Cursor 当成「转正后的新编号」清掉 |
| 换网后对话像重新加载整段历史、重连后聊天闪空 | 旧手机重连会再要一整段尾部窗口。新契约：`session.watch` 带已应用到的序号和历史代次；开发机只补缺口（`resume=replay`，空包不是清空），对不上才给尾部（`resume=tail`）。终端画面仍只留最新帧。契约见 `docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md` §4.3。自 CLI **0.24.150** / iOS **1.0.11** 起（本机与 suzhou 常驻远程已于 2026-08-30 17:05 左右换成该版）；旧客户端不带序号仍走整段尾部。真机换网体感仍须在手机上点一次确认 |
| 蜂窝下要等很久才连上 | 旧客户端串行先试局域网、无超时；不可达局域网会卡到系统默认约 60 秒。必须并发抢答 + 局域网 2 秒超时。重连若仍复用旧局域网地址也会同样慢 |
| 明明同 Wi-Fi，手机却一直显示中继 / 局域网探测无效 | **不是 Wi-Fi 问题。** 旧版 `remote.json` 里 `local_port` 恒为 0（只有 `on --port` 才写），`pair` 因此不写 `l=`，手机 `localHints` 为空，抢答退化成单路中继；`status` 的「LAN direct connect: on」只是开关记忆。另：有透明代理时 UDP 探针会给出 `198.18.0.0/15` fake-ip，旧实现会把它写进二维码。自 **0.24.207** 起：有效端口回落默认 8737、过滤 fake-ip/链路本地、`hello` 带 `local_hints`、可选 mDNS（`zeroconf`）广播。验收：`pair --json` 见 `l=192.168.…:8737`；同网状态条为局域网；出门蜂窝回落中继。旧配对无 `l=` 时连一次让 `hello` 写回即可，不必重扫 |
| 手机一直 Connecting / Connection failed；开发机 `Currently online: 0`；同 Wi-Fi 仍连不上 | **先分清三层，禁止把下一层当修好。** (1) `ios-deliver` 装上/启动成功 ≠ 已连接。(2) 局域网口 `ESTABLISHED`（如 `192.168.110.5→:8737`）只说明 TCP 通了，**不是**握手完成：开发机要等到第一条可解密帧才 `remote_channel_confirmed` / `online≥1`。(3) 本机用 `corral.v2` 打 `ws://<Wi-Fi>:8737` 若几十毫秒内收到 `DEVICE_OPEN`，开发机局域网服务是好的，不要先重启 daemon、不要怪中继。冷启动「回到前台」和会话列表会同时 `connect`：后一次拆前一次，前一次失败的 catch 再拆新连接，界面变成 Failed——必须单飞，过期一轮不得动当前 client；通道已握上后 `hello` 失败也不得打成 Failed。**验收（缺一不可）**：状态条离开 Connecting（局域网或中继文案）、`corral remote status` 当前在线 ≥ 1。禁止拿单测、装包、TCP、或「超时变成 Failed」交差。**【根因已定 · 2026-09-13，iOS 1.0.49 构建 59 + CLI 0.24.210 起】** 此前五轮补丁（Bonjour 抢先、按蜂窝关局域网、0.4s 超时、整轮 10s 超时、path 闸门、重叠 connect）全部无效，因为根因不在超时而在**连接泄漏**，两端各一半：(a) 手机端多路握手用 TaskGroup 等赢家，但 `URLSessionWebSocketTask.receive` 不响应 Swift Task 取消，group 退出又要等所有子任务结束——只要候选里有一条连不通的地址（ZeroTier `10.10.10.x`、旧 DHCP 地址），已握手成功的赢家会被拖到系统 TCP 超时（几十秒），界面一直 Connecting；输家 socket 在这期间不关，重连、过期一轮、`adopt` 直接覆盖旧 socket 又各漏一条。(b) 开发机把「WebSocket 握完 HELLO」当成一条通道，永不确认的僵尸也占名额（每主机 8 条），占满后真连接被拒——日志形态是连串 `remote_device_attached` / `remote_data_bind_issued` 后成簇迟到的 `remote_device_detached`。**修法（禁止再回到超时补丁）**：手机端超时/取消**立刻** `cancel(with:)` 关 socket（`HandshakeSocketLifecycle` 一次性状态机：open→adopted|closed，adopt 与 close 用锁互斥）；仲裁只等第一个赢家（`HandshakeArbiter`），输家立刻取消，迟到赢家与过期一轮的赢家一律 `Win.close()`；`adopt` 先拆旧 socket；`AppModel` 过期代次握手成功也 `disconnect()`。开发机端：握手后 **20 秒**（`UNCONFIRMED_TTL`）没收到可解密帧就关通道并给中继发 `DEVICE_CLOSE`（`remote_channel_unconfirmed_expired`）；同一手机新控制面确认后旧控制面被取代下线（`remote_device_superseded`，一台设备只留一条控制面）。**验收实录**：开发机重启远程服务后 1 秒内手机 `remote_channel_confirmed` + 数据面确认，`Currently online: 1`，`lsof :8737` 仅 2 条来自手机（控制面 + 数据面），40 秒无重连。排查时先看这两个数字，再看 events.log 有没有 `unconfirmed_expired` / `superseded`。 |
| 本机以为中继「TLS/证书坏了」其实域名根本不存在 | 旧默认 `relay.corral.sh` 是 **NXDOMAIN**。家里 OpenClash fake-ip 会给未解析域名塞 `198.18.x`，本地 `dig`/`curl` 看起来像「连上了再 TLS 挂」。判真伪：用 DoH（或非本机网络）查权威解析；正确默认是 `wss://corral-relay.caozc.top` |
| 手机一开终端，电脑窗口变窄 | 某处发了 `screen.resize`；手机端必须删掉这条调用；服务端应拒绝而非执行 |
| 新建会话页项目列表空白 | `projects.list` 缺 `path`/`name`（旧版只有 `cwd`/`label`）；两端需同时认两套字段 |
| 发送失败但输入框已清空 | 客户端在 `try?` 后无条件清空草稿；应仅在成功时清空并展示服务端错误文案 |
| 手机往已结束会话发消息红感叹号 / 回执 `unavailable` / 「快点动手实现」发不出 | 会话不在保活窗格里（`keepalive_name` 空），旧逻辑直接拒绝注入。自本修复起：`input.text` / `input.keys` / `input.image` 在注入前会先走原生恢复再粘贴（对齐电脑「回车重开」）。若仍失败：看回执 `reason`、该会话是否真能 resume、以及常驻远程是否已换新版。**不要**只当成中继超时 |
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
| 守护进程还是旧名 `pickup`（改名前起的），想换新名重启 | `corral remote off` 再 `corral remote on` 即可，`identity.key` 与手机配对都在 `~/.local/state/corral/remote/`，不会丢。**relay_url 保持在别名 `wss://pickup-relay.caozc.top`**：`is_public_relay()` 只认主域名，改成主域名反而会被要求先 `corral login`（多租户账号路径）；别名与主域名是同一服务（2026-08-25 实操验证） |
| 新版 CLI 守护进程连中继报 `HTTP 404`（events.log `remote_relay_disconnected`） | 首尔中继二进制落后（只有 `/v1`，新版 CLI 走 `/v2/host`）。按 agentsync 基础设施知识库 `corral-relay.caozc.top` 节升级中继，升级后 v1/v2 并存；注意实际单元名是 `pickup-relay.service`，不是文档早年写的 `corral-relay.service`（2026-08-25 已升级） |
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
| 手机上两台开发机点进去会话一模一样 | 不是身份撞车、也不是两台电脑共用历史。手机切机时先改「当前选中」再拿选中项判断要不要换连接，会继续拉上一台的列表。必须按「真正连着哪一台」决定重连。排查入口：`ios/docs/troubleshooting/2026-08-29-two-hosts-same-sessions.md` |

## 验证

宣称「手机列表/详情已可用」时，必须同时给出：常驻服务启动时间、手机或探针走的是中继还是直连、以及下面这条**与手机同款**的路径。只跑编译、只跑 unittest、只跑 `sessions.list --limit 5` 都不算完成。

```bash
# 协议与加密单测（含在全量 ci-test）
env -u TEXTUAL_DISABLE_KITTY_KEY python3 scripts/ci-test.py

# 本机冒烟（另开终端）
corral remote status   # 须见公网中继在线，勿长期 --no-relay
corral remote pair --readonly --json   # 二维码须为 v=2 且含 r= 中继；仅有 l= 则换网必挂
# 公共中继先 corral login
# 按手机真实请求走公网中继：整表订阅 + 每个助手抽一条详情 + 20s 超时 + 空闲后再心跳
python3 scripts/phone_remote_acceptance.py \
  --relay wss://pickup-relay.caozc.top \
  --key <hello/pair 输出的公钥> \
  --code <只读配对码>
# 必须打用户正在连的那台开发机（本机和开发机公钥不同）。只抽一条 Codex 不算过。
# 2026-08-30 17:11 本机 `dev`（0.24.150 常驻已重启）经 `wss://pickup-relay.caozc.top`：
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

<!-- 该文档整理/压缩于 2026-09-05 -->
