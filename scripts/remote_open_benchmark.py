#!/usr/bin/env python3
"""Repeatable host-side benchmark for opening a session in the mobile remote path.

Measures the same local library calls ``corral remote`` makes for one phone open:
session store load/refresh, list snapshot, first history page, Your prompts and two
concurrent opens. Output is timings, counts and sizes only: never a session key,
title, cwd, prompt text or user-history path.

How to compare (also printed by ``--help``):

1. Run it before a host change and again after, with the same ``--samples`` and
   ``--limit``::

       python scripts/remote_open_benchmark.py --json /tmp/remote-open-before.json
       python scripts/remote_open_benchmark.py --json /tmp/remote-open-after.json

2. Compare ``concurrency.serial_sum_ms`` with
   ``concurrency.concurrent_open_wall_ms``. A ratio above 1.0 means two different
   sessions actually overlap; the per-session-lock change should raise it.
3. Compare ``sessions[].page_cold_ms`` / ``prompts_cold_ms`` (parse path) and
   ``sessions[].page_warm_ms`` / ``prompts_after_page_ms`` (in-memory cache-hit
   path); ``page_reopen_ms`` / ``prompts_reopen_ms`` cover a warm on-disk cache.
4. Compare ``store.load``, ``store.refresh`` and ``store.refresh_state`` for the
   host baseline. Keep the cache mode identical between runs.

Cold means "no in-memory transcript and an empty on-disk transcript cache", not
"the operating system dropped the file from its page cache". It deliberately
excludes ``SessionHub`` construction and the session scan; those are reported
separately as ``load`` and ``cold_load``. Every number is therefore comparable
across runs even when the store scan itself is noisy.

The benchmark never starts the scan worker and never writes to the user's cache.
It seeds a private cache directory with a read-only snapshot of the live shared
scan index (when available) so ``store.load``/``refresh`` measure the same
shared-index path the daemon uses instead of an unbounded local rescan under
host load. Use ``--no-shared-index`` for a true isolated local scan.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"

ACTIVE_RUNTIMES = ("claude", "codex", "opencode", "cursor", "pi")
DORMANT_RUNTIMES = ("kimi",)
DEFAULT_SCAN_LIMIT = 200
DEFAULT_PAGE_LIMIT = 1500

_SIZE_CLASSES = (
    ("<1MB", 1.0),
    ("1-10MB", 10.0),
    ("10-50MB", 50.0),
    ("50-100MB", 100.0),
    ("100MB+", None),
)

_DEFINITIONS = {
    "page_cold_ms": (
        "message_page on a key with no in-memory transcript and a brand-new empty "
        "on-disk transcript cache: native history parse plus page assembly."
    ),
    "page_warm_ms": (
        "Immediately repeated message_page on the same hub and transcript: signature "
        "stat plus page assembly, no history parse."
    ),
    "page_reopen_ms": (
        "message_page after dropping the in-memory transcript but keeping the on-disk "
        "transcript cache: persistent-cache restore (new process / daemon restart)."
    ),
    "prompts_after_page_ms": (
        "First user_prompts call after page_cold: the transcript is already parsed, "
        "but the call may still fill earlier history and then filters every message."
    ),
    "prompts_warm_ms": (
        "Second user_prompts call on the same loaded transcript: filter walk only, "
        "no parse and no earlier-history fill."
    ),
    "prompts_reopen_ms": (
        "user_prompts in the page_reopen state: persistent transcript restore plus the "
        "cached Your-prompts projection; no fill when the projection covers the history."
    ),
    "prompts_cold_ms": (
        "user_prompts with a fresh transcript and empty cache: cold parse, fill back "
        "to the session start, then filter. This is the worst first open of Your prompts."
    ),
    "detail_ms": "session_detail on the loaded store: metadata projection, no history parse.",
    "concurrent_open_wall_ms": (
        "Wall clock for ThreadPoolExecutor(2) opening two different sessions on one hub, "
        "both transcript-cold."
    ),
    "serial_sum_ms": (
        "Sum of two sequential transcript-cold opens of the same pair, measured in the "
        "same round. ratio = serial_sum_ms / concurrent_open_wall_ms."
    ),
    "load_ms": (
        "First SessionStore.load in this process. With the default shared-index fixture "
        "this consumes the live daemon's published scan index; without it, a local scan."
    ),
    "cold_load_ms": (
        "SessionStore.load in a fresh subprocess with an empty cache and no shared index "
        "(true local scan), bounded by --cold-load-timeout."
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def ensure_source_on_path() -> None:
    if SRC_DIR.is_dir() and str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))


def percentile(values: list[float], percent: float) -> float | None:
    """Linear-interpolated percentile; deterministic for small samples."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    bounded = max(0.0, min(100.0, float(percent)))
    position = (len(ordered) - 1) * bounded / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def summarize(values: list[float]) -> dict:
    clean = [round(float(value), 3) for value in values if value is not None]
    if not clean:
        return {"n": 0, "samples_ms": []}
    return {
        "n": len(clean),
        "samples_ms": clean,
        "min_ms": round(min(clean), 3),
        "p50_ms": round(percentile(clean, 50) or 0.0, 3),
        "p95_ms": round(percentile(clean, 95) or 0.0, 3),
        "max_ms": round(max(clean), 3),
    }


def session_size_mb(session: dict) -> float:
    try:
        kilobytes = float(session.get("size_kb") or 0.0)
    except (TypeError, ValueError):
        kilobytes = 0.0
    if kilobytes <= 0:
        path = str(session.get("path") or "")
        if path:
            try:
                kilobytes = os.stat(path).st_size / 1024.0
            except OSError:
                kilobytes = 0.0
    return round(kilobytes / 1024.0, 3)


