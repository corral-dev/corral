# Windows 兼容性评估（2026-08-27）

> 触发词：Windows 兼容、win32、ConPTY、WSL、无 tmux、本机安装包、PowerShell shim。
> 本文件是「为何不做 / 若重开从哪估价」的权威记录，不是待办清单。

## 裁定（2026-08-27）

机主看完阻碍面后明确决定：**不做 Windows 兼容（含原生 A/B/C 与专项 WSL 产品化工作）**。

- 支持范围维持现状：**仅 macOS 与 Linux**。
- 禁止为此开 Session Host 抽象、win 轮子、Windows CI、PowerShell shim，或把「无 tmux 半残 TUI」加回来。
- 用户若在 Windows 上自用 WSL2 跑现有 Linux 构建，属于用户侧自行解决，产品不承诺、不专项维护。
- 重开条件：机主显式推翻本裁定；重开时仍须先选档位（见下），不得默认上 C。

## 现状

- README / `install.sh` / 发布矩阵**明确只支持 macOS 与 Linux**；`install.sh` 在非 Darwin/Linux 上直接退出。
- CI 矩阵只有 `ubuntu-latest` + `macos-latest`；无 `windows-latest`，无 `win_amd64` 轮子。
- 产品核心路径（TUI 托管、内嵌 pane、断线保活、手机 remote 托管会话）**以 tmux 为软件级硬依赖**（`cli._require_tmux`，下限 3.2）。
- 极少数模块已有 win32 分支（如 `schedprio._windows_set_process_above_normal`），属于锦上添花，不构成平台支持。

## 曾评估的三档目标（归档，非待办）

| 档位 | 用户拿到什么 | 相对成本 |
|------|-------------|---------|
| **A. WSL 即 Windows 支持** | 文档/安装提示引导进 WSL2；产品仍是 Linux 构建 | 低（文档 + 探测文案） |
| **B. 原生只读 / Agent API** | `list`/`show`/`export` 等能扫历史；无托管 TUI | 中（进程探测 + 路径 + 锁 + 安装） |
| **C. 原生完整 TUI** | 与 macOS/Linux 同级：保活、内嵌、直启、remote | 极高（等于重做会话宿主层） |

成本判断：即使最便宜的 A 也要持续文案/探测维护；B/C 触及进程判活与会话宿主，投入远大于当前收益 → **整档搁置**。

## 阻碍面（按严重度，tmux 之外）

### P0 — 没有等价物就等于没有产品

1. **会话宿主层 = tmux 全家桶**（已知，但仍是最大项）  
   - `keepalive.wrap_plan` / `attach` / `reap_idle`  
   - `embed.ControlChannel`（`tmux -C attach`）、`capture-pane -e`、`send-keys`、`resize-window`  
   - remote 侧同样假设会话在 corral keepalive socket 里  
   - Windows 上没有可直接替换的 API：ConPTY 只管伪终端，**不提供**多会话命名空间、detach/reattach、按名抓帧、控制模式协议。  
   - 做 C 档 = 自研「Windows Session Host」（ConPTY + Job Object + 自写终端仿真或接第三方）并抽象出与 `keepalive`/`embed` 同级的后端接口。

2. **进程判活 / 宿主绑定栈全是 Unix 工具链**（即使有了某种 host 也会挡 B/C）  
   - `scan/common.py`：`pgrep`、`ps -axo`、`ps -p … -o command=`/`etime=`、`ps eww`、Linux `/proc`、macOS `lsof`  
   - `live_processes()` 在非 `linux`/`darwin` 上直接 `continue` → **Windows 上活会话永远扫不到 cwd**  
   - keepalive/`liveness.annotate` 依赖 `ps -eo pid,ppid` 祖先链把 agent pid 贴到 pane  
   - 替代：`psutil` 或 Win32 `CreateToolhelp32Snapshot` / `QueryFullProcessImageName` / 打开句柄枚举；所有扫描器与 annotate 要过一遍

### P1 — 功能面直接 ImportError / 行为残废

3. **POSIX 终端与文件锁**  
   - `theme.py`：无条件 `import termios` / `tty`（OSC 背景色探测）；Windows 上一 import 就炸  
   - `cli.py` 标题守护：`fcntl.flock` 单实例锁；需换成 `msvcrt.locking` 或 portalocker 类抽象  
   - remote：`os.kill(pid, SIGTERM/SIGKILL)`、daemon 注册 `SIGINT`/`SIGTERM`；Windows 信号语义不同，停守护进程要走另一套

