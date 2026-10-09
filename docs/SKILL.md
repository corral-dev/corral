---
name: corral
description: Query local Claude Code, Codex CLI, OpenCode, Cursor Agent CLI, and Pi session history through the `corral` CLI — list recent sessions, search by topic, read a session's conversation, export a shareable transcript with tool calls and thinking, or build a handoff context package to continue interrupted work. Read-only, no side effects.
---

# corral：本地编程会话数据接口

`corral` 扫描本机 `~/.claude/projects/`、`~/.codex/sessions/`、`~/.pi/agent/sessions/`、`~/.cursor/chats/` 和 OpenCode 的
SQLite 数据库（`~/.local/share/opencode/opencode.db`，只读打开）下的会话历史，为大模型 Agent
提供结构化查询命令。**这些命令只读、无副作用**：不会拉起新会话、不会自动接续任务、不会修改
任何历史文件。拿到数据之后要做什么（继续任务、汇总给用户、转发给另一个 Agent）由调用方决定。

所有命令输出统一 JSON envelope，写到 stdout：

```json
{"ok": true, "data": {...}, "error": null, "meta": {"version": 1}}
```

失败时 `ok` 为 `false`，`data` 为 `null`，`error` 包含：

- `code`：程序可判断的错误分类（`usage_error` / `not_found` / `ambiguous` / `history_unavailable` / `not_resumable` / `internal_error`）
- `message`：人类可读的错误说明
- `hint`：建议的排查方向
- `next_commands`：可以直接执行的后续命令列表（绝对入口身份，照抄即可；不要改写成裸 `corral`，以免命中 PATH 上另一份旧副本）

退出码：`0` 成功、`1` 一般失败（含未预料的 `internal_error`）、`2` 用法错误（参数不对，含未知 `--fields`）、`3` 会话不存在、`5` 会话标识有歧义。
不要只看 stdout 是否有内容来判断成功，检查退出码或 `ok` 字段。

## 安装与探针（CLI 缺失时不要停下问人）

调任何子命令前先确认入口与能力。`corral` 不在 PATH 上时，按下面可信源装到用户目录后继续原任务（用户要这个能力即已授权用户目录安装；`sudo`/系统目录/覆盖已装版本仍需显式授权）：

- 可信源：`github.com/x0c/corral`（`corral` 包）。安装渠道三选一：`pipx install corral`、`brew install x0c/corral/corral`、源码树内 `bash cli/scripts/dev-install.sh`（editable，供开发）。
- 装完先钉绝对路径并验能力（之后全会话只用该绝对路径；`next_commands` 给的即是）：

```bash
CORRAL_BIN="$(command -v corral)"          # 钉住本会话唯一入口
"$CORRAL_BIN" describe >/dev/null && echo describe-ok
"$CORRAL_BIN" diagnose | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['ok']; print('diagnose-ok', d['data']['version'])"
```

- `diagnose` 的 `package_file` / `install_channel` / `stale_source_warning` 告诉你当前跑的是哪份拷贝；源码树里开发却加载了别处副本时会有告警，以告警为准换入口。
- `diagnose` 的 `keepalive_server` 报告保活 tmux server 的调度档（`priority`、`interactive_job`、`clamped`）。`clamped: true` 表示托管助手整体被 macOS 限流（新会话起得慢、同样的活比 Corral 外慢数倍）；只能重启一次 server 解决，见 `docs/MAINTAINER_GUIDE.md`「Keepalive server scheduling class」。
- 源码树 editable 安装用于维护和开发，不会替代环境准备。跑耗时检查前先用 `python3 scripts/dev_env.py check --repo .` 确认就绪（未就绪时跑 `python3 scripts/dev_env.py prepare --repo .`；只诊断用 `doctor --repo . --json`，命令经 `run --repo . -- <cmd>` 走 checkout `.venv`），再按 [测试环境指南](TEST_ENVIRONMENT_GUIDE.md)（`python3 scripts/acceptance.py --json`，只演练用 `--dry-run`）做隔离验收；focused acceptance 不代替完整 UI/终端及干净安装门禁。安装/依赖失败应先按环境问题处理，不要误判成会话查询结果为空。

