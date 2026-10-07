"""富消息解析：在纯文本对话之外，保留助手到底做了哪些事。

现有的 `load_conversation` 只提取真人消息和助手的文本回复，**工具调用全部丢弃**
（`docs/SKILL.md` 把这条写成了产品边界：实测一条会话丢掉 1683 次工具调用，含
361 次改文件、837 次命令执行）。在电脑上这没问题——用户自己盯着终端；但在手机上
只看到「我改好了」而看不到改了什么，等于让人凭空相信助手。

所以这一层单独解析原始历史，额外产出工具调用摘要。它**不改动**任何现有扫描器的
输出，只是并行的第二种读法；`agent_api` 的字段契约不受影响。

解析口径按各助手真实历史格式对齐（本机实采，非推测）：

- Codex：``response_item`` 下的 ``function_call`` / ``custom_tool_call``，
  结果在同 ``call_id`` 的 ``*_output`` 条目里。``custom_tool_call`` 的 ``input``
  是一段 JS 源码，命令藏在 ``tools.exec_command({...})`` 调用里。
- Claude：assistant 消息 ``content`` 数组里的 ``tool_use``，结果是下一条 user
  消息里同 ``tool_use_id`` 的 ``tool_result``。
- Cursor：assistant 消息 ``content`` 里的 ``tool-call``，结果在 ``role="tool"``
  的 ``tool-result`` 里，按 ``toolCallId`` 关联。
- Pi：JSONL ``message`` 里助手 ``content`` 的 ``toolCall``，结果是
  ``role=toolResult`` 且带 ``toolCallId`` / ``isError``。只走活动分支
  （``active_messages``），不要按字节偏移扫整份文件以免混入已废弃分叉。
- Kimi / OpenCode：暂时回落到纯文本（保持可用，不产出工具卡片）。
  漏登记的助手在桌面预览正常、手机详情却是空白，因为远程层不会回落到扫描器。

拿不准的格式一律降级成「有一次工具调用」，绝不猜测语义——宁可少显示，也不能
在手机上编造助手做过的事。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field

from corral.scan.common import classify_tool

_MAX_DETAIL = 4000
_MAX_OUTPUT = 2000
_MAX_TEXT = 40000
_MAX_WIRE_TOOLS = 32

# 只驱动手机端「问题选项按钮」；词表与分类函数已下沉到 scan.common.classify_tool。
QUESTION_KINDS = {"question"}

# Codex async panel: asked and answered without blocking the turn. Unlike a
# sync overlay, mid-turn commentary and unrelated tool activity must not settle
# it; see pending_prompts_from_messages and request_user_input_async.rs.
_ASYNC_QUESTION_TOOL = "request_user_input_async"

classify = classify_tool


@dataclass
class ToolCall:
    """一次工具调用及其结果，供手机端渲染成可折叠卡片。"""

    call_id: str
    name: str
    kind: str
    summary: str
    detail: str = ""
    status: str = "running"  # running / ok / error
    output: str = ""
    options: list[str] = field(default_factory=list)  # 仅单道提问：可选答案
    # 一次询问里的多道题；有值时不要再读摊平后的 options。
    question_groups: list[dict] = field(default_factory=list)
    # Per-question native shape (prompt, option descriptions, multi-select) for
    # session.prompts / input.question. Cached with the tool; never on the history wire.
    questions_meta: list[dict] = field(default_factory=list)

    def has_body(self) -> bool:
        """Whether detail/output exist server-side (for on-demand fetch)."""
        return bool(self.detail) or bool(self.output)

    def to_summary_dict(self) -> dict:
        """History-wire shape: identity (+ question status), no tool bodies.

        Keep options/questions so live prompts stay actionable without a second
        round trip. Bodies (detail/output) load via session.toolDetail.
        Status is omitted for normal tools — the phone timeline does not show
        success/failure; questions still send status so pending prompts work.
        """
        data = {
            "id": self.call_id,
            "name": self.name,
            "kind": self.kind,
            "summary": self.summary,
            "has_detail": self.has_body(),
        }
        if self.kind in QUESTION_KINDS:
            data["status"] = self.status
        if self.options:
            data["options"] = self.options
        if self.question_groups:
            data["questions"] = self.question_groups
        # Active questions need the prompt body with the options UI.
        if self.kind in QUESTION_KINDS and self.detail:
            data["detail"] = self.detail[:_MAX_DETAIL]
        return data

    def to_dict(self) -> dict:
        data = {
            "id": self.call_id,
            "name": self.name,
            "kind": self.kind,
            "summary": self.summary,
            "has_detail": self.has_body(),
        }
        if self.kind in QUESTION_KINDS:
            data["status"] = self.status
        if self.detail:
            data["detail"] = self.detail
        if self.output:
            data["output"] = self.output
        if self.options:
            data["options"] = self.options
        if self.question_groups:
            data["questions"] = self.question_groups
        if self.questions_meta:
            data["questions_meta"] = self.questions_meta
        return data

    @classmethod
    def from_dict(cls, data: dict) -> ToolCall:
        options = data.get("options")
        groups = data.get("questions")
        meta = data.get("questions_meta")
        # has_detail is wire metadata only; bodies live in detail/output when present.
        return cls(
            call_id=str(data.get("id") or ""),
            name=str(data.get("name") or "tool"),
            kind=str(data.get("kind") or "other"),
            summary=str(data.get("summary") or ""),
            detail=str(data.get("detail") or ""),
            status=str(data.get("status") or "running"),
            output=str(data.get("output") or ""),
            options=list(options) if isinstance(options, list) else [],
            question_groups=[g for g in groups if isinstance(g, dict)]
            if isinstance(groups, list)
            else [],
            questions_meta=[q for q in meta if isinstance(q, dict)]
            if isinstance(meta, list)
            else [],
        )


@dataclass
class RichMessage:
    """聊天流里的一条。``tools`` 非空时表示这一条里助手做了哪些事。"""

    seq: int
    role: str  # user / assistant
    text: str = ""
    timestamp: float | None = None
    tools: list[ToolCall] = field(default_factory=list)

    def to_dict(self) -> dict:
        data: dict = {"seq": self.seq, "role": self.role}
        if self.text:
            data["text"] = self.text[:_MAX_TEXT]
        if self.timestamp is not None:
            data["ts"] = self.timestamp
        if self.tools:
            data["tools"] = [t.to_dict() for t in self.tools]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> RichMessage:
        raw_tools = data.get("tools")
        tools = [
            ToolCall.from_dict(item)
            for item in raw_tools
            if isinstance(item, dict)
        ] if isinstance(raw_tools, list) else []
        timestamp = data.get("ts")
        try:
            parsed_ts = float(timestamp) if timestamp is not None else None
        except (TypeError, ValueError):
            parsed_ts = None
        try:
            seq = int(data.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        return cls(
            seq=seq,
            role=str(data.get("role") or "assistant"),
            text=str(data.get("text") or ""),
            timestamp=parsed_ts,
            tools=tools,
        )

    def to_wire_dict(self) -> dict:
        """Mobile history payload: text + tool summaries, not tool bodies.

        Tool detail/output are fetched on demand via session.toolDetail so a
        tool-heavy turn does not inflate first paint.
        """
        data: dict = {"seq": self.seq, "role": self.role}
        if self.text:
            data["text"] = self.text[:_MAX_TEXT]
        if self.timestamp is not None:
            data["ts"] = self.timestamp
        if not self.tools:
            return data
        summaries = [tool.to_summary_dict() for tool in self.tools]
        if len(summaries) <= _MAX_WIRE_TOOLS:
            data["tools"] = summaries
            return data
        head = _MAX_WIRE_TOOLS - 8
        data["tools"] = summaries[:head] + summaries[-8:]
        data["tools_truncated"] = len(summaries) - len(data["tools"])
        return data

    def tool_detail_page(
        self,
        *,
        tool_id: str | None = None,
        offset: int = 0,
        limit: int = _MAX_WIRE_TOOLS,
    ) -> dict:
        """Bounded tool bodies for one message (on-demand sheet / expand)."""
        bounded = max(1, min(limit or _MAX_WIRE_TOOLS, _MAX_WIRE_TOOLS))
        start = max(0, offset)
        if tool_id:
            matched = [tool for tool in self.tools if tool.call_id == tool_id]
            wire = [tool.to_dict() for tool in matched]
            return {
                "seq": self.seq,
                "tools": wire,
                "offset": 0,
                "has_more": False,
                "total": len(matched),
            }
        slice_tools = self.tools[start : start + bounded]
        wire = [tool.to_dict() for tool in slice_tools]
        return {
            "seq": self.seq,
            "tools": wire,
            "offset": start,
            "has_more": start + len(slice_tools) < len(self.tools),
            "total": len(self.tools),
        }


# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------

def _clip(text: object, limit: int) -> str:
    value = "" if text is None else str(text)
    value = value.strip()
    return value if len(value) <= limit else value[:limit] + "…"


# 手机聊天只给人话。导出和桌面右栏仍走扫描器原文。
# 不要把「对本仓库做 code review」这类真人可见提问算进来：那是技能默认词，也是真实会话。
_PHONE_INJECTED_USER_PREFIXES = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<user_info>",
    "<turn_aborted>",
    "<subagent_notification>",
    "<user_action>",
    "<task-notification>",
    "<local-command",
    "<command-name>",
    "<command-message>",
    "<system-reminder>",
)

# 整句出现在正文里才算注入；不要用「做一次 code review」这种也可能是真人提问的前缀。
_PHONE_INJECTED_USER_MARKERS = (
    "Briefly inform the user about the task result",
    "Implement the plan as specified, it is attached for your reference",
    "Do NOT edit the plan file itself",
    "To-do's from the plan have already been created",
    "【本轮回复契约】",
)
# 注意：corral 自身的跨运行时接力提示词视作真人提问，不在这里过滤。


def _phone_injected_user(text: str) -> bool:
    """这条 user 轮次在手机上应被丢掉，避免系统说明伪装成第一句人话。"""
    stripped = (text or "").lstrip()
    if stripped.startswith(_PHONE_INJECTED_USER_PREFIXES):
        return True
    return any(marker in stripped for marker in _PHONE_INJECTED_USER_MARKERS)


def _basename(path: object) -> str:
    text = str(path or "").strip()
    return os.path.basename(text.rstrip("/")) or text


def _first_line(text: object, limit: int = 120) -> str:
    value = str(text or "").strip().splitlines()
    return _clip(value[0] if value else "", limit)


# 命令开头常见的环境准备语句：把它们当摘要毫无信息量（实测 Cursor 侧几乎每条
# 命令都以 `export PATH=…` 开头，摘要清一色相同，用户完全看不出跑了什么）。
_NOISE_COMMAND_PREFIX = ("export ", "set -", "cd ", "#", "source ", "unset ", "PATH=")


def _command_summary(text: object, limit: int = 160) -> str:
    """从一段可能多行的命令里挑出最能说明「这是在干什么」的一行。"""
    lines = [line.strip() for line in str(text or "").splitlines()]
    meaningful = [
        line
        for line in lines
        if line and not line.startswith(_NOISE_COMMAND_PREFIX) and line not in ("&&", "||")
    ]
    return _clip((meaningful or [line for line in lines if line] or [""])[0], limit)


def summarize(name: str, kind: str, args: dict | str) -> tuple[str, str]:
    """把工具参数压成 (一行摘要, 展开详情)。参数结构千奇百怪，取不到就退回工具名。"""
    detail = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, indent=2)
    detail = _clip(detail, _MAX_DETAIL)
    if not isinstance(args, dict):
        return (f"{name}: {_first_line(args)}" if args else name), detail
    for key in ("file_path", "path", "filePath", "notebook_path"):
        if args.get(key):
            return f"{name} {_basename(args[key])}", detail
    for key in ("cmd", "command", "script"):
        if args.get(key):
            return _command_summary(args[key]), detail
    for key in ("pattern", "query", "q", "search_term"):
        if args.get(key):
            return f"{name} {_first_line(args[key], 100)}", detail
    for key in ("prompt", "instruction", "description", "question"):
        if args.get(key):
            return f"{name} {_first_line(args[key], 100)}", detail
    return name, detail


def _option_label(item: object) -> str:
    if isinstance(item, str):
        return _clip(item, 80)
    if isinstance(item, dict):
        return _clip(
            item.get("label") or item.get("title") or item.get("text") or item.get("name") or "",
            80,
        )
    return ""


def _extract_question_groups(args: dict) -> list[dict]:
    """把提问参数收成「一道题一组」。多道题禁止摊成一份选项列表。"""
    if not isinstance(args, dict):
        return []
    raw_questions = args.get("questions")
    if isinstance(raw_questions, list) and raw_questions and isinstance(raw_questions[0], dict):
        groups: list[dict] = []
        for item in raw_questions:
            if not isinstance(item, dict):
                continue
            title = _clip(
                item.get("question") or item.get("prompt") or item.get("header") or "",
                200,
            )
            nested = item.get("options") if isinstance(item.get("options"), list) else item.get("choices")
            labels = [_option_label(entry) for entry in nested] if isinstance(nested, list) else []
            labels = [label for label in labels if label]
            if title or labels:
                groups.append({"summary": title, "options": labels})
        return groups
    title = _clip(str(args.get("question") or args.get("prompt") or ""), 200)
    for key in ("options", "choices"):
        raw = args.get(key)
        if not isinstance(raw, list):
            continue
        labels = [_option_label(entry) for entry in raw]
        labels = [label for label in labels if label]
        if labels:
            return [{"summary": title, "options": labels}]
    return []


def _question_fields(kind: str, args: dict) -> tuple[list[str], list[dict]]:
    """单道题写入 options；多道题只写入 question_groups，避免摊平。"""
    if kind not in QUESTION_KINDS:
        return [], []
    groups = _extract_question_groups(args)
    if not groups:
        return [], []
    if len(groups) == 1:
        return list(groups[0].get("options") or []), []
    return [], groups


def _question_meta(kind: str, args: dict) -> list[dict]:
    """Native per-question shape. Option ids are native positions (keystroke order)."""
    if kind not in QUESTION_KINDS or not isinstance(args, dict):
        return []
    raw = args.get("questions")
    items = raw if isinstance(raw, list) and raw and isinstance(raw[0], dict) else [args]
    meta: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        prompt = _clip(
            item.get("question") or item.get("prompt") or item.get("title") or item.get("header") or "",
            1000,
        )
        header = _clip(item.get("header") or "", 80)
        nested = item.get("options") if isinstance(item.get("options"), list) else item.get("choices")
        options: list[dict] = []
        for index, entry in enumerate(nested if isinstance(nested, list) else []):
            label = _option_label(entry)
            if not label:
                continue
            option = {"id": str(index), "label": label}
            if isinstance(entry, dict) and entry.get("description"):
                option["description"] = _clip(entry.get("description"), 300)
            options.append(option)
        if not prompt and not options:
            continue
        meta.append(
            {
                "id": str(len(meta)),
                "prompt": prompt,
                "header": header if header != prompt else "",
                "multi_select": bool(
                    item.get("multiSelect") or item.get("multi_select") or item.get("multiple")
                ),
                "is_secret": bool(item.get("isSecret") or item.get("is_secret")),
                # Claude's preview picker has no free-text row; typed text is a note on a choice.
                "custom_needs_choice": any(
                    isinstance(entry, dict) and entry.get("preview")
                    for entry in (nested if isinstance(nested, list) else [])
                ),
                "options": options,
            }
        )
    return meta


def _extract_options(args: dict) -> list[str]:
    """单道提问的候选答案。多道题返回空列表，改走 question_groups。"""
    options, _groups = _question_fields("question", args)
    return options


def _result_text(value: object) -> str:
    """各家的工具结果结构不同：字符串、片段数组、带 output 的对象都见过。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return _clip(value, _MAX_OUTPUT)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return _clip("\n".join(p for p in parts if p), _MAX_OUTPUT)
    if isinstance(value, dict):
        for key in ("text", "output", "content", "result", "stdout"):
            if value.get(key):
                return _result_text(value[key])
        return _clip(json.dumps(value, ensure_ascii=False), _MAX_OUTPUT)
    return _clip(str(value), _MAX_OUTPUT)


