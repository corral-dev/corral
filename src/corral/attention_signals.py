"""从各运行时的本地历史中提取会话关注状态证据。

这里刻意只识别结构化事件，不根据自然语言、问号或文件更新时间猜测状态。
JSONL 只读有界尾部，SQLite 只查询指定会话的少量尾部记录；任何未知格式均
返回 ``unknown``，不能影响正常的会话扫描。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from corral.attention import AttentionEvidence

_JSONL_TAIL_BYTES = 512 * 1024
_JSONL_TAIL_ENTRIES = 768
# A single image tool result (screenshots) can exceed the tail window and evict every
# lifecycle record; read further back until enough complete records are in view.
_JSONL_TAIL_MIN_ENTRIES = 32
_JSONL_TAIL_MAX_BYTES = 8 * 1024 * 1024
_DB_TAIL_ROWS = 192
_CODEX_ATTENTION_READERS: OrderedDict[str, dict] = OrderedDict()
_CODEX_ATTENTION_LOCK = threading.RLock()
_QUESTION_TOOLS = frozenset({"AskUserQuestion", "request_user_input", "question", "AskQuestion"})


def _evidence(
    phase: str = "unknown",
    *,
    activity_token: str | None = None,
    question_token: str | None = None,
    observed_at: float = 0.0,
) -> AttentionEvidence:
    return AttentionEvidence(
        phase=phase,
        activity_token=activity_token,
        question_token=question_token,
        observed_at=observed_at,
        source="history",
    )


def _token(runtime: str, kind: str, native: Any) -> str | None:
    """只对原生标识或时间做摘要，绝不把正文写入状态存储。"""
    if native is None or native == "":
        return None
    raw = f"{runtime}\0{kind}\0{native}".encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:24]


def _event_native(entry: dict, *keys: str) -> Any:
    for key in keys:
        value = entry.get(key)
        if value is not None and value != "":
            return value
    return None


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        # Kimi/OpenCode 常用毫秒 epoch；秒 epoch 远小于此阈值。
        return float(value) / 1000 if value > 10_000_000_000 else float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _stable_observed_at(session: dict, path: str = "") -> float:
    """返回不会随重复扫描漂移的历史时间。"""
    for key in ("event_time", "file_mtime", "mtime"):
        value = _timestamp(session.get(key))
        if value is not None:
            return value
    if path:
        try:
            return os.path.getmtime(path)
        except OSError:
            pass
    return 0.0


def _advance_observed(current: float, *values: Any) -> float:
    for value in values:
        parsed = _timestamp(value)
        if parsed is not None:
            return parsed
    return current


def _path_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _finalize_history_phase(phase: str, live: bool) -> str:
    """把历史证据收成可展示阶段；进程活着本身不能冒充执行中。

    Cursor 不走这里：它的 ``unknown`` 要留给观察器决定绿点。
    """
    if phase in {"working", "waiting"} and not live:
        return "idle"
    if phase == "unknown":
        # 常驻 TUI 停在输入框、额度挂掉、或尾部看不到执行证据时，不能把
        # unknown 留给状态库去沿用旧的执行中。
        return "idle"
    return phase


def _read_jsonl_tail(path: str) -> list[dict]:
    window = _JSONL_TAIL_BYTES
    while True:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as file:
                offset = max(0, size - window)
                file.seek(offset)
                data = file.read().decode("utf-8", errors="replace")
        except OSError:
            return []

        lines = data.splitlines()
        if offset and lines:
            lines = lines[1:]
        if offset == 0 or len(lines) >= _JSONL_TAIL_MIN_ENTRIES or window >= _JSONL_TAIL_MAX_BYTES:
            break
        window = min(window * 4, _JSONL_TAIL_MAX_BYTES)

    entries: list[dict] = []
    for line in lines[-_JSONL_TAIL_ENTRIES:]:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _content_parts(entry: dict) -> Iterable[dict]:
    message = entry.get("message")
    if not isinstance(message, dict):
        return ()
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    return (part for part in content if isinstance(part, dict))


def _is_human_claude_user(entry: dict) -> bool:
    if entry.get("type") != "user":
        return False
    message = entry.get("message")
    if not isinstance(message, dict) or message.get("role") not in (None, "user"):
        return False
    origin = entry.get("origin")
    if origin is None:
        origin = message.get("origin")
    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    if origin_kind not in (None, "human"):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(part.get("type") == "text" for part in content if isinstance(part, dict))
    return False


def _inspect_claude(session: dict) -> AttentionEvidence:
    path = str(session.get("path") or "")
    entries = _read_jsonl_tail(path)
    if not entries:
        return _evidence(observed_at=_stable_observed_at(session, path))

    live = session.get("live") is True
    phase = "unknown"
    pending: dict[str, str] = {}
    activity_token = None
    observed_at = _stable_observed_at(session, path)

    for entry in entries:
        entry_type = entry.get("type")
        native = _event_native(entry, "uuid", "id", "timestamp")
        if _is_human_claude_user(entry):
            observed_at = _advance_observed(observed_at, entry.get("timestamp"))
            # Claude 的中断标记是固定协议值，不做自然语言近似匹配。
            message = entry.get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            if content == "[Request interrupted by user]":
                phase = "idle"
                activity_token = _token("claude", "interrupted", native)
            # 用户开口不是执行证据。常驻窗口停在输入框时最后一条常常是 user。

        if entry_type == "assistant":
            has_agent_output = False
            for part in _content_parts(entry):
                part_type = part.get("type")
                # Text, reasoning and tool calls are all execution; long tool-only rounds
                # write no text for minutes.
                if part_type in {"text", "thinking", "redacted_thinking", "tool_use"}:
                    has_agent_output = True
                if part_type == "tool_use" and part.get("name") == "AskUserQuestion":
                    call_id = str(part.get("id") or "")
                    if call_id:
                        pending[call_id] = _token("claude", "question", call_id) or call_id
                        has_agent_output = True
                elif part_type == "tool_result":
                    pending.pop(str(part.get("tool_use_id") or ""), None)
            if has_agent_output:
                phase = "working"
                activity_token = _token("claude", "assistant", native) or activity_token
                observed_at = _advance_observed(observed_at, entry.get("timestamp"))

        # tool_result 在 Claude 历史中通常包在 type=user 的 content 数组里。
        for part in _content_parts(entry):
            if part.get("type") == "tool_result":
                if pending.pop(str(part.get("tool_use_id") or ""), None) is not None:
                    phase = "working"
                    observed_at = _advance_observed(observed_at, entry.get("timestamp"))

        subtype = entry.get("subtype")
        hook_name = entry.get("hook_name") or entry.get("hookName")
        if (entry_type == "system" and subtype == "turn_duration") or hook_name in {"Stop", "StopFailure"}:
            phase = "idle"
            activity_token = _token("claude", "stop", native) or activity_token
            observed_at = _advance_observed(observed_at, entry.get("timestamp"))

    if pending and live:
        question_token = next(reversed(pending.values()))
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=question_token,
            observed_at=observed_at,
        )
    phase = _finalize_history_phase(phase, live)
    return _evidence(phase, activity_token=activity_token, observed_at=observed_at)


def _settle_codex_async_user_text(text: str, pending: dict[str, set[int]]) -> None:
    """Use the conversation parser's native reply contract for matching answers."""
    if not pending or not text.strip():
        return
    # Lazy import: ordinary attention scans do not load the remote transcript stack.
    from corral.remote.richmsg import (
        _NATIVE_REPLY_RE,
        _native_identity_parts,
        _native_reply_items,
    )

    def settle(match: Any) -> str:
        items = _native_reply_items(match.group(1))
        if items is None:
            return match.group(0)
        for item in items:
            identity = _native_identity_parts(item["questionItemId"])
            if identity is None:
                continue
            call_id, index = identity
            if call_id in pending:
                pending[call_id].discard(index)
                if not pending[call_id]:
                    pending.pop(call_id)
        return ""

    # Ordinary steering clears the native panel; a reply envelope only answers
    # its own questions, preserving siblings and other requests.
    if _NATIVE_REPLY_RE.sub(settle, text).strip():
        pending.clear()


