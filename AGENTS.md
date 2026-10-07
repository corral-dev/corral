<!-- managed:inherited-agents:start -->
<!-- source: ~/Codes/Corral/AGENTS.md -->
# Corral

终端会话接力 CLI，支持跨 Claude Code / Codex / OpenCode / Cursor / Pi 会话恢复与接力（另有 Kimi 兼容实现 dormant：保留、不维护、无界面入口）。

通用工程规范：[Python 规范](~/Codes/_standards/python.md)

## 文档导航

Read the documents whose described content is relevant before deciding or changing behavior.

- [Agent workflow](~/Codes/Corral/cli/docs/AGENT_WORKFLOW_GUIDE.md): Readiness, resource ownership, resumable delivery and checkpoints.
- [Product task routing](~/Codes/Corral/cli/docs/PROJECT_TASK_ROUTING_GUIDE.md): Detailed cross-component preconditions and investigation routes.
- [CLI instructions](~/Codes/Corral/cli/AGENTS.md): Terminal UI, runtime integration, local development and CLI delivery.
- [Apple instructions](~/Codes/Corral/apple/AGENTS.md): Shared iPhone/Mac client, native UI, signing and device delivery.
- [Relay instructions](~/Codes/Corral/relay/AGENTS.md): Encrypted relay service, APNs and self-hosting.
- [Public presentation](~/Codes/Corral/cli/docs/PUBLIC_PRESENTATION_GUIDE.md): Approved identity, brand assets and sanitized captures.
- [Independent UI annotation](~/Codes/_standards/workspace-docs/IOS_UI_ANNOTATION_COMPONENT_DESIGN.md): Independent EditHere core and optional Corral adapter boundary.
- [Remote protocol](~/Codes/Corral/cli/docs/REMOTE_KNOWLEDGE_BASE.md): Pairing, relay/security boundaries, message delivery and multi-runtime acceptance.
- [Mobile data plane](~/Codes/Corral/cli/docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md): History loading, live data isolation, question state and reconnect recovery.
- [Managed session identity](~/Codes/Corral/cli/docs/design/PI_SESSION_IDENTITY_EXTENSION_DESIGN.md): Pi/Codex ownership, claims, plugins and legacy migration.
- [Completion notifications](~/Codes/Corral/cli/docs/design/AGENT_COMPLETION_NOTIFICATIONS_DESIGN.md): Completed/interrupted evidence, notification preferences, deduplication and delivery.
- [Historical multi-agent report](~/Codes/Corral/docs/2026-08-16-多助手会话专项汇报.md): Single-day archive, not current behavior authority.
- [UI change-request adapter](~/Codes/Corral/docs/design/IOS_UI_CHANGE_REQUEST_DESIGN.md): Corral's optional adapter for independent UI annotation.

## 组件一览

| 目录 | 技术栈 | 状态 |
|---|---|---|
| `cli/` | Python | 活跃 |
| `apple/` | SwiftUI | 活跃 |
| `relay/` | Go | 活跃（零知识中继 + APNs） Remote：`ssh://git@forgejo.caozc.top:2222/Max/corral-relay.git` |

## SessKit and Corral ownership

- Before changing session behavior, identify the owning layer and state where the fix belongs. A symptom appearing in Corral does not make its implementation Corral-owned.
- **SessKit owns native history interpretation and reusable session semantics:** runtime file/database formats, session and message extraction, real activity timestamps, native completion/error evidence, normalized schemas, transcript/activity projections and incremental history readers. Fix these in `~/Codes/SessKit`, with its parser tests and contract; never duplicate the fix in Corral scanners, remote handlers or Apple clients.
- **Corral owns product behavior:** runtime launch/resume/handoff, managed-session ownership and claims, tmux hosting, generated titles, attention/read/notification policy, product cache storage, remote transport, and client presentation, filtering, sorting and date grouping. Consume SessKit's normalized evidence; do not reinterpret raw history to bypass an upstream defect. Generic evidence belongs upstream; product decisions remain here.
- Keep the dependency direction **Corral → SessKit**. Corral's scan aliases and bridge adapt/delegate; they are not a second parser. Pass host-specific behavior through explicit extension contracts. Never move Corral UI, relay, hosting or product policy into SessKit, and never make SessKit depend on Corral.
- Runtime support is decided per product: Corral's five-runtime allowlist and dormant compatibility do not narrow SessKit's independent runtime support.
- For an upstream fix, publish a versioned SessKit artifact, update Corral's complete dependency pin, invalidate derived caches through the provider version, and verify the installed host plus affected clients. For an independent Corral fix, do not change SessKit merely because it supplies the data. Integration details: [session scanning knowledge base](~/Codes/Corral/cli/docs/SESSION_SCANNING_KNOWLEDGE_BASE.md).

## 领域地图（doc-init）

<!-- 覆盖度复核基线：2026-09-29 · 源码指纹 扫描 673 文件 / Python 190 · Swift 86 · Go 23 / 3 子模块 -->