## 命令

公开运行时仅 Claude Code、Codex、OpenCode、Cursor 和 Pi；Kimi 兼容实现 dormant，不在公开入口中展示。

跑 `corral --help` 查看全部公开命令；`corral <command> --help` 查看具体用法。
跑 `corral describe` 获取会话数据命令的机器可读参数说明（与实现同源，不会漂移）；
`corral describe <command>` 看单个命令的完整参数和输出字段。

| 命令 | 用途 |
| --- | --- |
| `corral list [--runtime R] [--limit N] [--top N] [--compact] [--status S] [--cwd 子串] [--live] [--keepalive] [--fields a,b]` | 结构化列出会话 |
| `corral search <关键词...> [--deep] [--runtime R] [--limit N] [--top N] [--compact] [--live] [--keepalive] [--fields a,b]` | 按主题找会话 |
| `corral show <会话> [--messages N \| --full] [--compact] [--out 路径] [--fields a,b]` | 会话详情 + 对话内容 |
| `corral share <会话> [--out 路径] [--compact]` | 导出含 thinking / 工具调用的统一 transcript，给其他 Agent 做元认知 |
| `corral export [--since T] [--until T] [--runtime R] [--status S] [--cwd 子串] [--limit N] [--out 路径] [--compact]` | 导出某时间范围内所有会话的完整对话，合并为一个 JSON |
| `corral context <会话>` | 生成接续该会话所需的上下文数据包 |
| `corral plan continue <会话> --instruction <文本>` | 生成带新指令的非交互式原生续接计划；只返回数据，不执行 |
| `corral describe [command]` | 查看命令 / 参数 / 输出字段说明 |
| `corral diagnose` | 只读诊断：events.log / embed-error.log / `last_error` / 截图目录 / tmux / 保活 server 调度档（`keepalive_server`）/ 配色自检 / **安装路径**（`package_file`、`install_channel`、`stale_source_warning`）；不启动 TUI |


### 会话标识（`<会话>` 参数）

支持完整会话 ID、ID 前缀（如 `8892cd3d`）、或带运行时限定的 `runtime:id`（如 `claude:8892cd3d`、
`opencode:ses_0ae26219`、`pi:<session-id>`、`cursor:<chat-uuid>`）。
前缀在多个运行时之间重复时会返回退出码 5（`ambiguous`），`error.next_commands` 里给出具体候选
的 `corral show runtime:id` 命令，照着执行即可消歧。

## 典型流程

**按主题找到会话并接续未完成的工作：**

```bash
corral search 天气 app                    # 快速搜标题/首尾消息/工作目录
corral search 天气 app --top 3 --compact  # 只取最相关的 3 条，减少 token
corral search 天气 app --deep             # 快速搜没结果时，搜全部对话内容（较慢）
corral show <候选会话的 short_id>          # 确认是不是要找的那次会话
corral share <候选会话> --out /tmp/corral-share.json  # 含工具调用与 thinking 的统一 transcript
corral show <候选会话> --full --out /tmp/corral-session.json  # 纯文本对话（不含工具调用）
corral context <会话>                     # 拿到 history_path / suggested_prompt / resume_command
corral plan continue <会话> --instruction "继续完成剩余工作并汇报结果" # 只生成外部执行器可运行的计划
```

`corral context` 返回的 `resume_command` 是同运行时原生恢复该会话的 shell 命令（可能为 `null`，
比如原历史文件已被删除）；`suggested_prompt` 是跨运行时接力时可以直接复用的首条提示词，内含
原会话历史文件路径、格式提示和从原会话自动提取的对话摘录（原始需求 + 最近数条对话，
截断版）——摘录给目标运行时一个任务与进展锚点，原始历史文件仍是权威来源，目标运行时应以摘录
为线索去读取历史、判断已完成和未完成的部分。

