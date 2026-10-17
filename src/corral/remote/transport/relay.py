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
import sys
import time
from dataclasses import dataclass

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
_OPEN_TIMEOUT = 10.0
_ACK_TIMEOUT = 10.0
_CLOSE_TIMEOUT = 2.0
_RESUME_CHECK_INTERVAL = 1.0
_RESUME_GAP = 5.0
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


def _suspend_time() -> float:
    """An OS clock including suspend; fall back without guessing from wall time."""
    name = "CLOCK_MONOTONIC_RAW" if sys.platform == "darwin" else "CLOCK_BOOTTIME"
    clock = getattr(time, name, None)
    if clock is not None:
        try:
            return time.clock_gettime(clock)
        except OSError:
            pass
    return time.monotonic()


class _SystemResumed(ConnectionError):
    def __init__(self, gap: float) -> None:
        super().__init__("System resumed; rebuild relay connection")
        self.suspended_ms = round(gap * 1000)


async def _watch_resume() -> None:
    offset = _suspend_time() - time.monotonic()
    while True:
        await asyncio.sleep(_RESUME_CHECK_INTERVAL)
        current = _suspend_time() - time.monotonic()
        if current - offset > _RESUME_GAP:
            raise _SystemResumed(current - offset)
        offset = current


@dataclass
class _Attempt:
    lane: str
    number: int
    started: float
    opened: float | None = None
    ready: float | None = None
    received: float | None = None
    failed: float | None = None

    def fields(self, socket=None, error: Exception | None = None) -> dict:
        now = time.monotonic()
        fields = {
            "lane": self.lane,
            "attempt": self.number,
            "stage": "active" if self.ready is not None else "registering" if self.opened is not None else "opening",
            "duration_ms": round((now - self.started) * 1000),
        }
        if self.opened is not None:
            fields["open_ms"] = round((self.opened - self.started) * 1000)
        if self.ready is not None:
            fields["ready_ms"] = round((self.ready - self.started) * 1000)
        if self.received is not None:
            fields["last_frame_age_ms"] = round((now - self.received) * 1000)
        if self.failed is not None:
            fields["close_wait_ms"] = round((now - self.failed) * 1000)
        latency = getattr(socket, "latency", 0)
        if isinstance(latency, (int, float)) and 0 < latency < float("inf"):
            fields["ping_rtt_ms"] = round(latency * 1000)
        if error is not None:
            fields["error_type"] = type(error).__name__
            if isinstance(error, _SystemResumed):
                fields["failure_kind"] = "system_resume"
                fields["suspended_ms"] = error.suspended_ms
            for name in ("sent", "rcvd"):
                close = getattr(error, name, None)
                if close is not None:
                    fields[f"{name}_code"] = close.code
            if getattr(getattr(error, "sent", None), "reason", "") == "keepalive ping timeout":
                fields["failure_kind"] = "heartbeat_timeout"
        return fields


