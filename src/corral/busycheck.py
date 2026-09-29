"""Shared busy/idle verdict: never treat a hosted agent waiting on background work as idle.

On 2026-09-30 a whole-server migration ended every hosted agent because its
``wait_until_idle`` only looked at the ``working`` attention phase. A Claude
Code session that had finished its reply but was still waiting on its own
background shell tasks was counted as idle, and its background waiters were
lost. The same idle judgment backs silent reclaim (``reclaim.py``) and the
legacy keepalive reaping (``keepalive.reap_idle`` / ``reap_pressure``), so the
verdict lives here once and all three destructive paths share it. Contract in
``docs/MAINTAINER_GUIDE.md`` ("A hosted session waiting on background work is
busy, never idle").

Two signals, cheapest first. Both are snapshot-based: one ``BusyChecker`` per
pass holds a single ``ps`` table plus one ``tmux list-panes`` pane-pid map, so
a periodic tick costs two subprocess forks no matter how many sessions exist:

1. Process tree: live descendants of the pane pid other than the agent binary
   itself and known long-lived helpers (MCP servers, language servers, agent
   daemons, matched by command/args patterns). Anything unrecognized counts
   as busy — when unsure, destructive actions must wait. Single-snapshot CPU
   times cannot show recency without a second sample and per-descendant
   ``lsof`` is too costly for a tick, so mere existence of a non-helper
   descendant is the signal. Daemons that reparented to init escape this
   signal by design; the transcript signal covers the registered ones.
2. Transcript (Claude only in v1): a ``queue-operation`` ``<task-notification>``
   enqueue without a matching remove, or a ``moved to the background (ID: …)``
   tool result without a later completion, means the agent still expects a
   completion notification. Only sessions that would otherwise read as idle
   pay for this read, and only the tail/full small-file scan they need. Other
   runtimes rely on signal (1) until their markers are mapped.

A failed probe is never a licence: ``from_probe`` returns None when tmux or
``ps`` is unusable, and callers must treat that as "cannot prove idle" (the
reclaim pass becomes a no-op, reaping skips, migration keeps waiting).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# Agent foreground binaries: the pane's own process (or its direct agent
# child when tmux wrapped the command in a shell) is never background work.
_AGENT_COMMS = frozenset({
    "claude", "codex", "opencode", "pi", "cursor-agent", "agent", "kimi",
    "gemini", "node", "bun", "deno", "python", "python3",
})
# ...but bare runtimes only count as the agent when their command line says so.
_AGENT_ARGS = (
    "corral.codex_proxy", "app-server", "/claude", "claude-code",
    "opencode", "/pi", "cursor-agent", "kimi", "codex",
)
# Long-lived helpers that stay resident under an idle agent and must not keep
# it "busy" forever. Narrow on purpose: anything unrecognized counts as busy.
# (Observed per-agent fan-out on a 16 GB Mac: server-memory,
# codebase-memory-mcp, node_repl, node; see PERFORMANCE_KNOWLEDGE_BASE.md E9.)
_HELPER_ARGS = (
    "mcp", "modelcontextprotocol", "server-memory", "codebase-memory",
    "node_repl", "language-server", "language_server", "pygls",
    "typescript-language", "tsserver", "gopls", "rust-analyzer", "clangd",
    "pylsp", "basedpyright", "pyright", "opencode serve",
)

_TASK_ID = re.compile(r"<task-id>([^<>\s]+)</task-id>")
_OPERATION = re.compile(r'"operation"\s*:\s*"(enqueue|remove)"')
_MOVED_TO_BACKGROUND = re.compile(r"moved to the background \(ID:\s*([^)]+)\)")
# Full-file scan is bounded: only candidate sessions pay, and histories bigger
# than this fall back to the tail window (a pending task older than the window
# then reads as idle — documented miss, the process tree still guards live work).
_MAX_FULL_SCAN_BYTES = 8 * 1024 * 1024
_TAIL_SCAN_BYTES = 256 * 1024

_PS_TIMEOUT = 1.5


@dataclass(frozen=True)
class Proc:
    """One row of ``ps -eo pid,ppid,command``."""

    pid: int
    ppid: int
    command: str

    @property
    def comm(self) -> str:
        base = self.command.strip().split(" ")[0] if self.command.strip() else ""
        return os.path.basename(base).lower()


@dataclass(frozen=True)
class BusyVerdict:
    """One hosted session's background-work verdict."""

    busy: bool
    reason: str  # "idle" | "background_process" | "background_transcript" | "background_unknown"
    evidence: dict[str, Any] = field(default_factory=dict)


