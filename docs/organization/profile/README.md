**Languages:** English | [简体中文](README.zh-CN.md)

<p align="center"><img src="corral-icon.png" alt="Corral" width="112" height="112"></p>

# Corral

Run your coding agents together, keep them running, and pick up their work from your phone or Mac.

Corral supports Claude Code, Codex, OpenCode, Cursor and Pi. The agents run on your host; the terminal app and native Apple clients let you follow their work, answer questions and work across several sessions.

| Repository | Start here |
| --- | --- |
| [corral](https://github.com/corral-dev/corral) | Terminal workspace, host service and installation · MIT |
| [corral-apple](https://github.com/corral-dev/corral-apple) | Native iPhone and Mac clients · self-build beta · GPL-3.0 |
| [corral-relay](https://github.com/corral-dev/corral-relay) | Self-hosted encrypted relay for access across networks · AGPL-3.0 |

Install the terminal app on macOS or Linux with Homebrew:

```bash
brew install x0c/tap/corral
corral
```

Apple clients can be built from source with Xcode. Public App Store and TestFlight downloads are not available yet. Local access works on the same network; access from another network uses a relay you configure and host yourself. Corral's open-source builds do not bundle a shared public relay.

Report product issues in the matching repository and remove private conversations, host addresses and pairing credentials. The reusable [SessKit](https://github.com/x0c/sesskit) history library is an independent upstream project.
