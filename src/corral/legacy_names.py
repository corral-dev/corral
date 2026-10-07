"""改名兼容层：Corral 之前叫 pickup，再早叫 SessionContinue。

本模块是源码里**唯一**仍含 ``pickup`` / ``PICKUP_`` / ``pickup-keepalive`` 字面量的地方
（测试里显式断言旧名兜底的用例除外）。其它模块必须通过这里的助手读写，
不能再把旧名散落到业务代码里。

兼容约定（一个发版周期）：
- 读环境变量：``CORRAL_*`` → ``PICKUP_*`` → ``SC_*``
- 写入托管会话：同时注入 ``CORRAL_*`` 和 ``PICKUP_*``，并保留已有的 ``SC_*``
- 缓存目录：默认 ``~/.cache/corral``；若新目录不存在而旧 ``pickup`` 目录存在，迁一次
- tmux：新建走 ``corral-keepalive`` / ``corral-``；发现/标注/回收/内嵌还要认
  ``pickup-keepalive`` 以及 ``pickup-`` / ``sc-`` 前缀
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 产品身份
# ---------------------------------------------------------------------------

PRODUCT_NAME = "Corral"
CLI_NAME = "corral"
PACKAGE_NAME = "corral"

# ---------------------------------------------------------------------------
# 环境变量前缀（查找顺序：新 → pickup 过渡名 → SessionContinue 更旧名）
# ---------------------------------------------------------------------------

ENV_PREFIX = "CORRAL_"
LEGACY_ENV_PREFIXES: tuple[str, ...] = ("PICKUP_", "SC_")
ALL_ENV_PREFIXES: tuple[str, ...] = (ENV_PREFIX, *LEGACY_ENV_PREFIXES)

# ---------------------------------------------------------------------------
# tmux：新建用新 socket/前缀；发现必须覆盖过渡名与更旧名
# ---------------------------------------------------------------------------

SOCKET_NAME = "corral-keepalive"
LEGACY_SOCKET_NAMES: tuple[str, ...] = ("pickup-keepalive",)
ALL_SOCKET_NAMES: tuple[str, ...] = (SOCKET_NAME, *LEGACY_SOCKET_NAMES)

SESSION_PREFIX = "corral-"
# Project shells (remote/shell_terminal.py): same server, a prefix every agent
# path ignores (it does not start with "corral-"), so scanning, adoption and
# reclaim never see them.
SHELL_SESSION_PREFIX = "corralsh-"
LEGACY_SESSION_PREFIXES: tuple[str, ...] = ("pickup-", "sc-")
ALL_SESSION_PREFIXES: tuple[str, ...] = (SESSION_PREFIX, *LEGACY_SESSION_PREFIXES)
# 仅更旧的 SessionContinue 前缀（部分测试/注释仍单独提到它）
LEGACY_SESSION_PREFIX = "sc-"

# ---------------------------------------------------------------------------
# 缓存 / 状态目录名
# ---------------------------------------------------------------------------

CACHE_DIRNAME = "corral"
LEGACY_CACHE_DIRNAMES: tuple[str, ...] = ("pickup", "session-continue")
STATE_DIRNAME = "corral"
LEGACY_STATE_DIRNAMES: tuple[str, ...] = ("pickup",)

# ---------------------------------------------------------------------------
# Pi 托管隔离目录：新建 corral-<ident>，发现仍认 pickup-<ident>
# ---------------------------------------------------------------------------

HOSTED_DIR_PREFIX = "corral-"
LEGACY_HOSTED_DIR_PREFIXES: tuple[str, ...] = ("pickup-",)

# ---------------------------------------------------------------------------
# 剪贴板图片哨兵：新粘贴用 CORRAL_；解析时仍认 PICKUP_（过渡期内已打开的页面）
# ---------------------------------------------------------------------------

IMG_SENTINEL_BEGIN = "\u241eCORRAL_IMG_BEGIN\u241e"
IMG_SENTINEL_END = "\u241eCORRAL_IMG_END\u241e"
LEGACY_IMG_SENTINEL_BEGIN = "\u241ePICKUP_IMG_BEGIN\u241e"
LEGACY_IMG_SENTINEL_END = "\u241ePICKUP_IMG_END\u241e"

# ---------------------------------------------------------------------------
# 命令拦截脚本：新守卫 + 过渡期 / 更旧期守卫
# ---------------------------------------------------------------------------

SHIM_ACTIVE_ENV = "CORRAL_SHIM_ACTIVE"
SHIM_VERSION_ENV = "CORRAL_SHIM_VERSION"
RUNTIME_ENV = "CORRAL_RUNTIME"
SESSION_ID_ENV = "CORRAL_SESSION_ID"
SHIM_PASSTHROUGH_ENVS: tuple[str, ...] = (
    "CORRAL_SHIM_ACTIVE",
    "CORRAL_RUNTIME",
    "PICKUP_SHIM_ACTIVE",
    "PICKUP_RUNTIME",
    "SC_RUNTIME",
)
SHIM_LAUNCH_ENVS: tuple[str, ...] = ("CORRAL_SHIM_ACTIVE", "PICKUP_SHIM_ACTIVE")


def shim_posix_guard_lines() -> list[str]:
    """命令拦截脚本里的放行守卫，避免业务模块写出旧环境变量名。"""
    return [f'    [ -n "${{{key}:-}}" ] && return 0' for key in SHIM_PASSTHROUGH_ENVS]


def shim_fish_guard_lines() -> list[str]:
    return [f"        if set -q {key}; return 0; end" for key in SHIM_PASSTHROUGH_ENVS]


def shim_posix_launch_prefix() -> str:
    return " ".join(f"{key}=1" for key in SHIM_LAUNCH_ENVS)


def shim_fish_launch_env() -> str:
    return " ".join(f"{key}=1" for key in SHIM_LAUNCH_ENVS)

# 扫描器从进程 environ 提取托管身份时要认的键（macOS ps eww 白名单）
PROCESS_ENV_KEYS: tuple[str, ...] = (
    "CORRAL_SESSION_ID",
    "PICKUP_SESSION_ID",
    "SC_SESSION_ID",
    "CORRAL_RUNTIME",
    "PICKUP_RUNTIME",
    "SC_RUNTIME",
    "PI_CODING_AGENT_SESSION_DIR",
    # Pi 身份桥：macOS `ps eww` 只提取白名单键；漏掉这两项会让 Linux
    # 精确绑定、Mac 却退回旧启发式。
    "CORRAL_PI_INSTANCE_ID",
    "CORRAL_PI_CLAIM_PATH",
)


def getenv(suffix: str, default: str | None = None) -> str | None:
    """读 ``CORRAL_{suffix}``，没有再读 ``PICKUP_{suffix}``，再没有读 ``SC_{suffix}``。

    以「变量是否出现在环境里」为准：显式设成空字符串也算命中（例如
    ``CORRAL_PROJECT_ROOTS=`` 表示跳过扫描），不会掉进更旧的名字。
    """
    for prefix in ALL_ENV_PREFIXES:
        key = prefix + suffix
        if key in os.environ:
            return os.environ[key]
    return default


def getenv_from(environ: object, suffix: str, default: str | None = None) -> str | None:
    """与 :func:`getenv` 相同，但从给定映射读取（供 i18n 测试注入）。"""
    for prefix in ALL_ENV_PREFIXES:
        key = prefix + suffix
        try:
            if key in environ:  # type: ignore[operator]
                return environ[key]  # type: ignore[index]
        except (TypeError, KeyError):
            continue
    return default


def env_is_set(suffix: str) -> bool:
    return any((prefix + suffix) in os.environ for prefix in ALL_ENV_PREFIXES)


def env_is_disabled(suffix: str) -> bool:
    """按查找顺序取第一个已设置的值，该值为 ``0`` 即视为关闭。"""
    raw = getenv(suffix)
    return raw is not None and raw.strip() == "0"


def pop_env(suffix: str) -> None:
    for prefix in ALL_ENV_PREFIXES:
        os.environ.pop(prefix + suffix, None)


def setenv_primary(suffix: str, value: str) -> None:
    os.environ[ENV_PREFIX + suffix] = value


def hosted_env_pairs(runtime_id: str, ident: str) -> list[str]:
    """托管会话 ``tmux new-session -e`` 参数：新名、过渡名、更旧名都注入。"""
    pairs: list[str] = []
    for prefix in ALL_ENV_PREFIXES:
        pairs += [
            "-e", f"{prefix}RUNTIME={runtime_id}",
            "-e", f"{prefix}SESSION_ID={ident}",
        ]
    if runtime_id == "codex":
        from corral.codex_identity import claim_env_pairs
        pairs += claim_env_pairs(ident)
    return pairs


def hosted_session_id(env: dict[str, str] | None) -> str:
    """从进程环境取出托管会话 id：CORRAL_ → PICKUP_ → SC_。"""
    if not env:
        return ""
    return (
        env.get("CORRAL_SESSION_ID")
        or env.get("PICKUP_SESSION_ID")
        or env.get("SC_SESSION_ID")
        or ""
    )


def hosted_runtime_id(env: dict[str, str] | None) -> str:
    if not env:
        return ""
    return (
        env.get("CORRAL_RUNTIME")
        or env.get("PICKUP_RUNTIME")
        or env.get("SC_RUNTIME")
        or ""
    )


def tmux_base_argv(socket: str | None = None) -> tuple[str, ...]:
    return ("tmux", "-L", socket or SOCKET_NAME)


def socket_for_session(name: str) -> str:
    """按会话名前缀选 socket：``corral-*`` 走新 socket，其余存量走过渡 socket。"""
    if name.startswith((SESSION_PREFIX, SHELL_SESSION_PREFIX)):
        return SOCKET_NAME
    return LEGACY_SOCKET_NAMES[0]


def tmux_argv_for_session(name: str) -> tuple[str, ...]:
    return tmux_base_argv(socket_for_session(name))


def is_managed_session(name: str) -> bool:
    return bool(name) and name.startswith(ALL_SESSION_PREFIXES)


def hosted_isolation_dirname(ident: str) -> str:
    return f"{HOSTED_DIR_PREFIX}{ident}"


def is_hosted_isolation_dir(directory: str) -> bool:
    base = os.path.basename(str(directory or "").rstrip("/"))
    return base.startswith((HOSTED_DIR_PREFIX, *LEGACY_HOSTED_DIR_PREFIXES))


def image_sentinel_payload(text: str) -> str | None:
    """若整段是图片哨兵包裹的载荷则返回中间部分；新旧哨兵都认。"""
    for begin, end in (
        (IMG_SENTINEL_BEGIN, IMG_SENTINEL_END),
        (LEGACY_IMG_SENTINEL_BEGIN, LEGACY_IMG_SENTINEL_END),
    ):
        if text.startswith(begin) and text.endswith(end):
            return text[len(begin) : -len(end)]
    return None


def _rename_once(src: Path, dest: Path) -> None:
    if dest.exists() or not src.exists():
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dest)
    except OSError:
        pass


def cache_dir() -> Path:
    """默认 ``~/.cache/corral``，尊重 ``CORRAL_CACHE_DIR`` → ``PICKUP_CACHE_DIR`` → XDG。

    新目录不存在且旧 ``pickup``（或更早的 ``session-continue``）目录存在时，迁一次。
    显式覆盖路径不做迁移，避免测试夹具误搬用户家目录。
    """
    override = getenv("CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    new = root / CACHE_DIRNAME
    if not new.exists():
        for old_name in LEGACY_CACHE_DIRNAMES:
            old = root / old_name
            if old.exists():
                _rename_once(old, new)
                break
    return new


def state_dir() -> Path:
    """默认 ``~/.local/state/corral``，尊重 ``CORRAL_STATE_DIR`` → ``PICKUP_STATE_DIR``。"""
    override = getenv("STATE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    new = root / STATE_DIRNAME
    if not new.exists():
        for old_name in LEGACY_STATE_DIRNAMES:
            old = root / old_name
            if old.exists():
                _rename_once(old, new)
                break
    return new
