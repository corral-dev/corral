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
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from sesskit import titles as sesskit_titles

from corral import embed, keepalive, titles
from corral.cache import history_signature
from corral.i18n import t
from corral.models import LaunchRequest, NewSessionRequest, session_key
from corral.remote import questions, richmsg, transcript_cache
from corral.remote.screen import ScreenEncoder
from corral.runtime import LaunchError
from corral.split_layout import default_layout_db
from corral.store import SessionStore

_SCAN_LIMIT = 200
# Event-driven history refresh: wake early on FS change; idle reconcile matches the
# old 15s cadence so phone attention / list freshness do not regress to ~60s.
_REFRESH_RECONCILE = 15.0
_REFRESH_MIN_GAP = 15.0  # do not scan more often than the old remote cadence under thrash
_TITLE_POLL_SLICE = 15.0
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
_MAX_IN_MEMORY_TRANSCRIPTS = 48
_CONVERSATION_DELTA_LIMIT = 200  # 每条被看会话只留最近这么多增量；溢出则 replay 失败走 tail
# New session keys that first appear already terminal still notify if this fresh.
_STATUS_NOTIFY_FRESH_SECONDS = 300.0

_ATTENTION_LABELS = {"none": "none", "unread": "unread", "working": "working", "waiting": "waiting"}

# Dump/adapters (EditHere corral-cursor): wait for the TUI to accept keys before
# pasting, then pause longer than phone send_text so Enter is not lost on a
# still-starting Cursor Agent. Retry submit if the prompt is still in the composer.
_TURN_READY_TIMEOUT = 45.0
_TURN_READY_POLL = 0.25
_TURN_SUBMIT_PAUSE = 0.25
_TURN_SUBMIT_RETRIES = 3
_TURN_IMAGE_GAP = 0.15
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b."
)