4. **路径与数据目录约定**  
   - 缓存/状态普遍 `XDG_CACHE_HOME` → `~/.cache/corral`、`XDG_STATE_HOME` → `~/.local/state/corral`  
   - Windows 惯例是 `%LOCALAPPDATA%\corral`；不改也能靠 `Path.home()` 落到 `%USERPROFILE%\.cache\…`，但与其它 Windows 工具不一致，升级/清理体验差  
   - 助手历史多为 `~/.claude`、`~/.codex`、`~/.cursor/chats` 等——**若官方 Windows 版也写 user profile 点目录，只读扫描有机会直接可用**；须逐助手实测，不能假设

5. **文件权限模型**  
   - 大量 `mkdir(mode=0o700)` / `chmod(0o600)`（cache、attention、remote、sidebar、pi_identity）  
   - Windows 上 mode 大多被忽略；安全边界要改成「仅当前用户 ACL」或接受降级并文档化（Pi 身份设计已提到这点）

### P2 — 分发 / 开发体验 / 周边功能

6. **安装与发布**  
   - `install.sh` bash + curl；无 PowerShell/`winget`/ scoop 路径  
   - Release 资产命名只有 `macosx_*_universal2` / `manylinux_*` / `musllinux_*`  
   - Homebrew 配方与 Linuxbrew 无关 Windows  
   - 原生加速：需 `cp310-abi3-win_amd64`（及可能的 arm64）轮子 + CI 构建

7. **命令拦截 shim**  
   - 只支持 bash / zsh / fish 的 shell 函数注入（刻意不做 PATH shim）  
   - Windows 默认壳是 PowerShell / cmd；要另做 `profile.ps1` 函数或接受「Windows 无 shim」

8. **子进程会话语义**  
   - 标题守护 `start_new_session=True`、多处 Unix 进程组假设  
   - Windows 上对应 CREATE_NEW_PROCESS_GROUP / DETACHED_PROCESS，行为需逐条核对

9. **TUI / 终端能力差**（宿主解决后仍在）  
   - Textual 在 Windows Terminal 上大体可用，但 OSC 真彩探测、真实硬件光标可见性（IME 中文输入依赖）、SGR 鼠标、ConPTY 重绘语义与 macOS/Linux PTY 不同  
   - `selftest.sh`、截图流水线、tmux 集成测全部是 Unix shell——Windows CI 要另搭验收

10. **助手生态本身**  
    - Claude Code / Codex / OpenCode / Pi / Cursor Agent 在原生 Windows 上的支持度、历史路径、放行参数是否与 Unix 一致，是外部依赖；某助手只官方支持 WSL 时，Corral 原生 C 档也救不了它

## 明确「不是阻碍」或成本较低的项

- Textual 作为 UI 框架（相对 curses）已经跨平台得多。  
- `schedprio` 已有 Above Normal 分支。  
- `agent_api` 只读子命令**故意不检查 tmux**——B 档可先落在这条面上。  
- Python 3.10+ / 纯扫描逻辑（读 JSONL/SQLite）本身不绑定 OS。

## 若机主重开时的推进顺序

1. **先推翻上方裁定并选定档位**（A / B / C）。  
2. 若 A：README + `install.sh` 探测到 Windows 时提示 WSL；不改架构。  
3. 若 B：抽象进程查询（替换 `pgrep`/`ps`/`lsof`/`/proc`）→ 惰性化 `termios`/`fcntl` → 数据目录映射 → win 轮子与 CI → **仍拒绝启动托管 TUI**（保持「无 host 就退出」的诚实态度，或明确降级为只读列表）。  
4. 若 C：先写 Session Host 抽象（`keepalive`/`embed` 背后可插 tmux | winhost），再实现 ConPTY 宿主；在此之前不要把 Windows 写进 Requirements。

## 禁止事项

- **在裁定有效期内**：禁止开 Windows 兼容相关实现、CI 矩阵、安装通道或 Requirements 文案。  
- 禁止在未替换会话宿主前宣称「Corral 支持 Windows」。  
- 禁止为了过 Windows import 检查，把无 tmux 的半残 TUI 启动路径加回来（与现有「tmux 硬依赖、不优雅降级」架构约束冲突）。  
- 禁止用 Git Bash / MSYS 伪造成「原生支持」而不写明限制。

## 相关代码锚点

| 领域 | 入口 |
|------|------|
| tmux 硬依赖 | `cli._require_tmux`、`keepalive.py`、`embed.py` |
| 进程判活 | `scan/common.py`（`live_processes` 的 `else: continue`） |
| termios / flock | `theme.py`、`cli._run_title_daemon` |
| 路径 / XDG | `legacy_names.py`、`titles.CACHE_DIR`、各 `scan/*.py` |
| shim | `shim.py`（`SUPPORTED_SHELLS`） |
| 安装 | `install.sh`、`.github/workflows/release.yml` |
| remote 信号 | `remote/cli.py`、`remote/daemon.py` |

<!-- 该文档整理/压缩于 2026-10-20 -->
