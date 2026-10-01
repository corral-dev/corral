# Agent 完成通知（手机系统通知）——设计与实施计划

> 状态：**待评审设计**（2026-09-19）。本轮只做设计与调研，不改产品代码。
> 适用入口：规划或实现「会话结束系统通知 / 干完了推送 / 异常结束也要通知」、
> 设置页通知开关、触发口径（SessKit）、推送投递与验收时，先读本文。
> 验收硬门槛沿用 `REMOTE_KNOWLEDGE_BASE.md`：走中继上的整表订阅 +
> **每个助手一条详情**，禁止用 5 条摘要、单条 Codex 或本机 unittest 冒充。

## 0. 一句话结论

- **要改 SessKit，但只补“结束身份”**：让每个 `SessionInfo` 带一个
  `completion_id`（同一会话里每一轮结束唯一、重启可重算），并收紧
  Cursor 的已完成判定。通知开关、去重、投递、重试仍在 Corral。
- **触发 = 现状轮询 + 去重键升级，不新增常驻监听**：`SessionHub`
  已有 `status_tag` 跃迁检测与推送链路；缺的是“同一会话连续两轮完成”
  与“重启后重放”两种情况下的去重。本设计把去重键从
  `(session_key, kind)` 升级为 `(session_key, completion_id)`。
- **设置开关 = 手机本地偏好 + 上报开发机偏好，两处缺一不可**：
  手机设置页管“这台手机收不收”；`push.register` 新增 `notify_completed`
  / `notify_aborted` 管“开发机给这台设备发不发”。

## 1. 现状（已核源码，不是推测）

### 1.1 链路已经通了

- 开发机 `SessionHub._detect_status_changes`
  （`cli/src/corral/remote/sessions.py`）在每次刷新后检测
  SessKit `status_tag` 跃迁为 `STATUS_DONE` / `STATUS_ABORTED`，
  经 `PushNotifier.on_status_change`（`cli/src/corral/remote/push.py`）
  加密后请中继代发 APNs（`RelayClient.send_push` →
  `relay/internal/hub` `forwardPush` → `internal/push`）。
- 手机 `NotificationService`（`ios/CorralNSE/NotificationService.swift`）
  用 `host_id` 取钥匙串里的开发机公钥，本地解密后改写标题正文；
  `userInfo["session_key"]` 已透出，点击通知可按会话键打开详情；
  `CORRAL_WAITING` 分类已有快捷回复（`PushRegistrar`）。
- `waiting`（等你回答）推送与上述同一条路，已有单测
  （`cli/tests/test_remote_push.py`）。

### 1.2 已确认的缺口

1. **设置页没有完成通知开关**：`HostSettingsView` 的通知区只镜像
   系统授权状态（开 / 去系统设置 / 去开启），没有“工作完成通知”
   独立开关。`push.register` 只上报 `token` + `env`，没有偏好字段。
2. **去重键太粗**：`PushNotifier._last_sent` 按
   `(session_key, kind)` + 120 秒节流。同一会话 120 秒内完成两轮，
   第二轮被吞；重启后 `_last_sent` 与 `_last_status` 都从快照重建，
   语义是“启动基线不推”，不是持久去重。
3. **Cursor 的已完成不可信**：SessKit `parsers/cursor.py`
   `_build_session_info` 里“有标题或有用户话就 DONE”，会把“刚开了个头”
   的会话标成已完成。Codex / Pi 的异常结束已在 SessKit ≥0.1.5 修好
   （`CONTRACT.md` “Verified gaps closed”），Cursor 仍是弱项。
4. **`session_payload` 没带判定证据**：推送正文只带了 160 字 `last_agent`，
   没有 `file_mtime` / `size_bytes` / `event_time`，手机与日志都无法
   事后核对“推的是哪一轮”。
5. **通知分类只有等待回复有**：完成 / 中断通知当前走默认分类，
   没有独立分类标识，后续想给完成通知加“查看 / 标已读”动作时要补。

### 1.3 不需要做的事（已排除）