| 领域 | 入口锚点 |
|------|---------|
| 终端界面 | cli/src/corral/ui/ · cli/src/corral/activity_board.py · cli/src/corral/cli.py · cli/src/corral/display.py · cli/src/corral/textutil.py · cli/src/corral/theme.py · cli/src/corral/store.py · cli/src/corral/i18n.py · cli/src/corral/split_layout.py · cli/src/corral/ui_prefs.py |
| 会话关注状态 | cli/src/corral/attention.py · cli/src/corral/attention_signals.py · cli/src/corral/cursor_observer.py · cli/src/corral/store.py · cli/src/corral/ui/ |
| 会话全文搜索 | cli/src/corral/search.py · cli/src/corral/ui/search_modal.py |
| 内嵌实时终端 | cli/src/corral/embed.py · cli/src/corral/ui/embed_pane.py |
| 会话扫描与对话内容 | **真源在 SessKit**（`~/Codes/SessKit` / `sesskit` 包）；`cli/src/corral/scan/` 为模块别名 · cli/src/corral/transcript.py · cli/src/corral/models.py · cli/src/corral/runtime/ · cli/docs/SESSION_SCANNING_KNOWLEDGE_BASE.md |
| 跨助手接力与启动 | cli/src/corral/runtime/ · cli/src/corral/runtime/pi.py · cli/src/corral/models.py |
| 新助手接入 | cli/src/corral/runtime/ · cli/src/corral/scan/ · cli/src/corral/runtime/pi.py · cli/src/corral/scan/pi.py |
| 托管会话身份 | cli/src/corral/pi_identity.py · cli/src/corral/pi_migration.py · cli/src/corral/codex_identity.py · cli/src/corral/pi_extension/ · cli/docs/design/PI_SESSION_IDENTITY_EXTENSION_DESIGN.md · cli/tests/test_pi_identity.py · cli/tests/test_pi_migration.py |
| 性能、派生缓存与原生加速 | cli/src/corral/cache.py · cli/src/corral/cache_cli.py · cli/src/corral/scan_index.py · cli/src/corral/native.py · cli/src/corral/schedprio.py · cli/src/corral/bootstrap.py · cli/rust/lib.rs · cli/Cargo.toml · cli/scripts/benchmark.py |
| 可观测与诊断 | cli/src/corral/observe.py · cli/src/corral/agent_api.py |
| 会话保活 | cli/src/corral/keepalive.py · cli/src/corral/liveness.py · cli/src/corral/legacy_names.py |
| 直启子命令 | cli/src/corral/cli.py · cli/src/corral/projects.py |
| 命令拦截（shim） | cli/src/corral/shim.py · cli/src/corral/bootstrap.py · cli/src/corral/runtime/registry.py |
| 标题补全 | cli/src/corral/titles.py · cli/src/corral/titlegen.py |
| Agent 只读查询 | cli/src/corral/agent_api.py |
| 手机远程接力（开发机侧） | cli/src/corral/remote/ · cli/docs/REMOTE_KNOWLEDGE_BASE.md（文首「开源中继硬规则」：公开默认关中继、无维护者域名） · cli/src/corral/bootstrap.py · cli/tests/test_remote_service.py |
| Apple 客户端（iPhone + 原生 Mac） | apple/ · apple/AGENTS.md · apple/Shared/（两端共享代码）· apple/macOS/ · apple/docs/UI_DESIGN_KNOWLEDGE_BASE.md · apple/docs/design/MACOS_CLIENT_DESIGN.md（局域网优先；自建中继才换网；开源与自编译版禁止内置共享中继，仅官方付费版可用托管中继，见远程知识库开源中继硬规则第 6 条） |
| 零知识中继与 APNs | relay/ · relay/docs/PROTOCOL_V2.md · 开源自建见 relay/README；个人多租户公网运维只在私有 agentsync（禁止写入公开门面当默认） |
| Windows / WSL 兼容（已裁定不做） | cli/docs/design/WINDOWS_COMPATIBILITY_DESIGN.md |
| 开源发布与一键安装 | cli/install.sh · cli/.github/workflows/ · cli/scripts/publish-release.sh |
| CI 流水线 | cli/.github/workflows/test.yml · cli/scripts/ci-test.py · cli/.githooks/pre-push · cli/scripts/install-git-hooks.sh |
| 客户端自动更新 | cli/src/corral/updater.py · cli/src/corral/ui/update_toast.py |
| 隐私与本地数据边界 | cli/PRIVACY.md |

## 待补充知识库（doc-init backlog）

（当前无待补充项；开源自建中继看 `relay/README.md`；维护者本机多租户公网运维只在私有 agentsync，禁止当开源默认地址。）

覆盖度扫描必须跳过手机端 Xcode 构建缓存（`apple/.derivedData*`）；不跳过会把文件数顶到上限、把 Swift 入口扫没。

<!-- project-owned: runtime-support -->
## 跨端缺陷修复（永久，2026-10-05 机主要求）

用户报告的缺陷无论出现在哪一端（iPhone、原生 Mac、终端界面 TUI），都要按同一用户场景检查其余两端；同样存在的在同一任务里一并修复、逐端验收，交付说明写明每端的检查结论（已修 / 不复现 / 该端无此功能）。

- **开发机侧**（`cli/`，含远程服务与 SessKit 扫描）的根因修一处，三端都受益，客户端无需发版，但仍要在受影响的端上验收。
- **`apple/Shared/` 公用代码**修一处覆盖 iPhone 与 Mac：两端都要真机 / 真窗验收并一起发版安装；只发一端，另一端继续跑旧代码，且公用改动可能改坏另一端。`#if os(...)` 平台分支与 `Shared/UI/Platform/` 只修到其中一端。
- **各端外壳**（`apple/iOS/`、`apple/macOS/`、TUI）代码互不共享；同类功能（分屏、侧栏、分组、焦点、搜索、状态点、提问面板等）要逐端对照行为，不能因代码不同就跳过。

## 运行时支持（Corral 产品边界，永久）

- 仅支持五个活跃运行时：Claude Code、Codex、OpenCode、Cursor、Pi；此为显式 allowlist，新增不在 allowlist 内的运行时需未来用户显式决策后才可加入。
- 已有 Kimi 兼容实现（适配器/shim/扫描/样式/资源/历史解析/测试夹具）可保留于源码与磁盘，处于 dormant：不再维护、不新增功能、不在任何用户可见入口中出现（新建会话、助手选择器/目录/筛选、接力/恢复/菜单、TUI 与手机侧列表/详情入口均隐藏）。
- 存量用户历史文件始终保留不删；展示与投影层按活跃 allowlist 过滤，不泄露入口；直接的程序化兼容调用可保留但不经 UI 暴露。

