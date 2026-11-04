"""Render README assets for `corral wrap` from fixed sample data.

Uses the product's own frame function and PNG cell renderer, so the GIF shows
the real storyboard and the still equals `corral wrap --png` output for the
same values. Sample values are fictional; no local history is read.

Run from the CLI repository root (Pillow is needed only here, for GIF encoding):
    PYTHONPATH=src .venv/bin/python docs/screenshots/wrap_capture.py
"""

from __future__ import annotations

import io
import math
import os
import random
import sys
from datetime import datetime

from PIL import Image

from corral import wrap_card, wrap_png
from corral.theme import RUNTIME_LABEL_STYLES
from corral.wrap_stats import Period, WrapStats

OUT = os.path.dirname(os.path.abspath(__file__))
FPS = 20
NAMES = {"claude": "Claude", "codex": "Codex", "opencode": "OpenCode", "cursor": "Cursor", "pi": "Pi"}


def sample_stats() -> WrapStats:
    end = datetime(2026, 11, 4, 18, 0).timestamp()
    stats = WrapStats(period=Period("week", end - 6 * 86400 - 18 * 3600, end))
    stats.by_runtime = {"claude": 61, "codex": 44, "opencode": 23, "cursor": 9, "pi": 7}
    stats.sessions = sum(stats.by_runtime.values())
    stats.tool_calls = 18_406
    stats.tokens = 1_284_000_000
    stats.peak_parallel = 6
    stats.peak_at = datetime(2026, 11, 3, 15, 20).timestamp()
    rng = random.Random(7)
    for day in range(7):
        weekend = day >= 5
        for hour in range(24):
            work = math.exp(-((hour - 14.5) ** 2) / 18) + 0.4 * math.exp(-((hour - 21) ** 2) / 4)
            scale = 0.35 if weekend else 1.0
            stats.heatmap[day][hour] = max(0.0, work * scale * 3600 * (0.6 + 0.8 * rng.random()) - 400)
    stats.heatmap[1][15] = 4800.0
    return stats


def frame_image(grid: wrap_card.Grid) -> Image.Image:
    return Image.frombytes("RGB", (wrap_png.IMAGE_W, wrap_png.IMAGE_H), wrap_png.render_rgb(grid))


def main() -> int:
    view = wrap_card.build_view(sample_stats(), NAMES, dict(RUNTIME_LABEL_STYLES))
    final = wrap_card.compose(view, None)
    wrap_png.write_png(final, os.path.join(OUT, "wrap.png"))
    frames: list[Image.Image] = []
    steps = int(wrap_card.REVEAL_END * FPS) + 1
    for i in range(steps):
        frames.append(frame_image(wrap_card.compose(view, i / FPS)))
    # Idle shimmer once, then hold the final frame.
    shimmer_steps = int(wrap_card.SHIMMER_DUR * FPS) + 1
    for i in range(shimmer_steps):
        frames.append(frame_image(wrap_card.compose(view, None, idle_t=i / FPS)))
    frames.append(frame_image(final))
    durations = [int(1000 / FPS)] * (len(frames) - 1) + [2500]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=durations,
                   loop=0, optimize=True, disposal=1)
    with open(os.path.join(OUT, "wrap.gif"), "wb") as handle:
        handle.write(buffer.getvalue())
    print(f"wrap.gif {len(frames)} frames {len(buffer.getvalue()) // 1024} KiB; wrap.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
