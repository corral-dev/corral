"""命令拦截（shim）：让用户手敲的 `claude` / `codex` / … 自动改走 corral 托管启动。

设计取舍（改之前先读，这几条是安全边界不是风格问题）：

1. **只做交互式 shell 函数拦截，不做 PATH shim 目录。** 用户的诉求是"我自己在终端
   敲命令时自动托管"，脚本、CI、编辑器插件、别的 Agent 拉起的子进程一个都不该被
   托管。shell 函数天然只在交互式 shell 里定义，子进程继承不到（bash 不 `export -f`、
   zsh 根本不导出函数），等于免费砍掉了绝大部分事故面，也顺带避免了 PATH shim 的
   经典问题（遮蔽同名系统工具、`which` 自定位拿到 shim、卸载运行时后仍显示"已安装"）。
2. **防递归三重保险。** corral 拉起运行时用的是裸命令名走 PATH：① 函数不被子进程
   继承；② 走 corral 那一支带上 `CORRAL_SHIM_ACTIVE=1`，任何嵌套交互式 shell 见到
   即放行；③ 托管会话里注入的 `CORRAL_RUNTIME`（及旧名 `SC_RUNTIME`）触发放行。
   **不要把用户自己的 tmux/screen 当成放行条件**：corral 用独立的保活 socket，
   和用户自己的复用器不是一层，拦掉等于日常在 tmux 里敲命令永远进不了托管。
3. **失败一律退回真身。** 找不到 `corral` 命令、不是真实终端、带了无头/管理类参数，
   全部 `command <cmd>` 直接执行原命令。**用户的 `claude` 因为装了 corral 而不能用，
   是唯一不可接受的结局**——生成的脚本里任何一处判断出错，后果都必须是"没托管"，
   不能是"命令坏了"。
4. **无头调用必须放行。** corral 自己在后台用各助手无头入口（如 `claude -p` /
   `codex exec` / `opencode run` / `kimi -p` / `agent -p`）生成会话
   标题（见 `titlegen.py`）；这类调用一旦被包进 tmux 托管，标题功能会直接失效并堆积
   进程。放行判据同时看"非真实终端"和"参数里出现无头/管理类词"，两条都要留。
   管理类**子命令**只认第一个位置参数，避免 `claude please update the docs` 这种
   提示词被误判成 `update` 子命令；无头**旗标**仍在任意位置命中即放行。
5. **`agent` 只在认出是 Cursor CLI 时拦截。** 官方现行主命令就是 `agent`
   （`cursor-agent` 是兼容名）。名字太通用，不能无条件拦截；PATH 上的 `agent`
   能看出是官方 Cursor 安装或 cursor-mode-model 包装时才默认拦。其它同名工具
   不拦；认不出时仍可用 `--include agent` 强制开启。

生成物：一个受 corral 管理的脚本文件（`~/.cache/corral/shim/`），用户 shell 配置里
只留一行 `source`。**不在配置里内联函数正文**——后续升级只需重写脚本文件，不必反复
改用户的 shell 配置。**默认不安装**：只有用户显式 `corral shim install` 才写入；
安装脚本与交互式启动 corral 都不得隐式补齐。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from corral.i18n import join_names, t
from corral.legacy_names import (
    CACHE_DIRNAME,
    getenv,
    shim_fish_guard_lines,
    shim_fish_launch_env,
    shim_posix_guard_lines,
    shim_posix_launch_prefix,
)

SHIM_API_VERSION = 1
SHIM_SCRIPT_VERSION = 3  # 生成脚本内容有语义变化时 +1，status 据此判 outdated

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_PERMISSION = 4

BLOCK_BEGIN = "# >>> corral shim >>>"
BLOCK_END = "# <<< corral shim <<<"

SUPPORTED_SHELLS = ("bash", "zsh", "fish")


@dataclass(frozen=True)
class ShimTarget:
    """一个可被拦截的运行时命令。"""

    command: str  # 用户实际敲的可执行文件名
    runtime_id: str  # 映射到的 corral 直启子命令
    default_on: bool  # 是否默认拦截
    passthrough_words: tuple[str, ...]  # 该命令下必须直接放行的子命令/参数


# 通用放行词：任何运行时带上这些都属于"无头或管理类调用"，不该被托管进 tmux。
COMMON_PASSTHROUGH = (
    "-p", "--print", "-h", "--help", "-v", "-V", "--version",
    "--json", "--output-format", "--headless", "--no-interactive",
)

# Cursor 官方现行主命令是 `agent`；这些是不能进托管会话的管理/无头入口。
# `resume` 是交互续聊，要托管，不要放进来。
CURSOR_PASSTHROUGH = (
    "update", "login", "logout", "status", "whoami", "about", "models",
    "mcp", "sandbox", "worker", "acp", "ls", "create-chat",
    "generate-rule", "rule", "help",
    "install-shell-integration", "uninstall-shell-integration",
    "--list-models",
)

# 与 `runtime/registry.py` 的默认注册表对应（tests/test_shim.py 断言两边不漂移）。
TARGETS: tuple[ShimTarget, ...] = (
    ShimTarget("claude", "claude", True,
               ("update", "doctor", "mcp", "config", "install", "migrate-installer", "setup-token")),
    ShimTarget("codex", "codex", True,
               ("exec", "apply", "login", "logout", "mcp", "completion")),
    # opencode v2（本机实装 2.0.16 `--help` 实测，全集 18 个：upgrade/update/
    # uninstall/acp/api/debug/auth/mcp/plugin/models/stats/mini/run/session/
    # service/reload/pair/serve）。其中无头或管理类子命令直接放行，不进托管。
    # `mini`（"Start the minimal interactive interface"）是交互式子命令，故意
    # 不进放行表：`opencode mini …` 走 `corral opencode mini …` 托管（直启侧按
    # SUBCOMMANDS 透传；真实终端默认 TUI 内嵌，非终端/--no-keepalive/内嵌不可用
    # 时走 cli.py 的 keepalive wrap + execvp 全屏接管旧路径）。
    # v1 独有的 github（含 attach/agent/export/import/pr/db/completion）在 v2
    # 已不存在（`opencode github --help` 回到顶层帮助），删掉：此后
    # `opencode github …` 不再直通真身，改为托管进 `corral opencode github …`，
    # 而 v2 的 opencode 本来就没有 github，任何路径都是明确报错、可接受。
    ShimTarget("opencode", "opencode", True,
               ("run", "serve", "auth", "upgrade", "models",
                "api", "service", "reload", "pair", "session", "update")),
    ShimTarget("kimi", "kimi", True, ("update", "mcp", "config", "login", "logout")),
    # Pi 的主命令名不通用，默认接管；安装、维护、认证与导出类调用不能进托管会话。
    ShimTarget("pi", "pi", True,
               ("install", "remove", "uninstall", "update", "list", "config", "auth",
                "--export", "--list-models")),
    ShimTarget("cursor-agent", "cursor", True, CURSOR_PASSTHROUGH),
    # 名字太通用，default_on=False；PATH 上认出是 Cursor CLI 时仍会自动选中。
    ShimTarget("agent", "cursor", False, CURSOR_PASSTHROUGH),
)

# PATH / 脚本正文里出现这些，就认定 `agent` 是 Cursor CLI（含官方安装与包装器）。
_CURSOR_CLI_MARKERS = ("cursor-agent", "cursor-mode-model", "CURSOR_INVOKED_AS")


class ShimError(Exception):
    """可稳定映射到结构化错误和退出码的拦截器异常。"""

    def __init__(self, code: str, message: str, *, exit_code: int = EXIT_ERROR,
                 hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.hint = hint


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ShimError("usage_error", message, exit_code=EXIT_USAGE,
                        hint=t("shim.err.usage_hint"))


# ---------------------------------------------------------------- 路径与探测


def _home_path(home: str | os.PathLike[str] | None) -> Path:
    return Path(home).expanduser() if home is not None else Path.home()


def _cache_dir(home: str | os.PathLike[str] | None) -> Path:
    """与 `cursor_observer._cache_dir` 同一套约定（CORRAL_CACHE_DIR > XDG > ~/.cache）。"""
    override = getenv("CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg_root = os.environ.get("XDG_CACHE_HOME")
    if xdg_root:
        return Path(xdg_root).expanduser() / CACHE_DIRNAME
    return _home_path(home) / ".cache" / CACHE_DIRNAME


def script_path(shell: str, home: str | os.PathLike[str] | None = None) -> Path:
    name = "corral-shim.fish" if shell == "fish" else "corral-shim.sh"
    return _cache_dir(home) / "shim" / name


def rc_path(shell: str, home: str | os.PathLike[str] | None = None) -> Path:
    """各 shell 的交互式配置文件。

    bash 用 `.bashrc`、zsh 优先 `$ZDOTDIR`——这两个文件都只在交互式 shell 被读取，
    正好是我们想要的作用域；fish 的 `config.fish` 所有场景都读，靠脚本内部的
    `status is-interactive` 收窄。
    """
    base = _home_path(home)
    if shell == "bash":
        return base / ".bashrc"
    if shell == "zsh":
        zdotdir = os.environ.get("ZDOTDIR")
        return (Path(zdotdir).expanduser() if zdotdir else base) / ".zshrc"
    if shell == "fish":
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        root = Path(xdg_config).expanduser() if xdg_config else base / ".config"
        return root / "fish" / "config.fish"
    raise ShimError(
        "unsupported_shell",
        t("shim.err.unsupported_shell", shell=shell),
        exit_code=EXIT_USAGE,
        hint=t("shim.err.unsupported_shell_hint", shells="/".join(SUPPORTED_SHELLS)),
    )


def detect_shell() -> str:
    """探测当前用户的交互式 shell；探测不到时报错要求显式指定，不瞎猜。"""
    raw = os.environ.get("SHELL") or ""
    name = os.path.basename(raw.strip())
    if name in SUPPORTED_SHELLS:
        return name
    raise ShimError(
        "shell_not_detected",
        t("shim.err.shell_not_detected", shell=repr(raw)),
        exit_code=EXIT_USAGE,
        hint=t("shim.err.shell_not_detected_hint", shells="/".join(SUPPORTED_SHELLS)),
    )


def looks_like_cursor_cli(command: str = "agent") -> bool:
    """PATH 上的该命令是不是 Cursor CLI（官方安装或转调官方 Agent 的包装）。

    只看路径和脚本头，不执行该命令——执行会有副作用，也太慢。认不出就当不是，
    后果是不拦，符合「失败方向必须是没托管」。
    """
    path = shutil.which(command)
    if not path:
        return False
    blob = path
    try:
        blob = f"{path}\n{os.path.realpath(path)}"
    except OSError:
        pass
    if any(marker in blob for marker in _CURSOR_CLI_MARKERS):
        return True
    try:
        head = Path(path).read_bytes()[:8192].decode("utf-8", "ignore")
    except OSError:
        return False
    return any(marker in head for marker in _CURSOR_CLI_MARKERS)


def selected_targets(include: Iterable[str] = ()) -> tuple[ShimTarget, ...]:
    """默认目标 + 认出是 Cursor 的 `agent` + 显式 --include 的目标。"""
    extra = {name.strip() for name in include if name and name.strip()}
    known = {target.command for target in TARGETS}
    unknown = extra - known
    if unknown:
        raise ShimError(
            "unknown_command",
            t("shim.err.unknown_command", commands=join_names(sorted(unknown))),
            exit_code=EXIT_USAGE,
            hint=t("shim.err.unknown_command_hint", commands=join_names(sorted(known))),
        )
    auto_agent = looks_like_cursor_cli("agent")
    selected = []
    for target in TARGETS:
        if target.default_on or target.command in extra:
            selected.append(target)
        elif target.command == "agent" and auto_agent:
            selected.append(target)
    return tuple(selected)


def _installed_targets(targets: Iterable[ShimTarget]) -> tuple[ShimTarget, ...]:
    """只给本机真实存在的命令生成函数。

    给没装的命令定义函数会把"command not found"变成一句 corral 的报错，反而更难懂；
    新装了运行时的用户重跑一次 install 即可，`status` 会主动报出这种漂移。
    """
    return tuple(target for target in targets if shutil.which(target.command))


# ---------------------------------------------------------------- 脚本生成


def _render_posix(targets: Sequence[ShimTarget]) -> str:
    """bash / zsh 共用的脚本正文。"""
    lines = [
        "# corral shim —— 由 `corral shim install` 生成，请勿手动编辑。",
        "# 作用：交互式敲下面这些命令时自动改走 `corral <运行时>`（默认带上跳过权限",
        "# 问询的参数）；无头调用、管理类子命令、非真实终端、已在托管会话内一律原样放行。",
        f"# 卸载：corral shim uninstall    版本：{SHIM_SCRIPT_VERSION}",
        "",
        f"CORRAL_SHIM_VERSION={SHIM_SCRIPT_VERSION}",
        "",
        "# 非交互式 shell 直接跳过（bash/zsh 正常只在交互式读取本文件，这里再兜一层）",
        "case $- in",
        "    *i*) ;;",
        "    *) return 0 2>/dev/null || true ;;",
        "esac",
        "",
        "_corral_shim_passthrough() {",
        "    # 返回 0 = 直接执行真身；返回 1 = 交给 corral 托管。任何拿不准的情况都返回 0。",
        "    local words=\"$1\"",
        "    shift",
        *shim_posix_guard_lines(),
        "    { [ -t 0 ] && [ -t 1 ]; } || return 0            # 非真实终端：脚本/管道/无头调用",
        "    command -v corral >/dev/null 2>&1 || return 0    # corral 不在了，退回真身",
        "    local arg sub=\"\"",
        "    for arg in \"$@\"; do",
        "        case \"$arg\" in",
        f"            {'|'.join(COMMON_PASSTHROUGH)}) return 0 ;;",
        "        esac",
        "        case \"$arg\" in",
        "            -*)",
        "                case \" $words \" in",
        "                    *\" $arg \"*) return 0 ;;",
        "                esac",
        "                ;;",
        "            *)",
        "                [ -z \"$sub\" ] && sub=\"$arg\"",
        "                ;;",
        "        esac",
        "    done",
        "    [ -n \"$sub\" ] || return 1",
        "    case \" $words \" in",
        "        *\" $sub \"*) return 0 ;;",
        "    esac",
        "    return 1",
        "}",
        "",
    ]
    for target in targets:
        words = " ".join(target.passthrough_words)
        lines += [
            f"{target.command}() {{",
            f"    if _corral_shim_passthrough \"{words}\" \"$@\"; then",
            f"        command {target.command} \"$@\"",
            "    else",
            f"        {shim_posix_launch_prefix()} command corral {target.runtime_id} \"$@\"",
            "    fi",
            "}",
            "",
        ]
    return "\n".join(lines)


def _render_fish(targets: Sequence[ShimTarget]) -> str:
    """fish 版本：语法与 POSIX 完全不同，单独渲染一份。"""
    lines = [
        "# corral shim —— 由 `corral shim install` 生成，请勿手动编辑。",
        "# 作用：交互式敲下面这些命令时自动改走 `corral <运行时>`（默认带上跳过权限",
        "# 问询的参数）；无头调用、管理类子命令、非真实终端、已在托管会话内一律原样放行。",
        f"# 卸载：corral shim uninstall    版本：{SHIM_SCRIPT_VERSION}",
        "",
        f"set -g CORRAL_SHIM_VERSION {SHIM_SCRIPT_VERSION}",
        "",
        "if status is-interactive",
        "    function _corral_shim_passthrough",
        "        # 返回 0 = 直接执行真身；返回 1 = 交给 corral 托管。拿不准一律 0。",
        "        set -l words (string split ' ' -- $argv[1])",
        "        set -l rest $argv[2..-1]",
        *shim_fish_guard_lines(),
        "        if not isatty stdin; return 0; end",
        "        if not isatty stdout; return 0; end",
        "        if not command -q corral; return 0; end",
        "        set -l sub ''",
        "        for arg in $rest",
        f"            if contains -- $arg {' '.join(COMMON_PASSTHROUGH)}",
        "                return 0",
        "            end",
        "            if string match -q -- '-*' $arg",
        "                if contains -- $arg $words",
        "                    return 0",
        "                end",
        "                continue",
        "            end",
        "            if test -z \"$sub\"",
        "                set sub $arg",
        "            end",
        "        end",
        "        if test -n \"$sub\"; and contains -- $sub $words",
        "            return 0",
        "        end",
        "        return 1",
        "    end",
        "",
    ]
    for target in targets:
        words = " ".join(target.passthrough_words)
        lines += [
            f"    function {target.command} --wraps {target.command}",
            f"        if _corral_shim_passthrough '{words}' $argv",
            f"            command {target.command} $argv",
            "        else",
            f"            env {shim_fish_launch_env()} corral {target.runtime_id} $argv",
            "        end",
            "    end",
            "",
        ]
    lines += ["end", ""]
    return "\n".join(lines)


def render_script(shell: str, targets: Sequence[ShimTarget]) -> str:
    return _render_fish(targets) if shell == "fish" else _render_posix(targets)


def _block(shell: str, path: Path) -> str:
    """用户 shell 配置里那一小段（只负责 source 生成好的脚本）。"""
    quoted = str(path)
    if shell == "fish":
        body = f"test -f '{quoted}'; and source '{quoted}'"
    else:
        body = f'[ -f "{quoted}" ] && . "{quoted}"'
    return f"{BLOCK_BEGIN}\n{body}\n{BLOCK_END}"


# ---------------------------------------------------------------- 文件读写


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise ShimError("permission_denied", t("shim.err.permission_read", path=path),
                        exit_code=EXIT_PERMISSION) from exc
    except OSError as exc:
        raise ShimError("read_failed", t("shim.err.read_failed", path=path, error=exc)) from exc


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.corral-tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _backup(path: Path, raw: str, home: str | os.PathLike[str] | None) -> Path:
    """改用户 shell 配置前先留一份备份，出问题能原样还原。"""
    backup_dir = _cache_dir(home) / "shim" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{path.name}.{int(time.time())}.bak"
    target.write_text(raw, encoding="utf-8")
    return target


def strip_block(text: str) -> str:
    """移除配置里由 corral 管理的那一段（含标记行），其余内容原样保留。

    容忍用户在块内手改过内容、以及历史上重复安装留下的多个块。
    """
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == BLOCK_BEGIN:
            skipping = True
            continue
        if stripped == BLOCK_END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    result = "\n".join(out)
    return result


def _normalize_tail(text: str) -> str:
    """去掉块被移除后残留的多余空行，避免反复安装/卸载把配置尾部撑开。"""
    return text.rstrip("\n")


# ---------------------------------------------------------------- 状态与动作


def _shimmed_commands(shell: str, script_text: str | None) -> set[str]:
    """从脚本正文里解析出真正被定义了函数的命令名。

    **必须按整行匹配**：`agent` 是 `cursor-agent` 的后缀，用子串判断会让「只装了
    cursor-agent 的脚本」被误报成「agent 也拦了」，用户据此以为通用的 `agent`
    命令已被接管。
    """
    if not script_text:
        return set()
    found: set[str] = set()
    for line in script_text.splitlines():
        stripped = line.strip()
        if shell == "fish":
            if stripped.startswith("function ") and stripped.count(" ") >= 1:
                found.add(stripped.split()[1])
        elif stripped.endswith("() {"):
            found.add(stripped[: -len("() {")])
    return found


def _command_states(shell: str, targets: Sequence[ShimTarget], script_text: str | None) -> list[dict]:
    states = []
    defined = _shimmed_commands(shell, script_text)
    for target in TARGETS:
        present = shutil.which(target.command) is not None
        selected = target in targets
        shimmed = target.command in defined
        states.append({
            "command": target.command,
            "runtime": target.runtime_id,
            "installed": present,
            "selected": selected,
            "shimmed": shimmed,
        })
    return states


def status(shell: str | None = None, home: str | os.PathLike[str] | None = None,
           include: Iterable[str] = ()) -> dict[str, Any]:
    """只读检查：配置里的引用块在不在、脚本在不在、版本对不对、有没有漏拦的运行时。"""
    shell = shell or detect_shell()
    targets = _installed_targets(selected_targets(include))
    spath = script_path(shell, home)
    rpath = rc_path(shell, home)
    script_text = _read_text(spath)
    rc_text = _read_text(rpath) or ""
    block_installed = BLOCK_BEGIN in rc_text
    expected = render_script(shell, targets)
    commands = _command_states(shell, targets, script_text)

    if not block_installed or script_text is None:
        state = "not_installed"
    elif script_text != expected:
        state = "outdated"
    else:
        state = "installed"

    # 「待补拦截」只在已安装的前提下才有意义：没装过的时候每个命令都"没被拦"，
    # 全列出来只是噪音，还会让用户以为出了问题。
    drift = ([c["command"] for c in commands if c["selected"] and not c["shimmed"]]
             if state != "not_installed" else [])

    return {
        "status": state,
        "shell": shell,
        "rc_path": str(rpath),
        "rc_exists": rpath.exists(),
        "block_installed": block_installed,
        "script_path": str(spath),
        "script_exists": script_text is not None,
        "script_version": SHIM_SCRIPT_VERSION,
        "commands": commands,
        "missing_commands": drift,
    }


def _apply(*, shell: str | None, home: str | os.PathLike[str] | None,
           include: Iterable[str], dry_run: bool, remove: bool) -> dict[str, Any]:
    shell = shell or detect_shell()
    targets = _installed_targets(selected_targets(include))
    if not remove and not targets:
        raise ShimError(
            "no_runtime_installed",
            t("shim.err.no_runtime"),
            exit_code=EXIT_NOT_FOUND,
            hint=t("shim.err.no_runtime_hint"),
        )

    spath = script_path(shell, home)
    rpath = rc_path(shell, home)
    script_text = _read_text(spath)
    rc_text = _read_text(rpath)
    rc_current = rc_text or ""

    if remove:
        desired_script = None
        stripped = _normalize_tail(strip_block(rc_current))
        desired_rc = (stripped + "\n") if stripped else ""
    else:
        desired_script = render_script(shell, targets)
        stripped = _normalize_tail(strip_block(rc_current))
        block = _block(shell, spath)
        desired_rc = (f"{stripped}\n\n{block}\n" if stripped else f"{block}\n")

    script_changed = desired_script != script_text
    rc_changed = desired_rc != rc_current
    changed = script_changed or rc_changed

    if not changed:
        state = "unchanged"
    elif remove:
        state = "would_uninstall" if dry_run else "uninstalled"
    elif script_text is None and not (rc_text and BLOCK_BEGIN in rc_text):
        state = "would_install" if dry_run else "installed"
    else:
        state = "would_update" if dry_run else "updated"

    backup_path: Path | None = None
    if changed and not dry_run:
        try:
            if rc_changed and rc_text is not None:
                backup_path = _backup(rpath, rc_text, home)
            if script_changed:
                if desired_script is None:
                    spath.unlink(missing_ok=True)
                else:
                    _atomic_write(spath, desired_script)
            if rc_changed:
                _atomic_write(rpath, desired_rc)
        except PermissionError as exc:
            raise ShimError("permission_denied", t("shim.err.permission_write", path=rpath),
                            exit_code=EXIT_PERMISSION) from exc
        except OSError as exc:
            raise ShimError("write_failed", t("shim.err.write_failed", error=exc)) from exc

    result = status(shell, home, include) if not dry_run else {
        "status": state, "shell": shell, "rc_path": str(rpath),
        "script_path": str(spath),
        "commands": _command_states(shell, targets, desired_script),
        "missing_commands": [],
    }
    result.update({
        "status": state,
        "changed": changed,
        "dry_run": dry_run,
        "backup_path": str(backup_path) if backup_path else None,
        "reload_hint": _reload_hint(shell, rpath) if (changed and not dry_run) else None,
    })
    return result


def _reload_hint(shell: str, rpath: Path) -> str:
    return t("shim.reload_hint", path=rpath)


def install(shell: str | None = None, home: str | os.PathLike[str] | None = None,
            include: Iterable[str] = (), dry_run: bool = False) -> dict[str, Any]:
    """幂等安装/升级命令拦截。重复执行只会把块与脚本刷新到最新，不会叠加。"""
    return _apply(shell=shell, home=home, include=include, dry_run=dry_run, remove=False)



def uninstall(shell: str | None = None, home: str | os.PathLike[str] | None = None,
              include: Iterable[str] = (), dry_run: bool = False) -> dict[str, Any]:
    """移除 corral 写入的配置块与生成脚本，用户配置其余内容原样保留。"""
    return _apply(shell=shell, home=home, include=include, dry_run=dry_run, remove=True)


# ---------------------------------------------------------------- CLI


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(prog="corral shim", add_help=True,
                             description=t("shim.cli.description"))
    parser.add_argument("action", choices=("status", "install", "uninstall"))
    parser.add_argument("--shell", choices=SUPPORTED_SHELLS, default=None,
                        help=t("shim.cli.help.shell"))
    parser.add_argument("--include", action="append", default=[], metavar="CMD",
                        help=t("shim.cli.help.include"))
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help=t("shim.cli.help.json"))
    parser.add_argument("--dry-run", action="store_true", help=t("shim.cli.help.dry_run"))
    return parser


def _ok(data: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    return {"ok": True, "data": data, "error": None,
            "meta": {"version": SHIM_API_VERSION, "dry_run": dry_run}}


def _error(exc: ShimError, *, dry_run: bool) -> dict[str, Any]:
    return {"ok": False, "data": None,
            "error": {"code": exc.code, "message": exc.message, "hint": exc.hint},
            "meta": {"version": SHIM_API_VERSION, "dry_run": dry_run}}


_STATUS_CODES = frozenset({
    "installed", "updated", "unchanged", "uninstalled", "not_installed",
    "outdated", "would_install", "would_update", "would_uninstall",
})


def _status_label(code: str) -> str:
    return t(f"shim.status.{code}") if code in _STATUS_CODES else code


def _print(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    if not payload["ok"]:
        error = payload["error"]
        print(t("shim.print.error", message=error["message"]), file=sys.stderr)
        if error.get("hint"):
            print(t("shim.print.hint", hint=error["hint"]), file=sys.stderr)
        return
    data = payload["data"]
    print(t("shim.print.status",
            status=_status_label(str(data.get("status") or "")),
            shell=data.get("shell")))
    print(t("shim.print.rc", path=data.get("rc_path")))
    print(t("shim.print.script", path=data.get("script_path")))
    shimmed = [c["command"] for c in data.get("commands", []) if c.get("shimmed")]
    if shimmed:
        print(t("shim.print.shimmed", commands=join_names(shimmed)))
    skipped = [c["command"] for c in data.get("commands", [])
               if c.get("installed") and not c.get("selected")]
    if skipped:
        print(t("shim.print.skipped", commands=join_names(skipped)))
    if data.get("missing_commands"):
        print(t("shim.print.missing", commands=join_names(list(data["missing_commands"]))))
    if data.get("reload_hint"):
        print(f"  {data['reload_hint']}")


def cli_main(argv: Sequence[str] | None = None) -> int:
    """处理 `corral shim …`；非 TTY 自动输出 JSON envelope。"""
    args_list = list(argv if argv is not None else sys.argv[1:])
    json_requested = "--json" in args_list
    dry_run = "--dry-run" in args_list
    json_output = json_requested or not sys.stdout.isatty()
    try:
        args = _parser().parse_args(args_list)
        if args.action == "status" and args.dry_run:
            raise ShimError("usage_error", t("shim.err.status_dry_run"),
                            exit_code=EXIT_USAGE)
        if args.action == "status":
            data = status(args.shell, include=args.include)
        elif args.action == "install":
            data = install(args.shell, include=args.include, dry_run=args.dry_run)
        else:
            data = uninstall(args.shell, include=args.include, dry_run=args.dry_run)
        _print(_ok(data, dry_run=args.dry_run), json_output=json_output)
        return EXIT_OK
    except ShimError as exc:
        _print(_error(exc, dry_run=dry_run), json_output=json_output)
        return exc.exit_code
    except Exception as exc:  # 兜底：绝不把裸异常栈丢给用户
        wrapped = ShimError("shim_failed", t("shim.err.failed", error=exc))
        _print(_error(wrapped, dry_run=dry_run), json_output=json_output)
        return wrapped.exit_code


__all__ = ["cli_main", "install", "looks_like_cursor_cli", "render_script",
           "status", "uninstall", "TARGETS"]
