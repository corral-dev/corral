# 跨助手接力与启动知识库

## §0. 领域定义、主称谓与边界

### Session fork naming (2026-10-01)

The same-assistant session branching action is named **Fork Session** in English and **分叉会话** in Simplified Chinese across the TUI and iOS client. Replace the former Copy Session wording consistently in action labels, accessibility hints, progress/error text and newly generated fork titles. This changes presentation only: retain the existing fork/clone behavior and stable internal `session.copy`, translation keys and adapter methods. Keep existing user history and titles intact; normalize legacy copy suffixes to the fork suffix when deriving a new fork title. Copy-to-clipboard actions retain their existing copy wording.

本知识库描述 corral 如何把用户选中的会话，转换为可执行但尚未执行的**启动计划（LaunchPlan）**。本领域的主称谓固定为：

- **跨助手接力**：源助手导出**接力材料（Handoff）**，目标助手读取源历史后创建自己的新会话。
- **高级操作**：用户对既有会话的业务入口。弹窗第一项是**导出会话**（写与 `corral share` 相同的 `corral.share/v1` JSON 到缓存目录，并把绝对路径复制到剪贴板，不启动会话）；第二项是**分叉会话**（同助手分叉历史：有官方分叉则走分叉启动，否则磁盘复制历史并换新身份后再原生恢复）；第三项是**重启会话**（结束卡住的托管进程后按原会话原地恢复，上下文保留；仅对 corral 正托管且非占位的会话可用，其余置灰，替代“先 q 结束再回车恢复”的两步操作；**菜单选定后立刻执行，不再二次确认**，2026-09-13）；其后每一项（含来源自身）是**读取源历史后新建会话**（同助手另起用于原会话卡住 / 出 bug）。真正的原生恢复走侧边栏回车，不走高级操作。
- **启动计划（LaunchPlan）**：由参数数组与可选工作目录组成的、可独立测试的启动描述；生成与真正启动分离。
- **接力材料（Handoff）**：源历史文件、源工作目录、历史阅读提示、任务标题和对话摘录组成的统一交接包。
- **原生恢复**：同一助手按自身会话标识恢复原会话。
- **空白新建会话**：在选定有效工作目录启动助手，但不关联、不读取任何既有会话历史。

本领域覆盖 `models.py`、`runtime/`、高级操作的业务触发，以及 `corral context` 复用提示词的边界。它不覆盖各助手历史文件的 JSONL/SQLite 解析细节、终端界面实现、内嵌面板、标题生成，也不描述会话保活的内部实现。

## §1. 业务目标与不可变边界

目标是在不篡改历史的前提下，让用户可在同一助手继续原会话、换助手接力未完成工作，或在指定项目目录从零开始。

1. 同一助手恢复优先保留其原生上下文与会话语义；不得为了统一而把它改造成新会话。
2. 跨助手接力必须让目标助手新建自己的会话，再按提示读取源历史；绝不能把源会话冒充成目标助手的原生会话。
3. 源历史文件和工作区都是事实来源。接力材料只提供定位线索，不能成为改写或覆盖历史的理由。
4. 注册表是编排中心：界面和调用方不根据 Claude、Codex 等名称拼接启动参数，也不维护任意两助手之间的转换规则。
5. 启动计划只表达 `argv` 与 `cwd`，必须交由无 Shell 的进程启动方式执行；生成计划本身不启动进程、不改写历史。
6. 接力与保活分层：注册表先得出启动计划，后续是否托管或保活是运行时无关的外层行为。本领域不向适配器泄露保活概念。

Codex native resume, including Restart Session, must keep the original thread ID while loading the account currently selected on disk. Every interactive Codex launch that Corral constructs, including a blank new session, a handoff into a new session, a fork, and native resume, must avoid the shared app-server. Add `--no-daemon` to direct Codex launch plans: restarting only its TUI or opening a new TUI without this flag can reconnect to a shared app-server that still holds the previous account and its exhausted quota. The embedded host (`embed.host_session`) executes the plan directly. The detached/attached keepalive wrapper (`keepalive.wrap_plan`) instead runs a private official app-server through `codex_proxy`; that proxy must remove `--no-daemon` from the TUI arguments when it adds `--remote`, because Codex rejects that combination. Do not restart the shared daemon or other users' sessions as a side effect of one session restart. Verify both the session identity and the new server account; a changed status-bar percentage alone is insufficient.