def size_class(size_mb: float) -> str:
    for label, ceiling in _SIZE_CLASSES:
        if ceiling is None or size_mb < ceiling:
            return label
    return _SIZE_CLASSES[-1][0]


@dataclass(frozen=True)
class Sample:
    runtime: str
    rank: int
    size_mb: float
    size_class: str
    key: str
    path: str

    @property
    def id(self) -> str:
        return f"{self.runtime}#{self.rank}"

    def public(self) -> dict:
        """Everything safe to print; ``key``/``path`` stay internal."""
        return {
            "id": self.id,
            "runtime": self.runtime,
            "rank": self.rank,
            "size_mb": round(self.size_mb, 3),
            "size_class": self.size_class,
        }


def select_samples(sessions: list[dict], per_runtime: int, key_fn) -> list[Sample]:
    """Largest ``per_runtime`` sessions per active runtime, deterministic order."""
    if per_runtime <= 0:
        return []
    groups: dict[str, list[dict]] = {runtime: [] for runtime in ACTIVE_RUNTIMES}
    for session in sessions:
        runtime = str(session.get("source") or "")
        if runtime in groups:
            groups[runtime].append(session)
    selected: list[Sample] = []
    for runtime in ACTIVE_RUNTIMES:
        rows = sorted(
            groups[runtime],
            key=lambda item: (
                -session_size_mb(item),
                str(item.get("id") or ""),
                str(item.get("path") or ""),
            ),
        )
        for rank, session in enumerate(rows[:per_runtime], start=1):
            size = session_size_mb(session)
            selected.append(
                Sample(
                    runtime=runtime,
                    rank=rank,
                    size_mb=size,
                    size_class=size_class(size),
                    key=str(key_fn(session)),
                    path=str(session.get("path") or ""),
                )
            )
    return selected


def sample_pair(samples: list[Sample | dict]) -> list:
    """Two largest samples from different runtimes when possible."""

    def runtime_of(item) -> str:
        return item.runtime if isinstance(item, Sample) else str(item.get("runtime") or "")

    def size_of(item) -> float:
        return item.size_mb if isinstance(item, Sample) else float(item.get("size_mb") or 0.0)

    ordered = sorted(samples, key=lambda item: (-size_of(item), runtime_of(item)))
    if len(ordered) < 2:
        return ordered
    first = ordered[0]
    for candidate in ordered[1:]:
        if runtime_of(candidate) != runtime_of(first):
            return [first, candidate]
    return [first, ordered[1]]


_PATH_RE = re.compile(r"(?:~|/)[^\s'\"()\[\],;]+")
_KEY_RE = re.compile(r"\b(?:claude|codex|opencode|cursor|pi|kimi|shell):[A-Za-z0-9._:-]+")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def redact(text: str) -> str:
    """Remove identifiers and paths from diagnostics; never applied to output fields."""
    cleaned = _PATH_RE.sub("<path>", str(text))
    cleaned = _KEY_RE.sub("<key>", cleaned)
    cleaned = _UUID_RE.sub("<id>", cleaned)
    return cleaned


def redacted_error(exc: BaseException) -> str:
    message = redact(str(exc))[:200]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{float(value):.1f}"