def _compact_codex_attention_entry(entry: dict) -> dict | None:
    """Keep lifecycle/question evidence, never large command output or reasoning."""
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if kind == "item_completed":
        item = payload.get("item")
        if (
            not isinstance(item, dict)
            or item.get("type") != "AgentMessage"
            or not isinstance(item.get("questions"), list)
            or not item["questions"]
            or item.get("delivery") != "async"
        ):
            return None
    elif kind not in {
        "user_message", "task_started", "task_complete", "turn_aborted", "error", "agent_message",
        "function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output",
        "message", "reasoning",
    }:
        return None
    compact = {key: payload[key] for key in (
        "type", "turn_id", "call_id", "id", "completed_at", "started_at", "name", "role", "channel",
    ) if key in payload}
    if kind == "item_completed":
        item = payload["item"]
        compact["item"] = {"type": item.get("type"), "id": item.get("id"), "delivery": "async",
                           "questions": [None] * len(item["questions"])}
    elif kind == "function_call" and payload.get("name") == "request_user_input_async":
        args = payload.get("arguments")
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except (ValueError, TypeError):
            args = None
        questions = args.get("questions") if isinstance(args, dict) else None
        compact["arguments"] = {"questions": [None] * len(questions)} if isinstance(questions, list) else {}
    elif kind in {"function_call_output", "custom_tool_call_output"}:
        from corral.remote.richmsg import _is_async_acceptance_receipt

        compact["output"] = {"accepted": _is_async_acceptance_receipt(payload.get("output"))}
    elif kind == "user_message":
        text = str(payload.get("message") or "")
        compact["message"] = (
            text if "<send_user_message_question_reply>" in text else ("steering" if text.strip() else "")
        )
    return {"type": entry.get("type"), "timestamp": entry.get("timestamp"), "payload": compact}


def _read_codex_attention_delta(path: str, state: dict) -> list[dict]:
    """Cold-open only the current turn; then consume complete appended records.

    Async questions outlive an arbitrary tail window. Retaining their reduced
    state and a byte cursor avoids rereading a long turn on every refresh.
    """
    stat = os.stat(path)
    identity = (stat.st_dev, stat.st_ino)
    offset = state.get("offset", 0)
    if state and (state.get("identity") != identity or stat.st_size < offset
                  or (stat.st_size == offset and stat.st_mtime_ns != state.get("mtime_ns"))):
        state.clear()
    entries: list[dict] = []
    with open(path, "rb") as file:
        if state:
            file.seek(max(0, state["offset"] - 64))
            if file.read(min(64, state["offset"])) != state.get("checkpoint"):
                state.clear()
        if state:
            file.seek(state["offset"])
            while True:
                start = file.tell()
                line = file.readline()
                if not line or not line.endswith(b"\n"):
                    offset = start
                    break
                try:
                    entry = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(entry, dict) and (compact := _compact_codex_attention_entry(entry)) is not None:
                    entries.append(compact)
        else:
            # Reverse blocks bound working memory; stop at the newest turn
            # boundary instead of walking the session's older history.
            end = stat.st_size
            offset = end
            search_end = end
            while search_end:
                start = max(0, search_end - _JSONL_TAIL_BYTES)
                file.seek(start)
                last = file.read(search_end - start)
                newline = last.rfind(b"\n")
                if newline >= 0:
                    offset = start + newline + 1
                    break
                offset = start
                search_end = start
            position = offset
            remainder = b""
            stopped = False
            while position and not stopped:
                start = max(0, position - _JSONL_TAIL_BYTES)
                file.seek(start)
                lines = (file.read(position - start) + remainder).split(b"\n")
                remainder = lines.pop(0) if start else b""
                for line in reversed(lines):
                    try:
                        entry = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(entry, dict):
                        continue
                    compact = _compact_codex_attention_entry(entry)
                    if compact is not None:
                        entries.append(compact)
                        if compact["payload"].get("type") in {"task_started", "task_complete", "turn_aborted"}:
                            stopped = True
                            break
                position = start
            entries.reverse()
        file.seek(max(0, offset - 64))
        checkpoint = file.read(min(64, offset))
    state.update(offset=offset, identity=identity, mtime_ns=stat.st_mtime_ns, checkpoint=checkpoint)
    return entries