Legacy Corral managed Codex panes may predate the proxy and have no exact thread claim. A scanner can then report the shared app-server PID as live while failing to associate the pane, making Enter incorrectly say the session is running in another terminal. Adopt such a pane only when its live process command contains `codex resume` with the full thread UUID matching the scanned session. The eight-character pane name alone is insufficient evidence; never adopt a genuinely external Codex process.

## §2. 状态与分流

用户从已存在会话进入高级操作时，选择的目标助手决定唯一分流；空白新建会话则不经过源会话与接力材料。

```mermaid
flowchart TD
    A[选会话] --> B{入口}
    B -- 回车 --> C[原生恢复]
    C --> D[来源适配器生成原生 LaunchPlan]
    B -- Ctrl+T 高级操作 --> P{弹窗选项}
    P -- 导出会话 --> Q[写 share JSON 并复制路径]
    P -- 分叉会话 --> R[同助手分叉]
    P -- 选目标助手 含自身 --> E[选目标助手]
    E --> F[源适配器导出 Handoff]
    F --> G{源历史文件存在且可读？}
    G -- 否 --> H[拒绝生成计划并说明历史不可用]
    G -- 是 --> I[目标适配器生成新会话 LaunchPlan]
    I --> J[目标助手读取原始历史并继续任务]

    K[选项目目录] --> L[空白新建会话]
    L --> M{工作目录可用？}
    M -- 是 --> N[目标适配器生成空白 LaunchPlan]
    M -- 否 --> O[不带工作目录启动]
```

- **原生恢复**（侧边栏回车）仅按源助手的会话 ID 和其私有恢复参数生成计划；历史路径即使不存在，也不应被跨助手历史校验误伤。
- **接力新建**（高级操作）先校验源历史文件，再调用“源导出、目标导入”这一条通用链路；`LaunchRequest.force_new=True` 时即使目标等于来源也走这条路。目标计划不得携带原生恢复参数。
- **空白新建会话**只使用用户选择的工作目录与目标助手，不渲染接力提示词、不读取历史。
- 直启透传是第四条独立路径：用户直接指定助手和参数时，只构造透传计划，不选择会话、不生成 Handoff。

## §3. 核心对象与职责契约

### 3.1 接力材料（Handoff）

`models.py` 中的 Handoff 是跨助手协议，而不是某个助手的历史格式。它携带来源助手标识与显示名、标题、绝对历史路径、原工作目录、历史阅读提示和 `conversation_digest`。

- `history_path` 必须指向当前机器真实存在的历史文件；导出时转换为绝对路径。
- **接力前缺少历史位置是可预期的拒绝，不是整个界面的致命错误。** 扫描到的临时、外部或已失效会话可能没有可交接的历史文件；此时保留原会话不动，向用户说明该会话不能接力，并让界面继续可用。禁止把这类 `LaunchError` 冒泡成未捕获后台异常而退出终端界面；也不要为了绕过校验伪造空路径或把别的会话历史塞给目标助手。
- `original_cwd` 可以为空或已经失效，不能因此阻止接力；目标计划应通过 `usable_cwd` 决定是否采用它。
- `history_reading_hint` 由源适配器提供，告诉目标助手怎样只读定位源历史，不应由编排层猜测。
- OpenCode 历史数据库是共享容器，导出时还必须把会话 ID 写入阅读提示，避免目标读取错误会话。这段说明会整段塞进目标的 `--prompt`；扫描侧不得再把提问正文当命令行解析（见 [会话扫描知识库](SESSION_SCANNING_KNOWLEDGE_BASE.md) §6），改提示词也不用为了判活去回避 `session` 等词。
- Handoff 是只读交接描述，不包含可执行 Shell 字符串，也不拥有写入源历史的能力。

