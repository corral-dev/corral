"""会话中枢：把 corral 已有的会话能力包成一套供手机调用的接口。

这一层是 `corral remote` 里唯一持有 `SessionStore`、tmux 托管层和布局库的地方，
上面的协议层只管路由。所有带副作用的动作（送输入、新建、结束、删除）都收在这里，
`agent_api` 保持一行不动的只读契约。

线程模型：一个后台线程按固定周期重扫磁盘（与桌面端 TUI 同一套 `SessionStore`），
一个后台线程按需抓取被订阅会话的终端画面。两者都只往队列里塞事件，网络侧在自己的
事件循环里取走，彼此不阻塞。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from sesskit import titles as sesskit_titles

from corral import embed, keepalive, titles
from corral.activity_board import resolve_active_marker
from corral.cache import history_signature
from corral.i18n import t
from corral.models import LaunchRequest, NewSessionRequest, is_shell_session, session_key
from corral.remote import questions, richmsg, shell_terminal, transcript_cache
from corral.remote.screen import ScreenEncoder
from corral.remote.terminal_stream import TerminalStream
from corral.runtime import LaunchError
from corral.runtime.registry import ACTIVE_RUNTIME_IDS
from corral.split_layout import default_layout_db, is_auto_group_name
from corral.store import SessionStore

_SCAN_LIMIT = 200
# Event-driven history refresh: wake early on FS change; idle reconcile matches the
# old 15s cadence so phone attention / list freshness do not regress to ~60s.
_REFRESH_RECONCILE = 15.0
_REFRESH_MIN_GAP = 15.0  # do not scan more often than the old remote cadence under thrash
_TITLE_POLL_SLICE = 15.0
# Hot-session state probe (dots, Working, Ended, hosted panes); runs every tick
# regardless of memory pressure so clients stay within ~2 s of the host.
_STATE_TICK = 1.0
# List managed tmux panes every N ticks: adopt new TUI panes, drop vanished ones.
_STATE_HOSTS_EVERY_TICKS = 2
# After a new session history file appears, follow shared-index publishes this
# long (the scan worker parses it on its next pass) with this minimum gap.
_ARRIVAL_FOLLOW_SECONDS = 20.0
_ARRIVAL_MIN_GAP = 1.0
_LAYOUT_POLL_SECONDS = 1.0
_PHONE_LIST_LIMIT = 80
_SCREEN_INTERVAL = 0.2       # 有人在看终端视图时的抓帧周期
_CONVERSATION_INTERVAL = 1.0  # 实时会话的富消息轮询周期（空闲）
_CONVERSATION_ACTIVE_INTERVAL = 0.25  # 正在处理或等回复时收紧，不改画面周期
_HOST_WIDTH = 120            # 新建托管会话的默认窗口宽度（按桌面常见宽度，手机横向平移）
_HOST_HEIGHT = 40
MESSAGE_PAGE_LIMIT = 80
MESSAGE_PAGE_LIMIT_MAX = 120
MESSAGE_PAGE_BYTES = 256 * 1024
MESSAGE_EVENT_BYTES = 64 * 1024
# session.userPrompts: chunk size while filling a transcript back to its start,
# per-prompt text cap, and the payload budget past which texts shrink further.
_PROMPT_FILL_CHUNK = 400
USER_PROMPT_TEXT_LIMIT = 500
USER_PROMPT_SHORT_TEXT_LIMIT = 160
USER_PROMPTS_BYTES = 1024 * 1024
_MAX_IN_MEMORY_TRANSCRIPTS = 48
_CONVERSATION_DELTA_LIMIT = 200  # 每条被看会话只留最近这么多增量；溢出则 replay 失败走 tail
# New session keys that first appear already terminal still notify if this fresh.
_STATUS_NOTIFY_FRESH_SECONDS = 300.0

def _scan_index_stamp() -> tuple | None:
    """Cheap change stamp of the shared scan index (stat only, no JSON parse)."""
    try:
        from corral import scan_index

        info = os.stat(scan_index.index_path())
    except Exception:
        return None
    return (info.st_mtime_ns, info.st_size)


_ATTENTION_LABELS = {"none": "none", "unread": "unread", "working": "working", "waiting": "waiting"}

# Dump/adapters (EditHere corral-cursor): wait for the TUI to accept keys before
# pasting, then pause longer than phone send_text so Enter is not lost on a
# still-starting Cursor Agent. Retry submit if the prompt is still in the composer.
_TURN_READY_TIMEOUT = 45.0
_TURN_READY_POLL = 0.25
# A freshly resumed assistant draws its banner, starts tools and only then
# accepts Enter; input pasted earlier sits in the composer unsubmitted (seen on
# Codex 2026-10-04). Wait until the pane stops changing for a quiet window.
_RESUME_SETTLE_QUIET = 1.0
_RESUME_SETTLE_TIMEOUT = 20.0
_TURN_SUBMIT_PAUSE = 0.25
_TURN_SUBMIT_RETRIES = 3
_TURN_IMAGE_GAP = 0.15
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b."
)

# 注入原因码 → 用户可见本地化文案键。原因码只做内部分类，绝不进用户 copy。
_INJECT_CAUSE_MESSAGE_KEYS = {
    "pane_gone": "remote.err.inject_cause_pane_gone",
    "tmux_busy": "remote.err.inject_cause_tmux_busy",
    "tmux_error": "remote.err.inject_cause_tmux_error",
    "tmux_unavailable": "remote.err.inject_cause_tmux_unavailable",
    "uncertain": "remote.err.inject_cause_uncertain",
}


class ActionError(RuntimeError):
    """动作无法执行；给用户看的 message 必须走 i18n.t()，随开发机界面语言。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# Long enough for a broken install or a rejected flag to exit, short enough
# that a healthy launch from the phone does not feel slower.
_STARTUP_CHECK_SECONDS = 1.0


def _raise_if_failed_at_start(name: str, message_key: str) -> None:
    """Report an assistant that died right after launch with what it printed."""
    report = embed.wait_for_startup_failure(name, timeout=_STARTUP_CHECK_SECONDS)
    if report is not None:
        raise ActionError("unavailable", t(message_key, error=embed.exit_summary(report)))


