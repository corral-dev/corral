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

import contextlib
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
# Item counts alone are a weak memory budget: one legal frame is 8 MiB. The
# budget counts queued *and* in-flight payload bytes and admits one legal
# maximum frame, matching the outbound lane writer's accounting.
_DEFAULT_QUEUE_BYTES = int(getattr(protocol, "_MAX_FRAME_BYTES", 8 * 1024 * 1024))
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

    每条通道自带一个工作线程：解密与顺序敏感的业务处理都在这里串行完成。

    **为什么必须串行**：加密用的是逐帧递增的计数器，乱序解密会直接判定失败并
    断开通道。同时业务侧会调用 tmux、扫磁盘这类可能阻塞几十毫秒的操作，放在
    网络事件循环里会拖慢所有其他设备的帧，所以也不能就地处理。一条通道一个
    线程，两个要求同时满足。

    **例外**：少数只读慢调用（多 MB 历史冷解析、屏幕抓取）由
    ``RemoteService.dispatch`` 交给连接自己的有界读工作线程，避免一次冷解析把
    后续输入 / ``terminal.attach`` / 标记已读堵在该通道的读线程后面；输入、
    画面订阅和所有写操作仍按到达顺序就地处理。
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
        queue_bytes: int = _DEFAULT_QUEUE_BYTES,
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
        self._queue_bytes_max = max(1, queue_bytes)
        # Payload bytes accepted but not yet processed, including the frame the
        # worker is currently handling. Guarded separately from the channel's
        # encryption lock: producers run on the transport's event loop.
        self._queued_bytes = 0
        self._bytes_lock = threading.Lock()
        # 延迟到首帧再起线程：避免中继灌未知通道号时「建对象即占 OS 线程」
        self._worker: threading.Thread | None = None

    @property
    def ready(self) -> bool:
        return self._secure is not None and self._confirmed

    @property
    def is_data_plane(self) -> bool:
        return self._confirmed and not self._owns_logical_connection

    @property
    def queued_bytes(self) -> int:
        with self._bytes_lock:
            return self._queued_bytes

    def _ensure_worker(self) -> None:
        if self._worker is not None or self._closed:
            return
        worker = threading.Thread(target=self._run, daemon=True, name="remote-channel")
        self._worker = worker
        worker.start()

    def submit(self, frame_type: int, payload: bytes) -> None:
        """由网络侧调用，把一帧交给工作线程。

        准入按「条数 + 字节」双上限，且在途帧同样计入（工作线程处理完才
        归还字节），因此一条慢调用不能让后续帧无限堆在内存里。

        溢出一律只重置该平面：控制面队列满说明手机在灌数据，断开这一条
        （可能连带设备离线）；数据面队列满只关数据面，控制连接保持可用。
        **禁止只丢帧后继续**——加密计数器已经前进，后续帧会因顺序不连续
        被判失败，客户端本来也保不住这条通道；显式关掉让手机带新计数器重绑
        才是诚实的处理。
        """
        if self._closed:
            return
        self._ensure_worker()
        size = len(payload)
        overflow = ""
        with self._bytes_lock:
            if self._queued_bytes + size > self._queue_bytes_max:
                overflow = "bytes"
            else:
                try:
                    self._queue.put_nowait((frame_type, payload))
                except queue.Full:
                    overflow = "frames"
                else:
                    self._queued_bytes += size
        if overflow:
            self._overflow(overflow, size)

    def _overflow(self, reason: str, size: int) -> None:
        plane = "data" if self.is_data_plane else "control"
        observe.event(
            "remote_channel_overflow",
            plane=plane,
            reason=reason,
            frame_bytes=size,
            queued_bytes=self.queued_bytes,
        )
        # Control-plane overflow closes the device connection (today's
        # behaviour). Data-plane overflow closes only that plane: `close()`
        # detaches the data plane and keeps the control connection alive.
        self.close()

    def _release_bytes(self, size: int) -> None:
        with self._bytes_lock:
            self._queued_bytes = max(0, self._queued_bytes - size)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            try:
                self._on_frame(*item)
            finally:
                self._release_bytes(len(item[1]))

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
        # Slow read-only RPCs go to the connection's bounded read workers;
        # everything else keeps this channel's serial order.
        self.service.dispatch(self._connection, message, reply=self._send_message)

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
        with self._lock:
            # Read workers and the reader thread can both reply; check under the
            # same lock that serialises the counter so a close cannot leave a
            # half-encrypted frame or an AttributeError behind.
            secure = self._secure
            if secure is None or self._closed:
                return
            payload = protocol.pack(
                message,
                compress=self._connection is not None and self._connection.compression_enabled,
            )
            frame = secure.encrypt(payload)
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
            # A full queue must not strand the worker thread forever: this
            # channel is closing, so the oldest queued frame is undeliverable
            # anyway (the client rebinds with fresh counters). Drop exactly one
            # to make room for the stop marker, and release its byte budget.
            with contextlib.suppress(queue.Empty):
                dropped = self._queue.get_nowait()
                if dropped is not _STOP:
                    self._release_bytes(len(dropped[1]))
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(_STOP)
        if self._close_transport is not None:
            try:
                self._close_transport()
            except Exception:
                pass