def _inspect_codex(session: dict) -> AttentionEvidence:
    path = str(session.get("path") or "")
    if session.get("live") is not True:
        return _inspect_codex_entries(session, _read_jsonl_tail(path), {})
    with _CODEX_ATTENTION_LOCK:
        state = _CODEX_ATTENTION_READERS.setdefault(path, {})
        _CODEX_ATTENTION_READERS.move_to_end(path)
        while len(_CODEX_ATTENTION_READERS) > 32:
            _CODEX_ATTENTION_READERS.popitem(last=False)
        try:
            entries = _read_codex_attention_delta(path, state)
        except OSError:
            _CODEX_ATTENTION_READERS.pop(path, None)
            return _evidence(observed_at=_stable_observed_at(session, path))
        return _inspect_codex_entries(session, entries, state)


def _inspect_codex_entries(session: dict, entries: list[dict], state: dict) -> AttentionEvidence:
    path = str(session.get("path") or "")
    if not entries and "phase" not in state:
        return _evidence(observed_at=_stable_observed_at(session, path))
    live = session.get("live") is True
    phase = state.get("phase", "unknown")
    pending: dict[str, str] = state.setdefault("pending", {})
    async_pending: dict[str, set[int]] = state.setdefault("async_pending", {})
    activity_token = state.get("activity_token")
    observed_at = state.get("observed_at", _stable_observed_at(session, path))

    for entry in entries:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")
        native = _event_native(payload, "turn_id", "call_id", "id", "completed_at", "started_at")
        if native is None:
            native = entry.get("timestamp")

        relevant = False

        if entry.get("type") == "event_msg" and payload_type == "user_message":
            # User text may answer an async question without opening a new turn.
            _settle_codex_async_user_text(str(payload.get("message") or ""), async_pending)
            relevant = True
        elif payload_type == "task_started":
            async_pending.clear()
            phase = "working"
            relevant = True
        elif payload_type == "task_complete":
            async_pending.clear()
            pending.clear()
            phase = "idle"
            activity_token = _token("codex", "complete", native) or activity_token
            relevant = True
        elif payload_type == "turn_aborted":
            async_pending.clear()
            pending.clear()
            phase = "idle"
            activity_token = _token("codex", "aborted", native) or activity_token
            relevant = True
        elif payload_type == "error":
            async_pending.clear()
            phase = "idle"
            relevant = True
        elif payload_type == "item_completed":
            item = payload.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "AgentMessage"
                and item.get("delivery") == "async"
                and isinstance(item.get("questions"), list)
                and item["questions"]
                and item.get("id")
            ):
                async_pending[str(item["id"])] = set(range(len(item["questions"])))
                relevant = True
        elif payload_type == "function_call" and payload.get("name") == "request_user_input_async":
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            try:
                args = payload.get("arguments") or {}
                args = json.loads(args) if isinstance(args, str) else args
            except (ValueError, TypeError):
                args = {}
            questions = args.get("questions") if isinstance(args, dict) else None
            if call_id and isinstance(questions, list) and questions:
                async_pending[call_id] = set(range(len(questions)))
                relevant = True
        elif payload_type == "agent_message":
            phase = "working"
            activity_token = _token("codex", "assistant", native) or activity_token
            relevant = True
        elif payload_type == "function_call" and payload.get("name") == "request_user_input":
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            if call_id:
                pending[call_id] = _token("codex", "question", call_id) or call_id
                relevant = True
        elif entry.get("type") == "response_item" and payload_type in {
            "reasoning", "function_call", "custom_tool_call", "function_call_output", "custom_tool_call_output",
        }:
            # A bounded tail can omit task_started during a long tool-heavy turn.
            # Native response items are execution evidence; process presence is not.
            call_id = str(payload.get("call_id") or "")
            pending.pop(call_id, None)
            if payload_type in {"function_call_output", "custom_tool_call_output"} and call_id in async_pending:
                from corral.remote.richmsg import _is_async_acceptance_receipt

                if not _is_async_acceptance_receipt(payload.get("output")):
                    async_pending.pop(call_id, None)
            phase = "working"
            relevant = True
        elif (
            entry.get("type") == "response_item"
            and payload_type == "message"
            and payload.get("role") == "assistant"
        ):
            phase = "idle" if payload.get("channel") == "final" else "working"
            activity_token = _token("codex", "assistant", native) or activity_token
            relevant = True
        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            if pending.pop(str(payload.get("call_id") or ""), None) is not None:
                phase = "working"
                relevant = True
        if relevant:
            observed_at = _advance_observed(
                observed_at,
                entry.get("timestamp"),
                payload.get("completed_at"),
                payload.get("started_at"),
            )

    state.update(phase=phase, activity_token=activity_token, observed_at=observed_at)

    if async_pending and live:
        call_id = next(reversed(async_pending))
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=_token("codex", "question", call_id),
            observed_at=observed_at,
        )
    if pending and live:
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=next(reversed(pending.values())),
            observed_at=observed_at,
        )
    phase = _finalize_history_phase(phase, live)
    return _evidence(phase, activity_token=activity_token, observed_at=observed_at)