class PartialInjectionError(RuntimeError):
    """Some input reached the pane but a later step failed — outcome is ambiguous."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message or "partial injection")
        self.message = message or "partial injection"


def _phone_steer_promote(session: dict) -> bool:
    """Phone must steer mid-turn; Cursor needs a second empty Enter to promote.

    Skip only while the agent is waiting for an answer so we do not submit an
    empty follow-up right after the user's choice.
    """
    source = str(session.get("source") or "").strip().lower()
    if source != "cursor":
        return False
    attention = str(session.get("attention_kind") or "none").strip().lower()
    return attention != "waiting"


def _plain_pane_text(raw: str | None) -> str:
    if not raw:
        return ""
    return _ANSI_RE.sub("", raw)


def _pane_accepts_input(plain: str) -> bool:
    """True when the hosted assistant UI shows an input prompt."""
    if "→" not in plain and "->" not in plain:
        return False
    # Fresh Cursor / Claude panes show the arrow once the TUI is interactive.
    return True


def _composer_still_holds(plain: str, needle: str) -> bool:
    """True when ``needle`` still sits in the bottom composer after ``→``."""
    marker = (needle or "").strip().splitlines()[0].strip()
    if not marker:
        return False
    # Keep the marker short — long prompt first lines may wrap in the pane.
    marker = marker[:48]
    idx = plain.rfind("→")
    if idx < 0:
        idx = plain.rfind("->")
    if idx < 0:
        return False
    window = plain[idx : idx + 1600]
    if marker not in window:
        return False
    # Submitted turns move the prompt into history; the composer becomes the
    # follow-up placeholder while the agent runs.
    if "Add a follow-up" in window:
        return False
    return True


@dataclass
class _ScreenWatch:
    key: str
    encoder: ScreenEncoder
    scroll_offset: int = 0
    watchers: int = 0
    cols: int = 0
    rows: int = 0
    last_capture: tuple[object, ...] | None = None


class _DeltaBuffer:
    """有界增量环形缓冲。只保留最近 N 条；溢出后旧序号不可回放。

    借鉴 OpenCAN EventBuffer 的 Since / 溢出语义，但用的是 Corral 消息 seq，
    不引入独立事件序号。缓冲为空时由调用方改查规范化缓存。
    """

    def __init__(self, maxlen: int = _CONVERSATION_DELTA_LIMIT) -> None:
        cap = maxlen if maxlen > 0 else _CONVERSATION_DELTA_LIMIT
        self._items: deque[richmsg.RichMessage] = deque(maxlen=cap)

    def append(self, messages: list[richmsg.RichMessage]) -> None:
        for message in messages:
            self._items.append(message)

    def clear(self) -> None:
        self._items.clear()

    @property
    def empty(self) -> bool:
        return not self._items

    def since_or_gap(self, after_seq: int) -> list[richmsg.RichMessage] | None:
        """返回 seq > after_seq 的增量。空列表表示已追上；None 表示缺口已滚出。"""
        if not self._items:
            return None
        oldest = self._items[0].seq
        newest = self._items[-1].seq
        if after_seq >= newest:
            return []
        if after_seq + 1 < oldest:
            return None
        return [item for item in self._items if item.seq > after_seq]


@dataclass
class _ConversationWatch:
    key: str
    reader: richmsg.RichReader
    watchers: int = 0
    generation: int = 1
    deltas: _DeltaBuffer = field(default_factory=_DeltaBuffer)
    # 手机订阅通道继续用 key（可能是占位卡旧键）；canonical_key 指向转正后的会话。
    canonical_key: str = ""


@dataclass
class _Transcript:
    key: str
    path: str
    signature: tuple[int, int, int, int] | None
    generation: int
    reader: richmsg.RichReader
    messages: list[richmsg.RichMessage]


def _same_identity(
    left: tuple[int, int, int, int] | None,
    right: tuple[int, int, int, int] | None,
) -> bool:
    return left is not None and right is not None and left[:2] == right[:2]


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _wire_message_batches(messages: list[richmsg.RichMessage], *, session_key: str) -> list[dict]:
    """把实时增量切成有上限的批次，避免一次工具洪峰撑大单帧。"""
    batches: list[dict] = []
    current: list[dict] = []
    for message in messages:
        candidate = current + [message.to_wire_dict()]
        probe = {
            "version": 1,
            "kind": "delta",
            "session": session_key,
            "messages": candidate,
        }
        if current and _json_size(probe) > MESSAGE_EVENT_BYTES:
            batches.append({
                "version": 1,
                "kind": "delta",
                "session": session_key,
                "from_seq": current[0]["seq"],
                "to_seq": current[-1]["seq"],
                "messages": current,
            })
            current = [message.to_wire_dict()]
        else:
            current = candidate
    if current:
        batches.append({
            "version": 1,
            "kind": "delta",
            "session": session_key,
            "from_seq": current[0]["seq"],
            "to_seq": current[-1]["seq"],
            "messages": current,
        })
    return batches


def _phone_list_window_items(
    items: list[dict],
    *,
    is_priority,
    cap: int = _PHONE_LIST_LIMIT,
) -> list[dict]:
    """等待/执行中/置顶优先，其余按原顺序截断；优先集超过上限时全部保留。"""
    must: list[dict] = []
    rest: list[dict] = []
    for item in items:
        if is_priority(item):
            must.append(item)
        else:
            rest.append(item)
    if len(must) >= cap:
        return must
    return must + rest[: cap - len(must)]


def _session_is_priority(session: dict, layout) -> bool:
    """移动端按单会话判定：等回复/执行中/独立置顶优先；分组不参与。"""
    attention = str(session.get("attention_kind") or "none")
    if attention in ("waiting", "working"):
        return True
    if layout is None:
        return False
    key = session_key(session)
    if key in (getattr(layout, "pinned_session_keys", {}) or {}):
        return True
    # 移动端没有分组概念：哪怕该会话在桌面侧栏属于某个分屏组，也只看它自己
    # 有没有独立置顶。整组置顶是桌面侧栏的展示行为，不能把同组其它会话一起抬进
    # 移动端置顶区（否则 pin 一条会把整组都钉上去）。
    return False


def _phone_list_window(payloads: list[dict], *, cap: int = _PHONE_LIST_LIMIT) -> list[dict]:
    """手机首包只带当前页用得上的会话：等待/执行中/独立置顶优先，分组不参与。"""
    return _phone_list_window_items(
        payloads,
        is_priority=lambda payload: (
            payload.get("attention") in ("waiting", "working")
            or payload.get("pinned")
        ),
        cap=cap,
    )


def _phone_list_window_sessions(
    sessions: list[dict], layout, *, cap: int = _PHONE_LIST_LIMIT
) -> list[dict]:
    return _phone_list_window_items(
        sessions,
        is_priority=lambda session: _session_is_priority(session, layout),
        cap=cap,
    )


def _list_version_blob(rows: list) -> str:
    blob = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _message_page(
    items: list[richmsg.RichMessage],
    *,
    limit: int,
    before_seq: int | None = None,
    generation: int = 1,
    has_earlier: bool = False,
) -> dict:
    """生成受消息条数与 JSON 体积双重约束的历史窗口。"""
    bounded_limit = max(1, min(limit or MESSAGE_PAGE_LIMIT, MESSAGE_PAGE_LIMIT_MAX))
    scoped = [item for item in items if before_seq is None or item.seq < before_seq]
    has_more = len(scoped) > bounded_limit or has_earlier
    selected = scoped[-bounded_limit:]
    wire = [item.to_wire_dict() for item in selected]
    while len(wire) > 1 and _json_size({"messages": wire}) > MESSAGE_PAGE_BYTES:
        wire.pop(0)
        selected.pop(0)
        has_more = True
    oldest = wire[0]["seq"] if wire else 0
    newest = wire[-1]["seq"] if wire else 0
    return {
        "version": 1,
        "kind": "snapshot",
        "messages": wire,
        "oldest_seq": oldest,
        "newest_seq": newest,
        "from": oldest,
        "to": newest,
        "total": len(items),
        "generation": generation,
        "has_more": has_more,
    }


def _user_prompt_rows(users: list[richmsg.RichMessage]) -> list[dict]:
    """Wire rows for session.userPrompts, shrinking texts to stay within budget."""

    def rows(limit: int) -> list[dict]:
        out: list[dict] = []
        for item in users:
            text = item.text.strip()
            row: dict = {
                "seq": item.seq,
                "role": "user",
                "text": text if len(text) <= limit else text[:limit].rstrip() + "…",
            }
            if item.timestamp is not None:
                row["ts"] = item.timestamp
            out.append(row)
        return out

    wire = rows(USER_PROMPT_TEXT_LIMIT)
    if _json_size({"prompts": wire}) > USER_PROMPTS_BYTES:
        wire = rows(USER_PROMPT_SHORT_TEXT_LIMIT)
    return wire


def _continuous_after(
    items: list[richmsg.RichMessage],
    after_seq: int,
    *,
    cap: int = _CONVERSATION_DELTA_LIMIT,
) -> list[richmsg.RichMessage] | None:
    """从规范化缓存取出 after_seq 之后的连续消息。对不上或超过上限则 None。"""
    newest = items[-1].seq if items else 0
    if after_seq > newest:
        return None
    newer = [item for item in items if item.seq > after_seq]
    if not newer:
        return []
    if newer[0].seq != after_seq + 1:
        return None
    if len(newer) > cap:
        return None
    wire = [item.to_wire_dict() for item in newer]
    if _json_size({"messages": wire}) > MESSAGE_PAGE_BYTES:
        return None
    return newer


def _replay_page(
    items: list[richmsg.RichMessage],
    *,
    after_seq: int,
    generation: int,
    total: int,
) -> dict:
    """只含缺口的回包。空 messages 表示已追上，不是清空。"""
    wire = [item.to_wire_dict() for item in items]
    oldest = wire[0]["seq"] if wire else after_seq
    newest = wire[-1]["seq"] if wire else after_seq
    return {
        "version": 1,
        "kind": "snapshot",
        "messages": wire,
        "oldest_seq": oldest,
        "newest_seq": newest,
        "from": oldest,
        "to": newest,
        "total": total,
        "generation": generation,
        "has_more": False,
        "resume": "replay",
    }


def _try_replay(
    watch: _ConversationWatch,
    transcript: _Transcript,
    after_seq: int,
    client_generation: int | None,
) -> list[richmsg.RichMessage] | None:
    """generation 一致且缺口连续可补时返回消息（可空）；否则 None 让调用方走 tail。"""
    if client_generation is None or int(client_generation) != transcript.generation:
        return None
    if not watch.deltas.empty:
        return watch.deltas.since_or_gap(after_seq)
    return _continuous_after(transcript.messages, after_seq)


def default_title_spawn_fn(limit: int) -> None:
    """Same detached title daemon the TUI uses; imported lazily to avoid cycles."""
    from corral import _spawn_title_daemon

    _spawn_title_daemon(limit)


class SessionHub:
    """开发机上所有会话相关能力的唯一入口。

    ``on_event`` 会在后台线程里被调用，参数是 (通道名, 数据)。实现方必须自己
    把它转投到网络侧的事件循环，不要在里面做阻塞 I/O。
    """

    def __init__(self, on_event=None, *, scan_limit: int = _SCAN_LIMIT,
                 title_spawn_fn=None) -> None:
        self.store = SessionStore(limit=scan_limit)
        # The remote host participates in the same title state machine.  The
        # caller may inject the gateway-backed launcher; leaving it unset keeps
        # library construction side-effect free for tests and read-only users.
        self.store._title_spawn_fn = title_spawn_fn
        self.store.turn_state_listener = self._on_turn_state
        self.registry = self.store.registry
        self.layout_db = default_layout_db()
        self._on_event = on_event or (lambda channel, data: None)
        self._lock = threading.Lock()
        # 同会话重启串行锁：杀旧起新必须串行，禁止并行起新复用未死透的旧名。
        self._restart_locks: dict[str, threading.RLock] = {}
        self._screens: dict[str, _ScreenWatch] = {}
        # Desktop raw terminal streams: key -> (stream, viewer ids).
        self._terminals: dict[str, tuple[TerminalStream, set[str]]] = {}
        self._conversations: dict[str, _ConversationWatch] = {}
        self._transcripts: dict[str, _Transcript] = {}
        self._transcript_cache = transcript_cache.TranscriptCache()
        self._transcript_io = threading.Lock()
        self._sessions_watchers = 0
        self._layout_watchers = 0
        self._layout_thread: threading.Thread | None = None
        self._layout_revision_seen: int | None = None
        self._stop = threading.Event()
        self._search_refresh = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_attention: dict[str, str] = {}
        self._attention_hook = None  # 由推送层注入：(session, 旧状态, 新状态)
        self._media = None  # media.PreviewService, created on first image request
        self._last_live: dict[str, bool] = {}
        self._last_markers: dict[str, str] = {}
        self._last_status: dict[str, str] = {}
        self._last_completion: dict[str, str] = {}
        self._status_hook = None  # 推送层：SessKit status_tag 已完成/已中断
        self._history_watcher = None
        # Set once the first scan is loaded. Library users that never call
        # `start()` are ready immediately; the daemon clears it before it opens
        # its transports so connections are accepted while the scan runs.
        self._ready = threading.Event()
        self._ready.set()

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        from corral.history_watch import HistoryWatcher
        from corral.schedprio import demote_background

        demote_background()
        watcher = HistoryWatcher()
        self._history_watcher = watcher
        watcher.start()
        try:
            self.store.load()
            self._snapshot_attention()
            self._snapshot_live()
            self._snapshot_status()
        finally:
            self._ready.set()
        for target in (self._refresh_loop, self._screen_loop, self._conversation_loop, self._search_loop):
            thread = threading.Thread(target=target, daemon=True, name=f"remote-{target.__name__}")
            thread.start()
            self._threads.append(thread)

    def mark_starting(self) -> None:
        """Requests needing session data wait until `start()` finishes its scan."""
        self._ready.clear()

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        self._search_refresh.set()
        with self._lock:
            terminals = [stream for stream, _ in self._terminals.values()]
            self._terminals.clear()
        for stream in terminals:
            stream.stop()
        watcher = self._history_watcher
        if watcher is not None:
            watcher.stop()
            self._history_watcher = None
        for thread in self._threads:
            thread.join(timeout=1.0)
        embed.close_channel()
        self._transcript_cache.close()

    def set_attention_hook(self, hook) -> None:
        """注册关注状态变化回调，供推送层订阅。"""
        self._attention_hook = hook

    def set_status_hook(self, hook) -> None:
        """注册 SessKit status_tag 变化回调（已完成 / 已中断 → 系统通知）。"""
        self._status_hook = hook

    # -- 后台循环 ---------------------------------------------------------

    def _search_index(self):
        from corral.search import ConversationIndex

        with self._lock:
            index = getattr(self, "_fulltext_index", None)
            if index is None:
                index = ConversationIndex()
                self._fulltext_index = index
            return index

    def _search_sessions(self) -> list[dict]:
        return [session for session in self.store.all_sessions()
                if str(session.get("source") or "") in ACTIVE_RUNTIME_IDS]

    def _search_loop(self) -> None:
        """Own parsing outside RPC threads; publish only complete index snapshots."""
        from corral import observe
        from corral.schedprio import demote_background

        demote_background()
        index = self._search_index()
        while not self._stop.is_set():
            self._search_refresh.clear()
            try:
                index.refresh(self.store, self._search_sessions())
            except Exception as exc:
                observe.event("search_index_refresh_failed", error=str(exc))
            # Also validate file/WAL signatures between catalog scans so fresh
            # messages in a known session do not wait for the next full scan.
            self._search_refresh.wait(2.0)

    def _refresh_loop(self) -> None:
        """One thread owns list state: a 1 s state probe plus the throttled full scan.

        Dots, Working, Ended and newly hosted panes come from
        ``store.refresh_state`` every tick, independent of memory pressure (owner
        budget: about 2 s to every client). Only the full history scan keeps the
        FS-event / reconcile / min-gap cadence that backs off under pressure.
        A new session history file is the exception: follow the shared index
        for a short window so the arrival lists as soon as the worker publishes.
        """
        from corral.history_watch import memory_pressured, pressure_cadence
        from corral.schedprio import demote_background

        demote_background()
        watcher = self._history_watcher
        last_scan = time.monotonic()  # start() just loaded
        last_title_poll = last_scan
        arrival_seen = self._watcher_arrivals(watcher)
        arrival_until = 0.0
        index_stamp = _scan_index_stamp()
        tick = 0
        while not self._stop.is_set():
            # Fixed tick: FS events are read via ``watcher.is_set()`` below, so a
            # write burst cannot spin this loop faster than once per second.
            if self._stop.wait(_STATE_TICK):
                return
            now = time.monotonic()
            tick += 1
            reconcile, min_gap = pressure_cadence(
                _REFRESH_RECONCILE, _REFRESH_MIN_GAP, memory_pressured(),
            )
            if watcher is None:
                reconcile = min_gap = _TITLE_POLL_SLICE
            arrivals = self._watcher_arrivals(watcher)
            if arrivals != arrival_seen:
                arrival_seen = arrivals
                arrival_until = now + _ARRIVAL_FOLLOW_SECONDS
            since_scan = now - last_scan
            full = since_scan >= reconcile or (
                watcher is not None and watcher.is_set() and since_scan >= min_gap
            )
            if not full and now < arrival_until and since_scan >= _ARRIVAL_MIN_GAP:
                stamp = _scan_index_stamp()
                full = stamp != index_stamp
            title_keys: set[str] = set()
            if now - last_title_poll >= _TITLE_POLL_SLICE and not full:
                # Title updates are independent of history; ~0.2 s per poll here,
                # so keep the old 15 s slice instead of the state tick.
                title_keys = self.store.poll_title_updates()
                last_title_poll = now
            changed = False
            if full:
                if watcher is not None:
                    watcher.clear()
                # Stamp before scanning: a publish that lands during this (multi-
                # second) refresh must still trigger the next arrival-follow scan.
                index_stamp = _scan_index_stamp()
                try:
                    changed = self.store.refresh()
                except Exception:
                    # History scan failures must not block state or title propagation.
                    pass
                last_scan = time.monotonic()
                self._search_refresh.set()
                self._reclaim_inactive_hosts()
                title_keys.update(self.store.poll_title_updates())
                last_title_poll = time.monotonic()
            else:
                try:
                    changed = self.store.refresh_state(
                        list_hosts=tick % _STATE_HOSTS_EVERY_TICKS == 0,
                    )
                except Exception:
                    changed = False
            self._follow_key_migrations()
            self._detect_attention_changes()
            self._detect_live_changes()
            self._detect_status_changes()
            markers_changed = self._detect_marker_changes()
            if (changed or title_keys or markers_changed) and self._sessions_watchers:
                self._on_event("sessions", self.list_snapshot())
            if title_keys:
                self._emit_title_events(title_keys)

    def _on_turn_state(self) -> None:
        """A hot session's turn ended or restarted: notify and publish now.

        Runs on the refresh thread from inside the store probe, before the rest
        of the tick; the later per-tick detectors see no further change.
        """
        if not self._ready.is_set():
            return
        self._detect_status_changes()
        self._detect_marker_changes()
        if self._sessions_watchers:
            self._on_event("sessions", self.list_snapshot())

    @staticmethod
    def _watcher_arrivals(watcher) -> int:
        try:
            return int(getattr(watcher, "arrival_seq", 0) or 0)
        except Exception:
            return 0

    def _detect_marker_changes(self) -> bool:
        """True when any row's TUI marker flipped since the last pass.

        A ``recent`` mark expires with time alone, without any history change,
        so the list push cannot rely on ``store.refresh()`` reporting a change.
        """
        sessions = self.store.all_sessions()
        current = {
            session_key(session): resolve_active_marker(session) or ""
            for session in sessions
        }
        changed_keys = {
            key for key, marker in current.items()
            if self._last_markers.get(key) != marker
        }
        changed = current != self._last_markers
        self._last_markers = current
        # Details can unsubscribe from the list. Their footer also follows the
        # marker, including recent expiry and interruption with unchanged live/attention.
        with self._lock:
            watches = [watch for watch in self._conversations.values() if watch.watchers > 0]
        if changed_keys and watches:
            layout = self._layout()
            for session in sessions:
                key = session_key(session)
                if key not in changed_keys:
                    continue
                summary = self.session_payload(session, layout)
                for watch in watches:
                    if watch.key != key and watch.canonical_key != key:
                        continue
                    self._on_event(f"session:{watch.key}", {
                        "version": 1, "kind": "metadata", "session": watch.key,
                        "summary": summary,
                    })
        return changed

    def _reclaim_inactive_hosts(self) -> None:
        """Silent reclaim tick on the refresh thread; the daemon may run with no TUI open.

        `reclaim.maybe_reclaim` owns the machine-wide throttle, the protections and
        the audit event. Failures must never disturb the refresh loop.
        """
        try:
            from corral import reclaim

            if not self.store.loaded or not reclaim.enabled():
                return
            reclaim.maybe_reclaim(lambda: [dict(s) for s in self.store.all_sessions()])
        except Exception:  # noqa: BLE001 — best effort
            pass

    def _emit_title_events(self, title_keys: set[str]) -> None:
        with self._lock:
            watches = [
                w for w in self._conversations.values()
                if w.watchers > 0
                and (w.canonical_key or w.key) in title_keys
            ]
        for watch in watches:
            session = self.store.find_session(watch.canonical_key or watch.key)
            if session is None:
                continue
            self._on_event(
                f"session:{watch.key}",
                {
                    "version": 1,
                    "kind": "metadata",
                    "session": watch.key,
                    "revision": self.store.title_revision,
                    "summary": self.session_payload(session, self._layout()),
                },
            )

    def _screen_loop(self) -> None:
        while not self._stop.wait(_SCREEN_INTERVAL):
            with self._lock:
                watches = [w for w in self._screens.values() if w.watchers > 0]
            for watch in watches:
                try:
                    self._pump_screen(watch)
                except Exception:
                    continue

    def _conversation_poll_interval(self) -> float:
        """被看会话正在处理或等回复时加快对话轮询；全空闲回到 1 秒。不改画面周期。"""
        with self._lock:
            keys = [
                watch.canonical_key or watch.key
                for watch in self._conversations.values()
                if watch.watchers > 0
            ]
        for key in keys:
            session = self.store.find_session(key)
            if session is None:
                continue
            kind = _ATTENTION_LABELS.get(str(session.get("attention_kind") or "none"), "none")
            if kind in ("working", "waiting"):
                return _CONVERSATION_ACTIVE_INTERVAL
        return _CONVERSATION_INTERVAL

    def _conversation_loop(self) -> None:
        while not self._stop.wait(self._conversation_poll_interval()):
            with self._lock:
                watches = [w for w in self._conversations.values() if w.watchers > 0]
            seen_readers: set[int] = set()
            for watch in watches:
                reader_id = id(watch.reader)
                if reader_id in seen_readers:
                    continue
                seen_readers.add(reader_id)
                new_messages: list[richmsg.RichMessage] = []
                try:
                    with self._transcript_io:
                        new_messages = watch.reader.poll()
                except Exception:
                    continue
                if new_messages:
                    self._publish_new_messages(
                        watch.canonical_key or watch.key, new_messages
                    )

    # -- 会话查询 ---------------------------------------------------------

    def _layout(self):
        # 移动端没有分组概念：读布局必须跳过成员独立钉到整组置顶的提升，
        # 否则 pin 一条会把同组其它会话一起抬进手机置顶区。桌面侧栏仍走
        # 默认 read()（组可见时整组展示）。
        try:
            reader = getattr(self.layout_db, "read_with_promote", None)
            if callable(reader):
                return reader(skip_promote=True)
            return self.layout_db.read()
        except Exception:
            return None

    @staticmethod
    def _wire_str(value: object) -> str | None:
        """手机端把若干字段按 String 解码；类型不符会让整份列表解码失败而空白。"""
        if value is None:
            return None
        text = str(value)
        return text

    @staticmethod
    def _wire_float(value: object, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def session_payload(self, session: dict, layout=None) -> dict:
        key = session_key(session)
        title = self.store.get_title(session)
        attention = str(session.get("attention_kind") or "none")
        runtime = self._wire_str(session.get("source"))
        payload = {
            "key": key,
            "runtime": runtime,
            "id": self._wire_str(session.get("id")),
            "short_id": self._wire_str(session.get("short_id")),
            "title": str(title or ""),
            "cwd": str(session.get("cwd") or ""),
            "cwd_display": str(session.get("cwd_display") or ""),
            "mtime": self._wire_float(session.get("mtime")),
            "time": str(session.get("display_time") or ""),
            "size_kb": round(self._wire_float(session.get("size_kb")), 1),
            "status": str(session.get("status_tag") or ""),
            "live": bool(session.get("live")),
            "hosted": bool(session.get("keepalive_name")),
            "attention": _ATTENTION_LABELS.get(attention, "none"),
            # Same dot as the TUI sidebar / Active sessions; the phone must not
            # derive green from ``live``.
            "marker": resolve_active_marker(session) or "",
            "last_user": str(session.get("last_user_msg") or "")[:160],
            "last_agent": str(session.get("last_agent_msg") or "")[:160],
            # 完成通知去重与事后核对用：只读透传，不进排序/筛选/版本指纹。
            "completion_id": str(session.get("completion_id") or ""),
            "file_mtime": self._wire_float(session.get("file_mtime") or session.get("mtime")),
            "size_bytes": int(session.get("size_bytes") or 0),
            "rich": richmsg.supports_tool_calls(str(session.get("source") or "")),
            # 布局读失败时也给出稳定布尔，避免手机端 optional 与「未置顶」语义漂移
            "pinned": False,
        }
        if layout is not None:
            # 移动端没有分组概念：只发独立会话置顶，不发 group 字段。桌面侧栏的
            # 分屏组（含整组置顶）只影响桌面展示，不得把同组其它会话一起抬进
            # 移动端置顶区（否则 pin 一条会把整组都钉上去）。
            pinned_sessions = getattr(layout, "pinned_session_keys", {}) or {}
            payload["pinned"] = key in pinned_sessions
        return payload

    def _session_matches(self, session: dict, layout, needle: str) -> bool:
        title = str(self.store.get_title(session) or "")
        cwd = str(session.get("cwd_display") or session.get("cwd") or "")
        last_user = str(session.get("last_user_msg") or "")
        last_agent = str(session.get("last_agent_msg") or "")
        haystack = f"{title}\n{cwd}\n{last_user}\n{last_agent}".lower()
        return needle in haystack

    def _window_version(self, sessions: list[dict], layout) -> str:
        rows = []
        for session in sessions:
            key = session_key(session)
            # 移动端版本指纹只看独立会话置顶：桌面整组置顶变化不得让手机列表
            # 版本跳动（否则组一动手机就整表重拉）。
            pinned = False
            if layout is not None:
                pinned = key in (getattr(layout, "pinned_session_keys", {}) or {})
            rows.append(
                [
                    key,
                    str(session.get("attention_kind") or "none"),
                    str(self.store.get_title(session) or ""),
                    round(self._wire_float(session.get("mtime")), 3),
                    str(session.get("last_user_msg") or "")[:160],
                    str(session.get("last_agent_msg") or "")[:160],
                    bool(session.get("live")),
                    pinned,
                    resolve_active_marker(session) or "",
                ]
            )
        return _list_version_blob(rows)

    def _listed_payloads(
        self, query: str = "", limit: int = 0, layout=None
    ) -> tuple[list[dict], int, bool]:
        layout = self._layout() if layout is None else layout
        sessions = self.store.all_sessions()
        total = len(sessions)
        if query:
            needle = query.strip().lower()
            matched = [item for item in sessions if self._session_matches(item, layout, needle)]
            has_more = limit > 0 and len(matched) > limit
            chosen = matched[:limit] if limit > 0 else matched
            payloads = [self.session_payload(item, layout) for item in chosen]
            return payloads, len(matched), has_more
        if limit > 0:
            chosen = sessions[:limit]
            payloads = [self.session_payload(item, layout) for item in chosen]
            return payloads, total, total > limit
        windowed = _phone_list_window_sessions(sessions, layout)
        payloads = [self.session_payload(item, layout) for item in windowed]
        return payloads, total, total > len(windowed)

    def list_sessions(self, query: str = "", limit: int = 0) -> list[dict]:
        payloads, _, _ = self._listed_payloads(query=query, limit=limit)
        return payloads

    def list_snapshot(
        self, query: str = "", limit: int = 0, since_version: str = ""
    ) -> dict:
        """给手机的列表回包：默认窗口带版本号；版本未变不带会话数组。"""
        layout = self._layout()
        if query or limit > 0:
            payloads, total, has_more = self._listed_payloads(
                query=query, limit=limit, layout=layout
            )
            return {
                "version": _list_version_blob(
                    [item.get("key") for item in payloads]
                ),
                "revision": self.store.title_revision,
                "unchanged": False,
                "has_more": has_more,
                "total": total,
                "sessions": payloads,
            }
        sessions = self.store.all_sessions()
        windowed = _phone_list_window_sessions(sessions, layout)
        total = len(sessions)
        has_more = total > len(windowed)
        version = self._window_version(windowed, layout)
        if since_version and since_version == version:
            return {
                "version": version,
                "revision": self.store.title_revision,
                "unchanged": True,
                "has_more": has_more,
                "total": total,
            }
        payloads = [self.session_payload(item, layout) for item in windowed]
        return {
            "version": version,
            "revision": self.store.title_revision,
            "unchanged": False,
            "has_more": has_more,
            "total": total,
            "sessions": payloads,
        }

    def resolve_session_key(self, key: str) -> str:
        """手机可能还拿着占位卡旧键；助手落下真实历史后换成正式键，旧键仍须能用。

        电脑侧栏会跟着迁编号，远程详情页不会。禁止把旧键当成「已经不在列表里」。

        守护进程重启会丢掉内存里的占位→正式迁移表：重启后旧临时键经精确
        ``keepalive_name``（``corral-<runtime>-<ident>`` 等全部前后缀）反查到
        已 ``annotate`` 贴名的正式会话；原生 id 前缀/完整两种形态按同运行时
        精确一对一认领。命中零条或多条时仍返回原键（上游报 ``not_found``），
        禁止用 cwd/标题/任意旧卡兜底。
        """
        current = str(key or "")
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            if self.store.find_session(current) is not None:
                return current
            migrated = (self.store.session_key_migrations() or {}).get(current)
            if not migrated or migrated == current:
                break
            current = str(migrated)
        aliased = self._resolve_restart_alias(str(key or ""))
        if aliased is not None:
            return aliased
        return str(key or "")

    def _resolve_restart_alias(self, key: str) -> str | None:
        """重启后旧键的精确别名：只认托管名与原生 id，不认 cwd/标题。"""
        runtime, sep, sid = str(key or "").partition(":")
        if not sep or not runtime or not sid:
            return None
        try:
            sessions = self.store.all_sessions()
        except Exception:
            return None
        from corral.legacy_names import ALL_SESSION_PREFIXES

        candidate_names = {f"{prefix}{runtime}-{sid}" for prefix in ALL_SESSION_PREFIXES}
        keepalive_hits: list[str] = []
        for session in sessions:
            try:
                name = str(session.get("keepalive_name") or "")
            except Exception:
                continue
            if name and name in candidate_names:
                try:
                    from corral.models import session_key as _session_key

                    keepalive_hits.append(_session_key(session))
                except Exception:
                    continue
        if len(keepalive_hits) == 1:
            return keepalive_hits[0]
        if keepalive_hits:
            return None
        # 原生 id 前缀/完整形态：同运行时下精确一对一才认领，避免串到相邻会话。
        normalized = sid.replace("-", "").lower()
        prefix_hits: list[str] = []
        for session in sessions:
            try:
                if str(session.get("source") or "") != runtime:
                    continue
                native_id = str(session.get("id") or "")
                short_id = str(session.get("short_id") or "")
            except Exception:
                continue
            if not native_id:
                continue
            if native_id == sid or short_id == sid:
                try:
                    from corral.models import session_key as _session_key

                    prefix_hits.append(_session_key(session))
                except Exception:
                    continue
            elif normalized and native_id.replace("-", "").lower().startswith(normalized):
                try:
                    from corral.models import session_key as _session_key

                    prefix_hits.append(_session_key(session))
                except Exception:
                    continue
        if len(prefix_hits) == 1:
            return prefix_hits[0]
        return None

    def require_session(self, key: str) -> dict:
        session = self.store.find_session(self.resolve_session_key(key))
        if session is None:
            raise ActionError("not_found", t("remote.err.session_gone"))
        return session

    def _follow_key_migrations(self) -> None:
        """占位卡转正后，已打开的对话订阅改读正式历史，事件仍发到手机原来的通道。"""
        migrations = self.store.session_key_migrations() or {}
        if not migrations:
            return
        with self._lock:
            watches = [
                watch
                for watch in self._conversations.values()
                if watch.watchers > 0
            ]
        for watch in watches:
            new_key = migrations.get(watch.key) or migrations.get(watch.canonical_key)
            if not new_key or new_key == (watch.canonical_key or watch.key):
                continue
            session = self.store.find_session(new_key)
            if session is None:
                continue
            try:
                transcript = self._ensure_transcript(session)
            except Exception:
                continue
            with self._lock:
                current = self._conversations.get(watch.key)
                if current is None:
                    continue
                current.reader = transcript.reader
                current.generation = transcript.generation
                current.canonical_key = new_key

    def _remember_transcript(self, transcript: _Transcript) -> None:
        with self._lock:
            self._transcripts[transcript.key] = transcript
            watched: set[str] = set()
            for item in self._conversations.values():
                if item.watchers > 0:
                    watched.add(item.key)
                    if item.canonical_key:
                        watched.add(item.canonical_key)
            if len(self._transcripts) > _MAX_IN_MEMORY_TRANSCRIPTS:
                for cached_key in list(self._transcripts):
                    if len(self._transcripts) <= _MAX_IN_MEMORY_TRANSCRIPTS:
                        break
                    if cached_key not in watched:
                        self._transcripts.pop(cached_key, None)

    def _persist_transcript(self, transcript: _Transcript) -> None:
        runtime = str(transcript.reader.session.get("source") or "")
        try:
            self._transcript_cache.put(
                runtime,
                transcript.key,
                transcript.path,
                transcript.messages,
                transcript.reader.export_state(),
                transcript.generation,
            )
        except Exception:
            return

    def _append_transcript(self, key: str, new_messages: list[richmsg.RichMessage]) -> None:
        with self._lock:
            current = self._transcripts.get(key)
            if current is None or not new_messages:
                return
            by_seq = {item.seq: index for index, item in enumerate(current.messages)}
            for item in new_messages:
                index = by_seq.get(item.seq)
                if index is None:
                    by_seq[item.seq] = len(current.messages)
                    current.messages.append(item)
                else:
                    # Same seq with updated tool status (Claude/Pi result fill-in).
                    current.messages[index] = item
            current.signature = history_signature(current.path) if current.path else None
            snapshot = current
        self._persist_transcript(snapshot)

    def _publish_new_messages(
        self, transcript_key: str, new_messages: list[richmsg.RichMessage]
    ) -> None:
        """规范化游标刚读到的新消息：写入缓存，并推给所有正在看这条会话的手机通道。

        占位卡转正后 transcript 挂在正式键上，事件仍发到手机原来的 ``session:{watch.key}``。
        ``read_all`` / ``read_earlier`` 是窗口构建，不要走这里。
        """
        if not new_messages:
            return
        self._append_transcript(transcript_key, new_messages)
        with self._lock:
            targets = [
                watch
                for watch in self._conversations.values()
                if watch.watchers > 0
                and (
                    watch.key == transcript_key
                    or watch.canonical_key == transcript_key
                )
            ]
            for watch in targets:
                watch.deltas.append(new_messages)
        for watch in targets:
            for event in _wire_message_batches(new_messages, session_key=watch.key):
                self._on_event(f"session:{watch.key}", event)

    def _ensure_transcript(self, session: dict) -> _Transcript:
        """返回当前会话的规范化消息缓存；文件没变就不重新解析。"""
        key = session_key(session)
        path = str(session.get("path") or "")
        signature = history_signature(path) if path else None
        with self._lock:
            current = self._transcripts.get(key)
            if current is not None and current.path == path and current.signature == signature:
                return current
        with self._transcript_io:
            return self._load_transcript(session, key, path, signature)

    def _load_transcript(
        self,
        session: dict,
        key: str,
        path: str,
        signature: tuple[int, int, int, int] | None,
    ) -> _Transcript:
        with self._lock:
            current = self._transcripts.get(key)
            if current is not None and current.path == path and current.signature == signature:
                return current
            incremental = (
                current is not None
                and current.path == path
                and _same_identity(current.signature, signature)
                and current.signature is not None
                and signature is not None
                and signature[2] >= current.signature[2]
            )
            reader = current.reader if current is not None else None
            messages = current.messages if current is not None else None
            generation = current.generation if current is not None else 1
        if incremental and reader is not None and messages is not None and current is not None:
            new_messages = reader.poll()
            if new_messages:
                self._publish_new_messages(key, new_messages)
            with self._lock:
                stored = self._transcripts.get(key)
                if stored is not None and stored.reader is reader:
                    stored.signature = signature
                    to_persist = stored if not new_messages else None
                else:
                    to_persist = None
                    stored = current
            if to_persist is not None:
                self._persist_transcript(to_persist)
            return stored

        runtime = str(session.get("source") or "")
        cached = self._transcript_cache.get(runtime, key, path) if path else None
        if cached is not None:
            messages, state, generation = cached
            reader = richmsg.RichReader(session)
            reader.restore_state(state, messages)
            transcript = _Transcript(key, path, signature, generation, reader, messages)
            self._remember_transcript(transcript)
            new_messages = reader.poll()
            if new_messages:
                self._publish_new_messages(key, new_messages)
            else:
                self._persist_transcript(transcript)
            return transcript

        reader = richmsg.RichReader(session)
        messages = reader.read_all(limit=MESSAGE_PAGE_LIMIT)
        rebuilt = current is not None and not incremental
        generation = generation + 1 if rebuilt else 1
        transcript = _Transcript(key, path, signature, generation, reader, messages)
        self._remember_transcript(transcript)
        self._persist_transcript(transcript)
        return transcript

    def session_detail(self, key: str) -> dict:
        session = self.require_session(key)
        payload = self.session_payload(session, self._layout())
        runtime = self._runtime_of(session)
        payload["runtime_name"] = getattr(runtime, "display_name", "")
        payload["resumable"] = self._resumable(runtime, session)
        payload["history_path"] = session.get("path") or ""
        return payload

    def messages(
        self,
        key: str,
        limit: int = MESSAGE_PAGE_LIMIT,
        before_seq: int | None = None,
    ) -> list[dict]:
        """兼容旧调用方，返回受限历史窗口，不把整份会话搬上网络。"""
        return self.message_page(key, limit=limit, before_seq=before_seq)["messages"]

    def message_page(
        self,
        key: str,
        *,
        limit: int = MESSAGE_PAGE_LIMIT,
        before_seq: int | None = None,
    ) -> dict:
        """返回带游标元数据的有限历史窗口，不重新解析整份历史。"""
        session = self.require_session(key)
        transcript = self._ensure_transcript(session)
        if before_seq is not None:
            with self._transcript_io:
                self._fill_earlier(transcript, before_seq, limit)
        return _message_page(
            transcript.messages,
            limit=limit,
            before_seq=before_seq,
            generation=transcript.generation,
            has_earlier=transcript.reader.has_earlier(),
        )

    def _fill_earlier(
        self,
        transcript: _Transcript,
        before_seq: int,
        limit: int,
    ) -> None:
        """内存里没有更早消息时，从窗口左缘再向前读一块并 prepend。"""
        scoped = [item for item in transcript.messages if item.seq < before_seq]
        while len(scoped) < max(1, limit) and transcript.reader.has_earlier():
            older_than = min((item.seq for item in transcript.messages), default=before_seq)
            try:
                earlier = transcript.reader.read_earlier(limit, before_seq=older_than)
            except Exception:
                break
            if not earlier:
                break
            transcript.messages = earlier + transcript.messages
            scoped = [item for item in transcript.messages if item.seq < before_seq]
            self._persist_transcript(transcript)

    def tool_detail(
        self,
        key: str,
        *,
        seq: int,
        tool_id: str | None = None,
        offset: int = 0,
        limit: int = 32,
    ) -> dict:
        """On-demand tool bodies for one history message (not first-paint path)."""
        session = self.require_session(key)
        transcript = self._ensure_transcript(session)
        target = next((item for item in transcript.messages if item.seq == seq), None)
        if target is None:
            # Message may sit earlier than the current window; fill toward seq.
            with self._transcript_io:
                self._fill_earlier(transcript, seq + 1, max(1, limit))
            target = next((item for item in transcript.messages if item.seq == seq), None)
        if target is None:
            return {
                "seq": seq,
                "tools": [],
                "offset": 0,
                "has_more": False,
                "total": 0,
                "unavailable": "message_not_loaded",
            }
        return target.tool_detail_page(tool_id=tool_id, offset=offset, limit=limit)

    def image_preview(
        self,
        key: str,
        *,
        seq: int,
        ref: str,
        max_px: int,
        quality: int,
    ) -> dict:
        """Downscaled preview of an image that message ``seq`` literally refers to.

        The containment check keeps ``media.image`` from becoming a general file
        reader: only references present in that message's text are resolved.
        """
        from corral.remote import media

        session = self.require_session(key)
        reference = media.normalize_ref(ref)
        transcript = self._ensure_transcript(session)
        target = next((item for item in transcript.messages if item.seq == seq), None)
        if target is None:
            with self._transcript_io:
                self._fill_earlier(transcript, seq + 1, 1)
            target = next((item for item in transcript.messages if item.seq == seq), None)
        if target is None or not reference or reference not in (target.text or ""):
            raise ActionError("not_found", t("remote.err.image_not_found"))
        if self._media is None:
            self._media = media.PreviewService()
        try:
            preview = self._media.preview(
                reference,
                cwd=str(session.get("cwd") or ""),
                max_px=max_px,
                quality=quality,
            )
        except media.MediaError as exc:
            if exc.code == "not_found":
                raise ActionError("not_found", t("remote.err.image_not_found")) from exc
            raise ActionError("unavailable", t("remote.err.image_unavailable")) from exc
        return preview.to_wire()

    def user_prompts(self, key: str) -> dict:
        """Every human prompt of the session, oldest first (clients' Your prompts).

        Clients page history in windows, but Your prompts lists the whole session,
        so the transcript is filled back to its start here. The I/O lock is taken
        per chunk so other sessions keep opening meanwhile, and the cache is
        written once at the end. Seqs share the ``session.messages`` space, which
        lets a client page earlier up to a prompt before jumping to it.
        """
        from corral.ui.session_hud import is_injected_user_prompt

        session = self.require_session(key)
        transcript = self._ensure_transcript(session)
        filled = False
        while transcript.reader.has_earlier():
            with self._transcript_io:
                oldest = min((item.seq for item in transcript.messages), default=1)
                try:
                    earlier = transcript.reader.read_earlier(
                        _PROMPT_FILL_CHUNK, before_seq=oldest
                    )
                except Exception:
                    break
                if not earlier:
                    break
                with self._lock:
                    transcript.messages = earlier + transcript.messages
                filled = True
        if filled:
            self._persist_transcript(transcript)
        with self._lock:
            users = [
                item
                for item in transcript.messages
                if item.role == "user"
                and (item.text or "").strip()
                and not is_injected_user_prompt(item.text)
            ]
        return {
            "prompts": _user_prompt_rows(users),
            "generation": transcript.generation,
            "total": len(users),
        }

    def prompts(self, key: str) -> list[dict]:
        """当前仍待回答的提问型工具调用（含可点选项列表）。"""
        session = self.require_session(key)
        if str(session.get("source") or "") == "opencode":
            return questions.pending_prompts(session, [])
        transcript = self._ensure_transcript(session)
        return questions.pending_prompts(session, transcript.messages)

    def answer_question(self, key: str, request_id: str, answers: object) -> dict:
        """Finish the pending native question; never falls back to a chat turn."""
        session = self.require_session(key)
        return questions.answer(
            session,
            self.prompts(key),
            request_id,
            answers,
            pane_name=lambda: self._keepalive_name(key),
        )

    def projects(self) -> list[dict]:
        # path/name 与 iOS NewSessionSheet 对齐；cwd/label 保留给桌面侧同一套项目列表语义。
        return [
            {
                "cwd": entry.get("cwd_key") or "",
                "path": entry.get("cwd_key") or "",
                "label": str(entry.get("label") or ""),
                "name": str(entry.get("label") or ""),
                "count": entry.get("count") or 0,
                "mtime": entry.get("latest_mtime") or 0.0,
            }
            for entry in self.store.projects()
        ]

    def runtimes(self) -> list[dict]:
        result = []
        for runtime_id in self.registry.ids:
            runtime = self.registry.get(runtime_id)
            result.append({
                "id": runtime_id,
                "name": runtime.display_name,
                "available": bool(runtime.is_available()),
            })
        return result

    def _runtime_of(self, session: dict):
        try:
            return self.registry.get(str(session.get("source") or ""))
        except Exception as exc:
            raise ActionError("unavailable", t("remote.err.assistant_unavailable")) from exc

    @staticmethod
    def _resumable(runtime, session: dict) -> bool:
        try:
            runtime.build_resume_plan(session)
        except Exception:
            return False
        return True

    # -- 订阅 -------------------------------------------------------------

    def watch_sessions(self) -> None:
        with self._lock:
            self._sessions_watchers += 1

    def unwatch_sessions(self) -> None:
        with self._lock:
            self._sessions_watchers = max(0, self._sessions_watchers - 1)

    # -- 桌面布局（与 TUI 共用的分屏组 / 置顶）-------------------------------

    def watch_layout(self) -> None:
        """Count a desktop layout watcher; start the revision poller on first use.

        The TUI writes the layout store directly, so changes made there never
        reach the history watcher. A one-second revision read (an indexed meta
        row) is cheap and only runs while a desktop client watches.
        """
        with self._lock:
            self._layout_watchers += 1
            if self._layout_thread is None or not self._layout_thread.is_alive():
                self._layout_thread = threading.Thread(
                    target=self._layout_loop, name="corral-layout-watch", daemon=True
                )
                self._layout_thread.start()

    def unwatch_layout(self) -> None:
        with self._lock:
            self._layout_watchers = max(0, self._layout_watchers - 1)

    def _layout_loop(self) -> None:
        while not self._stop.wait(_LAYOUT_POLL_SECONDS):
            with self._lock:
                if self._layout_watchers <= 0:
                    self._layout_thread = None
                    return
            self._emit_layout_if_changed()

    def _emit_layout_if_changed(self, *, force: bool = False) -> None:
        try:
            revision = int(self.layout_db.read_revision())
        except Exception:
            return
        if not force and revision == self._layout_revision_seen:
            return
        self._layout_revision_seen = revision
        self._on_event("layout", self.layout_snapshot())

    def layout_snapshot(self) -> dict:
        """Desktop projection of the TUI sidebar memory (groups and pins).

        Uses the TUI's default read (member pins promote the group), exactly as
        the desktop sidebar renders it. Never part of the phone payload.
        """
        try:
            layout = self.layout_db.read()
        except Exception:
            layout = None
        if layout is None:
            return {"revision": 0, "groups": [], "pinned_sessions": {}}
        pinned_groups = dict(getattr(layout, "pinned_group_ids", {}) or {})
        groups = []
        for group in layout.ordered_groups():
            groups.append(
                {
                    "id": group.group_id,
                    "name": group.name,
                    # False: `name` is the hidden internal identity; clients join
                    # member titles instead (TERMINAL_UI_KNOWLEDGE_BASE split names).
                    "named": not is_auto_group_name(group.name),
                    "project": group.project_cwd,
                    "members": list(group.session_keys),
                    "focus": group.focus_key or "",
                    "collapsed": bool(group.collapsed),
                    "pinned": group.group_id in pinned_groups,
                    "pinned_at": self._wire_float(pinned_groups.get(group.group_id)),
                    "updated_at": self._wire_float(group.updated_at),
                }
            )
        return {
            "revision": int(getattr(layout, "revision", 0) or 0),
            "groups": groups,
            "pinned_sessions": {
                key: self._wire_float(at)
                for key, at in (getattr(layout, "pinned_session_keys", {}) or {}).items()
            },
        }

    def _layout_mutated(self) -> dict:
        snapshot = self.layout_snapshot()
        self._layout_revision_seen = snapshot["revision"]
        self._on_event("layout", snapshot)
        return snapshot

    def layout_set_group(self, project: str, keys: list[str], focus: str | None) -> dict:
        canonical = [self.resolve_session_key(key) for key in keys if key]
        if len(canonical) < 2:
            raise ActionError("usage_error", t("remote.err.layout_group_size"))
        focus_key = self.resolve_session_key(focus) if focus else None
        self.layout_db.set_group(project or "", canonical, focus_key=focus_key)
        return self._layout_mutated()

    def layout_remove_session(self, key: str) -> dict:
        self.layout_db.remove_session(self.resolve_session_key(key))
        return self._layout_mutated()

    def layout_set_focus(self, project: str, key: str) -> dict:
        self.layout_db.set_focus(project or "", self.resolve_session_key(key))
        return self._layout_mutated()

    def layout_toggle_pin(self, key: str) -> dict:
        """TUI pin semantics: a visible group member pins or unpins its whole group."""
        canonical = self.resolve_session_key(key)
        layout = self.layout_db.read()
        group = layout.get_group(canonical) if layout is not None else None
        if group is not None:
            self.layout_db.toggle_group_pin(group.group_id)
        else:
            self.layout_db.toggle_session_pin(canonical)
        return self._layout_mutated()

    def layout_toggle_group_pin(self, group_id: str) -> dict:
        self.layout_db.toggle_group_pin(group_id)
        return self._layout_mutated()

    def fulltext_search(self, query: str, top: int = 40) -> dict:
        """Conversation-body search with hit lines, shared with the TUI's Ctrl+F.

        Daemons warm and maintain a persisted index on a dedicated background
        thread. Queries never wait for changed histories once a snapshot exists.
        Library callers without start() retain synchronous refresh semantics.
        """
        index = self._search_index()
        sessions = self._search_sessions()
        if not self._threads or not index.ready:
            index.refresh(self.store, sessions)
        titles = {session_key(session): self.store.get_title(session) for session in sessions}
        outcome = index.search(sessions, query, titles=titles, top=max(1, min(int(top), 100)))
        return {
            "total": outcome.total,
            "matches": [
                {
                    "key": match.key,
                    "title": match.title,
                    "total_hits": match.total_hits,
                    "lines": [
                        {
                            "role": line.role,
                            "text": line.text,
                            "spans": [list(span) for span in line.spans],
                            "ts": self._wire_float(line.timestamp),
                        }
                        for line in match.lines
                    ],
                }
                for match in outcome.matches
            ],
        }

    def layout_set_collapsed(self, group_id: str, collapsed: bool) -> dict:
        self.layout_db.set_collapsed(group_id, collapsed)
        return self._layout_mutated()

    def layout_rename_group(self, group_id: str, name: str) -> dict:
        self.layout_db.rename_group(group_id, name)
        return self._layout_mutated()

    def conversation_snapshot(self, key: str) -> list[dict]:
        """不改订阅计数，只读当前富消息全文（仅供本机内部兼容路径）。"""
        session = self.require_session(key)
        transcript = self._ensure_transcript(session)
        return [item.to_wire_dict() for item in transcript.messages]

    def conversation_page(
        self,
        key: str,
        *,
        limit: int = MESSAGE_PAGE_LIMIT,
        before_seq: int | None = None,
    ) -> dict:
        """不改订阅计数，只读一页当前富消息。"""
        return self.message_page(key, limit=limit, before_seq=before_seq)

    def watch_conversation(
        self,
        key: str,
        *,
        limit: int = MESSAGE_PAGE_LIMIT,
        after_seq: int | None = None,
        generation: int | None = None,
    ) -> dict:
        """订阅一条会话的实时聊天流，同时只返回有限历史窗口。

        首包与实时增量共用同一份规范化缓存和同一把 reader：
        打开会话时不再为了推进游标或生成首包而把历史读两遍。

        手机重连可带 after_seq / generation：代次一致且缺口仍在则只回放更新
        （resume=replay）；否则退回当前尾部窗口（resume=tail）。未传 after_seq
        保持今天的尾部行为，旧手机不用改。
        """
        session = self.require_session(key)
        transcript = self._ensure_transcript(session)
        replayed: list[richmsg.RichMessage] | None = None
        with self._lock:
            watch = self._conversations.get(key)
            canonical = session_key(session)
            if watch is None:
                watch = _ConversationWatch(
                    key,
                    transcript.reader,
                    generation=transcript.generation,
                    canonical_key=canonical,
                )
                self._conversations[key] = watch
            else:
                if watch.generation != transcript.generation:
                    watch.deltas.clear()
                watch.reader = transcript.reader
                watch.generation = transcript.generation
                watch.canonical_key = canonical
            watch.watchers += 1
            if after_seq is not None:
                replayed = _try_replay(watch, transcript, int(after_seq), generation)
        if after_seq is not None and replayed is not None:
            page = _replay_page(
                replayed,
                after_seq=int(after_seq),
                generation=transcript.generation,
                total=len(transcript.messages),
            )
            page.update(attention=self.session_payload(session)["attention"], live=bool(session.get("live")))
            return page
        page = _message_page(
            transcript.messages,
            limit=limit,
            generation=transcript.generation,
            has_earlier=transcript.reader.has_earlier(),
        )
        page["resume"] = "tail"
        page.update(attention=self.session_payload(session)["attention"], live=bool(session.get("live")))
        return page

    def unwatch_conversation(self, key: str) -> None:
        with self._lock:
            watch = self._conversations.get(key)
            if watch is None:
                return
            watch.watchers -= 1
            if watch.watchers <= 0:
                self._conversations.pop(key, None)

    def watch_screen(self, key: str) -> dict | None:
        """订阅终端画面。返回首帧（整屏）；会话没有托管在 tmux 里则返回 None。"""
        session = self.require_session(key)
        if not session.get("keepalive_name"):
            raise ActionError("unavailable", t("remote.err.no_live_screen"))
        with self._lock:
            watch = self._screens.get(key)
            if watch is None:
                watch = _ScreenWatch(key, ScreenEncoder())
                self._screens[key] = watch
            watch.watchers += 1
            watch.encoder.reset()
            watch.last_capture = None
        return self._capture_frame(watch)

    def unwatch_screen(self, key: str) -> None:
        with self._lock:
            watch = self._screens.get(key)
            if watch is None:
                return
            watch.watchers -= 1
            if watch.watchers <= 0:
                self._screens.pop(key, None)

    def resync_screen(self, key: str) -> dict | None:
        """已在订阅中的连接再要一帧整屏（不增加引用计数）。

        聊天页为状态条、终端页为画面会各调一次 screen.watch；协议层对同一连接
        只记一次订阅，第二次必须仍能拿到 full 基准帧，否则手机只能拿着空网格
        硬套增量，画面会错乱。
        """
        with self._lock:
            watch = self._screens.get(key)
            if watch is None or watch.watchers <= 0:
                raise ActionError("usage_error", t("remote.err.not_watching_screen"))
            watch.encoder.reset()
            watch.last_capture = None
        return self._capture_frame(watch)

    def scroll_screen(self, key: str, offset: int) -> dict | None:
        with self._lock:
            watch = self._screens.get(key)
            if watch is None:
                raise ActionError("usage_error", t("remote.err.not_watching_screen"))
            watch.scroll_offset = max(0, int(offset))
            watch.encoder.reset()
            watch.last_capture = None
        return self._capture_frame(watch)

    # -- 桌面终端原始流（Mac 终端视图） ----------------------------------

    def terminal_attach(self, key: str, viewer: str, cols: int, rows: int, *, vote: bool) -> dict:
        """Start (or join) the session's raw stream; a snapshot event follows."""
        name = self._keepalive_name(key)
        stream_class = shell_terminal.ShellTerminalStream if shell_terminal.is_shell_key(key) else TerminalStream
        with self._lock:
            entry = self._terminals.get(key)
            if entry is None:
                stream = stream_class(
                    key,
                    name,
                    emit=lambda payload, key=key: self._on_event(f"term:{key}", payload),
                    resolve_name=lambda key=key: self._hosted_name(key),
                )
                entry = (stream, set())
                self._terminals[key] = entry
                stream.start()
            stream, viewers = entry
            viewers.add(viewer)
        return self._terminal_size(stream, viewer, cols, rows, vote=vote, snapshot=True)

    def terminal_resize(self, key: str, viewer: str, cols: int, rows: int, *, vote: bool) -> dict:
        return self._terminal_size(self._terminal(key), viewer, cols, rows, vote=vote, snapshot=False)

    def terminal_resync(self, key: str) -> None:
        self._terminal(key).request_snapshot()

    def terminal_theme(self, key: str, report: bytes) -> None:
        self._terminal(key).set_theme(report)

    def terminal_input(self, key: str, data: bytes, viewer: str = "") -> None:
        if not data:
            raise ActionError("usage_error", t("remote.err.no_content"))
        with self._lock:
            entry = self._terminals.get(key)
        if entry is not None and viewer and isinstance(entry[0], shell_terminal.ShellTerminalStream):
            entry[0].activate(viewer)  # typing makes this viewer the shell's size source
        name = entry[0].name if entry is not None else self._keepalive_name(key)
        if not embed.send_bytes(name, data):
            raise ActionError("unavailable", t("remote.err.session_not_running"))

    def terminal_detach(self, key: str, viewer: str) -> None:
        with self._lock:
            entry = self._terminals.get(key)
            if entry is None:
                return
            stream, viewers = entry
            viewers.discard(viewer)
            last = not viewers
            if last:
                self._terminals.pop(key, None)
        stream.withdraw(viewer)
        if last:
            stream.stop()

    def _terminal(self, key: str) -> TerminalStream:
        with self._lock:
            entry = self._terminals.get(key)
        if entry is None:
            raise ActionError("usage_error", t("remote.err.not_watching_screen"))
        return entry[0]

    @staticmethod
    def _terminal_size(stream: TerminalStream, viewer: str, cols: int, rows: int,
                       *, vote: bool, snapshot: bool) -> dict:
        if vote:
            effective = stream.vote(viewer, cols, rows)
        else:
            effective = embed.pane_size(stream.name) or (cols, rows)
        if snapshot:
            stream.request_snapshot()
        return {"cols": effective[0], "rows": effective[1]}

    def _hosted_name(self, key: str) -> str:
        if shell_terminal.is_shell_key(key):
            name = shell_terminal.name_for_key(key)
            return name if name and shell_terminal.alive(name) else ""
        session = self.store.find_session(self.resolve_session_key(key)) or {}
        return str(session.get("keepalive_name") or "")

    def _keepalive_name(
        self,
        key: str,
        *,
        resume_if_needed: bool = False,
        resumed: list[bool] | None = None,
    ) -> str:
        """Return the hosted tmux name; optionally native-resume a stopped session.

        Phone chat can open ended history and still send. Desktop Enter-to-restart
        already resumes; phone input must do the same instead of a red failed bubble.

        ``resumed`` (optional out-list) receives ``True`` when this call
        native-resumed the session, so callers can wait for the fresh pane.

        The stored name is returned without a liveness fork on the happy path.
        A stale (dead) binding is recovered failure-triggered at the inject step
        (`_recover_dead_pane_binding`): inject first, and only when it certainly
        failed AND the pane is tri-state dead (`embed.pane_liveness`, authoritative
        target-not-found only) the binding is cleared and the SAME conversation
        is natively resumed once. Uncertainty (timeouts) never resumes.
        """
        if shell_terminal.is_shell_key(key):
            return self._shell_name(key)
        session = self.require_session(key)
        name = str(session.get("keepalive_name") or "")
        if name:
            return name
        if resume_if_needed:
            self.resume_session(key)
            session = self.require_session(key)
            name = str(session.get("keepalive_name") or "")
            if name:
                if resumed is not None:
                    resumed.append(True)
                return name
        raise ActionError("unavailable", t("remote.err.session_not_running"))

    @staticmethod
    def _shell_name(key: str) -> str:
        name = shell_terminal.name_for_key(key)
        if not name or not shell_terminal.alive(name):
            raise ActionError("not_found", t("remote.err.shell_ended"))
        return name

    @staticmethod
    def _pane_proven_dead(name: str) -> bool:
        """Tri-state death check: only authoritative target-not-found counts.

        Timeouts and transport errors are ``"unknown"``, never death — two
        unknowns must not clear a binding or resume (that would duplicate a
        merely busy pane). See `embed.pane_liveness`.
        """
        try:
            return bool(name) and embed.pane_liveness(name) == "dead"
        except Exception:
            return False

    @staticmethod
    def _inject_cause_text(cause: str) -> str:
        """Map an internal inject cause code to a human localized description.

        Raw codes never reach the user copy or the wire detail.
        """
        key = _INJECT_CAUSE_MESSAGE_KEYS.get(str(cause or ""))
        if key is None:
            key = "remote.err.inject_cause_tmux_error"
        return t(key)

    def _recover_dead_pane_binding(self, key: str, name: str, *, cause: str) -> tuple[str, bool]:
        """Certain injection failure on the stored binding: recover and return
        ``(pane_name, resumed)`` for exactly one retry.

        Serialized on the canonical key (`_restart_lock_for`, the same lock the
        phone restart path uses — same acquisition order everywhere, so no
        deadlock): concurrent recoveries re-read the binding under the lock, so
        only the first one resumes; the others reuse the new binding. Never
        retargets, never copy/handoff. ``resumed`` tells the caller which error
        wording is truthful (a restart DID happen vs nothing was restarted).
        Only a tri-state-dead pane resumes. Resume failure propagates its own
        cause. Live-or-unknown panes never resume.
        """
        canonical = self.resolve_session_key(key)
        lock = self._restart_lock_for(canonical)
        with lock:
            current = self.store.find_session(canonical)
            if current is None:
                raise ActionError("not_found", t("remote.err.session_gone"))
            target = str(current.get("keepalive_name") or "")
            moved = bool(target) and target != name
            if moved:
                # Binding moved while we were injecting (another recovery won
                # the race, or the desktop re-hosted): judge the current one.
                name = target
            if name and not self._pane_proven_dead(name):
                # Current binding is live-or-unknown. Our attempt certainly
                # failed with no effect, so exactly one retry on the current
                # same-session binding is safe. ``moved`` keeps the post-retry
                # wording truthful (restart happened vs nothing restarted).
                return name, moved
            if not name:
                # No binding left under the lock: same-conversation resume
                # cannot duplicate anything.
                pass
            self.store.mark_hosted(canonical, None)
            if name:
                try:
                    embed.forget_alive(name)
                except Exception:
                    pass
            # Same-conversation native resume only (binding was cleared above,
            # so the resume path cannot no-op on the dead name; if the desktop
            # re-hosted concurrently, resume no-ops on the live binding and we
            # simply reuse it). Failure propagates with its own actionable
            # cause; unknown/partial receipts are kept.
            self.resume_session(canonical)
            session = self.require_session(canonical)
            new_name = str(session.get("keepalive_name") or "")
            if not new_name:
                raise ActionError(
                    "unavailable", t("remote.err.inject_resumed_not_ready")
                )
            return new_name, True

    def _attempt_with_recovery(self, key: str, name: str, attempt) -> tuple[str, dict]:
        """Run ``attempt(name) -> InjectionResult``; certain failure on a proven-dead
        binding resumes the same conversation once and retries once.

        Uncertainty (possible side effect) raises PartialInjectionError immediately —
        never retried, never resumed. A failed post-resume retry raises the
        truthful after-resume error (a restart DID happen). Returns the live
        pane name and the refreshed session.
        """
        res = attempt(name)
        if res.uncertain:
            raise PartialInjectionError(t("remote.err.inject_partial"))
        if res.ok:
            return name, self.store.find_session(self.resolve_session_key(key)) or {}
        name, resumed = self._recover_dead_pane_binding(key, name, cause=res.cause)
        session = self.store.find_session(self.resolve_session_key(key)) or {}
        res = attempt(name)
        if res.uncertain:
            raise PartialInjectionError(t("remote.err.inject_partial"))
        if not res.ok:
            # Truthful wording: only claim a restart when one actually happened.
            if resumed:
                raise ActionError(
                    "unavailable",
                    t(
                        "remote.err.inject_after_resume",
                        detail=self._inject_cause_text(res.cause),
                    ),
                )
            raise ActionError(
                "unavailable",
                t(
                    "remote.err.inject_transient",
                    detail=self._inject_cause_text(res.cause),
                ),
            )
        return name, session

    def _capture_frame(self, watch: _ScreenWatch) -> dict | None:
        session = self.store.find_session(self.resolve_session_key(watch.key))
        if session is None:
            return None
        name = str(session.get("keepalive_name") or "")
        if not name:
            return None
        state = embed.pane_state(name)
        if state is None:
            return None
        cursor_x, cursor_y, cursor_visible, _mouse_any, _mouse_sgr, history_size, pane_w, pane_h = state
        # 宽高已并进 pane_state，不必再单独问一次 pane_size。
        if pane_w > 0 and pane_h > 0:
            watch.cols, watch.rows = pane_w, pane_h
        if watch.cols <= 0:
            return None
        # 抓一屏画面。刻意不调用 resize：手机端订阅不该改变会话窗口尺寸，
        # 否则电脑上正在看同一个会话的人会被手机挤窄（这条是设计约束，别顺手改）。
        text = embed.capture(name, watch.scroll_offset, watch.rows)
        if text is None:
            return None
        capture_state = (
            text,
            watch.cols,
            watch.rows,
            cursor_x,
            cursor_y,
            cursor_visible,
            history_size,
            watch.scroll_offset,
        )
        if watch.last_capture == capture_state:
            return None
        watch.last_capture = capture_state
        grid = embed.parse_screen(text, watch.cols, watch.rows)
        frame = watch.encoder.encode(
            grid,
            cursor=(cursor_x, cursor_y, cursor_visible),
            history_size=history_size,
            history_offset=watch.scroll_offset,
        )
        return frame.to_dict() if frame is not None else None

    def _pump_screen(self, watch: _ScreenWatch) -> None:
        frame = self._capture_frame(watch)
        if frame is not None:
            self._on_event(f"screen:{watch.key}", frame)

    # -- 输入 -------------------------------------------------------------

    def _send_shell_text(self, key: str, text: str, submit: bool) -> None:
        """Paste into a project shell (bracketed when the shell asked for it)."""
        name = self._shell_name(key)
        if text and not embed.paste_detailed(name, text).ok:
            raise ActionError("unavailable", t("remote.err.shell_ended"))
        if submit and not embed.send_key(name, "Enter"):
            raise PartialInjectionError(t("remote.err.inject_partial"))

    def send_text(self, key: str, text: str, submit: bool = True) -> None:
        """把一段文本送进会话。

        走 tmux 粘贴缓冲而不是逐字符发送：这条路径对中文输入法、多行文本和
        括号粘贴语义都是安全的，是桌面端已经验证过的写法。回车单独补一次，
        因为部分助手会把粘贴内容里的换行当成软换行而不是提交。

        Phone submits always steer (never queue-until-done follow-up). For
        Cursor, a second empty Enter promotes a mid-turn queue into the active
        run; skip that only while the agent is waiting for an answer.

        Raises ActionError when injection certainly failed with no side effect.
        Raises PartialInjectionError when anything may already have reached
        the pane (uncertain paste/Enter, or Enter failure after a paste).

        A certain paste failure on a proven-dead binding clears it and natively
        resumes the same conversation once, then retries once on the new pane.
        """
        if shell_terminal.is_shell_key(key):
            self._send_shell_text(key, text, submit)
            return
        woke: list[bool] = []
        name = self._keepalive_name(key, resume_if_needed=True, resumed=woke)
        session = self.store.find_session(self.resolve_session_key(key)) or {}
        if woke:
            # Just native-resumed: Enter sent while the assistant is still
            # starting is dropped and the text stays in the composer.
            self._wait_pane_settled(name)
        pasted = False
        if text:
            name, session = self._attempt_with_recovery(
                key, name, lambda pane: embed.paste_detailed(pane, text)
            )
            pasted = True
        if submit:
            time.sleep(0.05)  # 给目标程序一点时间收完粘贴，避免回车抢在正文前面
            res = self._submit_enter(name, session)
            if res.uncertain or (not res.ok and pasted):
                raise PartialInjectionError(t("remote.err.inject_partial"))
            if not res.ok:
                # Certain Enter failure with nothing pasted (empty text): the
                # Enter itself hit a dead binding — same single
                # same-conversation recovery, then retry once.
                name, session = self._attempt_with_recovery(
                    key, name, lambda pane: embed.send_key_detailed(pane, "Enter")
                )
            if text:
                self._emit_provisional_working(key)
        # Only a submitted turn is a user message. Unsubmitted text is live
        # terminal typing (desktop client) and must not appear in the chat.
        if text and submit:
            # 立刻回显到手机传来的通道，不占规范化 seq；助手历史落地后的正式消息才带 seq。
            self._on_event(
                f"session:{key}",
                {
                    "version": 1,
                    "kind": "echo",
                    "session": key,
                    "role": "user",
                    "text": text,
                },
            )

    def _emit_provisional_working(self, key: str) -> None:
        """Tell every watcher the turn started the moment input was submitted.

        The scanner only reports working once the agent writes its transcript,
        which can lag the submit by seconds. This hint does not touch the
        authoritative attention, the list payload, or push decisions; clients
        expire it themselves when no confirmation follows.
        """
        self._on_event(
            f"session:{key}",
            {
                "version": 1,
                "kind": "attention",
                "session": key,
                "attention": "working",
                "provisional": True,
                "live": True,
            },
        )

    def send_keys(self, key: str, keys: list[str]) -> None:
        """Uncertain key delivery is partial/unknown, never a plain rejection."""
        name = self._keepalive_name(key, resume_if_needed=True)
        cleaned = [str(k) for k in keys if str(k).strip()]
        if not cleaned:
            raise ActionError("usage_error", t("remote.err.no_keys"))
        self._attempt_with_recovery(
            key, name, lambda pane: embed.send_key_detailed(pane, *cleaned)
        )

    def send_image(self, key: str, image_bytes: bytes) -> str:
        """把图片落到会话工作目录并把路径交给助手，复用桌面端已有的落盘+粘贴路径协议。"""
        if not image_bytes:
            raise ActionError("usage_error", t("remote.err.no_image"))
        name = self._keepalive_name(key, resume_if_needed=True)
        path = embed.save_image_and_paste_path(name, image_bytes)
        if not path:
            raise ActionError("unavailable", t("remote.err.image_save_failed"))
        return path

    def send_turn(
        self,
        key: str,
        text: str,
        *,
        images: list[bytes] | None = None,
        submit: bool = True,
        ready_timeout: float = _TURN_READY_TIMEOUT,
    ) -> list[str]:
        """Deliver images + text as one turn. Built for dump adapters (EditHere).

        ``send_image`` only pastes a path; ``send_text`` submits, but on a
        still-starting Cursor Agent the Enter can land before the TUI accepts
        keys — the prompt then sits in the composer forever while dump.log
        already says success. This method waits until the pane shows an input
        prompt, pastes paths then text, submits with a longer pause, and
        retries Enter while the prompt is still stuck in the composer.

        Returns the saved image paths (empty when no images).
        """
        name = self._keepalive_name(key, resume_if_needed=True)
        session = self.store.find_session(self.resolve_session_key(key)) or {}
        if self._pane_proven_dead(name):
            # Dead binding would burn the whole ready-timeout in captures that
            # can never turn ready; resume the same conversation up front.
            name = self._recover_dead_pane_binding(key, name, cause="pane_gone")
            session = self.store.find_session(self.resolve_session_key(key)) or {}
        self._wait_pane_ready(name, timeout=ready_timeout)

        paths: list[str] = []
        for index, image_bytes in enumerate(images or []):
            if not image_bytes:
                raise ActionError("usage_error", t("remote.err.no_image"))
            path = embed.save_image_and_paste_path(name, image_bytes)
            if not path:
                raise ActionError("unavailable", t("remote.err.image_save_failed"))
            paths.append(path)
            if index + 1 < len(images or []):
                time.sleep(_TURN_IMAGE_GAP)

        pasted = False
        if text:
            if paths:
                # Image paths are already in the pane: a text failure now must
                # NOT resume/retry (that would replay the turn without its
                # images, or double-deliver). Partial/unknown, always.
                res = embed.paste_detailed(name, text)
                if res.uncertain or not res.ok:
                    raise PartialInjectionError(t("remote.err.inject_partial"))
            else:
                name, session = self._attempt_with_recovery(
                    key, name, lambda pane: embed.paste_detailed(pane, text)
                )
            pasted = True

        if submit:
            time.sleep(_TURN_SUBMIT_PAUSE)
            res = self._submit_enter(name, session)
            if res.uncertain or not res.ok:
                if pasted or paths:
                    raise PartialInjectionError(t("remote.err.inject_partial"))
                raise ActionError("unavailable", t("remote.err.inject_failed"))
            self._ensure_turn_submitted(name, session, text)
            self._emit_provisional_working(key)

        if text:
            self._on_event(
                f"session:{key}",
                {
                    "version": 1,
                    "kind": "echo",
                    "session": key,
                    "role": "user",
                    "text": text,
                },
            )
        return paths

    def _wait_pane_settled(
        self,
        name: str,
        *,
        quiet: float = _RESUME_SETTLE_QUIET,
        timeout: float = _RESUME_SETTLE_TIMEOUT,
    ) -> None:
        """Best-effort: return once the pane shows content unchanged for ``quiet``.

        Runtime-agnostic (startup spinners keep the pane changing, a ready
        composer does not). Never raises: on timeout the caller still injects,
        which is no worse than injecting immediately.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        last: str | None = None
        stable_since = time.monotonic()
        while True:
            plain = _plain_pane_text(embed.capture(name, 0, 0))
            now = time.monotonic()
            if plain != last:
                last = plain
                stable_since = now
            elif plain.strip() and now - stable_since >= quiet:
                return
            if now >= deadline:
                return
            time.sleep(_TURN_READY_POLL)

    def _wait_pane_ready(self, name: str, *, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            plain = _plain_pane_text(embed.capture(name, 0, 0))
            if _pane_accepts_input(plain):
                return
            if time.monotonic() >= deadline:
                raise ActionError("unavailable", t("remote.err.inject_failed"))
            time.sleep(_TURN_READY_POLL)

    def _submit_enter(self, name: str, session: dict):
        """Send Enter (+ Cursor steer promote). Returns the first Enter's result.

        Uncertainty propagates: an Enter that may have landed is partial/unknown
        for the caller, never a plain failure. The promote Enter stays
        best-effort — the first Enter already landed by then.
        """
        res = embed.send_key_detailed(name, "Enter")
        if not res.ok:
            return res
        if _phone_steer_promote(session):
            time.sleep(0.05)
            # Best-effort promote; the first Enter already landed. Same detailed
            # path so there is one observable injection point (result ignored).
            embed.send_key_detailed(name, "Enter")
        return res

    def _ensure_turn_submitted(self, name: str, session: dict, text: str) -> None:
        """Retry Enter while the prompt is still sitting in the composer."""
        marker = (text or "").strip()
        if not marker:
            return
        for _ in range(_TURN_SUBMIT_RETRIES):
            time.sleep(_TURN_SUBMIT_PAUSE)
            plain = _plain_pane_text(embed.capture(name, 0, 0))
            if not _composer_still_holds(plain, marker):
                return
            res = self._submit_enter(name, session)
            if res.uncertain or not res.ok:
                raise PartialInjectionError(t("remote.err.inject_partial"))
        plain = _plain_pane_text(embed.capture(name, 0, 0))
        if _composer_still_holds(plain, marker):
            raise PartialInjectionError(t("remote.err.inject_partial"))

    # -- 会话动作 ---------------------------------------------------------

    def mark_read(self, key: str) -> str:
        state = self.store.mark_session_read(key)
        return _ATTENTION_LABELS.get(state.kind, "none")

    def toggle_pin(self, key: str) -> bool:
        """切换移动端单会话置顶：只翻独立会话键，分组不参与。

        移动端没有分组概念：不管该会话在桌面侧栏是否属于某个分屏组，都只
        切 ``pinned_session_keys``。桌面整组置顶只是桌面侧栏的展示派生（落盘
        不存，由成员独立钉算出来），手机 pin 一条不得把同组其它会话一起钉上去。
        """
        key = self.resolve_session_key(key)
        layout = self.layout_db.toggle_session_pin(key, promote_to_group=False)
        return key in (getattr(layout, "pinned_session_keys", {}) or {})

    def stop_session(self, key: str) -> None:
        session = self.require_session(key)
        canonical = session_key(session)
        name = str(session.get("keepalive_name") or "")
        if not name:
            raise ActionError("unavailable", t("remote.err.session_not_running"))
        keepalive.kill(name)
        self.store.mark_hosted(canonical, None)

    def delete_session(self, key: str) -> None:
        session = self.require_session(key)
        canonical = session_key(session)
        runtime = self._runtime_of(session)
        self.store.mark_deleted(canonical)
        if key != canonical:
            self.store.mark_deleted(key)
        try:
            runtime.delete_session(session)
        except Exception as exc:
            self.store.abort_delete(canonical)
            if key != canonical:
                self.store.abort_delete(key)
            raise ActionError("unavailable", t("remote.err.delete_failed", error=exc)) from exc
        # 删除成功后同步清掉侧栏置顶/分组记忆，避免剩余成员仍挂着「幽灵组」
        for item in {key, canonical}:
            try:
                self.layout_db.remove_session(item)
            except Exception:
                pass

    def _host(self, plan, runtime_id: str, title: str, cwd: str | None) -> dict:
        ident = keepalive.new_session_ident()
        width, height = embed.normalize_host_size(_HOST_WIDTH, _HOST_HEIGHT)
        try:
            name = embed.host_session(plan, runtime_id, ident, width, height)
        except embed.EmbedError as exc:
            raise ActionError("unavailable", t("remote.err.launch_failed", error=exc)) from exc
        _raise_if_failed_at_start(name, "remote.err.launch_failed")
        session = self.store.register_hosted_session(
            runtime_id=runtime_id,
            keepalive_name=name,
            title=title,
            cwd=cwd,
            ident=ident,
        )
        return self.session_payload(session, self._layout())

    def new_session(
        self, runtime_id: str, cwd: str | None, *, whitelist: list[str] | None = None
    ) -> dict:
        runtime_id = str(runtime_id or "").strip()
        if not runtime_id:
            raise ActionError("usage_error", t("remote.err.pick_assistant"))
        if cwd is not None:
            cwd = str(cwd).strip() or None
        if cwd:
            self._assert_cwd_allowed(cwd, whitelist or [])
        if not embed.available():
            raise ActionError("unavailable", t("remote.err.tmux_missing_start"))
        try:
            runtime = self.registry.get(runtime_id)
            plan = runtime.build_new_session_plan(cwd)
        except (KeyError, LaunchError) as exc:
            raise ActionError("usage_error", t("remote.err.cannot_start", error=exc)) from exc
        request = NewSessionRequest(target_runtime_id=runtime_id, cwd=cwd or "")
        title = f"{runtime.display_name} · 新会话"
        del request  # 结构体只用于表达意图，实际启动只需要计划本身
        return self._host(plan, runtime_id, title, cwd)

    def _assert_cwd_allowed(self, cwd: str, whitelist: list[str]) -> None:
        """新建会话的工作目录必须落在已知项目或用户白名单内。"""
        import os
        from pathlib import Path

        try:
            target = str(Path(cwd).expanduser().resolve())
        except OSError as exc:
            raise ActionError("usage_error", t("remote.err.invalid_project_path")) from exc
        allowed: set[str] = set()
        for entry in self.projects():
            for key in ("path", "cwd"):
                raw = entry.get(key) or ""
                if not raw:
                    continue
                try:
                    allowed.add(str(Path(str(raw)).expanduser().resolve()))
                except OSError:
                    continue
        for raw in whitelist:
            try:
                allowed.add(str(Path(str(raw)).expanduser().resolve()))
            except OSError:
                continue
        if target in allowed:
            return
        # 也允许已知项目的子目录
        for root in allowed:
            try:
                if os.path.commonpath([target, root]) == root:
                    return
            except ValueError:
                continue
        raise ActionError(
            "usage_error",
            t("remote.err.project_not_allowed"),
        )

    def resume_session(self, key: str) -> dict:
        """原生恢复：用助手自己的恢复命令重开这条会话，历史完整延续。"""
        if not embed.available():
            raise ActionError("unavailable", t("remote.err.tmux_missing_resume"))
        session = self.require_session(key)
        canonical = session_key(session)
        # Resume, explicit restart and failure-triggered input recovery share
        # one lock. Recovery already holds it when it calls us, hence RLock.
        with self._restart_lock_for(canonical):
            return self._resume_locked(canonical)

    def _resume_locked(self, canonical: str) -> dict:
        session = self.require_session(canonical)
        name = str(session.get("keepalive_name") or "")
        if name:
            state = embed.pane_liveness(name)
            if state == "alive":
                return self.session_payload(session, self._layout())
            if state != "dead":
                # A timeout is not evidence of death. Do not report success or
                # start a competing assistant while the old pane may be alive.
                raise ActionError(
                    "unavailable", t("remote.err.resume_failed", error=self._inject_cause_text("tmux_busy"))
                )
            self.store.mark_hosted(canonical, None)
            embed.close_channel(name)
            embed.forget_alive(name)
            session = self.require_session(canonical)
        runtime = self._runtime_of(session)
        try:
            plan = runtime.build_resume_plan(session)
        except LaunchError as exc:
            raise ActionError("unavailable", t("remote.err.cannot_native_resume", error=exc)) from exc
        ident = keepalive.new_session_ident()
        width, height = embed.normalize_host_size(_HOST_WIDTH, _HOST_HEIGHT)
        try:
            name = embed.host_session(plan, runtime.id, ident, width, height)
        except embed.EmbedError as exc:
            raise ActionError("unavailable", t("remote.err.resume_failed", error=exc)) from exc
        _raise_if_failed_at_start(name, "remote.err.resume_failed")
        self.store.mark_hosted(canonical, name)
        refreshed = self.store.find_session(canonical) or session
        return self.session_payload(refreshed, self._layout())

    def _restart_lock_for(self, canonical: str) -> threading.RLock:
        """同会话重启串行：杀旧起新必须在同一把锁里，禁止并行起新。"""
        with self._lock:
            lock = self._restart_locks.get(canonical)
            if lock is None:
                lock = threading.RLock()
                self._restart_locks[canonical] = lock
            return lock

    def restart_session(self, key: str) -> dict:
        """桌面高级操作「重启会话」的远程入口：只换托管进程，不碰历史与身份。

        复用 TUI ``_restart_hosted_session`` / ``_restart_and_focus`` 的同一套
        非 UI 原语（plan 预检 → kill → 关通道 → 忘探活 → 等旧死 → 同 ident
        起新 → 改挂托管标记），不另起一套重启语义。手机菜单选择即确认，
        不做二次确认（2026-09-13 桌面裁定）。落盘历史、标题、项目、会话键、
        分屏/置顶记忆一律保留；失败直接报错，不伪装成功。
        """
        if not embed.available():
            raise ActionError("unavailable", t("remote.err.tmux_missing_restart"))
        session = self.require_session(key)
        if session.get("provisional"):
            raise ActionError("usage_error", t("remote.err.restart_provisional"))
        if is_shell_session(session):
            raise ActionError("usage_error", t("remote.err.restart_not_supported"))
        runtime = self._runtime_of(session)
        if runtime.id not in ACTIVE_RUNTIME_IDS:
            raise ActionError("usage_error", t("remote.err.restart_not_supported"))
        canonical = session_key(session)
        if not session.get("keepalive_name"):
            # 已结束：没有可杀的进程，直接走原生恢复（与桌面回车同一条路）。
            return self.resume_session(canonical)
        # 预检先行：恢复计划都生成不出来时，原进程必须原样保留。
        try:
            self.registry.build_launch_plan(
                LaunchRequest(session, runtime.id, self.store.get_title(session))
            )
        except LaunchError as exc:
            raise ActionError(
                "unavailable", t("remote.err.cannot_restart", error=exc)
            ) from exc
        with self._restart_lock_for(canonical):
            return self._restart_locked(canonical, runtime.id)

    def _restart_locked(self, canonical: str, runtime_id: str) -> dict:
        """锁内重启：重读当前绑定（并发期间会话可能已变），杀旧后起新。"""
        current = self.store.find_session(canonical)
        if current is None:
            raise ActionError("not_found", t("remote.err.session_gone"))
        name = str(current.get("keepalive_name") or "")
        if not name:
            return self.resume_session(canonical)
        try:
            plan = self.registry.build_launch_plan(
                LaunchRequest(current, runtime_id, self.store.get_title(current))
            )
        except LaunchError as exc:
            raise ActionError(
                "unavailable", t("remote.err.cannot_restart", error=exc)
            ) from exc
        # 杀旧 best-effort，但必须验死：旧 pane 还活着就拒绝，绝不能把
        # 同名复用回来的旧进程当成“已重启”返回（那会静默假装成功）。
        # 尺寸沿用旧 pane 的真实几何（组身份=同 tmux 名 + 同会话键改挂，
        # 落盘历史不动），取不到才回落默认托管尺寸。
        try:
            old_size = embed.pane_size(name)
        except Exception:
            old_size = None
        keepalive.kill(name)
        try:
            embed.close_channel(name)
        except Exception:
            pass
        try:
            embed.forget_alive(name)
        except Exception:
            pass
        for _ in range(10):
            try:
                alive = embed.is_alive(name)
            except Exception:
                alive = False
                break
            if not alive:
                break
            time.sleep(0.1)
        try:
            still_alive = bool(embed.is_alive(name))
        except Exception:
            still_alive = False
        if still_alive:
            raise ActionError(
                "unavailable", t("remote.err.restart_still_running")
            )
        # 同会话恢复：沿用原会话 id 做 ident（与桌面 `_restart_and_focus` 一致，
        # 落盘历史不变，不插占位卡）。
        ident = str(current.get("id") or "").strip() or name.rsplit("-", 1)[-1]
        if old_size is not None:
            width, height = embed.normalize_host_size(old_size[0], old_size[1])
        else:
            width, height = embed.normalize_host_size(_HOST_WIDTH, _HOST_HEIGHT)
        try:
            new_name = embed.host_session(plan, runtime_id, ident, width, height)
        except embed.EmbedError as exc:
            # 失败不碰托管标记（与桌面 `_on_restart_failed` 一致）：不伪装成功，
            # 下一轮扫描按绑定的真实存活纠正展示。
            raise ActionError(
                "unavailable", t("remote.err.restart_failed", error=exc)
            ) from exc
        _raise_if_failed_at_start(new_name, "remote.err.restart_failed")
        self.store.mark_hosted(canonical, new_name)
        refreshed = self.store.find_session(canonical) or current
        return self.session_payload(refreshed, self._layout())

    def handoff_session(self, key: str, target_runtime_id: str) -> dict:
        """跨助手接力：把原会话导出成提示词，在目标助手里新开一局。"""
        if not embed.available():
            raise ActionError("unavailable", t("remote.err.tmux_missing_handoff"))
        session = self.require_session(key)
        source = self._runtime_of(session)
        try:
            target = self.registry.get(target_runtime_id)
        except KeyError as exc:
            raise ActionError("usage_error", t("remote.err.assistant_missing")) from exc
        title = self.store.get_title(session)
        request = LaunchRequest(session=session, target_runtime_id=target_runtime_id, title=title)
        try:
            handoff = source.export_handoff(request.session, request.title)
            plan = target.build_new_plan(handoff)
        except LaunchError as exc:
            raise ActionError("unavailable", t("remote.err.handoff_failed", error=exc)) from exc
        return self._host(plan, target_runtime_id, f"接力 · {title}", session.get("cwd"))

    def copy_session(self, key: str) -> dict:
        """同助手复制会话：官方分叉优先，否则磁盘克隆后原生恢复。

        复用注册表 `prepare_copy_request`（标题/copy 后缀/未安装报错都在里面）
        与 `build_launch_plan`（Claude/Codex/OpenCode/Pi 走官方分叉，Cursor/Kimi
        走磁盘克隆后再原生恢复），不另起炉灶。缺历史/不支持分叉时把原
        LaunchError message 透出，不伪造路径。
        """
        if not embed.available():
            raise ActionError("unavailable", t("remote.err.tmux_missing_handoff"))
        session = self.require_session(key)
        title = self.store.get_title(session)
        try:
            request = self.registry.prepare_copy_request(session, title)
            plan = self.registry.build_launch_plan(request)
        except LaunchError as exc:
            raise ActionError("unavailable", t("remote.err.handoff_failed", error=exc)) from exc
        copy_title = request.title
        return self._host(plan, request.target_runtime_id, copy_title, session.get("cwd"))

    # -- 关注状态变化 -----------------------------------------------------

    def _snapshot_attention(self) -> None:
        self._last_attention = {
            session_key(s): str(s.get("attention_kind") or "none") for s in self.store.all_sessions()
        }

    def _snapshot_live(self) -> None:
        self._last_live = {
            session_key(s): bool(s.get("live")) for s in self.store.all_sessions()
        }

    def _snapshot_status(self) -> None:
        self._last_status = {
            session_key(s): str(s.get("status_tag") or "") for s in self.store.all_sessions()
        }
        self._last_completion = {
            session_key(s): str(s.get("completion_id") or "") for s in self.store.all_sessions()
        }

    def _detect_attention_changes(self) -> None:
        """关注状态变化：正在看的对话走实时事件；系统推送仍只报「等你回答」。

        推送层只收 waiting 跃迁，避免长任务刷屏。已经打开详情的手机必须立刻
        看到「正在处理 / 等你回答」，所以 conversation watch 订阅任意状态变化。
        一轮结束通知走 ``_detect_status_changes``（SessKit status_tag），不在这里发。
        """
        hook = self._attention_hook
        layout = self._layout() if hook else None
        with self._lock:
            watches = [
                watch
                for watch in self._conversations.values()
                if watch.watchers > 0
            ]
        for session in self.store.all_sessions():
            key = session_key(session)
            current = str(session.get("attention_kind") or "none")
            previous = self._last_attention.get(key)
            self._last_attention[key] = current
            if previous is not None and current == previous:
                continue
            label = _ATTENTION_LABELS.get(current, "none")
            for watch in watches:
                if watch.key != key and watch.canonical_key != key:
                    continue
                self._on_event(
                    f"session:{watch.key}",
                    {
                        "version": 1,
                        "kind": "attention",
                        "session": watch.key,
                        "attention": label,
                        # Open details may have dropped list watch; live must ride
                        # with attention or the phone keeps showing Ended.
                        "live": bool(session.get("live")),
                    },
                )
            if previous is None:
                continue
            if current == "waiting":
                payload = self.session_payload(session, layout)
                self._emit_notification(payload, "waiting")
                try:
                    if hook is not None:
                        hook(payload, previous, current)
                except Exception:
                    continue

    def _detect_live_changes(self) -> None:
        """Process alive flips for open conversation watches.

        List watch is often torn down on the detail page, so a live-only flip
        (still ``working``, but process rebound) must patch the open detail via
        metadata — otherwise the header stays on Ended while messages keep
        arriving.
        """
        with self._lock:
            watches = [
                watch
                for watch in self._conversations.values()
                if watch.watchers > 0
            ]
        if not watches:
            # Still advance the baseline so the first open after a flip is clean.
            for session in self.store.all_sessions():
                self._last_live[session_key(session)] = bool(session.get("live"))
            return
        layout = self._layout()
        for session in self.store.all_sessions():
            key = session_key(session)
            current = bool(session.get("live"))
            previous = self._last_live.get(key)
            self._last_live[key] = current
            if previous is None or previous == current:
                continue
            summary = self.session_payload(session, layout)
            for watch in watches:
                if watch.key != key and watch.canonical_key != key:
                    continue
                self._on_event(
                    f"session:{watch.key}",
                    {
                        "version": 1,
                        "kind": "metadata",
                        "session": watch.key,
                        "summary": summary,
                    },
                )

    def _emit_notification(self, payload: dict, kind: str) -> None:
        """Encrypted desktop event, independent of APNs preferences/list windows."""
        if self._sessions_watchers:
            self._on_event(
                "sessions",
                {"kind": "notification", "notification_kind": kind, "notification": payload},
            )

    def _detect_status_changes(self) -> None:
        """SessKit status_tag 变化 → 推送层（已完成 / 已中断）。

        启动基线不推。同值抖动不推——但同一会话 `completion_id` 变了
        说明新一轮结束了（DONE→DONE 也要推，由 PushNotifier 按轮去重）。
        若新会话在两次扫描之间已经结束（首次出现就是已完成/已中断），
        只要历史很新仍要推——否则短会话会漏通知。
        """
        hook = self._status_hook
        layout = self._layout()
        now = time.time()
        terminal_payloads: list[dict] = []
        for session in self.store.all_sessions():
            key = session_key(session)
            current = str(session.get("status_tag") or "")
            previous = self._last_status.get(key)
            self._last_status[key] = current
            current_cid = str(session.get("completion_id") or "")
            previous_cid = self._last_completion.get(key)
            self._last_completion[key] = current_cid
            if current in (
                sesskit_titles.STATUS_DONE,
                sesskit_titles.STATUS_ABORTED,
            ):
                terminal_payloads.append(self.session_payload(session, layout))
            if previous is not None and current == previous:
                # 同标签但新一轮（completion_id 变了）：DONE→DONE 也推。
                # 非终端态没有 completion_id，不在此列。
                if not current_cid or current_cid == (previous_cid or ""):
                    continue
                if current not in (
                    sesskit_titles.STATUS_DONE,
                    sesskit_titles.STATUS_ABORTED,
                ):
                    continue
            if previous is None:
                # First sight of this key after start: only notify if it is already
                # terminal and the history is fresh (completed between scans).
                if current not in (
                    sesskit_titles.STATUS_DONE,
                    sesskit_titles.STATUS_ABORTED,
                ):
                    continue
                try:
                    mtime = float(session.get("mtime") or 0.0)
                except (TypeError, ValueError):
                    mtime = 0.0
                if mtime <= 0 or (now - mtime) > _STATUS_NOTIFY_FRESH_SECONDS:
                    continue
            payload = self.session_payload(session, layout)
            if current == sesskit_titles.STATUS_DONE and current_cid:
                self._emit_notification(payload, "completed")
            elif current == sesskit_titles.STATUS_ABORTED:
                self._emit_notification(payload, "aborted")
            try:
                if hook is not None:
                    hook(payload, previous or "", current)
            except Exception:
                continue
        # 有界重试：待确认回执超时/失败、且仍是最新轮次的，按设备重发。
        # 新一轮/已消失会话的旧待确认在 retry_due 内丢弃，不补发 stale 轮次。
        # 生产组装注册的是绑定方法 hook（daemon: set_status_hook(push.on_status_change)），
        # 其上没有 retry_due 属性——必须经绑定 __self__ 解析到 PushNotifier，否则
        # 真实生产链路的重试驱动永远不会被调用。
        retry = getattr(hook, "retry_due", None)
        if retry is None:
            owner = getattr(hook, "__self__", None)
            retry = getattr(owner, "retry_due", None)
        if callable(retry):
            try:
                retry(terminal_payloads)
            except Exception:
                pass

    # -- 杂项 -------------------------------------------------------------

    def title_cache_size(self) -> int:
        try:
            return len(titles.load_cache())
        except Exception:
            return 0
