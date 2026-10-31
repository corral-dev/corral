"""OpenCode CLI 运行时适配器。"""

from __future__ import annotations

import dataclasses
import os
import subprocess

from corral.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo
from corral.runtime.base import BaseRuntime, usable_cwd
from corral.scan import opencode as scan_opencode

# 主命令 `--fork` 支持探测缓存：None=尚未探测（进程内只探一次，禁止每次启动
# 都 fork 子进程）。v2.2.5 实测主 `--help` 已无 --fork（旧 `opencode -s <id>
# --fork` 报 `ERROR Unrecognized flag: --fork` 退出码 1，内嵌侧只看到
# exit 1 + ERROR）；`run` 子命令的 --fork 不受影响。探测失败默认 True（沿用
# 旧 argv，老版本行为零变化）。
_MAIN_FORK_SUPPORT: bool | None = None


def main_supports_fork(executable: str = "opencode") -> bool:
    """已装 opencode 主命令是否声明 `--fork`（v2.2.5 已删）。

    只看 `SUBCOMMANDS` 之前的 flags 段，避免把 `run` 子命令的 --fork 误判成
    主命令支持。结果进程内缓存。
    """
    global _MAIN_FORK_SUPPORT
    if _MAIN_FORK_SUPPORT is not None:
        return _MAIN_FORK_SUPPORT
    try:
        proc = subprocess.run(
            [executable, "--help"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        head = output.split("SUBCOMMANDS")[0]
        _MAIN_FORK_SUPPORT = "--fork" in head
    except Exception:
        _MAIN_FORK_SUPPORT = True
    return _MAIN_FORK_SUPPORT


class OpenCodeRuntime(BaseRuntime):
    id = "opencode"
    display_name = "OpenCode"
    executable = "opencode"
    history_reading_hint = (
        "OpenCode 会话历史存在 SQLite 数据库 opencode.db 里（session_v2/session_message "
        "两表，无 part 表；工作目录读 session_v2.directory，path 列禁用）；用户正文在 "
        "session_message.data.text，助手正文取 data.content[] 里 type='text' 项的 .text "
        "拼接（排除 reasoning 项），工具调用看 content[] 里 type='tool' 项"
        "（name/state.status/state.input/state.content[].text），种类只看 "
        "session_message.type 列；优先运行 `opencode session export <会话ID>` 导出该会话"
        "完整 JSON 阅读，或用只读方式（sqlite3 \"file:<路径>?mode=ro\"）查询，不要写这个库。"
    )
    # `--auto` 是 OpenCode 官方的放行参数（自动批准所有未被显式拒绝的权限请求），
    # 主命令（TUI）与 `run` 子命令都正式声明了它，因此和其他适配器一样统一走
    # auto_approve_args。v2（2.0.16 `--help` 实测：主 flag 有 --auto，`run` 有
    # --auto/-s）两条命令仍声明 --auto；`mini` 子命令不认 --auto（带上直接打印
    # 帮助退出），见 compose_passthrough_argv。历史上这里曾留空：早期版本只有隐藏的
    # --dangerously-skip-permissions 且仅 `run` 接受，主命令的 yargs 严格校验会
    # 因未声明的 flag 直接退出。该限制在现版本已不存在（1.18.9 实测两条命令都声明
    # 了 --auto，旧名作为隐藏别名折叠进同一个开关），装不上 --auto 的旧版本按
    # 「该升级 opencode」处理，不为它保留降级分支。
    auto_approve_args = ("--auto",)
    # session_interrupt counts presses: the first only shows "esc again to
    # interrupt" (2.0.26 verified in tmux). Two Escapes in one write parse as a
    # single alt+escape and are dropped, hence separate presses.
    interrupt_keys = ("Escape", "Escape")
    # OpenCode 的全部子命令（v2 2.0.16 `--help` 实测：upgrade/update/uninstall/
    # acp/api/debug/auth/mcp/plugin/models/stats/mini/run/session/service/
    # reload/pair/serve；顶层 `opencode export` 已死，接力改走 `session export`）。
    # 直启 `corral opencode <词>` 时首个词命中这里就按透传处理，否则会被当成项目名
    # 去模糊匹配。v1 独有的 attach/agent/export/import/github/pr/db/completion
    # 在 v2 已不存在，不要加回。
    SUBCOMMANDS = frozenset(
        (
            "upgrade", "update", "uninstall", "acp", "api", "debug", "auth",
            "mcp", "plugin", "models", "stats", "mini", "run", "session",
            "service", "reload", "pair", "serve",
        )
    )
    # 认 --auto 的子命令（其余子命令带上它会用法错误退出），见 compose_passthrough_argv。
    _AUTO_APPROVE_SUBCOMMANDS = ("run",)

    def composer_region(self, screen: str) -> str | None:
        # The bottommost run of `┃` lines; completion popups render below it
        # (2.0.32).
        lines = screen.splitlines()
        marked = [i for i, line in enumerate(lines) if line.lstrip().startswith("┃")]
        if not marked:
            return None
        start = end = marked[-1]
        while start > 0 and lines[start - 1].lstrip().startswith("┃"):
            start -= 1
        return "\n".join(lines[start : end + 1])

    def scan_signature(self) -> object | None:
        return scan_opencode.scan_signature()

    def scan_sessions(self, limit: int, keep_ids: set[str] | None = None) -> list[SessionInfo]:
        from corral.runtime.host_extension import corral_host_extension
        from corral.runtime.sesskit_bridge import call_scan

        return call_scan(
            scan_opencode.scan_sessions, limit=limit, keep_ids=keep_ids, host=corral_host_extension()
        )

    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        from corral.runtime.sesskit_bridge import load_runtime_conversation

        return load_runtime_conversation(session)

    def delete_session(self, session: SessionInfo) -> None:
        scan_opencode.delete_session(
            str(session.get("path") or ""), str(session.get("id") or "")
        )

    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "-s",
                str(session["id"]),
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_continue_plan(self, session: SessionInfo, instruction: str) -> LaunchPlan:
        """构造供外部执行器使用的非交互式 OpenCode 原生续接计划。"""
        return LaunchPlan(
            argv=(
                self.executable,
                "run",
                *self.auto_approve_args,
                "-s",
                str(session["id"]),
                instruction,
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def build_fork_plan(self, session: SessionInfo) -> LaunchPlan | None:
        """同助手原生分叉；新版无主命令 `--fork` 时返回 None 走克隆+恢复。

        旧版（主 `--help` 含 --fork，如 v2.0.x）：`opencode --auto -s <id>
        --fork`，原样保留。新版（v2.2.5 实测已删，带 flag 直接
        `ERROR Unrecognized flag` 退出码 1）：返回 None，调用方
        `prepare_copy_request` 改走 `clone_session`（整库行复制，图片等内联
        附件原样保留）后再原生恢复。两版 argv/分支各有回归断言。
        """
        if not main_supports_fork(self.executable):
            return None
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "-s",
                str(session["id"]),
                "--fork",
            ),
            cwd=usable_cwd(str(session.get("cwd") or "")),
        )

    def clone_session(self, session: SessionInfo) -> SessionInfo:
        """磁盘复制历史并换新身份（SessKit SQL 行复制），供无 --fork 新版回退。"""
        return scan_opencode.clone_session(session)

    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        # OpenCode 主命令的位置参数是项目路径，提示词只能通过 --prompt 传入；也没有
        # --add-dir 等价物，读取源历史落在工作目录外时仍会命中「外部目录」权限，
        # 靠 --auto 自动放行。
        return LaunchPlan(
            argv=(
                self.executable,
                *self.auto_approve_args,
                "--prompt",
                handoff.render_prompt(),
            ),
            cwd=usable_cwd(handoff.original_cwd),
        )

    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        return LaunchPlan(
            argv=(self.executable, *self.auto_approve_args),
            cwd=usable_cwd(cwd),
        )

    def compose_passthrough_argv(self, user_args: tuple[str, ...]) -> tuple[str, ...]:
        """`corral opencode …` 直启透传：`--auto` 的位置在 OpenCode 上是有讲究的。

        1. **必须排在子命令之后**。主命令的第一个位置参数是项目路径，`--auto` 一旦
           前置，yargs 就不再把后面的词当子命令：`opencode --auto run …` 会变成
           「在名为 run 的目录里开 TUI」（1.18.9 实测 `opencode --auto stats` 报
           `Failed to change directory to <当前目录>/stats`），用户的命令被静默改成
           了另一件事。
        2. **只有主命令和 `run` 声明了它**。`stats`/`session`/`auth` 等子命令带上它
           会被严格校验判为未知参数、用法错误退出，所以这些路径一律不垫。
        3. **`mini` 开头一律原样透传**。`mini` 不认 `--auto`（2.0.16 实测
           `mini --auto` 直接打印帮助退出），`corral opencode mini …` 按直启
           全屏接管，不垫任何参数。这条分支必须在路径判断之前：cwd 下若恰好
           有 `mini` 同名目录，`_looks_like_path` 会把它误判成项目路径而前置
           `--auto`，垫上就起不来。

        第一个参数不像子命令（是路径、或本来就是 flag、或干脆没有参数）时按主命令
        处理，前置即可。
        """
        if any(arg in self.auto_approve_args for arg in user_args):
            return (self.executable, *user_args)
        head = user_args[0] if user_args else ""
        if head == "mini":
            return (self.executable, *user_args)
        if head and not head.startswith("-") and not self._looks_like_path(head):
            if head not in self._AUTO_APPROVE_SUBCOMMANDS:
                return (self.executable, *user_args)
            return (self.executable, head, *self.auto_approve_args, *user_args[1:])
        return (self.executable, *self.auto_approve_args, *user_args)

    @staticmethod
    def _looks_like_path(token: str) -> bool:
        """区分「项目路径位置参数」和「子命令名」，前者仍按主命令垫放行参数。"""
        return (
            os.sep in token
            or token.startswith((".", "~"))
            or os.path.isdir(token)
        )

    def export_handoff(self, session: SessionInfo, title: str) -> Handoff:
        """在基类通用实现之上补充会话 ID：历史 db 是全库共享的，没有 ID 无法定位会话。"""
        handoff = super().export_handoff(session, title)
        return dataclasses.replace(
            handoff,
            history_reading_hint=(
                f"{self.history_reading_hint}本次要读取的会话 ID：{session['id']}。"
            ),
        )