_FAILURE_RE = re.compile(
    r"^(?:exit code:?\s*[1-9]|error:|traceback \(most recent call last\)|command failed|fatal:)",
    re.I | re.M,
)


def _looks_failed(text: str) -> bool:
    """只在助手没有显式给出成功/失败信号时，退而求其次从结果文本里判断。

    判据必须严格到「几乎不会误报」：手机上把一次正常执行标成红色失败，比不标
    颜色更糟——用户会因此以为出了事而白跑一趟。所以只认行首出现的明确失败标记，
    不认正文里偶然出现的 error 字样（实测有命令把 error 当普通输出打印）。
    """
    return bool(_FAILURE_RE.search(text[:600]))


# ---------------------------------------------------------------------------
# 增量读取
# ---------------------------------------------------------------------------

# 与 sessions.MESSAGE_PAGE_LIMIT 对齐；richmsg 不能反向导入 sessions。
_DEFAULT_WINDOW = 80
# 尾部窗口左侧还有未读字节时，序号从这里起，给向前翻页留出更小序号。
_TAIL_SEQ_BASE = 1_000_000
_JSONL_RUNTIMES = frozenset({"codex", "claude"})
_CURSOR_TAIL_BATCH = 128


class RichReader:
    """按会话保存读取进度，`poll()` 只返回上次之后新增的消息。

    对 JSONL 记住字节偏移；对 Cursor 的 SQLite 记住 rowid。第一次打开只解析
    文件尾部足够填满一页的记录，游标停在末尾，向前翻页再从左缘偏移补读。
    文件被截断或换掉（新会话复用同一路径）时自动整轮重读，不会卡在错误的偏移上。
    """

    def __init__(self, session: dict) -> None:
        self.session = dict(session)
        self.runtime_id = str(session.get("source") or "")
        self.path = str(session.get("path") or "")
        self._offset = 0
        self._rowid = 0
        self._seq = 0
        self._size = 0
        self._pending: dict[str, ToolCall] = {}
        # call_id → 宿主助手消息：结果回填后要按原 seq 再推一次，手机端才能合并状态。
        self._host_by_call: dict[str, RichMessage] = {}
        self._earliest_offset = 0
        self._earliest_rowid = 0
        self._has_earlier = False
        self._unmatched_results = 0
        self._read_until: int | None = None
        self.parsed_line_count = 0
        self._pi_fps: dict[int, tuple] = {}
        # SessKit-backed Pi projection state. The opaque cursor is persisted
        # via export/restore without interpreting its bytes; the event reader
        # itself is process-local and reopened on demand.
        self._pi_cursor: str | None = None
        self._pi_reader: object | None = None
        self._pi_by_mid: dict[str, RichMessage] = {}
        self._pi_live = False
        # SessKit-backed Cursor/OpenCode projection state (P2). Same cursor
        # discipline as the Pi/Claude/Codex paths: the opaque cursor is
        # persisted without interpreting its bytes; the event reader is
        # process-local and reopened on demand. One runtime per RichReader,
        # so Cursor and OpenCode share this slot. `_co_full` is the
        # materialized full message list with stable global seqs; tail and
        # earlier windows slice it. Projection itself goes through P1's
        # shared `_project_typed_batch` (P1-owned, not modified here).
        self._co_cursor: str | None = None
        self._co_reader: object | None = None
        self._co_gen: str | None = None
        self._co_fps: dict[int, tuple] = {}
        self._co_full: list[RichMessage] | None = None
        self._co_floor: int | None = None
        # SessKit-backed Claude/Codex projection state (P1). Same cursor
        # discipline as Pi: the opaque cursor is persisted without
        # interpreting its bytes; the event reader is process-local and
        # reopened on demand. `_cc_full` is the materialized full message
        # list with stable global seqs; tail/earlier windows slice it.
        self._cc_cursor: str | None = None
        self._cc_reader: object | None = None
        self._cc_gen: str | None = None
        self._cc_fps: dict[int, tuple] = {}
        self._cc_full: list[RichMessage] | None = None
        self._cc_floor: int | None = None
        self._replacement_messages: list[RichMessage] | None = None

    def take_replacement(self) -> list[RichMessage] | None:
        """Consume a rematerialized generation, including an empty history."""
        replacement = self._replacement_messages
        self._replacement_messages = None
        return replacement

    def reset(self) -> None:
        self._replacement_messages = None
        self._offset = 0
        self._rowid = 0
        self._seq = 0
        self._size = 0
        self._pending = {}
        self._host_by_call = {}
        self._earliest_offset = 0
        self._earliest_rowid = 0
        self._has_earlier = False
        self._unmatched_results = 0
        self._read_until = None
        self.parsed_line_count = 0
        self._pi_fps = {}
        self._pi_cursor = None
        self._pi_reader = None
        self._pi_by_mid = {}
        self._pi_live = False
        self._co_cursor = None
        self._co_reader = None
        self._co_gen = None
        self._co_fps = {}
        self._co_full = None
        self._co_floor = None
        self._cc_cursor = None
        self._cc_reader = None
        self._cc_gen = None
        self._cc_fps = {}
        self._cc_full = None
        self._cc_floor = None

    def has_earlier(self) -> bool:
        """尾部窗口左侧是否还有未解析的历史。"""
        return bool(self._has_earlier)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _register_tool(self, host: RichMessage, tool: ToolCall) -> None:
        if not tool.call_id:
            return
        self._pending[tool.call_id] = tool
        self._host_by_call[tool.call_id] = host

    def _finish_tool(
        self,
        call_id: str,
        *,
        output: object,
        failed: bool | None = None,
    ) -> RichMessage | None:
        """回填工具结果；返回需要重新推送的宿主消息（无则 None）。"""
        tool = self._pending.pop(call_id, None)
        if tool is None:
            self._unmatched_results += 1
            return None
        tool.output = _result_text(output)
        if failed is None:
            tool.status = "error" if _looks_failed(tool.output) else "ok"
        else:
            tool.status = "error" if failed or _looks_failed(tool.output) else "ok"
        return self._host_by_call.get(call_id)

    def poll(self) -> list[RichMessage]:
        parser = _PARSERS.get(self.runtime_id)
        if parser is None:
            return []
        try:
            return parser(self)
        except (OSError, sqlite3.Error, ValueError):
            # 历史文件正被写入或格式异常：这一轮当作没有新消息，下一轮再试。
            return []

    def read_all(self, limit: int | None = None) -> list[RichMessage]:
        """缓存未命中时的第一次打开：只解析尾部窗口，不要从文件头读到尾。"""
        self.reset()
        target = limit if limit is not None else _DEFAULT_WINDOW
        try:
            if self.runtime_id in _CC_SESSKIT_RUNTIMES and _sesskit_reader_available(
                self.runtime_id
            ):
                return _read_cc_tail(self, target)
            if self.runtime_id in _CO_SESSKIT_RUNTIMES and _sesskit_reader_available(
                self.runtime_id
            ):
                return _read_co_tail(self, target)
            if self.runtime_id in _JSONL_RUNTIMES:
                return self._read_jsonl_tail(target)
            if self.runtime_id == "cursor":
                return self._read_cursor_tail(target)
            return self.poll()
        except (OSError, sqlite3.Error, ValueError):
            return []

    def read_earlier(self, limit: int, *, before_seq: int) -> list[RichMessage]:
        """从已解析窗口左缘再向前读一块，序号排在 ``before_seq`` 之前。"""
        if not self._has_earlier:
            return []
        target = max(1, limit or _DEFAULT_WINDOW)
        try:
            if self.runtime_id in _CC_SESSKIT_RUNTIMES and _sesskit_reader_available(
                self.runtime_id
            ):
                # SessKit-backed windows carry stable global seqs; the slice
                # already ends before ``before_seq`` — no shifting needed.
                return _read_cc_earlier(self, target, before_seq=before_seq)
            if self.runtime_id in _CO_SESSKIT_RUNTIMES and _sesskit_reader_available(
                self.runtime_id
            ):
                return _read_co_earlier(self, target, before_seq=before_seq)
            if self.runtime_id in _JSONL_RUNTIMES:
                messages = self._read_jsonl_earlier(target)
            elif self.runtime_id == "cursor":
                messages = self._read_cursor_earlier(target)
            else:
                return []
        except (OSError, sqlite3.Error, ValueError):
            return []
        _shift_seqs(messages, before_seq - 1)
        return messages

    def export_state(self) -> dict:
        return {
            "offset": self._offset,
            "rowid": self._rowid,
            "seq": self._seq,
            "size": self._size,
            "pending": {call_id: tool.to_dict() for call_id, tool in self._pending.items()},
            "host_seq": {call_id: host.seq for call_id, host in self._host_by_call.items()},
            "earliest_offset": self._earliest_offset,
            "earliest_rowid": self._earliest_rowid,
            "has_earlier": self._has_earlier,
            "pi_fps": getattr(self, "_pi_fps", {}) or {},
            "pi_cursor": getattr(self, "_pi_cursor", None),
            # SessKit-backed Claude/Codex projection: opaque cursor plus the
            # generation and message fingerprints needed for reset diffing.
            "cc_cursor": getattr(self, "_cc_cursor", None),
            "cc_gen": getattr(self, "_cc_gen", None),
            "cc_fps": getattr(self, "_cc_fps", {}) or {},
            "cc_floor": getattr(self, "_cc_floor", None),
            # SessKit-backed Cursor/OpenCode projection (P2): opaque cursor
            # plus the generation, fingerprints, and tail floor for reset
            # diffing and earlier paging.
            "co_cursor": getattr(self, "_co_cursor", None),
            "co_gen": getattr(self, "_co_gen", None),
            "co_fps": getattr(self, "_co_fps", {}) or {},
            "co_floor": getattr(self, "_co_floor", None),
        }

    def restore_state(self, state: dict, messages: list[RichMessage]) -> None:
        """从缓存恢复读取游标，并把未完成的工具调用重新挂回宿主消息。"""
        try:
            self._offset = int(state.get("offset") or 0)
            self._rowid = int(state.get("rowid") or 0)
            self._seq = int(state.get("seq") or 0)
            self._size = int(state.get("size") or 0)
            self._earliest_offset = int(state.get("earliest_offset") or 0)
            self._earliest_rowid = int(state.get("earliest_rowid") or 0)
            self._has_earlier = bool(state.get("has_earlier"))
        except (TypeError, ValueError):
            self.reset()
            return
        raw_fps = state.get("pi_fps") if isinstance(state.get("pi_fps"), dict) else {}
        restored_fps: dict[int, tuple] = {}
        for key, value in raw_fps.items():
            try:
                restored_fps[int(key)] = _normalize_pi_fp(value)
            except (TypeError, ValueError):
                continue
        # Old caches omit pi_fps; seed from restored messages so the next poll
        # does not re-push every turn as a "tool status changed" delta.
        if not restored_fps and messages and self.runtime_id == "pi":
            restored_fps = {item.seq: _pi_fingerprint(item) for item in messages}
        self._pi_fps = restored_fps
        # The SessKit cursor resumes incrementally; the remounted pending tools
        # below keep tool-result pairing working across the restart. Message
        # grouping for already-pushed turns is not needed again: one native
        # entry carries all of its parts, so post-restore polls only open new
        # message ids. A cursor mismatch still rebuilds via fingerprint diff.
        raw_cursor = state.get("pi_cursor")
        self._pi_cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
        self._pi_reader = None
        self._pi_by_mid = {}
        self._pi_live = self.runtime_id == "pi"
        raw_cc_cursor = state.get("cc_cursor")
        self._cc_cursor = (
            raw_cc_cursor if isinstance(raw_cc_cursor, str) and raw_cc_cursor else None
        )
        raw_cc_gen = state.get("cc_gen")
        self._cc_gen = raw_cc_gen if isinstance(raw_cc_gen, str) and raw_cc_gen else None
        raw_cc_fps = state.get("cc_fps") if isinstance(state.get("cc_fps"), dict) else {}
        restored_cc_fps: dict[int, tuple] = {}
        for key, value in raw_cc_fps.items():
            try:
                restored_cc_fps[int(key)] = _normalize_pi_fp(value)
            except (TypeError, ValueError):
                continue
        if not restored_cc_fps and messages and self.runtime_id in _CC_SESSKIT_RUNTIMES:
            restored_cc_fps = {item.seq: _pi_fingerprint(item) for item in messages}
        self._cc_fps = restored_cc_fps
        try:
            self._cc_floor = (
                int(state["cc_floor"]) if state.get("cc_floor") is not None else None
            )
        except (TypeError, ValueError):
            self._cc_floor = None
        self._cc_reader = None
        self._cc_full = None
        raw_co_cursor = state.get("co_cursor")
        self._co_cursor = (
            raw_co_cursor if isinstance(raw_co_cursor, str) and raw_co_cursor else None
        )
        raw_co_gen = state.get("co_gen")
        self._co_gen = raw_co_gen if isinstance(raw_co_gen, str) and raw_co_gen else None
        raw_co_fps = state.get("co_fps") if isinstance(state.get("co_fps"), dict) else {}
        restored_co_fps: dict[int, tuple] = {}
        for key, value in raw_co_fps.items():
            try:
                restored_co_fps[int(key)] = _normalize_pi_fp(value)
            except (TypeError, ValueError):
                continue
        if not restored_co_fps and messages and self.runtime_id in _CO_SESSKIT_RUNTIMES:
            restored_co_fps = {item.seq: _pi_fingerprint(item) for item in messages}
        self._co_fps = restored_co_fps
        try:
            self._co_floor = (
                int(state["co_floor"]) if state.get("co_floor") is not None else None
            )
        except (TypeError, ValueError):
            self._co_floor = None
        self._co_reader = None
        self._co_full = None
        by_seq = {item.seq: item for item in messages}
        pending_raw = state.get("pending") if isinstance(state.get("pending"), dict) else {}
        host_seq_raw = state.get("host_seq") if isinstance(state.get("host_seq"), dict) else {}
        self._pending = {}
        self._host_by_call = {}
        for call_id, tool_data in pending_raw.items():
            if not isinstance(tool_data, dict):
                continue
            cid = str(call_id)
            try:
                host_seq = int(host_seq_raw.get(cid) or 0)
            except (TypeError, ValueError):
                host_seq = 0
            host = by_seq.get(host_seq)
            tool = None
            if host is not None:
                tool = next((item for item in host.tools if item.call_id == cid), None)
                self._host_by_call[cid] = host
            if tool is None:
                tool = ToolCall.from_dict(tool_data)
            self._pending[cid] = tool

    def _read_jsonl_tail(self, limit: int) -> list[RichMessage]:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size <= 0:
            self._size = 0
            return []
        budget = _JSONL_CHUNK
        messages: list[RichMessage] = []
        start = 0
        while True:
            start = _jsonl_aligned_start(self.path, size, budget)
            self._offset = start
            self._seq = _TAIL_SEQ_BASE if start > 0 else 0
            self._pending = {}
            self._host_by_call = {}
            self._unmatched_results = 0
            self._size = 0
            self._read_until = None
            self.parsed_line_count = 0
            messages = self.poll()
            # Prefer enough text/messages for first paint. Do not keep doubling
            # the read window solely to pair historical tool results — bodies
            # load on demand and unpaired tools may stay "running" until later.
            if len(messages) >= limit or start == 0:
                break
            if budget >= size:
                start = 0
                self._offset = 0
                self._seq = 0
                self._pending = {}
                self._host_by_call = {}
                self._unmatched_results = 0
                self._size = 0
                self.parsed_line_count = 0
                messages = self.poll()
                break
            budget = min(size, budget * 2)
        self._earliest_offset = start
        self._has_earlier = start > 0
        return messages

    def _read_jsonl_earlier(self, limit: int) -> list[RichMessage]:
        end = self._earliest_offset
        if end <= 0:
            self._has_earlier = False
            return []
        budget = _JSONL_CHUNK
        messages: list[RichMessage] = []
        start = end
        while True:
            start = _jsonl_aligned_start(self.path, end, budget)
            messages = self._parse_jsonl_slice(start, end)
            if len(messages) >= limit or start == 0:
                break
            if budget >= end:
                start = 0
                messages = self._parse_jsonl_slice(0, end)
                break
            budget = min(end, budget * 2)
        self._earliest_offset = start
        self._has_earlier = start > 0
        return messages

    def _parse_jsonl_slice(self, start: int, end: int) -> list[RichMessage]:
        parser = _PARSERS.get(self.runtime_id)
        if parser is None or start >= end:
            self._unmatched_results = 0
            return []
        saved = (
            self._offset,
            self._seq,
            self._size,
            self._pending,
            self._host_by_call,
            self._read_until,
        )
        try:
            self._offset = start
            self._seq = 0
            self._pending = {}
            self._host_by_call = {}
            self._unmatched_results = 0
            self._read_until = end
            return parser(self)
        finally:
            self._offset, self._seq, self._size, self._pending, self._host_by_call, self._read_until = saved

    def _read_cursor_tail(self, limit: int) -> list[RichMessage]:
        from corral.scan.cursor import connect_store_ro

        db_path = _cursor_db_path(self.path)
        if not os.path.exists(db_path):
            return []
        conn = connect_store_ro(db_path)
        if conn is None:
            return []
        fetch_limit = max(limit * 4, _CURSOR_TAIL_BATCH)
        messages: list[RichMessage] = []
        has_earlier = False
        min_rowid = 0
        max_rowid = 0
        try:
            conn.row_factory = None
            while True:
                rows = conn.execute(
                    "SELECT rowid, data FROM blobs "
                    "WHERE substr(data, 1, 1) = X'7B' "
                    "ORDER BY rowid DESC LIMIT ?",
                    (fetch_limit,),
                ).fetchall()
                rows = list(reversed(rows))
                min_rowid, max_rowid, has_earlier = _cursor_window_bounds(conn, rows)
                self._seq = _TAIL_SEQ_BASE if has_earlier else 0
                self._pending = {}
                self._host_by_call = {}
                self._unmatched_results = 0
                self.parsed_line_count = 0
                messages = _cursor_consume_rows(self, rows)
                unpaired = self._unmatched_results > 0
                if (len(messages) >= limit and not unpaired) or not has_earlier:
                    break
                if fetch_limit >= 1_000_000:
                    break
                fetch_limit *= 2
        except sqlite3.Error:
            conn.close()
            return []
        conn.close()
        self._rowid = max_rowid
        self._earliest_rowid = min_rowid
        self._has_earlier = has_earlier
        return messages

    def _read_cursor_earlier(self, limit: int) -> list[RichMessage]:
        from corral.scan.cursor import connect_store_ro

        if self._earliest_rowid <= 0 and not self._has_earlier:
            self._has_earlier = False
            return []
        db_path = _cursor_db_path(self.path)
        if not os.path.exists(db_path):
            self._has_earlier = False
            return []
        conn = connect_store_ro(db_path)
        if conn is None:
            return []
        fetch_limit = max(limit * 4, _CURSOR_TAIL_BATCH)
        messages: list[RichMessage] = []
        has_earlier = False
        min_rowid = self._earliest_rowid
        try:
            conn.row_factory = None
            while True:
                rows = conn.execute(
                    "SELECT rowid, data FROM blobs "
                    "WHERE rowid < ? AND substr(data, 1, 1) = X'7B' "
                    "ORDER BY rowid DESC LIMIT ?",
                    (self._earliest_rowid, fetch_limit),
                ).fetchall()
                rows = list(reversed(rows))
                min_rowid, _, has_earlier = _cursor_window_bounds(conn, rows)
                messages = self._parse_cursor_slice(rows)
                unpaired = self._unmatched_results > 0
                if (len(messages) >= limit and not unpaired) or not has_earlier:
                    break
                if not rows or fetch_limit >= 1_000_000:
                    break
                fetch_limit *= 2
        except sqlite3.Error:
            conn.close()
            return []
        conn.close()
        if min_rowid:
            self._earliest_rowid = min_rowid
        self._has_earlier = has_earlier
        return messages

    def _parse_cursor_slice(self, rows: list) -> list[RichMessage]:
        saved = (
            self._offset,
            self._rowid,
            self._seq,
            self._pending,
            self._host_by_call,
        )
        try:
            self._seq = 0
            self._pending = {}
            self._host_by_call = {}
            self._unmatched_results = 0
            return _cursor_consume_rows(self, rows)
        finally:
            self._offset, self._rowid, self._seq, self._pending, self._host_by_call = saved