def _inspect_kimi(session: dict) -> AttentionEvidence:
    path = str(session.get("path") or "")
    try:
        before_stat = os.stat(path)
        before_signature = (before_stat.st_size, before_stat.st_mtime_ns)
    except OSError:
        before_signature = None
    entries = _read_jsonl_tail(path)
    try:
        after_stat = os.stat(path)
        stable_read = before_signature == (after_stat.st_size, after_stat.st_mtime_ns)
    except OSError:
        stable_read = False
    if not entries:
        return _evidence(observed_at=_stable_observed_at(session, path))

    live = session.get("live") is True
    phase = "unknown"
    pending: dict[str, str] = {}
    activity_token = None
    last_structured_type = None
    last_structured_native = None
    observed_at = _stable_observed_at(session, path)

    for entry in entries:
        top_type = entry.get("type")
        event = entry.get("event")
        event = event if isinstance(event, dict) else {}
        event_type = event.get("type")
        native = _event_native(event, "uuid", "toolCallId", "turnId", "messageId")
        if native is None:
            native = _event_native(entry, "uuid", "time")

        if top_type == "turn.prompt":
            # 提问落盘不是执行证据；真正在跑要看随后的 tool.call / content.part。
            last_structured_type = top_type
            last_structured_native = native
            observed_at = _advance_observed(observed_at, entry.get("time"))
        elif top_type == "turn.cancel":
            phase = "idle"
            last_structured_type = top_type
            last_structured_native = native
            activity_token = _token("kimi", "cancel", native) or activity_token
            observed_at = _advance_observed(observed_at, entry.get("time"))
        elif top_type == "context.append_loop_event":
            last_structured_type = event_type
            last_structured_native = native
            if event_type in {"tool.call", "tool.result", "content.part", "step.end"}:
                observed_at = _advance_observed(observed_at, entry.get("time"))
            if event_type == "tool.call" and event.get("name") == "AskUserQuestion":
                phase = "working"
                call_id = str(event.get("toolCallId") or event.get("uuid") or "")
                if call_id:
                    pending[call_id] = _token("kimi", "question", call_id) or call_id
            elif event_type == "tool.result":
                if pending.pop(str(event.get("toolCallId") or ""), None) is not None:
                    phase = "working"
            elif event_type == "content.part":
                part = event.get("part")
                if isinstance(part, dict) and part.get("type") == "text":
                    phase = "working"
                    activity_token = _token("kimi", "assistant", native) or activity_token

    # step.end 既可能是中间工具步，也可能是本轮末尾。只有它确为尾部最新结构化
    # 事件且读取前后文件签名稳定时，才保守视为整轮停止；mtime 绝不单独产生状态。
    if last_structured_type == "step.end":
        if stable_read:
            phase = "idle"
            activity_token = _token("kimi", "step_end", last_structured_native) or activity_token

    if pending and live:
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=next(reversed(pending.values())),
            observed_at=observed_at,
        )
    phase = _finalize_history_phase(phase, live)
    return _evidence(phase, activity_token=activity_token, observed_at=observed_at)


def _inspect_pi(session: dict) -> AttentionEvidence:
    """从 Pi 已落盘的完整消息与工具调用判断会话关注状态。

    Pi 只在一轮助手输出完成后写入 assistant 消息，因此 ``stop``、``error`` 和
    ``aborted`` 都是稳定的空闲证据。常驻 TUI 不退出时 ``live`` 仍可为真，但不能
    只因为最后一条是用户消息就亮绿；执行中只认尚未收束的工具调用。自定义扩展若
    使用统一的结构化提问工具名，同样可得到等待回答提示。

    身份扩展 1.1+ 在 claim 上附带 ``agentPhase``（来自 ``agent_start`` /
    ``agent_settled`` / ``ui_prompt_*``）。Working 尚未落盘时由该观察相位补绿/黄点，
    仍禁止用「进程还在」冒充执行中。
    """
    path = str(session.get("path") or "")
    entries = _read_jsonl_tail(path)
    if not entries:
        history = _evidence(observed_at=_stable_observed_at(session, path))
        return _merge_pi_claim_phase(session, history)

    live = session.get("live") is True
    phase = "unknown"
    pending: dict[str, str] = {}
    activity_token = None
    observed_at = _stable_observed_at(session, path)

    for entry in entries:
        if entry.get("type") != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        native = _event_native(entry, "id", "timestamp")
        timestamp = message.get("timestamp") or entry.get("timestamp")

        if role == "user":
            # 用户开口不是执行证据。额度挂掉、窗口停在输入框时最后一条也常是 user。
            observed_at = _advance_observed(observed_at, timestamp)
            continue

        if role == "toolResult":
            call_id = str(message.get("toolCallId") or "")
            if pending.pop(call_id, None) is not None:
                phase = "working"
                observed_at = _advance_observed(observed_at, timestamp)
            continue

        if role != "assistant":
            continue

        tool_calls = [
            part for part in _content_parts(entry) if part.get("type") == "toolCall"
        ]
        for tool_call in tool_calls:
            if tool_call.get("name") not in _QUESTION_TOOLS:
                continue
            call_id = str(tool_call.get("id") or "")
            if call_id:
                pending[call_id] = _token("pi", "question", call_id) or call_id

        stop_reason = str(message.get("stopReason") or "")
        if tool_calls or stop_reason == "toolUse":
            phase = "working"
        elif stop_reason in {"stop", "error", "aborted", "length"}:
            phase = "idle"
            activity_token = _token("pi", "assistant", native) or activity_token
        observed_at = _advance_observed(observed_at, timestamp)

    if pending and live:
        history = _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=next(reversed(pending.values())),
            observed_at=observed_at,
        )
        return _merge_pi_claim_phase(session, history)
    phase = _finalize_history_phase(phase, live)
    history = _evidence(phase, activity_token=activity_token, observed_at=observed_at)
    return _merge_pi_claim_phase(session, history)


