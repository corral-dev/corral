**语言：** [English](README.md) | 简体中文

<p align="center"><img src="corral-icon.png" alt="Corral" width="112" height="112"></p>

# Corral

集中运行编程助手，让任务继续执行，并在手机或 Mac 上接着跟进。

Corral 支持 Claude Code、Codex、OpenCode、Cursor 和 Pi。助手运行在开发机上；终端应用和原生 Apple 客户端让你查看进展、回答问题，并同时处理多个会话。

| 仓库 | 从这里开始 |
| --- | --- |
| [corral](https://github.com/corral-dev/corral) | 终端工作区、开发机服务和安装入口 · MIT |
| [corral-apple](https://github.com/corral-dev/corral-apple) | 原生 iPhone 与 Mac 客户端 · 自行构建的测试版 · GPL-3.0 |
| [corral-relay](https://github.com/corral-dev/corral-relay) | 跨网络访问所需的自建加密中继 · AGPL-3.0 |

在 macOS 或 Linux 上通过 Homebrew 安装终端应用：

```bash
brew install x0c/tap/corral
corral
```

Apple 客户端可使用 Xcode 从源码构建，目前尚无公开的 App Store 或 TestFlight 下载。同一网络内可以直连；跨网络使用需要配置你自己部署的中继。Corral 开源版本不内置共享公共中继。

请在相应仓库报告产品问题，并去除私人对话、机器地址和配对凭据。可复用的历史解析库 [SessKit](https://github.com/x0c/sesskit)是独立的上游项目。
