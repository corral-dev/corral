"""手机端与开发机之间的应用层协议。

分两层：

**中继层（明文，中继看得见）** —— 只有路由所需的最小信息：谁发给谁、多大。
每个 WebSocket 二进制帧的结构是 ``[1 字节版本=2][1 字节类型][16 字节会话通道 ID][载荷]``。
中继据此把帧转给对端，不解析载荷。子协议为 ``corral.v2``。

**应用层（密文，只有两端看得见）** —— 载荷解密后是一段 UTF-8 JSON，形如：

    {"t": "req", "id": 7, "m": "session.get", "p": {...}}
    {"t": "res", "id": 7, "ok": true, "d": {...}}
    {"t": "res", "id": 7, "ok": false, "e": {"code": "not_found", "message": "..."}}
    {"t": "evt", "c": "screen:claude:abc", "d": {...}}

请求-响应用 ``id`` 关联；订阅推送用 ``c``（channel）标识，客户端按需退订。
``id`` 由客户端分配，服务端原样回传，不做全局唯一性要求。
"""

from __future__ import annotations

import json
import zlib
from typing import Any

# --- 中继层帧类型 ---------------------------------------------------------

FRAME_DATA = 0x10          # 承载加密载荷
FRAME_DEVICE_OPEN = 0x01   # 中继 → 主机：某设备接入
FRAME_DEVICE_CLOSE = 0x02  # 中继 → 主机：某设备断开
FRAME_HELLO = 0x03         # 握手材料（明文，见 crypto 模块的说明）
FRAME_PING = 0x04
FRAME_PONG = 0x05
FRAME_PUSH = 0x06          # 主机 → 中继：请中继代发一条推送（内容已加密）
FRAME_REGISTERED = 0x07    # 中继 → 主机：注册成功，可以开始接客
FRAME_LANE_ATTACHED = 0x08 # 中继 → 主机：附加 bulk 通道附着成功
FRAME_PUSH_RECEIPT = 0x09  # 中继 → 主机：推送路由回执（只代表 APNs 已接受，不代表手机已展示）

ZERO_CHANNEL = b"\x00" * 16

CHANNEL_ID_LEN = 16
FRAME_VERSION = 2
SUBPROTOCOL = "corral.v2"
CAPABILITY_HOST_LANE_ATTACH = "host_lane_attach"
LANE_INTERACTION = "interaction"
LANE_BULK = "bulk"
_MAX_FRAME_BYTES = 8 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 4 * 1024 * 1024
_PAYLOAD_MAGIC = b"CR"
_PAYLOAD_VERSION = 1
_PAYLOAD_HEADER_BYTES = 8
_CODEC_NONE = 0
_CODEC_DEFLATE = 1
_COMPRESSION_THRESHOLD = 1024


class ProtocolError(RuntimeError):
    pass


def encode_frame(frame_type: int, channel_id: bytes, payload: bytes) -> bytes:
    if len(channel_id) != CHANNEL_ID_LEN:
        raise ProtocolError("通道标识长度不合法")
    return bytes([FRAME_VERSION, frame_type]) + channel_id + payload


def decode_frame(raw: bytes) -> tuple[int, bytes, bytes]:
    if len(raw) < 2 + CHANNEL_ID_LEN:
        raise ProtocolError("帧过短")
    if len(raw) > _MAX_FRAME_BYTES:
        raise ProtocolError("帧过大")
    if raw[0] != FRAME_VERSION:
        raise ProtocolError("帧版本不是 v2")
    return raw[1], raw[2 : 2 + CHANNEL_ID_LEN], raw[2 + CHANNEL_ID_LEN :]


# --- 应用层消息 -----------------------------------------------------------

def dumps(message: dict) -> bytes:
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > _MAX_UNCOMPRESSED_BYTES:
        raise ProtocolError("消息未压缩体积过大")
    return raw