def _merge_pi_claim_phase(session: dict, history: AttentionEvidence) -> AttentionEvidence:
    """Overlay live claim agentPhase onto history when the TUI is ahead of jsonl."""
    if session.get("live") is not True:
        return history
    claim_phase = str(session.get("agent_phase") or "").strip()
    if claim_phase not in {"working", "waiting"}:
        return history
    # Structured history questions stay authoritative for yellow dots.
    if history.phase == "waiting" and history.question_token:
        return history
    observed_at = _timestamp(session.get("agent_phase_at")) or 0.0
    if observed_at <= 0:
        observed_at = history.observed_at or _stable_observed_at(
            session, str(session.get("path") or "")
        )
    # Stale claim working must not override a newer completed assistant stop.
    if (
        history.phase == "idle"
        and history.activity_token
        and history.observed_at > observed_at
    ):
        return history
    event = str(session.get("agent_phase_event") or claim_phase)
    token = _token("pi", "claim", f"{event}\0{session.get('agent_phase_at') or observed_at}")
    if claim_phase == "waiting":
        return AttentionEvidence(
            phase="waiting",
            activity_token=token,
            question_token=token or "pi-ui-prompt",
            observed_at=observed_at,
            source="observer",
        )
    return AttentionEvidence(
        phase="working",
        activity_token=token,
        observed_at=observed_at,
        source="observer",
    )


