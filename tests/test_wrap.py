"""`corral wrap`: aggregation policy, card frames, PNG export and the animated app."""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
import zlib
from datetime import datetime

from corral import wrap_card, wrap_png, wrap_stats
from corral.wrap_stats import (
    ACTIVE_GAP_SECONDS,
    EVENT_TAIL_SECONDS,
    Period,
    SessionWrap,
    WrapStats,
    aggregate,
    hour_buckets,
    merge_intervals,
    peak_overlap,
)

NAMES = {"opencode": "OpenCode", "pi": "Pi", "claude": "Claude", "codex": "Codex", "cursor": "Cursor"}
COLORS = {"opencode": "#34D399", "pi": "#FBBF24", "claude": "#D97757", "codex": "#60A5FA", "cursor": "#A78BFA"}
NOON = datetime(2026, 11, 2, 12, 0).timestamp()  # a Monday


def _stats(**kw) -> WrapStats:
    stats = WrapStats(period=Period("week", NOON - 6 * 86400, NOON + 3600))
    stats.sessions = 292
    stats.by_runtime = {"opencode": 217, "pi": 54, "claude": 18, "codex": 3}
    stats.tool_calls = 36813
    stats.tokens = 7_350_397_197
    stats.peak_parallel = 13
    stats.peak_at = NOON
    stats.heatmap[0][12] = 3600.0
    stats.heatmap[4][11] = 1800.0
    for key, value in kw.items():
        setattr(stats, key, value)
    return stats


def _text(grid: wrap_card.Grid) -> str:
    return "\n".join("".join(cell.ch for cell in row) for row in grid)


class IntervalPolicyTests(unittest.TestCase):
    def test_gaps_up_to_threshold_merge_and_tail_is_padded(self) -> None:
        times = [0.0, ACTIVE_GAP_SECONDS, ACTIVE_GAP_SECONDS * 3]
        self.assertEqual(
            merge_intervals(times),
            [(0.0, ACTIVE_GAP_SECONDS + EVENT_TAIL_SECONDS),
             (ACTIVE_GAP_SECONDS * 3, ACTIVE_GAP_SECONDS * 3 + EVENT_TAIL_SECONDS)],
        )

    def test_idle_session_does_not_inflate_parallel_peak(self) -> None:
        # One session active at both ends of a day must not overlap a midday session.
        long_idle = merge_intervals([0.0, 86400.0])
        midday = merge_intervals([43200.0, 43260.0])
        self.assertEqual(peak_overlap(long_idle + midday)[0], 1)

    def test_peak_counts_simultaneous_sessions_and_its_moment(self) -> None:
        peak, at = peak_overlap([(0, 100), (50, 150), (60, 70), (200, 300)])
        self.assertEqual((peak, at), (3, 60))

    def test_touching_intervals_are_not_parallel(self) -> None:
        self.assertEqual(peak_overlap([(0, 100), (100, 200)])[0], 1)

    def test_hour_buckets_split_at_local_hour_boundary(self) -> None:
        start = datetime(2026, 11, 2, 12, 50).timestamp()
        buckets = hour_buckets([(start, start + 1200)])
        self.assertAlmostEqual(buckets[(0, 12)], 600)
        self.assertAlmostEqual(buckets[(0, 13)], 600)

    def test_period_week_starts_at_local_midnight_six_days_back(self) -> None:
        period = wrap_stats.make_period("week", now=NOON)
        self.assertEqual(datetime.fromtimestamp(period.start), datetime(2026, 10, 27))
        self.assertEqual(wrap_stats.make_period("all", now=NOON).start, 0.0)


class AggregateTests(unittest.TestCase):
    def test_unreadable_and_uncovered_are_disclosed_not_zeroed(self) -> None:
        period = Period("week", 0, 10_000)
        items = [
            SessionWrap("pi", True, True, 3, 100, ((0, 100),), ((0, 12, 100.0),)),
            SessionWrap("pi", True, True, 2, None, ((50, 150),), ()),
            SessionWrap("claude", False),
            SessionWrap("codex", True, False),
        ]
        stats = aggregate(period, items)
        self.assertEqual((stats.sessions, stats.tool_calls, stats.tokens), (2, 5, 100))
        self.assertEqual((stats.tokens_uncovered, stats.unreadable), (1, 1))
        self.assertEqual(stats.by_runtime, {"pi": 2})
        self.assertEqual(stats.peak_parallel, 2)
        self.assertEqual(stats.heatmap[0][12], 100.0)

    def test_candidates_use_activity_clock(self) -> None:
        period = Period("week", 1000, 2000)
        scanned = {"pi": [{"mtime": 999}, {"mtime": 1000}], "codex": [{"mtime": 1500}]}
        self.assertEqual(len(wrap_stats.candidates(scanned, period)), 2)