- 不引入各助手官方钩子（Claude hooks / Codex `notify` / Cursor hooks /
  Pi 扩展事件）作为**唯一触发**：它们形态各异、版本差异大，且
  Pi `agent_settled` 后仍可自动重试（结束事件 ≠ 整轮结束）。
  官方钩子只作为可选的“早唤醒”（见 §4 可选增强），触发口径仍以
  SessKit 快照为准。
- 不监听进程退出：常驻 TUI 不退出时进程一直在；短命打印模式退出
  也不代表一轮结束。`REMOTE_KNOWLEDGE_BASE.md` 已明令禁止。
- 不改 TUI 侧栏关注圆点语义：`live` / `status_tag` / 关注态三套状态
  各管各的（`SESSION_SCANNING_KNOWLEDGE_BASE.md` §6），通知只消费
  `status_tag` + 去重键。

## 2. 触发口径：SessKit 只补 `completion_id` + 收紧 Cursor

### 2.1 SessKit 侧（小改，跨仓）

新增 `SessionInfo` 可选字段 `completion_id: str`（`total=False`，
不进 `required`，旧 Corral 忽略未知字段仍可读）：

- 定义：同一会话里“一轮结束”的稳定标识。**2026-09-30 洪水修正：
  禁止再用 `file_mtime_ns:size_bytes` 做身份成分**——元数据追加/触碰会改
  mtime 与 size，导致同一真实结束不断换 id、同一会话连推几十条。
  身份只能来自稳定的原生结束/轮次/消息证据（终端记录标识、轮次/消息 id、
  原生 stop/finish 标记、终局文本哈希），与文件/DB 聚合 mtime、大小、
  标题、队列元数据无关；元数据变更不得改变未变结束的 id；
  不同真实轮次的相同回答文本仍须不同 id（轮次/消息锚点不同）；
  拿不到精确终局锚点时返回空 id（unknown），不得伪造成功。
  （Codex 取 `task_complete`/`turn_aborted` 那条记录的稳定字段，
  `user`/`agent` 文本锚点同样携带原生事件 id（`response_item` 必有，
  旧 `event_msg` 缺失即空）——同文本不同真实轮次不得同 id；
  Pi 取分支叶子 `parentId` 链尾 + `stopReason`；
  Cursor 取 `updatedAtMs + prompt_history` 首条摘要；
  其余沿用“尾部事件时间 + 尾文本哈希”，但事件时间必须是原生记录时间，
  禁止用文件 mtime。）
- 要求：同一轮重复扫描必须算出同一个值；新一轮结束必须变；
  进程重启后对同一份历史重算仍得同一个值（持久去重不靠内存）。
- **窗口稳定的身份输入（2026-10-01 验收驳回，SessKit ≥0.2.4）**：
  滚动扫描窗口内用户/列表摘要“有没有”变化，不得改变同一原生终局事件的
  id——锚点与参与哈希的尾料都必须是终局锚点事件自身的确定性函数，
  不能取自窗口摘要。已验证失败（SessKit 0.2.3 实装复现）：
  同一终局事件前后两次都是 DONE，64KB 尾部丢了用户摘要后 id 变化，
  同一真实完成连推两条。验收缺口更新（2026-10-01 当日）：
  SessKit 0.2.4 已把 Claude 身份尾料改成锚点终局事件自身正文
  （窗口稳定；展示摘要回填不变），Corral 0.24.243 / SessKit 0.2.4
  已安装到两台开发机，升级缓存及窗口边界回归均通过。
  Corral 侧不做补偿、不改扫描，只按 `(session_key, completion_id)`
  去重语义消费。
- **Provider upgrade baseline (2026-10-01 acceptance correction)**: a new
  SessKit completion-identity contract must invalidate older derived session
  metadata and shared scanner snapshots before the remote service establishes
  its baseline. Preserve native histories and per-device notification ledgers.
  An upgrade or restart must not emit historical completions, and continued
  metadata appends must not re-notify the same terminal event. The coordinator
  reproduced SessKit 0.2.4 returning an old completion id from Corral's warm
  metadata cache for an unchanged native file; fresh native parsing returned the
  new id. Shared-worker upgrade behavior was verified in-tree this same day (see
  implementation note below). A dependency pin alone does not establish that the
  running consumer uses the new contract.