`corral plan continue` 用于调用方需要把同运行时续接交给后台执行器时。它会依据运行时适配器返回
`session_ref`、`runtime`、原 `cwd`、能力边界和 `launch`；其中 `launch.argv` 是**参数数组**，新指令
作为一个独立元素保留，调用方必须以 `execve` / `subprocess` 的 argv 形式执行，不能重新拼成 shell
字符串。`launch.cwd` 为 `null` 时表示原目录已不可用，调用方应自行拒绝或选择安全的工作目录。

该命令同样只读：不会启动 CLI、不会写入原会话历史、不会创建新会话，也不会消耗模型额度。返回的
`capabilities.execution=external_only` 明确表示执行责任属于调用方；`corral` 不提供执行、停止或向运行中
会话下发消息的能力。内置 Claude/Codex 计划使用各自的非交互式原生续接入口，以便外部后台任务接收
完成输出；人类在 TUI 中的原生交互恢复行为不受影响。

**列出某个项目最近的会话：**

```bash
corral list --cwd my-weather-app --limit 20
corral list --cwd my-weather-app --limit 80 --top 5 --compact
```

**只要还没回复的会话：**

```bash
corral list --status pending
```

## 管家编排典型流程

**找到当前正在运行的 CodingAgent,判断能不能直接下指令:**

```bash
corral list --live --compact                 # 一步拿到「现在有哪些会话进程真的在跑」
corral list --live --status pending --compact # 更进一步：正在跑、且已在等你回复的
```

`live` 是按进程真实判活（pid 存活/写文件锁定），不是文件时间推断；`status` 是最后一轮角色推断的
会话内容状态。两者组合判断能不能打扰：`live=true` 且 `status=done` 说明进程还开着但已经把当前
任务处理完，可以直接接着聊；`live=true` 且 `status=pending` 说明正忙着等你回复，插话前先看
`last_user`/`last_agent` 摘要搞清楚它在问什么。`live=false` 的会话只能走 `resume_command` 重新
拉起一个进程，不能"接管"——corral 不提供向运行中进程注入输入的能力（本文档开头即说明：corral 只读、无
副作用,拿到数据后要做什么由调用方决定）。

`keepalive=true` 的会话额外要注意：它的进程挂在 corral 的后台保活层里，`live` 通常也是 `true`，但
`resume_command` 此时不安全——执行它会另起一个和保活进程抢同一份会话文件的新进程。这种会话只能
由人类回到 `corral` TUI 按 Enter 接回现场，管家 Agent 判断"能不能直接下指令"时应把 `keepalive=true`
当作"这条会话已经有人在管，只能提示人类去接，不要建议或代为执行 resume_command"的信号。

`attention` 是界面关注态，与侧栏圆点同源：`waiting`（等你回话）/ `working`（正在干活）/
`unread`（有未读新结果）/ `none`。它只描述这条会话此刻的关注信号，**不是**「该不该阻止电脑休眠」
这类产品结论。本机工具若要根据会话决定合盖后是否继续跑，应自己组合 `live`、`keepalive`、
`attention`、`mtime`，并叠电量、发热等安全策略；不要让 corral 新增一条布尔「有没有活跃会话」命令。

只要 corral 托管着、进程还在跑的会话：

```bash
corral list --live --keepalive --compact
```

`list`/`search` 默认输出已经带 `last_user`/`last_agent`（最近一轮真人消息和助手回复，硬截断精简），
多数情况下看这两个字段就够判断"这条会话在干嘛"，不必为每条候选都跑一次 `corral show`。

## 字段说明要点

- `title`：只读已生成的标题缓存或本地兜底标题，不会触发新的标题生成（不花账号额度）。
- `live` / `pid`：`live` 是运行中会话的进程是否真实存活（Claude 用 pid 注册表 + `os.kill`，Codex
  用 `pgrep`+`lsof` 探测持有对应会话文件的进程，OpenCode 用命令行 `-s`/`--session` 或完整托管会话
  id 精确绑定，同一目录可同时标多条；`opencode run` 不算运行中。Kimi 用命令行 `-S`/`--session`
  或完整托管会话 id 精确绑定，同一目录可同时标多条；`kimi -p` / `server` / `web` 不算运行中），
  不是根据文件时间猜的。
  `pid` 只在 `live=true` 时非空，供调用方定位/给该进程发信号；corral 本身不提供拉起/接管等副作用
  命令，`pid` 只是可见性。
