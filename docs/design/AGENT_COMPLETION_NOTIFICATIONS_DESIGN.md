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

- 定义：同一会话里“一轮结束”的稳定标识。建议
  `f"{file_mtime_ns}:{size_bytes}:{status_tag}:{tail_hash}"`，
  其中 `tail_hash` 取该会话“本轮最后一组事件”的短哈希
  （Codex 取 `task_complete`/`turn_aborted` 那条记录的稳定字段；
  Pi 取分支叶子 `parentId` 链尾 + `stopReason`；
  Cursor 取 `updatedAtMs + prompt_history` 首条摘要；
  其余沿用“尾部事件时间 + 尾文本哈希”)。
- 要求：同一轮重复扫描必须算出同一个值；新一轮结束必须变；
  进程重启后对同一份历史重算仍得同一个值（持久去重不靠内存）。
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
- `STATUS_DONE` + 空 `completion_id`（老 SessKit / Cursor 弱证据）：
  默认不推完成通知，只记日志；SessKit 升级且字段齐后自动恢复。
  这是上线初期的安全闸，不是永久分支。

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

### 3.2 投递与重试

- `PushNotifier._emit` 发送前查设备偏好：`kind==completed` 看
  `notify_completed`，`aborted` 看 `notify_aborted`，`waiting` 不受此开关影响。
- 已发集合落盘（与 `remote.json` 同目录，0600，原子写）：
  `{(session_key, completion_id, kind) -> ts}`，有界（如 500 条，LRU）。
  进程重启后先读盘：盘里有就不重发；`_last_status` 快照仍只做启动基线。
- 发送失败（`sender` 抛错 / 中继断开）：记 `observe.event` 并保留“待补发”
  标记，下次 `_detect_status_changes` 同一 `completion_id` 仍在时重试；
  `completion_id` 已变说明有新一轮，旧的不再补（只推最新的）。
- 节流：保留 120 秒同 `(key, kind)` 节流防抖动；但同一 `completion_id`
  只发一次，不同 `completion_id` 不受节流牵连（修掉“第二轮被吞”）。

### 3.3 推送内容（载荷已带 `kind`，NSE 侧按 `kind` 选分类）

- `kind` 保持 `completed` / `aborted` / `waiting` 三值；NSE 按 `kind`
  选分类：`waiting` 沿用 `CORRAL_WAITING`（快捷回复）；完成/中断用新分类
  `CORRAL_COMPLETED`（动作：打开会话；暂不加快捷回复，避免误发指令）。
- 正文：完成 = `last_agent` 首行（160 字内），为空回退“Session finished”；
  中断 = `last_agent` 报错摘要（SessKit ≥0.1.5 已保留 429/限流原文），
  为空回退“Session stopped with an error”。成功与异常文案永不混用。
- `userInfo` 沿用 `session_key` + `host_id`；点击通知按现有
  `PushRegistrar.didReceive` 路径进详情（无文本时不发 `input.text`）。

### 3.4 中继（大概率不动）

- `FRAME_PUSH` / `forwardPush` / APNs 负载结构都不用改：偏好过滤在开发机
  做，中继仍只做零知识转发。唯一要核的是多租户日推送配额
  （`account.go` `Pushes`/`PushLimit`）：完成通知会增加推送量，
  上线前确认配额够用，不够则先调配额再上线。

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