def _connect_ro(path: str, *, immutable: bool = False) -> sqlite3.Connection | None:
    try:
        suffix = "?mode=ro&immutable=1" if immutable else "?mode=ro"
        connection = sqlite3.connect(f"file:{os.path.abspath(path)}{suffix}", uri=True, timeout=0.15)
        connection.row_factory = sqlite3.Row
        return connection
    except (OSError, sqlite3.Error):
        return None


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return {}
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _opencode_has_v1_session(connection: sqlite3.Connection, session_id: str) -> bool:
    """id 是否落在 v1 session 表里（含双表并存的迁移行）。

    双表并存时扫描/对话/transcript 一律走 v1（SessKit `_is_v1_session` /
    `_opencode_use_v2` 同口径），关注证据必须跟随，否则同一会话的绿点
    与对话/状态看到的不是同一份历史。探针失败（纯 v2 库无此表）按无命中
    处理，调用方继续走 v2。"""
    try:
        hit = connection.execute(
            "SELECT 1 FROM session WHERE id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        return hit is not None
    except sqlite3.Error:
        return False


def _opencode_v2_tail_rows(connection: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    """v2 尾部行（seq 降序）；查不到表时返回空列表，调用方走未知态降级。"""
    try:
        return connection.execute(
            "SELECT type, seq, time_created, time_updated, id, data FROM session_message "
            "WHERE session_id = ? ORDER BY seq DESC, id DESC LIMIT ?",
            (session_id, _DB_TAIL_ROWS),
        ).fetchall()
    except sqlite3.Error:
        return []


def _inspect_opencode_v2_tail(
    tail: list[sqlite3.Row],
    *,
    live: bool,
    observed_at: float,
) -> AttentionEvidence | None:
    """v2 专属会话的关注证据；空尾部返回 None，由调用方回落 v1 分支。

    只收 v1 session 表里没有的 id（双表并存的迁移行由调用方先拦，走 v1）。
    口径（只读实测契约，与 SessKit v2 解析同源）：
    - 倒序跳过 system/synthetic/compaction/idle/agent-switched/model-switched
      后首行定状态：user → idle（开口不是执行证据，见 Pi 同条）；
      assistant + 非空 error → idle（报错是终止态，等同 v1 finish=stop）；
      finish=stop → idle；finish=tool-calls → live 才 working；
      finish 缺失/unknown（落盘中）→ 有文本/工具才 working，否则 unknown；
    - question 工具（state.metadata.answers 为空）→ waiting，completed/error
      仍带 answers 即视为已答；尾部 idle failed 不出事件但翻转本轮 working。
    - 进程已死一律收成 idle：常驻空转与已结束都不能留 working/waiting。
      v2 有行但全是跳过型尾巴时回落 v1（v1 行为零变化）；v1 缺表的新库回落
      unknown（reconcile 只认明确 working/waiting，unknown 不会冒充绿点）。
    """
    if not tail:
        return None
    pending: dict[str, str] = {}
    activity_token: str | None = None
    relevant_times: list[float] = []
    skipped_idle_failed = False
    newest: sqlite3.Row | None = None
    for row in tail:
        row_type = str(row["type"] or "")
        if row_type in {"system", "synthetic", "compaction", "idle", "agent-switched", "model-switched"}:
            if row_type == "idle":
                idle_data = _json_object(row["data"])
                if idle_data.get("outcome") == "failed":
                    skipped_idle_failed = True
            continue
        newest = row
        break
    if newest is None:
        return None
    message = _json_object(newest["data"])
    role = str(newest["type"] or "")
    created = message.get("time", {}).get("created") if isinstance(message.get("time"), dict) else None
    message_time = _timestamp(created) or _timestamp(newest["time_updated"]) or _timestamp(newest["time_created"])
    if message_time is not None:
        relevant_times.append(message_time)
    if role == "user":
        phase: str = "idle"
    elif role == "assistant":
        native = newest["id"] or newest["time_updated"] or newest["time_created"]
        activity_token = _token("opencode", "assistant", native)
        if message.get("error"):
            phase = "idle"
        elif message.get("finish") == "stop":
            phase = "idle"
        elif message.get("finish") == "tool-calls":
            phase = "working" if live else "idle"
        else:
            content = message.get("content")
            items = content if isinstance(content, list) else []
            has_signal = any(
                isinstance(item, dict)
                and (
                    (item.get("type") == "text" and isinstance(item.get("text"), str) and item["text"].strip())
                    or (item.get("type") == "tool")
                )
                for item in items
            )
            if live and has_signal:
                phase = "working"
            elif not live:
                phase = "idle"
            else:
                phase = "unknown"
        # 尾部向前扫 question 工具：v2 落盘只有 completed/error 两种终态；
        # completed + metadata.answers 非空 = 已作答；completed 无 answers =
        # 仍在等用户作答（落盘早于作答）；error = 已驳回/参数错，不算等待。
        for row in tail:
            if str(row["type"] or "") != "assistant":
                if str(row["type"] or "") == "user":
                    break
                continue
            data = _json_object(row["data"])
            content = data.get("content")
            items = content if isinstance(content, list) else []
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "tool":
                    continue
                if item.get("name") not in _QUESTION_TOOLS:
                    continue
                state = item.get("state") if isinstance(item.get("state"), dict) else {}
                if state.get("status") in {"pending", "running"}:
                    call_id = str(item.get("id") or row["id"] or "")
                    if call_id:
                        pending[call_id] = _token("opencode", "question", call_id) or call_id
                    continue
                if state.get("status") == "completed":
                    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
                    if metadata.get("answers"):
                        pending.pop(str(item.get("id") or ""), None)
                    else:
                        call_id = str(item.get("id") or row["id"] or "")
                        if call_id:
                            pending[call_id] = _token("opencode", "question", call_id) or call_id
            if data.get("finish") == "stop" or data.get("error"):
                break
    else:
        phase = "unknown"
    if skipped_idle_failed and phase == "working":
        phase = "idle"
    if relevant_times:
        observed_at = max(relevant_times)
    if pending and live:
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=next(reversed(pending.values())),
            observed_at=observed_at,
        )
    if not live:
        phase = "idle" if phase in {"working", "waiting"} else phase
        if phase == "unknown":
            phase = "idle"
    return _evidence(phase, activity_token=activity_token, observed_at=observed_at)


def _inspect_opencode(session: dict) -> AttentionEvidence:
    db_path = str(session.get("path") or "")
    session_id = str(session.get("id") or "")
    observed_at = _stable_observed_at(session, db_path)
    if not db_path or not session_id or not os.path.isfile(db_path):
        return _evidence(observed_at=observed_at)
    connection = _connect_ro(db_path)
    if connection is None:
        return _evidence(observed_at=observed_at)
    live = session.get("live") is True
    v2_tail: list[sqlite3.Row] = []
    try:
        # 双表并存的迁移行走 v1（与扫描/对话/transcript 同口径），v1 行为零变化。
        if not _opencode_has_v1_session(connection, session_id):
            v2_tail = _opencode_v2_tail_rows(connection, session_id)
        if v2_tail:
            v2_evidence = _inspect_opencode_v2_tail(v2_tail, live=live, observed_at=observed_at)
            if v2_evidence is not None:
                return v2_evidence
            # v2 有行但全是跳过型尾巴（compaction/idle）：v1 行为零变化，回落 v1。
        messages = connection.execute(
            "SELECT id, time_created, time_updated, data FROM message "
            "WHERE session_id = ? ORDER BY time_created DESC, id DESC LIMIT ?",
            (session_id, _DB_TAIL_ROWS),
        ).fetchall()
        parts = connection.execute(
            "SELECT id, message_id, time_created, time_updated, data FROM part "
            "WHERE session_id = ? ORDER BY time_created DESC, id DESC LIMIT ?",
            (session_id, _DB_TAIL_ROWS),
        ).fetchall()
    except sqlite3.Error:
        # v1 缺表的新库：v2 已判过（v2_tail 非空即已返回），此处仅当 v2
        # 无行又无 v1 表——回 unknown。注意 reconcile() 里仍活着的 unknown
        # 不会自动变绿：要亮绿必须有明确 working/waiting 证据，见知识库。
        if not v2_tail:
            return _evidence(observed_at=observed_at)
        messages = []
        parts = []
    finally:
        connection.close()

    pending: dict[str, str] = {}
    activity_token = None
    relevant_times: list[float] = []
    newest_id = str(messages[0]["id"] or "") if messages else ""
    newest_has_parts = False
    newest_open_step = False
    running_tools = False
    # 逆序恢复时间顺序，确保回答/完成能消掉此前的问题。
    for row in reversed(parts):
        part = _json_object(row["data"])
        part_type = part.get("type")
        row_time = _timestamp(row["time_updated"]) or _timestamp(row["time_created"])
        if newest_id and str(row["message_id"] or "") == newest_id:
            newest_has_parts = True
            if part_type == "step-start":
                newest_open_step = True
            elif part_type == "step-finish":
                newest_open_step = False
        if part_type != "tool":
            continue
        if row_time is not None:
            relevant_times.append(row_time)
        call_id = str(part.get("callID") or row["id"] or "")
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = state.get("status")
        if part.get("tool") in _QUESTION_TOOLS:
            if status in {"pending", "running"} and call_id:
                pending[call_id] = _token("opencode", "question", call_id) or call_id
            elif call_id:
                pending.pop(call_id, None)
        elif status in {"pending", "running"}:
            running_tools = True

    phase = "unknown"
    if messages:
        newest = messages[0]
        message = _json_object(newest["data"])
        role = message.get("role")
        native = newest["id"] or newest["time_updated"] or newest["time_created"]
        message_time = _timestamp(newest["time_updated"]) or _timestamp(newest["time_created"])
        if role in {"assistant", "user"} and message_time is not None:
            relevant_times.append(message_time)
        if role == "assistant":
            completed = (message.get("time") or {}).get("completed") if isinstance(message.get("time"), dict) else None
            finish = message.get("finish")
            activity_token = _token("opencode", "assistant", native)
            if message.get("error") or finish == "stop" or completed is not None:
                phase = "idle"
            elif live and (running_tools or newest_open_step or newest_has_parts):
                # 本条助手消息已经开始写 step/正文/工具，才算正在跑。
                phase = "working"
            else:
                # 空的下一轮助手占位：tool-calls 完成后 OpenCode 会先插入下一条
                # assistant 行；没有 part 就还没开始生成，常驻窗口不能亮绿。
                # 进程已死且没有完成标记时同样视为空闲，避免 status 停在 unknown。
                phase = "idle"
        elif role == "user":
            phase = "idle"

    if live and (running_tools or newest_open_step) and phase != "waiting":
        phase = "working"

    if relevant_times:
        observed_at = max(relevant_times)

    if pending and live:
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=next(reversed(pending.values())),
            observed_at=observed_at,
        )
    return _evidence(phase, activity_token=activity_token, observed_at=observed_at)


