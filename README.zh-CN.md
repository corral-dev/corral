**语言：** [English](README.md) | 简体中文

<p align="center">
  <img src="docs/screenshots/corral-icon.png" alt="Corral" width="112" height="112">
</p>
<h1 align="center">Corral</h1>
<p align="center"><strong>在一个终端里运行所有编程助手，离开电脑也能用手机接着做。</strong></p>
<p align="center">Claude Code、Codex、Cursor、OpenCode、Kimi Code 和 Pi 并排工作。你走开后它们继续运行，哪个在等你回复一眼就能看到。</p>

<p align="center">
  <a href="https://github.com/x0c/corral/releases/latest"><img src="https://img.shields.io/github/v/release/x0c/corral" alt="最新版本"></a>
  <a href="https://github.com/x0c/corral/actions/workflows/test.yml"><img src="https://github.com/x0c/corral/actions/workflows/test.yml/badge.svg" alt="测试"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue" alt="MIT 许可证"></a>
</p>

<p align="center">
  <img src="docs/screenshots/demo.gif" alt="在 Corral 里切换编程助手会话、搜索，并把两个会话并排打开" width="100%">
</p>
<p align="center"><em>演示来自真实终端界面，使用的是虚构会话，不是你电脑上的实际记录。</em></p>

<p align="center">
  <img src="docs/screenshots/list.png" alt="Corral 侧栏列出 Claude Code、Codex 等编程助手会话，旁边是对话预览" width="100%">
</p>

## 三个部分

| 部分 | 用途 | 状态 |
| --- | --- | --- |
| **终端应用** | 在一个终端里运行、查看、切换所有编程助手。 | 已可用，安装方法见下文 |
| **iPhone 与 Mac 应用** | 离开终端也能跟进助手、回答它的提问。 | iPhone：开发中，暂无公开下载 · Mac：规划中 |
| **Corral Ideas** | 在一个本地页面里随手记想法，由一个统筹助手整理成任务，交给编程助手去做。 | 规划中 |

## 安装

**macOS 或已安装 Homebrew 的 Linux**：

```bash
brew install x0c/tap/corral
corral
```

请使用完整名称：`brew install corral` 会安装另一个不相关的项目。Homebrew 会一并安装 Python 和 tmux。

<details>
<summary>不使用 Homebrew</summary>

先安装 **Python 3.10+** 和 **tmux 3.2+**，然后运行：

```bash
curl -fsSL https://raw.githubusercontent.com/x0c/corral/main/install.sh | bash
corral
```

如果终端找不到 `corral`，按安装器提示设置 PATH。回退到源码构建时需要 Rust。

</details>

请另外安装并登录至少一个支持的编程助手。Corral 免费，采用 MIT 许可证；各助手使用自己的账号，可能产生费用。**不支持 Windows 和 WSL。**

## 能帮你做什么

- **知道哪个在等你。** 所有助手正在执行的任务和等你回答的问题，集中在同一个侧栏里。
- **同时推进多个任务。** 最多四个会话分屏，相关任务可以分组、置顶。
- **走开了助手照样跑。** 关掉 Corral 或断开 SSH 后，托管会话继续运行，开发机需要保持唤醒。
- **换个助手接着做。** 把任务连同对话历史交给另一个助手新开会话，比如 Claude 写完交给 Codex 检查。
- **用手机接着做。** 离开电脑也能查看回复、追问，或回答助手的提问。iPhone 客户端仍在开发中，见下文。
- **找回以前的对话。** 跨所有助手搜索聊过的内容，也可以按项目和标题筛选。

## 开始使用

打开 `corral`，侧栏会显示已有的助手会话。选中对话即可阅读，这一步不会启动助手。按 `Enter` 恢复已结束的会话，或进入正在托管的助手终端。

按 `Ctrl+N` 开始新任务。也可以在终端指定助手启动：

```bash
corral claude
# 也支持：corral codex | corral opencode | corral kimi | corral cursor | corral pi
```

用 `Space` 选中二至四个会话，再按 `Enter` 分屏打开。关掉其中一个显示区域只是收起画面，不会结束正在托管的助手。

如果想换助手继续：打开会话，按 `Ctrl+T`，选择助手。例如 Claude 完成初步实现后，可以把这段对话交给 Codex 新开会话检查。源对话保持完整，接手的助手自行读取所需历史。要用原助手原生恢复，按 `Enter` 即可。

按 `Ctrl+\` 把键盘控制交回侧栏。退出 Corral 后，托管的助手继续运行。

Corral 启动助手时，会在支持的情况下启用自动批准模式。这些助手可以使用你当前本地用户的权限执行操作。

### 记住这几个快捷键

| 按键 | 操作 |
| --- | --- |
| `/` | 筛选项目和会话标题 |
| `Ctrl+F` | 搜索对话正文 |
| `Enter` | 恢复或进入选中的会话 |
| `Ctrl+N` | 新建会话 |
| `Space`，然后 `Enter` | 选中二至四个会话并分屏打开 |
| `Ctrl+T` | 导出、复制或交给另一个助手 |
| `Ctrl+\` | 把输入焦点交回侧栏 |
| `Esc` | 关闭当前弹窗或退出 |

侧栏快捷键在侧栏获得焦点时生效。当前页面可用的操作会显示在底部提示栏。

<details>
<summary>记得聊过什么，就能找到那段对话</summary>

![全文搜索结果与命中的对话片段](docs/screenshots/search.png)

</details>

## 用手机接着做

离开电脑，助手也不用停下：在 iPhone 上查看对话、回复助手，或回答它提出的问题。

**iPhone 客户端仍在开发中；本仓库暂不提供公开的 App 下载。** 安装命令行工具不会同时安装手机客户端。

<p align="center">
  <img src="docs/screenshots/ios-sessions.png" alt="iPhone 会话列表" width="220">
  <img src="docs/screenshots/ios-chat.png" alt="iPhone 对话和助手提问" width="220">
</p>

已有客户端安装包，并在开发机装好远程功能所需依赖后，运行：

```bash
corral remote pair
```

配对时会自动开启远程连接。先在同一局域网配对。外出使用需要自行部署中继；Corral 不内置共享公共中继。手机通信采用端到端加密。配对及中继配置见[远程使用指南](docs/REMOTE_KNOWLEDGE_BASE.md)。

## 隐私

- 会话浏览和搜索读取本地助手历史。
- 可选的标题生成会把简短摘录发给已配置的语言模型网关，可能消耗该网关额度。
- 更新检查会访问 GitHub。
- 配对手机意味着授权它访问并操作已授权会话；远程通信采用端到端加密。

数据流向与控制方式见[隐私说明](PRIVACY.md)。

## 给脚本和编程助手

```bash
corral list --top 10 --compact
corral search "login" --deep
corral show <session-id-prefix> --messages 10 --compact
```

JSON 输出、导出及接力计划详见[命令参考](docs/SKILL.md)。

## 文档

- [命令参考](docs/SKILL.md)：命令行用法与自动化
- [终端指南](docs/TERMINAL_UI_KNOWLEDGE_BASE.md)：分屏、分组、焦点与快捷键
- [远程指南](docs/REMOTE_KNOWLEDGE_BASE.md)：手机配对与自建中继
- [维护指南](docs/MAINTAINER_GUIDE.md)：开发、测试与发布
- [报告问题或提出建议](https://github.com/x0c/corral/issues)：请附系统、终端、Corral 版本和复现步骤，并去除对话中的隐私内容

如果 Corral 成了你日常工作的一部分，欢迎点一个 Star，让更多开发者发现它。

[MIT 许可证](LICENSE)