- **Provider upgrade implementation (2026-10-01, shipped in 0.24.243)**:
  `cache.provider_cohort()` qualifies every
  cached row, shared snapshot (`provider_cohort` field, mismatch ⇒ local scan),
  and worker heartbeat (missing/mismatch ⇒ `is_active()` false, no reuse);
  cold-flush purge keeps current-cohort rows (bare or host-tagged) and drops
  old-contract rows; the next provider handoff advances all five pins to 0.2.5. Per-device
  ledgers and preferences unchanged; no compensating push logic.
- **Independent acceptance (2026-10-01)**: the coordinator seeded the actual
  previous cache contract (bare parser version plus host tag), then scanned an
  unchanged native Claude history through SessKit 0.2.4 and the real Corral
  cache. The stale row was rejected before baseline creation. Appending native
  metadata past the 64 KB boundary added zero captured sends; a fresh hub with
  reloaded ledgers added zero; a distinct native final event with identical text
  added exactly one. These checks used the real hub and notifier with an
  isolated sender, never a phone. Against the still-running old scanner, the new
  consumer rejected both its live heartbeat and shared snapshot. Post-install
  relay and physical notification acceptance remain separate evidence.
- **Modern Codex correction (2026-10-01, Corral 0.24.244 / SessKit 0.2.5
  published and installed on both development hosts)**:
  First-candidate acceptance also rejected stale terminal inheritance: public
  native scans of prior completion/abort followed by a new modern turn and
  command/message items still returned the previous terminal state with a
  nonempty identity. Mid-turn suppression must invalidate older terminal
  verdicts too. The same worker corrected ordered activity invalidation; the
  coordinator independently passed those three public-scan cases and the real
  hub/notifier path: repeated mid-turn items added zero sends, a running restart
  added zero, the genuine terminal event added one, and metadata/rescan/finished
  restart added zero. Full provider tests: 516 passed; published wheel digest
  and exact parser source matched. Twelve stable native histories were sampled,
  including one modern ongoing turn with no notifiable terminal id. Both hosts
  passed the installed consumer gate. The exact same public scan/hub/notifier
  acceptance also passed with each host's installed interpreter, with zero
  synthetic phone pushes. The new worker heartbeat and shared snapshot cohort
  are active on both hosts. Saved completion/abort preferences were restored
  only for their original paired devices; unrelated configuration was preserved.
  The retained logs covered the first 243 seconds after restoration on both
  hosts and contained zero APNs accepts. This is a bounded observation, not a
  delivery proof for a future genuine completion.
  The cache/Claude fixes above shipped and passed their acceptance, but a live
  Codex RPC history continued producing completion pushes during command and
  message activity. Its stable native history had a started turn and modern
  completed items, with no completed/aborted turn; additional activity followed
  its earlier final-answer item. The installed shared snapshot still projected
  DONE. This is a separate native-finality defect, not proof that cache cohorts
  failed. See SessKit `docs/CONTRACT.md` Completion identity for the current
  requirement and official protocol references. Private backups preserve the
  pre-containment preferences; the temporary mute is now removed after installed
  acceptance. Both encrypted-relay full-table subscriptions passed: one detail
  from each of the five active assistants on the desktop host; one from each
  of the four present assistants on the Linux host (no Claude history there).
  iPhone readback is 1.0.74 (84); launch still returns Locked, so physical
  reception, notification tap/navigation and affected-screen visual acceptance
  remain unverified. The signed build is also available on AppShelf.
  Bounded-read limit: if every modern framing marker is outside both head/tail
  windows, legacy assistant-text fallback remains possible. A structural probe
  reproduced that limit; no matching live failure was observed. Do not claim
  arbitrary evicted histories or physical phone reception/tap were verified.
