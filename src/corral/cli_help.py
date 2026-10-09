"""Lightweight help parsers shared by bootstrap and the interactive entry."""

from __future__ import annotations

import argparse

from corral.i18n import t


def build_main_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="corral",
        usage="%(prog)s [options]\n       %(prog)s <command> [args]",
        description=t("cli.help.description"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=t("cli.help.epilog"),
    )
    parser.add_argument("--limit", type=int, default=50, help=t("cli.help.limit"))
    parser.add_argument("--json", action="store_true", dest="json_mode",
                        help=t("cli.help.json"))
    parser.add_argument("--no-input", action="store_true", dest="no_input",
                        help=t("cli.help.no_input"))
    parser.add_argument("--no-keepalive", action="store_true", dest="no_keepalive",
                        help=t("cli.help.no_keepalive"))
    parser.add_argument("--no-color", action="store_true", dest="no_color",
                        help=t("cli.help.no_color"))
    parser.add_argument("-d", "--debug", "--verbose", action="store_true", dest="debug",
                        help=t("cli.help.debug"))
    parser.add_argument("-q", "--quiet", action="store_true", dest="quiet",
                        help=t("cli.help.quiet"))
    parser.add_argument("--version", "-V", "-v", action="store_true", dest="show_version",
                        help=t("cli.help.version"))
    parser.add_argument("--generate-titles", action="store_true", dest="generate_titles",
                        help=argparse.SUPPRESS)  # 内部用途：TUI 拉起的后台标题生成进程
    return parser


def build_update_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="corral update",
        description=t("cli.help.update_description"),
        epilog=t("cli.help.update_epilog"),
    )