<!-- managed:inherited-agents:end -->

# corral 项目规范


- [Network UX implementation review](docs/reviews/NETWORK_UX_2026-09-10-review.md): **must read** before correcting, validating, or releasing the September 10 command-receipt and relay-lane changes; the four recorded findings were corrected in source (see that doc’s Corrections applied). Skipping it can reintroduce false delivery, duplicate execution, or shared disconnections.

## CLI documentation

Read the documents whose described content is relevant before deciding or editing.

- [Task routing](docs/AGENT_TASK_ROUTING_GUIDE.md): Detailed domain preconditions, symptom routes and exceptions.
- [docs/DEVELOPMENT_ENVIRONMENT_GUIDE.md](docs/DEVELOPMENT_ENVIRONMENT_GUIDE.md): Checkout readiness, locked dependencies and SessKit artifact handoff.
- [docs/TEST_ENVIRONMENT_GUIDE.md](docs/TEST_ENVIRONMENT_GUIDE.md): Isolated real-terminal fixtures, screenshots and behavioral assertions.
- [docs/PUBLIC_PRESENTATION_GUIDE.md](docs/PUBLIC_PRESENTATION_GUIDE.md): Approved identity, brand assets and sanitized public presentation.
- [docs/design/SESSION_TITLE_DESIGN.md](docs/design/SESSION_TITLE_DESIGN.md): Shared title policy, gateway generation and synchronization.
- [README.md](README.md): 使用、修改、评审或扩展会话扫描、会话关注圆点、Cursor 状态观察、终端界面、标题生成、运行时适配和跨运行时接力.
- [docs/TERMINAL_UI_KNOWLEDGE_BASE.md](docs/TERMINAL_UI_KNOWLEDGE_BASE.md): TUI sidebar, split panes, focus, shortcuts, selection and visual acceptance.
- [docs/EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md](docs/EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md): Live terminal capture, dimensions, control channels and theme detection.
- [docs/SESSION_SCANNING_KNOWLEDGE_BASE.md](docs/SESSION_SCANNING_KNOWLEDGE_BASE.md): SessKit integration, runtime histories, identity, liveness and completion evidence.
- [docs/design/PI_SESSION_IDENTITY_EXTENSION_DESIGN.md](docs/design/PI_SESSION_IDENTITY_EXTENSION_DESIGN.md): Pi/Codex ownership claims, managed identity and legacy migration.
- [docs/design/WEB_TASK_BUTLER_DESIGN.md](docs/design/WEB_TASK_BUTLER_DESIGN.md): Local Markdown task planning, coordinator/executor responsibilities, boundaries and validation experiments.
- [docs/design/WINDOWS_COMPATIBILITY_DESIGN.md](docs/design/WINDOWS_COMPATIBILITY_DESIGN.md): Decision against Windows and dedicated WSL product support.
- [docs/PERFORMANCE_KNOWLEDGE_BASE.md](docs/PERFORMANCE_KNOWLEDGE_BASE.md): Performance, caching, load, offline data and profiling.
- [docs/CROSS_RUNTIME_HANDOFF_KNOWLEDGE_BASE.md](docs/CROSS_RUNTIME_HANDOFF_KNOWLEDGE_BASE.md): Native resume, cross-runtime handoff, launch plans and working directories.
- [docs/NEW_RUNTIME_ONBOARDING_KNOWLEDGE_BASE.md](docs/NEW_RUNTIME_ONBOARDING_KNOWLEDGE_BASE.md): Runtime adapters, scanning, hosting and integration boundaries.
- [docs/OBSERVABILITY_KNOWLEDGE_BASE.md](docs/OBSERVABILITY_KNOWLEDGE_BASE.md): Local diagnostics, event logs and error evidence.
- [docs/MAINTAINER_GUIDE.md](docs/MAINTAINER_GUIDE.md): Development, CI, dependency pins, installation and release verification.
- [docs/REMOTE_KNOWLEDGE_BASE.md](docs/REMOTE_KNOWLEDGE_BASE.md): Pairing, encrypted remote protocol, delivery, relay boundaries and remote acceptance.
- [docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md](docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md): History loading, live-data isolation, pagination and recovery.
- [docs/design/MOBILE_SESSION_ACTIONS_DESIGN.md](docs/design/MOBILE_SESSION_ACTIONS_DESIGN.md): 实现、评审或联调 iOS 会话页右上菜单的复制（`session.copy`，新增）与接力（`session.handoff`，服务端已有）前必读.
- [docs/SKILL.md](docs/SKILL.md): 修改、评审 `agent_api.py` 面向 Agent 的子命令、字段或退出码语义（含 `diagnose`）.
- [PRIVACY.md](PRIVACY.md): 修改、评审或排查历史文件读取、会话关注状态库、Cursor 用户级观察配置、缓存写入、标题生成、跨运行时接力和开源隐私边界.
- [CONTRIBUTING.md](CONTRIBUTING.md): 修改开源贡献流程、验证命令、设计边界或 PR 要求.

## 架构约束