- **Framing-eviction gate rejected (2026-10-01, installed provider 0.2.5)**:
  the coordinator reproduced the documented fallback limit through public
  native scanning and the real hub/notifier. A 42 KB started-turn fixture with
  modern markers outside both windows and a trailing progress message produced
  one captured false completion, with no native terminal event and no actual
  phone push. The previous ordinary-case acceptance and dual-host delivery
  remain valid, but this additional gate is pending. The same OpenCode session
  owns the correction: keep legacy display compatibility, require native
  turn-end evidence for a nonempty notification identity. See provider CONTRACT
  for the authority. End notifications are temporarily muted again on both
  hosts; original preference backups remain the restoration source.
- **Native terminal identity gate rejected (2026-10-01, candidate)**:
  framing eviction now produces an empty identity and zero captured sends,
  but two genuine native turns with identical final text and no `payload.id`
  collide and produce only one send. Stable native sampling found 174 terminal
  rows using `turn_id` and event timestamps rather than `payload.id`. The same
  worker must anchor identities to actual native terminal evidence, preserving
  distinct turns and metadata/restart stability. Provider CONTRACT remains
  authoritative; no notification restoration or delivery claim is made yet.
- Cursor 收紧（二选一，SessKit 仓内定）：
  A. 无明确“助手最终答复 / 结构化完成”证据时宁可 `STATUS_NONE`
  也不给 `STATUS_DONE`；B. 维持现状但 `completion_id` 为空，
  Corral 对空 `completion_id` 的 Cursor DONE 不推完成通知。
  推荐 A（与 CONTRACT “Prefer unknown over false DONE” 一致）。

SessKit 验收沿用其 `CONTRACT.md` “Verification”：fixture +
真机历史抽样，至少覆盖成功与异常各一条，且 Corral `sesskit_bridge`
与 SessKit 注册表返回同一 `(role, text)` 序列。

### 2.2 Corral 侧消费（不改扫描，只改推送层）

- `session_payload` 增带 `completion_id`、`file_mtime`、`size_bytes`
 （只读透传，不参与列表排序/筛选/版本指纹，避免手机整表重拉）。
- `_detect_status_changes` 保持“跃迁检测 + 新 key 首见鲜度（300 秒）”
  语义不变；`PushNotifier` 去重键改为
  `(session_key, completion_id or status_tag, kind)`，持久化已发集合
  （见 §3），内存 120 秒节流保留（防抖动），但**节流只跳过发送，
  不跳过去重记账**。
- `STATUS_DONE` + 空 `completion_id`（无精确终局锚点的弱证据）：
  默认不推完成通知，只记日志。这是永久安全闸（SessKit 以空 id 表达
  unknown，Corral 永不据此推完成通知），不是上线初期的临时分支。

## 3. 开关与投递（Corral + iOS + 中继，不动 SessKit）

### 3.1 开关语义（两层，默认全开）

| 层 | 存哪 | 管什么 | 默认 |
|---|---|---|---|
| 手机本地偏好 | `UserDefaults`（随 `preferDirectConnection` 同例） | 这台手机展不展示完成/中断通知 | 开 |
| 设备推送偏好 | 开发机 `remote.json devices[].notify_completed` / `notify_aborted` | 开发机给这台设备发不发 | 开（缺字段=开，老手机不受影响） |

- 设置页（`HostSettingsView` 通知区）在系统授权行之下加两行 Toggle：
  “工作完成时通知” / “异常中断时通知”。关闭任一，手机本地不再展示
  该类（NSE 侧按 `kind` 丢弃），并经 `push.register` 同步到开发机
  （开发机直接不发，省配额也省电）。
- `push.register` 新增可选 `notify_completed: Bool` /
  `notify_aborted: Bool`；`touch_device` 只在字段出现时覆盖
  （沿用“读最新再叠加”，禁止整份覆盖，见 `config.py` 注释）。
