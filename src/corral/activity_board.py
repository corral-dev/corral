"""Status dots and the Attention list-filter predicate.

Sidebar dots include waiting, working, unread and real recent hosted activity.
Native completion or interruption evidence blocks the recent fallback; fresh
working/waiting wins. The Attention filter adds recent unread results and
sessions that ended or were opened within the last half hour. Remote clients
consume the same host-computed evidence.
"""

from __future__ import annotations

import time
from typing import Literal

from sesskit.titles import STATUS_ABORTED, STATUS_DONE

from corral.attention import AttentionKind
from corral.display import JUST_NOW_SECONDS

_SIGNAL_KINDS: frozenset[AttentionKind] = frozenset({"waiting", "working", "unread"})
# 界面圆点与活跃筛选共用的活跃标记。「刚刚」档的圆点画成绿。
ActiveMarker = Literal["waiting", "working", "unread", "recent"]
ACTIVE_MARKER_STYLES: dict[str, str] = {
    "waiting": "bold yellow",
    "working": "bold green",
    "unread": "bold red",
    # 「刚刚」仍活跃、但没有待办信号：并进绿点，禁止单独画青/蓝。
    "recent": "bold green",
}
# 「刚刚还在活跃」与侧栏时间行「刚刚」共用同一条界（display.JUST_NOW_SECONDS）
# 和同一时间源（会话最近真实活动时间）：侧栏显示「刚刚」的托管会话，也算活跃。
RECENT_ACTIVE_SECONDS = JUST_NOW_SECONDS
# Attention filter windows (owner decision 2026-10-26): a session stays in view
# for half an hour after its last activity or open; unread results for a week.
ATTENTION_RECENT_SECONDS = 30 * 60
ATTENTION_UNREAD_SECONDS = 7 * 24 * 60 * 60


def resolve_active_marker(
    session: dict | None = None,
    *,
    attention_kind: str | None = None,
    hosted: bool | None = None,
    mtime: float | None = None,
    now: float | None = None,
) -> ActiveMarker | None:
    """侧栏圆点与活跃筛选的共用判定。

      - 等回话 / 干活 / 未读 → 对应黄 / 绿 / 红（不要求本窗口托管，外部会话也可画点）
      - 本窗口托管且「刚刚」窗口内仍有活动、但无待办信号 → ``recent``（绿点，与干活同色）；
        当前轮已原生结束（中断，或带完成标识的已完成）时不给
      - 否则无标记

      Preserve ``recent``; 也禁止再给这一档单独画青/蓝点。
    """
    if session is not None:
        if attention_kind is None:
            attention_kind = session.get("attention_kind")
        if hosted is None:
            hosted = bool(session.get("keepalive_name"))
        if mtime is None:
            mtime = float(session.get("mtime") or 0.0)
    kind = str(attention_kind or "none")
    if kind in _SIGNAL_KINDS:
        return kind  # type: ignore[return-value]
    # A retained process or metadata write cannot restart an ended turn: an
    # aborted turn, or a completed one with native terminal evidence (non-empty
    # completion id). Fresh working/waiting evidence above still wins when the
    # agent resumes.
    if session is not None and (
        session.get("status_tag") == STATUS_ABORTED
        or (session.get("status_tag") == STATUS_DONE and session.get("completion_id"))
    ):
        return None
    if not hosted:
        return None
    if now is None:
        now = time.time()
    stamp = float(mtime or 0.0)
    # 未来时间 / 时钟漂移出的负差值按刚刚活跃处理（与 display 的相对时间同规则）。
    if not stamp or now - stamp > RECENT_ACTIVE_SECONDS:
        return None
    return "recent"


def needs_attention(session: dict, *, now: float | None = None) -> bool:
    """Attention filter membership, shared by the TUI and remote rows.

    Any yellow/green dot, an unread result from the last week, or a session
    whose last activity or deliberate open (``viewed_at``) is within half an
    hour. A pane that merely stays open is not an open.
    """
    if now is None:
        now = time.time()
    marker = resolve_active_marker(session, now=now)
    if marker in {"waiting", "working", "recent"}:
        return True
    mtime = float(session.get("mtime") or 0.0)
    if marker == "unread" and _within(mtime, now, ATTENTION_UNREAD_SECONDS):
        return True
    anchor = max(mtime, float(session.get("viewed_at") or 0.0))
    return _within(anchor, now, ATTENTION_RECENT_SECONDS)


def _within(stamp: float, now: float, seconds: float) -> bool:
    # Future stamps from clock drift count as fresh, like relative times.
    return bool(stamp) and now - stamp <= seconds


def active_marker_style(marker: str | None) -> str | None:
    """圆点 Rich 样式；无标记时返回 None（不留占位空格）。"""
    if not marker:
        return None
    return ACTIVE_MARKER_STYLES.get(str(marker))