class ActionError(RuntimeError):
    """动作无法执行；给用户看的 message 必须走 i18n.t()，随开发机界面语言。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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
        self.registry = self.store.registry
        self.layout_db = default_layout_db()
        self._on_event = on_event or (lambda channel, data: None)
        self._lock = threading.Lock()
        self._screens: dict[str, _ScreenWatch] = {}
        self._conversations: dict[str, _ConversationWatch] = {}
        self._transcripts: dict[str, _Transcript] = {}
        self._transcript_cache = transcript_cache.TranscriptCache()
        self._transcript_io = threading.Lock()
        self._sessions_watchers = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_attention: dict[str, str] = {}
        self._attention_hook = None  # 由推送层注入：(session, 旧状态, 新状态)
        self._last_live: dict[str, bool] = {}
        self._last_status: dict[str, str] = {}
        self._last_completion: dict[str, str] = {}
        self._status_hook = None  # 推送层：SessKit status_tag 已完成/已中断
        self._history_watcher = None

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        from corral.history_watch import HistoryWatcher
        from corral.schedprio import demote_background

        demote_background()
        watcher = HistoryWatcher()
        self._history_watcher = watcher
        watcher.start()
        self.store.load()
        self._snapshot_attention()
        self._snapshot_live()
        self._snapshot_status()
        for target in (self._refresh_loop, self._screen_loop, self._conversation_loop):
            thread = threading.Thread(target=target, daemon=True, name=f"remote-{target.__name__}")
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
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

    def _refresh_loop(self) -> None:
        from corral.schedprio import demote_background

        demote_background()
        watcher = self._history_watcher
        last_scan = 0.0
        while not self._stop.is_set():
            # Title updates are independent of history mtimes — poll them on the
            # short slice even when the full scan sleeps until FSEvents / reconcile.
            title_only = True
            if watcher is None:
                if self._stop.wait(_TITLE_POLL_SLICE):
                    return
                title_only = False
            else:
                deadline = time.monotonic() + _REFRESH_RECONCILE
                while not self._stop.is_set():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        title_only = False
                        break
                    if watcher.wait(timeout=min(_TITLE_POLL_SLICE, left)):
                        title_only = False
                        break
                    # Slice elapsed without FS change: still propagate titles.
                    title_keys = self.store.poll_title_updates()
                    if title_keys and self._sessions_watchers:
                        self._emit_title_events(title_keys)
                if self._stop.is_set():
                    return
                if last_scan > 0:
                    gap = _REFRESH_MIN_GAP - (time.monotonic() - last_scan)
                    if gap > 0 and self._stop.wait(gap):
                        return
                watcher.clear()

            title_keys = self.store.poll_title_updates()
            changed = False
            if not title_only:
                try:
                    changed = self.store.refresh()
                except Exception:
                    # History scan failures must not block title-only propagation.
                    pass
                last_scan = time.monotonic()
            title_keys.update(self.store.poll_title_updates())
            self._follow_key_migrations()
            self._detect_attention_changes()
            self._detect_live_changes()
            self._detect_status_changes()
            if (changed or title_keys) and self._sessions_watchers:
                self._on_event("sessions", self.list_snapshot())
            if title_keys:
                self._emit_title_events(title_keys)

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
        return str(key or "")

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
                "label": entry.get("label") or "",
                "name": entry.get("label") or "",
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

    def _keepalive_name(self, key: str, *, resume_if_needed: bool = False) -> str:
        """Return the hosted tmux name; optionally native-resume a stopped session.

        Phone chat can open ended history and still send. Desktop Enter-to-restart
        already resumes; phone input must do the same instead of a red failed bubble.
        """
        session = self.require_session(key)
        name = str(session.get("keepalive_name") or "")
        if name:
            return name
        if resume_if_needed:
            self.resume_session(key)
            session = self.require_session(key)
            name = str(session.get("keepalive_name") or "")
            if name:
                return name
        raise ActionError("unavailable", t("remote.err.session_not_running"))

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

    def send_text(self, key: str, text: str, submit: bool = True) -> None:
        """把一段文本送进会话。

        走 tmux 粘贴缓冲而不是逐字符发送：这条路径对中文输入法、多行文本和
        括号粘贴语义都是安全的，是桌面端已经验证过的写法。回车单独补一次，
        因为部分助手会把粘贴内容里的换行当成软换行而不是提交。

        Phone submits always steer (never queue-until-done follow-up). For
        Cursor, a second empty Enter promotes a mid-turn queue into the active
        run; skip that only while the agent is waiting for an answer.

        Raises ActionError when injection fails with no proven side effect.
        Raises PartialInjectionError when paste succeeded but Enter failed.
        """
        name = self._keepalive_name(key, resume_if_needed=True)
        session = self.store.find_session(self.resolve_session_key(key)) or {}
        pasted = False
        if text:
            if not embed.paste(name, text):
                raise ActionError("unavailable", t("remote.err.inject_failed"))
            pasted = True
        if submit:
            time.sleep(0.05)  # 给目标程序一点时间收完粘贴，避免回车抢在正文前面
            if not self._submit_enter(name, session, pasted=pasted):
                if pasted:
                    raise PartialInjectionError(t("remote.err.inject_partial"))
                raise ActionError("unavailable", t("remote.err.inject_failed"))
        if text:
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

    def send_keys(self, key: str, keys: list[str]) -> None:
        name = self._keepalive_name(key, resume_if_needed=True)
        cleaned = [str(k) for k in keys if str(k).strip()]
        if not cleaned:
            raise ActionError("usage_error", t("remote.err.no_keys"))
        if not embed.send_key(name, *cleaned):
            raise ActionError("unavailable", t("remote.err.inject_failed"))

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
            if not embed.paste(name, text):
                raise ActionError("unavailable", t("remote.err.inject_failed"))
            pasted = True

        if submit:
            time.sleep(_TURN_SUBMIT_PAUSE)
            if not self._submit_enter(name, session, pasted=pasted):
                if pasted or paths:
                    raise PartialInjectionError(t("remote.err.inject_partial"))
                raise ActionError("unavailable", t("remote.err.inject_failed"))
            self._ensure_turn_submitted(name, session, text)

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

    def _wait_pane_ready(self, name: str, *, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            plain = _plain_pane_text(embed.capture(name, 0, 0))
            if _pane_accepts_input(plain):
                return
            if time.monotonic() >= deadline:
                raise ActionError("unavailable", t("remote.err.inject_failed"))
            time.sleep(_TURN_READY_POLL)

    def _submit_enter(self, name: str, session: dict, *, pasted: bool) -> bool:
        """Send Enter (+ Cursor steer promote). Returns False if the first Enter failed."""
        del pasted  # callers use this only for error class selection
        if not embed.send_key(name, "Enter"):
            return False
        if _phone_steer_promote(session):
            time.sleep(0.05)
            embed.send_key(name, "Enter")  # best-effort; first Enter already landed
        return True

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
            if not self._submit_enter(name, session, pasted=True):
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
        if session.get("keepalive_name"):
            return self.session_payload(session, self._layout())
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
        canonical = session_key(session)
        self.store.mark_hosted(canonical, name)
        refreshed = self.store.find_session(canonical) or session
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
            if hook is None or previous is None:
                continue
            if current == "waiting":
                try:
                    hook(self.session_payload(session, layout), previous, current)
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

    def _detect_status_changes(self) -> None:
        """SessKit status_tag 变化 → 推送层（已完成 / 已中断）。

        启动基线不推。同值抖动不推——但同一会话 `completion_id` 变了
        说明新一轮结束了（DONE→DONE 也要推，由 PushNotifier 按轮去重）。
        若新会话在两次扫描之间已经结束（首次出现就是已完成/已中断），
        只要历史很新仍要推——否则短会话会漏通知。
        """
        hook = self._status_hook
        if hook is None:
            return
        layout = self._layout()
        now = time.time()
        for session in self.store.all_sessions():
            key = session_key(session)
            current = str(session.get("status_tag") or "")
            previous = self._last_status.get(key)
            self._last_status[key] = current
            current_cid = str(session.get("completion_id") or "")
            previous_cid = self._last_completion.get(key)
            self._last_completion[key] = current_cid
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
            try:
                hook(self.session_payload(session, layout), previous or "", current)
            except Exception:
                continue

    # -- 杂项 -------------------------------------------------------------

    def title_cache_size(self) -> int:
        try:
            return len(titles.load_cache())
        except Exception:
            return 0