- `keepalive`：会话是否正挂在 corral 自己的后台保活（tmux）里（人类在 `corral` TUI 里用 Enter/`a` 启动会话时
  默认会经过这层，SSH 断开也不中断）。为 `true` 时该会话的进程实际跑在保活层里，`resume_command`
  会另起一个直接抢同一份会话文件的新进程，不应该在这种情况下使用；corral 不提供从命令行直接"接管"
  保活会话的能力（这属于交互式 TUI 的 Enter 键行为），调用方需要接管时只能提示人类回到 `corral` TUI 操作。
- `attention`：英文枚举 `waiting` / `working` / `unread` / `none`，与终端界面侧栏圆点同源；程序判断用这个
  字段。不是「是否活跃到该阻止休眠」的结论，调用方按自己的产品规则组合本字段与 `live` / `keepalive` / `mtime`。
- `last_user` / `last_agent`：最后一条真人消息和助手最后一轮回复的硬截断摘要（约 120 字），用于
  快速判断会话在干嘛；需要完整对话仍然用 `corral show`。
- `status`：英文枚举 `done` / `pending` / `aborted` / `unknown`，程序判断用这个字段，不要解析
  `status_tag`（中文 + emoji，只给人看）。
- `resumable` / `resume_command`：是否能生成同运行时原生恢复命令，以及可直接执行的恢复命令；这些字段
  只基于扫描结果和运行时适配器生成，不会额外读取会话全文。
- `session_ref` / `launch`：仅 `plan continue` 返回。`session_ref` 是带运行时的唯一标识；`launch.argv`
  是不经 shell 解释的启动参数数组，`launch.cwd` 是推荐工作目录。不要把 `argv` 拼为 `resume_command`
  风格的字符串，也不要让 `corral` 代为执行。
- `score` / `matched_via` / `matched_fields`：仅 `search` 返回。`score` 是相关性分数，排序先按分数倒序，
  再按更新时间倒序；`matched_via` 是 `quick` 或 `deep`，兼容旧调用方；`matched_fields` 是命中的字段
  列表，如 `title`、`first_user_msg`、`conversation`。
- `mtime`：Unix 时间戳，按更新时间排序或做"最近"过滤时用这个，不要解析 `time` 的人类可读格式。
- `scanned` / `matched` / `returned` / `potentially_limited`（`list`/`search`；`export` 有除 `matched`/`returned` 外的同名字段）：零命中时先看这组数再下结论——`scanned` 是实际读到的会话总数（过滤前），`matched` 是命中数（`--top` 截断前），`returned` 是实际返回数。`count=0` 且 `scanned>0`、`potentially_limited=false` 才是"范围内干净"；`scanned=0` 是根本没读到；`failed_runtimes` 非空（{运行时: 失败摘要}）说明某家扫描抛异常，结论不可信；`truncated_runtimes` 非空说明某家读满了 `--limit`，更早的会话没读到，调大 `--limit` 再查。
- `--fields` 只接受 `describe <command>` 列出的字段：未知字段直接 `usage_error`（退出码 2）并在错误里列出可用值，照着改就行。不要传猜出来的字段名。
- `--limit` 控制的是**扫描深度**（每个运行时最多看多少条历史），不是"最多返回几条"——过滤条件
  （`--status`、`--cwd`、关键词）是在扫描出的这批里再筛选，如果确定目标会话较早，适当调大
  `--limit`（`show`/`context` 默认扫描深度是 200，比 `list`/`search` 的 50 更大）。
- `--top` 才是**结果数量上限**。给 Agent 调用时推荐同时传 `--limit` 和 `--top`：前者决定找多深，
  后者控制 stdout 体积。
