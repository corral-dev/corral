"""Animated `corral wrap` card (Textual).

Loading runs in a worker thread and feeds real partial totals into the
loading frame; the reveal storyboard and idle shimmer come from
``corral.wrap_card.compose``. The timer runs only while something moves.
Design: docs/design/SHAREABLE_WRAP_CARD_DESIGN.md §5.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from rich.segment import Segment
from rich.style import Style
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Static

from corral import wrap_card
from corral.i18n import t
from corral.wrap_stats import WrapStats

FPS = 30


def default_png_path(now: float | None = None) -> str:
    stamp = datetime.fromtimestamp(time.time() if now is None else now).strftime("%Y%m%d")
    return f"corral-wrap-{stamp}.png"


def grid_strip(row: list[wrap_card.Cell]) -> Strip:
    segments = [Segment(c.ch, Style(color=c.fg, bgcolor=c.bg, bold=c.bold)) for c in row]
    return Strip(segments, wrap_card.WIDTH)


class WrapCard(Widget):
    DEFAULT_CSS = f"""
    WrapCard {{ width: {wrap_card.WIDTH}; height: {wrap_card.HEIGHT}; }}
    """

    def __init__(self) -> None:
        super().__init__()
        self.grid: wrap_card.Grid = []

    def show(self, grid: wrap_card.Grid) -> None:
        self.grid = grid
        self.refresh()

    def render_line(self, y: int) -> Strip:
        if 0 <= y < len(self.grid):
            return grid_strip(self.grid[y])
        return Strip.blank(self.size.width)


class WrapApp(App):
    """Loads stats with ``loader(progress)``, then plays the card."""

    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { align: center middle; }
    #wrap-hint { width: auto; height: 1; color: $text-muted; margin-top: 1; }
    #wrap-small { width: auto; height: auto; color: $text-muted; display: none; }
    Screen.-small WrapCard, Screen.-small #wrap-hint { display: none; }
    Screen.-small #wrap-small { display: block; }
    """
    BINDINGS = [
        Binding("s", "save", show=False),
        Binding("r", "replay", show=False),
        Binding("q", "quit", show=False),
        Binding("escape", "quit", show=False),
    ]

    def __init__(self, loader: Callable[[Callable[[WrapStats, int, int], None]], WrapStats],
                 view_factory: Callable[..., wrap_card.CardView], *, animate: bool = True) -> None:
        super().__init__()
        self._loader = loader
        self._view_factory = view_factory
        self._animate = animate
        self.stats: WrapStats | None = None
        self.error: str | None = None
        self._view: wrap_card.CardView | None = None
        self._loading_since = time.monotonic()
        self._reveal_since: float | None = None
        self._idle_since: float | None = None
        self._tick = None
        self._hint_reset = None

    def compose(self) -> ComposeResult:
        yield WrapCard()
        yield Static(t("wrap.hint"), id="wrap-hint")
        yield Static(t("wrap.too_small", w=wrap_card.WIDTH, h=wrap_card.HEIGHT + 2), id="wrap-small")

    def on_mount(self) -> None:
        if self.animation_level == "none":
            self._animate = False
        self._check_size()
        self._tick = self.set_interval(1 / FPS, self._on_tick, pause=not self._animate)
        self.run_worker(self._load, thread=True, exclusive=True)
        self._render_frame()

    def on_resize(self) -> None:
        self._check_size()

    def _check_size(self) -> None:
        small = self.size.width < wrap_card.WIDTH or self.size.height < wrap_card.HEIGHT + 2
        self.screen.set_class(small, "-small")

    # ------------------------------------------------------------ loading

    def _load(self) -> None:
        def progress(partial: WrapStats, done: int, total: int) -> None:
            self.call_from_thread(self._on_progress, partial, done, total)

        try:
            stats = self._loader(progress)
        except Exception as exc:  # noqa: BLE001 — shown to the user, then exit
            self.call_from_thread(self._on_failed, f"{type(exc).__name__}: {exc}")
            return
        self.call_from_thread(self._on_loaded, stats)

    def _on_progress(self, partial: WrapStats, done: int, total: int) -> None:
        if self.stats is not None:
            return
        self._view = self._view_factory(partial, progress=(done, total))
        if not self._animate:
            self._render_frame()

    def _on_failed(self, message: str) -> None:
        self.error = message
        self.exit()

    def _on_loaded(self, stats: WrapStats) -> None:
        self.stats = stats
        self._view = self._view_factory(stats)
        if self._animate:
            self._reveal_since = time.monotonic()
            self._tick.resume()
        else:
            self._idle_since = None
        self._render_frame()

    # ------------------------------------------------------------ frames

    def _frame(self) -> wrap_card.Grid | None:
        view = self._view
        now = time.monotonic()
        if view is None:
            view = self._view_factory(None, progress=(0, -1))
        if self.stats is None:
            return wrap_card.compose(view, None, loading_t=(now - self._loading_since) if self._animate else 0.0)
        if self._reveal_since is not None:
            reveal_t = now - self._reveal_since
            if not wrap_card.reveal_done(reveal_t):
                return wrap_card.compose(view, reveal_t)
            self._reveal_since = None
            self._idle_since = now
        idle_t = (now - self._idle_since) if (self._idle_since is not None and self._animate) else None
        if idle_t is not None and not wrap_card.shimmer_active(idle_t):
            self._sleep_until_shimmer(idle_t)
            idle_t = None
        return wrap_card.compose(view, None, idle_t=idle_t)

    def _sleep_until_shimmer(self, idle_t: float) -> None:
        if self._tick is not None:
            self._tick.pause()
        wait = wrap_card.SHIMMER_EVERY - (idle_t % wrap_card.SHIMMER_EVERY)
        self.set_timer(wait, self._wake)

    def _wake(self) -> None:
        if self._idle_since is not None and self._tick is not None:
            self._tick.resume()

    def _on_tick(self) -> None:
        self._render_frame()

    def _render_frame(self) -> None:
        grid = self._frame()
        if grid is not None:
            self.query_one(WrapCard).show(grid)

    # ------------------------------------------------------------ actions

    def final_grid(self) -> wrap_card.Grid | None:
        if self._view is None or self.stats is None:
            return None
        return wrap_card.compose(self._view, None)

    def _save(self, path: str) -> None:
        """Encode off the UI thread; the hint line reports the result."""
        grid = self.final_grid()
        if grid is None:
            return

        def write() -> None:
            from corral.wrap_png import write_png

            try:
                message = t("wrap.saved", path=write_png(grid, path))
            except OSError as exc:
                message = t("wrap.save_failed", error=exc.strerror or str(exc))
            self.call_from_thread(self._show_hint, message)

        self.run_worker(write, thread=True, group="wrap-save")

    def _show_hint(self, message: str) -> None:
        hint = self.query_one("#wrap-hint", Static)
        hint.update(message)
        if self._hint_reset is not None:
            self._hint_reset.stop()
        self._hint_reset = self.set_timer(4.0, lambda: hint.update(t("wrap.hint")))

    def action_save(self) -> None:
        self._save(default_png_path())

    def action_replay(self) -> None:
        if self.stats is None or not self._animate:
            return
        self._idle_since = None
        self._reveal_since = time.monotonic()
        self._tick.resume()