def _looks_like_agent(proc: Proc) -> bool:
    lowered = proc.command.lower()
    if proc.comm in _AGENT_COMMS and any(hint in lowered for hint in _AGENT_ARGS):
        return True
    return False


def _looks_like_helper(proc: Proc) -> bool:
    lowered = proc.command.lower()
    return any(hint in lowered for hint in _HELPER_ARGS)


def _is_self_or_agent_or_helper(proc: Proc, pane_pid: int) -> bool:
    if proc.pid == pane_pid:
        return True
    return _looks_like_agent(proc) or _looks_like_helper(proc)


def descendants(pane_pid: int, table: Sequence[Proc]) -> list[Proc]:
    """Every live process under ``pane_pid`` (children, grandchildren, ...)."""
    children: dict[int, list[Proc]] = {}
    for proc in table:
        children.setdefault(proc.ppid, []).append(proc)
    found: list[Proc] = []
    stack = list(children.get(pane_pid, []))
    seen = {pane_pid}
    while stack:
        proc = stack.pop()
        if proc.pid in seen:
            continue
        seen.add(proc.pid)
        found.append(proc)
        stack.extend(children.get(proc.pid, ()))
    return found


def snapshot_ps(
    run: Callable[..., Any] | None = None,
) -> list[Proc] | None:
    """One ``ps`` call for the whole machine; None when it cannot be read."""
    check = run or subprocess.check_output
    try:
        out = check(
            ["ps", "-eo", "pid,ppid,command"],
            stderr=subprocess.DEVNULL, timeout=_PS_TIMEOUT,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    if isinstance(out, bytes):
        out = out.decode(errors="ignore")
    table: list[Proc] = []
    for line in out.splitlines()[1:]:  # skip the header
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        table.append(Proc(pid, ppid, parts[2] if len(parts) > 2 else ""))
    return table


def process_verdict(pane_pid: int | None, table: Sequence[Proc] | None) -> BusyVerdict:
    """Signal (1): busy when a non-agent, non-helper descendant is alive."""
    if pane_pid is None or pane_pid <= 0:
        return BusyVerdict(True, "background_unknown", {"why": "no_pane_pid"})
    if table is None:
        return BusyVerdict(True, "background_unknown", {"why": "ps_unavailable"})
    if all(proc.pid != pane_pid for proc in table):
        # The pane was listed alive but its pid is gone from ps: a race, pid
        # reuse, or a truncated snapshot — never proof of idle.
        return BusyVerdict(True, "background_unknown", {"why": "pane_not_in_ps"})
    strangers = [
        proc for proc in descendants(pane_pid, table)
        if not _is_self_or_agent_or_helper(proc, pane_pid)
    ]
    if strangers:
        shown = sorted({proc.command[:120] for proc in strangers})[:5]
        return BusyVerdict(True, "background_process", {
            "pane_pid": pane_pid,
            "background_commands": shown,
            "stranger_count": len(strangers),
        })
    return BusyVerdict(False, "idle", {
        "pane_pid": pane_pid,
        "descendant_count": len(descendants(pane_pid, table)),
    })


def _tail_text(path: str, limit: int) -> str | None:
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    try:
        with open(path, "rb") as handle:
            offset = max(0, size - limit)
            handle.seek(offset)
            raw = handle.read()
            if offset > 0:
                # The window starts mid-line: drop the fragment so a cut
                # marker line can neither match nor unmatch half-way. (A cut
                # line that was the still-open enqueue is then missed — a
                # narrow, documented tail-window miss; the process tree still
                # guards live work.)
                _, _, raw = raw.partition(b"\n")
            return raw.decode("utf-8", "ignore")
    except OSError:
        return None


def claude_pending_background(path: str | None) -> tuple[bool, dict[str, Any]]:
    """Signal (2): True when a Claude history shows a background task started
    without a completion record (enqueue without remove, by ``<task-id>``).

    Only lines carrying background markers are parsed; the file is read once,
    whole when small, tail-only when large.
    """
    if not path:
        return False, {"checked": False, "why": "no_history_path"}
    try:
        size = os.path.getsize(path)
    except OSError:
        return False, {"checked": False, "why": "history_unreadable"}
    if not path.endswith(".jsonl"):
        return False, {"checked": False, "why": "not_claude_jsonl"}
    text = _tail_text(path, _MAX_FULL_SCAN_BYTES if size <= _MAX_FULL_SCAN_BYTES else _TAIL_SCAN_BYTES)
    if text is None:
        return False, {"checked": False, "why": "history_unreadable"}
    enqueued: set[str] = set()
    removed: set[str] = set()
    moved: set[str] = set()
    for line in text.splitlines():
        if "queue-operation" in line and "task-notification" in line:
            match = _TASK_ID.search(line)
            op = _OPERATION.search(line)
            if not match or op is None:
                continue
            if op.group(1) == "remove":
                removed.add(match.group(1))
            else:
                enqueued.add(match.group(1))
        elif "moved to the background" in line:
            match = _MOVED_TO_BACKGROUND.search(line)
            if match:
                moved.add(match.group(1).strip())
    # A moved-to-background command surfaces its completion through the same
    # task-notification queue, so its ID joins the enqueue set only when no
    # notification for it was ever queued. IDs differ in shape (tool-use ids
    # vs task ids), therefore the two sets are checked independently below.
    pending = enqueued - removed
    evidence: dict[str, Any] = {
        "checked": True,
        "window": "full" if size <= _MAX_FULL_SCAN_BYTES else "tail",
        "pending_task_ids": sorted(pending)[:5],
        "background_moved_ids": sorted(moved)[:5],
    }
    if pending:
        return True, evidence
    if moved and not enqueued:
        # The move was recorded but no completion notification was ever
        # queued: the agent is still holding the waiter.
        return True, evidence
    return False, evidence


class BusyChecker:
    """One pass worth of background-work verdicts; build one per tick.

    ``ps_table`` / ``pane_pids`` may be injected (tests, or callers that
    already listed panes); otherwise the first verdict lazily probes once and
    every later verdict reuses the snapshot.
    """

    def __init__(
        self,
        *,
        ps_table: Sequence[Proc] | None = None,
        pane_pids: Mapping[str, int] | None = None,
        ps_fetcher: Callable[[], Sequence[Proc] | None] | None = None,
        pane_fetcher: Callable[[], Mapping[str, int] | None] | None = None,
        history_paths: Mapping[str, str] | None = None,
        transcript_runtimes: Sequence[str] = ("claude",),
    ) -> None:
        self._ps_table = ps_table
        self._pane_pids = dict(pane_pids) if pane_pids is not None else None
        self._ps_fetcher = ps_fetcher or snapshot_ps
        self._pane_fetcher = pane_fetcher
        self._history_paths = dict(history_paths) if history_paths else {}
        self._transcript_runtimes = frozenset(transcript_runtimes)
        self._ps_failed = False
        self._panes_failed = False

    @classmethod
    def from_probe(
        cls,
        *,
        sockets: Sequence[str] | None = None,
        history_paths: Mapping[str, str] | None = None,
    ) -> BusyChecker | None:
        """Probe tmux pane pids on the managed sockets. None when tmux or ps
        is unusable — the caller must then prove nothing idle."""
        from corral import keepalive
        from corral.legacy_names import ALL_SOCKET_NAMES, tmux_base_argv

        if shutil.which("tmux") is None:
            return None
        names = tuple(sockets) if sockets is not None else ALL_SOCKET_NAMES

        def fetch() -> Mapping[str, int] | None:
            merged: dict[str, int] = {}
            for socket in names:
                try:
                    out = subprocess.check_output(
                        [*tmux_base_argv(socket),
                         "list-panes", "-a", "-F", "#{session_name}|#{pane_pid}"],
                        stderr=subprocess.DEVNULL, timeout=keepalive.SUBPROCESS_TIMEOUT,
                        env=keepalive.tmux_env(),
                    ).decode()
                except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    return None
                for line in out.splitlines():
                    name, _, pid = line.partition("|")
                    try:
                        merged[name] = int(pid.strip())
                    except ValueError:
                        continue
            return merged

        checker = cls(pane_fetcher=fetch, history_paths=history_paths)
        if checker._panes() is None or checker._ps() is None:
            return None
        return checker

    def _ps(self) -> Sequence[Proc] | None:
        if self._ps_table is None and not self._ps_failed:
            try:
                fetched = self._ps_fetcher()
            except Exception:  # noqa: BLE001 - a broken probe must read as unknown
                fetched = None
            if fetched is None:
                self._ps_failed = True
            else:
                self._ps_table = fetched
        return None if self._ps_failed else self._ps_table

    def _panes(self) -> Mapping[str, int] | None:
        if self._pane_pids is None and not self._panes_failed:
            if self._pane_fetcher is None:
                self._panes_failed = True
            else:
                try:
                    fetched = self._pane_fetcher()
                except Exception:  # noqa: BLE001
                    fetched = None
                if fetched is None:
                    self._panes_failed = True
                else:
                    self._pane_pids = dict(fetched)
        return None if self._panes_failed else self._pane_pids

    def verdict(self, name: str, *, runtime_id: str = "") -> BusyVerdict:
        """Process-tree verdict first; the transcript check runs only when
        the tree is quiet (it costs a history read)."""
        panes = self._panes()
        if panes is None:
            return BusyVerdict(True, "background_unknown", {"why": "pane_map_unavailable"})
        proc = process_verdict(panes.get(name), self._ps())
        if proc.busy:
            return proc
        if runtime_id in self._transcript_runtimes:
            pending, evidence = claude_pending_background(self._history_paths.get(name))
            merged = {**proc.evidence, "transcript": evidence}
            if pending:
                return BusyVerdict(True, "background_transcript", merged)
            return BusyVerdict(False, "idle", merged)
        return proc

    def process_busy_names(self, names: Sequence[str]) -> dict[str, BusyVerdict]:
        """Process-tree verdicts only, sharing this tick's snapshot.

        The transcript signal stays lazy: callers run it (via ``verdict``)
        only for sessions that would otherwise read as idle.
        """
        panes = self._panes()
        if panes is None:
            return {
                name: BusyVerdict(True, "background_unknown", {"why": "pane_map_unavailable"})
                for name in names
            }
        table = self._ps()
        out: dict[str, BusyVerdict] = {}
        for name in names:
            verdict = process_verdict(panes.get(name), table)
            if verdict.busy:
                out[name] = verdict
        return out

def measure_cost(
    sockets: Sequence[str] | None = None, *, repeats: int = 3,
) -> dict[str, float]:
    """Time the per-tick probe (one ps + one list-panes per socket)."""
    costs: list[float] = []
    for _ in range(max(1, repeats)):
        start = time.perf_counter()
        checker = BusyChecker.from_probe(sockets=sockets)
        if checker is not None:
            checker.process_busy_names([])
        costs.append((time.perf_counter() - start) * 1000.0)
    return {
        "runs": float(len(costs)),
        "min_ms": min(costs),
        "max_ms": max(costs),
    }
