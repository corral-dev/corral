"""Render a `corral wrap` card grid to a 1200×630 PNG with the standard library only.

Each cell is drawn at 16×32 pixels from an embedded subset of the Spleen bitmap
font (BSD-2-Clause, license in ``ui/assets/SPLEEN-LICENSE.txt``); block elements
are drawn geometrically so half-block heatmap cells stay exact. Bold is a
one-pixel horizontal overstrike. Design: docs/design/SHAREABLE_WRAP_CARD_DESIGN.md §5.4.
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import zlib
from functools import lru_cache
from importlib import resources

from corral.wrap_card import HEIGHT, WIDTH, Grid

IMAGE_W, IMAGE_H = 1200, 630
CELL_W, CELL_H = 16, 32
PAGE = "#0C0A09"

_Pixel = bytes


@lru_cache(maxsize=1)
def _font() -> dict[str, tuple[int, ...]]:
    path = resources.files("corral.ui.assets").joinpath("spleen-16x32-subset.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, tuple[int, ...]] = {}
    for ch, hexrows in data["glyphs"].items():
        out[ch] = tuple(int(hexrows[i:i + 4], 16) for i in range(0, len(hexrows), 4))
    return out


@lru_cache(maxsize=256)
def _rgb(color: str) -> _Pixel:
    c = color.lstrip("#")
    return bytes((int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)))


def _block_mask(ch: str) -> tuple[int, ...] | None:
    """Geometric masks (16-bit rows) for block elements; None for font glyphs."""
    full = 0xFFFF
    if ch == "█":
        return (full,) * CELL_H
    if ch == "▀":
        return (full,) * (CELL_H // 2) + (0,) * (CELL_H // 2)
    if ch == "▄":
        return (0,) * (CELL_H // 2) + (full,) * (CELL_H // 2)
    if ch == "▌":
        return (0xFF00,) * CELL_H
    return None


@lru_cache(maxsize=1024)
def _mask(ch: str, bold: bool) -> tuple[int, ...]:
    rows = _block_mask(ch)
    if rows is None:
        rows = _font().get(ch) or _font().get("?") or (0,) * CELL_H
        if bold and ch.strip():
            rows = tuple((row | (row >> 1)) & 0xFFFF for row in rows)
    return rows


@lru_cache(maxsize=8192)
def _cell_rows(ch: str, fg: str, bg: str, bold: bool) -> tuple[bytes, ...]:
    """Pixel rows (RGB bytes) for one cell."""
    on, off = _rgb(fg), _rgb(bg)
    out = []
    for row in _mask(ch, bold):
        out.append(b"".join(on if row & (0x8000 >> x) else off for x in range(CELL_W)))
    return tuple(out)


def render_rgb(grid: Grid) -> bytes:
    """Raw RGB rows of the full 1200×630 image (card centered on the page color)."""
    page = _rgb(PAGE)
    card_w, card_h = WIDTH * CELL_W, HEIGHT * CELL_H
    left = (IMAGE_W - card_w) // 2
    top = (IMAGE_H - card_h) // 2
    margin_l = page * left
    margin_r = page * (IMAGE_W - card_w - left)
    blank_row = page * IMAGE_W
    rows: list[bytes] = [blank_row] * top
    for line in grid:
        cells = [_cell_rows(c.ch, c.fg, c.bg, c.bold) for c in line]
        for py in range(CELL_H):
            rows.append(margin_l + b"".join(cell[py] for cell in cells) + margin_r)
    rows.extend([blank_row] * (IMAGE_H - len(rows)))
    return b"".join(rows)


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def write_png(grid: Grid, path: str) -> str:
    """Atomically write the card image; returns the absolute path."""
    data = encode_png(IMAGE_W, IMAGE_H, render_rgb(grid))
    target = os.path.abspath(os.path.expanduser(path))
    directory = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(prefix=".corral-wrap-", suffix=".png", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        # mkstemp creates 0600; a shareable image gets the user's normal file mode.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


__all__ = ["IMAGE_H", "IMAGE_W", "encode_png", "render_rgb", "write_png"]
