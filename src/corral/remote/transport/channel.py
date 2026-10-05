"""开发机侧的单条设备通道：握手、解密、投递给业务层，再把回包加密送出。

一条 WebSocket 长连接上可能同时跑着多台设备（同一台开发机可以配多部手机），
所以帧里带 16 字节的通道标识，本类对应其中一条。

**密钥确认**：握手阶段对端自称的长期公钥还不足以授权——公钥在局域网明文与中继
上都可见，任何人都能重放 HELLO。必须等对端用派生出的会话密钥发出第一条可解密
帧，才证明它持有对应私钥，此时才 `attach` 并写盘。

控制面与数据面是两条物理连接、两套独立握手（各自的 channel_id 与计数器）。
数据面附着到同一逻辑 Connection；校验失败或数据面溢出只关这一条，不得踢控制面。
"""

from __future__ import annotations

import queue
import threading
import time

from corral import observe
from corral.remote import protocol
from corral.remote.crypto import ChannelError, Handshake
from corral.remote.service import Connection, RemoteService

_STOP = object()
# 首条密文若带毫秒时间戳，允许的时钟偏差（防重放 HELLO+旧 DATA）
_CONFIRM_SKEW_MS = 5 * 60 * 1000
_DEFAULT_QUEUE_SIZE = 256
# A channel that never sends a decryptable frame must not hold a slot forever.
# Handshake losers on the phone (parallel LAN/relay race) can leave the socket
# open after HELLO; the transport closes them once this many seconds pass
# without key confirmation. Normal clients send `hello` right after handshake.
UNCONFIRMED_TTL = 20.0


_MAX_INSTANCE_ID_LEN = 64


def _client_instance(message: dict) -> str:
    """Per-process client instance id carried on every request (`ci`)."""
    value = message.get("ci")
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if 0 < len(value) <= _MAX_INSTANCE_ID_LEN else ""