- `hello` 的 `capabilities` 新增 `"completion_notify": True`；
  手机据此决定展不展示那两行 Toggle（老开发机不展，只保留系统授权行）。

### 3.2 投递与重试（2026-09-30 可靠性修订：enqueue ≠ APNs 接受；
### 2026-09-30 review 修正：显式重试接线、失败持久化、靶向重试、节流轮次规则）

- `PushNotifier._emit` 发送前查设备偏好：`kind==completed` 看
  `notify_completed`，`aborted` 看 `notify_aborted`，`waiting` 不受此开关影响。
- 已发集合落盘（与 `remote.json` 同目录，0600，原子写），键为
  `(session_key, completion_id-or-status, kind, device_id)`（按设备去重；
  无设备后缀的旧键只读兼容，避免升级后重推风暴）。
  进程重启后先读盘：盘里有就不重发；`_last_status` 快照仍只做启动基线。
- **只把中继回执 `FRAME_PUSH_RECEIPT ok:true` 当成功**（APNs HTTP 200 = 已接受，
  不是已送达手机——日志一律用 queued / accepted / failed 命名，禁止 delivered）。
  `sender` 仅入队时记 `remote_push_queued`，不记已发；回执 `ok` 才记
  `remote_push_accepted`（兼记 `remote_push_sent` 别名）并按设备落盘；
  回执失败记 `remote_push_failed{ code, status, reason }` 且不落盘。
  无回执（旧中继 / 回执丢失）按退避超时视为未知失败，进入待重试。
- 待确认集合（`push-pending.json`，0600，原子写，有界）记录
  `push_id -> {round, device, kind, session, completion, ts, attempts,
  last_code, parked}`，发送前先落盘（同步回执先到也不丢），发送异常
  只更新 `ts/last_code` 不删除，可重试性永不因本地异常丢失。
  瞬时失败（transport/throttled/no_push_config/local_send/未知超时）保留待重试，
  退避 `min(60s × attempts, 600s)`；永久失败（bad_token/bad_request/rejected）
  直接标记 `parked` 等新轮次，不空转。成功永不标记 parked。
- `SessionHub` 每次扫描后经显式重试驱动重发仍是最新 `completion_id` 的轮次。
  生产组装（`daemon.py`）注册的是绑定方法 `push.on_status_change`，
  其上取不到 `retry_due`，因此驱动必须经绑定 `__self__` 解析
  （`getattr(hook,"retry_due",None) or getattr(getattr(hook,"__self__",None),
  "retry_due",None)`），回归须走真实 `RemoteDaemon/SessionHub` 组装验证，
  禁止只直调 `retry_due` 冒充。
  `completion_id` 已变则旧轮自然过期，只推最新；同一 (round, device) 发送
  超过 5 次后 `parked`（持久化，重启后仍有效），等新轮次。
- 重试是靶向单设备的：一次只重发到期的那一个 (round, device)，已接受的
  同胞设备永不重发；在途（未到期）的同胞待确认抑制重复入队；
  重试不重置同胞的 attempts。
- 节流：`(key, kind)` 记录 `(ts, round)`。同轮 120 秒内重复跃迁抑制（防抖动）；
  **新 `completion_id`（新轮）不受旧节流牵连**，节流窗内也必须发出；
  跳过不落 pending（旧轮重放永不补发）。回执失败/本地发送失败清该键节流，
  以便下轮扫描重试（扫描间隔本身即退避）。
- 中继 wire 契约（`FRAME_PUSH` + `0x09` 回执）本次 review 不变，见 §3.4 与
  `relay/docs/PROTOCOL_V2.md`；relay 侧仅做新增注释/错误文案英文化与 gofmt。

### 3.2.1 Closure corrections (2026-09-30, final; sources before code)

Apple provider rules (coordinator-fetched official source,
https://developer.apple.com/documentation/usernotifications/handling-notification-responses-from-apns,
search crawl 2026-09-30):