_JSONL_CHUNK = 256 * 1024
_JSONL_YIELD_SECONDS = 0.002


def _shift_seqs(messages: list[RichMessage], target_last: int) -> None:
    """把一段新解析的消息序号平移到 ``target_last`` 结尾，不改相对顺序。"""
    if not messages:
        return
    delta = target_last - messages[-1].seq
    if delta == 0:
        return
    for item in messages:
        item.seq += delta


def _jsonl_aligned_start(path: str, end: int, budget: int) -> int:
    """从 ``end`` 向前取一块，落到第一条完整行的起始字节。"""
    if end <= 0:
        return 0
    budget = max(int(budget), 1)
    while True:
        raw_start = max(0, end - budget)
        try:
            with open(path, "rb") as handle:
                handle.seek(raw_start)
                data = handle.read(end - raw_start)
        except OSError:
            return 0
        if raw_start == 0:
            return 0
        newline = data.find(b"\n")
        if newline >= 0:
            return raw_start + newline + 1
        if budget >= end:
            return 0
        budget = min(end, budget * 2)


def _iter_new_jsonl(reader: RichReader):
    """从上次的字节偏移继续读；只吐出完整的行，半行留到下轮。

    按块读而不是一次 ``read()`` 整份剩余内容：大历史解析时周期性让出 GIL，
    中继心跳才应答得及，手机 20 秒超时才不会把整条连接掐死。
    ``_read_until`` 有值时只读到该偏移（向前翻页），不改截断检测与文件尺寸。
    """
    try:
        size = os.path.getsize(reader.path)
    except OSError:
        return
    slice_end = reader._read_until
    if slice_end is None:
        if size < reader._size:  # 文件被截断/替换
            reader.reset()
        reader._size = size
        end = size
        if size <= reader._offset:
            return
    else:
        end = min(slice_end, size)
        if reader._offset >= end:
            return
    leftover = b""
    with open(reader.path, "rb") as handle:
        handle.seek(reader._offset)
        while reader._offset < end:
            chunk = handle.read(min(_JSONL_CHUNK, end - reader._offset))
            if not chunk:
                break
            data = leftover + chunk
            lines = data.splitlines(keepends=True)
            if lines and not lines[-1].endswith(b"\n"):
                leftover = lines.pop()
            else:
                leftover = b""
            for raw in lines:
                reader._offset += len(raw)
                stripped = raw.strip()
                if not stripped:
                    continue
                reader.parsed_line_count += 1
                try:
                    yield json.loads(stripped.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
            if len(chunk) >= _JSONL_CHUNK:
                time.sleep(_JSONL_YIELD_SECONDS)


# --- Codex ----------------------------------------------------------------

_EXEC_CMD_RE = re.compile(r'exec_command\(\s*\{.*?"cmd"\s*:\s*"((?:[^"\\]|\\.)*)"', re.S)


def _codex_custom_input(raw: str) -> tuple[str, str]:
    """`custom_tool_call` 的 input 是一段 JS，真正的命令埋在 exec_command 参数里。"""
    match = _EXEC_CMD_RE.search(raw or "")
    if not match:
        return _first_line(raw, 160), _clip(raw, _MAX_DETAIL)
    try:
        command = json.loads(f'"{match.group(1)}"')
    except ValueError:
        command = match.group(1)
    return _first_line(command, 160), _clip(command, _MAX_DETAIL)


def _parse_codex_legacy(reader: RichReader) -> list[RichMessage]:
    """Fallback for SessKit builds without ``open_reader``; marked for removal.

    Only reached when ``_sesskit_reader_available("codex")`` is false. Do not
    extend: all new Codex phone behavior goes through the SessKit projection.
    Remove once the minimum SessKit version provides the reader API.
    """
    from corral.scan.codex import (
        assistant_message_text,
        task_complete_error_text,
        user_message_text,
    )

    messages: list[RichMessage] = []

    def attach(tool: ToolCall) -> None:
        if messages and messages[-1].role == "assistant":
            messages[-1].tools.append(tool)
        else:
            messages.append(RichMessage(reader._next_seq(), "assistant", "", None, [tool]))

    def append_chat(role: str, text: str, timestamp: float | None) -> None:
        clipped = _clip(text, _MAX_TEXT)
        if not clipped or _phone_injected_user(clipped):
            return
        if messages and messages[-1].role == role and messages[-1].text == clipped:
            return
        messages.append(RichMessage(reader._next_seq(), role, clipped, timestamp))

    for entry in _iter_new_jsonl(reader):
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        kind = payload.get("type")
        timestamp = _codex_time(entry)

        user_text = user_message_text(entry) or _codex_string_role_text(payload, "user")
        if user_text:
            append_chat("user", user_text, timestamp)
            continue
        assistant_text = assistant_message_text(entry)
        if not assistant_text and kind == "agent_message":
            assistant_text = str(payload.get("message") or payload.get("text") or "").strip()
        if not assistant_text and entry.get("type") == "event_msg" and kind == "task_complete":
            assistant_text = str(payload.get("last_agent_message") or "").strip()
            if not assistant_text:
                # Quota / 401 / provider failures complete with null text;
                # keep the error visible instead of dropping the turn.
                assistant_text = task_complete_error_text(payload)
        if assistant_text:
            append_chat("assistant", assistant_text, timestamp)

        if kind == "function_call":
            name = str(payload.get("name") or "tool")
            try:
                args = json.loads(payload.get("arguments") or "{}")
            except ValueError:
                args = payload.get("arguments") or {}
            summary, detail = summarize(name, classify(name), args)
            options, groups = _question_fields(classify(name), args)
            tool = ToolCall(
                call_id=str(payload.get("call_id") or payload.get("id") or ""),
                name=name,
                kind=classify(name),
                summary=summary,
                detail=detail,
                options=options,
                question_groups=groups,
                questions_meta=_question_meta(classify(name), args),
            )
            attach(tool)
            if messages:
                reader._register_tool(messages[-1], tool)
        elif kind == "custom_tool_call":
            name = str(payload.get("name") or "tool")
            summary, detail = _codex_custom_input(str(payload.get("input") or ""))
            tool = ToolCall(
                call_id=str(payload.get("call_id") or payload.get("id") or ""),
                name=name,
                kind=classify(name) if classify(name) != "other" else "shell",
                summary=summary or name,
                detail=detail,
            )
            attach(tool)
            if messages:
                reader._register_tool(messages[-1], tool)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            host = reader._finish_tool(
                str(payload.get("call_id") or ""),
                output=payload.get("output"),
            )
            if host is not None and (not messages or messages[-1] is not host):
                messages.append(host)
    return messages


def _codex_time(entry: dict) -> float | None:
    from corral.scan.codex import entry_time  # 复用既有的时间解析，避免两套口径

    try:
        return entry_time(entry)
    except Exception:
        return None


def _codex_content_text(content: object) -> str:
    if isinstance(content, str):
        return _clip(content, _MAX_TEXT)
    if isinstance(content, list):
        parts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in ("input_text", "output_text", "text")
        ]
        return _clip("\n".join(part for part in parts if part), _MAX_TEXT)
    return ""