- `corral.cli` / `store` / `display` / `theme` 只负责入口、会话展示状态与用户选择，不得直接拼接某个运行时的启动参数。
- **入口分层与包顶层的兼容导出（改错了不报错，只会静默变慢或让老调用方失效）**：真正的命令入口是 `bootstrap.py`（`[project.scripts]` 指向它），它按子命令惰性分发，**只有进交互界面才 import Textual 与扫描器**——往 `bootstrap.py` 顶部加任何重量级 import，或把快速子命令（`--version`、`cache`、Agent 只读查询、`update`）改成经 `cli.py` 走一圈，都不会报错，只会让每次敲命令白付几百毫秒导入成本（实测 Textual 导入约 198ms），细则见 `docs/PERFORMANCE_KNOWLEDGE_BASE.md`「性能架构」。同理，`src/corral/__init__.py` 必须保持零重依赖：它只有 `importlib`/`os`/`sys`，历史扁平模块时代的符号（如 `RUNTIME_LABEL_STYLES`、`SessionStore`、`_filter_sessions_by_query`）靠 `_SYMBOL_EXPORTS` + `__getattr__` 惰性重导出。**移动或重命名这些符号时必须同步这张映射表**，否则 `corral.X` 形式的老调用方会在运行期才抛 `AttributeError`；也不要为了省事把它改成顶层 `from … import …`，那会让包顶层重新拖进整棵依赖树。`TEXTUAL_DISABLE_KITTY_KEY` 的 `setdefault` 必须留在包顶层（早于任何 `import textual`），原因与真实事故见 `docs/MAINTAINER_GUIDE.md`「CI 工作流」节。
- **派生缓存只做加速，任何异常都必须降级为「未命中」**（`cache.py`）：数据库损坏、锁竞争、只读文件系统都不得阻断原始历史读取，`CORRAL_CACHE=0` 要能完全绕开。一轮扫描内的元数据快照由 `begin_scan()` / `end_scan()` 圈定，两个并发扫描入口（`runtime/registry.py` 的 `scan_all`、`agent_api.py` 的 `_scan_runtimes`）都必须成对调用且 `end_scan()` 放在 `finally` 里；**快照严禁跨扫描长期持有**（同进程后续扫描会看不到本轮新写入的会话），payload 解码必须保持惰性。这几条写反了都不报错，只会表现成「列表少了会话」或「优化白做」，细则与实测数据见 `docs/PERFORMANCE_KNOWLEDGE_BASE.md`「派生缓存边界」。
- 运行时私有行为必须收敛在 `runtime/` 对应适配器中；新增运行时只实现扫描、对话预览、原生恢复、历史格式提示、接力新会话（读取其他运行时历史）和空白新会话（不关联任何历史，仅指定工作目录）两种启动能力，并在默认注册表注册一次。
- 跨运行时接力统一走“源适配器导出 `Handoff` → 目标适配器生成 `LaunchPlan`”，禁止增加 Claude→Gemini、Codex→Gemini 等两两转换分支。
- 同运行时使用原生恢复；跨运行时必须新建目标会话、让目标 Agent 按需读取原始 JSONL，不能改写或伪造原会话。
- 标题生成是独立服务，不属于任何运行时适配器。生成后端统一走 `titlegen.py` 的 `TitleGenerator` 抽象，经共享 OpenAI 兼容 LLM 网关发请求（配置见 `~/.config/corral/llm-gateway.json` / `CORRAL_LLM_GATEWAY_*`），**禁止**再经 Claude/Codex 等助手 CLI 生成标题。`titles.py` 不得直接拼接任何 CLI 命令；`titlegen.py` 与 `runtime/` 互不 import——运行时适配器管「怎么恢复/接力会话」，标题生成器管「怎么问一次模型」，职责不同，不要合并。标题和界面状态使用“运行时 + 会话 ID”作为唯一键，新增运行时不得退回纯会话 ID。若某 CLI 历史上曾把标题生成调用落盘成会话历史，对应扫描器须保留 `titles.PROMPT_MARKER` 前缀过滤。TUI 与远程共用同一份标题缓存与 `TitleState`；远程在仅标题变化时也要推列表/详情事件（见 `docs/design/SESSION_TITLE_DESIGN.md`）。**生成标题的语言跟该会话用户提问的主语言，不跟界面语言，也不默认中文**（2026-08-30 裁定；细则见 `docs/MAINTAINER_GUIDE.md`「标题与排序」）。禁止把标题当 `i18n.t()` 文案，也禁止写「英文会话也可以用中文」。`PROMPT_MARKER` 是噪音过滤用的固定原文，不得翻译。
- 会话预览：选中非进行中会话时，右栏直接展示完整对话（**默认钉在最新消息**，上滚看更早；用户离开底部后列表刷新不得强行钉回）；已托管会话右栏展示内嵌实时终端。**在别的终端窗口里跑、没被 corral 托管的会话（`live` 且无 `keepalive_name`）拿不到实时画面**——右栏走完整对话那一路并在详情头写明原因，打开它必须先确认（那是对同一份历史另起恢复进程，不是接管），细则见 `docs/EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md` §1。唯一界面是左栏会话列表 + 右栏（可最多四格均分内嵌终端），禁止再加回全屏预览或纯列表第二套入口。右侧顶栏可点选已安装助手在当前项目下加格；分屏会话会形成持久会话组，结束后仍保留，运行成员才参与启动恢复；组名、成员、折叠、置顶与侧栏显隐见 `split_layout.py`（`~/.cache/corral/sidebar-layout.sqlite3`）。**这份记忆多窗口共享：所有写入必须经 `SidebarLayoutDB` 在事务里重读最新再叠加，界面只持有只读快照，禁止改快照后整份覆盖写**（那正是多开窗口互相抹掉置顶与分组的老缺陷）。细则与 `_detail_stick_bottom` 见 `docs/TERMINAL_UI_KNOWLEDGE_BASE.md` / `docs/MAINTAINER_GUIDE.md`。
- **外部运行会话（2026-08-08 裁定）**：上条会话预览规则中“打开它必须先确认”的旧表述已废止。外部运行会话只能保持静态预览，**不得弹确认框，也不得针对同一份历史另起恢复进程**；等待原窗口结束后才可正常恢复。
- **侧边栏末行间隔、会话组与关注圆点（硬约定）**：凡往左栏加控件（搜索框、新建项、未来任何块），**最后一行必须是间隔空行**，画在该控件自身高度内并算进命中区与选中高亮；禁止用 `margin`、兄弟空隙或 `ListItem` padding 做分隔（点在空隙上不会落到本项）。会话卡例外：固定三行正文、高度 3，不再另加末行空行；标题统一使用基础标题样式，不因运行中整行变绿；**首行整体 bold（与下面两行拉开层级），其中项目名比标题淡一档（`dim`）、标题本身不得 dim**——项目名是定位用的前缀，同亮度会和标题抢视线；淡化只用 `dim` 这类相对语汇，不要写死具体颜色（深浅色主题都要成立），窄栏截断时别把 `dim` 涂进标题；**首行最左是关注圆点**（等待回答黄 > 执行中绿 > 未读新结果红 > 无；本窗口托管且「刚刚」仍有活动、无待办信号的会话也画绿点，禁止单独画青/蓝点；当前轮已原生结束（中断，或带完成标识的已完成）则不画这档绿点，新的执行中/等待信号仍优先，见 `docs/REMOTE_KNOWLEDGE_BASE.md` 结束状态规则；圆点必须跟上 Active sessions，共用 `resolve_active_marker`，禁止为对齐去砍看板「刚刚」档），独立会话卡圆点后接空格分隔的「项目 标题」（**不带冒号**），**组内子项不写项目名前缀**（项目已在组卡第二行）；**无圆点时不留占位空格**，标题直接顶到最左并吃满整行宽度（截断宽度按有无圆点取 `width - 2` 或 `width`）；第二行运行时靠右、第三行时间靠右。**第三行时间按新鲜度分四档亮度**（半小时内 / 三小时内 / 一天内 / 更早），最新一档与标题同色（着重显示），越旧越暗；档位色一律用 `$foreground` + 透明度经组件样式解析，禁止写死颜色或退回单级 `dim`，也禁止让时间行带上自己的背景色（会盖掉整行的选中/分屏底色）。圆点不得参与排序、筛选或计数。圆点字符 `●` 的 East Asian Width 是 Ambiguous：Rich 按 1 格算，把它放进首行文本流时必须让宽度预算与 Rich 一致，不要按「CJK 字体看起来占 2 格」去补偿；出图时 `docs/screenshots/capture.py` 只给「内容恰为该字形」的独立 `<text>` 换成非 CJK 等宽族来修观感。当前基准：搜索框高 2、新建项高 2、活动看板高 3（首行名称、第二行上一页/下一页、第三行留白）、会话卡与会话组卡高 3；组卡第一行只保留展开/收起三角、可选置顶标记和分屏显示名（用户起的名字，否则成员标题以 ` + ` 拼接；不显示水果名/emoji，规则见 `docs/TERMINAL_UI_KNOWLEDGE_BASE.md`「侧边栏会话组与置顶」），**不得画关注圆点**；组卡第二行「项目 · 分屏 · N 个会话」与显示名同列左对齐；第三行留白（不写时间，避免与成员卡重复）；成员用贴左缘的半角框线 `├─ `/`└─ `（续行同列 `│`，无前导空格、不用 dim；禁止混全角竖线以免三行卡之间断线）且不在顶层重复。分栏时左栏固定宽 39（`ui/main_screen.py` 的 `LIST_PANE_WIDTH`），内层 `#sidebar-sticky` / `#sidebar-scroll` 的垂直/水平 `scrollbar-size` 均为 0（滚动条不占列宽，键盘与滚轮滚动照常）。**筛选框、＋新建和活动看板固定不滚**（`#project-search` 在列表外，`＋ 新建` 与活动看板在 `#sidebar-sticky`）；置顶块、Pinned 线与未置顶日期段都在 `#sidebar-scroll` 里一起滚——置顶只改变排序（钉在列表最上），不冻在视口里，钉再多也不会裁切或挤掉未置顶；指针在固定头上滚轮仍带动会话列表、顶部不动。**改左栏宽度必须同步改 `selftest.sh` 的 IME 光标锚定断言**——那里把面板起点硬编码成第 40 列（`expected_x=$((40 + inner_x))`，即 39 宽 + 1 列空隙），只改宽度会让端到端冒烟直接判失败。**右栏分屏（≥2 格）时，侧边栏给当前会话组整组铺底（Group 行 + 全部成员），激活会话再重一档**；光标停在组卡上时整组贴 `-group-selected`（成员与组卡同档高光），激活成员再叠 `-split-active`。底色标在 `ListItem` 上，组标题 / 组标题且光标在其上 / 激活格 / 激活格且光标在其上四级必须单调递进。置顶用 `p` / `Ctrl+P`：独立会话可单独置顶，会话组只能整体置顶（组内成员改为整组置顶）；`Ctrl+P` 是与 `Ctrl+F` 同级的全局键，右栏实时格持焦时仍可用；已关闭 Textual 命令面板，不要再展示 `^p palette`；未置顶区跟 SessionStore 稳定顺序走（进入后已有项不因 mtime 更新而飘；新建会话仍插最前），只有置顶块固定在最上；**置顶与未置顶都非空时中间插一行居中 `Pinned↑`/`置顶↑` 的 `$primary` 蓝横线；未置顶按本地日历日切桶（今天 / 昨天 / 近 7 日内其余各日用星期几 / 更早合成一桶且不标 Older；桶内不重排），命名桶后面还有内容时才在该桶末尾插线（标签为 `Today↑` 这种「名字 + 向上箭头」，标明上面这一段；箭头渲染时追加，不进词条）；高 1、disabled、键盘跳过；禁止在日期分隔线上写 Older/其他标签。**默认只展开今天与昨天**；更早收成高 3 的三层叠卡（`OlderStackCard`：带框正面卡 + 右侧两道叠边，框内 ▶/▼ 与数量；禁止裸 ─ 装饰线），点击或回车展开/收回，项目筛选非空时自动展开**。细则见 `docs/TERMINAL_UI_KNOWLEDGE_BASE.md` / `docs/MAINTAINER_GUIDE.md`「界面」节。
- `agent_api.py`（`corral list`/`search`/`show`/`export`/`share`/`context`/`describe`）是只读数据接口，禁止新增任何执行/拉起副作用命令——corral 只负责把会话数据交出来，怎么用是调用方的事。暴露更多可见性字段（如运行中会话的 `live`/`pid`）不违反这条约束，只要新字段本身来自扫描/只读探测、不触发任何拉起或写操作；真正"接管/下发指令给运行中会话"的能力不属于 corral，留给调用方基于这些数据自行实现。命令参数与 `corral describe` 的输出必须共用同一份 `COMMANDS` 定义，不能各写一份导致漂移。新增或修改子命令时同步 `docs/SKILL.md`。
- Agent 接口里 `list`/`search` 的 `--limit` 固定表示每个运行时的扫描深度，`--top` 才表示最终返回条数；`--compact` 必须同时做到紧凑 JSON 和精简默认字段。改这三个参数或 `show --out` 大结果落盘行为时，同步 `corral describe`、`docs/SKILL.md` 和 `docs/MAINTAINER_GUIDE.md`。
- 会话保活（`keepalive.py`）是运行时无关的启动包装层，只在 `registry` 生成 `LaunchPlan` 之后、`execute_launch` 之前介入，禁止塞进 `runtime/` 某个具体适配器，也禁止让适配器感知 tmux 的存在。改保活匹配/回收逻辑（含软上限压力回收）、问「最大进程数 / 上限改成 N 个 / 本机有没有超限」前先读 `docs/MAINTAINER_GUIDE.md`「会话保活」节（查超限只数保活托管会话，禁止对真实保活调用回收）。`corral claude`/`corral codex` 直启子命令默认带 `_DirectLaunch` 进 TUI、经 `embed.host_session` 托管（与界面内「新建会话」同一路径），托管成功后必须立即登记侧边栏占位卡，禁止等待运行时写出首条历史；扫描器随后发现真实历史、占位卡转正时，侧边栏选中态与右栏分屏键必须一起迁移，不能退回「＋ 新建会话」空态；仅非真实终端 / `--no-keepalive` / 内嵌不可用时退回 `keepalive.enabled`/`wrap_plan` + `execute_launch` 旧路径（保活的第三个调用点，与 TUI 的 `_launch()` 复用同一套开关语义）。
- 内嵌面板（`embed.py`）是与 `keepalive.py` 平级的运行时无关层：不 attach，用 `capture-pane` 拿画面、经常驻 `tmux -C attach` 控制通道（`ControlChannel`）送按键与修改类命令（通道死亡自动回退外部 fork），把托管在保活 socket（`corral-*`/`sc-*` 命名空间）里的会话渲染进 TUI 右半屏。控制通道按 tmux 会话名维护通道池，多分屏可同时存活；`close_channel(name)` 只关指定格，省略 name 时关闭全部。适配器不感知本模块；`ui.main_screen.MainScreen` / `ui.split_pane_area.SplitPaneArea` / `ui.embed_pane.EmbedPane` 是主要调用方。tmux 是软件级硬依赖（TUI 与直启启动时检查，缺失即报错退出；agent_api 只读子命令不受影响）。环境变量新名为 `CORRAL_*`（`CORRAL_KEEPALIVE`、`CORRAL_KEEPALIVE_IDLE_HOURS`、`CORRAL_KEEPALIVE_MAX_SESSIONS`、`CORRAL_KEEPALIVE_PRESSURE_IDLE_MINUTES`、`CORRAL_TITLE_MODEL`、`CORRAL_RUNTIME`、`CORRAL_SESSION_ID`），旧名 `SC_*` 一律保留兜底读取/注入，不得删除兼容路径。`CORRAL_TITLE_GENERATOR` / `SC_TITLE_GENERATOR` 已退役：源码不再读取，禁止加回当生成开关或「保留兼容」。Silent automatic reclaim of inactive hosted sessions is ON by default (owner decision 2026-09-29, supersedes the 2026-09-14 disabled-by-default rule): no notifications, one audit event per reclaim, never on TUI startup or session creation, protects working/waiting/viewed/pinned/attached sessions; `CORRAL_RECLAIM=0` disables; manual termination remains available. Contract in `docs/MAINTAINER_GUIDE.md`「会话保活」。
- 运行时跳过权限审批的危险启动参数（如 Claude 的 `--dangerously-skip-permissions`、Codex 的 `--dangerously-bypass-approvals-and-sandbox`）必须声明为对应适配器的 `auto_approve_args` 类属性，不得在 `build_resume_plan`/`build_new_plan`/直启透传等多处各写一份字面量字符串；入口层和 `registry.build_passthrough_plan` 只负责按需拼接这个属性，不感知具体参数内容。
- **助手模型与推理强度完全归用户配置所有**：corral 的恢复、接力、空白新建、直启、命令拦截和后台标题生成都不得内置或注入模型/推理强度，必须继承对应助手自身的全局默认。唯一例外是用户明确给直启传入的参数，或用户明确设置仅用于标题生成的 `CORRAL_TITLE_MODEL`（兼容旧名 `SC_TITLE_MODEL`）；不得为了省额度或“质量更好”私自选模型。
- **「默认跳过全部权限问询」是本项目的既定产品默认，不是待讨论选项**（机主 2026-08-01 明确拍板）。凡是 corral 拉起运行时的路径——原生恢复、跨运行时接力、空白新建、直启透传，以及未来的命令拦截/shim 入口——都必须自动垫上该运行时的放行参数，让用户拿到的是开箱免打断的体验。新增运行时时，找出并验证它的放行参数属于接入工作的必做项，不是可选增强；找不到就在维护指南里如实记录能力差距，而不是默默留空。放行参数在某些运行时里只属于部分子命令、或对位置敏感（OpenCode 的 `--auto` 两者都占），这类规则写进适配器的 `compose_passthrough_argv`，不要塞进注册表。**禁止把它改成默认关闭、需显式开启，也不要再以「不安全」为由向机主重复征询确认**——理由是当前各家模型自身的谨慎度已足以覆盖日常风险，机主已知悉并接受。唯一允许不加的情形是运行时自身硬性拒绝该参数（如 Claude 在 root/sudo 下带 `--dangerously-skip-permissions` 会直接退出；OpenCode 的 `stats`/`export`/`auth` 等子命令不认 `--auto`），这类情形按"加了就起不来"的事实判断，与安全权衡无关。**运行时旧版本不支持放行参数不属于此列**：按"该升级那个助手"处理，不为旧版保留降级分支（机主 2026-08-04 拍板）。