### 3.2 对话摘录（conversation_digest）

摘录用于让刚启动的目标助手快速锁定任务与进度，不替代原始历史。

- 基类统一通过 `load_conversation` 构建；各适配器不得各自复制一份摘要算法。
- 摘录保留原始需求及最近最多八条用户/助手消息，并压平多行、按长度截断，避免破坏提示词的逐行结构。
- 角色只能标记为“用户”“助手”，禁止用“你”；“你”会被接手的模型错误理解为它自己。
- 解析异常或没有可用对话时，静默回退到扫描阶段已有的首尾消息；再无内容则保留空串，接力仍可继续。
- 原始历史永远是权威：提示词必须说明摘录是截断版，要求目标助手以它为线索核对、补全源文件与工作区事实。

### 3.3 启动计划（LaunchPlan）

LaunchPlan 只包含 `argv` 参数数组和可选 `cwd`，让计划能被测试、托管或最终执行层安全消费。

- `argv` 是参数数组，不是需再次解析的命令字符串；调用方不得拼接进 `sh -c`、`eval` 等 Shell。
- `cwd=None` 表示不改变当前目录；直启透传固定使用此语义。
- 真正执行前才检查可执行文件是否已安装、切换可用工作目录并替换当前进程。
- 计划生成与执行解耦，支持高级操作、空白新建、直启和只读计划查询复用同一模型。
- **跨助手接力的进程工作目录 ≠ 历史文件目录。** `LaunchPlan.cwd` 只取源会话记录的项目目录（`handoff.original_cwd`，经 `usable_cwd`）；Claude / Codex / Cursor / Kimi 另在 argv 里加 `--add-dir <源历史文件所在目录>`，只为让目标助手有权只读打开落在项目外的历史（如 `~/.claude/projects/…`、`~/.codex/sessions/…/日期/`、`~/.cursor/chats/…`）。OpenCode / Pi 没有等价参数，靠工作目录外的路径权限或提示词指引去读。排查「接力后默认工作目录像是会话文件目录」时：先分清进程 cwd 与 `--add-dir`；后者本就是历史父目录，不是项目根。仅当源扫描里的 `cwd` 为空或目录已删时，进程 cwd 才会降级为 `None`（继承当前环境），仍不得把历史父目录塞进 `LaunchPlan.cwd`。

### 3.4 运行时适配器

每个助手适配器拥有其私有恢复方式、跨助手新会话方式、空白新建方式及历史阅读提示。

- `build_resume_plan`：只服务同助手的原生恢复。
- `build_new_plan`：接收 Handoff，为跨助手接力创建目标助手的新会话。
- `build_new_session_plan`：只在指定目录启动空白新会话，不能夹带 Handoff 提示词。
- `export_handoff`：把本助手私有会话投影为统一 Handoff；历史不可用时明确失败。
- 新增助手只需实现这些能力并在默认注册表登记一次；禁止新增“助手 A → 助手 B”的专用分支。

### 3.5 注册表编排

`runtime/registry.py` 的注册表是业务分流唯一入口。

- `build_launch_plan` 比较来源与目标：相同且未 `force_new` 即调用来源的原生恢复；不同或 `force_new` 则执行导出 Handoff → 目标 `build_new_plan`。
- `build_new_session_plan` 将空白新建请求路由给目标适配器，不读取既有会话。
- `build_passthrough_plan` 将直启参数原样交给目标可执行文件，仅按规则补齐自动批准参数。
- 注册表不认识具体命令行参数含义，避免编排层与适配器的私有行为耦合。

### 3.6 自动批准参数（auto_approve_args）

危险的自动批准参数必须在对应适配器的 `auto_approve_args` 中声明为单一来源。