def _codex_string_role_text(payload: dict, role: str) -> str:
    """桌面扫描器要求 content 为列表；夹具和少数旧记录把正文写成字符串。"""
    if payload.get("type") != "message" or payload.get("role") != role:
        return ""
    content = payload.get("content")
    if isinstance(content, str):
        return content.strip()
    return ""


def _codex_injected(text: str) -> bool:
    """兼容旧名：Codex 用户注入与各助手共用同一套前缀。"""
    return _phone_injected_user(text)


# --- Claude ---------------------------------------------------------------

def _parse_claude_legacy(reader: RichReader) -> list[RichMessage]:
    """Fallback for SessKit builds without ``open_reader``; marked for removal.

    Only reached when ``_sesskit_reader_available("claude")`` is false. Do not
    extend: all new Claude phone behavior goes through the SessKit projection.
    Remove once the minimum SessKit version provides the reader API.
    """
    from corral.scan.claude import INTERRUPTED_MARKER, entry_time, extract_text
    try:
        from corral.scan.claude import system_error_text
    except ImportError:
        # 随 SessKit 下个版本发布；老包上手机详情暂不显示上游报错，列表不受影响。
        def system_error_text(entry):
            return ""

    def _flush_pending_error() -> None:
        pend = getattr(reader, "_claude_pending_error", None)
        if not pend:
            return
        text, ts = pend
        reader._claude_pending_error = None
        clipped = _clip(text, _MAX_TEXT)
        if clipped and (not messages or messages[-1].role != "assistant" or messages[-1].text != clipped):
            messages.append(RichMessage(reader._next_seq(), "assistant", clipped, ts))

    messages: list[RichMessage] = []
    for entry in _iter_new_jsonl(reader):
        if entry.get("isMeta") or entry.get("isSidechain"):
            continue
        entry_type = entry.get("type")
        if entry_type == "system":
            # 2.1+ 上游报错（401/504/连接失败）：重试连记多条，只留最后一条。
            err_text = system_error_text(entry)
            if err_text:
                try:
                    err_ts = entry_time(entry)
                except Exception:
                    err_ts = None
                reader._claude_pending_error = (err_text, err_ts)
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        try:
            timestamp = entry_time(entry)
        except Exception:
            timestamp = None

        if entry_type == "user":
            # tool_result 挂在 user 轮次下，但不是真人说的话，只用来回填工具状态。
            if isinstance(content, list):
                handled = False
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        handled = True
                        host = reader._finish_tool(
                            str(part.get("tool_use_id") or ""),
                            output=part.get("content"),
                            failed=bool(part.get("is_error")),
                        )
                        if host is not None and (not messages or messages[-1] is not host):
                            messages.append(host)
                if handled:
                    continue
            origin = entry.get("origin")
            if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
                continue
            text = extract_text(content or "")
            clipped = _clip(text or "", _MAX_TEXT)
            if clipped and clipped != INTERRUPTED_MARKER and not _phone_injected_user(clipped):
                _flush_pending_error()
                messages.append(RichMessage(reader._next_seq(), "user", clipped, timestamp))
            continue

        if entry_type != "assistant" or not isinstance(content, list):
            continue
        # Non-empty thinking reads as reply text, matching Claude Code's inline display.
        texts = [
            text
            for part in content
            if isinstance(part, dict) and part.get("type") in ("text", "thinking")
            for text in [(part.get("text") or part.get("thinking") or "").strip()]
            if text
        ]
        tools: list[ToolCall] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "tool_use":
                continue
            name = str(part.get("name") or "tool")
            kind = classify(name)
            args = _tool_args(part.get("input"))
            summary, detail = summarize(name, kind, args)
            options, groups = _question_fields(kind, args)
            tool = ToolCall(
                call_id=str(part.get("id") or ""),
                name=name,
                kind=kind,
                summary=summary,
                detail=detail,
                options=options,
                question_groups=groups,
                questions_meta=_question_meta(kind, args),
            )
            tools.append(tool)
        if texts or tools:
            if texts:
                # 真实正文落地=本轮已恢复，丢掉之前攒的报错。
                reader._claude_pending_error = None
            message = RichMessage(
                reader._next_seq(), "assistant", _clip("\n\n".join(texts), _MAX_TEXT), timestamp, tools
            )
            messages.append(message)
            for tool in tools:
                reader._register_tool(message, tool)
    _flush_pending_error()
    return messages


# --- Cursor ---------------------------------------------------------------

def _cursor_db_path(path: str) -> str:
    return path if path.endswith("store.db") else os.path.join(path, "store.db")


def _cursor_window_bounds(conn: sqlite3.Connection, rows: list) -> tuple[int, int, bool]:
    """返回窗口最小/最大 rowid，以及更早是否还有 JSON 行。"""
    if not rows:
        return 0, 0, False
    min_rowid = int(rows[0][0])
    max_rowid = int(rows[-1][0])
    older = conn.execute(
        "SELECT 1 FROM blobs WHERE rowid < ? AND substr(data, 1, 1) = X'7B' LIMIT 1",
        (min_rowid,),
    ).fetchone()
    return min_rowid, max_rowid, older is not None


def _cursor_consume_rows(reader: RichReader, rows: list) -> list[RichMessage]:
    from corral.scan.cursor import user_text_from_blob

    messages: list[RichMessage] = []
    for rowid, blob in rows:
        reader._rowid = max(reader._rowid, int(rowid))
        reader.parsed_line_count += 1
        if not isinstance(blob, (bytes, bytearray, memoryview)):
            continue
        raw = bytes(blob)
        try:
            entry = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue  # DAG 二进制 blob，跳过
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = entry.get("content")
        if role == "user":
            text = user_text_from_blob(entry)
            clipped = _clip(text or "", _MAX_TEXT)
            if clipped and not _phone_injected_user(clipped):
                messages.append(RichMessage(reader._next_seq(), "user", clipped))
        elif role == "assistant":
            texts, tools = _cursor_assistant(content, reader)
            if texts or tools:
                message = RichMessage(reader._next_seq(), "assistant", texts, None, tools)
                messages.append(message)
                for tool in tools:
                    reader._register_tool(message, tool)
        elif role == "tool" and isinstance(content, list):
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "tool-result":
                    continue
                host = reader._finish_tool(
                    str(part.get("toolCallId") or ""),
                    output=part.get("result"),
                )
                if host is not None and (not messages or messages[-1] is not host):
                    messages.append(host)
    return messages