- `--compact` 会输出无缩进 JSON；`list`/`search`/`show` 还会默认裁剪到常用字段。需要精确字段时用
  `--fields` 覆盖（`list`/`search`/`show` 均支持）——`--compact` 单独使用时的默认字段集不含
  `cwd`/`pid`，只要调用方需要这两项（如判断会话在哪个目录、能否对运行中进程发信号），必须显式传
  `--fields` 指名，不能只传 `--compact` 就假设拿得到。
- `show --full` 可能很大；需要完整历史时优先加 `--out <path>`，stdout 会只返回输出文件路径、字节数
  和消息数量，完整 JSON envelope 写在该文件里。`--out` 只写入普通文件（目录/不存在的父目录/特殊文件直接 `usage_error`）；已存在的同名普通文件会被覆盖，落盘前确认路径。
- `corral export` 是「按时间范围批量拿完整对话」的入口：等价于对区间内每条会话跑一次 `show --full`，
  再按最后更新时间正序合并成一个 JSON（`data.sessions[]`，每条含 `list` 全部字段 + `messages` 完整
  对话）。`--since`/`--until` 均为闭区间，任一侧省略即无界；时间可写 `2026-07-20`、`'2026-07-20 15:30'`、
  相对量 `7d`/`24h`/`30m`（距今）或 Unix 时间戳；只给日期时 `--until` 自动补到当天 23:59:59。范围内会话
  多时合并结果可达数 MB，**强烈建议加 `--out <path>`**——stdout 只回文件路径、字节数、会话数与消息总数，
  完整内容写在文件里。时间过滤按会话的 `mtime`（最后更新时间）判定。
- `corral share` 是给其他 Agent 做元认知 / 迭代用的统一 transcript：`data.schema` 为 `sesskit.transcript/v1`（兼容读旧 `corral.share/v1`），
  `data.events[]` 按原始历史顺序包含 `user_message` / `assistant_message` / `thinking` / `tool_call` /
  `tool_result`（工具参数与结果不截断）。**不要用 `show`/`export` 的 `messages` 充当这一用途**——那两条
  命令仍然只出纯文本。大结果同样优先 `--out`。`tool_result.status` 在 Claude 认 `is_error`、OpenCode 认
  `state.status`、Pi 认 `isError`；Kimi 目前只按 output 正文启发式，`isError=true` 仍可能是 `ok`。
  TUI 高级操作「导出会话」写出同一份 envelope 到 `~/.cache/corral/share/`（尊重 `CORRAL_CACHE_DIR`），
  并把绝对路径复制到剪贴板。

## 拿会话数据做总结 / 周报时的边界

`corral` 交出来的是**对话记录**，不是工作成果台账。下面 5 条是设计使然、不会改的产品边界；
用 `show` / `export` 的结果做周报、日报、工作总结前必须先按这些边界校正，否则结论会失真。
括号内是本机真实数据实测值（2026-08-01），供判断量级用。

1. **`show` / `export` 的对话里不含助手实际执行的动作。** 这两条命令的 `messages` 只有真人消息和助手的**文本回复**；助手改了哪些
   文件、跑了哪些命令、提交了什么代码，连同这些操作的结果**全部不在 `show`/`export` 里**——所有运行时一致
   （各扫描器的 `load_conversation` 只提取文本，不提取工具调用）。实测一条 17.7 MB 的 Claude 会话，
   导出后只剩约 19.6 万字符纯文本，被丢弃的 1683 次工具调用里含 361 次改文件、73 次新建文件、
   837 次命令执行（其中 46 条是完整的 git 提交 / 打标签 / 推送，提交说明本身就是最好的成果素材）。
   **后果**：只看 `show`/`export` 时，"索引已落地""改完了"这类话只是**口述**，无法据此核实真的改了、改了哪些文件。
   **怎么办**：需要工具调用、thinking 或改码证据时，用 `corral share <会话>`（统一事件流，含 `tool_call` /
   `tool_result` / `thinking`）；或用 `history_path` 自己去读原始历史文件（`context` 的
   `history_reading_hint` 说明该运行时的格式），或按下面第 5 条走 git 侧自查。
   `show`/`export` 的纯文本契约本身不会改。