- 原生恢复、跨助手新会话、空白新建及直启透传应复用这份声明，不得各处硬编码同一参数。
- 直启若用户已显式携带某自动批准参数，注册表不得重复插入。
- 参数只在特定子命令有效的运行时是有意例外：OpenCode 的该参数只可用于非交互续接命令，因此不能放入通用 `auto_approve_args`，否则会破坏其裸直启和交互启动。
- Pi 的 `--approve` 属于根命令，恢复、分叉、跨助手接力和空白新建均复用它；同运行时恢复/分叉传入历史 JSONL 路径，跨助手接力仍只传统一交接提示词，绝不改写源文件。
- corral 不得私自覆盖助手的模型或推理强度；各助手自己的全局设置是启动计划的唯一默认来源。只有用户在直启时显式给出的参数可以改变该次会话。

## §4. 关键业务规则

1. **禁止两两分支。** 不允许实现“Claude 转 X”“Codex 转 X”这样的组合逻辑。统一链路只能是“源导出 Handoff → 目标导入 Handoff”，新增助手的成本随助手数量线性增长。
2. **禁止伪造原生恢复。** 跨助手目标必须新建会话，不得传源会话 ID 作为目标助手的恢复 ID。
3. **禁止改写或伪造原会话文件。** 接力提示词只能要求只读历史；目标的新增内容只能落在目标助手自己的新会话中。
4. **不注入完成态。** Handoff 和 `render_prompt` 禁止恢复或新增 `status_tag`、`status_note`、`✅已完成` 等来源列表状态。接力目的在于继续判断未完成工作，提前宣告完成会让目标助手停止推进。
5. **工作目录必须可用。** 所有恢复、跨助手新建和空白新建都经 `usable_cwd` 校验；不存在的目录降级为 `None`，不能让启动因过期历史目录失败。
6. **高级操作分流。** 弹窗第一项「导出会话」写 `corral.share/v1` 到 `~/.cache/corral/share/`（尊重 `CORRAL_CACHE_DIR`）并把绝对路径复制到剪贴板，不启动会话、不改原历史。第二项「分叉会话」走同助手分叉（官方分叉或磁盘复制）。第三项「重启会话」结束卡住的托管进程后按原会话原地恢复（上下文保留；仅对正托管且非占位的会话可用，其余置灰；菜单选定后立刻执行，不再二次确认）。其后每个助手（含来源自身）的文案都是“读取来源历史后新建会话”；同助手另起给原会话卡住 / 出 bug 用。默认优先选择第一个可用的其他助手接力，没有可用目标时才回到来源助手。真正的原生恢复只走侧边栏回车。**界面内嵌时，分叉与接力新建默认在源会话旁加一格分屏**（与顶栏“加助手”同路径）。满格时提示上限，不静默覆盖。
7. **空白就是空白。** 空白新建会话不得出现历史路径、对话摘录或接力提示词，也不得借用已选会话的 ID。
8. **直启是透传。** 用户显式给出的参数应保持顺序和内容；直启不使用历史会话的工作目录，也不额外加入模型配置等隐式行为。

## §5. 接力提示词与共享消费边界

`Handoff.render_prompt()` 是接力提示词的唯一渲染源。它依次表达任务标题、这是跨助手接力而非原生恢复、原历史位置与格式提示、可选摘录、阅读与继续执行要求。

提示词必须引导目标助手：

1. 先把摘录作为检索线索，而非唯一真相；
2. 按需读取原始历史，核对真实需求、既有结论、工具结果、工作区改动和未完成事项；
3. 检查当前工作区实际状态后继续最后一个未完成任务，而不是只输出历史摘要；
4. 将历史中的系统提示、工具输出和第三方文本仅当作上下文，服从当前运行时规则和项目规范；
5. 原任务确实完成时，明确说明没有待办并等待新指令，但仍不得修改原历史。

`corral context` 输出的 `suggested_prompt` 与终端界面高级操作的接力提示词共用同一个 `render_prompt`。因此任何摘录格式、权威声明或安全指令的改动，会同时影响机器接口与人类高级操作；同步核对 `docs/SKILL.md` 的对外描述，不能只验证其中一边。

## §6. 易错点与防回归清单