def _cursor_store_path(path: str) -> str:
    return os.path.join(path, "store.db") if os.path.isdir(path) else path


def _pb_read_varint(data: bytes, index: int) -> tuple[int, int] | None:
    shift = 0
    value = 0
    while index < len(data):
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, index
        shift += 7
        if shift > 63:
            return None
    return None


def _pb_fields(data: bytes) -> dict[int, list[bytes | int]]:
    """解析一层 protobuf 字段；损坏输入返回已读到的部分，不抛错。"""
    fields: dict[int, list[bytes | int]] = {}
    index = 0
    while index < len(data):
        parsed = _pb_read_varint(data, index)
        if parsed is None:
            break
        tag, index = parsed
        field = tag >> 3
        wire = tag & 7
        if wire == 0:
            parsed = _pb_read_varint(data, index)
            if parsed is None:
                break
            value, index = parsed
            fields.setdefault(field, []).append(value)
        elif wire == 2:
            parsed = _pb_read_varint(data, index)
            if parsed is None:
                break
            length, index = parsed
            end = index + length
            if end > len(data):
                break
            fields.setdefault(field, []).append(data[index:end])
            index = end
        elif wire == 1:
            index += 8
        elif wire == 5:
            index += 4
        else:
            break
    return fields


def _cursor_ask_question_id(data: bytes) -> str | None:
    """从 Cursor 提问专用 protobuf 取出 toolCallId。

    等待用户作答时，AskQuestion 往往只写 field 2 包裹的记录（内层 field 23 为
    题目、field 57 为调用标识），JSON tool-call 要等用户选完才落盘。其它工具的
    同类 field 2 记录没有 field 23，不能当成提问。二进制 DAG（首字节 0x0A）不含
    这两字段，解析后会自然忽略。
    """
    if not data or data[:1] == b"{":
        return None

    def from_fields(fields: dict[int, list[bytes | int]]) -> str | None:
        questions = fields.get(23)
        call_ids = fields.get(57)
        if not questions or not call_ids:
            return None
        raw = call_ids[-1]
        if not isinstance(raw, bytes):
            return None
        call_id = raw.decode("utf-8", errors="replace").strip("\0")
        return call_id or None

    fields = _pb_fields(data)
    found = from_fields(fields)
    if found:
        return found
    for wrapped in fields.get(2, []):
        if isinstance(wrapped, bytes):
            found = from_fields(_pb_fields(wrapped))
            if found:
                return found
    return None


