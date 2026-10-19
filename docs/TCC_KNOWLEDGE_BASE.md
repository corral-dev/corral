# macOS TCC 身份知识库

macOS 权限弹窗署名跟的是**二进制身份**，不是你敲的命令名。corral 用户看到
"Python"/"tmux" 申请文件、截图权限，根因都在这里。

## 1. 归因规则（实测结论）

- 有 `.app` bundle 的认 `CFBundleIdentifier`（`client_type=0`）；裸 Mach-O /
  脚本认**绝对路径 + 代码签名要求**（`client_type=1`）。
- Python 脚本没有自己的 TCC 身份，身份就是**解释器二进制**（pipx venv /
  Homebrew / uv 的 `python3.x`，多为 ad-hoc 签名）。路径一变（升级解释器）
  就视为新应用，旧授权 orphan，弹窗重来。TCC 存的是 resolve 后的真实路径，
  symlink 挡不住。
- 截图/录屏/辅助功能/AppleEvents 查的是**责任链顶端**（responsible process），
  不是直接干活的进程。从终端起的进程责任链是终端；corral 的 tmux server 是
  `launchd` 的 `Interactive` job（`tmux_server.ensure_server`，`tmux -D` 前台
  常驻），父进程是 launchd——所以托管 agent 里调 `screencapture` /
  ScreenCaptureKit / `osascript` / `find` 碰受保护位置时，弹窗署名 tmux。
- 文件类（桌面/文稿/下载）按**实际 open 文件的进程**算：corral 自己
  （扫描、`embed.save_image_and_paste_path` 落盘、查 `pane_current_path`）
  触发的记 Python/tmux 头上；agent 二进制自己直接读文件记它自己头上，
  这一类收敛不到 corral 名下（系统限制，不是 corral 能修的）。

自查（本机跑）：

```bash
corral diagnose            # 看 data.tcc_identity：跳板/解释器/tmux/责任链
corral tcc status          # 同上的人话版
codesign -dv --verbose=2 $(which tmux)
sudo launchctl procinfo <agent-pid> | grep -i responsible
```

## 2. `corral tcc` 跳板（opt-in，能修的部分）

`corral tcc wrap` 编一个 ~50KB 的 C 跳板（`src/corral/tcc.py` 内嵌源码）：

- 常驻父进程、spawn 真正的 Python，**绝不 `exec` 换像**（换像不断责任链，
  但直接检查类权限会跟新像走；保持父进程才有稳定归因）。
- ad-hoc 签名 + 稳定 identifier `com.x0c.corral.cli`；二进制字节定稿后按
  源码 tag 缓存，Python 升级只改旁边的 sidecar JSON，不动二进制 →
  cdhash 不变 → 授权不掉。用跳板启动（`corral-tcc …`）后，corral 自身的
  弹窗归到跳板名下。
- 默认 Developer ID 不强制：ad-hoc 对个人机够用；要跨 rebuild 稳定才需
  `CORRAL_TCC_SIGN_IDENTITY` / `--sign-identity`（首次仍需授权一次）。
- 幂等：字节不变不碰二进制（`changed=false`，无需重新授权）；
  `--rebuild` 才换身份（会重新弹窗一次）。

修不了的部分（不要再往跳板上堆期望）：

- tmux server 是 launchd 的孩子，责任链顶永远是 tmux 自己：在 pane 里截图
  的弹窗继续署名 tmux，给 tmux 授一次即可（`status` 里给出了它的真实路径，
  Homebrew 升级换 Cellar 路径后需重授一次）。
- 各 agent 二进制自己直接 open 文件的弹窗署名它们自己，这是对的——知道是
  谁在读文件。

## 3. 改动入口

- 实现：`src/corral/tcc.py`（只依赖标准库 + `legacy_names`，bootstrap 走
  快速分发，不进 Textual/扫描器）。
- 命令：`corral tcc status|wrap|unwrap`（见 `src/corral/bootstrap.py`）。
- 诊断：`corral diagnose` 的 `data.tcc_identity`（字段文档见
  `agent_api.COMMANDS` 的 diagnose 项，`docs/SKILL.md` 有一句同步说明）。
- 测试：`tests/test_tcc.py`（编排单测 + 真机 cc 编译透传测试；wrap 测试的
  软链一律指隔离目录，禁止碰 `~/.local/bin`）。