## 发版要求

- 产品行为或代码修复完成后必须发布新版本（补丁位递增），同步 `pyproject.toml` / `Cargo.toml` / `Cargo.lock` / `src/corral/__init__.py`。以 `release: vX.Y.Z …` 提交、打 annotated tag、推送 `github` 与 `origin`，再运行 `bash scripts/publish-release.sh`。纯文档或规则整理且不改变产品行为时可不 bump 版本。
- 完工时按全局无条件 ship-all 规则集成、验证并交付完整当前工作区，包括其他 Agent 的改动；脏树、foreign WIP 或并行发布窗口不构成只发布部分内容或延后的理由。不得搬走、隐藏、丢弃其他人的文件。失败检查要在完整工作区上查明并修复，不能缩小验证或发布范围。
- 本机发布路径必须完成完整验证和干净安装核验，不以 tag 或 CI 队列状态代替实际发布；具体顺序与远端核对见 `docs/MAINTAINER_GUIDE.md`「开源发布 / 多 Agent 并行时的发版卫生」。

## 验证要求

- 耗时检查前，先读 [开发环境指南](docs/DEVELOPMENT_ENVIRONMENT_GUIDE.md) 做依赖与运行环境就绪检查，再读 [测试环境指南](docs/TEST_ENVIRONMENT_GUIDE.md) 选择隔离验收入口。开发、测试、构建依赖须有明确来源；将环境/安装失败与产品断言或真实 UI/终端失败分开记录。重复失败要根据日志、夹具前置条件和环境证据复核策略，不得无限原样重跑或因此跳过完整验证。
- 完整验证结果只有在相关源码 / 构建输入与环境、解释器、依赖版本指纹都一致时才能复用。`ci-test.py` / `ci_stamp.py` 是唯一完整套件戳机制：戳文件同时绑定源码指纹与环境指纹（`uv.lock` 字节 + checkout `.venv` 解释器版本与已装分发集合）；旧的纯源码戳永不匹配，任一变化即重跑全套。保留全部 UI、真实终端和干净安装门禁，细节见 `docs/MAINTAINER_GUIDE.md`「Readiness and dependency handoff」。
- 改动代码、界面或运行时适配器后至少执行（先确认环境就绪，未就绪时先 `prepare`）：

  ```bash
  python3 scripts/dev_env.py check --repo .
  python3 -m compileall -q src/corral tests
  env -u TEXTUAL_DISABLE_KITTY_KEY python3 scripts/ci-test.py
  ```

  `ci-test.py` 在 checkout `.venv` 已存在且就绪时自动重进该解释器跑全套（CI 无 `.venv` 路径不变；`.venv` 存在但未就绪则直接失败并指引 `prepare`，不在错误依赖上跑）。该脚本与 CI 一样先跑 Ruff 再跑全量单测。不要用单测子集、跳过 UI / 终端集成或手动跳过标记替代发布门禁。