def render_markdown(result: dict) -> str:
    """Paste-ready tables; only public fields from ``result`` are used."""
    config = result.get("config") or {}
    environment = result.get("environment") or {}
    load_average = result.get("load_average") or {}
    lines: list[str] = []
    lines.append("## Corral remote session-open benchmark")
    lines.append("")
    platform_info = result.get("platform") or {}
    lines.append(
        "- machine: {machine} · {system} {arch} · Python {python} · corral {version}".format(
            machine=result.get("machine") or "local",
            system=platform_info.get("system") or "?",
            arch=platform_info.get("machine") or "?",
            python=platform_info.get("python") or "?",
            version=result.get("corral_version") or "?",
        )
    )
    lines.append(
        "- started: {started} · duration: {duration:.1f} s · samples/runtime: {samples} · "
        "scan limit: {limit} · page limit: {page}".format(
            started=result.get("started_at") or "?",
            duration=float(result.get("duration_ms") or 0.0) / 1000.0,
            samples=config.get("samples_per_runtime"),
            limit=config.get("scan_limit"),
            page=config.get("page_limit"),
        )
    )
    average = load_average.get("start") or []
    lines.append(
        "- load average: {} · cache: {} · shared-index fixture: {}".format(
            " / ".join(f"{value:.2f}" for value in average) if average else "?",
            environment.get("cache_mode") or "?",
            "yes" if environment.get("shared_index_seeded") else "no",
        )
    )
    if result.get("timed_out"):
        lines.append("- **timed out**: partial results only; raise the budget only for a deliberate run.")
    lines.append("")

    load = result.get("load") or {}
    refresh = result.get("refresh") or {}
    refresh_state = result.get("refresh_state") or {}
    snapshot = result.get("list_snapshot") or {}
    cold_load = result.get("cold_load") or {}
    counts = load.get("counts") or {}
    lines.append("### Host baseline")
    lines.append("")
    lines.append("| metric | p50 ms | max ms | notes |")
    lines.append("|---|---|---|---|")
    lines.append(
        "| store.load | {value} | {value} | {mode}; shared={shared}; cache_hit={hit} |".format(
            value=_fmt_ms(load.get("ms")),
            mode=load.get("mode") or "?",
            shared=str(bool(load.get("scan_shared"))).lower(),
            hit=str(bool(load.get("scan_cache_hit_all"))).lower(),
        )
    )
    lines.append(
        "| store.refresh | {p50} | {max} | {n} samples; scan shared={shared} |".format(
            p50=_fmt_ms(refresh.get("p50_ms")),
            max=_fmt_ms(refresh.get("max_ms")),
            n=refresh.get("n", 0),
            shared=str(any(bool(item.get("scan_shared")) for item in refresh.get("samples") or [])).lower(),
        )
    )
    lines.append(
        "| store.refresh_state | {p50} | {max} | {n} samples |".format(
            p50=_fmt_ms(refresh_state.get("p50_ms")),
            max=_fmt_ms(refresh_state.get("max_ms")),
            n=refresh_state.get("n", 0),
        )
    )
    lines.append(
        "| list_snapshot | {p50} | {max} | {rows} rows · {size:.1f} KB JSON · total {total} |".format(
            p50=_fmt_ms(snapshot.get("p50_ms")),
            max=_fmt_ms(snapshot.get("max_ms")),
            rows=snapshot.get("rows", 0),
            size=float(snapshot.get("json_bytes") or 0) / 1024.0,
            total=snapshot.get("total", 0),
        )
    )
    if cold_load.get("skipped"):
        cold_note = f"skipped ({cold_load.get('reason') or 'no budget'})"
        cold_value = "—"
    elif cold_load.get("timed_out"):
        cold_note = f"timed out after {float(cold_load.get('budget_s') or 0):.0f} s"
        cold_value = "—"
    else:
        cold_note = "isolated empty cache, no shared index"
        cold_value = _fmt_ms(cold_load.get("ms"))
    lines.append(f"| cold store.load | {cold_value} | {cold_value} | {cold_note} |")
    lines.append("")
    count_text = " · ".join(
        f"{runtime} {counts.get(runtime, 0)}"
        for runtime in (*ACTIVE_RUNTIMES, *DORMANT_RUNTIMES)
        if runtime in counts
    )
    if count_text:
        lines.append(f"Sessions: {load.get('session_count', 0)} scanned ({count_text}).")
        lines.append("")

    sessions = result.get("sessions") or []
    lines.append("### First page and Your prompts")
    lines.append("")
    lines.append(
        "| session | size | class | page cold | page warm | prompts cold | prompts warm | "
        "prompts after page | detail | msgs |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for item in sessions:
        lines.append(
            "| {id} | {size:.1f} MB | {klass} | {page_cold} | {page_warm} | {prompts_cold} | "
            "{prompts_warm} | {after_page} | {detail} | {messages} |".format(
                id=item.get("id"),
                size=float(item.get("size_mb") or 0.0),
                klass=item.get("size_class") or "?",
                page_cold=_fmt_ms(item.get("page_cold_ms")),
                page_warm=_fmt_ms(item.get("page_warm_ms")),
                prompts_cold=_fmt_ms(item.get("prompts_cold_ms")),
                prompts_warm=_fmt_ms(item.get("prompts_warm_ms")),
                after_page=_fmt_ms(item.get("prompts_after_page_ms")),
                detail=_fmt_ms(item.get("detail_ms")),
                messages=item.get("messages", 0),
            )
        )
    if not sessions:
        lines.append("| — | — | — | — | — | — | — | — | — | — |")
    lines.append("")

    lines.append("### Reopen with a warm transcript cache")
    lines.append("")
    lines.append("| session | page reopen | prompts reopen | prompts |")
    lines.append("|---|---|---|---|")
    for item in sessions:
        lines.append(
            "| {id} | {page} | {prompts} | {total} |".format(
                id=item.get("id"),
                page=_fmt_ms(item.get("page_reopen_ms")),
                prompts=_fmt_ms(item.get("prompts_reopen_ms")),
                total=item.get("prompts_reopen_total", "—"),
            )
        )
    if not sessions:
        lines.append("| — | — | — | — |")
    lines.append("")

    concurrency = result.get("concurrency") or {}
    lines.append("### Concurrent vs serial open")
    lines.append("")
    lines.append("| pair | serial sum ms | concurrent wall ms | ratio | rounds |")
    lines.append("|---|---|---|---|---|")
    pair = concurrency.get("pair") or []
    lines.append(
        "| {pair} | {serial} | {concurrent} | {ratio} | {rounds} |".format(
            pair=" + ".join(pair) if pair else "—",
            serial=_fmt_ms(concurrency.get("serial_sum_ms")),
            concurrent=_fmt_ms(concurrency.get("concurrent_open_wall_ms")),
            ratio=(
                f"{float(concurrency['ratio']):.2f}"
                if concurrency.get("ratio") is not None
                else "—"
            ),
            rounds=concurrency.get("n", 0),
        )
    )
    round_ratios = [
        f"{float(row['ratio']):.2f}"
        for row in concurrency.get("rounds") or []
        if row.get("ratio") is not None
    ]
    if round_ratios:
        lines.append("")
        lines.append(
            "Per-round ratios: " + " · ".join(round_ratios)
            + " (order alternates: serial-first, concurrent-first, …)."
        )
    lines.append("")
    lines.append(
        "Cold = no in-memory transcript + empty transcript cache (not disk-cold). "
        "page_warm is the same hub's second call; prompts_after_page is the first "
        "Your prompts call after that page; prompts_warm is the second call. "
        "page_reopen / prompts_reopen drop only the in-memory transcript and keep "
        "the on-disk cache (daemon restart / new process)."
    )
    if result.get("errors"):
        lines.append("")
        lines.append("Errors: " + "; ".join(result["errors"][:5]))
    return "\n".join(lines)


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self.started = time.monotonic()
        self.seconds = max(0.0, float(seconds))

    def remaining(self) -> float:
        return max(0.0, self.seconds - (time.monotonic() - self.started))

    def exceeded(self) -> bool:
        return self.remaining() <= 0.0


class _SharedIndexFixture:
    """Private copy of the live scan index + a synthetic worker heartbeat.

    The product reads this exactly like a daemon-warm cache: ``store.load`` and
    ``refresh`` consume the shared index and skip a local rescan. The source
    cache is only read; every write lands under the benchmark's temp root.
    """

    def __init__(
        self,
        payload: dict,
        target: Path,
        heartbeat: Path,
        cohort: str,
        index_age_s: float | None,
        reason: str | None = None,
    ) -> None:
        self.payload = payload
        self.target = target
        self.heartbeat = heartbeat
        self.cohort = cohort
        self.index_age_s = index_age_s
        self.reason = reason
        self.seeded = bool(payload)

    def install(self) -> None:
        if not self.seeded:
            return
        payload = dict(self.payload)
        payload["published_at"] = time.time()
        _atomic_write_text(self.target, json.dumps(payload, ensure_ascii=False))
        _atomic_write_text(
            self.heartbeat,
            json.dumps({"pid": os.getpid(), "updated_at": time.time(), "cohort": self.cohort}),
        )


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{time.monotonic_ns()}")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _load_shared_fixture(source_dir: Path, target: Path, scan_index, limit: int) -> _SharedIndexFixture:
    cohort = str(scan_index.provider_cohort())
    heartbeat = target.parent / "scan-worker.json"
    reason = "missing"
    candidates = sorted(
        source_dir.glob("scan-index-*.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
        reverse=True,
    )
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            reason = "unreadable"
            continue
        if not isinstance(payload, dict) or payload.get("version") != scan_index.INDEX_VERSION:
            reason = "version"
            continue
        if payload.get("provider_cohort") != cohort:
            reason = "cohort"
            continue
        try:
            published_limit = int(payload.get("limit") or 0)
            published_at = float(payload.get("published_at") or 0.0)
        except (TypeError, ValueError):
            reason = "version"
            continue
        if published_limit < limit:
            reason = "limit"
            continue
        if not isinstance(payload.get("sessions"), dict):
            reason = "sessions"
            continue
        age = max(0.0, time.time() - published_at) if published_at else None
        return _SharedIndexFixture(payload, target, heartbeat, cohort, age)
    return _SharedIndexFixture({}, target, heartbeat, cohort, None, reason=reason)


def _user_cache_dir() -> Path:
    override = os.environ.get("CORRAL_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    root = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return root / "corral"


def _prepare_cache_root(value: str | None) -> tuple[Path, bool, bool]:
    if value:
        path = Path(value).expanduser().resolve()
        preloaded = path.exists() and any(path.iterdir())
        path.mkdir(parents=True, exist_ok=True)
        return path, False, preloaded
    path = Path(tempfile.mkdtemp(prefix="corral-remote-open-"))
    return path, True, False


def _unlink_sqlite(path: Path | None) -> None:
    if path is None:
        return
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass


def _drop_transcript(hub, key: str) -> None:
    transcripts = getattr(hub, "_transcripts", None)
    if not isinstance(transcripts, dict):
        return
    lock = getattr(hub, "_lock", None)
    if lock is not None:
        with lock:
            transcripts.pop(key, None)
    else:
        transcripts.pop(key, None)


def _install_fresh_transcript_cache(hub, cache_root: Path, tag: str, transcript_cache_cls) -> Path:
    old = getattr(hub, "_transcript_cache", None)
    old_path = getattr(old, "path", None)
    path = cache_root / f"transcripts-{tag}.sqlite3"
    _unlink_sqlite(path)
    hub._transcript_cache = transcript_cache_cls(path=path)
    if old is not None:
        try:
            old.close()
        except Exception:  # noqa: BLE001 — cache teardown is best-effort
            pass
        if old_path is not None:
            _unlink_sqlite(Path(old_path))
    return path


def _discard_transcript_cache(hub) -> None:
    cache = getattr(hub, "_transcript_cache", None)
    if cache is None:
        return
    path = getattr(cache, "path", None)
    try:
        cache.close()
    except Exception:  # noqa: BLE001 — cache teardown is best-effort
        pass
    if path is not None:
        _unlink_sqlite(Path(path))


def _timed_ms(callable_) -> tuple[float, object]:
    started = time.perf_counter()
    value = callable_()
    return (time.perf_counter() - started) * 1000.0, value


def _measure_message_page(hub, sample: Sample, page_limit: int, cache_root: Path, tag: str, cls) -> dict:
    _drop_transcript(hub, sample.key)
    _install_fresh_transcript_cache(hub, cache_root, f"{tag}-cold", cls)
    cold_ms, page = _timed_ms(lambda: hub.message_page(sample.key, limit=page_limit))
    warm_ms, _ = _timed_ms(lambda: hub.message_page(sample.key, limit=page_limit))
    detail_ms, _ = _timed_ms(lambda: hub.session_detail(sample.key))
    after_page_ms, prompts_after = _timed_ms(lambda: hub.user_prompts(sample.key))
    warm_prompts_ms, _ = _timed_ms(lambda: hub.user_prompts(sample.key))
    # New process / daemon restart with a warm on-disk transcript cache and the
    # prompt projection saved by the calls above.
    _drop_transcript(hub, sample.key)
    reopen_ms, _ = _timed_ms(lambda: hub.message_page(sample.key, limit=page_limit))
    _drop_transcript(hub, sample.key)
    prompts_reopen_ms, prompts_reopen = _timed_ms(lambda: hub.user_prompts(sample.key))
    _drop_transcript(hub, sample.key)
    _install_fresh_transcript_cache(hub, cache_root, f"{tag}-prompts", cls)
    prompts_cold_ms, prompts_cold = _timed_ms(lambda: hub.user_prompts(sample.key))
    prompts_cold_warm_ms, _ = _timed_ms(lambda: hub.user_prompts(sample.key))
    _drop_transcript(hub, sample.key)
    _discard_transcript_cache(hub)
    messages = page.get("messages") if isinstance(page, dict) else []
    return {
        **sample.public(),
        "page_cold_ms": round(cold_ms, 3),
        "page_warm_ms": round(warm_ms, 3),
        "page_reopen_ms": round(reopen_ms, 3),
        "detail_ms": round(detail_ms, 3),
        "messages": len(messages or []),
        "page_total": (page or {}).get("total"),
        "page_json_bytes": len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        "prompts_after_page_ms": round(after_page_ms, 3),
        "prompts_after_page_total": (prompts_after or {}).get("total"),
        "prompts_warm_ms": round(warm_prompts_ms, 3),
        "prompts_reopen_ms": round(prompts_reopen_ms, 3),
        "prompts_reopen_total": (prompts_reopen or {}).get("total"),
        "prompts_cold_ms": round(prompts_cold_ms, 3),
        "prompts_cold_total": (prompts_cold or {}).get("total"),
        "prompts_cold_warm_ms": round(prompts_cold_warm_ms, 3),
    }


def _measure_concurrency(
    hub,
    samples: list[Sample],
    page_limit: int,
    cache_root: Path,
    rounds: int,
    cls,
) -> dict:
    pair = sample_pair(samples)
    if len(pair) < 2:
        return {"pair": [item.public()["id"] for item in pair], "n": 0, "error": "need_two_sessions"}
    labels = [item.id for item in pair]

    def open_one(sample: Sample) -> None:
        hub.message_page(sample.key, limit=page_limit)

    def run_serial(round_index: int) -> tuple[float, list[float]]:
        total = 0.0
        solos: list[float] = []
        for index, sample in enumerate(pair):
            _drop_transcript(hub, sample.key)
            _install_fresh_transcript_cache(hub, cache_root, f"conc-{round_index}-s{index}", cls)
            solo_ms, _ = _timed_ms(lambda item=sample: open_one(item))
            solos.append(round(solo_ms, 3))
            total += solo_ms
        return total, solos

    def run_concurrent(round_index: int) -> float:
        for index, sample in enumerate(pair):
            _drop_transcript(hub, sample.key)
            _install_fresh_transcript_cache(hub, cache_root, f"conc-{round_index}-c{index}", cls)
        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(open_one, pair))
        return (time.perf_counter() - started) * 1000.0

    round_rows: list[dict] = []
    for round_index in range(max(1, rounds)):
        serial_total = 0.0
        serial_solo: list[float] = []
        concurrent_ms = 0.0
        try:
            # Alternate the order: whichever half runs second sees the other
            # half's files in the OS page cache. Averaging both orders keeps
            # the serial/concurrent comparison from favouring either side.
            if round_index % 2:
                concurrent_ms = run_concurrent(round_index)
                serial_total, serial_solo = run_serial(round_index)
            else:
                serial_total, serial_solo = run_serial(round_index)
                concurrent_ms = run_concurrent(round_index)
        except Exception as exc:  # noqa: BLE001 — one bad sample must not lose the run
            round_rows.append({"round": round_index + 1, "error": redacted_error(exc)})
            break
        finally:
            for sample in pair:
                _drop_transcript(hub, sample.key)
            _discard_transcript_cache(hub)
        ratio = serial_total / concurrent_ms if concurrent_ms > 0 else None
        round_rows.append(
            {
                "round": round_index + 1,
                "solo_ms": serial_solo,
                "serial_sum_ms": round(serial_total, 3),
                "concurrent_open_wall_ms": round(concurrent_ms, 3),
                "ratio": round(ratio, 3) if ratio is not None else None,
            }
        )
    valid = [row for row in round_rows if "concurrent_open_wall_ms" in row]
    serial_values = [float(row["serial_sum_ms"]) for row in valid]
    concurrent_values = [float(row["concurrent_open_wall_ms"]) for row in valid]
    serial_median = statistics.median(serial_values) if serial_values else None
    concurrent_median = statistics.median(concurrent_values) if concurrent_values else None
    return {
        "pair": labels,
        "n": len(valid),
        "rounds": round_rows,
        "serial_sum_ms": round(serial_median, 3) if serial_median is not None else None,
        "concurrent_open_wall_ms": (
            round(concurrent_median, 3) if concurrent_median is not None else None
        ),
        "ratio": (
            round(serial_median / concurrent_median, 3)
            if serial_median is not None and concurrent_median
            else None
        ),
    }


def _probe_cold_load(args, cache_root: Path, deadline: _Deadline) -> dict:
    budget = min(float(args.cold_load_timeout), deadline.remaining())
    if args.cold_load_timeout <= 0:
        return {"skipped": True, "reason": "disabled"}
    if budget < 5.0:
        return {"skipped": True, "reason": "timeout_budget"}
    cold_root = cache_root / "cold-load"
    cold_root.mkdir(parents=True, exist_ok=True)
    output = cache_root / "cold-load.json"
    env = os.environ.copy()
    env["CORRAL_CACHE_DIR"] = str(cold_root)
    env["CORRAL_ISOLATE_MANAGED_HOSTS"] = "1"
    env["CORRAL_CACHE"] = "1"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--load-probe",
        "--limit",
        str(args.limit),
        "--json",
        str(output),
    ]
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=budget,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "skipped": False,
            "timed_out": True,
            "budget_s": round(budget, 3),
            "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
    except OSError as exc:
        return {"skipped": False, "error": redacted_error(exc)}
    payload: dict = {"skipped": False}
    try:
        loaded = json.loads(output.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload.update(loaded)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        payload["error"] = "probe_output_unreadable"
    payload["exit_code"] = completed.returncode
    payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    return payload


def _run_load_probe(args) -> int:
    """Internal subprocess mode: one isolated SessionStore.load, JSON only."""
    ensure_source_on_path()
    from corral.store import SessionStore

    payload: dict = {"ok": False}
    try:
        store = SessionStore(limit=args.limit)
        load_ms, _ = _timed_ms(store.load)
        load_error = store.get_load_error()
        payload = {
            "ok": load_error is None,
            "ms": round(load_ms, 3),
            "session_count": sum(len(bucket) for bucket in store.sessions.values()),
            "visible_session_count": len(store.all_sessions()),
            "counts": {runtime: len(bucket) for runtime, bucket in store.sessions.items()},
            "scan_shared": bool(getattr(store.registry, "last_scan_shared", False)),
            "scan_cache_hit_all": bool(getattr(store.registry, "last_scan_cache_hit_all", False)),
            "error": redact(str(load_error)) if load_error is not None else None,
        }
    except Exception as exc:  # noqa: BLE001 — the parent records the failure
        payload = {"ok": False, "error": redacted_error(exc)}
    if args.json:
        _atomic_write_text(Path(args.json).expanduser(), json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure the host-side cost of opening a remote session. Prints timings and "
            "counts only; never session keys, titles, cwd, prompt text or history paths."
        ),
        epilog=(
            "how to compare\n"
            "--------------\n"
            "Run before and after a host change with identical --samples and --limit:\n"
            "  python scripts/remote_open_benchmark.py --json /tmp/before.json\n"
            "  python scripts/remote_open_benchmark.py --json /tmp/after.json\n"
            "Then compare concurrency.serial_sum_ms vs concurrency.concurrent_open_wall_ms\n"
            "(ratio > 1 means the two opens overlap), sessions[].page_cold_ms and\n"
            "prompts_cold_ms for the parse path, and page_warm_ms / prompts_after_page_ms\n"
            "for the in-memory cache-hit path; page_reopen_ms / prompts_reopen_ms cover a\n"
            "warm on-disk transcript cache. Keep the cache mode identical and record load\n"
            "average.\n"
            "\n"
            "definitions\n"
            "-----------\n"
            + "\n".join(f"  {name}: {text}" for name, text in _DEFINITIONS.items())
            + "\n"
            "\ncold means transcript-cold (no in-memory transcript and an empty on-disk\n"
            "transcript cache), not disk-cold; SessionHub construction and the session scan\n"
            "are excluded and reported separately as load / cold_load. The benchmark seeds a\n"
            "private cache with a read-only snapshot of the live shared scan index by default\n"
            "so store.load/refresh measure the daemon-warm path instead of an unbounded local\n"
            "rescan under host load; pass --no-shared-index for a true isolated local scan.\n"
            "\n"
            "examples\n"
            "--------\n"
            "  python scripts/remote_open_benchmark.py --samples 1 --limit 200 --json /tmp/x.json\n"
            "  python scripts/remote_open_benchmark.py --markdown --no-shared-index --samples 1\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", default="", help="write the full JSON report to this path")
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help="sessions per active runtime (default: 1; larger values need a larger --timeout)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_SCAN_LIMIT,
        help=f"per-runtime scan depth (default: {DEFAULT_SCAN_LIMIT})",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=DEFAULT_PAGE_LIMIT,
        help=(
            "message_page limit passed through unchanged (default: "
            f"{DEFAULT_PAGE_LIMIT}; the product clamps the wire window)"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=170.0,
        help=(
            "budget in seconds for the whole run (default: 170); remaining phases are "
            "skipped at the budget and a hard stop exits at budget + 15 s if a call hangs"
        ),
    )
    parser.add_argument(
        "--cold-load-timeout",
        type=float,
        default=45.0,
        help="budget for the isolated cold-store subprocess; 0 disables it (default: 45)",
    )
    parser.add_argument(
        "--concurrency-rounds",
        type=int,
        default=3,
        help="serial-vs-concurrent rounds on the two largest samples (default: 3)",
    )
    parser.add_argument(
        "--cache-root",
        default="",
        help="reuse this directory as the isolated cache root instead of a fresh temp dir",
    )
    parser.add_argument(
        "--no-shared-index",
        action="store_true",
        help="do not seed the live shared scan index; measure a true isolated local scan",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="print only the markdown table (JSON still goes to --json when given)",
    )
    parser.add_argument(
        "--machine-label",
        default="local",
        help="machine label in the report (default: local; platform.node() is not printed)",
    )
    parser.add_argument(
        "--load-probe",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def _positive(name: str, value: float) -> float:
    if value < 1:
        raise SystemExit(f"{name} must be >= 1")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.load_probe:
        return _run_load_probe(args)

    _positive("--samples", args.samples)
    _positive("--limit", args.limit)
    _positive("--page-limit", args.page_limit)
    _positive("--timeout", args.timeout)
    if args.cold_load_timeout < 0:
        raise SystemExit("--cold-load-timeout must be >= 0")
    _positive("--concurrency-rounds", args.concurrency_rounds)

    ensure_source_on_path()
    started_wall = time.time()
    started_at = utc_now()
    deadline = _Deadline(args.timeout)

    def _hard_stop() -> None:
        print("\nbenchmark hard timeout: no report written", file=sys.stderr)
        os._exit(3)

    hard_stop = threading.Timer(float(args.timeout) + 15.0, _hard_stop)
    hard_stop.daemon = True
    hard_stop.start()
    try:
        load_average_start = list(os.getloadavg())
    except (AttributeError, OSError):
        load_average_start = []

    source_cache = _user_cache_dir()
    cache_root, owns_cache, preloaded = _prepare_cache_root(args.cache_root or None)
    if not owns_cache and cache_root.resolve() == source_cache.resolve():
        raise SystemExit("--cache-root must differ from the user cache directory")
    os.environ["CORRAL_CACHE_DIR"] = str(cache_root)
    os.environ["CORRAL_CACHE"] = "1"
    if args.no_shared_index:
        os.environ["CORRAL_ISOLATE_MANAGED_HOSTS"] = "1"
    else:
        os.environ.pop("CORRAL_ISOLATE_MANAGED_HOSTS", None)

    errors: list[str] = []
    timed_out = False
    hub = None
    fixture = None
    result: dict = {}
    cleanup_ok = True
    try:
        import corral
        from corral import scan_index, scan_worker
        from corral.models import session_key
        from corral.remote.sessions import SessionHub
        from corral.remote.transcript_cache import TranscriptCache

        # The benchmark reports the in-process path; never fork a scan worker into
        # its private cache. The daemon's own worker is untouched.
        def _no_scan_worker(_limit: int):
            return None

        scan_worker.ensure_scan_worker = _no_scan_worker  # type: ignore[assignment]

        fixture = _load_shared_fixture(
            source_cache,
            scan_index.index_path(),
            scan_index,
            args.limit,
        )
        if args.no_shared_index:
            fixture = _SharedIndexFixture(
                {},
                fixture.target,
                fixture.heartbeat,
                str(scan_index.provider_cohort()),
                None,
                reason="disabled",
            )
        elif fixture.seeded:
            fixture.install()

        hub = SessionHub(scan_limit=args.limit)
        load_ms, _ = _timed_ms(hub.store.load)
        load_error = hub.store.get_load_error()
        if load_error is not None:
            errors.append(f"load: {redact(str(load_error))}")
        counts = {runtime: len(bucket) for runtime, bucket in hub.store.sessions.items()}
        sessions = hub.store.all_sessions()
        load = {
            "ms": round(load_ms, 3),
            "mode": (
                "shared-index-fixture"
                if fixture.seeded and not args.no_shared_index
                else ("isolated-local-scan" if args.no_shared_index else "local-scan-fallback")
            ),
            "scan_shared": bool(getattr(hub.registry, "last_scan_shared", False)),
            "scan_cache_hit_all": bool(getattr(hub.registry, "last_scan_cache_hit_all", False)),
            "scan_shared_miss": getattr(hub.registry, "last_scan_shared_miss", None),
            "session_count": sum(counts.values()),
            "visible_session_count": len(sessions),
            "counts": counts,
            "fixture_reason": fixture.reason,
            "fixture_index_age_s": fixture.index_age_s,
        }

        refresh: dict = {"samples": []}
        for index in range(3):
            if deadline.exceeded():
                timed_out = True
                break
            if fixture.seeded:
                fixture.install()
            try:
                elapsed, changed = _timed_ms(hub.store.refresh)
            except Exception as exc:  # noqa: BLE001 — report, keep earlier samples
                errors.append(f"refresh[{index}]: {redacted_error(exc)}")
                break
            refresh["samples"].append(
                {
                    "ms": round(elapsed, 3),
                    "changed": bool(changed),
                    "scan_shared": bool(getattr(hub.registry, "last_scan_shared", False)),
                    "scan_cache_hit_all": bool(
                        getattr(hub.registry, "last_scan_cache_hit_all", False)
                    ),
                    "scan_shared_miss": getattr(hub.registry, "last_scan_shared_miss", None),
                }
            )
        refresh.update(summarize([item["ms"] for item in refresh["samples"]]))

        refresh_state: dict = {"samples": []}
        for index in range(5):
            if deadline.exceeded():
                timed_out = True
                break
            try:
                elapsed, changed = _timed_ms(hub.store.refresh_state)
            except Exception as exc:  # noqa: BLE001 — report, keep earlier samples
                errors.append(f"refresh_state[{index}]: {redacted_error(exc)}")
                break
            refresh_state["samples"].append({"ms": round(elapsed, 3), "changed": bool(changed)})
        refresh_state.update(summarize([item["ms"] for item in refresh_state["samples"]]))

        selected = select_samples(sessions, args.samples, session_key)
        measured: list[dict] = []
        for index, sample in enumerate(selected, start=1):
            if deadline.exceeded():
                timed_out = True
                break
            try:
                measured.append(
                    _measure_message_page(
                        hub,
                        sample,
                        args.page_limit,
                        cache_root,
                        f"s{index}",
                        TranscriptCache,
                    )
                )
            except Exception as exc:  # noqa: BLE001 — one bad session must not lose the run
                errors.append(f"{sample.id}: {redacted_error(exc)}")
                _drop_transcript(hub, sample.key)
                _discard_transcript_cache(hub)
                measured.append({**sample.public(), "error": redacted_error(exc)})

        concurrency: dict
        healthy = [
            sample
            for sample in selected
            if any(item["id"] == sample.id and "error" not in item for item in measured)
        ]
        if deadline.exceeded():
            timed_out = True
            concurrency = {"pair": [], "n": 0, "error": "timeout"}
        else:
            concurrency = _measure_concurrency(
                hub,
                healthy,
                args.page_limit,
                cache_root,
                args.concurrency_rounds,
                TranscriptCache,
            )
        if concurrency.get("error") and concurrency["error"] != "need_two_sessions":
            errors.append(f"concurrency: {redacted_error(RuntimeError(str(concurrency['error'])))}")

        snapshot_samples: list[dict] = []
        snapshot: dict = {}
        if not deadline.exceeded():
            snapshot_payload: dict = {}
            for _ in range(3):
                elapsed, snapshot_payload = _timed_ms(hub.list_snapshot)
                snapshot_samples.append({"ms": round(elapsed, 3)})
            unchanged_samples: list[float] = []
            version = str(snapshot_payload.get("version") or "")
            for _ in range(2):
                elapsed, _unchanged = _timed_ms(
                    lambda: hub.list_snapshot(since_version=version)
                )
                unchanged_samples.append(round(elapsed, 3))
            snapshot = {
                **summarize([item["ms"] for item in snapshot_samples]),
                "samples": snapshot_samples,
                "rows": len(snapshot_payload.get("sessions") or []),
                "total": snapshot_payload.get("total"),
                "has_more": bool(snapshot_payload.get("has_more")),
                "json_bytes": len(
                    json.dumps(
                        snapshot_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ),
                "unchanged_ms": unchanged_samples,
            }
        else:
            timed_out = True
            snapshot = {"n": 0, "samples": []}

        cold_load = _probe_cold_load(args, cache_root, deadline)
        if cold_load.get("error"):
            errors.append(f"cold_load: {cold_load['error']}")

        duration_ms = (time.time() - started_wall) * 1000.0
        try:
            load_average_end = list(os.getloadavg())
        except (AttributeError, OSError):
            load_average_end = []
        result = {
            "schema": 1,
            "kind": "corral-remote-open-benchmark",
            "started_at": started_at,
            "finished_at": utc_now(),
            "duration_ms": round(duration_ms, 3),
            "timed_out": timed_out,
            "machine": args.machine_label,
            "platform": {
                "system": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "corral_version": getattr(corral, "__version__", "unknown"),
            "load_average": {"start": load_average_start, "end": load_average_end},
            "config": {
                "samples_per_runtime": args.samples,
                "scan_limit": args.limit,
                "page_limit": args.page_limit,
                "timeout_s": args.timeout,
                "cold_load_timeout_s": args.cold_load_timeout,
                "concurrency_rounds": args.concurrency_rounds,
            },
            "environment": {
                "cache_mode": (
                    "reused-cache-root" if not owns_cache else "isolated-temp"
                ),
                "cache_preloaded": preloaded,
                "shared_index_requested": not args.no_shared_index,
                "shared_index_seeded": bool(fixture.seeded),
                "shared_index_reason": fixture.reason,
                "shared_index_age_s": fixture.index_age_s,
                "scan_worker": "disabled-by-benchmark",
                "machine_label_redacted": args.machine_label == "local",
            },
            "definitions": _DEFINITIONS,
            "load": load,
            "refresh": refresh,
            "refresh_state": refresh_state,
            "sessions": measured,
            "concurrency": concurrency,
            "list_snapshot": snapshot,
            "cold_load": cold_load,
            "errors": errors,
        }
    except Exception as exc:  # noqa: BLE001 — always emit a report if at all possible
        errors.append(f"fatal: {redacted_error(exc)}")
        result = {
            "schema": 1,
            "kind": "corral-remote-open-benchmark",
            "started_at": started_at,
            "finished_at": utc_now(),
            "duration_ms": round((time.time() - started_wall) * 1000.0, 3),
            "timed_out": timed_out,
            "machine": args.machine_label,
            "platform": {
                "system": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "corral_version": "unknown",
            "load_average": {"start": load_average_start, "end": []},
            "config": {
                "samples_per_runtime": args.samples,
                "scan_limit": args.limit,
                "page_limit": args.page_limit,
                "timeout_s": args.timeout,
                "cold_load_timeout_s": args.cold_load_timeout,
                "concurrency_rounds": args.concurrency_rounds,
            },
            "environment": {
                "cache_mode": "isolated-temp" if owns_cache else "reused-cache-root",
                "cache_preloaded": preloaded,
                "shared_index_requested": not args.no_shared_index,
                "shared_index_seeded": False,
                "scan_worker": "disabled-by-benchmark",
                "machine_label_redacted": args.machine_label == "local",
            },
            "definitions": _DEFINITIONS,
            "load": {"mode": "failed", "counts": {}, "session_count": 0},
            "refresh": {"samples": [], "n": 0},
            "refresh_state": {"samples": [], "n": 0},
            "sessions": [],
            "concurrency": {"pair": [], "n": 0},
            "list_snapshot": {"samples": [], "n": 0},
            "cold_load": {"skipped": True, "reason": "fatal"},
            "errors": errors,
        }
    finally:
        try:
            if hub is not None:
                hub.stop()
                layout_db = getattr(hub, "layout_db", None)
                if layout_db is not None:
                    layout_db.close()
        except Exception:  # noqa: BLE001 — teardown is best-effort
            cleanup_ok = False
        time.sleep(0.05)
        if owns_cache:
            shutil.rmtree(cache_root, ignore_errors=True)
            cleanup_ok = cleanup_ok and not cache_root.exists()

    result["environment"]["cache_cleaned"] = cleanup_ok
    hard_stop.cancel()

    markdown = render_markdown(result)
    print(markdown)
    if args.json:
        try:
            _atomic_write_text(Path(args.json).expanduser(), json.dumps(result, ensure_ascii=False, indent=2))
        except OSError as exc:
            print(f"\njson write failed: {redacted_error(exc)}", file=sys.stderr)
            return 1
    elif not args.markdown:
        print("\n```json")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("```")
    if args.json:
        print(f"\nJSON: {Path(args.json).expanduser()}")
    if not result.get("sessions"):
        return 1
    return 2 if result.get("timed_out") else 0


if __name__ == "__main__":
    raise SystemExit(main())