2. **`title` 只能当索引，不能当工作内容。** 标题要么是运行时自己写的原生标题，要么是从首条用户
   消息首行兜底而来，所以经常是"好的 做流程设计计划""那还需要改动吗"这种没有信息量的句子。
   **不要按标题聚类、归类或直接抄进周报**；判断一条会话在干嘛，看 `last_user`/`last_agent`，
   不够就读 `messages`。

3. **`last_agent` 不保证有值，不能作为唯一分流依据。** 各运行时扫描 15 条的实测空值率：
   Cursor 15/15（扫描阶段不打开该运行时的对话库，这个字段恒为空串）、Kimi 9/15、Codex 4/15、
   OpenCode 1/15、Claude 0/15。更进一步，Cursor 与 Kimi 的部分会话**整条对话都取不到助手消息**
   （抽查 5 条：Cursor 3 条、Kimi 2 条只有用户提问侧）。
   **怎么办**：`last_agent` 为空时回退到 `last_user` + `messages`，不要判成"这条会话没内容"而跳过。

4. **`messages` 的 user 侧混着系统注入文本，需要自行过滤。** Claude 的 task-notification 类事件
   已在扫描层滤掉，但仍有几类会以"真人消息"的身份混进来（实测 Cursor 侧 206 条用户消息里有 16 条，
   约 8%）；2026-08 再扫约 2700 条后，还要加上 Codex / OpenConductor 那几类：
   - Cursor 的计划附件指令，特征是含 `Implement the plan as specified, it is attached for your reference`
     / `Do NOT edit the plan file itself`；
   - `Briefly inform the user about the task result…` 这类运行时内部提示；
   - Codex 的 `Implement the plan.`（整句）、`<skill>…` 全文展开、`<turn_aborted>`、
     `<subagent_notification>`；
   - OpenConductor 角色提示（`你是 OpenConductor 的…`、`【权威对话账本`、带 `用户最新补充` 的 `原始任务：`）。

   `$doc-update`、`/grilling` 和带 `<image>` 配文的提问是真人输入，不要当注入丢掉。
   corral 自身的跨运行时接力提示词（「你正在接力一个来自…」「You are picking up
   a session from…」）视作真人提问：Your prompts 小窗、手机对话时间线与本节都不再
   把它当注入过滤。

   TUI 的 Your prompts 小窗已经按同一套特征过滤（`is_injected_user_prompt`）；本接口的
   `show`/`export` 仍返回原文，调用方写周报时要自己剔除。

   **后果**：不过滤会把这些当成真人需求，凭空多出一堆"用户要求"。汇总前按上述特征剔除。

5. **没有任何成果结构化字段。** 不提供 commits / PR / 变更文件列表，也不会有——`corral` 只负责把
   会话数据交出来（见本文档开头的只读约定）。所以"做完了"只能从对话文字推断，很容易把
   **计划了**写成**做完了**。
   **怎么办**：真实成果去代码仓库侧取，用 `corral` 提供的 `cwd` 和 `mtime` 做锚点即可对齐：

   ```bash
   # 1. 圈出时间范围内涉及哪些工作目录（export 没有 --fields，落盘后再取需要的字段）
   corral export --since 7d --out /tmp/week.json
   python3 -c "import json;d=json.load(open('/tmp/week.json'))['data'];\
   print('\n'.join(sorted({s['cwd'] for s in d['sessions'] if s.get('cwd')})))"
   # 2. 再到各工作目录用 git 拿可核验的成果
   git -C <上一步列出的目录> log --since=7.days --stat
   ```

   两边对齐后，git 侧给"实际改了什么"，`corral` 侧给"为什么改、当时在讨论什么"。

## 与 OpenConductor 项目关联

`corral` 只提供 `cwd` / `cwd_display`（会话当时的原始工作目录），**不提供、也不应该提供**任何
"项目 ID"字段：OpenConductor 的项目标识是项目根目录（通常是含 `.git` 的目录）绝对路径的 SHA1
摘要（形如 `proj_a1b2c3d4e5f6a7b8`），既算不出来（`corral` 不知道 OpenConductor 的项目扫描结果），
也不能直接由 cwd 推导（会话当时的 cwd 可能是项目子目录，不等于项目根目录）。

