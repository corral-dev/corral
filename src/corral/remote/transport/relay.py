"""向公网中继建立出站长连接。

开发机不需要开任何端口、不需要公网 IP、不需要 VPN——它自己连出去，手机也连到
同一个中继，中继按配对关系把两边接起来。中继看得到的只有「哪台开发机和哪部手机
在通信、每帧多大」，看不到任何会话内容（内容在这条连接之上再做了一层端到端加密）。

断线重连用指数退避，上限一分钟：开发机可能在合盖、换网、路由重启之间反复掉线，
死磕重连既没意义又会给中继带来无谓压力。

开发机侧自设通道上限与建通道速率——不把记账交给中继，中继被攻破或被替换时
也不能靠灌 CHANNEL_OPEN 把本机线程打满。

当中继在 REGISTERED 里宣告 ``host_lane_attach`` 时，再附着一条 bulk 出站 socket，
把数据面帧与交互面帧拆到两条物理连接上；旧中继无该能力则保持单连接。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import time

from corral import __version__, observe
from corral.remote import config as remote_config
from corral.remote import protocol, ratelimit
from corral.remote.config import RemoteState
from corral.remote.service import RemoteService
from corral.remote.transport.channel import UNCONFIRMED_TTL, HostChannel

_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0
_PING_INTERVAL = 20.0
_PING_TIMEOUT = 60.0  # 解析大历史会占 GIL；20s 超时会把手机正在等的回包连同连接一起掐掉
# 正常用户个位数手机；略大于中继侧 16，给瞬断重连留余量，但仍是硬顶
_MAX_CHANNELS = 8

_MISSING_WS_HINT = (
    "手机端接力需要额外的网络组件。请执行：pip install 'corral[remote]'\n"
    "（用 pipx 安装的话：pipx inject corral cryptography websockets）"
)


class RelayUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__(_MISSING_WS_HINT)


def _websockets():
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise RelayUnavailable() from exc
    return websockets


class RelayClient:
    """一条到中继的长连接，内部可承载多台已配对手机。"""

    def __init__(self, service: RemoteService, state: RemoteState, static_private: bytes) -> None:
        self.service = service
        self.state = state
        self.static_private = static_private
        self._channels: dict[bytes, HostChannel] = {}
        self._socket = None
        self._bulk_socket = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = asyncio.Event()
        self.connected_at: float | None = None
        self.last_error: str = ""
        self._registration_generation: int = 0
        self._registration_node: str = ""
        self._host_lane_attach: bool = False
        self._bulk_attached = asyncio.Event()
        self._bulk_task: asyncio.Task | None = None
        self._receipt_handler = None  # 由推送层注册：(push_id, receipt_dict) -> None

    @property
    def url(self) -> str:
        base = self.state.relay_url.rstrip("/")
        return f"{base}/v2/host"

    @property
    def attach_url(self) -> str:
        base = self.state.relay_url.rstrip("/")
        return f"{base}/v2/host/attach"

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def bulk_lane_attached(self) -> bool:
        return self._bulk_attached.is_set() and self._bulk_socket is not None

    async def run(self, stop: asyncio.Event) -> None:
        websockets = _websockets()
        backoff = _INITIAL_BACKOFF
        self._loop = asyncio.get_running_loop()
        while not stop.is_set():
            try:
                async with websockets.connect(
                    self.url,
                    additional_headers=self._headers(),
                    subprotocols=[protocol.SUBPROTOCOL],
                    ping_interval=_PING_INTERVAL,
                    ping_timeout=_PING_TIMEOUT,
                    max_size=8 * 1024 * 1024,
                ) as socket:
                    self._socket = socket
                    self._connected.set()
                    self.connected_at = time.time()
                    self.last_error = ""
                    backoff = _INITIAL_BACKOFF
                    observe.event("remote_relay_connected", url=self.url)
                    remote_config.clear_host_prev_key()
                    await self._pump(socket, stop, on_registered=lambda: self._maybe_start_bulk(stop))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                observe.event("remote_relay_disconnected", error=str(exc))
            finally:
                await self._stop_bulk_task()
                await self._close_bulk()
                self._socket = None
                self._connected.clear()
                self.connected_at = None
                self._registration_generation = 0
                self._registration_node = ""
                self._host_lane_attach = False
                self._drop_channels()
            if stop.is_set():
                break
            # 抖动避免大量开发机在中继重启后同一秒回来
            delay = min(backoff, _MAX_BACKOFF) * (0.8 + random.random() * 0.4)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            backoff = min(backoff * 2, _MAX_BACKOFF)

    def _maybe_start_bulk(self, stop: asyncio.Event) -> None:
        if not self._host_lane_attach or self._registration_generation <= 0:
            return
        if self._bulk_task is not None and not self._bulk_task.done():
            return
        self._bulk_task = asyncio.create_task(self._run_bulk_lane(stop), name="relay-bulk-lane")

    async def _stop_bulk_task(self) -> None:
        task = self._bulk_task
        self._bulk_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _run_bulk_lane(self, stop: asyncio.Event) -> None:
        if self.bulk_lane_attached or not self._host_lane_attach:
            return
        websockets = _websockets()
        try:
            async with websockets.connect(
                self.attach_url,
                additional_headers=self._attach_headers(protocol.LANE_BULK),
                subprotocols=[protocol.SUBPROTOCOL],
                ping_interval=_PING_INTERVAL,
                ping_timeout=_PING_TIMEOUT,
                max_size=8 * 1024 * 1024,
            ) as socket:
                self._bulk_socket = socket
                observe.event(
                    "remote_relay_bulk_attached",
                    generation=self._registration_generation,
                    node=self._registration_node,
                )
                await self._pump(socket, stop, bulk=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            observe.event("remote_relay_bulk_disconnected", error=str(exc))
        finally:
            await self._close_bulk()

    async def _close_bulk(self) -> None:
        self._bulk_attached.clear()
        socket = self._bulk_socket
        self._bulk_socket = None
        if socket is None:
            return
        with contextlib.suppress(Exception):
            await socket.close()

    def _headers(self) -> dict:
        from corral.remote import crypto as remote_crypto

        host_key = remote_config.load_or_create_host_key()
        nonce = os.urandom(16)
        ts = int(time.time())
        assertion = remote_crypto.sign_host_assertion(host_key, self.state.host_id, ts, nonce)
        pub = remote_crypto.host_public_key_bytes(host_key)
        headers = {
            "X-Corral-Host-Name": self.state.host_name,
            "X-Corral-Host-Pub": pub.hex(),
            "X-Corral-Version": __version__,
            "X-Corral-Auth": assertion,
            "X-Corral-Bundle-Id": "com.x0c.corral",
        }
        prev = remote_config.load_host_prev_key()
        if prev is not None:
            headers["X-Corral-Prev-Auth"] = remote_crypto.sign_host_assertion(
                prev, self.state.host_id, ts, nonce
            )
        return headers

    def _attach_headers(self, lane: str) -> dict:
        from corral.remote import crypto as remote_crypto

        host_key = remote_config.load_or_create_host_key()
        nonce = os.urandom(16)
        ts = int(time.time())
        pub = remote_crypto.host_public_key_bytes(host_key)
        attach = remote_crypto.sign_host_lane_attach(
            host_key,
            self.state.host_id,
            self._registration_node,
            self._registration_generation,
            lane,
            ts,
            nonce,
        )
        return {
            "X-Corral-Host-Pub": pub.hex(),
            "X-Corral-Version": __version__,
            "X-Corral-Lane-Attach": attach,
        }

    async def _pump(self, socket, stop: asyncio.Event, *, bulk: bool = False, on_registered=None) -> None:
        stopper = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                receiver = asyncio.create_task(socket.recv())
                done, _ = await asyncio.wait({receiver, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if receiver not in done:
                    receiver.cancel()
                    return
                raw = receiver.result()
                if isinstance(raw, str):
                    continue  # 中继的文本消息只用于人读的诊断，业务一律走二进制
                self._on_frame(raw, bulk=bulk, on_registered=on_registered)
        finally:
            stopper.cancel()

    def set_receipt_handler(self, handler) -> None:
        """注册推送回执回调（PushNotifier.set_sender 在识别到本客户端时自动调用，
        无需改 daemon 组装层）。"""
        self._receipt_handler = handler

    def _on_push_receipt(self, payload: bytes) -> None:
        handler = self._receipt_handler
        if handler is None:
            return
        try:
            receipt = protocol.parse_push_receipt(payload)
        except protocol.ProtocolError:
            return
        try:
            handler(receipt.get("id") or "", receipt)
        except Exception:
            pass

    def _on_frame(self, raw: bytes, *, bulk: bool = False, on_registered=None) -> None:
        try:
            frame_type, channel_id, payload = protocol.decode_frame(raw)
        except protocol.ProtocolError:
            return
        if frame_type == protocol.FRAME_PUSH_RECEIPT:
            # 回执走 interaction 面；bulk 面即使收到也按同样语义处理，不致命。
            self._on_push_receipt(payload)
            return
        if frame_type == protocol.FRAME_REGISTERED and not bulk:
            self._on_registered(payload, on_registered)
            return
        if frame_type == protocol.FRAME_LANE_ATTACHED and bulk:
            self._bulk_attached.set()
            return
        if frame_type == protocol.FRAME_DEVICE_OPEN:
            self._open_channel(channel_id)
        elif frame_type == protocol.FRAME_DEVICE_CLOSE:
            self._close_channel(channel_id)
        elif frame_type in (protocol.FRAME_HELLO, protocol.FRAME_DATA):
            channel = self._channels.get(channel_id) or self._open_channel(channel_id)
            if channel is None:
                return
            channel.submit(frame_type, payload)

    def _on_registered(self, payload: bytes, on_registered) -> None:
        generation = 0
        node = ""
        capabilities: list[str] = []
        if payload:
            try:
                body = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                body = {}
            if isinstance(body, dict):
                try:
                    generation = int(body.get("generation") or 0)
                except (TypeError, ValueError):
                    generation = 0
                node = str(body.get("node") or "")
                caps = body.get("capabilities") or []
                if isinstance(caps, list):
                    capabilities = [str(item) for item in caps]
        self._registration_generation = generation
        self._registration_node = node
        self._host_lane_attach = protocol.CAPABILITY_HOST_LANE_ATTACH in capabilities
        observe.event(
            "remote_relay_registered",
            generation=generation,
            node=node,
            host_lane_attach=self._host_lane_attach,
        )
        if on_registered is not None and self._host_lane_attach:
            on_registered()

    def _open_channel(self, channel_id: bytes) -> HostChannel | None:
        existing = self._channels.get(channel_id)
        if existing is not None:
            return existing
        if len(self._channels) >= _MAX_CHANNELS:
            observe.event("remote_channel_limit", count=len(self._channels))
            return None
        if not ratelimit.CHANNEL_OPENS.allow_request(self.state.host_id or "host"):
            observe.event("remote_channel_rate_limited")
            return None
        channel = HostChannel(
            self.service,
            self.static_private,
            channel_id,
            lambda frame_type, payload, cid=channel_id: self._write(frame_type, cid, payload),
            address="relay",
            # Host-initiated close (kick / supersede / expiry) must free the slot
            # here and tell the relay to drop the device socket; otherwise the
            # relay keeps the phone attached to a channel the host has forgotten.
            close_transport=lambda cid=channel_id: self._release_channel(cid),
        )
        # 中继上每个 DEVICE_OPEN 是一条独立通道（独立握手与计数器）。
        # 数据面第二条 WebSocket 会再开一个 channel_id，由 HostChannel 在 hello
        # 里 bind 到同一逻辑 Connection，而不是当成第二台设备。
        self._channels[channel_id] = channel
        loop = self._loop
        if loop is not None:
            loop.call_later(UNCONFIRMED_TTL, self._expire_unconfirmed, channel_id)
        return channel

    def _expire_unconfirmed(self, channel_id: bytes) -> None:
        """Drop a channel that completed HELLO but never proved it holds the key."""
        channel = self._channels.get(channel_id)
        if channel is None or channel.ready:
            return
        observe.event("remote_channel_unconfirmed_expired", transport="relay")
        self._close_channel(channel_id)

    def _release_channel(self, channel_id: bytes) -> None:
        """Forget the channel and ask the relay to detach that device socket."""
        self._channels.pop(channel_id, None)
        self._write(protocol.FRAME_DEVICE_CLOSE, channel_id, b"")

    def _close_channel(self, channel_id: bytes) -> None:
        channel = self._channels.pop(channel_id, None)
        if channel is not None:
            channel.close()

    def _drop_channels(self) -> None:
        for channel in list(self._channels.values()):
            channel.close()
        self._channels.clear()

    def _write(self, frame_type: int, channel_id: bytes, payload: bytes) -> None:
        """由通道工作线程调用，必须把发送动作转投回事件循环。"""
        loop = self._loop
        if loop is None:
            return
        channel = self._channels.get(channel_id)
        use_bulk = bool(channel is not None and channel.is_data_plane and self.bulk_lane_attached)
        socket = self._bulk_socket if use_bulk else self._socket
        if socket is None:
            socket = self._socket
        if socket is None:
            return
        frame = protocol.encode_frame(frame_type, channel_id, payload)
        asyncio.run_coroutine_threadsafe(_safe_send(socket, frame), loop)

    def send_push(self, token: str, env: str, payload: bytes, push_id: str = "") -> None:
        """请中继代发一条推送。载荷已经是加密壳，中继读不懂里面写了什么。

        ``push_id`` 为本帧关联标识（可空，旧中继忽略未知字段）：新中继会回
        ``FRAME_PUSH_RECEIPT``，推送层只凭回执记已发；仅入队永不算成功。
        """
        if not token:
            return
        body = json.dumps(
            {
                "token": token,
                "env": env or "production",
                "payload": payload.decode("ascii"),
                "id": push_id or "",
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self._write(protocol.FRAME_PUSH, protocol.ZERO_CHANNEL, body)


async def _safe_send(socket, frame: bytes) -> None:
    try:
        await socket.send(frame)
    except Exception:
        pass  # 连接刚好断了：重连后手机会重新订阅，这一帧丢掉无害
