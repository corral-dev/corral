# corral 性能知识库

## 什么时候读

改、评审、优化或排查启动、会话扫描、对话预览、内嵌终端渲染、**侧边栏列表重建与分屏加格**、缓存、原生扩展、安装包或发布流水线时先读本文；**排查「电脑忙时 corral 卡、自身占用却不高」「自身 CPU 占用过高 / 风扇狂转 / 两个窗口特别吃 CPU」「Cursor 进程过多 / 活动监视器一堆 agent / cursor-agent」「同类会话管理 / 内嵌终端 TUI 的性能坑」「打开大历史第一次解析整份 JSONL / 详情把通道堵住」时也读**（见「系统高负载下的调度优先级」、「自身占用过高」与「同类应用踩坑地图」）。**性能优化动手前先做一轮外部调研**（同类 TUI / 终端工具的公开优化经验），再结合本地计时拆解，不要只靠本地 profile 闭门造车（机主 2026-08-17 纠正；本地计时的做法见「新开分屏（加格）链路」节）。各助手历史语义仍以 `SESSION_SCANNING_KNOWLEDGE_BASE.md` 为准，终端交互语义仍以 `EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md` 为准。

## 系统高负载下的调度优先级（为什么「自己不重却卡」）

用户常见体感：电脑 CPU 已经被浏览器 / IDE 打满时，corral 占用并不高，界面却开始掉帧、按键迟钝。根因通常不是业务逻辑变慢，而是**调度等级偏低**：

- **macOS**：未标注 QoS 的线程落在 Default（图形 App 主线程才是 User Interactive）。系统忙时会优先给更高档的进程 CPU，CLI TUI 可被饿死。业界同构事故：键盘重映射工具 kanata 在编译打满 CPU 时按键处理被饿 100–275ms，系统误判长按并自动连发；修法是把处理线程抬到 `QOS_CLASS_USER_INTERACTIVE`（[kanata#2040](https://github.com/jtroo/kanata/pull/2040)）。Apple 官方也写明：界面相关工作要用 User Interactive，否则界面会像冻住（[Energy Efficiency Guide](https://developer.apple.com/library/archive/documentation/Performance/Conceptual/power_efficiency_guidelines_osx/PrioritizeWorkAtTheTaskLevel.html)）。
- **Apple Silicon**：QoS 还会影响更偏向性能核还是能效核。Background 档会被钉在能效核；交互档优先性能核。别把「慢」一两个原因混为一谈——单线程算法慢 ≠ 被钉到能效核（见 Eclectic Light 对命令行工具与 QoS 的辨析）。
- **对策**（`schedprio.py`，v0.24.72 起进入 TUI 时生效）：启动时先撤销遗留的 macOS 后台让位标记，再把主线程提到 User Interactive；抓帧 / 控制通道读 / 鼠标发送线程使用 User Initiated。Linux 尽力 `nice(-5)`，Windows 尽力抬到 Above Normal。调用失败一律忽略，不得挡启动。
- **前台会话不得被后台治理误伤**：corral 正在展示或接收输入的界面及其托管助手属于用户正在等待结果的工作，绝不能被本机性能治理工具标记为后台让位；工具必须识别并拒绝此类目标。排查“列表不慢、但首帧或输入很卡”时，先检查会话及其运行时是否被后台降级，再归因到扫描或 Cursor 重绘。
- **不要**给标题生成守护进程、纯扫描后台也抬到 Interactive——那些可以让路；只保「用户正在看的界面」。远程守护的刷新线程用 `demote_background()`（Utility / nice+5）。
- **优先级反转**：界面线程若同步等更低 QoS 的辅助进程（如未抬档的 tmux 子进程），高负载下仍可能一起卡。macOS 对 Mach IPC 有 QoS override，但对「fork 出去的普通 tmux 客户端」不保证同等提权——因此热路径应走常驻控制通道，并给喂画面的线程也抬档。
- 这解决的是**被别人抢走时间片**，不是替代抓帧节流 / 原生解析等业务侧优化。若空闲时也卡，仍按本文其它节与下方踩坑地图排查。

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
5. **活动监视器里 Cursor / agent / cursor-agent 进程很多** → 先分清图形版和命令行助手。图形版没开时，这些几乎全是调度界面里打开过、还挂在保活里的 Cursor 命令行会话：**一张卡一只顶层进程，不是泄漏、也不是同一条聊天开了两份**。侧栏显示「就绪」的会话进程也还活着；空闲回收默认约 2 小时，**另有托管软上限 10**：超限时会关掉闲置 >10 分钟且非「执行中」的卡（进界面 / 新开托管时顺带跑）。刚开过不久、或仍在执行中的不会被压关，因此仍可能暂时多于 10。真正打满核的通常只有仍在「执行中」的那几只。问「有没有超限」只数保活托管会话，不要拿活动监视器进程数去比 10，也**不要对真实保活跑回收来“验证”**。不要为了消掉进程个数去关图形版 Cursor、杀 ChatGPT 名下的 node，或当成 fork 风暴去拆保活。要马上减进程：在调度界面结束不需要的托管会话（结束进程不删历史）；不要从活动监视器按短名一把杀光。权威机制见 `docs/MAINTAINER_GUIDE.md`「会话保活」回收段。

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

<!-- 该文档整理/压缩于 2026-09-05 -->
