"""方法路由与订阅管理：把手机发来的消息翻译成对会话中枢的调用。

这一层**与传输方式无关**——中继长连接和局域网直连共用同一个实例，各自把已经
解密好的消息喂进来。这样做的直接好处是：加一种连接方式不需要复制一遍全部业务
逻辑，也不会出现「中继能用、直连不能用」的功能漂移。

订阅的记账放在这里而不是会话中枢里：中枢只知道「有几个人在看这条会话」，
不知道这些人分别连在哪条线上；断线时由这一层负责把该连接的订阅全部退掉，
避免没人看了还在后台抓帧。

安全边界（2026-08-08 审查后）：
- 设备清单以磁盘为准，感知另一进程的 unpair，并主动踢掉已解绑连接
- 只读配对只能调查询/订阅类方法
- 删除 / 结束会话必须带 confirm=true（手机端确认框之后再发）
- 配对失败、输入、新建会话走滑动窗口限流
"""

from __future__ import annotations

import re
import threading
import time

from corral import __version__, observe
from corral.i18n import t
from corral.remote import config as remote_config
from corral.remote import crypto, protocol, ratelimit
from corral.remote.command_receipts import (
    DEFAULT_LEASE_SEC,
    MAX_LEASE_SEC,
    STATUS_DISPATCHING,
    CommandReceiptStore,
    digest_for_image,
    digest_for_keys,
    digest_for_text,
    new_host_run_id,
)
from corral.remote.sessions import (
    MESSAGE_PAGE_LIMIT,
    MESSAGE_PAGE_LIMIT_MAX,
    ActionError,
    PartialInjectionError,
    SessionHub,
    default_title_spawn_fn,
)

_PAIRING_TTL = 10 * 60  # 配对码有效期：够扫码，又不至于长期挂着一个可用凭据

# 只读配对允许的方法（看会话 / 画面 / 搜索；不能输入、不能改布局、不能启停）
_READONLY_METHODS = frozenset(
    {
        protocol.M_HELLO,
        protocol.M_SESSIONS_LIST,
        protocol.M_SESSIONS_WATCH,
        protocol.M_SESSIONS_UNWATCH,
        protocol.M_SESSION_GET,
        protocol.M_SESSION_MESSAGES,
        protocol.M_SESSION_PROMPTS,
        protocol.M_SESSION_WATCH,
        protocol.M_SESSION_UNWATCH,
        protocol.M_SCREEN_WATCH,
        protocol.M_SCREEN_UNWATCH,
        protocol.M_SCREEN_SCROLL,
        protocol.M_PROJECTS_LIST,
        protocol.M_RUNTIMES_LIST,
        protocol.M_SEARCH,
        protocol.M_LAYOUT_WATCH,
        protocol.M_LAYOUT_UNWATCH,
        protocol.M_SEARCH_FULLTEXT,
        # Read-only devices watch the desktop terminal stream without a size vote.
        protocol.M_TERMINAL_ATTACH,
        protocol.M_TERMINAL_RESIZE,
        protocol.M_TERMINAL_RESYNC,
        protocol.M_TERMINAL_DETACH,
        protocol.M_PUSH_REGISTER,
        protocol.M_SESSION_MARK_READ,
        protocol.M_COMMAND_STATUS,
        protocol.M_MEDIA_IMAGE,
    }
)

# Raw terminal input per request: keystrokes and IME commits, never a paste
# (pastes go through input.text so bracketed paste follows the agent's mode).
_TERMINAL_INPUT_MAX = 16 * 1024


def _terminal_viewer(connection: Connection) -> str:
    """Size-vote identity: one per live connection, shared registry with the TUI."""
    return f"remote:{connection.device_id or connection.device_public_key[:16]}:{id(connection):x}"


def _terminal_grid(params: dict) -> tuple[int, int]:
    cols = _int_param(params, "cols", 0, max_value=1000)
    rows = _int_param(params, "rows", 0, max_value=500)
    if cols < 1 or rows < 1:
        raise ActionError(protocol.E_USAGE, t("remote.err.bad_terminal_size"))
    return cols, rows


# 需要二次确认的破坏性操作
_CONFIRM_METHODS = frozenset(
    {
        protocol.M_SESSION_DELETE,
        protocol.M_SESSION_STOP,
    }
)

# tmux 键名白名单：字母数字、修饰前缀（含 C-_ / S-Up / C-S-minus）、具名特殊键。
# 与桌面内嵌「其余一律放行」对齐，避免手机侧合法控制键被误拒。
_TMUX_KEY_RE = re.compile(
    r"^(?:"
    r"[A-Za-z0-9]"
    r"|(?:[CMS]-)+[A-Za-z0-9_\\^[\]@/-]+"
    r"|C-Space|M-Space"
    r"|Enter|Escape|Space|Tab|BSpace|BTab|DC|IC"
    r"|Up|Down|Left|Right|Home|End|PageUp|PageDown|PPage|NPage"
    r"|F(?:[1-9]|1[0-2])"
    r"|KP/\d|KPEnter"
    r")$"
)

_PUSH_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class _DataBind:
    """控制面签发的短时数据面令牌。绑定设备公钥，一次性，超时作废。"""

    __slots__ = ("device_public_key", "connection", "expires_at")

    def __init__(self, device_public_key: str, connection: Connection, expires_at: float) -> None:
        self.device_public_key = device_public_key
        self.connection = connection
        self.expires_at = expires_at


class Connection:
    """一条已完成加密握手的逻辑连接。``send`` 必须是线程安全的。

    旧手机只有控制面 ``send``。新手机附着数据面后，大历史与终端帧走
    ``data_send``；未附着时全部仍走 ``send``，行为与今天完全一致。
    """

    def __init__(self, device_public_key: str, send, *, address: str = "") -> None:
        self.device_public_key = device_public_key
        self.send = send
        self.data_send = None  # 可选：数据面 writer；未附着则为 None
        self.address = address
        self.paired = False
        self.device_name = ""
        self.device_id = ""
        self.access = "full"
        self.channels: set[str] = set()
        self.closed = False
        self.compression_enabled = False
        self.command_receipts = False  # hello 协商 want_command_receipts 后打开
        self.close_hook = None  # 可选：() -> None，用于踢掉控制面底层传输
        self.data_close_hook = None  # 可选：() -> None，只关数据面
        self.data_channel = None  # 数据面 HostChannel 身份，供拆绑时对号入座

    def emit(self, message: dict, *, data: bool = False) -> None:
        """按平面发一帧。数据面未附着或 ``data=False`` 时走控制面。"""
        writer = self.data_send if data and self.data_send is not None else self.send
        writer(message)


