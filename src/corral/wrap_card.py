"""`corral wrap` card: one 72×19 cell grid per animation time.

Pure and deterministic: ``compose(view, t)`` returns the frame at ``t`` seconds
after the reveal starts (``t=None`` is the final frame). The terminal widget and
the PNG exporter both draw these cells, so the shared image is exactly the final
terminal frame. Layout and storyboard: docs/design/SHAREABLE_WRAP_CARD_DESIGN.md §5.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime

from corral.wrap_stats import PERIOD_ALL, PERIOD_MONTH, WrapStats

WIDTH = 72
HEIGHT = 19
REPO_URL = "github.com/corral-dev/corral"

BG = "#14110F"
BORDER_DIM = "#3A332E"
BORDER = "#6B5F55"
TEXT = "#EDE6DE"
DIM = "#8A817A"
FAINT = "#4E4640"
RED = "#BA1F14"
RED_SHADE = "#4A1A14"
RED_HI = "#F0563A"
GOLD = "#D9A441"
GOLD_HI = "#FFD77A"
WHITE = "#FFFFFF"
HEAT = ("#262019", "#4A2A1E", "#7A2E1C", "#B83A1E", "#E8762B")

# Same block logo as the splash screen (toilet -f pagga "corral").
LOGO_LINES = (
    "░█▀▀░█▀█░█▀█░█▀█░█▀█░█░░",
    "░█░░░█░█░█▀▄░█▀▄░█▀█░█░░",
    "░▀▀▀░▀▀▀░▀░▀░▀░▀░▀░▀░▀▀▀",
)
LOGO_X, LOGO_Y = 3, 2
STAT_X = (3, 20, 37, 54)
BAR_X, BAR_W = 13, 56
HEAT_X, HEAT_Y = 13, 13

# Storyboard (seconds after the data is complete).
BORDER_END = 0.5
LOGO_START, LOGO_SPAN = 0.2, 0.7
NUM_START, NUM_STAGGER, NUM_DUR, FLASH = 0.6, 0.12, 1.2, 0.15
BAR_START, BAR_DUR = 1.2, 0.8
HEAT_START, HEAT_SPAN, POP = 1.6, 1.2, 0.1
PEAK_START, PEAK_BLINK = 2.6, 0.6
# Finale beams (TTE Beams-inspired): (row, start, direction).
BEAMS = ((7, 2.9, 1), (14, 3.05, -1))
BEAM_DUR, BEAM_TRAIL = 0.55, 12
REVEAL_END = 3.6
SHIMMER_EVERY, SHIMMER_DUR = 4.0, 0.6
NOISE = "░▒▓"


@dataclass(frozen=True)
class Cell:
    ch: str = " "
    fg: str = TEXT
    bg: str = BG
    bold: bool = False


Grid = list[list[Cell]]


@dataclass(frozen=True)
class CardView:
    """Everything the card shows, already formatted. Built once from WrapStats."""

    title_range: str
    period_label: str
    stats: tuple[tuple[str, float, str, str], ...]  # label, value, kind, detail
    shares: tuple[tuple[str, str, float], ...]  # name, color, fraction
    heat_levels: tuple[tuple[int, ...], ...]  # 7 × 24 levels 0..4
    peak_cell: tuple[int, int] | None
    disclosure: str
    progress: tuple[int, int] | None = None  # (done, total) while loading


def compact(value: float) -> str:
    """36,639 / 7.33B style; integers below a million keep separators."""
    value = float(value)
    if abs(value) < 1_000_000:
        return f"{int(round(value)):,}"
    for unit, size in (("B", 1e9), ("M", 1e6)):
        if abs(value) >= size:
            scaled = value / size
            digits = 2 if scaled < 10 else 1 if scaled < 100 else 0
            return f"{scaled:.{digits}f}{unit}"
    return f"{int(value):,}"


def _range_label(stats: WrapStats) -> tuple[str, str]:
    end = datetime.fromtimestamp(stats.period.end)
    if stats.period.key == PERIOD_ALL:
        return f"through {end:%b} {end.day}, {end:%Y}", "all history"
    start = datetime.fromtimestamp(stats.period.start)
    label = "last 30 days" if stats.period.key == PERIOD_MONTH else "last 7 days"
    return f"{start:%b} {start.day} - {end:%b} {end.day}, {end:%Y}", label


def _heat_levels(heatmap: list[list[float]]) -> tuple[tuple[int, ...], ...]:
    peak = max((v for row in heatmap for v in row), default=0.0)
    if peak <= 0:
        return tuple(tuple(0 for _ in range(24)) for _ in range(7))
    def level(v: float) -> int:
        if v <= 0:
            return 0
        return min(4, 1 + int(3.999 * math.sqrt(v / peak)))
    return tuple(tuple(level(v) for v in row) for row in heatmap)


def build_view(stats: WrapStats, names: dict[str, str], colors: dict[str, str],
               progress: tuple[int, int] | None = None) -> CardView:
    title_range, period_label = _range_label(stats)
    per_session = stats.tool_calls / stats.sessions if stats.sessions else 0
    peak_detail = (time.strftime("%a %H:%M", time.localtime(stats.peak_at))
                   if stats.peak_at and stats.peak_parallel else "")
    agents = stats.agents
    stat_rows = (
        ("SESSIONS", float(stats.sessions), "int", f"{agents} agent{'s' if agents != 1 else ''}"),
        ("TOOL CALLS", float(stats.tool_calls), "int", f"{per_session:,.0f} / session" if stats.sessions else ""),
        ("TOKENS", float(stats.tokens), "compact", "incl. cache"),
        ("PEAK PARALLEL", float(stats.peak_parallel), "int", peak_detail),
    )
    shares = tuple((names.get(rid, rid), colors.get(rid, DIM), frac) for rid, _, frac in stats.runtime_shares())
    notes = []
    if stats.tokens_uncovered:
        notes.append(f"{stats.tokens_uncovered} without token data")
    if stats.unreadable:
        notes.append(f"{stats.unreadable} unreadable")
    if stats.scan_failures:
        notes.append(f"{len(stats.scan_failures)} agent scan failed")
    return CardView(
        title_range=title_range,
        period_label=period_label,
        stats=stat_rows,
        shares=shares,
        heat_levels=_heat_levels(stats.heatmap),
        peak_cell=stats.peak_cell(),
        disclosure=" · ".join(notes),
        progress=progress,
    )


# ---------------------------------------------------------------- helpers


def _clamp(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _ease_out(x: float) -> float:
    return 1 - (1 - _clamp(x)) ** 3


def _ease_out_expo(x: float) -> float:
    x = _clamp(x)
    return 1.0 if x >= 1 else 1 - 2 ** (-10 * x)


def _hash01(*parts: int) -> float:
    """Deterministic pseudo-random value in [0, 1) for a cell."""
    h = 2166136261
    for part in parts:
        h = ((h ^ (part & 0xFFFFFFFF)) * 16777619) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0x5BD1E995) & 0xFFFFFFFF
    h ^= h >> 15
    return h / 2**32


def _blank() -> Grid:
    return [[Cell() for _ in range(WIDTH)] for _ in range(HEIGHT)]


def _put(grid: Grid, x: int, y: int, text: str, fg: str = TEXT, *, bold: bool = False,
         bg: str | None = None) -> None:
    for i, ch in enumerate(text):
        cx = x + i
        if 0 <= cx < WIDTH and 0 <= y < HEIGHT:
            grid[y][cx] = Cell(ch, fg, bg or grid[y][cx].bg, bold)


def _perimeter() -> list[tuple[int, int]]:
    top = [(x, 0) for x in range(WIDTH)]
    right = [(WIDTH - 1, y) for y in range(1, HEIGHT)]
    bottom = [(x, HEIGHT - 1) for x in range(WIDTH - 2, -1, -1)]
    left = [(0, y) for y in range(HEIGHT - 2, 0, -1)]
    return top + right + bottom + left


_PERIMETER = _perimeter()


def _border_char(x: int, y: int) -> str:
    if (x, y) == (0, 0):
        return "╭"
    if (x, y) == (WIDTH - 1, 0):
        return "╮"
    if (x, y) == (0, HEIGHT - 1):
        return "╰"
    if (x, y) == (WIDTH - 1, HEIGHT - 1):
        return "╯"
    return "─" if y in (0, HEIGHT - 1) else "│"


def _border_text(view: CardView) -> dict[tuple[int, int], str]:
    """Characters embedded in the border: repo URL bottom-right, disclosure top-right."""
    out: dict[tuple[int, int], str] = {}
    url = f" {REPO_URL} "
    for i, ch in enumerate(url):
        out[(WIDTH - 3 - len(url) + i, HEIGHT - 1)] = ch
    if view.disclosure:
        note = f" {view.disclosure} "[: WIDTH - 8]
        for i, ch in enumerate(note):
            out[(WIDTH - 3 - len(note) + i, 0)] = ch
    return out


# ---------------------------------------------------------------- layers


def _draw_border(grid: Grid, view: CardView, t: float | None, loading_t: float | None) -> None:
    embedded = _border_text(view)
    total = len(_PERIMETER)
    if loading_t is not None:
        head = (loading_t / 1.6 % 1.0) * total
        lit_until = -1.0
    elif t is None or t >= BORDER_END:
        head, lit_until = None, float(total)
    else:
        lit_until = total * _ease_out(t / BORDER_END)
        head = lit_until
    for index, (x, y) in enumerate(_PERIMETER):
        ch = embedded.get((x, y), _border_char(x, y))
        is_text = (x, y) in embedded
        lit = index < lit_until
        color = DIM if is_text and lit else BORDER if lit else BORDER_DIM
        if is_text and not lit:
            ch = _border_char(x, y)
        if head is not None:
            behind = (head - index) % total if loading_t is not None else head - index
            if 0 <= behind < 1:
                color = GOLD_HI
            elif 0 <= behind < 7:
                color = GOLD
        grid[y][x] = Cell(ch, color)


def _logo_final(ch: str) -> Cell:
    if ch == "░":
        return Cell("░", RED_SHADE)
    return Cell(ch, RED, bold=True)


def _draw_logo(grid: Grid, t: float | None, loading_t: float | None, idle_t: float | None) -> None:
    frame = int((loading_t if loading_t is not None else (t or 0)) * 20)
    shimmer_x = None
    if idle_t is not None:
        phase = idle_t % SHIMMER_EVERY
        if phase < SHIMMER_DUR:
            shimmer_x = -6 + (len(LOGO_LINES[0]) + 12) * (phase / SHIMMER_DUR)
    for r, line in enumerate(LOGO_LINES):
        for c, ch in enumerate(line):
            x, y = LOGO_X + c, LOGO_Y + r
            final = _logo_final(ch)
            if loading_t is not None:
                if ch == "░":
                    grid[y][x] = final
                else:
                    pick = NOISE[int(_hash01(r, c, frame) * 3)]
                    grid[y][x] = Cell(pick, RED_SHADE if _hash01(c, r, frame) < 0.6 else RED)
                continue
            if t is not None and ch != "░":
                reveal = LOGO_START + LOGO_SPAN * _hash01(r, c, 7)
                if t < reveal:
                    grid[y][x] = Cell(NOISE[int(_hash01(r, c, frame) * 3)], RED_SHADE)
                    continue
                if t < reveal + 0.08:
                    grid[y][x] = Cell(ch, WHITE, bold=True)
                    continue
            if shimmer_x is not None and ch != "░" and abs((c - r * 2) - shimmer_x) < 2.5:
                grid[y][x] = Cell(ch, GOLD_HI, bold=True)
                continue
            grid[y][x] = final


def _draw_title(grid: Grid, view: CardView, t: float | None) -> None:
    right = WIDTH - 3
    title = "W R A P"
    _put(grid, right - len(title), 2, title, GOLD, bold=True)
    if view.progress is not None:
        done, total = view.progress
        line = "scanning history" if total < 0 else f"reading {done:,} / {total:,} sessions"
        _put(grid, right - len(line), 3, line, DIM)
        _put(grid, right - len(view.period_label), 4, view.period_label, FAINT)
        return
    _put(grid, right - len(view.title_range), 3, view.title_range, TEXT)
    _put(grid, right - len(view.period_label), 4, view.period_label, DIM)


def _format_value(value: float, kind: str) -> str:
    return compact(value) if kind == "compact" else f"{int(round(value)):,}"


def _draw_stats(grid: Grid, view: CardView, t: float | None) -> None:
    for i, (label, value, kind, detail) in enumerate(view.stats):
        x = STAT_X[i]
        _put(grid, x, 6, label, DIM)
        if view.progress is not None:
            _put(grid, x, 7, _format_value(value, kind), DIM, bold=True)
            continue
        start = NUM_START + NUM_STAGGER * i
        if t is None or t >= start + NUM_DUR + FLASH:
            shown, color = value, TEXT
        elif t < start:
            shown, color = 0.0, FAINT
        elif t < start + NUM_DUR:
            shown, color = value * _ease_out_expo((t - start) / NUM_DUR), TEXT
        else:
            shown, color = value, GOLD_HI
        _put(grid, x, 7, _format_value(shown, kind), color, bold=True)
        if i == 3 and detail:
            if t is None or t >= PEAK_START + PEAK_BLINK:
                _put(grid, x, 8, detail, DIM)
            elif t >= PEAK_START:
                typed = int(len(detail) * _clamp((t - PEAK_START) / PEAK_BLINK)) + 1
                _put(grid, x, 8, detail[:typed], GOLD)
        elif detail and (t is None or t >= start + NUM_DUR):
            _put(grid, x, 8, detail, DIM)


def _share_widths(view: CardView) -> list[int]:
    widths = [max(1, round(frac * BAR_W)) for _, _, frac in view.shares]
    while widths and sum(widths) > BAR_W:
        widths[widths.index(max(widths))] -= 1
    if widths and sum(widths) < BAR_W:
        widths[0] += BAR_W - sum(widths)
    return widths


def _draw_agents(grid: Grid, view: CardView, t: float | None) -> None:
    _put(grid, 3, 10, "AGENTS", DIM)
    if view.progress is not None or not view.shares:
        _put(grid, BAR_X, 10, "─" * BAR_W, FAINT)
        return
    colors: list[str] = []
    for (_, color, _), width in zip(view.shares, _share_widths(view), strict=True):
        colors.extend([color] * width)
    grow = BAR_W if t is None else BAR_W * _ease_out((t - BAR_START) / BAR_DUR)
    for i in range(BAR_W):
        if i < int(grow):
            grid[10][BAR_X + i] = Cell("█", colors[i])
        elif i == int(grow) and grow < BAR_W and t is not None and t >= BAR_START:
            grid[10][BAR_X + i] = Cell("▌", WHITE)
        else:
            grid[10][BAR_X + i] = Cell("─", FAINT)
    x = BAR_X
    for k, (name, color, frac) in enumerate(view.shares):
        item = f"{name} {frac * 100:.0f}%"
        if x + 2 + len(item) > BAR_X + BAR_W:
            rest = len(view.shares) - k
            _put(grid, x, 11, f"+{rest}", DIM)
            break
        appear = BAR_START + 0.2 + 0.15 * k
        if t is not None and t < appear:
            break
        fresh = t is not None and t < appear + 0.12
        _put(grid, x, 11, "●", color)
        _put(grid, x + 2, 11, item, DIM if fresh else TEXT)
        x += len(item) + 4


def _heat_color(level: int, d: int, h: int, view: CardView, t: float | None) -> str:
    is_peak = view.peak_cell == (d, h)
    if view.progress is not None:
        return HEAT[0]
    if t is not None:
        reveal = HEAT_START + HEAT_SPAN * ((h + 2 * d) / (23 + 12))
        if t < reveal:
            return HEAT[0] if t >= HEAT_START - 0.2 else BG
        if t < reveal + POP and level:
            return GOLD_HI if level >= 3 else HEAT[min(4, level + 1)]
        if is_peak and t < PEAK_START + PEAK_BLINK:
            if t >= PEAK_START:
                return GOLD_HI if int((t - PEAK_START) / 0.1) % 2 == 0 else HEAT[level]
    if is_peak and (t is None or t >= PEAK_START):
        return GOLD_HI
    return HEAT[level]


def _draw_heatmap(grid: Grid, view: CardView, t: float | None) -> None:
    _put(grid, 3, HEAT_Y, "ACTIVITY", DIM)
    _put(grid, 3, HEAT_Y + 1, "Mon-Sun", FAINT)
    _put(grid, 3, HEAT_Y + 2, "by hour", FAINT)
    for pair in range(4):
        y = HEAT_Y + pair
        top_day, bottom_day = pair * 2, pair * 2 + 1
        for h in range(24):
            x = HEAT_X + h * 2
            top = _heat_color(view.heat_levels[top_day][h], top_day, h, view, t)
            if bottom_day < 7:
                bottom = _heat_color(view.heat_levels[bottom_day][h], bottom_day, h, view, t)
            else:
                bottom = BG
            grid[y][x] = Cell("▀", top, bottom)
            grid[y][x + 1] = Cell("▀", top, bottom)
    axis_y = HEAT_Y + 4
    for hour, label in ((0, "0h"), (6, "6h"), (12, "12h"), (18, "18h")):
        _put(grid, HEAT_X + hour * 2, axis_y, label, FAINT)
    _put(grid, HEAT_X + 47 - 2, axis_y, "24h", FAINT)
    side = HEAT_X + 50
    _put(grid, side, HEAT_Y, "busiest", FAINT)
    if view.peak_cell and view.progress is None:
        d, h = view.peak_cell
        if t is None or t >= PEAK_START:
            _put(grid, side, HEAT_Y + 1, "■", GOLD_HI)
            _put(grid, side + 2, HEAT_Y + 1, ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[d], TEXT)
            _put(grid, side + 2, HEAT_Y + 2, f"{h:02d}h", TEXT)


def _draw_beams(grid: Grid, t: float | None) -> None:
    if t is None:
        return
    for row, start, direction in BEAMS:
        if not (start <= t < start + BEAM_DUR):
            continue
        travel = -BEAM_TRAIL + (WIDTH + 2 * BEAM_TRAIL) * _ease_out((t - start) / BEAM_DUR)
        head = travel if direction > 0 else WIDTH - 1 - travel
        for x in range(1, WIDTH - 1):
            behind = (head - x) * direction
            if not (0 <= behind < BEAM_TRAIL):
                continue
            cell = grid[row][x]
            color = WHITE if behind < 1 else GOLD_HI if behind < 4 else GOLD
            if cell.ch.strip():
                grid[row][x] = Cell(cell.ch, color, cell.bg, True)
            elif behind < 6:
                grid[row][x] = Cell("━" if behind < 2 else "─", color, cell.bg)


# ---------------------------------------------------------------- frames


def compose(view: CardView, t: float | None, *, loading_t: float | None = None,
            idle_t: float | None = None) -> Grid:
    """Frame at reveal time ``t`` (None = final). ``loading_t`` draws the loading state."""
    grid = _blank()
    if loading_t is not None:
        t = None
    _draw_border(grid, view, t, loading_t)
    _draw_logo(grid, t, loading_t, idle_t)
    _draw_title(grid, view, t)
    _draw_stats(grid, view, t)
    _draw_agents(grid, view, t)
    _draw_heatmap(grid, view, t)
    _draw_beams(grid, t)
    return grid


def reveal_done(t: float) -> bool:
    return t >= REVEAL_END


def shimmer_active(idle_t: float) -> bool:
    return idle_t % SHIMMER_EVERY < SHIMMER_DUR
