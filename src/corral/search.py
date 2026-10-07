"""会话对话正文的全文检索索引（运行时无关）。

和侧边栏筛选框不是一回事：筛选框只匹配项目名 / 路径 / 会话标题（`display.
_filter_sessions_by_query`），本模块搜的是会话里真正说过的话，供全文搜索弹窗
展示「命中的那一行」。

正文本身不重复解析：一律经 `SessionStore.get_conversation` 拿，那条路已经带
进程内缓存 + SQLite 派生缓存。整理后的搜索正文也按签名落进派生缓存，重启或
TUI / 远程服务切换时直接恢复，不必重新解析全部历史。

本模块不感知任何具体运行时，和 `keepalive` / `embed` 同属运行时无关层。
"""

from __future__ import annotations

import json
import threading
import unicodedata
import zlib
from dataclasses import dataclass

from corral.cache import get_cache, history_signature
from corral.models import session_key
from corral.projects import session_project_label

# 单条命中行最多保留多少字符；更长的行按命中位置开窗，只给关键词附近的上下文。
_MAX_LINE_CHARS = 200
# 开窗时命中词前面保留多少字符，让用户看得到这句话是从哪儿说起的。
_WINDOW_LEAD = 20
# 每个会话默认最多展示几条命中行。
DEFAULT_MAX_LINES = 3


@dataclass(frozen=True)
class MatchLine:
    """一条命中行：原文（可能已按命中位置开窗）+ 需要高亮的字符区间。"""

    role: str  # user / assistant
    text: str
    spans: tuple[tuple[int, int], ...]
    timestamp: float | None = None


@dataclass(frozen=True)
class SessionMatch:
    """一个会话的命中结果。`lines` 为空表示只有标题 / 项目命中，正文没提到。"""

    key: str
    session: dict
    title: str
    lines: tuple[MatchLine, ...]
    total_hits: int

    @property
    def meta_only(self) -> bool:
        return not self.lines


@dataclass(frozen=True)
class SearchOutcome:
    """一次查询的结果。

    `matches` 已按会话时间由新到旧排序，且**最多只有 `top` 条**——命中行的提取是
    整个查询里最贵的一步，只对真正会展示出来的那几条做。`total` 是命中的会话总数
    （含没展示的），供状态行如实告诉用户「还有多少条没显示」，不做静默截断。
    """

    matches: tuple[SessionMatch, ...]
    total: int

    def __len__(self) -> int:
        return len(self.matches)

    def __iter__(self):
        return iter(self.matches)

    def __getitem__(self, index):
        return self.matches[index]


@dataclass(frozen=True)
class _Entry:
    """单个会话的正文索引条目。signature 变了才需要重新读一次对话。"""

    signature: tuple
    lines: tuple[tuple[str, str, float | None], ...]  # (role, text, timestamp)
    blob: str  # 全部正文拼成的小写串，用于快速判定「这个会话到底有没有」


def split_keywords(query: str) -> list[str]:
    """查询串拆成关键词（空白分隔，小写）。多个关键词之间是「都要出现」。"""
    return [part for part in (query or "").lower().split() if part]


class _CleanMap(dict):
    """`str.translate` 用的懒查表：每个码点只判定一次，之后整串替换走 C 层。

    建索引原本 90% 的时间花在逐字符 `unicodedata.category()` 上（本机实测：168 个
    会话 229ms、461 个会话 1289ms）。真实语料的字符集合高度重复，改成查表后整轮
    建索引 261ms→95ms、1355ms→630ms；替换语义完全等价（在 8672 条真实消息上逐条
    比对过，无一条不一致）。
    """

    __slots__ = ()

    def __missing__(self, codepoint: int) -> str:
        char = chr(codepoint)
        if char == "\t":
            value = "    "
        elif char in _KEEP_CONTROL_CHARS or unicodedata.category(char)[0] != "C":
            value = char
        else:
            value = " "
        self[codepoint] = value
        return value


# 换行必须留着——正文要按行切开才能给出「命中的那一行」；ZWNJ/ZWJ 虽然也归在 Cf
# 类，但它们是连写和 emoji 组合的有效组成部分，一并保留（与
# `wrap_preview_text` 的口径一致）。制表符在上面摊平成四个空格。
_KEEP_CONTROL_CHARS = "\n\u200c\u200d"
_CLEAN_MAP = _CleanMap()


def _clean(text: str) -> str:
    """去掉会把 TUI 画崩的控制字符，并把制表符摊平。"""
    return text.translate(_CLEAN_MAP)


def _signature(session: dict) -> tuple:
    """Include file/WAL replacement; shared OpenCode DBs use per-session versions."""
    path = str(session.get("path") or "")
    return (
        path,
        session.get("size_bytes"),
        session.get("file_mtime"),
        history_signature(path) if path and session.get("source") != "opencode" else None,
    )