def loads(raw: bytes) -> dict:
    if len(raw) > _MAX_UNCOMPRESSED_BYTES:
        raise ProtocolError("消息未压缩体积过大")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("消息不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("消息顶层必须是对象")
    return value


def pack(message: dict, *, compress: bool = False) -> bytes:
    """把应用消息封装成可在加密前压缩的载荷。

    未协商压缩时继续返回旧版裸 JSON，保证旧手机客户端可以继续连接；
    协商成功后才加上 ``CR`` 载荷头。``deflate`` 编码使用 raw DEFLATE
    （RFC 1951），与 Apple Compression ``COMPRESSION_ZLIB`` 的实际输出互通。
    """
    raw = dumps(message)
    if not compress or len(raw) < _COMPRESSION_THRESHOLD:
        return raw
    compressor = zlib.compressobj(level=5, wbits=-15)
    encoded = compressor.compress(raw) + compressor.flush()
    if len(encoded) + _PAYLOAD_HEADER_BYTES >= len(raw):
        return raw
    header = (
        _PAYLOAD_MAGIC
        + bytes([_PAYLOAD_VERSION, _CODEC_DEFLATE])
        + len(raw).to_bytes(4, "big")
    )
    return header + encoded


def _inflate(body: bytes) -> bytes:
    """解开 raw DEFLATE；也接受 zlib 包装，避免半升级连接解不开。"""
    errors: list[Exception] = []
    for wbits in (15, -15):
        try:
            decompressor = zlib.decompressobj(wbits=wbits)
            decoded = decompressor.decompress(body, _MAX_UNCOMPRESSED_BYTES + 1)
            if len(decoded) > _MAX_UNCOMPRESSED_BYTES:
                raise ProtocolError("解压后消息过大")
            if not decompressor.eof:
                raise ProtocolError("压缩载荷解压失败")
            if decompressor.unused_data:
                raise ProtocolError("压缩载荷包含尾随数据")
            decoded += decompressor.flush()
            return decoded
        except zlib.error as exc:
            errors.append(exc)
            continue
    raise ProtocolError("压缩载荷解压失败") from (errors[-1] if errors else None)


def unpack(raw: bytes) -> dict:
    """解开新载荷；也接受未协商客户端发送的裸 JSON。"""
    if not raw.startswith(_PAYLOAD_MAGIC):
        return loads(raw)
    if len(raw) < _PAYLOAD_HEADER_BYTES:
        raise ProtocolError("压缩载荷头不完整")
    version = raw[2]
    codec = raw[3]
    expected = int.from_bytes(raw[4:8], "big")
    if version != _PAYLOAD_VERSION:
        raise ProtocolError("压缩载荷版本不支持")
    if expected > _MAX_UNCOMPRESSED_BYTES:
        raise ProtocolError("解压后消息过大")
    body = raw[_PAYLOAD_HEADER_BYTES:]
    if codec == _CODEC_NONE:
        decoded = body
    elif codec == _CODEC_DEFLATE:
        decoded = _inflate(body)
    else:
        raise ProtocolError("压缩算法不支持")
    if len(decoded) != expected:
        raise ProtocolError("解压后长度不匹配")
    return loads(decoded)


def request(req_id: int, method: str, params: dict | None = None) -> dict:
    return {"t": "req", "id": req_id, "m": method, "p": params or {}}


def response(req_id: int, data: Any) -> dict:
    return {"t": "res", "id": req_id, "ok": True, "d": data}


def error(req_id: int, code: str, message: str, hint: str = "") -> dict:
    payload: dict = {"code": code, "message": message}
    if hint:
        payload["hint"] = hint
    return {"t": "res", "id": req_id, "ok": False, "e": payload}


def event(channel: str, data: Any) -> dict:
    return {"t": "evt", "c": channel, "d": data}


# --- 方法名 ---------------------------------------------------------------
#
# 命名规则：``名词.动词``。带副作用的方法一律放在 `input.` / `session.` 下，
# 只读查询放在 `sessions.` / `projects.` / `search` 下，便于日后按前缀做权限分级。

M_HELLO = "hello"                    # 客户端自报家门 + 取服务端能力

# 控制/数据双连接（两条物理 WebSocket；帧头仍是 v2，中继只转发密文）。
# 旧客户端忽略 capabilities 里的未知字段，也不带 want_data_plane，行为与单连接时一致。
PLANE_CONTROL = "control"
PLANE_DATA = "data"
CAPABILITY_PLANES = [PLANE_CONTROL, PLANE_DATA]
DATA_BIND_TTL_SEC = 120              # data_bind 最长存活秒数；一次性且绑定设备公钥
# Durable input receipts (Slice A). Host advertises; client opts in with want_command_receipts.
CAPABILITY_COMMAND_RECEIPTS = "command_receipts"
# History wire omits tool bodies; client fetches via session.toolDetail when needed.
CAPABILITY_TOOL_DETAIL = "tool_detail"
# Host supports per-device completion/abort notification prefs via push.register.
CAPABILITY_COMPLETION_NOTIFY = "completion_notify"
# Message-referenced images are previewed through media.image (downscaled on the host).
CAPABILITY_MEDIA_IMAGE = "media_image"
# Host supports session.restart (remote access to the desktop advanced-restart).
# Advertised only with the implementation; old hosts lack the method entirely.
CAPABILITY_SESSION_RESTART = "session_restart"
# Desktop clients (the Mac app) share the TUI's split groups and pins through
# the host layout store: layout.watch / layout.* below. Phone clients never
# call these and keep their group-free list payload.
CAPABILITY_DESKTOP_LAYOUT = "desktop_layout"
# Desktop clients with their own emulator: raw pane output on ``term:<key>``,
# sizing through the widest-viewer registry (see remote/terminal_stream.py).
CAPABILITY_TERMINAL_STREAM = "terminal_stream"
# Every human prompt of a session (session.userPrompts), independent of the
# history window a client has paged in. Old hosts lack the method entirely.
CAPABILITY_USER_PROMPTS = "user_prompts"
M_PAIR = "pair"                      # 用一次性配对码完成配对
M_PUSH_REGISTER = "push.register"    # 上报推送令牌
M_COMMAND_STATUS = "command.status"  # 只读：按 command_id 查回执（含 unseen）

M_SESSIONS_LIST = "sessions.list"
M_SESSIONS_WATCH = "sessions.watch"
M_SESSIONS_UNWATCH = "sessions.unwatch"
M_SESSION_GET = "session.get"
M_SESSION_MESSAGES = "session.messages"
M_SESSION_TOOL_DETAIL = "session.toolDetail"
M_SESSION_PROMPTS = "session.prompts"
# Read-only: {key} → every human prompt of the session, oldest first (Your prompts).
M_SESSION_USER_PROMPTS = "session.userPrompts"
M_SESSION_WATCH = "session.watch"
M_SESSION_UNWATCH = "session.unwatch"
M_SESSION_MARK_READ = "session.markRead"
# Read-only: {key, seq, ref, max_px?, quality?} → bounded preview of an image that message cites.
M_MEDIA_IMAGE = "media.image"

M_SCREEN_WATCH = "screen.watch"
M_SCREEN_UNWATCH = "screen.unwatch"
M_SCREEN_SCROLL = "screen.scroll"
M_SCREEN_RESIZE = "screen.resize"

M_INPUT_TEXT = "input.text"
M_INPUT_KEYS = "input.keys"
M_INPUT_IMAGE = "input.image"
# Answer the pending native agent question: {key, request_id, answers:[{question_id, selected:[option_id], text}]}
# → {status: delivered|stale|unavailable, detail?}. Never falls back to input.text.
M_INPUT_QUESTION = "input.question"

M_SESSION_NEW = "session.new"
M_SESSION_RESUME = "session.resume"
# 桌面高级操作「重启会话」的远程入口：params 只有 {key}（无需二次确认，
# 菜单选择即确认），返回 {"session": SessionSummary}，与 resume 同形。
# 已结束会话退回原生恢复；故意不进 _READONLY_METHODS（只读配对服务端拒掉）。
M_SESSION_RESTART = "session.restart"
M_SESSION_HANDOFF = "session.handoff"
# 同助手复制会话：params 只有 {key}，返回 {"session": SessionSummary}，与 handoff/resume 同形。
# 官方分叉优先，否则磁盘克隆；故意不进 service._READONLY_METHODS（只读配对服务端拒掉）。
M_SESSION_COPY = "session.copy"
M_SESSION_STOP = "session.stop"
M_SESSION_DELETE = "session.delete"
M_SESSION_PIN = "session.pin"

# Desktop layout (split groups shared with every TUI window of this host).
# layout.watch → snapshot + `layout` channel events on every revision change.
# Mutations return the new snapshot and are rejected for read-only pairings.
M_LAYOUT_WATCH = "layout.watch"
M_LAYOUT_UNWATCH = "layout.unwatch"
M_LAYOUT_SET_GROUP = "layout.setGroup"        # {project, keys:[2..4], focus?}
M_LAYOUT_REMOVE = "layout.removeSession"      # {key}: pane closed → leave its group
M_LAYOUT_FOCUS = "layout.setFocus"            # {project, key}
M_LAYOUT_PIN = "layout.pin"                   # {key}: TUI semantics (group member pins the group)
M_LAYOUT_PIN_GROUP = "layout.pinGroup"        # {group_id}
M_LAYOUT_COLLAPSE = "layout.collapse"         # {group_id, collapsed}
M_LAYOUT_RENAME_GROUP = "layout.renameGroup"  # {group_id, name}: empty name restores the automatic name

M_TERMINAL_ATTACH = "terminal.attach"   # {key, cols, rows} → {cols, rows}; snapshot follows as event
M_TERMINAL_RESIZE = "terminal.resize"   # {key, cols, rows} → {cols, rows}
M_TERMINAL_RESYNC = "terminal.resync"   # {key}: a fresh snapshot event (after a seq gap)
M_TERMINAL_INPUT = "terminal.input"     # {key, data: base64}: raw bytes to the pane
M_TERMINAL_DETACH = "terminal.detach"   # {key}
# {key, background, foreground?} (#rrggbb): the viewer's terminal colours, answered to the
# agent's OSC 10/11 queries; attach accepts the same two fields.
M_TERMINAL_THEME = "terminal.theme"

M_PROJECTS_LIST = "projects.list"
M_SEARCH = "search"
# Full-text conversation search (the TUI's Ctrl+F index) for desktop clients:
# {q, top?} → {total, matches:[{key, title, total_hits, lines:[{role, text, spans, ts}]}]}.
M_SEARCH_FULLTEXT = "search.fulltext"
M_RUNTIMES_LIST = "runtimes.list"

# 订阅通道名前缀
CH_SESSIONS = "sessions"
CH_LAYOUT = "layout"


def screen_channel(session_key: str) -> str:
    return f"screen:{session_key}"


def terminal_channel(session_key: str) -> str:
    return f"term:{session_key}"


def session_channel(session_key: str) -> str:
    return f"session:{session_key}"


# --- 错误码 ---------------------------------------------------------------

# 推送回执码（FRAME_PUSH_RECEIPT 载荷的 code 稳定词汇；reason 透传 Apple 原文）。
PUSH_RECEIPT_CODES = frozenset({
    "ok",
    "no_push_config",
    "bad_request",
    "bad_token",
    "rejected",
    "throttled",
    "transport",
    "internal",
})


def push_receipt(
    push_id: str,
    ok: bool,
    code: str,
    *,
    status: int = 0,
    reason: str = "",
    apns_id: str = "",
) -> dict:
    """构造推送回执的应用层表示（仅中继侧与测试使用；线上传输为帧载荷 JSON）。"""
    return {
        "id": push_id or "",
        "ok": bool(ok),
        "code": code if code in PUSH_RECEIPT_CODES else "internal",
        "status": int(status or 0),
        "reason": str(reason or ""),
        "apns_id": str(apns_id or ""),
    }


def parse_push_receipt(raw: bytes) -> dict:
    """解析回执帧载荷；非法时抛 ProtocolError，调用方按丢弃处理。"""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("回执不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("回执顶层必须是对象")
    return push_receipt(
        str(value.get("id") or ""),
        bool(value.get("ok")),
        str(value.get("code") or "internal"),
        status=value.get("status") or 0,
        reason=value.get("reason") or "",
        apns_id=value.get("apns_id") or "",
    )
#
# 与 agent_api 的错误码保持同一套词汇，手机端和 Agent 侧的处理逻辑可以复用。

E_USAGE = "usage_error"
E_NOT_FOUND = "not_found"
E_UNAUTHORIZED = "unauthorized"
E_UNAVAILABLE = "unavailable"
E_INTERNAL = "internal_error"
E_RATE_LIMITED = "rate_limited"
