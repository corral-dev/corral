"""极轻量命令分发：快速命令不加载 Textual、网络更新器或会话扫描器。"""

from __future__ import annotations

import os
import sys

from corral import __version__

_AGENT_ROOTS = {"list", "search", "show", "export", "share", "context", "plan", "describe", "diagnose"}


def _fast_version() -> None:
    import corral

    package_file = os.path.abspath(corral.__file__ or "")
    print(f"corral {__version__}")
    print(f"  package_file: {package_file}")
    print(f"  python:       {sys.executable}")


def _migrate_pi_history() -> None:
    """交互启动时幂等补齐旧 Pi 历史；失败不阻断 Corral 首屏。"""
    try:
        from corral.pi_migration import migrate_legacy_sessions

        migrate_legacy_sessions()
    except Exception:
        # 迁移本身逐目录记录错误；这里只兜住磁盘/环境级异常。源文件从不删除，
        # 因此失败时安全地留待下次启动重试。
        pass


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in {"--version", "-V", "-v"}:
        _fast_version()
        return
    if argv[:1] == ["cache"]:
        from corral.cache_cli import main as cache_main

        raise SystemExit(cache_main(argv[1:]))
    if argv and argv[0] in _AGENT_ROOTS:
        from corral.agent_api import dispatch

        raise SystemExit(dispatch(argv))
    if argv[:1] == ["update"]:
        from corral.updater import cli_update

        raise SystemExit(cli_update())
    if argv[:1] == ["remote"]:
        # 手机端接力的常驻服务与配对命令：不进 TUI，也不碰只读接口。
        from corral.remote.cli import main as remote_main

        raise SystemExit(remote_main(argv[1:]))
    if argv and argv[0] in {"login", "logout", "whoami"}:
        from corral.remote.cli import main as remote_main

        raise SystemExit(remote_main(argv))
    if argv[:1] == ["shim"]:
        # 命令拦截的安装/卸载/检查只读写 shell 配置，不碰扫描器、Textual 和 tmux。
        from corral.shim import cli_main as shim_main

        raise SystemExit(shim_main(argv[1:]))
    # 命令拦截默认关闭：只有用户显式 `corral shim install` 才写 shell 配置。
    # 交互启动仍可补齐旧 Pi 历史；Agent 只读接口、管道和版本查询不碰迁移。
    if sys.stdin.isatty() and sys.stdout.isatty():
        _migrate_pi_history()
    from corral.cli import main as cli_main

    cli_main()