- 改动扫描、标题或界面代码后，仍须独立测量首屏扫描耗时并如实记录（该指标不再是阻断线）：

  ```bash
  python3 -c "import time; from corral.runtime import default_registry; r=default_registry(); t=time.perf_counter(); r.scan_all(50); print(f'{(time.perf_counter()-t)*1000:.0f}ms')"
  ```

- 任何用户可见 UI 改动都要走真实界面并检查维护的截图；需要真实终端 / tmux 行为的改动也必须走真实 tmux 验收。会话扫描、标题、预览改动要抽查真实记录，标题生成还要核验安装后的真实命令。完整范围、隔离边界、清洁安装及日志要求见 `docs/MAINTAINER_GUIDE.md`「UI, terminal, and data acceptance」与 [测试环境指南](docs/TEST_ENVIRONMENT_GUIDE.md)；截图及日志不得含真实会话正文。

## 本机入口

产品代码在 `src/corral/`（标准 src-layout）。不要再直接跑已删除的根目录 `corral.py`。

**改名后还能执行 `pickup`（哪怕 `corral` 已经能用）：** 不是兼容别名。`[project.scripts]` 只注册 `corral`，装新包名不会拆掉旧入口。本机残留常见不止一种，必须逐项清，禁止加回 `pickup` console script：

