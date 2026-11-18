"""文本宽度/截断/折行工具：按终端显示宽度处理 CJK 与 emoji。

从 display.py 迁入（纯移动，零行为变化）：这些是跨层共用的通用文本工具，
不属于「会话展示状态」层。宽度口径与 Rich/Textual 的渲染宽度表一致
（`rich.cells.cell_len`），不要换回 `unicodedata.east_asian_width` 自实现。
"""

from __future__ import annotations

import unicodedata

from rich.cells import cell_len as _rich_cell_len
from rich.cells import chop_cells as _rich_chop_cells


def text_width(text: str) -> int:
    # cell_len 直接对整段文本计算（内部已经处理了宽字符/组合字符的展开），
    # 比逐字符调用 cell_len 再求和更准也更省——逐字符调用在 emoji 等需要
    # 上下文判断的场景下反而会算错。
    return _rich_cell_len(text)


def fit_cell(text: object, width: int, *, ellipsis: bool = False) -> str:
    """按终端显示宽度截断并补齐，避免中文和图标把表格列挤歪。

    ellipsis=True 时，放不下的尾部换成 `...`（按显示宽度计算，CJK/emoji 安全）。
    """
    if width <= 0:
        return ""
    raw = str(text)
    if ellipsis and text_width(raw) > width:
        marker = "..."
        if width <= text_width(marker):
            chunks = _rich_chop_cells(marker, width)
            fitted = chunks[0] if chunks else ""
        else:
            body = (_rich_chop_cells(raw, width - text_width(marker)) or [""])[0]
            fitted = body + marker
        return fitted + " " * (width - text_width(fitted))
    chunks = _rich_chop_cells(raw, width)
    fitted = chunks[0] if chunks else ""
    return fitted + " " * (width - text_width(fitted))


def fit_cell_right(text: object, width: int) -> str:
    """按终端显示宽度截断并右对齐补齐（数值列用）。"""
    if width <= 0:
        return ""
    chunks = _rich_chop_cells(str(text), width)
    fitted = chunks[0] if chunks else ""
    return " " * (width - text_width(fitted)) + fitted


def wrap_preview_text(text: str, width: int) -> list[str]:
    """按终端显示宽度折行，并移除会破坏 TUI 的控制字符。"""
    if width <= 0:
        return []

    # ZWNJ/ZWJ 虽属 Cf，但是文字连写和 emoji grapheme 的有效组成
    # 字符，不能像其他控制字符一样替换为空格。
    cleaned = "".join(
        ch if ch in "\n\t\u200c\u200d" or unicodedata.category(ch)[0] != "C" else " "
        for ch in text
    ).replace("\t", "    ")
    lines: list[str] = []
    for paragraph in cleaned.splitlines() or [""]:
        if not paragraph:
            lines.append("")
            continue
        lines.extend(_rich_chop_cells(paragraph, width))
    return lines


def parse_loop_count(value: object) -> int | None:
    """Normalize an optional non-negative agent loop count, returning None when invalid."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = int(stripped)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def format_loop_count(value: object) -> str:
    """Format positive agent loop counts compactly (`1.2k` at >= 1000); omit 0/None."""
    count = parse_loop_count(value)
    if count is None or count <= 0:
        return ""
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def loop_count_tier(value: object) -> str | None:
    """Contrast tier (`light` / `medium` / `heavy`) for positive loop counts."""
    count = parse_loop_count(value)
    if count is None or count <= 0:
        return None
    if count <= 3:
        return "light"
    if count <= 30:
        return "medium"
    return "heavy"