class HostChannel:
    """一条设备通道。``writer`` 收 (帧类型, 载荷) 并负责实际发送，必须线程安全。

    每条通道自带一个工作线程：解密与业务处理都在这里串行完成。

    **为什么必须串行**：加密用的是逐帧递增的计数器，乱序解密会直接判定失败并
    断开通道。同时业务侧会调用 tmux、扫磁盘这类可能阻塞几十毫秒的操作，放在
    网络事件循环里会拖慢所有其他设备的帧，所以也不能就地处理。一条通道一个
    线程，两个要求同时满足。
    """

    def __init__(
        self,
        service: RemoteService,
        static_private: bytes,
        channel_id: bytes,
        writer,
        *,
        address: str = "",
        close_transport=None,
        queue_size: int = _DEFAULT_QUEUE_SIZE,
    ) -> None:
        self.service = service
        self.channel_id = channel_id
        self._writer = writer
        self._address = address
        self._close_transport = close_transport
        self._handshake = Handshake(static_private)
        self._secure = None
        self._connection: Connection | None = None
        self._pending_device_key: str = ""
        self._confirmed = False
        # True = 本通道拥有逻辑 Connection（控制面）。数据面为 False。
        self._owns_logical_connection = True
        self._closed = False
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, queue_size))
        # 延迟到首帧再起线程：避免中继灌未知通道号时「建对象即占 OS 线程」
        self._worker: threading.Thread | None = None

    @property
    def ready(self) -> bool:
        return self._secure is not None and self._confirmed

    @property
    def is_data_plane(self) -> bool:
        return self._confirmed and not self._owns_logical_connection

    def _ensure_worker(self) -> None:
        if self._worker is not None or self._closed:
            return
        worker = threading.Thread(target=self._run, daemon=True, name="remote-channel")
        self._worker = worker
        worker.start()

    def submit(self, frame_type: int, payload: bytes) -> None:
        """由网络侧调用，把一帧交给工作线程。

        控制面队列满说明手机在灌数据，断开这一条（可能连带设备离线）。
        数据面队列满只丢弃这一帧，禁止因此 close() 控制面。
        """
        if self._closed:
            return
        self._ensure_worker()
        try:
            self._queue.put_nowait((frame_type, payload))
        except queue.Full:
            observe.event(
                "remote_channel_overflow",
                plane="data" if self.is_data_plane else "control",
            )
            if self.is_data_plane:
                return
            self.close()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            self._on_frame(*item)

    def _on_frame(self, frame_type: int, payload: bytes) -> None:
        """处理一帧。任何异常都只关闭这一条通道，不影响同一连接上的其他设备。"""
        try:
            if frame_type == protocol.FRAME_HELLO:
                self._on_hello(payload)
            elif frame_type == protocol.FRAME_DATA:
                self._on_data(payload)
        except ChannelError as exc:
            observe.event("remote_channel_error", reason=str(exc))
            self.close()
        except Exception as exc:
            observe.event("remote_channel_failed", error=str(exc))
            self.close()

    def _on_hello(self, payload: bytes) -> None:
        if self._secure is not None:
            raise ChannelError("重复握手")
        if len(payload) != 64:
            raise ChannelError("握手材料长度不合法")
        device_static, device_eph = payload[:32], payload[32:]
        device_key_hex = device_static.hex()
        if not self.service.accepts(device_key_hex):
            # 陌生设备且当前没有开配对窗口：直接断开，不给任何可以用来试探的反馈。
            observe.event("remote_channel_rejected")
            self.close()
            return
        secure = self._handshake.accept(device_static, device_eph)
        self._writer(protocol.FRAME_HELLO, self._handshake.ephemeral_public)
        self._secure = secure
        self._pending_device_key = device_key_hex
        # 故意不 attach：等第一条可解密密文完成密钥确认

    def _on_data(self, payload: bytes) -> None:
        if self._secure is None:
            raise ChannelError("尚未握手")
        with self._lock:
            plaintext = self._secure.decrypt(payload)
        if self._connection is not None and self._connection.compression_enabled:
            message = protocol.unpack(plaintext)
        else:
            message = protocol.loads(plaintext)
        if not self._confirmed:
            self._confirm_and_attach(message)
        if self._connection is None:
            raise ChannelError("通道未确认")
        self.service.handle(self._connection, message, reply=self._send_message)

    def _confirm_and_attach(self, message: dict) -> None:
        """首条可解密帧 = 密钥确认。可选校验毫秒时间戳防重放。"""
        ts = message.get("ts")
        if ts is None and isinstance(message.get("p"), dict):
            ts = message["p"].get("ts")
        if ts is not None:
            try:
                ts_ms = int(float(ts))
                # 兼容秒级时间戳
                if ts_ms < 10_000_000_000:
                    ts_ms *= 1000
                now_ms = int(time.time() * 1000)
                if abs(now_ms - ts_ms) > _CONFIRM_SKEW_MS:
                    raise ChannelError("握手确认时间戳超出允许范围")
            except (TypeError, ValueError) as exc:
                raise ChannelError("握手确认时间戳不合法") from exc
        params = message.get("p") if isinstance(message.get("p"), dict) else {}
        if str(params.get("plane") or "") == protocol.PLANE_DATA:
            connection = self.service.attach_data_plane(
                str(params.get("bind") or ""),
                self._pending_device_key,
                self._send_message,
                self.close,
                self,
            )
            if connection is None:
                observe.event("remote_data_plane_rejected", address=self._address)
                # 只关数据通道，不得踢已配对的控制连接。
                self._owns_logical_connection = False
                raise ChannelError("数据面绑定失败")
            self._owns_logical_connection = False
            self._connection = connection
            self._confirmed = True
            observe.event("remote_data_plane_confirmed", address=self._address)
            return
        connection = Connection(
            self._pending_device_key,
            self._send_message,
            address=self._address,
        )
        connection.instance_id = _client_instance(message)
        connection.close_hook = self.close
        self._owns_logical_connection = True
        self._connection = connection
        self._confirmed = True
        self.service.attach(connection)
        observe.event("remote_channel_confirmed", address=self._address)

    def _send_message(self, message: dict) -> None:
        if self._secure is None or self._closed:
            return
        with self._lock:
            payload = protocol.pack(
                message,
                compress=self._connection is not None and self._connection.compression_enabled,
            )
            frame = self._secure.encrypt(payload)
            # The counter was assigned just now, so the hand-off must stay inside
            # the lock: a reply and a pushed event racing on two threads would
            # otherwise reach the socket in the wrong order and the client drops
            # the channel (`加密帧顺序异常`). Writers only schedule, never block.
            self._writer(protocol.FRAME_DATA, frame)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        connection = self._connection
        self._connection = None
        self._secure = None
        self._confirmed = False
        if self._owns_logical_connection:
            if connection is not None:
                # detach 会顺带关掉数据面；先摘掉 close_hook 避免递归回本通道。
                if connection.close_hook is self.close:
                    connection.close_hook = None
                self.service.detach(connection)
        elif connection is not None:
            self.service.detach_data_plane(connection, self)
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass
        if self._close_transport is not None:
            try:
                self._close_transport()
            except Exception:
                pass