调用方（如需要把某条会话关联到 OpenConductor 已注册项目的管家 Agent）应该：

1. 调 `oc projects --json`，拿到项目列表，每项含 `id`（`proj_` 前缀的项目标识）和 `path`（项目根目录绝对路径）。
2. 用 `corral` 返回的 `cwd` 去匹配：`cwd` 等于某个 `path`，或 `cwd` 位于该 `path` 之下（前缀匹配），命中的
   那一项的 `id` 就是要用的项目标识。
3. 不要在 `corral` 侧本地计算或猜测这个 ID——匹配逻辑属于调用方职责，不属于 `corral`。

## 写操作面（`remote` / `cache` / `shim`）：本文档的只读约定之外

上面的 `list`/`search`/`show`/`share`/`export`/`context`/`plan continue`/`describe`/`diagnose`
是只读的；下面这些**真的会改本机状态**，调用前先认：

- `corral remote on/off/pair/unpair/rotate-key/rename`：开关常驻服务、配对手机、解绑设备、轮换密钥、改名。`describe` 不覆盖它们，用 `--help` 看参数。副作用命令统一支持 `--dry-run`（只演练不更改；输出形状与真实执行一致，多 `dry_run: true`，`changed` 恒 false）：

```bash
corral remote off --dry-run --json   # 先看会关掉哪个 pid，再决定是否真关
corral remote unpair <id> --dry-run --json
```

  `pair` automatically turns the service on before generating a code; `--dry-run` reports `would_start_service` without changes. `on`/`off` 已是开关语义（重复执行收敛到 `changed: false`）；`unpair` 找不到设备报退出码 3（`not_found`）；`login`/`logout` 是鉴权引导步骤，不支持 `--dry-run`。`--json` 下 envelope 与读命令同形状（含 `error.code`/`hint`/`next_commands` 与 `meta.version`）。
- `corral cache clear --dry-run`：预览将清理的缓存，不删除；`corral shim install/uninstall --dry-run`：预览将改的 shell 配置，不写文件。
- `corral --json`（TUI 根命令的扁平数组）是兼容保留，新集成一律用 `corral list`（带状态枚举、`short_id` 与稳定 envelope）。

## 非 Agent 用法

不带子命令直接运行 `corral` 会打开交互式终端 TUI，需要真实终端，供人类手动选择会话；在非真实
终端环境下会自动退化为 JSON 列表（等价于 `corral list`）。旧版 `corral --json` 参数仍然保留，但字段
少于 `corral list`（没有 `status` 英文枚举、`short_id` 等），新集成建议直接用本文档的子命令。

## 界面异常排查（只读）

TUI 卡顿、卡死、按键极慢、侧边栏不刷新、内嵌面板异常时（本机可观测，无远程遥测）：

1. 先跑 `corral diagnose`，看 `data.last_error`（最近一次完整 traceback；无记录则为 null），并核 `corral --version`。
2. 读 `~/.cache/corral/events.log`（JSON 行：`scan_all` / `list_rebuild` / `host_session` / `capture_slow` / `error`）。卡顿时重点看 `scan_all` 的 `duration_ms`、`session_count`、`reason`：v0.24.185+ 签名命中应为 `refresh_live` + `cache_hit=true`；仍每 3–4s 全是 `refresh` 且耗时偏高 → 先确认已完全退出旧 TUI，再读 `docs/PERFORMANCE_KNOWLEDGE_BASE.md` 与 `docs/OBSERVABILITY_KNOWLEDGE_BASE.md`。
3. 需要更多历史时再读 `~/.cache/corral/embed-error.log`（后台线程 + 致命闪退的完整栈）。
4. 真机 TUI 内按 **F12** 导出当前画面到 `~/.cache/corral/screenshots/`（勿把含真实对话的截图提交仓库）。
5. 需要细日志时设 `CORRAL_DEBUG=1` 后重启 TUI。

<!-- 该文档整理/压缩于 2026-09-05 -->