def _spans_in(lowered: str, keywords: list[str]) -> tuple[tuple[int, int], ...]:
    """找出该行里所有关键词出现的位置，重叠区间合并后按起点排序。"""
    raw: list[tuple[int, int]] = []
    for keyword in keywords:
        start = lowered.find(keyword)
        while start >= 0:
            raw.append((start, start + len(keyword)))
            start = lowered.find(keyword, start + 1)
    if not raw:
        return ()
    raw.sort()
    merged: list[tuple[int, int]] = [raw[0]]
    for start, end in raw[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _window(text: str, spans: tuple[tuple[int, int], ...]) -> tuple[str, tuple[tuple[int, int], ...]]:
    """行太长时按第一处命中开窗，保证关键词一定落在展示范围里。"""
    if len(text) <= _MAX_LINE_CHARS or not spans:
        return text, spans
    start = max(0, spans[0][0] - _WINDOW_LEAD)
    # 从词首往前退一点点即可；开头就命中时不加省略号，避免凭空多出一个「…」。
    prefix = "…" if start > 0 else ""
    # 两个省略号自己也占字符，必须从预算里扣掉，否则成品行会比 _MAX_LINE_CHARS 长
    # 出一两位——窄终端下这一两位就是被硬截掉的正文。
    end = start + _MAX_LINE_CHARS - len(prefix)
    suffix = "…" if end < len(text) else ""
    if suffix:
        end -= 1
    body = text[start:end]
    offset = len(prefix) - start
    shifted = tuple(
        (span_start + offset, min(span_end + offset, len(prefix) + len(body)))
        for span_start, span_end in spans
        if span_start >= start and span_start < end
    )
    return prefix + body + suffix, shifted


class ConversationIndex:
    """全部会话对话正文的内存索引。

    线程约定：`refresh()` 在后台线程跑（要读磁盘 / 解析 JSON），`search()` 在
    界面线程跑。两者不共享可变状态——refresh 先在局部字典里建好，最后整体换掉
    `_entries` 引用（CPython 下是原子操作），所以搜索永远看到一份自洽的快照，
    不需要加锁，也不会因为索引正在更新而卡住界面。
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self._ready = False
        # 首屏预热和「打开弹窗时发现还没建好」可能同时要求建索引；串行化避免两条
        # 线程把同一批会话各解析一遍。后到的那次会命中上一次的结果，几乎零成本。
        self._refresh_lock = threading.Lock()

    @property
    def ready(self) -> bool:
        """是否至少完整建过一次索引。"""
        return self._ready

    @property
    def indexed_count(self) -> int:
        return len(self._entries)

    def refresh(self, store, sessions: list[dict] | None = None, progress=None) -> int:
        """按需重建索引；签名没变的会话直接复用上一轮结果。

        `progress(done, total)` 每处理完一个会话回调一次，供界面显示建索引进度。
        返回索引里的会话数。
        """
        with self._refresh_lock:
            pending = list(store.all_sessions()) if sessions is None else list(sessions)
            previous = self._entries
            fresh: dict[str, _Entry] = {}
            signatures = {session_key(session): _signature(session) for session in pending}
            missing = [key for key, signature in signatures.items()
                       if key not in previous or previous[key].signature != signature]
            persisted = get_cache().get_search_entries(missing) if missing else {}
            writes = []
            total = len(pending)
            for done, session in enumerate(pending, start=1):
                key = session_key(session)
                signature = signatures[key]
                cached = previous.get(key)
                if cached is not None and cached.signature == signature:
                    fresh[key] = cached
                else:
                    encoded_signature = json.dumps([1, signature], separators=(",", ":"))
                    restored = _restore_entry(persisted.get(key), encoded_signature, signature)
                    entry = restored or _build_entry(store, session, signature)
                    fresh[key] = entry
                    if restored is None and entry.signature == signature:
                        payload = zlib.compress(json.dumps(entry.lines, ensure_ascii=False,
                                                           separators=(",", ":")).encode(), 1)
                        writes.append((key, encoded_signature, payload))
                if progress is not None:
                    progress(done, total)
            self._entries = fresh
            self._ready = True
            get_cache().put_search_entries(writes)
            return len(fresh)

    def search(
        self,
        sessions: list[dict],
        query: str,
        *,
        titles: dict[str, str] | None = None,
        max_lines: int = DEFAULT_MAX_LINES,
        top: int | None = None,
    ) -> SearchOutcome:
        """按关键词搜标题 / 项目 / 正文，返回按会话时间由新到旧排序的命中。

        `sessions` 由调用方传入当前列表数据，保证结果里的会话字段（标题、时间、
        运行中状态）始终是最新的——索引本身只存正文，不存展示态。

        分两遍是刻意的：第一遍只用已经小写好的 blob 判定「这个会话有没有」，排序
        之后才对前 `top` 条提取命中行。命中行提取要逐行 lower + 定位，是整个查询
        里最贵的一步；对着几百条命中全做一遍，界面线程会实打实卡住（461 个会话搜
        单字母实测 305ms，改成只算前 60 条后 33ms）。排序键只依赖会话时间，不依赖
        命中行，所以先排后截不会改变前 `top` 条的内容或顺序。
        """
        keywords = split_keywords(query)
        if not keywords:
            return SearchOutcome((), 0)
        titles = titles or {}
        entries = self._entries
        hits: list[tuple[float, str, dict, str, _Entry | None]] = []
        for session in sessions:
            key = session_key(session)
            title = titles.get(key) or str(session.get("fallback_title") or "")
            entry = entries.get(key)
            blob = entry.blob if entry is not None else ""
            meta = _meta_text(session, title)
            if not all(keyword in meta or keyword in blob for keyword in keywords):
                continue
            hits.append((session.get("mtime") or 0, key, session, title, entry))
        hits.sort(key=lambda hit: hit[0], reverse=True)
        shown = hits if top is None else hits[:top]
        matches = tuple(
            SessionMatch(key, session, title, *_collect_lines(entry, keywords, max_lines))
            for _mtime, key, session, title, entry in shown
        )
        return SearchOutcome(matches, len(hits))


def _meta_text(session: dict, title: str) -> str:
    """标题 / 项目名 / 路径拼成的小写串，让弹窗同时保留原来的筛选能力。"""
    parts = [
        title,
        str(session.get("fallback_title") or ""),
        session_project_label(session),
        str(session.get("cwd") or ""),
        str(session.get("cwd_display") or ""),
    ]
    return " ".join(part for part in parts if part).lower()


def _build_entry(store, session: dict, signature: tuple) -> _Entry:
    """读一个会话的对话正文并拆成行；读失败时留空条目，不让整轮建索引中断。"""
    try:
        messages = store.get_conversation(session)
    except Exception:
        # A transient read failure must neither persist an empty conversation
        # nor mark this signature current forever. Retry on the next refresh.
        return _Entry((), (), "")
    lines: list[tuple[str, str, float | None]] = []
    for message in messages:
        for raw in _clean(message.text or "").splitlines():
            stripped = raw.strip()
            if stripped:
                lines.append((message.role, stripped, message.timestamp))
    blob = "\n".join(line[1] for line in lines).lower()
    return _Entry(signature, tuple(lines), blob)


def _restore_entry(row: tuple[str, bytes] | None, encoded_signature: str,
                   signature: tuple) -> _Entry | None:
    if row is None or row[0] != encoded_signature:
        return None
    try:
        raw = json.loads(zlib.decompress(row[1]))
        if not isinstance(raw, list) or any(
            not isinstance(line, list) or len(line) != 3
            or not isinstance(line[0], str) or not isinstance(line[1], str)
            or (line[2] is not None and not isinstance(line[2], (int, float)))
            for line in raw
        ):
            return None
        lines = tuple(tuple(line) for line in raw)
        return _Entry(signature, lines, "\n".join(line[1] for line in lines).lower())
    except (ValueError, TypeError, zlib.error):
        return None


def _collect_lines(
    entry: _Entry | None, keywords: list[str], max_lines: int,
) -> tuple[tuple[MatchLine, ...], int]:
    """挑出要展示的命中行：同时含全部关键词的行优先，不够再用只命中部分的补足。

    返回 (展示行, 命中行总数)；总数用来告诉用户「还有多少处没展示」。
    """
    if entry is None or max_lines <= 0:
        return (), 0
    exact: list[tuple[str, str, float | None]] = []
    partial: list[tuple[str, str, float | None]] = []
    total = 0
    for role, text, timestamp in entry.lines:
        lowered = text.lower()
        if not any(keyword in lowered for keyword in keywords):
            continue
        total += 1
        if all(keyword in lowered for keyword in keywords):
            if len(exact) < max_lines:
                exact.append((role, text, timestamp))
        elif len(partial) < max_lines:
            partial.append((role, text, timestamp))
    picked = exact[:max_lines]
    if len(picked) < max_lines:
        picked = picked + partial[: max_lines - len(picked)]
    result = []
    for role, text, timestamp in picked:
        windowed, shifted = _window(text, _spans_in(text.lower(), keywords))
        result.append(MatchLine(role, windowed, shifted, timestamp))
    return tuple(result), total