1. `~/.local/bin/pickup`：改名前 pip/pipx 入口，或 27 字节的 `exec corral "$@"` 转发包装（包装还在时 `pickup --version` 看起来像新命令在跑）。
2. `~/Library/Python/*/bin/pickup` console script。
3. Homebrew Python `site-packages` 里的 `pickup/` 包、`pickup.py`、旧 `dist-info`（`python3 -m pickup` 仍能启动）。PEP 668 下 `pip uninstall pickup` 会直接失败，要把这些目录/文件删掉。
4. `~/.zshrc` 的 `# >>> pickup shim >>>` 块，以及 `~/.cache/pickup/`、`~/.cache/corral/shim/pickup-shim.sh`。指向已删仓库 `Codes/pickup` 的 editable `pickup.pth` 一并删。

清完核对：`command -v pickup` 为空、`python3 -m pickup` 报 `No module named pickup`、`corral --version` 仍指向本仓库。只跑 `dev-install.sh` / `pip uninstall` 不够。细则见 `docs/MAINTAINER_GUIDE.md` 内嵌面板节 2026-08-23 条。

**开发机一次性装好（推荐，彻底避免 pipx 旧副本）：**

```bash
cd cli
bash scripts/dev-install.sh
# 把本仓库 editable 装进「corral 命令实际用的解释器」（含 pipx venv）
corral --version   # 应看到 package_file 落在本仓库 …/cli/src/corral/
corral --limit 5
```