class _NoFilenoStderr:
    """Textual-style stderr capture: fileno() is -1."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return -1


class WorkerPoolTests(unittest.TestCase):
    def test_pool_spawns_under_captured_stderr_after_prepare(self) -> None:
        import sys

        wrap_stats.prepare_workers()
        period = Period("week", 0, 10)
        sessions = [{"source": "no-such-runtime", "id": str(i)} for i in range(10)]
        real = sys.stderr
        sys.stderr = _NoFilenoStderr()
        try:
            stats = wrap_stats.collect(period, sessions)
        finally:
            sys.stderr = real
        self.assertEqual((stats.sessions, stats.unreadable), (0, 10))


class CardFrameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.view = wrap_card.build_view(_stats(), NAMES, COLORS)

    def test_final_frame_shows_real_values_and_repo(self) -> None:
        text = _text(wrap_card.compose(self.view, None))
        for expected in ("292", "36,813", "7.35B", "13", "OpenCode 74%", wrap_card.REPO_URL, "W R A P"):
            self.assertIn(expected, text)

    def test_grid_size_is_fixed(self) -> None:
        for t in (None, 0.0, 1.0, 3.0):
            grid = wrap_card.compose(self.view, t)
            self.assertEqual(len(grid), wrap_card.HEIGHT)
            self.assertTrue(all(len(row) == wrap_card.WIDTH for row in grid))

    def test_reveal_counts_up_to_the_final_value(self) -> None:
        early = _text(wrap_card.compose(self.view, 0.0))
        self.assertNotIn("36,813", early)
        self.assertNotIn(wrap_card.REPO_URL, early)
        done = _text(wrap_card.compose(self.view, wrap_card.REVEAL_END))
        self.assertEqual(done, _text(wrap_card.compose(self.view, None)))

    def test_frames_are_deterministic(self) -> None:
        self.assertEqual(wrap_card.compose(self.view, 1.234), wrap_card.compose(self.view, 1.234))

    def test_loading_frame_shows_progress(self) -> None:
        view = wrap_card.build_view(_stats(), NAMES, COLORS, progress=(12, 300))
        self.assertIn("reading 12 / 300 sessions", _text(wrap_card.compose(view, None, loading_t=0.3)))

    def test_scan_phase_says_scanning_not_zero_of_zero(self) -> None:
        view = wrap_card.build_view(_stats(), NAMES, COLORS, progress=(0, -1))
        text = _text(wrap_card.compose(view, None, loading_t=0.3))
        self.assertIn("scanning history", text)
        self.assertNotIn("0 / 0", text)

    def test_disclosures_reach_the_card(self) -> None:
        view = wrap_card.build_view(_stats(tokens_uncovered=7, unreadable=2), NAMES, COLORS)
        self.assertIn("7 without token data · 2 unreadable", _text(wrap_card.compose(view, None)))

    def test_legend_overflow_is_counted(self) -> None:
        many = {rid: 10 for rid in NAMES}
        view = wrap_card.build_view(_stats(by_runtime=many, sessions=50), NAMES, COLORS)
        row = _text(wrap_card.compose(view, None)).splitlines()[11]
        self.assertTrue("Cursor" in row or "+" in row)
        self.assertLessEqual(len(row.rstrip()), wrap_card.WIDTH)

    def test_compact_number_formats(self) -> None:
        self.assertEqual(wrap_card.compact(36813), "36,813")
        self.assertEqual(wrap_card.compact(7_350_397_197), "7.35B")
        self.assertEqual(wrap_card.compact(52_100_000), "52.1M")
        self.assertEqual(wrap_card.compact(452_100_000), "452M")

    def test_empty_period_renders(self) -> None:
        view = wrap_card.build_view(WrapStats(period=Period("week", 0, 1)), NAMES, COLORS)
        self.assertIn("SESSIONS", _text(wrap_card.compose(view, None)))


def _decode_png(data: bytes) -> tuple[int, int, bytes]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, size = 8, b"", (0, 0)
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        if kind == b"IHDR":
            size = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    return size[0], size[1], zlib.decompress(idat)


class PngExportTests(unittest.TestCase):
    def test_png_is_valid_1200x630_and_matches_grid_colors(self) -> None:
        grid = wrap_card.compose(wrap_card.build_view(_stats(), NAMES, COLORS), None)
        width, height, raw = _decode_png(wrap_png.encode_png(1200, 630, wrap_png.render_rgb(grid)))
        self.assertEqual((width, height), (1200, 630))
        self.assertEqual(len(raw), height * (1 + width * 3))
        stride = 1 + width * 3
        left = (1200 - wrap_card.WIDTH * 16) // 2
        top = (630 - wrap_card.HEIGHT * 32) // 2
        # Agent bar first cell (row 10, BAR_X) is solid OpenCode green.
        x = left + wrap_card.BAR_X * 16 + 8
        y = top + 10 * 32 + 16
        self.assertEqual(raw[y * stride + 1 + x * 3: y * stride + 4 + x * 3], bytes.fromhex("34D399"))
        # Page margin keeps the page color.
        self.assertEqual(raw[1:4], bytes.fromhex(wrap_png.PAGE.lstrip("#")))

    def test_write_png_is_atomic_and_returns_absolute_path(self) -> None:
        grid = wrap_card.compose(wrap_card.build_view(_stats(), NAMES, COLORS), None)
        with tempfile.TemporaryDirectory() as tmp:
            path = wrap_png.write_png(grid, os.path.join(tmp, "card.png"))
            self.assertTrue(os.path.isabs(path))
            self.assertEqual(os.listdir(tmp), ["card.png"])
            umask = os.umask(0)
            os.umask(umask)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o666 & ~umask)

    def test_every_card_glyph_is_in_the_font_subset(self) -> None:
        font = wrap_png._font()
        view = wrap_card.build_view(_stats(tokens_uncovered=1), NAMES, COLORS)
        chars = {cell.ch for t in (None, 0.1, 0.5, 2.0, 3.0, 3.2) for row in wrap_card.compose(view, t) for cell in row}
        chars |= {cell.ch for row in wrap_card.compose(view, None, loading_t=0.5) for cell in row}
        missing = {c for c in chars if wrap_png._block_mask(c) is None and c not in font}
        self.assertEqual(missing, set())


class WrapAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_loads_reveals_and_saves(self) -> None:
        from corral.ui.wrap_app import WrapApp, WrapCard

        stats = _stats()

        def loader(progress):
            progress(WrapStats(period=stats.period), 0, 2)
            return stats

        def factory(s, progress=None):
            return wrap_card.build_view(s or WrapStats(period=stats.period), NAMES, COLORS, progress=progress)

        app = WrapApp(loader, factory, animate=True)
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                async with app.run_test(size=(90, 26)) as pilot:
                    await pilot.pause(delay=wrap_card.REVEAL_END + 0.5)
                    card = app.query_one(WrapCard)
                    self.assertIn("36,813", _text(card.grid))
                    await pilot.press("s")
                    await app.workers.wait_for_complete()
                    await pilot.pause(delay=0.1)
                    self.assertTrue(any(name.startswith("corral-wrap-") for name in os.listdir(tmp)))
                    await pilot.press("q")
            finally:
                os.chdir(cwd)
        self.assertIsNone(app.error)
        self.assertIsNotNone(app.final_grid())

    async def test_small_terminal_shows_resize_hint(self) -> None:
        from corral.ui.wrap_app import WrapApp

        stats = _stats()
        app = WrapApp(lambda progress: stats,
                      lambda s, progress=None: wrap_card.build_view(s or stats, NAMES, COLORS, progress=progress),
                      animate=False)
        async with app.run_test(size=(60, 18)) as pilot:
            await pilot.pause(delay=0.2)
            self.assertTrue(app.screen.has_class("-small"))
            await pilot.press("q")


if __name__ == "__main__":
    unittest.main()
