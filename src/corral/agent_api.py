#!/usr/bin/env python3
"""corral 的机器可读数据接口：面向大模型 Agent 的只读命令集合。

corral 只负责把本地会话数据结构化地交出来（列表 / 搜索 / 详情 / 接续上下文 / 分享 transcript），
不负责决定"拿到数据之后做什么"——不新增自动拉起、后台执行等副作用命令。
所有命令输出统一 JSON envelope：{ok, data, error, meta}。

退出码：0 成功、1 一般失败、2 用法错误、3 会话不存在、5 会话标识有歧义。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from corral import liveness, titles
from corral.cache import cache_dir, get_cache, scan_period
from corral.legacy_names import getenv
from corral.models import format_message_time, session_key
from corral.runtime import LaunchError, default_registry
from corral.runtime.base import usable_cwd
from corral.transcript import SCHEMA_ID, count_events, load_events

AGENT_API_VERSION = 1

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_AMBIGUOUS = 5

STATUS_LABELS = {
    titles.STATUS_DONE: "done",
    titles.STATUS_PENDING: "pending",
    titles.STATUS_ABORTED: "aborted",
    titles.STATUS_NONE: "unknown",
}

_RESOLVE_SCAN_LIMIT = 200  # show/share/context 按标识定位会话时的扫描深度，独立于 list/search 的展示条数

# --compact 模式下 list 的精简默认字段集（省 token）；需要更多字段用 --fields 显式指定
# live/keepalive/attention/last_user/last_agent 默认就带上：调用方一眼看懂这条会话在跑没跑、
# 有没有挂在 corral 托管里、界面关注态是等你 / 干活 / 未读，不必再多一次 show。
# pid 体积小但多数为 null，不进精简集，需要时用 --fields 或 --live 取。
DEFAULT_LIST_FIELDS = (
    "id", "short_id", "runtime", "title", "status", "live", "keepalive", "attention", "mtime",
    "cwd_display", "resumable", "resume_command", "last_user", "last_agent",
)
# search 的精简字段集在 list 基础上额外保留命中方式、命中字段和相关性得分，方便调用方理解排序
DEFAULT_SEARCH_FIELDS = DEFAULT_LIST_FIELDS + ("matched_via", "matched_fields", "score")
DEFAULT_SHOW_FIELDS = DEFAULT_LIST_FIELDS + ("messages", "message_count_shown", "message_count_total")

_SUMMARY_TRIM_LEN = 120  # last_user/last_agent 摘要的硬截断长度


def _trim(text: str | None, limit: int = _SUMMARY_TRIM_LEN) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


class ApiError(Exception):
    """携带退出码和结构化提示的命令级错误，由 dispatch 统一转成 JSON envelope。"""

    def __init__(self, code, message, exit_code=EXIT_ERROR, hint=None, next_commands=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.hint = hint
        self.next_commands = next_commands or []


class JSONArgumentParser(argparse.ArgumentParser):
    """参数错误时输出 JSON envelope + 退出码 2，而不是纯文本 usage 信息。"""

    def error(self, message):
        _print_envelope({
            "ok": False,
            "data": None,
            "error": {
                "code": "usage_error",
                "message": message,
                "hint": "运行 corral describe 或 corral describe <command> 查看用法",
                "next_commands": [f"{_bin()} describe"],
            },
            "meta": {"version": AGENT_API_VERSION},
        })
        raise SystemExit(EXIT_USAGE)


def _print_envelope(payload: dict, compact: bool = False) -> None:
    if compact:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def _bin() -> str:
    """当前 CLI 的绝对入口身份：next_commands 必须保持它，而不是裸命令名。

    pipx 与源码树两份 corral 并存是常态（diagnose 的 stale_source_warning 即为此存在）；
    裸 `corral show …` 可能命中 PATH 上另一份 capabilities 对不上的副本。给人类看的 hint
    文案仍用短名，只有机器照抄的 next_commands 走绝对身份。
    """
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0.endswith("__main__.py"):
        # `python -m corral …` 时 argv[0] 是 __main__.py 路径，不能直接执行，拼回 -m 形式。
        return f"{sys.executable} -m corral"
    if argv0:
        candidate = argv0 if os.path.isabs(argv0) else os.path.abspath(argv0)
        try:
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        except OSError:
            pass
    return shutil.which("corral") or "corral"


def _resolve_out_path(out: str) -> str:
    """校验 --out 写路径并返回规范绝对路径。

    写文件只允许发生在调用方显式传 --out 时（默认无副作用）；这里只做输入校验，
    不改变“已存在同名普通文件会被覆盖”的既有语义：
    - 拒绝空路径与 NUL 字节；
    - 拒绝已存在的目录与不存在的父目录（沿用旧报错文案）；
    - 拒绝已存在但不是普通文件的目标（fifo/socket/device），避免把 JSON 灌进奇怪的节点。
    """
    if not out or "\x00" in out:
        raise ApiError(
            "usage_error", f"输出路径无效：{out!r}", EXIT_USAGE,
            hint="--out 需要一个可写的文件路径，父目录必须已存在",
            next_commands=[f"{_bin()} describe"],
        )
    output_path = os.path.abspath(out)
    if os.path.isdir(output_path):
        raise ApiError("usage_error", f"输出路径是目录：{output_path}", EXIT_USAGE)
    parent = os.path.dirname(output_path) or "."
    if not os.path.isdir(parent):
        raise ApiError("usage_error", f"输出目录不存在：{parent}", EXIT_USAGE)
    if os.path.lexists(output_path) and not os.path.isfile(output_path):
        raise ApiError(
            "usage_error", f"输出路径不是普通文件，拒绝写入：{output_path}", EXIT_USAGE,
            hint="--out 只能写入普通文件",
        )
    return output_path


# session_payload 产出的全部字段名：--fields 白名单的真源（与 payload 共用定义，不另写一套）。
_SESSION_FIELD_NAMES = frozenset({
    "runtime", "id", "short_id", "title", "cwd", "cwd_display", "time", "mtime",
    "size_kb", "status", "status_tag", "history_path", "resumable", "resume_command",
    "live", "keepalive", "attention", "pid", "last_user", "last_agent",
})
_SEARCH_EXTRA_FIELDS = frozenset({"score", "matched_via", "matched_fields", "snippet"})
_SHOW_EXTRA_FIELDS = frozenset({"messages", "message_count_shown", "message_count_total"})


def _check_fields(fields: list[str] | None, valid: frozenset, command: str) -> None:
    """未知 --fields 必须报 usage_error，而不是静默丢弃。

    静默丢弃比直接报错更危险：调用方看到 exit 0，以为过滤生效，实际证据与请求对不上。
    错误里直接列出可用字段——这是最便宜的 Agent 自愈（照着改就行，不用再查一轮 describe）。
    """
    if not fields:
        return
    unknown = [f for f in fields if f not in valid]
    if unknown:
        raise ApiError(
            "usage_error",
            f"未知字段：{', '.join(unknown)}（{command} 可用字段：{', '.join(sorted(valid))}）",
            EXIT_USAGE,
            hint="字段名以 describe 为准",
            next_commands=[f"{_bin()} describe {command}"],
        )


def _scan_coverage(scanned: dict[str, list[dict]], failures: dict[str, str], limit: int) -> dict:
    """list/search/export 共用的覆盖面自述。

    `count=0` 只有在已证明的覆盖面里才等于“没有命中”：读了 0 条、有分片失败、
    或某家正好顶到扫描深度（截断信号），都必须显式说出来，不能让调用方把三种
    情况都总结成“全量干净”。
    """
    truncated = sorted(rid for rid, items in scanned.items() if limit and len(items) >= limit)
    return {
        "scanned": sum(len(items) for items in scanned.values()),
        "truncated_runtimes": truncated,
        "failed_runtimes": dict(failures),
        "potentially_limited": bool(failures) or bool(truncated),
    }


def _ok(data) -> dict:
    return {"ok": True, "data": data, "error": None, "meta": {"version": AGENT_API_VERSION}}


def _err(exc: ApiError) -> dict:
    return {
        "ok": False,
        "data": None,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "hint": exc.hint,
            "next_commands": exc.next_commands,
        },
        "meta": {"version": AGENT_API_VERSION},
    }


def _format_resume_command(argv: tuple[str, ...]) -> str:
    """把启动计划的 argv 拼成可直接在 shell 中运行的命令字符串。"""
    parts = []
    for arg in argv:
        if " " in arg or "\n" in arg or '"' in arg:
            escaped = arg.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'"{escaped}"')
        else:
            parts.append(arg)
    return " ".join(parts)


def _resume_command(runtime, session: dict) -> str | None:
    """生成同运行时原生恢复命令；失败时只标记不可恢复，不影响列表输出。"""
    try:
        plan = runtime.build_resume_plan(session)
        return _format_resume_command(plan.argv)
    except Exception:
        return None


def _load_conversation(runtime, session: dict):
    """Agent 查询与 TUI 共用持久正文缓存；文件变化时由签名自动失效。"""
    runtime_id = str(session.get("source") or "")
    key = session_key(session)
    path = str(session.get("path") or "")
    cached = get_cache().get_conversation(runtime_id, key, path) if path else None
    if cached is not None:
        return cached
    messages = runtime.load_conversation(session)
    if path:
        get_cache().put_conversation(runtime_id, key, path, list(messages))
    return messages


def _apply_fields(payload: dict, fields: list[str] | None) -> dict:
    if not fields:
        return payload
    return {k: v for k, v in payload.items() if k in fields}


def _attach_attention(sessions: list[dict]) -> None:
    """把关注态写进扫描结果，供 session_payload 输出。库损坏时整批降为 none。"""
    from corral.attention import AttentionStore

    keys = [session_key(item) for item in sessions if item.get("id")]
    try:
        states = AttentionStore().get_many(keys)
    except Exception:
        states = {}
    for item in sessions:
        if not item.get("id"):
            item["attention_kind"] = "none"
            continue
        state = states.get(session_key(item))
        kind = getattr(state, "kind", None) if state is not None else None
        item["attention_kind"] = kind if kind in {"waiting", "working", "unread", "none"} else "none"


def session_payload(session: dict, cache: dict, runtime=None, fields: list[str] | None = None) -> dict:
    """把内部会话结构裁剪为对 Agent 友好的输出：语义化标题、英文状态枚举。"""
    title, _ = titles.resolve_initial_title(session, cache)
    status_tag = session.get("status_tag") or ""
    resume_command = _resume_command(runtime, session) if runtime is not None else None
    attention = session.get("attention_kind") or "none"
    if attention not in {"waiting", "working", "unread", "none"}:
        attention = "none"
    payload = {
        "runtime": session.get("source"),
        "id": session.get("id"),
        "short_id": session.get("short_id"),
        "title": title,
        "cwd": session.get("cwd") or "",
        "cwd_display": session.get("cwd_display") or "",
        "time": session.get("display_time") or "",
        "mtime": session.get("mtime"),
        "size_kb": round(session.get("size_kb") or 0, 1),
        "status": STATUS_LABELS.get(status_tag, "unknown"),
        "status_tag": status_tag,
        "history_path": session.get("path") or "",
        "resumable": bool(resume_command),
        "resume_command": resume_command,
        "live": bool(session.get("live")),
        "keepalive": bool(session.get("keepalive_name")),
        "attention": attention,
        "pid": session.get("pid"),
        "last_user": _trim(session.get("last_user_msg")),
        "last_agent": _trim(session.get("last_agent_msg")),
    }
    return _apply_fields(payload, fields)


def _match_sessions(sessions: list[dict], ident: str) -> list[dict]:
    return [
        s for s in sessions
        if s.get("id") == ident or str(s.get("id") or "").startswith(ident) or s.get("short_id") == ident
    ]


def _scan_runtimes(runtimes: list, limit: int) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """并发扫描多个运行时的会话列表，返回 ({运行时 id: 会话列表}, {运行时 id: 失败摘要})。

    与 runtime.registry.scan_all 同语义：各运行时读取完全独立的历史目录，
    线程池只是为了重叠磁盘 I/O 等待；单个运行时扫描异常时该分片降级为空列表，
    不拖累其余运行时的结果，也不让 list/search/show 等命令因为一条脏数据
    直接报错退出——但失败必须出现在 failures 里由调用方如实上报，不能吞成
    “该运行时没有会话”（解析失败要指向适配器升级，而不是零命中）。
    """
    def _scan_one(runtime):
        try:
            return (runtime.id, runtime.scan_sessions(limit), "")
        except Exception as exc:
            return (runtime.id, [], f"{type(exc).__name__}: {exc}")

    # 与 scan_all 一样开一轮扫描期，让每个运行时的派生缓存元数据只查一次库。
    # 扫描期协议收敛在 cache.scan_period（见其 docstring），异常由它吞掉，
    # 派生缓存永远不能影响原始会话扫描结果。
    with scan_period():
        with ThreadPoolExecutor(max_workers=max(1, len(runtimes))) as pool:
            rows = list(pool.map(_scan_one, runtimes))
    scanned = {rid: items for rid, items, _ in rows}
    failures = {rid: err for rid, _, err in rows if err}
    return scanned, failures


def resolve_ref(registry, ref: str, limit: int) -> dict:
    """把用户提供的会话标识（完整 ID / 前缀 / runtime:id）解析为唯一会话。"""
    if not ref:
        raise ApiError("usage_error", "缺少会话标识", EXIT_USAGE)

    failures: dict[str, str] = {}
    if ":" in ref:
        runtime_id, _, ident = ref.partition(":")
        try:
            runtime = registry.get(runtime_id)
        except LaunchError as exc:
            raise ApiError("not_found", f"未注册的运行时：{runtime_id}", EXIT_NOT_FOUND) from exc
        matches = _match_sessions(runtime.scan_sessions(limit), ident)
    else:
        runtimes = list(registry)
        scanned, failures = _scan_runtimes(runtimes, limit)
        matches = []
        for runtime in runtimes:
            matches.extend(_match_sessions(scanned[runtime.id], ref))

    if not matches:
        hint = "确认会话 ID 或前缀是否正确，可先用 corral search 或 corral list 查看"
        if failures:
            hint += f"；以下运行时本次扫描失败，结果可能不全：{', '.join(sorted(failures))}，可跑 corral diagnose 排查"
        raise ApiError(
            "not_found", f"未找到匹配会话：{ref}", EXIT_NOT_FOUND,
            hint=hint,
            next_commands=[f"{_bin()} search {ref}", f"{_bin()} list"],
        )
    liveness.annotate(matches)
    _attach_attention(matches)

    exact = [s for s in matches if s.get("id") == ref or session_key(s) == ref]
    if len(exact) == 1:
        return exact[0]
    if len(matches) > 1:
        candidates = [session_key(s) for s in matches[:10]]
        raise ApiError(
            "ambiguous", f"会话标识存在多个候选：{ref}", EXIT_AMBIGUOUS,
            hint="使用更长的前缀或完整 runtime:id",
            next_commands=[f"{_bin()} show {c}" for c in candidates],
        )
    return matches[0]


def _find_snippet(messages, keywords: list[str]) -> str | None:
    for message in messages:
        low = message.text.lower()
        for kw in keywords:
            idx = low.find(kw)
            if idx != -1:
                start = max(0, idx - 40)
                end = min(len(message.text), idx + len(kw) + 80)
                return message.text[start:end].strip()
    return None


def _parse_fields(raw: str | None, default: tuple[str, ...] | None = None) -> list[str] | None:
    if raw:
        return [f.strip() for f in raw.split(",") if f.strip()]
    if default:
        return list(default)
    return None


def _apply_top(items: list, top: int | None) -> list:
    if top is None:
        return items
    return items[:max(0, top)]


def _score_quick_match(session: dict, title: str, keywords: list[str]) -> tuple[int, list[str]]:
    sources = [
        ("title", title, 100),
        ("fallback_title", session.get("fallback_title"), 80),
        ("first_user_msg", session.get("first_user_msg"), 60),
        ("last_user_msg", session.get("last_user_msg"), 60),
        ("last_agent_msg", session.get("last_agent_msg"), 35),
        ("cwd", session.get("cwd"), 20),
        ("cwd_display", session.get("cwd_display"), 20),
    ]
    score = 0
    matched: list[str] = []
    for name, value, weight in sources:
        text = str(value or "").lower()
        if not text:
            continue
        hits = sum(1 for kw in keywords if kw in text)
        if hits:
            score += weight * hits
            matched.append(name)
    return score, matched


def cmd_list(args, registry) -> dict:
    compact = getattr(args, "compact", False)
    top = getattr(args, "top", None)
    fields = _parse_fields(getattr(args, "fields", None), DEFAULT_LIST_FIELDS if compact else None)
    _check_fields(fields, _SESSION_FIELD_NAMES, "list")
    runtimes = [registry.get(args.runtime)] if args.runtime else list(registry)
    cache = titles.load_cache()

    scanned, failures = _scan_runtimes(runtimes, args.limit)
    flat = [session for bucket in scanned.values() for session in bucket]
    liveness.annotate(flat)
    _attach_attention(flat)

    candidates = []
    for runtime in runtimes:
        for session in scanned[runtime.id]:
            if args.status and STATUS_LABELS.get(session.get("status_tag") or "", "unknown") != args.status:
                continue
            if args.cwd and args.cwd.lower() not in str(session.get("cwd") or "").lower():
                continue
            # 用 `is True` 而不是 truthy：AgentApiTests 里的 args 常是裸 mock.Mock()，
            # 没显式设置的属性会自动生成一个真值 Mock（不是抛异常/落回默认值），
            # truthy 判断会让老测试静默被过滤成"只剩 live 会话"。
            if getattr(args, "live", None) is True and not session.get("live"):
                continue
            if getattr(args, "keepalive", None) is True and not session.get("keepalive_name"):
                continue
            candidates.append((runtime, session))

    candidates.sort(key=lambda item: item[1].get("mtime") or 0, reverse=True)
    matched = len(candidates)
    coverage = _scan_coverage(scanned, failures, args.limit)
    candidates = _apply_top(candidates, top)
    sessions = [session_payload(session, cache, runtime, fields) for runtime, session in candidates]

    data = {
        "count": len(sessions),
        "scan_limit": args.limit,
        "top": top,
        "sessions": sessions,
        "matched": matched,
        "returned": len(sessions),
    }
    data.update(coverage)
    return _ok(data)


def cmd_search(args, registry) -> dict:
    keywords = [k.lower() for k in args.keywords]
    compact = getattr(args, "compact", False)
    top = getattr(args, "top", None)
    fields = _parse_fields(getattr(args, "fields", None), DEFAULT_SEARCH_FIELDS if compact else None)
    _check_fields(fields, _SESSION_FIELD_NAMES | _SEARCH_EXTRA_FIELDS, "search")
    runtimes = [registry.get(args.runtime)] if args.runtime else list(registry)
    cache = titles.load_cache()

    scanned, failures = _scan_runtimes(runtimes, args.limit)
    flat = [session for bucket in scanned.values() for session in bucket]
    liveness.annotate(flat)
    _attach_attention(flat)

    results = []
    for runtime in runtimes:
        for session in scanned[runtime.id]:
            # 用 `is True` 而不是 truthy：AgentApiTests 里的 args 常是裸 mock.Mock()，
            # 没显式设置的属性会自动生成一个真值 Mock（不是抛异常/落回默认值），
            # truthy 判断会让老测试静默被过滤成"只剩 live 会话"。
            if getattr(args, "live", None) is True and not session.get("live"):
                continue
            if getattr(args, "keepalive", None) is True and not session.get("keepalive_name"):
                continue
            title, _ = titles.resolve_initial_title(session, cache)
            quick_parts = [
                title,
                session.get("fallback_title"),
                session.get("first_user_msg"),
                session.get("last_user_msg"),
                session.get("last_agent_msg"),
                session.get("cwd"),
                session.get("cwd_display"),
            ]
            haystack = " ".join(filter(None, quick_parts)).lower()

            if all(kw in haystack for kw in keywords):
                score, matched_fields = _score_quick_match(session, title, keywords)
                results.append((score, "quick", matched_fields, runtime, session, None))
            elif args.deep:
                messages = _load_conversation(runtime, session)
                full_text = "\n".join(m.text for m in messages).lower()
                if all(kw in full_text for kw in keywords):
                    score, matched_fields = _score_quick_match(session, title, keywords)
                    matched_fields = matched_fields + ["conversation"]
                    score += 10
                    snippet = _find_snippet(messages, keywords)
                    results.append((score, "deep", matched_fields, runtime, session, snippet))

    results.sort(key=lambda item: (item[0], item[4].get("mtime") or 0), reverse=True)
    matched = len(results)
    coverage = _scan_coverage(scanned, failures, args.limit)
    results = _apply_top(results, top)
    sessions = []
    for score, matched_via, matched_fields, runtime, session, snippet in results:
        payload = session_payload(session, cache, runtime)
        payload["score"] = score
        payload["matched_via"] = matched_via
        payload["matched_fields"] = matched_fields
        if snippet:
            payload["snippet"] = snippet
        sessions.append(_apply_fields(payload, fields))

    data = {
        "query": args.keywords,
        "deep": args.deep,
        "count": len(sessions),
        "scan_limit": args.limit,
        "top": top,
        "sessions": sessions,
        "matched": matched,
        "returned": len(sessions),
    }
    data.update(coverage)
    return _ok(data)


def cmd_show(args, registry) -> dict:
    session = resolve_ref(registry, args.session, args.limit)
    cache = titles.load_cache()
    runtime = registry.get(str(session.get("source") or ""))
    compact = getattr(args, "compact", False)
    out = getattr(args, "out", None)
    fields = _parse_fields(getattr(args, "fields", None), DEFAULT_SHOW_FIELDS if compact else None)
    _check_fields(fields, _SESSION_FIELD_NAMES | _SHOW_EXTRA_FIELDS, "show")
    payload = session_payload(session, cache, runtime)
    messages = _load_conversation(runtime, session)
    total_messages = len(messages)
    if not args.full:
        n = args.messages if args.messages else 20
        messages = messages[-n:]

    payload["messages"] = [
        {
            "role": m.role,
            "text": m.text,
            "time": format_message_time(m.timestamp) if m.timestamp else None,
            "mtime": m.timestamp,
        }
        for m in messages
    ]
    payload["message_count_shown"] = len(payload["messages"])
    payload["message_count_total"] = total_messages

    if out:
        envelope = _ok(payload)
        output_path = _resolve_out_path(out)
        with open(output_path, "w", encoding="utf-8") as fp:
            json.dump(envelope, fp, ensure_ascii=False, separators=(",", ":") if compact else None,
                      indent=None if compact else 2)
            fp.write("\n")
        summary = session_payload(session, cache, runtime, DEFAULT_LIST_FIELDS if compact else None)
        summary.update({
            "output_path": output_path,
            "output_bytes": os.path.getsize(output_path),
            "message_count_written": len(payload["messages"]),
            "message_count_total": total_messages,
            "messages_omitted": True,
        })
        return _ok(summary)

    return _ok(_apply_fields(payload, fields))


_REL_TIME_UNITS = {"d": 86400, "h": 3600, "m": 60}  # 相对时间单位：天 / 时 / 分


def _parse_time_bound(raw: str, *, is_until: bool) -> float:
    """把时间边界文本解析为 Unix 时间戳。

    支持：相对时间 `7d`/`24h`/`30m`（距现在多久之前）、绝对时间
    `2026-07-20` / `2026-07-20 15:30` / `2026-07-20 15:30:45`、以及纯 Unix 时间戳。
    只给日期（无时分）时，`--until` 侧补足到当天 23:59:59，保证含当天全部会话。
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw[-1] in _REL_TIME_UNITS and raw[:-1].isdigit():
        return time.time() - int(raw[:-1]) * _REL_TIME_UNITS[raw[-1]]
    if raw.isdigit() and len(raw) >= 6:  # 长数字按 Unix 时间戳；短数字留给日期格式报错
        return float(raw)
    for fmt, date_only in (("%Y-%m-%d %H:%M:%S", False), ("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if date_only and is_until:
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt.timestamp()
    raise ApiError(
        "usage_error",
        f"无法解析时间：{raw}（支持 2026-07-20、'2026-07-20 15:30'、7d/24h/30m 或 Unix 时间戳）",
        EXIT_USAGE,
    )


def cmd_export(args, registry) -> dict:
    """按时间范围导出区间内所有会话的完整对话，合并为一个 JSON。

    只读：与 list 共用扫描 / 过滤，与 show --full 共用完整对话结构；不触发任何拉起或写会话操作。
    """
    since = _parse_time_bound(args.since, is_until=False) if args.since else None
    until = _parse_time_bound(args.until, is_until=True) if args.until else None
    if since is not None and until is not None and since > until:
        raise ApiError("usage_error", "起始时间晚于结束时间", EXIT_USAGE)

    compact = getattr(args, "compact", False)
    runtimes = [registry.get(args.runtime)] if args.runtime else list(registry)
    cache = titles.load_cache()

    scanned, failures = _scan_runtimes(runtimes, args.limit)
    flat = [session for bucket in scanned.values() for session in bucket]
    liveness.annotate(flat)
    _attach_attention(flat)

    candidates = []
    for runtime in runtimes:
        for session in scanned[runtime.id]:
            mtime = session.get("mtime")
            if since is not None and (mtime is None or mtime < since):
                continue
            if until is not None and (mtime is None or mtime > until):
                continue
            if args.status and STATUS_LABELS.get(session.get("status_tag") or "", "unknown") != args.status:
                continue
            if args.cwd and args.cwd.lower() not in str(session.get("cwd") or "").lower():
                continue
            candidates.append((runtime, session))

    # 时间正序排列，导出文件从早到晚顺读即为一条时间线
    candidates.sort(key=lambda item: item[1].get("mtime") or 0)

    sessions = []
    for runtime, session in candidates:
        payload = session_payload(session, cache, runtime)
        messages = _load_conversation(runtime, session)
        payload["messages"] = [
            {
                "role": m.role,
                "text": m.text,
                "time": format_message_time(m.timestamp) if m.timestamp else None,
                "mtime": m.timestamp,
            }
            for m in messages
        ]
        payload["message_count_total"] = len(messages)
        sessions.append(payload)

    data = {
        "range": {
            "since": since,
            "until": until,
            "since_display": format_message_time(since) if since else None,
            "until_display": format_message_time(until) if until else None,
        },
        "count": len(sessions),
        "scan_limit": args.limit,
        "sessions": sessions,
    }
    data.update(_scan_coverage(scanned, failures, args.limit))

    out = getattr(args, "out", None)
    if not out:
        return _ok(data)

    envelope = _ok(data)
    output_path = _resolve_out_path(out)
    with open(output_path, "w", encoding="utf-8") as fp:
        json.dump(envelope, fp, ensure_ascii=False,
                  separators=(",", ":") if compact else None,
                  indent=None if compact else 2)
        fp.write("\n")
    return _ok({
        "output_path": output_path,
        "output_bytes": os.path.getsize(output_path),
        "session_count": len(sessions),
        "message_count_total": sum(s["message_count_total"] for s in sessions),
        "range": data["range"],
        "sessions_omitted": True,
    })


def share_cache_path(session: dict) -> str:
    """TUI「导出会话」落盘路径：``<cache>/share/<runtime>_<id>-<stamp>.json``。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    raw = session_key(session)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw).strip("._") or "session"
    directory = cache_dir() / "share"
    os.makedirs(directory, exist_ok=True)
    path = directory / f"{safe}-{stamp}.json"
    if path.exists():
        path = directory / f"{safe}-{stamp}-{os.getpid()}.json"
    return str(path)


def build_share_payload(session: dict, registry) -> dict:
    cache = titles.load_cache()
    runtime = registry.get(str(session.get("source") or ""))
    events = load_events(session)
    payload = session_payload(session, cache, runtime)
    payload["schema"] = SCHEMA_ID
    payload["runtime_name"] = getattr(runtime, "display_name", "") or ""
    payload["events"] = events
    payload["event_count"] = len(events)
    payload["counts"] = count_events(events)
    return payload


def write_share_envelope(payload: dict, out_path: str, *, compact: bool = False) -> str:
    """把 share envelope 写到 ``out_path``，返回绝对路径。"""
    output_path = _resolve_out_path(out_path)
    with open(output_path, "w", encoding="utf-8") as fp:
        json.dump(
            _ok(payload),
            fp,
            ensure_ascii=False,
            separators=(",", ":") if compact else None,
            indent=None if compact else 2,
        )
        fp.write("\n")
    return output_path


def export_share_to_cache(session: dict, registry, *, compact: bool = True) -> str:
    """写出 share transcript 到缓存目录，返回绝对路径（TUI 导出会话用）。"""
    return write_share_envelope(
        build_share_payload(session, registry),
        share_cache_path(session),
        compact=compact,
    )


def cmd_share(args, registry) -> dict:
    """导出单条会话的统一 transcript（含 thinking / 工具调用），供其他 Agent 做元认知。

    只读：不改 ``show`` / ``export`` 的纯文本契约；解析失败时 events 为空列表而不是报错。
    """
    session = resolve_ref(registry, args.session, args.limit)
    compact = getattr(args, "compact", False)
    out = getattr(args, "out", None)
    payload = build_share_payload(session, registry)

    if not out:
        return _ok(payload)

    output_path = write_share_envelope(payload, out, compact=compact)
    cache = titles.load_cache()
    runtime = registry.get(str(session.get("source") or ""))
    summary = session_payload(session, cache, runtime, DEFAULT_LIST_FIELDS if compact else None)
    summary.update({
        "schema": SCHEMA_ID,
        "output_path": output_path,
        "output_bytes": os.path.getsize(output_path),
        "event_count": payload["event_count"],
        "counts": payload["counts"],
        "events_omitted": True,
    })
    return _ok(summary)


def cmd_context(args, registry) -> dict:
    session = resolve_ref(registry, args.session, args.limit)
    cache = titles.load_cache()
    title, _ = titles.resolve_initial_title(session, cache)

    runtime = registry.get(str(session.get("source") or ""))
    try:
        handoff = runtime.export_handoff(session, title)
    except LaunchError as exc:
        raise ApiError("history_unavailable", str(exc), EXIT_ERROR) from exc

    try:
        resume_plan = runtime.build_resume_plan(session)
        resume_command = _format_resume_command(resume_plan.argv)
    except Exception:
        resume_command = None

    return _ok({
        "runtime": handoff.source_runtime_id,
        "runtime_name": handoff.source_runtime_name,
        "id": session.get("id"),
        "title": handoff.title,
        "status": STATUS_LABELS.get(session.get("status_tag") or "", "unknown"),
        "live": bool(session.get("live")),
        "pid": session.get("pid"),
        "cwd": handoff.original_cwd,
        "cwd_exists": bool(usable_cwd(handoff.original_cwd)),
        "history_path": handoff.history_path,
        "history_reading_hint": handoff.history_reading_hint,
        "suggested_prompt": handoff.render_prompt(),
        "resume_command": resume_command,
    })


def cmd_plan_continue(args, registry) -> dict:
    """生成外部执行器可用的续接计划；本函数只构造数据，绝不启动进程。"""
    instruction = args.instruction
    if not instruction or not instruction.strip():
        raise ApiError("usage_error", "续接指令不能为空", EXIT_USAGE)

    session = resolve_ref(registry, args.session, args.limit)
    runtime = registry.get(str(session.get("source") or ""))
    try:
        plan = runtime.build_continue_plan(session, instruction)
    except LaunchError as exc:
        raise ApiError(
            "not_resumable", f"该会话无法生成续接计划：{exc}", EXIT_ERROR,
            hint="确认会话所属运行时支持带新指令的原生续接",
            next_commands=[f"{_bin()} context {session_key(session)}"],
        ) from exc
    except Exception as exc:
        raise ApiError(
            "not_resumable", f"该会话无法生成续接计划：{exc}", EXIT_ERROR,
            hint="确认会话所属运行时支持带新指令的原生续接",
            next_commands=[f"{_bin()} context {session_key(session)}"],
        ) from exc

    return _ok({
        "session_ref": session_key(session),
        "runtime": runtime.id,
        "id": session.get("id"),
        "cwd": session.get("cwd") or "",
        "capabilities": {
            "resume": True,
            "continue_with_instruction": True,
            "execution": "external_only",
        },
        "launch": {
            "argv": list(plan.argv),
            "cwd": plan.cwd,
        },
    })


def cmd_describe(args, registry) -> dict:
    if args.target:
        target = args.target if isinstance(args.target, str) else " ".join(args.target)
        spec = next((c for c in COMMANDS if c["name"] == target), None)
        if spec is None:
            raise ApiError(
                "not_found", f"未知命令：{target}", EXIT_NOT_FOUND,
                hint="运行 corral describe 查看全部命令",
            )
        return _ok(_describe_command(spec, full=True))
    return _ok({"commands": [_describe_command(spec, full=False) for spec in COMMANDS]})


def _keepalive_server_report() -> dict | None:
    """Scheduling class of the running keepalive server (clamped = hosted agents throttled)."""
    try:
        from corral import keepalive, tmux_server

        return tmux_server.server_report(keepalive.SOCKET_NAME, keepalive.tmux_env())
    except Exception:  # noqa: BLE001 - diagnose must never fail on this probe
        return None


def cmd_diagnose(args, registry) -> dict:
    """只读诊断：缓存路径、日志是否存在、runtime 配色自检、tmux 版本。不启动 TUI。"""
    import corral
    from corral import embed, observe, updater

    shots_dir = os.path.join(observe.CACHE_DIR, observe.SCREENSHOTS_DIR_NAME)
    tmux_version = None
    try:
        ver = embed._tmux_version()
        tmux_version = list(ver) if ver is not None else None
    except Exception:
        tmux_version = None
    install = updater.install_report()
    hints = [
        "界面异常/闪退时先看 data.last_error（最近一次完整 traceback）",
        "也可直接读 events.log / embed-error.log",
        "TUI 内按 F12 导出当前截图到 screenshots_dir",
        "开发机请用 bash scripts/dev-install.sh（editable），勿只信 python3 -c import",
    ]
    hints.extend(install.get("hints") or [])
    return _ok({
        "cache_dir": observe.CACHE_DIR,
        "events_log": observe.EVENTS_LOG,
        "events_log_exists": os.path.isfile(observe.EVENTS_LOG),
        "embed_error_log": observe.EMBED_ERROR_LOG,
        "embed_error_log_exists": os.path.isfile(observe.EMBED_ERROR_LOG),
        "last_error": observe.read_last_error(),
        "screenshots_dir": shots_dir,
        "debug": bool(getenv("DEBUG")),
        "runtime_label_style_claude": corral.runtime_label_style("claude"),
        "tmux_version": tmux_version,
        "keepalive_server": _keepalive_server_report(),
        "version": install["version"],
        "package_file": install["package_file"],
        "python": install["python"],
        "install_channel": install["channel"],
        "checkout_root": install["checkout_root"],
        "loaded_from_checkout": install["loaded_from_checkout"],
        "stale_source_warning": install["stale_source_warning"],
        "hints": hints,
    })


def _describe_command(spec: dict, full: bool) -> dict:
    entry: dict = {
        "name": spec["name"],
        "help": spec["help"],
        # 副作用分级：Agent 不试运行就能枚举安全边界（read = 只读无副作用）。
        "risk": spec.get("risk", "read"),
        "args": [],
    }
    for arg in spec.get("args", []):
        arg_entry: dict = {"flags": arg["flags"]}
        for key in ("help", "default", "choices", "required", "nargs"):
            if key in arg["kwargs"]:
                arg_entry[key] = arg["kwargs"][key]
        arg_type = arg["kwargs"].get("type")
        if isinstance(arg_type, type):
            # type 对象本身进不了 JSON envelope；只暴露类型名（如 "int"）供调用方组装参数。
            arg_entry["type"] = arg_type.__name__
        entry["args"].append(arg_entry)
    if full:
        entry["fields"] = spec.get("fields", {})
    return entry


COMMANDS = [
    {
        "name": "list",
        "help": "结构化列出已注册运行时的会话",
        "risk": "read",
        "args": [
            {"flags": ["--runtime"], "kwargs": {"help": "只看指定运行时（claude / codex / opencode / kimi / cursor）"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 50, "help": "每个运行时最多扫描多少条历史（扫描深度）"}},  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            {"flags": ["--top"], "kwargs": {"type": int, "help": "最多返回多少条结果；不影响扫描深度"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true", "help": "使用紧凑 JSON，并默认只返回常用字段"}},
            {"flags": ["--status"], "kwargs": {"choices": ["done", "pending", "aborted", "unknown"], "help": "按状态过滤"}},  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            {"flags": ["--cwd"], "kwargs": {"help": "按工作目录子串过滤（大小写不敏感）"}},
            {"flags": ["--live"], "kwargs": {"action": "store_true", "help": "只返回进程仍在运行的会话"}},
            {
                "flags": ["--keepalive"],
                "kwargs": {"action": "store_true", "help": "只返回正挂在 corral 后台保活（tmux）里的会话"},
            },
            {"flags": ["--fields"], "kwargs": {"help": "逗号分隔的字段名，只返回这些字段"}},
        ],
        "fields": {
            "runtime": "运行时标识（claude / codex / opencode / kimi / cursor）",
            "id": "会话完整 ID",
            "short_id": "会话短 ID（前 8 位）",
            "title": "会话标题（缓存的生成标题或本地兜底标题，不触发新生成）",
            "cwd": "原会话工作目录绝对路径",
            "cwd_display": "工作目录的展示形式（可能是缩短路径）",
            "time": "最后更新时间（人类可读）",
            "mtime": "最后更新时间（Unix 时间戳）",
            "size_kb": "历史文件大小（KB）",
            "status": "英文状态枚举：done / pending / aborted / unknown",
            "status_tag": "中文状态标签（含图标），供人类展示用",
            "live": "进程是否真实运行中（true/false），不是文件时间推断",
            "keepalive": "是否正挂在 corral 的后台保活（tmux）里；为 true 时 resume_command 会另起一个竞争同一份会话文件的新进程，应改用 corral 的 Enter/接回操作而不是 resume_command",  # noqa: E501 - describe 字段文档，一行一条，拆行破坏输出
            "attention": (
                "界面关注态英文枚举：waiting / working / unread / none；"
                "与侧栏圆点同源，不是「该不该阻止休眠」的结论"
            ),
            "pid": "运行中会话的进程号；非运行中为 null，可用于定位/发信号给该进程",
            "last_user": "最后一条真人消息，硬截断精简，一眼看懂最近在聊什么",
            "last_agent": "助手最后一轮回复片段，硬截断精简",
            "history_path": "历史 JSONL 文件路径",
            "scanned": "本次各运行时实际读到的会话总数（过滤前）；count=0 时先看它：为 0 说明根本没读到，不是“全量干净”",  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            "matched": "过滤后（扫描深度内、全部过滤条件命中）的会话数；--top 截断前",
            "returned": "本次实际返回的会话数（--top 截断后，与 count 一致）",
            "truncated_runtimes": "读满扫描深度（--limit）的运行时 ID 列表；命中说明该运行时可能还有更早的会话没读到，调大 --limit 再查",  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            "failed_runtimes": "本次扫描抛异常的运行时 {id: 失败摘要}；非空时零命中结论不可信",
            "potentially_limited": "truncated_runtimes 或 failed_runtimes 非空时为 true，提醒调用方覆盖面可能不全",
            "resumable": "是否可生成同运行时原生恢复命令",
            "resume_command": "同运行时原生恢复该会话的 shell 命令（可能为 null）",
        },
    },
    {
        "name": "search",
        "help": "按关键词搜会话；默认搜标题/首尾消息/目录，--deep 时额外全文搜索对话内容",
        "risk": "read",
        "args": [
            {"flags": ["keywords"], "kwargs": {"nargs": "+", "help": "关键词（多个关键词为 AND 关系）"}},
            {"flags": ["--deep"], "kwargs": {"action": "store_true", "help": "对未命中的会话额外读取完整对话内容再搜一遍（较慢）"}},  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            {"flags": ["--runtime"], "kwargs": {"help": "只搜指定运行时"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 50, "help": "每个运行时最多扫描多少条参与搜索"}},
            {"flags": ["--top"], "kwargs": {"type": int, "help": "最多返回多少条结果；不影响扫描深度"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true", "help": "使用紧凑 JSON，并默认只返回常用字段"}},
            {"flags": ["--live"], "kwargs": {"action": "store_true", "help": "只返回进程仍在运行的会话"}},
            {
                "flags": ["--keepalive"],
                "kwargs": {"action": "store_true", "help": "只返回正挂在 corral 后台保活（tmux）里的会话"},
            },
            {"flags": ["--fields"], "kwargs": {"help": "逗号分隔的字段名，只返回这些字段"}},
        ],
        "fields": {
            "...": "与 list 命令的字段相同（含 live / keepalive / attention / pid / last_user / last_agent）",
            "score": "相关性分数；分数越高排序越靠前，同分按 mtime 倒序",
            "matched_via": "quick（元数据命中）或 deep（全文命中），兼容旧调用方",
            "matched_fields": "命中的字段列表，如 title / first_user_msg / conversation",
            "snippet": "deep 命中时的上下文片段",
            "scanned": "本次各运行时实际读到的会话总数（过滤前）",
            "matched": "关键词命中的会话数；--top 截断前",
            "returned": "本次实际返回的会话数（与 count 一致）",
            "truncated_runtimes": "读满扫描深度的运行时 ID 列表；命中时调大 --limit 再查",
            "failed_runtimes": "本次扫描抛异常的运行时 {id: 失败摘要}；非空时零命中结论不可信",
            "potentially_limited": "覆盖面可能不全时为 true",
        },
    },
    {
        "name": "show",
        "help": "查看单个会话的详情和对话内容",
        "risk": "read",
        "args": [
            {"flags": ["session"], "kwargs": {"help": "会话标识：完整 ID / ID 前缀 / runtime:id"}},
            {"flags": ["--messages"], "kwargs": {"type": int, "help": "只显示最后 N 条消息（默认 20）"}},
            {"flags": ["--full"], "kwargs": {"action": "store_true", "help": "显示完整对话，忽略 --messages"}},
            {"flags": ["--out"], "kwargs": {"help": "把 show 结果写入指定 JSON 文件，stdout 只返回文件引用摘要"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true", "help": "使用紧凑 JSON，并默认只返回常用字段"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 200, "help": "定位会话时的扫描深度"}},
            {"flags": ["--fields"], "kwargs": {"help": "逗号分隔的字段名，只返回这些字段（覆盖 --compact 的默认字段集）"}},  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
        ],
        "fields": {
            "...": "与 list 命令的字段相同",
            "messages": "[{role: user|assistant, text, time, mtime}]，按时间顺序的用户消息和每轮最终答复；time 为人类可读发送时间、mtime 为对应 Unix 时间戳，历史格式解析不出时间时两者均为 null；Monitor/task-notification 等系统注入事件已过滤，不出现在这里",  # noqa: E501 - describe 字段文档，一行一条，拆行破坏输出
            "message_count_shown": "本次实际返回的消息条数",
            "message_count_total": "该会话可提取的消息总数",
            "output_path": "--out 模式下写入的 JSON 文件绝对路径",
        },
    },
    {
        "name": "export",
        "help": "导出某个时间范围内所有会话的完整对话，合并为一个 JSON",
        "risk": "read",
        "args": [
            {"flags": ["--since"], "kwargs": {"help": "起始时间（含）：2026-07-20 / '2026-07-20 15:30' / 7d、24h、30m（距今）/ Unix 时间戳；省略则不设下界"}},  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            {"flags": ["--until"], "kwargs": {"help": "结束时间（含）：格式同 --since；只给日期时按当天 23:59:59 计；省略则不设上界"}},  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            {"flags": ["--runtime"], "kwargs": {"help": "只导出指定运行时（claude / codex / opencode / kimi / cursor）"}},  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            {"flags": ["--status"], "kwargs": {"choices": ["done", "pending", "aborted", "unknown"], "help": "按状态过滤"}},  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            {"flags": ["--cwd"], "kwargs": {"help": "按工作目录子串过滤（大小写不敏感）"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 200, "help": "每个运行时最多扫描多少条历史（扫描深度）"}},  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            {"flags": ["--out"], "kwargs": {"help": "写入指定 JSON 文件；省略则打到 stdout。大范围导出建议用 --out"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true", "help": "使用紧凑 JSON"}},
        ],
        "fields": {
            "range": "本次导出的时间范围；since/until 为 Unix 时间戳，*_display 为人类可读，null 表示该侧无界",
            "count": "命中的会话数",
            "scan_limit": "本次每个运行时的扫描深度（同 --limit）",
            "sessions": "按最后更新时间正序排列的会话数组；每条含 list 的全部字段，外加 messages 完整对话（结构同 show --full）与 message_count_total",  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            "output_path": "--out 模式下写入的 JSON 文件绝对路径；此模式 stdout 只回文件引用摘要，sessions_omitted 为 true",  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            "scanned": "本次各运行时实际读到的会话总数（时间/状态过滤前）",
            "truncated_runtimes": "读满扫描深度的运行时 ID 列表",
            "failed_runtimes": "本次扫描抛异常的运行时 {id: 失败摘要}",
            "potentially_limited": "覆盖面可能不全时为 true",  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
        },
    },
    {
        "name": "share",
        "help": "导出单条会话的统一 transcript（含 thinking 与工具调用），供其他 Agent 做元认知；不改 show/export 的纯文本契约",  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
        "risk": "read",  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
        "args": [
            {"flags": ["session"], "kwargs": {"help": "会话标识：完整 ID / ID 前缀 / runtime:id"}},
            {"flags": ["--out"], "kwargs": {"help": "把完整 transcript 写入指定 JSON 文件，stdout 只返回文件引用摘要"}},
            {"flags": ["--compact"], "kwargs": {"action": "store_true", "help": "使用紧凑 JSON"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 200, "help": "定位会话时的扫描深度"}},
        ],
        "fields": {
            "schema": "固定为 sesskit.transcript/v1（兼容读 corral.share/v1），调用方据此识别事件流版本",
            "...": "与 list 命令的会话元数据字段相同（runtime / id / title / cwd / history_path 等）",
            "runtime_name": "运行时显示名（如 Claude Code）",
            "events": "按原始历史顺序的统一事件数组，type 为 user_message / assistant_message / thinking / tool_call / tool_result；tool_call 含 id/name/kind/input，tool_result 含 call_id/status/output，均不截断",  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            "event_count": "events 条数",
            "counts": "按 type 分组的事件计数",
            "output_path": "--out 模式下写入的 JSON 文件绝对路径",
            "events_omitted": "--out 模式下 stdout 摘要不含 events 正文，为 true",
        },
    },
    {
        "name": "context",
        "help": "生成接续该会话所需的完整上下文数据包（不执行任何操作）",
        "risk": "read",
        "args": [
            {"flags": ["session"], "kwargs": {"help": "会话标识：完整 ID / ID 前缀 / runtime:id"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 200, "help": "定位会话时的扫描深度"}},
        ],
        "fields": {
            "runtime": "运行时标识",
            "runtime_name": "运行时展示名",
            "id": "会话 ID",
            "title": "会话标题",
            "status": "英文状态枚举",
            "live": "进程是否真实运行中；为 true 时优先用 pid 定位现有进程，不要用 resume_command 再起一个新进程",
            "pid": "运行中会话的进程号；非运行中为 null",
            "cwd": "原会话工作目录",
            "cwd_exists": "该目录在当前机器上是否仍然存在",
            "history_path": "历史 JSONL 文件绝对路径",
            "history_reading_hint": "如何解读该运行时历史格式的提示",
            "suggested_prompt": "跨运行时接力时建议使用的首条提示词（人类或 Agent 可直接复用）；内含会话状态和从原会话自动提取的对话摘录，原始历史文件仍是权威来源",  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            "resume_command": "同运行时原生恢复该会话的 shell 命令（可能为 null）",
        },
    },
    {
        "name": "plan continue",
        "help": "生成携带新指令的非交互式原生续接计划（只返回数据，不执行）",
        "risk": "read",
        "args": [
            {"flags": ["session"], "kwargs": {"help": "会话标识：完整 ID / ID 前缀 / runtime:id"}},
            {"flags": ["--instruction"], "kwargs": {"required": True, "help": "续接时发送给原会话的新指令"}},
            {"flags": ["--limit"], "kwargs": {"type": int, "default": 200, "help": "定位会话时的扫描深度"}},
        ],
        "fields": {
            "session_ref": "带运行时的唯一会话标识 runtime:id",
            "runtime": "运行时标识",
            "id": "原会话完整 ID",
            "cwd": "原会话工作目录；可能已不存在",
            "capabilities": "该计划的能力与边界；execution 为 external_only，corral 不会执行计划",
            "launch.argv": "不经 shell 解释的启动参数数组；新指令是其中独立的一项",
            "launch.cwd": "外部执行器启动进程时应使用的工作目录；目录不可用时为 null",
        },
    },
    {
        "name": "describe",
        "help": "查看命令列表或某个命令的完整参数 / 输出字段说明",
        "risk": "read",
        "args": [
            {"flags": ["target"], "kwargs": {"nargs": "*", "help": "命令名；省略则列出全部命令，可使用 plan continue"}},
        ],
        "fields": {},
    },
    {
        "name": "diagnose",
        "help": "只读诊断 TUI/缓存可观测性路径（events.log、embed-error.log、最近闪退、截图目录、tmux、安装路径）；不启动界面",  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
        "risk": "read",  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
        "args": [],
        "fields": {
            "cache_dir": "本地缓存目录（默认 ~/.cache/corral）",
            "events_log": "结构化事件日志路径",
            "events_log_exists": "events.log 是否已存在",
            "embed_error_log": "异常 traceback 日志路径（后台线程 + 致命闪退）",
            "embed_error_log_exists": "embed-error.log 是否已存在",
            "last_error": "embed-error.log 末条解析结果（ts/where/exc_type/exc_msg/traceback；已展开 WorkerFailed 等包装）；无记录时为 null",  # noqa: E501 - COMMANDS 数据表，一行一条参数/字段文档
            "screenshots_dir": "F12 截图输出目录",
            "debug": "当前是否开启 CORRAL_DEBUG",
            "runtime_label_style_claude": "配色自检样例（应为 bold #D97757）",
            "tmux_version": "探测到的 tmux 版本三元组或 null",
            "keepalive_server": (
                "保活 tmux server 调度档：running/pid/priority/interactive_job/clamped；"
                "clamped=true 表示托管助手被系统限流，需重启一次 server"
            ),
            "version": "当前加载的 corral.__version__",
            "package_file": "当前进程 import 的 corral.__file__ 绝对路径",
            "python": "当前解释器 sys.executable",
            "install_channel": "安装渠道：brew / pip / dev",
            "checkout_root": "若 cwd 落在源码树内则为该树根，否则 null",
            "loaded_from_checkout": "package_file 是否来自 checkout_root（开发验收用）",
            "stale_source_warning": "源码树内却加载了别处副本时的告警文案；正常为 null",
            "hints": "给 Agent/维护者的下一步排查提示",
        },
    },
]

HANDLERS = {
    "list": cmd_list,
    "search": cmd_search,
    "show": cmd_show,
    "export": cmd_export,
    "share": cmd_share,
    "context": cmd_context,
    "plan continue": cmd_plan_continue,
    "describe": cmd_describe,
    "diagnose": cmd_diagnose,
}

COMMAND_NAMES = tuple(spec["name"] for spec in COMMANDS)
COMMAND_ROOT_NAMES = tuple(dict.fromkeys(spec["name"].split()[0] for spec in COMMANDS))


def build_parser() -> JSONArgumentParser:
    parser = JSONArgumentParser(
        prog="corral",
        description="corral 的机器可读数据接口：只读，供大模型 Agent 查询本地会话。",
    )
    sub = parser.add_subparsers(dest="command")
    parents = {}
    for spec in COMMANDS:
        parts = spec["name"].split()
        current_sub = sub
        path = []
        for part in parts:
            path.append(part)
            key = tuple(path)
            sp = parents.get(key)
            if sp is None:
                sp = current_sub.add_parser(part, help=spec["help"] if len(path) == len(parts) else None)
                parents[key] = sp
                if len(path) < len(parts):
                    sp.set_defaults(command=" ".join(path))
                    current_sub = sp.add_subparsers(dest=f"command_part_{len(path)}")
                else:
                    sp.set_defaults(command=spec["name"])
            elif len(path) < len(parts):
                current_sub = sp.add_subparsers(dest=f"command_part_{len(path)}")
            else:
                sp.set_defaults(command=spec["name"])
        for arg in spec.get("args", []):
            sp.add_argument(*arg["flags"], **arg["kwargs"])
    return parser


def dispatch(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.error("缺少子命令，请使用 corral describe 查看可用命令")
        return EXIT_USAGE  # pragma: no cover — parser.error 内部已 sys.exit

    if args.command not in HANDLERS:
        parser.error(f"子命令不完整，请使用 corral describe {args.command} 查看用法")
        return EXIT_USAGE  # pragma: no cover — parser.error 内部已 sys.exit

    registry = default_registry()
    handler = HANDLERS[args.command]
    try:
        result = handler(args, registry)
    except ApiError as exc:
        _print_envelope(_err(exc), compact=getattr(args, "compact", False))
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - 机器契约：任何未预料异常都必须是 stdout envelope，不能是裸 traceback
        _print_envelope({
            "ok": False,
            "data": None,
            "error": {
                "code": "internal_error",
                "message": f"命令执行失败：{exc}",
                "hint": "这是 corral 未预料的内部错误；请带上本输出提 issue，可先跑 diagnose 收集环境信息",
                "next_commands": [f"{_bin()} diagnose"],
            },
            "meta": {"version": AGENT_API_VERSION},
        }, compact=getattr(args, "compact", False))
        return EXIT_ERROR

    _print_envelope(result, compact=getattr(args, "compact", False))
    return EXIT_OK


def main() -> None:
    sys.exit(dispatch(sys.argv[1:]))


if __name__ == "__main__":
    main()