之后改 `src/` 立刻生效，无需反复 `force-reinstall`；**仍须重启**已打开的 TUI。

备选（无 pipx / 只想装到当前 python3）：

```bash
cd cli
python3 -m pip install --user --force-reinstall --no-deps -e .
corral --limit 5
# 等价：python3 -m corral --limit 5
```

**验收必须核对「`corral` 命令实际加载的包」，不能只信系统 `python3 -c "import corral"`。** 本机常见：`~/.local/bin/corral` shebang 指向 **pipx venv**，而 Cursor / 普通 `python3` 可能 import 到仓库源码——单测已绿、敲 `corral` 仍是旧包。核对：

```bash
corral --version                 # 或 corral diagnose → data.package_file / stale_source_warning
command -v corral
# pipx 的入口常是 /bin/sh 包装器，不能只看第一行；以 corral --version 的 python/package_file 为准
```

在仓库目录内启动 TUI 若加载了别处的副本，stderr 会打 `[corral] …改源码不会生效` 告警。期望 `package_file` 落在本仓库 `cli/src/corral/`（editable）或你有意使用的 site-packages。样式自检：`corral diagnose` 的 `runtime_label_style_claude` 应为 `bold #D97757`。

## 领域地图（doc-init）

<!-- 覆盖度复核基线：2026-08-01 · 源码指纹 扫描 140 文件 / Python 83 · Rust 1 / 1 子模块 · 基线版本 0.24.33 -->

| 领域 | 入口锚点 |
|------|---------|
| 终端界面 | src/corral/ui/ · src/corral/activity_board.py · src/corral/cli.py · src/corral/display.py · src/corral/theme.py · src/corral/store.py · src/corral/i18n.py · src/corral/split_layout.py · src/corral/ui_prefs.py |
| 会话关注状态 | src/corral/attention.py · src/corral/attention_signals.py · src/corral/cursor_observer.py · src/corral/store.py · src/corral/ui/ |
| 会话全文搜索 | src/corral/search.py · src/corral/ui/search_modal.py |
| 内嵌实时终端 | src/corral/embed.py · src/corral/ui/embed_pane.py |
| 会话扫描与对话内容 | **真源 SessKit**；`src/corral/scan/` 别名 · src/corral/transcript.py · src/corral/models.py · src/corral/runtime/ · docs/SESSION_SCANNING_KNOWLEDGE_BASE.md |
| 跨助手接力与启动 | src/corral/runtime/ · src/corral/runtime/pi.py · src/corral/models.py |
| 新助手接入 | src/corral/runtime/ · src/corral/scan/ · src/corral/runtime/pi.py · src/corral/scan/pi.py |
| 性能、派生缓存与原生加速 | src/corral/cache.py · src/corral/cache_cli.py · src/corral/native.py · src/corral/schedprio.py · src/corral/bootstrap.py · rust/lib.rs · Cargo.toml · scripts/benchmark.py |
| 可观测与诊断 | src/corral/observe.py · src/corral/agent_api.py |
| 会话保活 | src/corral/keepalive.py |
| 直启子命令 | src/corral/cli.py · src/corral/projects.py |
| 命令拦截（shim） | src/corral/shim.py · src/corral/bootstrap.py · src/corral/runtime/registry.py |
| 标题补全 | src/corral/titles.py · src/corral/titlegen.py |
| Agent 只读查询 | src/corral/agent_api.py |
| 开源发布与一键安装 | install.sh · .github/workflows/ · scripts/publish-release.sh |
| CI 流水线 | .github/workflows/test.yml · scripts/ci-test.py · .githooks/pre-push · scripts/install-git-hooks.sh |
| 客户端自动更新 | src/corral/updater.py · src/corral/ui/update_toast.py |
| 隐私与本地数据边界 | PRIVACY.md |

## 待补充知识库（doc-init backlog）

（当前无待补充项；会话保活、标题补全、Agent 只读查询、直启、开源发布、客户端自动更新仍以维护指南 / SKILL 为主，需要独立知识库时再登记。）
