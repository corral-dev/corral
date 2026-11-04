"""`corral wrap`: shareable usage card (animated terminal card, PNG, JSON).

Design: docs/design/SHAREABLE_WRAP_CARD_DESIGN.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable

from corral import theme, wrap_card, wrap_stats
from corral.i18n import t
from corral.wrap_stats import PERIOD_ALL, PERIOD_MONTH, PERIOD_WEEK, Period, SessionWrap, WrapStats

SCAN_LIMIT = 1000
SCAN_LIMIT_ALL = 100_000
PROGRESS_INTERVAL = 0.1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corral wrap", description=t("wrap.help.description"))
    span = parser.add_mutually_exclusive_group()
    span.add_argument("--month", action="store_true", help=t("wrap.help.month"))
    span.add_argument("--all", action="store_true", help=t("wrap.help.all"))
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--png", metavar="PATH", help=t("wrap.help.png"))
    out.add_argument("--json", action="store_true", help=t("wrap.help.json"))
    parser.add_argument("--no-anim", action="store_true", help=t("wrap.help.no_anim"))
    return parser


def _period_key(args: argparse.Namespace) -> str:
    return PERIOD_ALL if args.all else PERIOD_MONTH if args.month else PERIOD_WEEK


def scan(registry, period: Period) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Corral's listing for every runtime, widening a runtime's limit when truncated."""
    from corral import agent_api

    runtimes = list(registry)
    limit = SCAN_LIMIT_ALL if period.key == PERIOD_ALL else SCAN_LIMIT
    scanned, failures, _ = agent_api._list_scan(registry, runtimes, limit, fresh=False)
    while limit < SCAN_LIMIT_ALL:
        truncated = [rt for rt in runtimes
                     if len(scanned.get(rt.id) or []) >= limit
                     and min(float(s.get("mtime") or 0) for s in scanned[rt.id]) >= period.start]
        if not truncated:
            break
        limit *= 4
        more, more_failures = agent_api._scan_runtimes(truncated, limit)
        scanned.update(more)
        failures.update(more_failures)
    return scanned, failures


def make_loader(registry, period: Period) -> Callable[[Callable[[WrapStats, int, int], None] | None], WrapStats]:
    def load(progress: Callable[[WrapStats, int, int], None] | None = None) -> WrapStats:
        scanned, failures = scan(registry, period)
        sessions = wrap_stats.candidates(scanned, period)
        seen: list[SessionWrap] = []
        last = [0.0]

        def on_item(done: int, total: int, item: SessionWrap) -> None:
            seen.append(item)
            now = time.monotonic()
            if progress and (now - last[0] >= PROGRESS_INTERVAL or done == total):
                last[0] = now
                progress(wrap_stats.aggregate(period, seen, failures), done, total)

        if progress:
            progress(WrapStats(period=period), 0, len(sessions))
        return wrap_stats.collect(period, sessions, on_item, scan_failures=failures)

    return load


def make_view_factory(registry, period: Period):
    names = {rt.id: rt.display_name for rt in registry}
    colors = dict(theme.RUNTIME_LABEL_STYLES)

    def factory(stats: WrapStats | None, progress: tuple[int, int] | None = None) -> wrap_card.CardView:
        return wrap_card.build_view(stats or WrapStats(period=period), names, colors, progress=progress)

    return factory


def print_static(grid: wrap_card.Grid) -> None:
    from rich.console import Console
    from rich.text import Text

    console = Console(highlight=False)
    for row in grid:
        line = Text()
        for cell in row:
            line.append(cell.ch, style=f"{'bold ' if cell.bold else ''}{cell.fg} on {cell.bg}")
        console.print(line, crop=False, soft_wrap=True)


def main(argv: list[str]) -> int:
    from corral import i18n
    from corral.runtime import default_registry

    i18n.init()
    args = build_parser().parse_args(argv)
    registry = default_registry()
    period = wrap_stats.make_period(_period_key(args))
    loader = make_loader(registry, period)
    view_factory = make_view_factory(registry, period)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if args.json or args.png or not interactive:
        stats = loader(None)
        if args.json:
            print(json.dumps({"ok": True, "data": stats.to_dict(), "error": None, "meta": {"version": 1}},
                             ensure_ascii=False))
            return 0
        grid = wrap_card.compose(view_factory(stats), None)
        if args.png:
            from corral.wrap_png import write_png

            try:
                print(write_png(grid, args.png))
            except OSError as exc:
                print(t("wrap.save_failed", error=exc.strerror or str(exc)), file=sys.stderr)
                return 1
            return 0
        print_static(grid)
        return 0

    from corral.ui.app import restore_terminal
    from corral.ui.wrap_app import WrapApp

    wrap_stats.prepare_workers()
    app = WrapApp(loader, view_factory, animate=not args.no_anim)
    try:
        app.run()
    except KeyboardInterrupt:
        restore_terminal()
        return 130
    if app.error:
        print(t("wrap.load_failed", error=app.error), file=sys.stderr)
        return 1
    final = app.final_grid()
    if final is not None:
        # Leave the card in the scrollback so it can be screenshotted after exit.
        print_static(final)
    return 0