- Retry Apple 5xx only AFTER 15 minutes: pending entries whose last receipt
  has `status >= 500` use an earliest-retry floor of 900s
  (`_APPLE_5XX_RETRY`), independent of the linear bounded backoff used for
  network errors and missing receipts. Receipt `status` is persisted in
  `push-pending.json` so the 900s rule survives restart. Attempt cap (5 sends
  per round/device) still applies.
- Never retry `BadDeviceToken / DeviceTokenNotForTopic / Forbidden /
  ExpiredToken / Unregistered / PayloadTooLarge`: the existing
  `_PERMANENT_CODES` (`bad_token/bad_request/rejected`) already parks all of
  these deliberately — preserved, no behavior change.
- Provider success is HTTP 200 only (relay `Sender.Send` maps `== 200` to ok;
  any other 2xx is treated as rejected, with a regression test).
- Persistence race: scanner sends and the asyncio receipt callback mutate the
  sent/pending ledgers concurrently. Snapshots were taken under `_lock` but
  written after releasing it to a SHARED `.tmp` path, so concurrent saves
  could clobber each other's temp file or persist an older snapshot over newer
  state (accepted dedupe lost after restart → duplicate push). Rule: mutate +
  snapshot + write happen atomically under one lock, and every write uses a
  UNIQUE temp path (`mkstemp`) before atomic `os.replace`. Lock order is flat
  (single `_lock`, never held across `sender()`), so no deadlock. Regression
  uses barrier/event sequencing (no flaky sleeps): newer accepted/pending state
  must win on disk, no `.tmp` leftovers, reload-then-no-resend after accept.

### 3.3 推送内容（载荷已带 `kind`，NSE 侧按 `kind` 选分类）

- `kind` 保持 `completed` / `aborted` / `waiting` 三值；NSE 按 `kind`
  选分类：`waiting` 沿用 `CORRAL_WAITING`（快捷回复）；完成/中断用新分类
  `CORRAL_COMPLETED`（动作：打开会话；暂不加快捷回复，避免误发指令）。
- 正文：完成 = `last_agent` 首行（160 字内），为空回退“Session finished”；
  中断 = `last_agent` 报错摘要（SessKit ≥0.1.5 已保留 429/限流原文），
  为空回退“Session stopped with an error”。成功与异常文案永不混用。
- `userInfo` 沿用 `session_key` + `host_id`；点击通知按现有
  `PushRegistrar.didReceive` 路径进详情（无文本时不发 `input.text`）。

### 3.4 中继（2026-09-30 可靠性修订：回执 + 永不因推送杀主连接）

- `FRAME_PUSH` 载荷加可选 `"id"`（客户端生成，旧中继忽略未知字段）；
  新增 `FRAME_PUSH_RECEIPT (0x09)` 中继 → 开发机（interaction 面）：
  `{"id","ok","code","status","reason","apns_id"}`，`code` 取
  `ok / no_push_config / bad_request / bad_token / rejected / throttled /
  transport / internal`，`reason` 透传 Apple `reason` 原文。
  载荷密文仍 opaque，中继只做零知识转发 + 路由级回执。
- 未配置推送（无 sender）必须回 `ok:false code:no_push_config`，禁止静默 `nil`
  成功。`forwardPush` 及推送错误一律转成回执，**永不因此关闭开发机主连接**；
  回执发送本身 best-effort（队列满则丢，不致命）。
- `Sender.Send` 返回结构化结果（HTTP 状态 + Apple `reason` + `apns-id`），
  空 token 按 `bad_request` 处理；4xx 按 Apple `reason` 映射
  (`BadDeviceToken/Unregistered→bad_token`，429→throttled，其余→rejected)，
  网络/超时/5xx→transport。偏好过滤仍在开发机做。
- 多租户日推送配额（`account.go` `Pushes`/`PushLimit`）上线前确认够用部分不变。
  旧开发机（无 `id`）与旧中继（无 `0x09`）互通：旧端忽略未知字段/帧；
  新开发机对旧中继按回执超时重试（有界），旧中继永不被当成功。

## 4. 可选增强（不进首版，留口子）

