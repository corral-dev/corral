"""活跃会话看板：自动铺当前需要盯的托管会话，不写入持久分屏组合。

成员资格（权威口径）：corral 自己托管、且关注态是等待回答 / 执行中 /
未读新结果，或「刚刚」（与侧栏时间行同一条 3 分钟界）内还有真实对话活动。
侧栏关注圆点必须覆盖同一集合（``resolve_active_marker``），缺圆点时补圆点，
禁止反过来砍掉看板成员。别的窗口里跑的会话没有实时画面，不进格子。
超过一页时当前页成员冻结，新急件排到后面；格子空出来才从队列按优先级补位。
**正在看看板期间当前页成员不主动撤**：只要会话仍被 corral 托管，跑完、已读、
不再活跃都继续钉在原格，直到离开看板、显式关格或会话不再托管；显式翻页按
当时的队列重切，不在队列里的成员随之让位。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Literal

from sesskit.titles import STATUS_ABORTED

from corral.attention import AttentionKind, AttentionState
from corral.display import JUST_NOW_SECONDS
from corral.split_layout import MAX_PANES

BOARD_KINDS: frozenset[AttentionKind] = frozenset({"waiting", "working", "unread"})
# 界面圆点 / 看板排序共用的活跃标记。「刚刚」档仍单独排序，但圆点画成绿。
ActiveMarker = Literal["waiting", "working", "unread", "recent"]
ACTIVE_MARKER_STYLES: dict[str, str] = {
    "waiting": "bold yellow",
    "working": "bold green",
    "unread": "bold red",
    # 「刚刚」仍活跃、但没有待办信号：并进绿点，禁止单独画青/蓝。
    "recent": "bold green",
}
_KIND_RANK: dict[str, int] = {
    "waiting": 0,
    "working": 1,
    "unread": 2,
    "recent": 3,
    "none": 3,
}
# 「刚刚还在活跃」与侧栏时间行「刚刚」共用同一条界（display.JUST_NOW_SECONDS）
# 和同一时间源（会话最近真实活动时间）：侧栏显示「刚刚」的托管会话，看板也认活跃。
RECENT_ACTIVE_SECONDS = JUST_NOW_SECONDS


@dataclass(frozen=True)
class BoardCandidate:
    """一条够格进看板的托管会话。"""

    key: str
    kind: ActiveMarker
    updated_at: float = 0.0


@dataclass(frozen=True)
class BoardSnapshot:
    """当前页要展示的成员，以及翻页/角标所需的计数。"""

    keys: tuple[str, ...]
    page: int
    page_count: int
    total: int
    waiting_off_page: int
    waiting_total: int


def resolve_active_marker(
    session: dict | None = None,
    *,
    attention_kind: str | None = None,
    hosted: bool | None = None,
    mtime: float | None = None,
    now: float | None = None,
) -> ActiveMarker | None:
    """Active sessions 与侧栏圆点的共用判定。

      - 等回话 / 干活 / 未读 → 对应黄 / 绿 / 红（不要求本窗口托管，外部会话也可画点）
      - 本窗口托管且「刚刚」窗口内仍有活动、但无待办信号 → ``recent``（绿点，与干活同色）
      - 否则无标记

      Active sessions 看板只收录本窗口托管成员；圆点用同一函数，保证凡进 Active
      的会话侧栏都有点。禁止为对齐而去掉 ``recent`` 档，也禁止再给这一档单独画青/蓝点。
    """
    if session is not None:
        if attention_kind is None:
            attention_kind = session.get("attention_kind")
        if hosted is None:
            hosted = bool(session.get("keepalive_name"))
        if mtime is None:
            mtime = float(session.get("mtime") or 0.0)
    kind = str(attention_kind or "none")
    if kind in BOARD_KINDS:
        return kind  # type: ignore[return-value]
    # A retained process or metadata write cannot restart an interrupted turn.
    # Fresh working/waiting evidence above still wins when the agent resumes.
    if session is not None and session.get("status_tag") == STATUS_ABORTED:
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


def active_marker_style(marker: str | None) -> str | None:
    """圆点 Rich 样式；无标记时返回 None（不留占位空格）。"""
    if not marker:
        return None
    return ACTIVE_MARKER_STYLES.get(str(marker))


def active_marker_rank(marker: str | None) -> int:
    """看板排序：等回话 > 干活 > 未读 > 刚刚活跃。"""
    return _KIND_RANK.get(str(marker or "none"), 9)


def collect_candidates(store, now: float | None = None) -> list[BoardCandidate]:
    """从会话库收集够格的托管会话，按「等回话 > 干活 > 未读 > 刚刚活跃」排。

    「刚刚活跃」档没有待办信号：会话最近真实活动（mtime，与侧栏「刚刚」文案
    同源）还在 ``RECENT_ACTIVE_SECONDS`` 窗口内即算。
    """
    import corral
    from corral.models import is_shell_session

    if now is None:
        now = time.time()
    candidates: list[BoardCandidate] = []
    for session in store.all_sessions():
        if is_shell_session(session):
            continue
        if not session.get("keepalive_name"):
            continue
        key = corral.session_key(session)
        state: AttentionState = store.attention_for(key)
        mtime = float(session.get("mtime") or 0.0)
        marker = resolve_active_marker(
            session,
            attention_kind=state.kind,
            hosted=True,
            mtime=mtime,
            now=now,
        )
        if marker is None:
            continue
        candidates.append(
            BoardCandidate(
                key=key,
                kind=marker,
                updated_at=max(state.updated_at, mtime),
            )
        )
    candidates.sort(
        key=lambda item: (active_marker_rank(item.kind), -item.updated_at, item.key)
    )
    return candidates


def collect_hosted_keys(store) -> set[str]:
    """当前仍被 corral 托管的会话键集合：观看期间「不撤格」的底线。

    会话结束、保活回收后 ``keepalive_name`` 会消失，此时格子已没有实时
    画面可铺，必须允许撤掉；仍被托管但关注态已消退的会话则由调用方
    据此钉在当前页，不再因跑完 / 已读被抽走。
    """
    import corral
    from corral.models import is_shell_session

    keys: set[str] = set()
    for session in store.all_sessions():
        if is_shell_session(session):
            continue
        if session.get("keepalive_name"):
            keys.add(corral.session_key(session))
    return keys


class ActivityBoard:
    """一次进入看板期间的稳定分页状态。离开看板时 ``reset()``。"""

    def __init__(self) -> None:
        self._page = 0
        self._locked: list[str] = []
        self._skipped: set[str] = set()
        self._typing_key: str | None = None
        self._eligible: list[str] = []

    def reset(self) -> None:
        """离开看板：丢掉本轮冻结页、跳过名单和打字钉住。"""
        self._page = 0
        self._locked = []
        self._skipped.clear()
        self._typing_key = None
        self._eligible = []

    def set_typing_key(self, key: str | None) -> None:
        """正在看的那一格：不够格也不撤，直到焦点离开。

        本轮已关掉的格子不能再钉住，否则关格后焦点还在那一格时会弹回来。
        """
        if key and key in self._skipped:
            self._typing_key = None
            return
        self._typing_key = key or None

    def dismiss(self, key: str) -> None:
        """本轮访问里把这一格拿掉（关格）；关注态变化或重新进入后再出现。"""
        if not key:
            return
        self._skipped.add(key)
        self._locked = [item for item in self._locked if item != key]
        if self._typing_key == key:
            self._typing_key = None

    def turn_page(self, delta: int) -> None:
        """显式翻页：按当前队列重新切片。

        键盘翻页在格子持焦打字时不要调（方括号会打进助手）。鼠标点侧栏
        翻页控件可以：调用方先清打字钉再切页。

        翻页是用户的主动导航：被钉住但已不在队列里的成员（跑完、已读）
        随重切让位，不再跨页钉住。多页时循环：末页再下一页回到首页，
        首页再上一页到末页。
        """
        eligible = [key for key in self._eligible if key not in self._skipped]
        if not eligible:
            self._page = 0
            self._locked = []
            return
        if not delta:
            return
        page_count = max(1, math.ceil(len(eligible) / MAX_PANES))
        if page_count <= 1:
            self._page = 0
            self._locked = eligible[:MAX_PANES]
            return
        self._page = (self._page + delta) % page_count
        start = self._page * MAX_PANES
        self._locked = eligible[start:start + MAX_PANES]

    def sync(
        self,
        candidates: list[BoardCandidate],
        hosted_keys: set[str] | None = None,
    ) -> BoardSnapshot:
        """按「当前页不插队、空位才补」更新锁定成员，返回这一帧快照。

        正在看看板期间不主动撤格：当前页成员只要仍在 ``hosted_keys`` 里
        就保留，即使关注态已经消退（跑完、已读、不再活跃）。撤格只发生在
        离开看板（``reset``）、显式关格（``dismiss``）、会话不再被托管，
        或显式翻页按当前队列重切时。

        补位不得把更前页的人拉进本页：队头新插进来的急件算前页，
        翻到后页后空位只从本页已有成员之后的队列取。
        """
        eligible = [
            item.key
            for item in candidates
            if item.key not in self._skipped
        ]
        self._eligible = list(eligible)
        eligible_set = set(eligible)
        hosted_set = hosted_keys or set()
        typing = self._typing_key

        if not self._locked:
            self._locked = eligible[:MAX_PANES]
            self._page = 0
        else:
            kept: list[str] = []
            for key in self._locked:
                if key in self._skipped:
                    continue
                if key == typing or key in eligible_set or key in hosted_set:
                    kept.append(key)
            # 后页空位不得用「当前队列下标」去切：新急件插到队头后，前页成员
            # 会整体后移，看起来像被补进本页。page>0 时，排在本页已有成员前面
            # 的一律视为更前页/插队，只从本页成员之后的队列补。
            min_locked_pos = None
            if self._page > 0:
                pos = {key: index for index, key in enumerate(eligible)}
                locked_positions = [pos[key] for key in kept if key in pos]
                if locked_positions:
                    min_locked_pos = min(locked_positions)
            for key in eligible:
                if len(kept) >= MAX_PANES:
                    break
                if key in kept:
                    continue
                if min_locked_pos is not None:
                    key_pos = pos.get(key)
                    if key_pos is not None and key_pos < min_locked_pos:
                        continue
                elif self._page > 0:
                    # 本页成员已全部不在队列里：不要用会错位的 start 下标，留给下面整页重切。
                    continue
                kept.append(key)
            if typing and typing not in kept and typing not in self._skipped:
                if len(kept) < MAX_PANES:
                    kept.append(typing)
                else:
                    kept[-1] = typing
            if not kept and eligible:
                page_count = max(1, math.ceil(len(eligible) / MAX_PANES))
                self._page = min(self._page, page_count - 1)
                kept = eligible[self._page * MAX_PANES:][:MAX_PANES]
            self._locked = kept

        visible = tuple(self._locked)
        # 被钉住的「已不够格但仍托管」成员也计入角标总数，否则侧栏会话数
        # 与右栏实际格子数对不上；打字钉住的格子沿用旧口径不计入。
        held = [
            key for key in visible
            if key not in eligible_set and key != typing
        ]
        total = len(eligible) + len(held)
        page_count = max(1, math.ceil(total / MAX_PANES)) if total else 1
        if self._page >= page_count:
            if held:
                # 本页还有被钉住的成员：页码跟着当前页走，不要因为够格队列缩短就把人打回第 1 页。
                page_count = self._page + 1
            else:
                self._page = page_count - 1
        waiting_keys = {item.key for item in candidates if item.kind == "waiting"}
        waiting_total = sum(1 for key in eligible if key in waiting_keys)
        waiting_off_page = sum(
            1 for key in eligible if key in waiting_keys and key not in visible
        )
        return BoardSnapshot(
            keys=visible,
            page=self._page,
            page_count=page_count,
            total=total,
            waiting_off_page=waiting_off_page,
            waiting_total=waiting_total,
        )