class RelayClient:
    """一条到中继的长连接，内部可承载多台已配对手机。"""

    def __init__(self, service: RemoteService, state: RemoteState, static_private: bytes) -> None:
        self.service = service
        self.state = state
        self.static_private = static_private
        self._channels: dict[bytes, HostChannel] = {}
        # Socket each channel's encrypted frames are pinned to (see `_write`).
        self._lanes: dict[bytes, object] = {}
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
        self._attempt_numbers = {"interaction": 0, "bulk": 0}

    def _begin_attempt(self, lane: str) -> _Attempt:
        self._attempt_numbers[lane] += 1
        attempt = _Attempt(lane, self._attempt_numbers[lane], time.monotonic())
        observe.event("remote_relay_attempt", **attempt.fields())
        return attempt

    async def _retry(self, stop: asyncio.Event, backoff: float, attempt: _Attempt) -> float:
        delay = min(backoff, _MAX_BACKOFF) * (0.8 + random.random() * 0.4)
        observe.event("remote_relay_retry_scheduled", **attempt.fields(), retry_ms=round(delay * 1000))
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        return min(backoff * 2, _MAX_BACKOFF)

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
            attempt = self._begin_attempt("interaction")
            socket = None
            try:
                async with websockets.connect(
                    self.url,
                    additional_headers=self._headers(),
                    subprotocols=[protocol.SUBPROTOCOL],
                    ping_interval=_PING_INTERVAL,
                    ping_timeout=_PING_TIMEOUT,
                    open_timeout=_OPEN_TIMEOUT,
                    close_timeout=_CLOSE_TIMEOUT,
                    max_size=8 * 1024 * 1024,
                ) as socket:
                    self._socket = socket
                    attempt.opened = time.monotonic()
                    observe.event("remote_relay_connected", url=self.url, **attempt.fields(socket))
                    await self._pump(
                        socket, stop, attempt=attempt, on_registered=lambda: self._maybe_start_bulk(stop)
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                observe.event("remote_relay_disconnected", error=str(exc), **attempt.fields(socket, exc))
            finally:
                self._connected.clear()
                self.connected_at = None
                await self._stop_bulk_task()
                await self._close_bulk()
                self._socket = None
                self._registration_generation = 0
                self._registration_node = ""
                self._host_lane_attach = False
                self._drop_channels()
            if stop.is_set():
                break
            if attempt.ready is not None:
                backoff = _INITIAL_BACKOFF
            backoff = await self._retry(stop, backoff, attempt)

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
        primary = self._socket
        generation = self._registration_generation
        backoff = _INITIAL_BACKOFF
        while not stop.is_set() and self._socket is primary and self._registration_generation == generation:
            attempt = self._begin_attempt("bulk")
            socket = None
            try:
                async with websockets.connect(
                    self.attach_url,
                    additional_headers=self._attach_headers(protocol.LANE_BULK),
                    subprotocols=[protocol.SUBPROTOCOL],
                    ping_interval=_PING_INTERVAL,
                    ping_timeout=_PING_TIMEOUT,
                    open_timeout=_OPEN_TIMEOUT,
                    close_timeout=_CLOSE_TIMEOUT,
                    max_size=8 * 1024 * 1024,
                ) as socket:
                    self._bulk_socket = socket
                    attempt.opened = time.monotonic()
                    observe.event("remote_relay_bulk_attached", generation=generation, **attempt.fields(socket))
                    await self._pump(socket, stop, bulk=True, attempt=attempt)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                observe.event("remote_relay_bulk_disconnected", error=str(exc), **attempt.fields(socket, exc))
            finally:
                await self._close_bulk()
            if stop.is_set() or self._socket is not primary or self._registration_generation != generation:
                break
            if attempt.ready is not None:
                backoff = _INITIAL_BACKOFF
            backoff = await self._retry(stop, backoff, attempt)

    async def _close_bulk(self) -> None:
        self._bulk_attached.clear()
        socket = self._bulk_socket
        self._bulk_socket = None
        if socket is None:
            return
        # A channel's encrypted counter cannot migrate to a replacement socket.
        # Tell its client to rebind immediately, even if no history frame is due.
        for cid, pinned in list(self._lanes.items()):
            if pinned is socket:
                self._close_channel(cid)
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

    async def _pump(
        self, socket, stop: asyncio.Event, *, bulk: bool = False, on_registered=None, attempt: _Attempt
    ) -> None:
        stopper = asyncio.create_task(stop.wait())
        resume = asyncio.create_task(_watch_resume())
        receiver = None
        try:
            while not stop.is_set():
                receiver = asyncio.create_task(socket.recv())
                timeout = None
                if attempt.ready is None:
                    timeout = max(0, attempt.opened + _ACK_TIMEOUT - time.monotonic())
                done, _ = await asyncio.wait(
                    {receiver, stopper, resume}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if stopper in done:
                    return
                if resume in done:
                    resume.result()
                if receiver not in done:
                    raise TimeoutError("Relay protocol acknowledgement timed out")
                raw = receiver.result()
                attempt.received = time.monotonic()
                if isinstance(raw, str):
                    continue  # 中继的文本消息只用于人读的诊断，业务一律走二进制
                self._on_frame(raw, bulk=bulk, on_registered=on_registered)
                expected = protocol.FRAME_LANE_ATTACHED if bulk else protocol.FRAME_REGISTERED
                try:
                    kind = protocol.decode_frame(raw)[0]
                except protocol.ProtocolError:
                    continue
                if attempt.ready is None and kind == expected:
                    attempt.ready = time.monotonic()
                    if not bulk:
                        self._connected.set()
                        self.connected_at = time.time()
                        self.last_error = ""
                        remote_config.clear_host_prev_key()
                    observe.event(
                        "remote_relay_ready", generation=self._registration_generation,
                        node=self._registration_node, **attempt.fields(socket)
                    )
        except Exception as exc:
            attempt.failed = time.monotonic()
            observe.event("remote_relay_receive_failed", **attempt.fields(socket, exc))
            raise
        finally:
            tasks = [task for task in (receiver, stopper, resume) if task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if bulk:
                await self._close_bulk()
            else:
                self._connected.clear()
                self.connected_at = None

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
        if not ratelimit.RELAY_CHANNEL_OPENS.allow_request(self.state.host_id or "host"):
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
        self._lanes.pop(channel_id, None)
        self._write(protocol.FRAME_DEVICE_CLOSE, channel_id, b"")

    def _close_channel(self, channel_id: bytes) -> None:
        channel = self._channels.pop(channel_id, None)
        self._lanes.pop(channel_id, None)
        if channel is not None:
            channel.close()

    def _drop_channels(self) -> None:
        for channel in list(self._channels.values()):
            channel.close()
        self._channels.clear()
        self._lanes.clear()

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
        if frame_type == protocol.FRAME_DATA and channel is not None:
            # Encrypted frames carry a strict counter, and the relay only keeps
            # order within one lane. Pin the channel to the lane of its first
            # frame; if that lane is gone, close the channel so the client
            # rebinds cleanly instead of receiving frames out of order.
            pinned = self._lanes.setdefault(channel_id, socket)
            if pinned is not socket:
                if pinned is not self._socket and pinned is not self._bulk_socket:
                    observe.event("remote_channel_lane_lost")
                    loop.call_soon_threadsafe(self._close_channel, channel_id)
                    return
                socket = pinned
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