1. **官方钩子做早唤醒**：Claude `Stop` hook / Codex `notify`
   (`agent-turn-complete`) / Cursor `afterAgentResponse` / Pi 扩展
   `agent_settled` 只负责“叫醒一次提前扫描”，触发仍以 SessKit 快照为准。
   每个都是可选安装、失败开放，绝不单独成触发。
2. **Pi `agentPhase` 辅助**：`agent_settled` 后进入“疑似结束”观察窗
   （如 30 秒），窗内 jsonl 落盘且 `status_tag` 翻成 DONE 才推；
   窗内又有 `agent_start` 则取消。这是对重试的缓冲，不是新触发。
3. **按会话免打扰**：`session.pin` 同例加 `session.mute`（手机→开发机），
   存布局库旁；首版不做，协议上 `session_payload` 已有 `pinned` 字段可仿。

## 5. 实施切片（建议 4 步，每步可独立验收）

- **Slice 0 — SessKit `completion_id` + Cursor 收紧**（跨仓）：
  改 `sesskit/models.py`（可选字段）、各 parser 尾哈希、
  `schemas/session.v1.json`（root + wheel 内两份同改）；
  单测 + CONTRACT 真机抽样。Corral 侧 `sesskit>=0.1.6`（待定号）。
- **Slice 1 — Corral 去重与证据透传**：
  `session_payload` 加三个只读字段；`PushNotifier` 去重键 + 落盘已发集合；
  `test_remote_push.py` 加“同会话两轮都推 / 重启不重推 / 节流不吞第二轮”用例。
- **Slice 2 — 开关端到端**：`push.register` 偏好字段 + `touch_device` 叠加；
  `hello capabilities.completion_notify`；iOS 设置页两行 Toggle +
  NSE 按 `kind` 丢弃；`test_remote_service.py` 加偏好用例。
- **Slice 3 — 真机验收**：走 `scripts/phone_remote_acceptance.py`
  加“完成通知探针”（或真机手工）：每个助手各跑一条成功 + 一条异常
  （额度/限流可用 fixture 历史 + 新鲜 mtime 触发首见路径），
  核对手机收到两类通知、点击进对会话、关闭开关后不再收到。

每步都不碰 TUI 侧栏、不碰关注圆点、不碰扫描签名；Slice 0 未合入前，
Slice 1/2 可先按“有 `completion_id` 则用、无则对 DONE 保守静默”的兼容逻辑开发。

## 6. 风险与取舍

- Cursor 弱证据是最大误报源：用“空 `completion_id` 不推 DONE”兜底，
  宁可漏推一次完成，也不把“刚开了个头”当成干完了推给用户。
- Pi 重试窗口：`agent_settled` ≠ 整轮结束，首版不靠它触发；观察窗只做可选增强。
- APNs 非保证送达：通知只是“提示”，手机前台仍以订阅恢复为准
  （`MOBILE_REMOTE_DATA_PLANE_DESIGN.md` §2.0 第 7 条）。
- 配额：完成通知是新增推送量，上线前先核中继日限额。
- 开源默认：`remote.json` 缺字段=开；新装仍 LAN-only（硬规则不变），
  开关只在已配对设备上生效。

## 7. 验收清单（发布前逐项勾）

- [ ] 同一会话 120 秒内完成两轮，两条都推（去重键含 `completion_id`）。
- [ ] 开发机重启后，旧完成不重推，新完成照推。
- [ ] Cursor “有标题就算 DONE”的弱证据不推完成通知（或 SessKit 修好后推对的）。
- [ ] Codex 额度耗尽推的是中断不是完成；Pi 429 同理。
- [ ] 设置页关“完成”后开发机不发完成、仍发中断；关“中断”反之；都关则只剩 waiting。
- [ ] 老手机（不发偏好字段）行为与今天一致。
- [ ] 中继验收：整表订阅 + 每个助手一条详情（REMOTE_KB 硬门槛）。

<!-- 该文档整理/压缩于 2026-09-29 -->