def _inspect_cursor(session: dict) -> AttentionEvidence:
    live = session.get("live") is True
    if not live and session.get("signal_probe") is not True:
        return _evidence(observed_at=_stable_observed_at(session))
    store_path = _cursor_store_path(str(session.get("path") or ""))
    observed_at = max(
        _stable_observed_at(session, store_path),
        _path_mtime(store_path),
        _path_mtime(store_path + "-wal"),
    )
    if not store_path or not os.path.isfile(store_path):
        return _evidence(observed_at=observed_at)
    # 有 WAL 时绝不能 immutable：冷会话若刚结束、最新轮次还在 wal 里，
    # immutable 会读到过期尾巴，关注圆点/已读基线都会偏。
    has_wal = os.path.isfile(store_path + "-wal")
    connection = _connect_ro(store_path, immutable=not live and not has_wal)
    if connection is None:
        return _evidence(observed_at=observed_at)
    try:
        json_rows = connection.execute(
            "SELECT rowid, data FROM blobs WHERE substr(data, 1, 1) = X'7B' "
            "ORDER BY rowid DESC LIMIT ?",
            (_DB_TAIL_ROWS,),
        ).fetchall()
        # 提问等待态常只在 field-2 protobuf 里；与 JSON 分查，避免当前提问被
        # 大量 JSON 尾巴挤出窗口。旧提问的 JSON 结果滚出窗口后，不能只凭还在
        # protobuf 尾巴里就判成仍在等待——见下方 continuation / JSON 窗口过滤。
        proto_rows = connection.execute(
            "SELECT rowid, data FROM blobs WHERE substr(data, 1, 1) = X'12' "
            "ORDER BY rowid DESC LIMIT ?",
            (_DB_TAIL_ROWS,),
        ).fetchall()
    except sqlite3.Error:
        return _evidence(observed_at=observed_at)
    finally:
        connection.close()

    answered: set[str] = set()
    tool_calls: set[str] = set()
    questions: dict[str, str] = {}
    question_rowids: dict[str, int] = {}
    activity_token = None
    continuation_max = 0
    last_output_rowid = 0
    last_tool_activity_rowid = 0
    last_work_tool_rowid = 0
    min_json_rowid = min((row["rowid"] for row in json_rows), default=0)

    def _note_question(call_id: str, rowid: int) -> None:
        questions[call_id] = _token("cursor", "question", call_id) or call_id
        question_rowids[call_id] = max(question_rowids.get(call_id, 0), rowid)

    for row in reversed(json_rows):
        rowid = row["rowid"]
        message = _json_object(row["data"])
        role = message.get("role")
        content = message.get("content")
        if not isinstance(content, list):
            content = []
        has_text = False
        has_ask_question = False
        for part in content:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            tool_name = part.get("toolName")
            call_id = str(part.get("toolCallId") or "")
            if part_type == "tool-result" and call_id:
                answered.add(call_id)
                last_tool_activity_rowid = max(last_tool_activity_rowid, rowid)
                if tool_name != "AskQuestion":
                    continuation_max = max(continuation_max, rowid)
                    last_work_tool_rowid = max(last_work_tool_rowid, rowid)
            elif part_type == "tool-call" and tool_name == "AskQuestion" and call_id:
                has_ask_question = True
                _note_question(call_id, rowid)
            elif part_type == "tool-call" and call_id:
                tool_calls.add(call_id)
                last_tool_activity_rowid = max(last_tool_activity_rowid, rowid)
                last_work_tool_rowid = max(last_work_tool_rowid, rowid)
                continuation_max = max(continuation_max, rowid)
                activity_token = _token("cursor", "tool", rowid) or activity_token
            elif part_type == "text" and str(part.get("text") or "").strip():
                has_text = True
        if has_text and not has_ask_question:
            continuation_max = max(continuation_max, rowid)
            last_output_rowid = max(last_output_rowid, rowid)
        terminal = (
            message.get("stopReason")
            or message.get("finishReason")
            or message.get("stop_reason")
            or message.get("status")
        )
        is_terminal = terminal in {"stop", "stopped", "error", "abort", "aborted", "cancelled"}
        if role == "assistant" and (has_text or message.get("error") or is_terminal):
            kind = "terminal" if message.get("error") or is_terminal else "assistant"
            activity_token = _token("cursor", kind, rowid)
            if message.get("error") or is_terminal:
                last_output_rowid = max(last_output_rowid, rowid)

    for row in reversed(proto_rows):
        raw = row["data"]
        if isinstance(raw, memoryview):
            raw = bytes(raw)
        if not isinstance(raw, (bytes, bytearray)):
            continue
        rowid = row["rowid"]
        call_id = _cursor_ask_question_id(bytes(raw))
        if call_id:
            _note_question(call_id, rowid)
        else:
            # 其它工具的 field-2 记录：助手已经在提问之后继续干活。
            continuation_max = max(continuation_max, rowid)
            last_tool_activity_rowid = max(last_tool_activity_rowid, rowid)
            last_work_tool_rowid = max(last_work_tool_rowid, rowid)
            activity_token = _token("cursor", "tool", rowid) or activity_token

    pending: list[str] = []
    for call_id, token in questions.items():
        if call_id in answered:
            continue
        question_rowid = question_rowids.get(call_id, 0)
        # JSON 窗口已经前移时，更早的 protobuf 提问无法核对是否已答，
        # 不能当成仍在等待。正在等答时，提问记录会新于当前 JSON 尾巴。
        if json_rows and question_rowid < min_json_rowid:
            continue
        if question_rowid < continuation_max:
            continue
        pending.append(token)
    if pending and live:
        return _evidence(
            "waiting",
            activity_token=activity_token,
            question_token=pending[-1],
            observed_at=observed_at,
        )
    # 最新动作已是可见答复或结束标记时给出 idle，否则常驻进程会把旧执行中钉死。
    # 但「正在跑工具」（Globbing / Shell 等）必须亮绿：中间助手正文曾把观察器
    # working 冲成 idle 后，若这里只回 unknown，绿点会整轮回不来。
    # AskQuestion 的问答来回不算干活，避免答完选择题被误判成执行中。
    open_tools = tool_calls - answered
    tools_newest = last_work_tool_rowid > 0 and last_work_tool_rowid >= last_output_rowid
    if live and (open_tools or tools_newest):
        return _evidence(
            "working",
            activity_token=activity_token or _token("cursor", "tool", last_work_tool_rowid),
            observed_at=observed_at,
        )
    if last_output_rowid > last_tool_activity_rowid:
        return _evidence("idle", activity_token=activity_token, observed_at=observed_at)
    return _evidence("unknown", activity_token=activity_token, observed_at=observed_at)


_INSPECTORS = {
    "claude": _inspect_claude,
    "codex": _inspect_codex,
    "kimi": _inspect_kimi,
    "opencode": _inspect_opencode,
    "cursor": _inspect_cursor,
    "pi": _inspect_pi,
}


def inspect_session(session: dict) -> AttentionEvidence:
    """提取单个会话的结构化状态证据；未知来源或损坏输入安全降级。"""
    if not isinstance(session, dict):
        return _evidence()
    inspector = _INSPECTORS.get(str(session.get("source") or ""))
    if inspector is None:
        return _evidence()
    try:
        return inspector(session)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return _evidence()