class RemoteService:
    def __init__(self, hub: SessionHub | None = None) -> None:
        self.hub = hub or SessionHub(
            on_event=self._dispatch_event,
            title_spawn_fn=default_title_spawn_fn,
        )
        if hub is not None:
            hub._on_event = self._dispatch_event
        self.state = remote_config.load_state()
        # 服务自持「上次加载」mtime：模块全局会被同进程的 remove_device 写脏，
        # 不能用来判断「磁盘是否相对我这份快照变了」。
        self._state_mtime = remote_config.state_mtime()
        self._lock = threading.Lock()
        self._connections: set[Connection] = set()
        self._subscribers: dict[str, set[Connection]] = {}
        self._audit: list[dict] = []  # 最近远程操作，供 status 展示
        self._data_binds: dict[str, _DataBind] = {}
        self.host_run_id = new_host_run_id()
        self.receipts = CommandReceiptStore(self.host_run_id)

    # -- 状态同步 ---------------------------------------------------------

    def refresh_state(self) -> remote_config.RemoteState:
        """从磁盘重载；另一进程的 unpair / 轮换 token 靠这个被看见。"""
        self.state = remote_config.reload_state_if_changed(
            self.state, known_mtime=self._state_mtime
        )
        self._state_mtime = remote_config.state_mtime()
        return self.state

    def _sync_state_mtime(self) -> None:
        """本服务刚写过盘（配对 / touch）后对齐自持 mtime。"""
        self._state_mtime = remote_config.state_mtime()

    def reconcile_devices(self) -> int:
        """踢掉已不在磁盘清单里的连接。返回被踢数量。"""
        self.refresh_state()
        known = {d.public_key for d in self.state.devices}
        kicked = 0
        with self._lock:
            targets = [c for c in self._connections if c.paired and c.device_public_key not in known]
        for connection in targets:
            observe.event("remote_device_revoked", device=connection.device_name or connection.device_id)
            self._kick(connection)
            kicked += 1
        return kicked

    def _kick(self, connection: Connection) -> None:
        connection.paired = False
        connection.closed = True
        hook = connection.close_hook
        self.detach(connection)
        if hook is not None:
            try:
                hook()
            except Exception:
                pass

    def kick_device(self, device_id: str = "", *, public_key: str = "") -> int:
        """按设备 id 或公钥断开在线连接。unpair 后由 daemon 对账即可，此方法供测试。"""
        with self._lock:
            targets = [
                c
                for c in self._connections
                if (device_id and c.device_id == device_id)
                or (public_key and c.device_public_key == public_key)
            ]
        for connection in targets:
            self._kick(connection)
        return len(targets)

    def online_devices(self) -> list[dict]:
        self.refresh_state()
        with self._lock:
            connections = list(self._connections)
        result = []
        for connection in connections:
            if connection.closed or not connection.paired:
                continue
            result.append(
                {
                    "id": connection.device_id,
                    "name": connection.device_name,
                    "access": connection.access,
                    "address": connection.address,
                    "public_key": connection.device_public_key[:16] + "…",
                }
            )
        return result

    def recent_audit(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._audit[-limit:])

    def record_rpc_timing(
        self,
        connection: Connection,
        method: str,
        duration_ms: int,
        *,
        plane: str,
        ok: bool,
        req_id: int = 0,
    ) -> None:
        """Slice0 分段计时：服务端业务耗时进 audit，供 status/快照展示。

        只记低基数事实（方法、耗时、平面、成败），不记会话 key、正文与参数，
        与 observe 脱敏口径一致。失败请求（鉴权/参数/内部错误）不记，
        由 remote_method_failed / remote_response_send_failed 覆盖。
        """
        entry = {
            "ts": time.time(),
            "method": method,
            "device": connection.device_name or connection.device_id or "?",
            "access": connection.access,
            "duration_ms": max(0, int(duration_ms)),
            "plane": plane,
            "ok": bool(ok),
            "req_id": int(req_id),
        }
        with self._lock:
            self._audit.append(entry)
            if len(self._audit) > 100:
                self._audit = self._audit[-100:]

    # NOTE: _audit_event 已删除（audit 只收 record_rpc_timing 的成功耗时条目；
    # 失败请求由 remote_method_failed / remote_response_send_failed 覆盖）。

    # -- 配对 -------------------------------------------------------------

    def begin_pairing(self, ttl: float = _PAIRING_TTL, *, mode: str = "full") -> str:
        code = crypto.new_pairing_code()
        remote_config.write_pairing(code, ttl, mode=mode)
        return code

    def pairing_open(self) -> bool:
        return remote_config.read_pairing() is not None

    def is_known_device(self, device_public_key: str) -> bool:
        self.refresh_state()
        return remote_config.find_device(self.state, device_public_key) is not None

    def device_access(self, device_public_key: str) -> str:
        self.refresh_state()
        device = remote_config.find_device(self.state, device_public_key)
        return device.access if device is not None else "full"

    def accepts(self, device_public_key: str) -> bool:
        """握手阶段的准入判断：已配对设备随时可进，陌生设备只在配对窗口内放行。"""
        return self.is_known_device(device_public_key) or self.pairing_open()

    # -- 连接生命周期 -----------------------------------------------------

    def attach(self, connection: Connection) -> None:
        self.refresh_state()
        device = remote_config.find_device(self.state, connection.device_public_key)
        connection.paired = device is not None
        if device is not None:
            connection.device_name = device.name
            connection.device_id = device.id
            connection.access = device.access
            remote_config.touch_device(self.state, connection.device_public_key)
            self._sync_state_mtime()
        with self._lock:
            # One device key owns one control plane. An older confirmed control
            # connection from the same key is a stale socket the phone already
            # abandoned (reconnect / path change); keeping it would leave the
            # device counted online forever and burn a channel slot.
            superseded = [
                other
                for other in self._connections
                if other is not connection and other.device_public_key == connection.device_public_key
            ]
            self._connections.add(connection)
        for old in superseded:
            observe.event(
                "remote_device_superseded",
                device=old.device_name or old.device_id,
                address=old.address,
            )
            self._supersede(old)
        observe.event(
            "remote_device_attached",
            device=connection.device_name or connection.device_id,
            reason="控制面连接",
        )

    def _supersede(self, connection: Connection) -> None:
        hook = connection.close_hook
        connection.close_hook = None
        self.detach(connection, reason="被同一设备的新控制面取代")
        if hook is not None:
            try:
                hook()
            except Exception:
                pass

    def detach(self, connection: Connection, *, reason: str = "控制面断开") -> None:
        """控制面断开：设备离线，退订，并关掉已附着的数据面。"""
        connection.closed = True
        data_hook = connection.data_close_hook
        connection.data_close_hook = None
        connection.data_send = None
        connection.data_channel = None
        with self._lock:
            was_attached = connection in self._connections
            self._connections.discard(connection)
            stale = [token for token, bind in self._data_binds.items() if bind.connection is connection]
            for token in stale:
                self._data_binds.pop(token, None)
            channels = list(connection.channels)
            for channel in channels:
                subscribers = self._subscribers.get(channel)
                if subscribers is not None:
                    subscribers.discard(connection)
                    if not subscribers:
                        self._subscribers.pop(channel, None)
            connection.channels.clear()
        for channel in channels:
            self._release_channel(channel, connection)
        if data_hook is not None:
            try:
                data_hook()
            except Exception:
                pass
        if was_attached:
            observe.event(
                "remote_device_detached",
                device=connection.device_name or connection.device_id,
                reason=reason,
            )

    def attach_data_plane(self, token: str, device_public_key: str, send, close_hook, channel) -> Connection | None:
        """把第二条物理连接附着到已有逻辑 Connection。校验失败返回 None，不得踢控制面。"""
        now = time.time()
        with self._lock:
            bind = self._data_binds.get(token)
            if bind is None:
                return None
            if bind.expires_at <= now:
                self._data_binds.pop(token, None)
                return None
            if bind.device_public_key != device_public_key:
                return None
            connection = bind.connection
            if connection.closed or connection not in self._connections:
                self._data_binds.pop(token, None)
                return None
            self._data_binds.pop(token, None)
        old_hook = connection.data_close_hook
        old_channel = connection.data_channel
        connection.data_close_hook = None
        if old_hook is not None and old_channel is not channel:
            try:
                old_hook()
            except Exception:
                pass
        connection.data_send = send
        connection.data_close_hook = close_hook
        connection.data_channel = channel
        observe.event(
            "remote_data_plane_attached",
            device=connection.device_name or connection.device_id,
            reason="数据面连接",
        )
        return connection

    def detach_data_plane(self, connection: Connection, channel) -> None:
        """仅数据面断开：控制面保持，允许之后重新签发 data_bind。"""
        if connection.data_channel is not channel:
            return
        connection.data_send = None
        connection.data_close_hook = None
        connection.data_channel = None
        observe.event(
            "remote_data_plane_detached",
            device=connection.device_name or connection.device_id,
            reason="数据面断开",
        )

    def _purge_data_binds(self) -> None:
        now = time.time()
        with self._lock:
            stale = [token for token, bind in self._data_binds.items() if bind.expires_at <= now]
            for token in stale:
                self._data_binds.pop(token, None)

    def _issue_data_bind(self, connection: Connection) -> str:
        self._purge_data_binds()
        token = crypto.random_id(16)
        bind = _DataBind(
            device_public_key=connection.device_public_key,
            connection=connection,
            expires_at=time.time() + protocol.DATA_BIND_TTL_SEC,
        )
        with self._lock:
            self._data_binds[token] = bind
        observe.event("remote_data_bind_issued", device=connection.device_name or connection.device_id)
        return token

    def _subscribe(self, connection: Connection, channel: str) -> bool:
        with self._lock:
            if channel in connection.channels:
                return False
            connection.channels.add(channel)
            self._subscribers.setdefault(channel, set()).add(connection)
        return True

    def _unsubscribe(self, connection: Connection, channel: str) -> bool:
        with self._lock:
            if channel not in connection.channels:
                return False
            connection.channels.discard(channel)
            subscribers = self._subscribers.get(channel)
            if subscribers is not None:
                subscribers.discard(connection)
                if not subscribers:
                    self._subscribers.pop(channel, None)
        return True

    def _release_channel(self, channel: str, connection: Connection) -> None:
        """连接断开时把中枢侧的订阅计数减回去，别让后台白抓帧。"""
        if channel == protocol.CH_SESSIONS:
            self.hub.unwatch_sessions()
        elif channel == protocol.CH_LAYOUT:
            self.hub.unwatch_layout()
        elif channel.startswith("screen:"):
            self.hub.unwatch_screen(channel[len("screen:") :])
        elif channel.startswith("term:"):
            self.hub.terminal_detach(channel[len("term:") :], _terminal_viewer(connection))
        elif channel.startswith("session:"):
            self.hub.unwatch_conversation(channel[len("session:") :])

    def _dispatch_event(self, channel: str, data) -> None:
        # 事件推送前顺手对账一次，避免已解绑设备还在收画面
        self.reconcile_devices()
        with self._lock:
            targets = list(self._subscribers.get(channel, ()))
        if not targets:
            return
        message = protocol.event(channel, data)
        via_data = channel.startswith(("screen:", "term:"))
        for connection in targets:
            if connection.closed:
                continue
            try:
                connection.emit(message, data=via_data)
            except Exception:
                continue

    # -- 消息处理 ---------------------------------------------------------

    def handle(self, connection: Connection, message: dict, *, reply=None) -> None:
        inbound = reply if reply is not None else connection.send
        if message.get("t") != "req":
            return
        req_id = int(message.get("id") or 0)
        method = str(message.get("m") or "")
        params = message.get("p") if isinstance(message.get("p"), dict) else {}
        started = time.perf_counter()
        try:
            data = self._invoke(connection, method, params)
        except ActionError as exc:
            inbound(protocol.error(req_id, exc.code, exc.message))
            return
        except NotImplementedError:
            inbound(protocol.error(req_id, protocol.E_USAGE, t("remote.err.unsupported_method", method=method)))
            return
        except Exception as exc:
            observe.event("remote_method_failed", method=method, error=str(exc))
            inbound(protocol.error(req_id, protocol.E_INTERNAL, t("remote.err.internal")))
            return
        duration_ms = int((time.perf_counter() - started) * 1000)
        writer = self._writer_for_response(connection, method, inbound)
        # plane 按响应实际写出的 socket 记：经 connection.data_send 发出即 data，
        # 其余（控制面原路、未附着数据面、旧客户端）记 control。覆盖三种情形：
        # 控制面大响应改道数据面、数据面进来的请求原路回数据面、控制面短 RPC
        # 原路回控制面。半升级过渡态同样按实际写出归属。
        data_send = connection.data_send
        plane = "data" if (data_send is not None and writer is data_send) else "control"
        self.record_rpc_timing(
            connection, method, duration_ms, plane=plane, ok=True, req_id=req_id
        )
        response = protocol.response(req_id, data)
        negotiate_compression = (
            not connection.compression_enabled and self._requests_compression(message)
        )
        if negotiate_compression:
            # 首个响应仍用裸 JSON 发出；写完后才切换，避免客户端还没收到协商结果就
            # 按压缩格式解码。旧客户端不带 caps，因此始终维持原始载荷。
            response["wire"] = {"version": 1, "compression": "deflate"}
        writer = self._writer_for_response(connection, method, inbound)
        try:
            writer(response)
        except Exception as exc:
            observe.event("remote_response_send_failed", method=method, error=str(exc))
            try:
                connection.send(protocol.error(req_id, protocol.E_INTERNAL, t("remote.err.internal")))
            except Exception:
                return
            return
        if negotiate_compression:
            connection.compression_enabled = True

    @staticmethod
    def _writer_for_response(connection: Connection, method: str, inbound):
        """大历史与终端帧走数据面；其余（含数据面自己的 hello）原路返回。

        未附着数据面时全部走 ``inbound``（旧手机即现有唯一 ``send``）。
        开在「错误平面」上的调用仍处理，只是大包优先改走数据面写出。
        """
        if method in _DATA_PAYLOAD_METHODS and connection.data_send is not None:
            return connection.data_send
        return inbound

    @staticmethod
    def _requests_compression(message: dict) -> bool:
        caps = message.get("caps")
        if not isinstance(caps, dict):
            return False
        compression = caps.get("compression")
        return isinstance(compression, list) and "deflate" in compression

    def _invoke(self, connection: Connection, method: str, params: dict):
        # 每次请求都以磁盘为准：解绑立刻生效
        if connection.paired and not self.is_known_device(connection.device_public_key):
            self._kick(connection)
            raise ActionError(protocol.E_UNAUTHORIZED, t("remote.err.device_unpaired"))
        if connection.paired:
            connection.access = self.device_access(connection.device_public_key)

        if method == protocol.M_PAIR:
            return self._pair(connection, params)
        if method == protocol.M_HELLO:
            return self._hello(connection, params)
        if not connection.paired:
            raise ActionError(protocol.E_UNAUTHORIZED, t("remote.err.device_not_paired"))
        if connection.access == "readonly" and method not in _READONLY_METHODS:
            raise ActionError(protocol.E_UNAUTHORIZED, t("remote.err.readonly"))
        if method in _CONFIRM_METHODS and not bool(params.get("confirm")):
            raise ActionError(
                protocol.E_USAGE,
                t("remote.err.need_confirm"),
            )
        handler = _HANDLERS.get(method)
        if handler is None:
            raise NotImplementedError(method)
        return handler(self, connection, params)

    # -- 具体方法 ---------------------------------------------------------

    def _pair(self, connection: Connection, params: dict):
        key = connection.device_public_key or "anon"
        pairing_mode, reason = remote_config.consume_pairing(str(params.get("code") or ""))
        if reason == "expired":
            self._note_pair_failure(key)
            raise ActionError(protocol.E_UNAUTHORIZED, t("remote.err.pairing_expired"))
        if reason == "wrong":
            self._note_pair_failure(key)
            raise ActionError(protocol.E_UNAUTHORIZED, t("remote.err.pairing_wrong"))
        access = str(params.get("access") or "").strip().lower()
        # 开发机 `pair --readonly` 写入的窗口模式优先，防止手机端自行申请 full
        if pairing_mode == "readonly":
            access = "readonly"
        elif access not in ("full", "readonly"):
            access = "full"
        device = remote_config.PairedDevice(
            id=crypto.random_id(8),
            name=remote_config.sanitize_display_name(str(params.get("name") or "iPhone")),
            public_key=connection.device_public_key,
            paired_at=time.time(),
            last_seen_at=time.time(),
            platform=remote_config.sanitize_display_name(
                str(params.get("platform") or "ios"), max_len=20, fallback="ios"
            ),
            access=access,
        )
        remote_config.add_device(self.state, device)
        self._sync_state_mtime()
        connection.paired = True
        connection.device_name = device.name
        connection.device_id = device.id
        connection.access = device.access
        observe.event("remote_paired", device=device.name, access=device.access)
        return {
            "device_id": device.id,
            "host_name": self.state.host_name,
            "access": device.access,
        }

    @staticmethod
    def _note_pair_failure(key: str) -> None:
        """只对失败的配对尝试计数；成功配对不占配额。"""
        if not ratelimit.PAIR_ATTEMPTS.allow_request(key) or not ratelimit.PAIR_ATTEMPTS_HOURLY.allow_request(
            key
        ):
            raise ActionError(protocol.E_RATE_LIMITED, t("remote.err.pairing_rate_limited"))

    def _hello(self, connection: Connection, params: dict):
        connection.device_name = remote_config.sanitize_display_name(
            str(params.get("name") or ""), fallback=connection.device_name or ""
        )
        self.refresh_state()
        # 未配对也返回中继/局域网开关，便于旧配对手机补上中继地址、不必重新扫码。
        # local_hints 同理：手机配对后 IP 变化也能自愈。旧客户端忽略未知字段。
        from corral.remote.lan import effective_local_port, local_hints

        payload = {
            "protocol": protocol_version(),
            "corral_version": __version__,
            "host_id": self.state.host_id,
            "host_name": self.state.host_name,
            "host_run_id": self.host_run_id,
            "paired": connection.paired,
            "pairing_open": self.pairing_open(),
            "access": connection.access if connection.paired else "",
            "runtimes": self.hub.runtimes() if connection.paired else [],
            "relay_url": self.state.relay_url if self.state.relay_enabled else "",
            "relay_enabled": self.state.relay_enabled,
            "local_enabled": self.state.local_enabled,
            "local_hints": (
                local_hints(effective_local_port(self.state))
                if self.state.local_enabled
                else []
            ),
            "capabilities": {
                "compression": ["deflate"],
                "message_page_limit": MESSAGE_PAGE_LIMIT,
                "message_page_limit_max": MESSAGE_PAGE_LIMIT_MAX,
                "planes": list(protocol.CAPABILITY_PLANES),
                protocol.CAPABILITY_COMMAND_RECEIPTS: True,
                protocol.CAPABILITY_TOOL_DETAIL: True,
                protocol.CAPABILITY_COMPLETION_NOTIFY: True,
                protocol.CAPABILITY_MEDIA_IMAGE: True,
                protocol.CAPABILITY_SESSION_RESTART: True,
                protocol.CAPABILITY_DESKTOP_LAYOUT: True,
                protocol.CAPABILITY_TERMINAL_STREAM: True,
            },
        }
        # 数据面 hello 只做附着确认，不再签发新令牌。
        if str(params.get("plane") or "") == protocol.PLANE_DATA:
            return payload
        # 旧客户端不带该字段，不发 token，行为与今天完全一致。
        if params.get("want_data_plane") is True:
            payload["data_bind"] = self._issue_data_bind(connection)
        # 回执能力：主机已在 capabilities 声明；客户端显式 opt-in 后本连接启用。
        if params.get("want_command_receipts") is True:
            connection.command_receipts = True
        return payload

    def _sessions_list_payload(self, params: dict) -> dict:
        query = str(params.get("q") or "")
        limit = _int_param(params, "limit", 0)
        since_version = str(params.get("since_version") or "")
        snapshot = getattr(self.hub, "list_snapshot", None)
        if callable(snapshot):
            return snapshot(query=query, limit=limit, since_version=since_version)
        return {
            "sessions": self.hub.list_sessions(query=query, limit=limit),
        }

    def _sessions_list(self, connection: Connection, params: dict):
        return self._sessions_list_payload(params)

    def _sessions_watch(self, connection: Connection, params: dict):
        if self._subscribe(connection, protocol.CH_SESSIONS):
            self.hub.watch_sessions()
        return self._sessions_list_payload(params)

    def _sessions_unwatch(self, connection: Connection, params: dict):
        if self._unsubscribe(connection, protocol.CH_SESSIONS):
            self.hub.unwatch_sessions()
        return {"ok": True}

    # -- 桌面布局（Mac 客户端与 TUI 共用分屏组）---------------------------------

    def _layout_watch(self, connection: Connection, params: dict):
        if self._subscribe(connection, protocol.CH_LAYOUT):
            self.hub.watch_layout()
        return self.hub.layout_snapshot()

    def _layout_unwatch(self, connection: Connection, params: dict):
        if self._unsubscribe(connection, protocol.CH_LAYOUT):
            self.hub.unwatch_layout()
        return {"ok": True}

    def _layout_set_group(self, connection: Connection, params: dict):
        keys = params.get("keys")
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise ActionError(protocol.E_USAGE, t("remote.err.layout_group_size"))
        focus = params.get("focus")
        return self.hub.layout_set_group(
            str(params.get("project") or ""), keys, focus if isinstance(focus, str) else None
        )

    def _layout_remove(self, connection: Connection, params: dict):
        return self.hub.layout_remove_session(_key(params))

    def _layout_focus(self, connection: Connection, params: dict):
        return self.hub.layout_set_focus(str(params.get("project") or ""), _key(params))

    def _layout_pin(self, connection: Connection, params: dict):
        return self.hub.layout_toggle_pin(_key(params))

    def _layout_pin_group(self, connection: Connection, params: dict):
        return self.hub.layout_toggle_group_pin(str(params.get("group_id") or ""))

    def _layout_collapse(self, connection: Connection, params: dict):
        return self.hub.layout_set_collapsed(
            str(params.get("group_id") or ""), bool(params.get("collapsed"))
        )

    def _session_get(self, connection: Connection, params: dict):
        return self.hub.session_detail(_key(params))

    def _session_messages(self, connection: Connection, params: dict):
        before_raw = params.get("before_seq")
        before_seq = (
            None
            if before_raw is None or before_raw == ""
            else _int_param(params, "before_seq", 0)
        )
        return self.hub.message_page(
            _key(params),
            limit=_int_param(
                params,
                "limit",
                MESSAGE_PAGE_LIMIT,
                max_value=MESSAGE_PAGE_LIMIT_MAX,
            ),
            before_seq=before_seq,
        )

    def _session_prompts(self, connection: Connection, params: dict):
        return {"prompts": self.hub.prompts(_key(params))}

    def _session_tool_detail(self, connection: Connection, params: dict):
        tool_raw = params.get("tool_id")
        tool_id = None if tool_raw is None or tool_raw == "" else str(tool_raw)
        return self.hub.tool_detail(
            _key(params),
            seq=_int_param(params, "seq", 0),
            tool_id=tool_id,
            offset=_int_param(params, "offset", 0),
            limit=_int_param(params, "limit", 32, max_value=32),
        )

    def _media_image(self, connection: Connection, params: dict):
        return self.hub.image_preview(
            _key(params),
            seq=_int_param(params, "seq", 0, max_value=10_000_000),
            ref=str(params.get("ref") or ""),
            max_px=_int_param(params, "max_px", 0),
            quality=_int_param(params, "quality", 0, max_value=100),
        )

    def _session_watch(self, connection: Connection, params: dict):
        key = _key(params)
        channel = protocol.session_channel(key)
        limit = _int_param(
            params,
            "limit",
            MESSAGE_PAGE_LIMIT,
            max_value=MESSAGE_PAGE_LIMIT_MAX,
        )
        after_seq = _optional_int_param(params, "after_seq")
        generation = _optional_int_param(params, "generation")
        if self._subscribe(connection, channel):
            return self.hub.watch_conversation(
                key,
                limit=limit,
                after_seq=after_seq,
                generation=generation,
            )
        else:
            return self.hub.conversation_page(key, limit=limit)

    def _session_unwatch(self, connection: Connection, params: dict):
        key = _key(params)
        if self._unsubscribe(connection, protocol.session_channel(key)):
            self.hub.unwatch_conversation(key)
        return {"ok": True}

    def _screen_watch(self, connection: Connection, params: dict):
        key = _key(params)
        channel = protocol.screen_channel(key)
        if self._subscribe(connection, channel):
            frame = self.hub.watch_screen(key)
        else:
            frame = self.hub.resync_screen(key)
        return {"frame": frame}

    def _screen_unwatch(self, connection: Connection, params: dict):
        key = _key(params)
        if self._unsubscribe(connection, protocol.screen_channel(key)):
            self.hub.unwatch_screen(key)
        return {"ok": True}

    def _screen_scroll(self, connection: Connection, params: dict):
        return {"frame": self.hub.scroll_screen(_key(params), _int_param(params, "offset", 0))}

    def _terminal_attach(self, connection: Connection, params: dict):
        key = _key(params)
        viewer = _terminal_viewer(connection)
        cols, rows = _terminal_grid(params)
        if not self._subscribe(connection, protocol.terminal_channel(key)):
            # Already attached on this connection: a size update plus a fresh snapshot.
            size = self.hub.terminal_resize(key, viewer, cols, rows, vote=connection.access != "readonly")
            self.hub.terminal_resync(key)
            return size
        try:
            return self.hub.terminal_attach(key, viewer, cols, rows, vote=connection.access != "readonly")
        except Exception:
            self._unsubscribe(connection, protocol.terminal_channel(key))
            raise

    def _terminal_resize(self, connection: Connection, params: dict):
        cols, rows = _terminal_grid(params)
        return self.hub.terminal_resize(
            _key(params), _terminal_viewer(connection), cols, rows, vote=connection.access != "readonly",
        )

    def _terminal_resync(self, connection: Connection, params: dict):
        self.hub.terminal_resync(_key(params))
        return {"ok": True}

    def _terminal_input(self, connection: Connection, params: dict):
        if not ratelimit.TERMINAL_TYPING.allow_request(connection.device_public_key):
            raise ActionError(protocol.E_RATE_LIMITED, t("remote.err.send_rate_limited"))
        import base64
        import binascii

        try:
            data = base64.b64decode(str(params.get("data") or ""), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ActionError(protocol.E_USAGE, t("remote.err.no_content")) from exc
        if len(data) > _TERMINAL_INPUT_MAX:
            raise ActionError(protocol.E_USAGE, t("remote.err.no_content"))
        self.hub.terminal_input(_key(params), data)
        return {"ok": True}

    def _terminal_detach(self, connection: Connection, params: dict):
        key = _key(params)
        if self._unsubscribe(connection, protocol.terminal_channel(key)):
            self.hub.terminal_detach(key, _terminal_viewer(connection))
        return {"ok": True}

    def _input_text(self, connection: Connection, params: dict):
        submit = bool(params.get("submit", True))
        limiter = ratelimit.INPUT_ACTIONS if submit else ratelimit.TERMINAL_TYPING
        if not limiter.allow_request(connection.device_public_key):
            raise ActionError(protocol.E_RATE_LIMITED, t("remote.err.send_rate_limited"))
        text = str(params.get("text") or "")
        if not text and not submit:
            raise ActionError(protocol.E_USAGE, t("remote.err.no_content"))
        key = _key(params)
        if not connection.command_receipts:
            self.hub.send_text(key, text, submit)
            return {"ok": True}
        digest = digest_for_text(key=key, text=text, submit=submit)
        return self._run_receipted_input(
            connection,
            params,
            method=protocol.M_INPUT_TEXT,
            target_key=key,
            digest=digest,
            side_effect=lambda: self.hub.send_text(key, text, submit),
        )

    def _input_keys(self, connection: Connection, params: dict):
        if not ratelimit.TERMINAL_TYPING.allow_request(connection.device_public_key):
            raise ActionError(protocol.E_RATE_LIMITED, t("remote.err.send_rate_limited"))
        keys = params.get("keys")
        if not isinstance(keys, list):
            raise ActionError(protocol.E_USAGE, t("remote.err.no_keys"))
        cleaned = []
        for raw in keys:
            key = str(raw).strip()
            if not key:
                continue
            if not _TMUX_KEY_RE.match(key):
                raise ActionError(
                    protocol.E_USAGE,
                    t("remote.err.unsupported_key").format(key=key[:32]),
                )
            cleaned.append(key)
        if not cleaned:
            raise ActionError(protocol.E_USAGE, t("remote.err.no_keys"))
        session_key = _key(params)
        if not connection.command_receipts:
            self.hub.send_keys(session_key, cleaned)
            return {"ok": True}
        digest = digest_for_keys(key=session_key, keys=cleaned)
        return self._run_receipted_input(
            connection,
            params,
            method=protocol.M_INPUT_KEYS,
            target_key=session_key,
            digest=digest,
            side_effect=lambda: self.hub.send_keys(session_key, cleaned),
        )

    def _input_question(self, connection: Connection, params: dict):
        if not ratelimit.INPUT_ACTIONS.allow_request(connection.device_public_key):
            raise ActionError(protocol.E_RATE_LIMITED, t("remote.err.send_rate_limited"))
        return self.hub.answer_question(
            _key(params),
            str(params.get("request_id") or ""),
            params.get("answers"),
        )

    def _input_image(self, connection: Connection, params: dict):
        if not ratelimit.INPUT_ACTIONS.allow_request(connection.device_public_key):
            raise ActionError(protocol.E_RATE_LIMITED, t("remote.err.send_rate_limited"))
        import base64
        import binascii

        encoded = str(params.get("data") or "").strip()
        if not encoded:
            raise ActionError(protocol.E_USAGE, t("remote.err.no_image"))
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ActionError(protocol.E_USAGE, t("remote.err.image_incomplete")) from exc
        if not raw:
            raise ActionError(protocol.E_USAGE, t("remote.err.no_image"))
        session_key = _key(params)
        if not connection.command_receipts:
            return {"path": self.hub.send_image(session_key, raw)}
        digest = digest_for_image(key=session_key, image_bytes=raw)
        path_box: dict[str, str] = {}

        def _send() -> None:
            path_box["path"] = self.hub.send_image(session_key, raw)

        receipt = self._run_receipted_input(
            connection,
            params,
            method=protocol.M_INPUT_IMAGE,
            target_key=session_key,
            digest=digest,
            side_effect=_send,
        )
        if "path" in path_box:
            receipt = {**receipt, "path": path_box["path"]}
        return receipt

    def _command_status(self, connection: Connection, params: dict):
        command_id = str(params.get("command_id") or "").strip()
        if not command_id:
            raise ActionError(protocol.E_USAGE, t("remote.err.missing_command_id"))
        return self.receipts.status(connection.device_public_key, command_id)

    def _run_receipted_input(
        self,
        connection: Connection,
        params: dict,
        *,
        method: str,
        target_key: str,
        digest: str,
        side_effect,
    ) -> dict:
        command_id = str(params.get("command_id") or "").strip()
        if not command_id:
            raise ActionError(protocol.E_USAGE, t("remote.err.missing_command_id"))
        client_digest = str(params.get("payload_digest") or "").strip()
        if client_digest and client_digest.lower() != digest.lower():
            raise ActionError(protocol.E_USAGE, t("remote.err.digest_mismatch"))
        lease_sec = DEFAULT_LEASE_SEC
        if "lease_sec" in params:
            lease_sec = _int_param(params, "lease_sec", DEFAULT_LEASE_SEC, max_value=MAX_LEASE_SEC)
            if lease_sec < 1:
                lease_sec = 1
        # Receipts record the canonical target: the phone may still hold a
        # retired placeholder key while the host dispatches to the formal one.
        # Retries must never retarget a different session.
        resolve = getattr(self.hub, "resolve_session_key", None)
        canonical_key = resolve(target_key) if callable(resolve) else target_key
        try:
            receipt, is_new = self.receipts.begin(
                device_key=connection.device_public_key,
                command_id=command_id,
                payload_digest=digest,
                target_key=canonical_key,
                method=method,
                lease_sec=float(lease_sec),
            )
        except ValueError as exc:
            raise ActionError(protocol.E_USAGE, str(exc)) from exc
        if not is_new:
            return receipt.to_wire()
        receipt = self.receipts.mark_dispatching(receipt)
        if receipt.status != STATUS_DISPATCHING:
            return receipt.to_wire()
        started_mono = time.perf_counter()
        try:
            side_effect()
        except PartialInjectionError as exc:
            # Paste may have landed; Enter (or a later step) did not — do not claim delivered.
            receipt = self.receipts.mark_unknown(
                receipt, reason="partial_injection", detail=_receipt_detail(exc.message)
            )
            _observe_receipt_outcome(
                connection, receipt, method, target_key,
                duration_ms=_receipt_ms(started_mono),
                detail=_receipt_detail(exc.message),
            )
            return receipt.to_wire()
        except ActionError as exc:
            receipt = self.receipts.mark_rejected(
                receipt,
                reason=exc.code or "action_error",
                retryable=exc.code in (protocol.E_UNAVAILABLE, protocol.E_RATE_LIMITED),
                detail=_receipt_detail(exc.message),
            )
            _observe_receipt_outcome(
                connection, receipt, method, target_key,
                duration_ms=_receipt_ms(started_mono),
                detail=_receipt_detail(exc.message),
            )
            return receipt.to_wire()
        except Exception as exc:
            receipt = self.receipts.mark_unknown(
                receipt, reason="ambiguous", detail=_receipt_detail(t("remote.err.internal"))
            )
            _observe_receipt_outcome(
                connection, receipt, method, target_key,
                duration_ms=_receipt_ms(started_mono),
                detail=_receipt_detail(str(exc)),
            )
            return receipt.to_wire()
        # Only mark delivered when the adapter completed with proven success.
        receipt = self.receipts.mark_delivered(receipt)
        _observe_receipt_outcome(
            connection, receipt, method, target_key,
            duration_ms=_receipt_ms(started_mono), detail="",
        )
        return receipt.to_wire()

    def _screen_resize(self, connection: Connection, params: dict):
        # 手机与桌面共享同一保活窗格：即使误发也不改桌面窗口尺寸。
        raise ActionError(protocol.E_USAGE, t("remote.err.resize_forbidden"))

    def _session_mark_read(self, connection: Connection, params: dict):
        return {"attention": self.hub.mark_read(_key(params))}

    def _session_pin(self, connection: Connection, params: dict):
        return {"pinned": self.hub.toggle_pin(_key(params))}

    def _session_stop(self, connection: Connection, params: dict):
        self.hub.stop_session(_key(params))
        return {"ok": True}

    def _session_delete(self, connection: Connection, params: dict):
        self.hub.delete_session(_key(params))
        return {"ok": True}

    def _session_new(self, connection: Connection, params: dict):
        if not ratelimit.SESSION_CREATE.allow_request(connection.device_public_key):
            raise ActionError(protocol.E_RATE_LIMITED, t("remote.err.new_session_rate_limited"))
        runtime = str(params.get("runtime") or "").strip()
        if not runtime:
            raise ActionError(protocol.E_USAGE, t("remote.err.pick_assistant"))
        cwd = params.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ActionError(protocol.E_USAGE, t("remote.err.bad_project_path"))
        return {"session": self.hub.new_session(runtime, cwd, whitelist=self.state.cwd_whitelist)}

    def _session_resume(self, connection: Connection, params: dict):
        if not ratelimit.SESSION_CREATE.allow_request(connection.device_public_key):
            raise ActionError(protocol.E_RATE_LIMITED, t("remote.err.action_rate_limited"))
        return {"session": self.hub.resume_session(_key(params))}

    def _session_restart(self, connection: Connection, params: dict):
        if not ratelimit.SESSION_CREATE.allow_request(connection.device_public_key):
            raise ActionError(protocol.E_RATE_LIMITED, t("remote.err.action_rate_limited"))
        return {"session": self.hub.restart_session(_key(params))}

    def _session_handoff(self, connection: Connection, params: dict):
        if not ratelimit.SESSION_CREATE.allow_request(connection.device_public_key):
            raise ActionError(protocol.E_RATE_LIMITED, t("remote.err.action_rate_limited"))
        return {"session": self.hub.handoff_session(_key(params), str(params.get("runtime") or ""))}

    def _session_copy(self, connection: Connection, params: dict):
        if not ratelimit.SESSION_CREATE.allow_request(connection.device_public_key):
            raise ActionError(protocol.E_RATE_LIMITED, t("remote.err.action_rate_limited"))
        return {"session": self.hub.copy_session(_key(params))}

    def _projects_list(self, connection: Connection, params: dict):
        return {"projects": self.hub.projects()}

    def _runtimes_list(self, connection: Connection, params: dict):
        return {"runtimes": self.hub.runtimes()}

    def _search(self, connection: Connection, params: dict):
        return {"sessions": self.hub.list_sessions(query=str(params.get("q") or ""), limit=100)}

    def _search_fulltext(self, connection: Connection, params: dict):
        query = str(params.get("q") or "").strip()
        if not query:
            return {"total": 0, "matches": []}
        return self.hub.fulltext_search(query, top=_int_param(params, "top", 40))

    def _push_register(self, connection: Connection, params: dict):
        if not ratelimit.PUSH_REGISTER.allow_request(connection.device_public_key):
            raise ActionError(protocol.E_RATE_LIMITED, t("remote.err.push_rate_limited"))
        token = str(params.get("token") or "").strip()
        if token and not _PUSH_TOKEN_RE.match(token):
            raise ActionError(protocol.E_USAGE, t("remote.err.bad_push_token"))
        env = str(params.get("env") or "").strip().lower()
        if env and env not in ("sandbox", "production"):
            raise ActionError(protocol.E_USAGE, t("remote.err.bad_push_env"))
        # 完成/中断通知偏好：字段出现才覆盖（缺字段=开，老手机不受影响）。
        updates: dict = {"push_token": token, "push_env": env}
        if "notify_completed" in params:
            updates["notify_completed"] = bool(params.get("notify_completed"))
        if "notify_aborted" in params:
            updates["notify_aborted"] = bool(params.get("notify_aborted"))
        device = remote_config.touch_device(
            self.state,
            connection.device_public_key,
            **updates,
        )
        self._sync_state_mtime()
        if device is None:
            raise ActionError(protocol.E_UNAUTHORIZED, t("remote.err.device_not_paired"))
        return {"ok": True}


def _key(params: dict) -> str:
    key = str(params.get("key") or "").strip()
    if not key:
        raise ActionError(protocol.E_USAGE, t("remote.err.missing_session_key"))
    return key


def _receipt_ms(started_mono: float) -> int:
    return max(0, int((time.perf_counter() - started_mono) * 1000))


def _receipt_detail(message: object, limit: int = 300) -> str:
    """回执人类可读文案：取 ActionError message（已本地化），截断防刷屏。

    会话正文、按键、图片字节永远不进这里——调用方只传异常 message。"""
    text = str(message or "").strip()
    if not text:
        return ""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


def _observe_receipt_outcome(
    connection: Connection,
    receipt,
    method: str,
    target_key: str,
    *,
    duration_ms: int,
    detail: str,
) -> None:
    """回执终态落盘后记一条低基数事件：方法、终态、耗时、人类可读失败文案。

    只记 target_key（会话键）不记正文；detail 来自 ActionError 本地化 message，
    不含用户输入。成功时 detail 为空串。"""
    observe.event(
        "remote_input_receipt",
        method=method,
        status=getattr(receipt, "status", "?"),
        reason=getattr(receipt, "reason", None),
        target_key=target_key,
        duration_ms=max(0, int(duration_ms)),
        detail=detail,
        device=connection.device_name or connection.device_id or "?",
    )


def _int_param(params: dict, name: str, default: int, *, max_value: int = 10_000) -> int:
    """把请求参数收成非负整数；坏值按 usage_error，避免 int() 把整条请求打成 500。"""
    raw = params.get(name, default)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ActionError(protocol.E_USAGE, t("remote.err.param_not_int", name=name)) from exc
    if value < 0:
        return 0
    return min(value, max_value)


def _optional_int_param(params: dict, name: str, *, max_value: int = 10_000_000) -> int | None:
    """缺省字段保持 None（旧客户端不传）；在场则收成非负整数。"""
    if name not in params:
        return None
    return _int_param(params, name, 0, max_value=max_value)


def protocol_version() -> int:
    from corral.remote import REMOTE_PROTOCOL_VERSION

    return REMOTE_PROTOCOL_VERSION


# 走数据面的大响应：历史页与终端帧。未附着时仍走控制面。
_DATA_PAYLOAD_METHODS = frozenset(
    {
        protocol.M_SESSION_MESSAGES,
        protocol.M_SESSION_TOOL_DETAIL,
        protocol.M_SESSION_GET,
        protocol.M_SESSION_WATCH,
        protocol.M_SCREEN_WATCH,
        protocol.M_SCREEN_SCROLL,
    }
)


_HANDLERS = {
    protocol.M_SESSIONS_LIST: RemoteService._sessions_list,
    protocol.M_SESSIONS_WATCH: RemoteService._sessions_watch,
    protocol.M_SESSIONS_UNWATCH: RemoteService._sessions_unwatch,
    protocol.M_SESSION_GET: RemoteService._session_get,
    protocol.M_SESSION_MESSAGES: RemoteService._session_messages,
    protocol.M_SESSION_TOOL_DETAIL: RemoteService._session_tool_detail,
    protocol.M_MEDIA_IMAGE: RemoteService._media_image,
    protocol.M_SESSION_PROMPTS: RemoteService._session_prompts,
    protocol.M_SESSION_WATCH: RemoteService._session_watch,
    protocol.M_SESSION_UNWATCH: RemoteService._session_unwatch,
    protocol.M_SESSION_MARK_READ: RemoteService._session_mark_read,
    protocol.M_SCREEN_WATCH: RemoteService._screen_watch,
    protocol.M_SCREEN_UNWATCH: RemoteService._screen_unwatch,
    protocol.M_SCREEN_SCROLL: RemoteService._screen_scroll,
    protocol.M_SCREEN_RESIZE: RemoteService._screen_resize,
    protocol.M_TERMINAL_ATTACH: RemoteService._terminal_attach,
    protocol.M_TERMINAL_RESIZE: RemoteService._terminal_resize,
    protocol.M_TERMINAL_RESYNC: RemoteService._terminal_resync,
    protocol.M_TERMINAL_INPUT: RemoteService._terminal_input,
    protocol.M_TERMINAL_DETACH: RemoteService._terminal_detach,
    protocol.M_INPUT_TEXT: RemoteService._input_text,
    protocol.M_INPUT_KEYS: RemoteService._input_keys,
    protocol.M_INPUT_IMAGE: RemoteService._input_image,
    protocol.M_INPUT_QUESTION: RemoteService._input_question,
    protocol.M_COMMAND_STATUS: RemoteService._command_status,
    protocol.M_SESSION_NEW: RemoteService._session_new,
    protocol.M_SESSION_RESUME: RemoteService._session_resume,
    protocol.M_SESSION_RESTART: RemoteService._session_restart,
    protocol.M_SESSION_HANDOFF: RemoteService._session_handoff,
    protocol.M_SESSION_COPY: RemoteService._session_copy,
    protocol.M_SESSION_STOP: RemoteService._session_stop,
    protocol.M_SESSION_DELETE: RemoteService._session_delete,
    protocol.M_SESSION_PIN: RemoteService._session_pin,
    protocol.M_LAYOUT_WATCH: RemoteService._layout_watch,
    protocol.M_LAYOUT_UNWATCH: RemoteService._layout_unwatch,
    protocol.M_LAYOUT_SET_GROUP: RemoteService._layout_set_group,
    protocol.M_LAYOUT_REMOVE: RemoteService._layout_remove,
    protocol.M_LAYOUT_FOCUS: RemoteService._layout_focus,
    protocol.M_LAYOUT_PIN: RemoteService._layout_pin,
    protocol.M_LAYOUT_PIN_GROUP: RemoteService._layout_pin_group,
    protocol.M_LAYOUT_COLLAPSE: RemoteService._layout_collapse,
    protocol.M_PROJECTS_LIST: RemoteService._projects_list,
    protocol.M_RUNTIMES_LIST: RemoteService._runtimes_list,
    protocol.M_SEARCH: RemoteService._search,
    protocol.M_SEARCH_FULLTEXT: RemoteService._search_fulltext,
    protocol.M_PUSH_REGISTER: RemoteService._push_register,
}