def _parse_cursor(reader: RichReader) -> list[RichMessage]:
    """Cursor: project SessKit typed activity; legacy native path is the fallback."""
    if _sesskit_reader_available("cursor"):
        return _co_sync(reader, "cursor")
    return _parse_cursor_legacy(reader)


def _parse_cursor_legacy(reader: RichReader) -> list[RichMessage]:
    """Fallback for SessKit builds without ``open_reader``; marked for removal.

    Only reached when `_sesskit_reader_available("cursor")` is false. Do not
    extend: all new Cursor phone behavior goes through the SessKit projection
    above. Remove once the minimum SessKit version provides the reader API.
    """
    from corral.scan.cursor import connect_store_ro

    db_path = _cursor_db_path(reader.path)
    if not os.path.exists(db_path):
        return []
    conn = connect_store_ro(db_path)
    if conn is None:
        return []
    try:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT rowid, data FROM blobs "
            "WHERE rowid > ? AND substr(data, 1, 1) = X'7B' "
            "ORDER BY rowid",
            (reader._rowid,),
        ).fetchall()
    except sqlite3.Error:
        conn.close()
        return []
    messages = _cursor_consume_rows(reader, rows)
    conn.close()
    return messages


def _cursor_assistant(content: object, reader: RichReader) -> tuple[str, list[ToolCall]]:
    if not isinstance(content, list):
        return _clip(content if isinstance(content, str) else "", _MAX_TEXT), []
    texts: list[str] = []
    tools: list[ToolCall] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and (part.get("text") or "").strip():
            texts.append(part["text"].strip())
        elif part.get("type") == "tool-call":
            name = str(part.get("toolName") or "tool")
            kind = classify(name)
            args = part.get("args") if isinstance(part.get("args"), dict) else {}
            summary, detail = summarize(name, kind, args)
            options, groups = _question_fields(kind, args)
            tool = ToolCall(
                call_id=str(part.get("toolCallId") or ""),
                name=name,
                kind=kind,
                summary=summary,
                detail=detail,
                options=options,
                question_groups=groups,
                questions_meta=_question_meta(kind, args),
            )
            tools.append(tool)
    return _clip("\n\n".join(texts), _MAX_TEXT), tools


# --- 其余运行时：回落到纯文本 ----------------------------------------------

def _parse_plain(reader: RichReader) -> list[RichMessage]:
    """Kimi / OpenCode 走桌面同一套纯文本对话；文件变长时补读新句。

    这几家没有独立的增量游标，所以每次都整份重读，只把尚未发出的尾部交给上层。
    会话条数有限，重读成本可接受。禁止在 ``_seq > 0`` 时直接返回空——否则
    正在看的会话追加新回复后手机一直停在旧画面。
    """
    from corral.runtime import default_registry

    registry = default_registry()
    try:
        runtime = registry.get(reader.runtime_id)
        plain = runtime.load_conversation(reader.session)
    except Exception:
        return []
    converted: list[tuple[str, str, float | None]] = []
    for item in plain:
        clipped = _clip(item.text, _MAX_TEXT)
        if not clipped:
            continue
        if item.role == "user" and _phone_injected_user(clipped):
            continue
        converted.append((item.role, clipped, item.timestamp))
    already = reader._seq
    return [
        RichMessage(reader._next_seq(), role, text, timestamp)
        for role, text, timestamp in converted[already:]
    ]


# --- Pi -------------------------------------------------------------------

def _pi_timestamp(item: dict, message: dict) -> float | None:
    from corral.scan.common import parse_timestamp

    return parse_timestamp(item.get("timestamp")) or parse_timestamp(message.get("timestamp"))


def _pi_build_messages(path: str) -> list[RichMessage]:
    """Parse the active Pi branch into rich messages (text + tool cards)."""
    from corral.scan import pi as scan_pi

    built: list[RichMessage] = []
    pending: dict[str, ToolCall] = {}
    host_by_call: dict[str, RichMessage] = {}
    seq = 0

    def next_seq() -> int:
        nonlocal seq
        seq += 1
        return seq

    for item in scan_pi.active_messages(scan_pi.read_entries(path)):
        message = item.get("message")
        if not isinstance(message, dict):
            continue
        timestamp = _pi_timestamp(item, message)
        role = message.get("role")

        if role == "user":
            clipped = _clip(scan_pi.message_text(message.get("content")), _MAX_TEXT)
            if clipped and not _phone_injected_user(clipped):
                built.append(RichMessage(next_seq(), "user", clipped, timestamp))
            continue

        if role == "toolResult":
            call_id = str(message.get("toolCallId") or "")
            tool = pending.pop(call_id, None)
            if tool is None:
                continue
            explicit = message.get("isError")
            if explicit is None and message.get("error"):
                explicit = True
            output = message.get("content")
            tool.output = _result_text(output)
            if isinstance(explicit, bool):
                tool.status = "error" if explicit or _looks_failed(tool.output) else "ok"
            else:
                tool.status = "error" if _looks_failed(tool.output) else "ok"
            continue

        if role != "assistant":
            continue

        content = message.get("content")
        texts: list[str] = []
        tools: list[ToolCall] = []
        if isinstance(content, str):
            if content.strip():
                texts.append(content.strip())
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "text":
                    value = str(part.get("text") or "").strip()
                    if value:
                        texts.append(value)
                elif part_type == "toolCall":
                    name = str(part.get("name") or "tool")
                    kind = classify(name)
                    raw_args = (
                        part.get("arguments")
                        if part.get("arguments") is not None
                        else part.get("input")
                    )
                    args = _tool_args(raw_args)
                    summary, detail = summarize(name, kind, args)
                    options, groups = _question_fields(kind, args)
                    tool = ToolCall(
                        call_id=str(part.get("id") or ""),
                        name=name,
                        kind=kind,
                        summary=summary,
                        detail=detail,
                        options=options,
                        question_groups=groups,
                        questions_meta=_question_meta(kind, args),
                    )
                    tools.append(tool)

        if not texts and not tools:
            # Empty-body failures (rate limit / connection error) still own the turn.
            stop_reason = str(message.get("stopReason") or "").strip()
            error_text = str(message.get("errorMessage") or "").strip()
            if error_text and (stop_reason in {"error", "aborted"} or error_text):
                texts.append(error_text)
            else:
                continue
        host = RichMessage(
            next_seq(),
            "assistant",
            _clip("\n\n".join(texts), _MAX_TEXT),
            timestamp,
            tools,
        )
        built.append(host)
        for tool in tools:
            if not tool.call_id:
                continue
            pending[tool.call_id] = tool
            host_by_call[tool.call_id] = host
    return built


def _pi_fingerprint(message: RichMessage) -> tuple:
    return (
        message.role,
        message.text or "",
        tuple(
            (tool.call_id, tool.status, tool.summary, bool(tool.output), bool(tool.detail))
            for tool in message.tools
        ),
    )