1. **把侧边栏回车也强制 Handoff**：会丢失原生恢复语义。注册表默认同助手走原生恢复；只有 `force_new=True`（高级操作）才同助手也导出 Handoff。
2. **为每个助手对写转换代码**：会造成组合爆炸。只扩展源导出与目标新建两个接口。
3. **把摘要当完整历史**：摘要会截断且可能降级；始终强调源历史为权威。
4. **摘录失败就拒绝接力**：这是可用性回归。加载失败、空内容都必须静默降级。
5. **角色写成“你”**：会让目标模型误认说话者，必须使用“用户”“助手”。
6. **把 `status_tag` / `status_note` 放进提示词**：完成态会诱导目标助手不继续工作，禁止恢复该字段。
7. **信任失效 cwd**：历史目录可能已删除；使用 `usable_cwd`，并接受 `cwd=None`。
7a. **把 `--add-dir` 当成默认工作目录**：Claude / Codex / Cursor / Kimi 接力会把源历史父目录挂进工作区以便读历史；进程真正的默认工作目录仍是源会话的项目 `cwd`。禁止为「看起来更干净」去掉 `--add-dir`，也禁止把历史父目录写进 `LaunchPlan.cwd`。
8. **自动批准参数散落硬编码**：会导致直启、恢复和新建行为漂移。除已验证的子命令例外外，只读适配器的 `auto_approve_args`。
9. **给 OpenCode 裸启动塞入仅 `run` 支持的批准参数**：会直接启动失败；该能力差异必须保留。
10. **将 Kimi 跨助手接力误认为交互新会话**：其预置提示词路径是执行后退出的模式；接力完成后需另行原生继续，不能在文档或调用方承诺持续交互。
11. **让空白新建携带接力材料**：会污染从零开始的会话。它只能接受目标助手和有效工作目录。
12. **把直启当作高级操作的替代**：直启是用户参数的就地透传，`cwd=None`，不应读取会话、改变目录或构建提示词。
13. **在适配器内接入保活实现**：适配器只生成 LaunchPlan；保活只可在计划生成之后的外层介入。
14. **改了 render_prompt 却只测 TUI**：`corral context` 会同步变化，必须覆盖两种消费面。
15. **为了判活去改写接力说明、删掉 `session` 等词**：错方向。OpenCode 跨助手新建本来就把整段说明放进 `--prompt`；操作系统里进程命令行是空格拼接的，扫描必须在 `--prompt` 处停扫，而不是让提示词迁就解析器。改完提示词后仍要用含这些词的原文跑扫描回归。
16. **把缺历史路径的 `LaunchError` 冒泡出 `@work` 的 `action_handoff`**：会变成 `WorkerFailed`，Textual `_handle_exception` 整屏退出。分叉会话已经 catch；接力新建的计划生成在 `_embed_open` 里，那里必须 notify + 响铃后返回，不能让异常回到 worker。典型触发是刚托管、历史还未落盘的占位卡（`path=""`）。**不要**为了消这个现象去伪造空路径、把别的会话历史塞给目标，或在 `_handle_exception` 里吞掉 `LaunchError`。也不要在 `action_handoff` 里先 `build_launch_plan` 再交给 `_embed_open`——成功路径会把对话摘录做两遍，现有捕获计划的回归会失败。真机：2026-08-24 刚托管 Codex 占位卡后按 Ctrl+T 选助手闪退。回归：`test_handoff_without_history_path_keeps_tui_alive`、`test_embed_open_launch_error_keeps_tui_alive`、`test_cross_runtime_requires_history_path`。

## §7. 验证与排查

### 自动化验证

改动 `models.py`、`runtime/`、高级操作分流或直启计划后，至少执行：

```bash
cd cli
python3 -m unittest -v test_runtime.py
python3 -m unittest -v test_session_scanning.py
```

`test_runtime.py` 至少应覆盖：

- Claude、Codex、OpenCode、Kimi、Cursor、Pi 的同助手原生恢复；
- 双向或多目标的跨助手接力，断言目标没有错误携带原生恢复参数；
- 源历史文件不存在时跨助手接力失败；
- 缺少历史位置的会话尝试接力时保留终端界面和原会话，展示可理解的失败提示（`test_handoff_without_history_path_keeps_tui_alive` / `test_embed_open_launch_error_keeps_tui_alive` / `test_cross_runtime_requires_history_path`）；
- 各助手空白新建计划不含接力提示词；
- 不存在的工作目录降级为 `None`；
- 新助手仅注册一次即可加入通用接力；
- 自动批准参数的前置、去重与 OpenCode 例外；
- 直启透传的 `cwd=None` 与参数原样保留。

