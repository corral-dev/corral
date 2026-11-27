"""极轻量命令分发：快速命令不加载 Textual、网络更新器或会话扫描器。"""

from __future__ import annotations

import os
import sys

from corral import __version__

_AGENT_ROOTS = {"list", "search", "show", "export", "share", "context", "plan", "describe", "diagnose"}
_AGENT_ROOTS |= {"run", "skills", "send", "peers", "a2a"}
_CORE_OR_RUNTIME_ROOTS = _AGENT_ROOTS | {
    "cache", "remote", "login", "logout", "whoami", "shim", "observer", "tcc",
    "update", "plugin", "internal", "flex", "_cursor-hook",
    "claude", "codex", "opencode", "cursor", "pi", "antigravity", "kimi",
}


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
    # Runtime help after --no-keepalive belongs to the runtime passthrough route.
    runtime_passthrough = (
        argv[:1] == ["--no-keepalive"] and len(argv) > 1 and not argv[1].startswith("-")
    )
    if (
        argv and argv[0].startswith("-") and not runtime_passthrough
        and any(arg in {"--help", "-h"} for arg in argv)
    ):
        from corral.cli_help import build_main_parser

        build_main_parser().parse_args(argv)
        return
    if argv and argv[0] in {"--version", "-V", "-v"}:
        _fast_version()
        return
    if argv[:1] == ["cache"]:
        from corral.cache_cli import main as cache_main

        raise SystemExit(cache_main(argv[1:]))
    if argv[:1] == ["plugin"]:
        from corral.plugins import cli_main as plugin_main

        raise SystemExit(plugin_main(argv[1:]))
    if argv[:1] == ["internal"]:
        from corral.plugins import internal_cli_main as internal_main

        raise SystemExit(internal_main(argv[1:]))
    if argv and argv[0] in _AGENT_ROOTS:
        from corral.agent_api import dispatch

        raise SystemExit(dispatch(argv))
    if argv[:1] == ["update"]:
        from corral.cli_help import build_update_parser

        args = build_update_parser().parse_args(argv[1:])
        from corral.updater import cli_update

        raise SystemExit(
            cli_update(
                target_version=args.target_version,
                status_only=args.status,
                json_mode=args.json_mode,
                sync_mac=not args.no_mac,
            )
        )
    if argv[:1] == ["remote"]:
        # 手机端接力的常驻服务与配对命令：不进 TUI，也不碰只读接口。
        from corral.remote.cli import main as remote_main

        raise SystemExit(remote_main(argv[1:]))
    if argv and argv[0] in {"login", "logout", "whoami"}:
        from corral.remote.cli import main as remote_main

        raise SystemExit(remote_main(argv, prog="corral"))
    if argv[:1] == ["shim"]:
        # 命令拦截的安装/卸载/检查只读写 shell 配置，不碰扫描器、Textual 和 tmux。
        from corral.shim import cli_main as shim_main

        raise SystemExit(shim_main(argv[1:]))
    if argv[:1] == ["observer"]:
        from corral.cursor_observer import cli_main as observer_main

        raise SystemExit(observer_main(argv[1:]))
    if argv[:1] == ["tcc"]:
        # macOS TCC 稳定身份：只碰跳板/探针，不碰扫描器、Textual 与 tmux。
        from corral.tcc import cli_main as tcc_main

        raise SystemExit(tcc_main(argv[1:]))
    if argv and not argv[0].startswith("-") and argv[0] not in _CORE_OR_RUNTIME_ROOTS:
        from corral import plugins
        from corral.cli_contract import EXIT_NOT_FOUND, corral_bin, error_envelope

        plugin = plugins.resolve_command(argv[0])
        if plugin is not None:
            raise SystemExit(plugins.exec_plugin(plugin, argv))
        path_bin = plugins.resolve_path_plugin(argv[0])
        if path_bin is not None:
            raise SystemExit(plugins.exec_path_plugin(path_bin, argv[1:]))
        official = plugins.official_hint_for_command(argv[0])
        if official is not None:
            import json

            install_cmd = f"{corral_bin()} plugin install {official}"
            if "--json" in argv or not sys.stdout.isatty():
                print(
                    json.dumps(
                        error_envelope(
                            "plugin_not_installed",
                            f"Plugin '{official}' is not installed (required for 'corral {argv[0]}').",
                            hint=f"Install it with: {install_cmd}",
                            next_commands=[install_cmd],
                        ),
                        ensure_ascii=False,
                    )
                )
            else:
                print(
                    f"Plugin '{official}' is not installed. Install with:\n  {install_cmd}",
                    file=sys.stderr,
                )
            raise SystemExit(EXIT_NOT_FOUND)
    # 命令拦截默认关闭：只有用户显式 `corral shim install` 才写 shell 配置。
    # 交互启动仍可补齐旧 Pi 历史；Agent 只读接口、管道和版本查询不碰迁移。
    if sys.stdin.isatty() and sys.stdout.isatty():
        _migrate_pi_history()
        from corral.skill_bundle import auto_register

        auto_register()
    from corral.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
