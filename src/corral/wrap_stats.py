"""Aggregate SessKit's normalized session data into the `corral wrap` card values.

Corral owns the period, the active-interval policy and the aggregation; SessKit
owns native history interpretation. Nothing here parses native files: every
value comes from ``sesskit.load_activity`` / ``load_usage`` output. Design and
metric definitions: docs/design/SHAREABLE_WRAP_CARD_DESIGN.md §4.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Consecutive events at most this far apart belong to one active interval.
ACTIVE_GAP_SECONDS = 300.0
# A lone event keeps its session active for this long (so single events overlap).
EVENT_TAIL_SECONDS = 60.0

PERIOD_WEEK = "week"
PERIOD_MONTH = "month"
PERIOD_ALL = "all"
PERIOD_DAYS = {PERIOD_WEEK: 7, PERIOD_MONTH: 30}

# Session states reported by SessKit's activity snapshot.
_READABLE = ("available", "empty")


@dataclass(frozen=True)
class Period:
    key: str
    start: float
    end: float

    @property
    def days(self) -> int:
        return max(1, round((self.end - self.start) / 86400))


@dataclass(frozen=True)
class SessionWrap:
    """Per-session contribution; small and picklable for worker processes."""

    runtime: str
    readable: bool
    in_period: bool = False
    tool_calls: int = 0
    tokens: int | None = None
    intervals: tuple[tuple[float, float], ...] = ()
    # (weekday 0=Mon, hour) -> active seconds
    hours: tuple[tuple[int, int, float], ...] = ()


@dataclass
class WrapStats:
    period: Period
    sessions: int = 0
    by_runtime: dict[str, int] = field(default_factory=dict)
    tool_calls: int = 0
    tokens: int = 0
    tokens_uncovered: int = 0
    unreadable: int = 0
    peak_parallel: int = 0
    peak_at: float | None = None
    heatmap: list[list[float]] = field(default_factory=lambda: [[0.0] * 24 for _ in range(7)])
    scan_failures: dict[str, str] = field(default_factory=dict)

    @property
    def agents(self) -> int:
        return sum(1 for count in self.by_runtime.values() if count)

    def runtime_shares(self) -> list[tuple[str, int, float]]:
        """(runtime, sessions, fraction) sorted by sessions descending."""
        total = self.sessions or 1
        rows = [(rid, n, n / total) for rid, n in self.by_runtime.items() if n]
        return sorted(rows, key=lambda row: (-row[1], row[0]))

    def peak_cell(self) -> tuple[int, int] | None:
        best = max(((v, d, h) for d, row in enumerate(self.heatmap) for h, v in enumerate(row)), default=None)
        if not best or best[0] <= 0:
            return None
        return best[1], best[2]

    def to_dict(self) -> dict:
        return {
            "period": {"key": self.period.key, "start": self.period.start, "end": self.period.end},
            "sessions": self.sessions,
            "by_runtime": dict(sorted(self.by_runtime.items())),
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "tokens_uncovered_sessions": self.tokens_uncovered,
            "unreadable_sessions": self.unreadable,
            "peak_parallel": self.peak_parallel,
            "peak_at": self.peak_at,
            "heatmap_active_seconds": [[round(v, 1) for v in row] for row in self.heatmap],
            "scan_failures": dict(self.scan_failures),
        }


def make_period(key: str, *, now: float | None = None) -> Period:
    """Last N local calendar days ending now; ``all`` starts at the epoch."""
    now = time.time() if now is None else now
    if key == PERIOD_ALL:
        return Period(key, 0.0, now)
    days = PERIOD_DAYS[key]
    today = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=days - 1)
    return Period(key, start.timestamp(), now)


def merge_intervals(times: Iterable[float]) -> list[tuple[float, float]]:
    """Active intervals from event times: merge gaps ≤ ACTIVE_GAP_SECONDS, pad the tail."""
    ordered = sorted(times)
    out: list[tuple[float, float]] = []
    for ts in ordered:
        if out and ts - out[-1][1] <= ACTIVE_GAP_SECONDS:
            out[-1] = (out[-1][0], ts)
        else:
            out.append((ts, ts))
    return [(a, b + EVENT_TAIL_SECONDS) for a, b in out]


def hour_buckets(intervals: Iterable[tuple[float, float]]) -> dict[tuple[int, int], float]:
    """Split intervals at local hour boundaries into (weekday, hour) -> seconds."""
    buckets: dict[tuple[int, int], float] = {}
    for start, end in intervals:
        cur = start
        while cur < end:
            moment = datetime.fromtimestamp(cur)
            next_hour = moment.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            stop = min(end, next_hour.timestamp())
            key = (moment.weekday(), moment.hour)
            buckets[key] = buckets.get(key, 0.0) + (stop - cur)
            cur = stop
    return buckets


def peak_overlap(intervals: Iterable[tuple[float, float]]) -> tuple[int, float | None]:
    """Maximum simultaneous intervals and when it first happened (ends before starts on ties)."""
    points: list[tuple[float, int]] = []
    for start, end in intervals:
        points.append((start, 1))
        points.append((end, -1))
    points.sort(key=lambda point: (point[0], point[1]))
    current = peak = 0
    peak_at: float | None = None
    for moment, delta in points:
        current += delta
        if current > peak:
            peak, peak_at = current, moment
    return peak, peak_at


def summarize_session(session: dict, start: float, end: float) -> SessionWrap:
    """One session's in-period contribution from SessKit's normalized output."""
    from sesskit import UsageSelection, load_activity, load_usage, summarize_usage

    runtime = str(session.get("source") or "")
    try:
        activity = load_activity(session)
    except Exception:  # noqa: BLE001 — an unreadable session is disclosed, not fatal
        return SessionWrap(runtime, readable=False)
    if activity.state not in _READABLE:
        return SessionWrap(runtime, readable=False)
    times: list[float] = []
    message_ids: set[str] = set()
    tool_calls = 0
    whole_history_inside = True
    for event in activity.events:
        ts = event.ts
        if ts is None:
            continue
        if not (start <= ts <= end):
            whole_history_inside = False
            continue
        times.append(ts)
        if event.message_id:
            message_ids.add(event.message_id)
        if event.type == "tool_call":
            tool_calls += 1
    if not times:
        return SessionWrap(runtime, readable=True)
    intervals = merge_intervals(times)
    hours = tuple((d, h, s) for (d, h), s in sorted(hour_buckets(intervals).items()))
    return SessionWrap(
        runtime,
        readable=True,
        in_period=True,
        tool_calls=tool_calls,
        tokens=_session_tokens(load_usage, summarize_usage, UsageSelection, session,
                               message_ids, whole_history_inside),
        intervals=tuple(intervals),
        hours=hours,
    )


def _session_tokens(load_usage, summarize_usage, selection_type, session: dict,
                    message_ids: set[str], whole: bool) -> int | None:
    try:
        snapshot = load_usage(session)
    except Exception:  # noqa: BLE001
        return None
    if snapshot.state == "empty":
        return 0
    if snapshot.state != "available" or snapshot.summary is None:
        return None
    if whole:
        summary = snapshot.summary
    elif message_ids:
        summary = summarize_usage(snapshot, selection=selection_type(message_ids=tuple(sorted(message_ids))))
    else:
        return None
    return summary.total_tokens.known_total


def aggregate(period: Period, contributions: Iterable[SessionWrap],
              scan_failures: dict[str, str] | None = None) -> WrapStats:
    stats = WrapStats(period=period, scan_failures=dict(scan_failures or {}))
    all_intervals: list[tuple[float, float]] = []
    for item in contributions:
        if not item.readable:
            stats.unreadable += 1
            continue
        if not item.in_period:
            continue
        stats.sessions += 1
        stats.by_runtime[item.runtime] = stats.by_runtime.get(item.runtime, 0) + 1
        stats.tool_calls += item.tool_calls
        if item.tokens is None:
            stats.tokens_uncovered += 1
        else:
            stats.tokens += item.tokens
        all_intervals.extend(item.intervals)
        for day, hour, seconds in item.hours:
            stats.heatmap[day][hour] += seconds
    stats.peak_parallel, stats.peak_at = peak_overlap(all_intervals)
    return stats


def candidates(scanned: dict[str, list[dict]], period: Period) -> list[dict]:
    """Sessions whose latest activity is inside the period (mtime is the activity clock)."""
    return [s for items in scanned.values() for s in items if float(s.get("mtime") or 0) >= period.start]


def prepare_workers() -> None:
    """Start multiprocessing's resource tracker while the real stderr is installed.

    Textual replaces ``sys.stderr`` with a capture whose ``fileno()`` is -1; a
    tracker first launched from inside the app passes that descriptor to spawn
    and fails with "bad value(s) in fds_to_keep". Call before the TUI starts.
    """
    from multiprocessing import resource_tracker

    resource_tracker.ensure_running()


def _worker_count(n: int) -> int:
    return max(1, min(n, (os.cpu_count() or 2) - 1, 6))


def collect(period: Period, sessions: list[dict],
            progress: Callable[[int, int, SessionWrap], None] | None = None,
            *, scan_failures: dict[str, str] | None = None,
            parallel: bool = True) -> WrapStats:
    """Summarize sessions (in worker processes when worthwhile) and aggregate."""
    total = len(sessions)
    results: list[SessionWrap] = []
    if parallel and total > 8:
        with ProcessPoolExecutor(max_workers=_worker_count(total)) as pool:
            futures = [pool.submit(summarize_session, s, period.start, period.end) for s in sessions]
            for future in as_completed(futures):
                item = future.result()
                results.append(item)
                if progress:
                    progress(len(results), total, item)
    else:
        for session in sessions:
            item = summarize_session(session, period.start, period.end)
            results.append(item)
            if progress:
                progress(len(results), total, item)
    return aggregate(period, results, scan_failures)