`test_session_scanning.py` 的 Handoff 摘录用例应覆盖首条需求与最近消息、角色标签、截断后的单行形态、重复窗口去重、加载失败回退、无摘要降级、提示词不含状态标签，以及多个目标助手都接收到同一份渲染提示词。

### 手工接力冒烟

1. 选一条有真实历史且工作目录仍存在的已结束会话，侧边栏回车，确认生成的是原生恢复（带会话 ID 的恢复参数），而非新会话提示词。
2. 再对该会话进入高级操作，选择同一助手，确认是读历史后新建（无原生恢复参数），并与源会话并排分屏。
3. 再选择一个已安装的其他助手，确认目标以新会话启动，并收到源历史路径、格式提示及接力说明；**界面内嵌时右栏应与被接力会话并排分屏**（源会话仍在，不得整屏换成新会话）。
4. 在目标助手中检查它会读取源历史与当前工作区、继续未完成任务，而不是只复述摘要或因“已完成”状态停止。
5. 用历史文件不存在和工作目录不存在的会话分别验证：前者跨助手接力应被阻止，后者允许启动但不切换到失效目录。
6. 通过 `corral context <会话>` 检查其 `suggested_prompt` 与高级操作启动时的接力提示词包含相同的摘要与安全约束。
7. 用空白新建流程选择项目和助手，确认不会出现任何历史文件、摘要或接力说明。
8. 直启某助手并给出自定义参数，确认用户参数未被改写、自动批准参数不重复，且当前目录不被历史 cwd 覆盖。

## §8. 与其他知识库的关系

- `docs/SESSION_SCANNING_KNOWLEDGE_BASE.md`：改、评审或排查会话来源、历史路径、会话工作目录、原生会话可恢复性和对话读取质量时联读；本知识库消费其扫描结果，不定义各助手解析细节。
- `docs/NEW_RUNTIME_ONBOARDING_KNOWLEDGE_BASE.md`：新增或评审一种助手的扫描、原生恢复、接力导出/导入、空白新建与注册验收时联读；本知识库定义接入后必须遵守的统一编排契约。
- `docs/TERMINAL_UI_KNOWLEDGE_BASE.md`：改、评审或排查高级操作入口、运行时选择、新建会话流程或用户可见文案时联读；本知识库只定义选择造成的业务分流，不涉及终端界面实现。
- `docs/SKILL.md`：改、评审 `corral context` 的接力数据、`suggested_prompt` 或机器接口说明时联读；它与高级操作共同消费 `Handoff.render_prompt`。
- `docs/MAINTAINER_GUIDE.md`：改、评审或排查运行时边界、接手提示词的对话摘录、直启、自动批准参数或与保活的分层关系时联读；其中的运行时差异是本知识库的实现依据。

## §9. 变更决策与维护准则

当新增助手、调整接力提示词或改变启动参数时，先回答以下问题：

1. 变化属于源导出、目标新建、原生恢复、空白新建还是直启透传？不要让一个入口承担另一个入口的语义。
2. 是否仍能通过注册表的通用链路完成，而无需按来源/目标名称分支？
3. 是否读取了历史但没有写入、伪造或迁移原历史？
4. 是否保留原始历史权威性，并让摘要的失败不阻断接力？
5. 是否同时验证了高级操作与 `corral context` 这两个 `render_prompt` 消费方？
6. 是否把所有危险自动批准参数收敛在正确适配器，并验证命令位置限制？
7. 是否明确运行时能力差异，而没有为了表面一致性生成不可执行的计划？

跨助手接力的成功标准不是“目标助手拿到了一段文本”，而是：目标在新的原生会话中，基于只读源历史和当前工作区，可靠地继续用户尚未完成的工作。

<!-- 该文档整理/压缩于 2026-09-05 -->