def _normalize_pi_fp(value: object) -> tuple:
    """JSON round-trips turn nested tuples into lists; compare in one shape."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return ("", "", ())
    role, text, tools = value
    tool_fps: list[tuple] = []
    if isinstance(tools, (list, tuple)):
        for item in tools:
            if not isinstance(item, (list, tuple)) or len(item) < 5:
                continue
            tool_fps.append(
                (str(item[0]), str(item[1]), str(item[2]), bool(item[3]), bool(item[4]))
            )
    return (str(role or ""), str(text or ""), tuple(tool_fps))


def _note_replacement(reader: RichReader, previous: dict, fresh: list[RichMessage]) -> None:
    """Sequence slots cannot merge across a changed projection identity."""
    def identity(fingerprint: tuple) -> tuple:
        role, text, tools = fingerprint
        return role, text, tuple(tool[0] for tool in tools)

    current = {item.seq: _pi_fingerprint(item) for item in fresh}
    if previous and any(
        seq not in current or identity(old) != identity(current[seq])
        for seq, old in previous.items()
    ):
        reader._replacement_messages = list(fresh)


# --- Pi via SessKit -------------------------------------------------------

_PI_SESSKIT_READER: bool | None = None


def _pi_reader_available() -> bool:
    """Feature-detect the SessKit incremental Pi reader.

    False on SessKit builds without ``get_adapter("pi").open_reader``; the
    legacy native path below stays as the fallback until the minimum SessKit
    version provides the reader API.
    """
    global _PI_SESSKIT_READER
    if _PI_SESSKIT_READER is None:
        try:
            from sesskit import get_adapter

            adapter = get_adapter("pi")
            _PI_SESSKIT_READER = callable(getattr(adapter, "open_reader", None))
        except Exception:
            _PI_SESSKIT_READER = False
    return _PI_SESSKIT_READER


def _pi_host_for(reader: RichReader, mid: str, role: str, ts: float | None) -> tuple[RichMessage, bool]:
    """Return the grouped host for one native message, creating it if needed."""
    return _typed_host_for(reader._pi_by_mid, mid, role, ts, reader._next_seq)


class _TypedBatch:
    """One projection batch: ordered hosts plus the batch-local tail.

    A batch is one poll window, one backward page, or one full rebuild —
    the same span the legacy native parsers consumed in a single call, so
    batch-local rules (Codex attach-to-previous, text dedup) behave exactly
    like the retired per-slice parsing.
    """

    def __init__(self) -> None:
        self.out: list[RichMessage] = []
        self.seen: set[int] = set()
        self.last: RichMessage | None = None


def _typed_group_key(event: object, runtime: str) -> str:
    """Grouping key for one native message: SessKit ``message_id`` for Pi,
    the native ``line:N`` record for Claude/Codex (their events carry no
    message id, but one native line's events always arrive in one batch)."""
    if runtime == "pi":
        return str(getattr(event, "message_id", None) or "")
    record = getattr(getattr(event, "evidence", None), "record", None)
    return str(record or "")


def _typed_host_for(
    groups: dict[str, RichMessage],
    key: str,
    role: str,
    ts: float | None,
    next_seq,
) -> tuple[RichMessage, bool]:
    """Return the grouped host for one native message, creating it if needed."""
    if key:
        host = groups.get(key)
        if host is not None and host.role == role:
            return host, False
    host = RichMessage(next_seq(), role, "", ts)
    if key:
        groups[key] = host
    return host, True


def _standard_tool_card(event: object) -> ToolCall:
    """Tool card from a typed call's raw input (Pi + Claude shape)."""
    name = str(getattr(event, "name", None) or "tool")
    kind = classify(name)
    args = _tool_args(getattr(event, "raw_input", None))
    summary, detail = summarize(name, kind, args)
    options, groups = _question_fields(kind, args)
    return ToolCall(
        call_id=str(getattr(event, "call_id", None) or ""),
        name=name,
        kind=kind,
        summary=summary,
        detail=detail,
        options=options,
        question_groups=groups,
        questions_meta=_question_meta(kind, args),
    )


def _codex_custom_card(name: str, call_id: str, summary: str, detail: str) -> ToolCall:
    """Legacy custom-tool card: no ``name:`` prefix, no question fields."""
    kind = classify(name)
    if kind == "other":
        kind = "shell"
    return ToolCall(
        call_id=call_id,
        name=name,
        kind=kind,
        summary=summary or name,
        detail=detail,
    )


def _codex_tool_card(event: object) -> ToolCall:
    """Tool card from a typed Codex call, preserving the custom/function split.

    The typed event no longer carries the native payload kind, so the split
    is recovered from the coerced input shape, verified against all 2051
    local Codex histories (counts are snapshots, not prevalence claims):

    - ``str`` input is always a custom ``custom_tool_call`` whose text is not
      JSON (37,849 cases); unparseable ``function_call`` argument strings
      never occur (all 78,435 function argument strings parse, always to a
      dict). Custom semantics: no ``name:`` prefix.
    - ``{"cmd": ...}`` is ambiguous: custom inputs matching the
      ``exec_command`` pattern coerce to it, but so do the 1,177
      ``function_call`` payloads literally named ``exec_command``. The two
      sets are name-disjoint in the sample (customs are only ``exec`` /
      ``apply_patch``), so that name takes the function path with its
      noise-skipping command summary; every other ``{"cmd"}`` dict is custom.
    - Every other dict is function arguments (custom inputs that are valid
      non-``cmd`` JSON never occur); lists fall through to the function path.
    """
    name = str(getattr(event, "name", None) or "tool")
    call_id = str(getattr(event, "call_id", None) or "")
    raw = getattr(event, "raw_input", None)
    if isinstance(raw, str):
        return _codex_custom_card(
            name, call_id, _first_line(raw, 160), _clip(raw, _MAX_DETAIL)
        )
    if (
        isinstance(raw, dict)
        and set(raw) == {"cmd"}
        and isinstance(raw.get("cmd"), str)
        and name != "exec_command"
    ):
        command = raw["cmd"]
        return _codex_custom_card(
            name, call_id, _first_line(command, 160), _clip(command, _MAX_DETAIL)
        )
    args = raw if isinstance(raw, (dict, list)) else {}
    kind = classify(name)
    summary, detail = summarize(name, kind, args)
    options, groups = _question_fields(kind, args) if isinstance(args, dict) else ([], [])
    return ToolCall(
        call_id=call_id,
        name=name,
        kind=kind,
        summary=summary,
        detail=detail,
        options=options,
        question_groups=groups,
        questions_meta=_question_meta(kind, args) if isinstance(args, dict) else [],
    )


def _feed_typed_event(
    reader: RichReader,
    event: object,
    *,
    runtime: str,
    groups: dict[str, RichMessage],
    batch: _TypedBatch,
) -> tuple[RichMessage | None, bool]:
    """Project one SessKit typed event onto phone cards.

    Returns (host message, is_new_host). Shared by the Pi/Claude/Codex
    projections; per-runtime hooks cover only genuine legacy wire
    differences: Codex attaches tool calls to the batch-local previous
    assistant card and dedups repeated text; Claude dedups the flushed
    upstream-error card; Pi owns the thinking-only error card below.
    ``compaction`` events never reach the phone; ``thinking`` does only for
    Claude, whose desktop shows non-empty thinking inline as reply text.
    """
    etype = getattr(event, "type", "")
    if etype == "thinking" and runtime == "claude":
        # Claude Code stores some visible progress notes as thinking blocks and
        # renders them inline; the phone shows them exactly like reply text.
        # Empty / redacted thinking carries no text and stays hidden.
        etype = "assistant_message"
    ts = getattr(event, "ts", None)
    key = _typed_group_key(event, runtime)
    if etype == "user_message":
        if (getattr(event, "origin", None) or "unknown") == "injected":
            return None, False
        raw_text = str(getattr(event, "text", None) or "")
        if runtime == "codex":
            # Recognize and settle native answers on the complete native text:
            # display clipping below would truncate a long answer and destroy
            # the closing tag/JSON. Only remaining ordinary text is clipped.
            raw_text = _strip_native_reply(reader, raw_text)
            if not raw_text:
                return None, False
        clipped = _clip(raw_text, _MAX_TEXT)
        if not clipped or _phone_injected_user(clipped):
            return None, False
        host = RichMessage(reader._next_seq(), "user", clipped, ts)
        if key:
            groups[key] = host
        return host, True
    if etype == "assistant_message":
        text = str(getattr(event, "text", None) or "").strip()
        if not text:
            return None, False
        clipped = _clip(text, _MAX_TEXT)
        prev = batch.last
        if prev is not None and prev.role == "assistant" and prev.text == clipped and (
            runtime == "codex" or getattr(event, "error", None) is not None
        ):
            # Codex adjacent-repeat rule, and the Claude flushed-error rule:
            # never show the same card text twice in a row.
            return prev, False
        host, is_new = _typed_host_for(groups, key, "assistant", ts, reader._next_seq)
        host.text = _clip(f"{host.text}\n\n{text}" if host.text else text, _MAX_TEXT)
        if runtime == "codex":
            _settle_async_on_turn_error(reader, event)
        return host, is_new
    if etype == "tool_call":
        tool = _codex_tool_card(event) if runtime == "codex" else _standard_tool_card(event)
        if runtime == "codex":
            prev = batch.last
            if prev is not None and prev.role == "assistant":
                prev.tools.append(tool)
                reader._register_tool(prev, tool)
                return prev, False
        host, is_new = _typed_host_for(groups, key, "assistant", ts, reader._next_seq)
        host.tools.append(tool)
        reader._register_tool(host, tool)
        return host, is_new
    if etype == "tool_result":
        call_id = str(getattr(event, "call_id", None) or "")
        if not call_id or call_id not in reader._pending:
            return None, False
        pending_tool = reader._pending.get(call_id)
        if (
            runtime == "codex"
            and pending_tool is not None
            and pending_tool.name == "request_user_input_async"
            and _is_async_acceptance_receipt(getattr(event, "raw_output", None))
        ):
            # ``{"accepted":true}`` is the async routing receipt, not an
            # answer: the AgentMessage (delivery=async) panel stays up until
            # a native <send_user_message_question_reply> arrives or the
            # live turn ends. Finishing here is what hid the phone form.
            return None, False
        result = getattr(event, "result", None)
        if (
            result is not None
            and getattr(result, "status", None) == "error"
            and getattr(getattr(result, "evidence", None), "origin", None) == "native"
        ):
            # Explicit native failure (old ``isError: true``): always an error.
            # Otherwise the existing text heuristic decides, exactly like the
            # legacy parsers did for ``isError: false`` / absent.
            failed: bool | None = True
        else:
            failed = None
        host = reader._finish_tool(
            call_id, output=getattr(event, "raw_output", None), failed=failed
        )
        return (host, False) if host is not None else (None, False)
    if etype == "lifecycle" and runtime == "codex":
        # Native turn-end boundary (task_complete / turn_aborted): settle this
        # turn's async panels. Produces no phone card.
        _settle_async_on_turn_end(reader, event)
        return None, False
    if etype == "lifecycle" and runtime == "pi":
        # SessKit carries the error of a thinking-only failed turn on a
        # typed-only lifecycle event. Like the legacy parser, the error text
        # owns the card only when the turn produced no text and no tools.
        error_text = str(getattr(getattr(event, "error", None), "message", "") or "").strip()
        existing = groups.get(key) if key else None
        if not error_text or (existing is not None and (existing.text or existing.tools)):
            return None, False
        host, is_new = _typed_host_for(groups, key, "assistant", ts, reader._next_seq)
        host.text = _clip(error_text, _MAX_TEXT)
        return host, is_new
    return None, False


def _is_async_acceptance_receipt(value: object) -> bool:
    """True when a Codex async tool output is only the routing receipt.

    The handler answers ``{"accepted":true}`` immediately while the
    ``delivery=async`` AgentMessage panel stays pending; only a native
    ``<send_user_message_question_reply>`` envelope (or turn end) resolves
    it. Anything else — answers, errors, empty — is not a bare receipt.
    """
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", "ignore")
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return False
            parsed = json.loads(text)
        elif isinstance(value, dict):
            parsed = value
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and "accepted" in item:
                    parsed = item
                    break
                if isinstance(item, str):
                    try:
                        maybe = json.loads(item.strip())
                    except ValueError:
                        continue
                    if isinstance(maybe, dict) and "accepted" in maybe:
                        parsed = maybe
                        break
            else:
                return False
        else:
            return False
    except (ValueError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    if "answers" in parsed or "questionItemId" in parsed:
        return False
    accepted = parsed.get("accepted")
    if isinstance(accepted, str):
        return accepted.strip().lower() == "true"
    return accepted is True


# Codex native async answers are submitted as a user-context fragment tagged
# <send_user_message_question_reply> carrying a JSON array of
# {questionItemId, question, answer} (codex-rs answered_question.rs). The
# rollout keeps that row, so without stripping the phone renders the whole
# wrapper as an ordinary user bubble. Only a well-formed envelope is control
# content: malformed JSON or a bare tag mention stays ordinary user text.
_NATIVE_REPLY_RE = re.compile(
    r"<send_user_message_question_reply>(.*?)</send_user_message_question_reply>",
    re.S,
)


def _native_identity_parts(value: object) -> tuple[str, int] | None:
    """Parse the official per-question identity.

    ``JSON.stringify(["request_user_input_async", <call_id>, <index>])``
    (codex-rs ``async_questions/state.rs``). Anything else — unparseable
    text, wrong tool, non-string call id, non-integer index — is not a native
    answer identity.
    """
    if not isinstance(value, str):
        return None
    try:
        parts = json.loads(value)
    except (ValueError, TypeError):
        return None
    if (
        not isinstance(parts, list)
        or len(parts) != 3
        or parts[0] != _ASYNC_QUESTION_TOOL
        or not isinstance(parts[1], str)
        or not parts[1]
        or not isinstance(parts[2], int)
        or isinstance(parts[2], bool)
        or parts[2] < 0
    ):
        return None
    return parts[1], parts[2]


def _native_reply_items(body: str) -> list[dict] | None:
    """Parse one envelope body; None when it is not a valid native answer.

    A recognized answer item carries the official string identity plus
    ``question`` and ``answer`` strings (codex-rs ``answered_question.rs``).
    Identity-only objects, missing fields, and wrong-typed fields are
    lookalikes, not answers: the whole fragment stays ordinary user text.
    """
    try:
        parsed = json.loads((body or "").strip())
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    items: list[dict] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            return None
        if _native_identity_parts(entry.get("questionItemId")) is None:
            return None
        question = entry.get("question")
        answer = entry.get("answer")
        if not isinstance(question, str) or not question.strip():
            return None
        if not isinstance(answer, str):
            return None
        items.append(entry)
    return items


def _expected_question_indices(tool: ToolCall) -> set[int] | None:
    """Question indices one complete native answer must cover, if known."""
    meta = tool.questions_meta
    if not meta:
        return None
    return set(range(len(meta)))


def _settle_native_reply(reader: RichReader, items: list[dict]) -> None:
    """Settle a pending async request only when the envelope answers it fully.

    The official TUI (``resolve_answers``) clears only the answered questions
    and keeps unanswered siblings pending. The phone always submits every
    question of one request at once, so a complete envelope settles the whole
    call; a partial envelope stays hidden but leaves the request pending
    instead of losing the unanswered siblings. No other request is ever
    touched — unknown identities settle nothing.
    """
    answered_by_call: dict[str, set[int]] = {}
    for entry in items:
        parsed = _native_identity_parts(entry.get("questionItemId"))
        if parsed is None:
            continue
        call_id, index = parsed
        answered_by_call.setdefault(call_id, set()).add(index)
    for call_id, answered in answered_by_call.items():
        tool = reader._pending.get(call_id)
        if tool is None or tool.name != _ASYNC_QUESTION_TOOL:
            continue
        if tool.kind not in QUESTION_KINDS:
            continue
        expected = _expected_question_indices(tool)
        if expected is not None and not expected <= answered:
            continue
        reader._pending.pop(call_id, None)
        tool.status = "ok"


def _strip_native_reply(reader: RichReader, text: str) -> str:
    """Remove recognized native answer envelopes, settling matching requests.

    Returns the remaining ordinary text (possibly empty). Unrecognized
    fragments — malformed JSON, non-answer bodies, bare tag mentions — are
    preserved verbatim as ordinary user text.
    """

    def _replace(match: re.Match) -> str:
        items = _native_reply_items(match.group(1))
        if items is None:
            return match.group(0)
        _settle_native_reply(reader, items)
        return ""

    return _NATIVE_REPLY_RE.sub(_replace, text).strip()


def _settle_async_on_turn_error(reader: RichReader, event: object) -> None:
    """Settle Codex async panels when their turn ends abnormally.

    Mirrors the official TUI ``take_question_drafts`` on turn end
    (``chatwidget/turn_runtime.rs`` + ``protocol.rs``): a turn-scoped native
    error such as ``turn_aborted`` clears the async panel. The
    ``{"accepted":true}`` routing receipt is not an error and never reaches
    here; ordinary commentary carries no error and leaves the panel up
    (``bottom_pane/async_questions/state.rs``).
    """
    error = getattr(event, "error", None)
    if error is None or getattr(error, "scope", None) != "turn":
        return
    for call_id in [
        call_id
        for call_id, tool in reader._pending.items()
        if tool.name == _ASYNC_QUESTION_TOOL
    ]:
        tool = reader._pending.pop(call_id, None)
        if tool is not None:
            tool.status = "error"


def _settle_async_on_turn_end(reader: RichReader, event: object) -> None:
    """Settle Codex async panels on the native turn-end lifecycle event.

    SessKit emits one typed-only ``lifecycle`` per native turn end
    (``task_complete`` always; ``turn_aborted`` already did): the final text
    card alone cannot distinguish normal completion from mid-turn commentary.
    Every pending async tool belongs to an ended turn here — events are
    chronological, so no later turn exists yet — and an older completed turn's
    tools were settled by their own boundary, so they stay settled. Clean
    completion reads ``ok`` (the call itself returned); an error-carrying end
    reads ``error``.
    """
    stop = str(getattr(event, "stop_reason", None) or "")
    text = str(getattr(event, "text", None) or "")
    if stop != "task_complete" and not text.startswith("turn_aborted:"):
        return
    failed = getattr(event, "error", None) is not None
    for call_id in [
        call_id
        for call_id, tool in reader._pending.items()
        if tool.name == _ASYNC_QUESTION_TOOL
    ]:
        tool = reader._pending.pop(call_id, None)
        if tool is not None:
            tool.status = "error" if failed else "ok"


def _project_typed_batch(
    reader: RichReader,
    runtime: str,
    events: object,
    groups: dict[str, RichMessage],
) -> list[RichMessage]:
    """Project one event batch, collecting each touched host once in order."""
    batch = _TypedBatch()
    for event in events or ():
        host, _is_new = _feed_typed_event(
            reader, event, runtime=runtime, groups=groups, batch=batch
        )
        if host is None:
            continue
        batch.last = host
        if host.seq in batch.seen:
            continue
        batch.seen.add(host.seq)
        batch.out.append(host)
    return batch.out


def _pi_feed_event(reader: RichReader, event: object) -> tuple[RichMessage | None, bool]:
    """Project one SessKit typed event onto phone cards.

    Thin wrapper over the shared projector with Pi's persistent grouping
    map; batch-local rules are unused on this path (callers batch hosts
    themselves, as before).
    """
    return _feed_typed_event(
        reader, event, runtime="pi", groups=reader._pi_by_mid, batch=_TypedBatch()
    )


def _parse_pi(reader: RichReader) -> list[RichMessage]:
    """Pi: project SessKit typed activity; legacy native path is the fallback."""
    if not _pi_reader_available():
        return _parse_pi_legacy(reader)
    path = reader.path
    if not path or not os.path.isfile(path):
        return []
    try:
        from sesskit import get_adapter

        wire_reader = reader._pi_reader
        if wire_reader is None:
            session = dict(reader.session)
            session.setdefault("source", "pi")
            wire_reader = get_adapter("pi").open_reader(session, reader._pi_cursor)
            reader._pi_reader = wire_reader
        result = wire_reader.poll()
        reader._pi_cursor = result.cursor
        try:
            reader._size = os.path.getsize(path)
        except OSError:
            pass
    except (OSError, ValueError):
        return []

    if getattr(result, "state", "available") == "unavailable" and not getattr(
        result, "events", ()
    ):
        # History briefly unreadable: report no news, keep fingerprints/cursor
        # so the next poll diffs instead of re-pushing everything.
        return []

    if getattr(result, "reset", False) or not reader._pi_live:
        return _pi_rebuild_from_events(reader, result.events)
    out: list[RichMessage] = []
    seen: set[int] = set()
    for event in result.events:
        host, _is_new = _pi_feed_event(reader, event)
        if host is None or host.seq in seen:
            continue
        seen.add(host.seq)
        out.append(host)
    for message in out:
        reader._pi_fps[message.seq] = _pi_fingerprint(message)
    return out


def _pi_rebuild_from_events(reader: RichReader, events: object) -> list[RichMessage]:
    """Rebuild all Pi phone cards, emitting only new/changed tails.

    Same observable behavior as the legacy full-branch rebuild: stable seqs
    are reused, tool-result updates re-emit their earlier card, and
    abandoned-branch content never appears.
    """
    prev_raw = getattr(reader, "_pi_fps", {}) or {}
    prev_fps = {int(key): _normalize_pi_fp(value) for key, value in prev_raw.items()}
    already = reader._seq
    reader._pending = {}
    reader._host_by_call = {}
    reader._pi_by_mid = {}
    reader._seq = 0
    fresh: list[RichMessage] = []
    for event in events or ():
        host, is_new = _pi_feed_event(reader, event)
        if host is not None and is_new:
            fresh.append(host)
    new_fps = {message.seq: _pi_fingerprint(message) for message in fresh}
    _note_replacement(reader, prev_fps, fresh)
    out = [
        message
        for message in fresh
        if message.seq > already or prev_fps.get(message.seq) != new_fps[message.seq]
    ]
    reader._pi_fps = new_fps
    reader._seq = fresh[-1].seq if fresh else already
    reader._pi_live = True
    return out


def _parse_pi_legacy(reader: RichReader) -> list[RichMessage]:
    """Fallback for SessKit builds without ``open_reader``; marked for removal.

    Only reached when `_pi_reader_available()` is false. Do not extend: all
    new Pi phone behavior goes through the SessKit projection above.
    Remove once the minimum SessKit version provides the reader API.

    Pi: full active-branch rebuild; emit new turns and updated tool hosts.

    Pi history is a parent-linked tree. Byte-offset JSONL reads would mix in
    abandoned forks, so each poll rebuilds the active leaf path (same as share
    export / desktop preview). Message count is modest; rebuild cost is fine.
    """
    path = reader.path
    if not path or not os.path.isfile(path):
        return []
    try:
        built = _pi_build_messages(path)
    except (OSError, ValueError):
        return []

    already = reader._seq
    prev_raw = getattr(reader, "_pi_fps", {}) or {}
    prev_fps = {int(key): _normalize_pi_fp(value) for key, value in prev_raw.items()}
    new_fps: dict[int, tuple] = {}
    out: list[RichMessage] = []
    for message in built:
        fingerprint = _pi_fingerprint(message)
        new_fps[message.seq] = fingerprint
        if message.seq > already:
            out.append(message)
        elif prev_fps.get(message.seq) != fingerprint:
            # Tool result landed on an already-pushed assistant turn.
            out.append(message)

    reader._pi_fps = new_fps
    reader._seq = built[-1].seq if built else already
    reader._pending = {}
    reader._host_by_call = {}
    for message in built:
        for tool in message.tools:
            if tool.status == "running" and tool.call_id:
                reader._pending[tool.call_id] = tool
                reader._host_by_call[tool.call_id] = message
    try:
        reader._size = os.path.getsize(path)
    except OSError:
        pass
    return out


# --- Claude/Codex via SessKit ---------------------------------------------

_CC_SESSKIT_RUNTIMES = frozenset({"claude", "codex"})
_SESSKIT_READER_AVAILABLE: dict[str, bool] = {}
_CC_PAGE_LIMIT = 200


def _sesskit_reader_available(runtime: str) -> bool:
    """Feature-detect the SessKit incremental reader for one runtime.

    False on SessKit builds without ``get_adapter(runtime).open_reader``;
    the legacy native path stays as the fallback until the minimum SessKit
    version provides the reader API.
    """
    available = _SESSKIT_READER_AVAILABLE.get(runtime)
    if available is None:
        try:
            from sesskit import get_adapter

            adapter = get_adapter(runtime)
            available = callable(getattr(adapter, "open_reader", None))
        except Exception:
            available = False
        _SESSKIT_READER_AVAILABLE[runtime] = available
    return available


def _cc_open_reader(reader: RichReader, runtime: str) -> object:
    from sesskit import get_adapter

    session = dict(reader.session)
    session.setdefault("source", runtime)
    return get_adapter(runtime).open_reader(session, reader._cc_cursor)


def _cc_collect_all(wire_reader: object) -> tuple[list, str]:
    """Walk the current SessKit generation oldest-first via backward pages."""
    chunks = []
    page = wire_reader.page(before=None, limit=_CC_PAGE_LIMIT)  # type: ignore[union-attr]
    chunks.append(page)
    while page.has_more and page.before:
        page = wire_reader.page(before=page.before, limit=_CC_PAGE_LIMIT)  # type: ignore[union-attr]
        chunks.append(page)
    events: list = []
    for chunk in reversed(chunks):
        events.extend(chunk.events)
    return events, chunks[0].generation


def _cc_note_floor(reader: RichReader) -> None:
    floor = reader._cc_floor
    full = reader._cc_full or []
    reader._has_earlier = (
        False if floor is None else any(item.seq < floor for item in full)
    )


def _cc_rebuild(
    reader: RichReader, runtime: str, wire_reader: object, result: object
) -> list[RichMessage]:
    """Full materialization after open/reset/generation change.

    Projects every event of the current generation as one batch (the same
    span the legacy full-file parse consumed), then emits only new or
    changed cards with stable seqs, exactly like the Pi rebuild.
    """
    try:
        events, generation = _cc_collect_all(wire_reader)
    except (OSError, ValueError):
        return []
    already = reader._seq
    prev_fps = dict(reader._cc_fps)
    reader._pending = {}
    reader._host_by_call = {}
    reader._seq = 0
    fresh = _project_typed_batch(reader, runtime, events, {})
    new_fps = {item.seq: _pi_fingerprint(item) for item in fresh}
    _note_replacement(reader, prev_fps, fresh)
    out = [
        item
        for item in fresh
        if item.seq > already or prev_fps.get(item.seq) != new_fps[item.seq]
    ]
    reader._cc_full = fresh
    reader._cc_fps = new_fps
    reader._cc_gen = generation
    reader._cc_cursor = getattr(result, "cursor", None)
    reader._seq = fresh[-1].seq if fresh else already
    reader.parsed_line_count += len(events)
    _cc_note_floor(reader)
    return out


def _cc_sync(reader: RichReader, runtime: str) -> list[RichMessage]:
    """Poll the SessKit reader, rebuilding only on reset/generation change."""
    wire_reader = reader._cc_reader
    if wire_reader is None:
        try:
            wire_reader = _cc_open_reader(reader, runtime)
        except (OSError, ValueError):
            return []
        reader._cc_reader = wire_reader
    try:
        result = wire_reader.poll()  # type: ignore[union-attr]
    except (OSError, ValueError):
        return []
    if getattr(result, "state", "available") == "unavailable" and not getattr(
        result, "events", ()
    ):
        # History briefly unreadable: report no news, keep fingerprints/cursor
        # so the next poll diffs instead of re-pushing everything.
        return []
    try:
        reader._size = os.path.getsize(reader.path)
    except OSError:
        pass
    if (
        getattr(result, "reset", False)
        or result.generation != reader._cc_gen
        or reader._cc_full is None
    ):
        return _cc_rebuild(reader, runtime, wire_reader, result)
    reader._cc_cursor = result.cursor
    if not result.events:
        return []
    batch = _project_typed_batch(reader, runtime, result.events, {})
    full = reader._cc_full or []
    by_seq = {item.seq: index for index, item in enumerate(full)}
    for item in batch:
        index = by_seq.get(item.seq)
        if index is None:
            by_seq[item.seq] = len(full)
            full.append(item)
        else:
            # Same seq with updated tool status (result fill-in).
            full[index] = item
        reader._cc_fps[item.seq] = _pi_fingerprint(item)
    reader._cc_full = full
    reader.parsed_line_count += len(result.events)
    return batch


def _parse_claude(reader: RichReader) -> list[RichMessage]:
    """Claude: project SessKit typed activity; legacy native path is the fallback."""
    if _sesskit_reader_available("claude"):
        return _cc_sync(reader, "claude")
    return _parse_claude_legacy(reader)


def _parse_codex(reader: RichReader) -> list[RichMessage]:
    """Codex: project SessKit typed activity; legacy native path is the fallback."""
    if _sesskit_reader_available("codex"):
        return _cc_sync(reader, "codex")
    return _parse_codex_legacy(reader)


def _read_cc_tail(reader: RichReader, limit: int) -> list[RichMessage]:
    """Cold open: materialize once, return only the tail window."""
    take = max(1, limit or _DEFAULT_WINDOW)
    _cc_sync(reader, reader.runtime_id)
    full = reader._cc_full or []
    tail = full[-take:] if len(full) > take else list(full)
    reader._cc_floor = tail[0].seq if tail else None
    _cc_note_floor(reader)
    return tail


def _read_cc_earlier(reader: RichReader, limit: int, *, before_seq: int) -> list[RichMessage]:
    """Backward page over the materialized list; seqs are already global.

    Never polls here: older cards are immutable within a generation, and
    consuming a poll delta inside a page read would strand it outside the
    transcript. The next poll/reset still rebuilds and diffs as usual.
    """
    take = max(1, limit or _DEFAULT_WINDOW)
    full = reader._cc_full
    if full is None:
        _cc_sync(reader, reader.runtime_id)
        full = reader._cc_full or []
    older = [item for item in full if item.seq < before_seq][-take:]
    if not older:
        reader._has_earlier = False
        return []
    if reader._cc_floor is None:
        reader._cc_floor = older[0].seq
    else:
        reader._cc_floor = min(reader._cc_floor, older[0].seq)
    _cc_note_floor(reader)
    return older


# --- Cursor/OpenCode via SessKit (P2) ---------------------------------------

_CO_SESSKIT_RUNTIMES = frozenset({"cursor", "opencode"})


def _co_open_reader(reader: RichReader, runtime: str) -> object:
    from sesskit import get_adapter

    session = dict(reader.session)
    session.setdefault("source", runtime)
    return get_adapter(runtime).open_reader(session, reader._co_cursor)


def _co_filter_events(runtime: str, events: object) -> list:
    """Runtime hook: drop phone-invisible error-only turns before projection.

    Plain conversation hides an assistant turn whose text only surfaces a
    native error (`sesskit.visibility.visible_in_conversation`, default
    `include_errors=False`; same rule in CONTRACT.md). OpenCode's previous
    phone path fell back to that plain text, so the projected cards must
    hide those turns too. Cursor never carries typed errors: identity.
    This is a pre-projection event filter — the shared projector itself is
    untouched (P1-owned).
    """
    items = list(events or ())
    if runtime != "opencode":
        return items
    visible = []
    for event in items:
        if getattr(event, "type", "") == "assistant_message":
            error = getattr(event, "error", None)
            if (
                error is not None
                and (getattr(event, "text", "") or "")
                == (getattr(error, "message", "") or "")
            ):
                continue
        visible.append(event)
    return visible


def _co_note_floor(reader: RichReader) -> None:
    floor = reader._co_floor
    full = reader._co_full or []
    reader._has_earlier = (
        False if floor is None else any(item.seq < floor for item in full)
    )


def _co_rebuild(
    reader: RichReader, runtime: str, wire_reader: object, result: object
) -> list[RichMessage]:
    """Full materialization after open/reset/generation change.

    Walks the current SessKit generation oldest-first through backward
    pages (reusing P1's page-walk helper), projects every event as one
    batch through the shared projector, then emits only new or changed
    cards with stable seqs, exactly like the Pi/CC rebuilds.
    """
    try:
        events, generation = _cc_collect_all(wire_reader)
    except (OSError, ValueError):
        return []
    already = reader._seq
    prev_fps = dict(reader._co_fps)
    reader._pending = {}
    reader._host_by_call = {}
    reader._seq = 0
    fresh = _project_typed_batch(reader, runtime, _co_filter_events(runtime, events), {})
    new_fps = {item.seq: _pi_fingerprint(item) for item in fresh}
    _note_replacement(reader, prev_fps, fresh)
    out = [
        item
        for item in fresh
        if item.seq > already or prev_fps.get(item.seq) != new_fps[item.seq]
    ]
    reader._co_full = fresh
    reader._co_fps = new_fps
    reader._co_gen = generation
    reader._co_cursor = getattr(result, "cursor", None)
    reader._seq = fresh[-1].seq if fresh else already
    _co_note_floor(reader)
    return out


def _co_sync(reader: RichReader, runtime: str) -> list[RichMessage]:
    """Poll the SessKit Cursor/OpenCode reader, rebuilding on reset/change."""
    wire_reader = reader._co_reader
    if wire_reader is None:
        try:
            wire_reader = _co_open_reader(reader, runtime)
        except (OSError, ValueError):
            return []
        reader._co_reader = wire_reader
    try:
        result = wire_reader.poll()  # type: ignore[union-attr]
    except (OSError, ValueError):
        return []
    if getattr(result, "state", "available") == "unavailable" and not getattr(
        result, "events", ()
    ):
        # History briefly unreadable: report no news, keep fingerprints/cursor
        # so the next poll diffs instead of re-pushing everything.
        return []
    try:
        reader._size = os.path.getsize(reader.path)
    except OSError:
        pass
    if (
        getattr(result, "reset", False)
        or result.generation != reader._co_gen
        or reader._co_full is None
    ):
        return _co_rebuild(reader, runtime, wire_reader, result)
    reader._co_cursor = result.cursor
    if not result.events:
        return []
    batch = _project_typed_batch(
        reader, runtime, _co_filter_events(runtime, result.events), {}
    )
    full = reader._co_full or []
    by_seq = {item.seq: index for index, item in enumerate(full)}
    for item in batch:
        index = by_seq.get(item.seq)
        if index is None:
            by_seq[item.seq] = len(full)
            full.append(item)
        else:
            # Same seq with updated tool status (result fill-in).
            full[index] = item
        reader._co_fps[item.seq] = _pi_fingerprint(item)
    reader._co_full = full
    return batch


def _parse_opencode(reader: RichReader) -> list[RichMessage]:
    """OpenCode: project SessKit typed activity; plain text is the fallback.

    The fallback keeps Kimi behavior (still on `_parse_plain`) working and
    covers SessKit builds without the reader. Unlike the fallback, the
    SessKit path emits tool cards, so OpenCode gains them on the phone.
    """
    if _sesskit_reader_available("opencode"):
        return _co_sync(reader, "opencode")
    return _parse_plain(reader)


def _read_co_tail(reader: RichReader, limit: int) -> list[RichMessage]:
    """Cold open: materialize once, return only the tail window."""
    take = max(1, limit or _DEFAULT_WINDOW)
    _co_sync(reader, reader.runtime_id)
    full = reader._co_full or []
    tail = full[-take:] if len(full) > take else list(full)
    reader._co_floor = tail[0].seq if tail else None
    _co_note_floor(reader)
    return tail


def _read_co_earlier(reader: RichReader, limit: int, *, before_seq: int) -> list[RichMessage]:
    """Backward page over the materialized list; seqs are already global.

    Never polls here: older cards are immutable within a generation, and
    consuming a poll delta inside a page read would strand it outside the
    transcript. The next poll/reset still rebuilds and diffs as usual.
    """
    take = max(1, limit or _DEFAULT_WINDOW)
    full = reader._co_full
    if full is None:
        _co_sync(reader, reader.runtime_id)
        full = reader._co_full or []
    older = [item for item in full if item.seq < before_seq][-take:]
    if not older:
        reader._has_earlier = False
        return []
    if reader._co_floor is None:
        reader._co_floor = older[0].seq
    else:
        reader._co_floor = min(reader._co_floor, older[0].seq)
    _co_note_floor(reader)
    return older


_PARSERS = {
    "codex": _parse_codex,
    "claude": _parse_claude,
    "cursor": _parse_cursor,
    "kimi": _parse_plain,
    "opencode": _parse_opencode,
    "pi": _parse_pi,
}


def supports_tool_calls(runtime_id: str) -> bool:
    return runtime_id in ("codex", "claude", "cursor", "opencode", "pi")


def _tool_args(raw: object) -> dict:
    """各助手工具参数有时是 dict、有时是 JSON 字符串，统一成 dict。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def pending_prompts_from_messages(items: list[RichMessage]) -> list[dict]:
    """从已解析消息里找出仍在等待用户回答的提问型工具调用。

    手机端可直接渲染 ``options`` 为可点按钮；没有选项时仍返回摘要供自由输入。
    一次询问里的多道题拆成多条；后面已经有新回复或新工具时旧提问不再返回。
    唯一的例外是 Codex ``request_user_input_async``：它是异步面板，官方在等答
    期间继续推进回合（``state.rs``），所以等答中的 commentary 与非提问工具不
    算 supersede；只有用户正文（steering 或原生信封回执）、更新的提问或回合
    结束才结算——turn 级原生错误在投影期直接结算（见 ``_feed_typed_event``）。
    只展示最新一个未答复的请求。
    """
    for message in reversed(items):
        running = [
            tool
            for tool in message.tools
            if tool.kind in QUESTION_KINDS and tool.status == "running"
        ]
        if running:
            prompts: list[dict] = []
            for tool in running:
                prompts.extend(_prompt_entries_for_tool(tool))
            return prompts
        if _message_supersedes_prompts(message):
            break
    else:
        return []
    # 新回复/新工具盖住了所有 running 提问：同步行为到此为止；异步面板若只是
    # 被等答 commentary 或无关工具盖住，必须保留（2026-10-01 确认缺陷）。
    return _retained_async_prompts(items)


def _retained_async_prompts(items: list[RichMessage]) -> list[dict]:
    """Return the newest running async panel hidden only by non-settling activity.

    A newer user text settles the panel either way: ordinary steering submits a
    real prompt (the official TUI calls ``clear_pending_questions`` on prompt
    submit, ``chatwidget/input_submission.rs``), and a native
    ``<send_user_message_question_reply>`` envelope answers it
    (``resolve_answers``). Anything newer that is not user text — assistant
    commentary, unrelated tool calls/results — leaves the panel up.
    """
    candidate: ToolCall | None = None
    candidate_seq = -1
    for message in items:
        for tool in message.tools:
            if (
                tool.kind in QUESTION_KINDS
                and tool.status == "running"
                and tool.name == _ASYNC_QUESTION_TOOL
                and message.seq >= candidate_seq
            ):
                candidate = tool
                candidate_seq = message.seq
    if candidate is None:
        return []
    for message in items:
        if (
            message.role == "user"
            and (message.text or "").strip()
            and message.seq > candidate_seq
        ):
            return []
    return _prompt_entries_for_tool(candidate)


def _prompt_entries_for_tool(tool: ToolCall) -> list[dict]:
    if tool.questions_meta:
        return prompt_entries(
            request_id=tool.call_id,
            name=tool.name,
            questions=tool.questions_meta,
            detail=tool.detail,
        )
    groups = tool.question_groups or [{"summary": tool.summary, "options": list(tool.options)}]
    multi = len(groups) > 1
    entries: list[dict] = []
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        entry: dict = {
            "id": f"{tool.call_id}:{index}" if multi else tool.call_id,
            "name": tool.name,
            "summary": group.get("summary") or tool.summary,
            "options": list(group.get("options") or []),
        }
        if tool.detail and not multi:
            entry["detail"] = tool.detail
        entries.append(entry)
    return entries


def prompt_entries(
    *,
    request_id: str,
    name: str,
    questions: list[dict],
    detail: str = "",
    allow_custom: bool | None = None,
) -> list[dict]:
    """session.prompts rows: legacy ``id/summary/options`` plus the native request shape.

    ``allow_custom`` None keeps each question's own flag (default True). Runtimes
    without a native answer path still list the question; ``input.question``
    reports them unavailable instead of pasting text.
    """
    multi = len(questions) > 1
    entries: list[dict] = []
    for index, question in enumerate(questions):
        options = [o for o in question.get("options") or [] if isinstance(o, dict)]
        prompt = str(question.get("prompt") or question.get("header") or name)
        custom = question.get("allow_custom", True) if allow_custom is None else allow_custom
        question_id = str(question.get("id") or index)
        if name == "request_user_input_async":
            # Official per-question identity: JSON.stringify(
            # ["request_user_input_async", <AgentMessage item id>, <index>]).
            # The async AgentMessage id equals the function_call call_id,
            # which is this request's ``request_id`` here.
            question_id = json.dumps(
                ["request_user_input_async", request_id, index],
                separators=(",", ":"),
            )
        entry: dict = {
            "id": f"{request_id}:{index}" if multi else request_id,
            "name": name,
            "summary": prompt,
            "prompt": prompt,
            "options": [str(o.get("label") or "") for o in options],
            "request_id": request_id,
            "question_id": question_id,
            "multi_select": bool(question.get("multi_select")),
            "allow_custom": bool(custom),
            "custom_needs_choice": bool(question.get("custom_needs_choice")),
            "is_secret": bool(question.get("is_secret")),
            "option_details": options,
        }
        if question.get("header"):
            entry["header"] = str(question["header"])
        if detail and not multi:
            entry["detail"] = detail
        entries.append(entry)
    return entries


def _message_supersedes_prompts(message: RichMessage) -> bool:
    if (message.text or "").strip():
        return True
    for tool in message.tools:
        if tool.kind not in QUESTION_KINDS or tool.status != "running":
            return True
    return False


def pending_prompts(session: dict) -> list[dict]:
    """从会话历史里找出仍在等待用户回答的提问型工具调用。"""
    return pending_prompts_from_messages(RichReader(session).read_all())
