"""局域网直连：完全跳过中继的逃生口。

给两类人用：同一个 Wi-Fi 下就在旁边的手机，以及已经有 Tailscale / ZeroTier 之类
私有网络、根本不需要第三方中继的用户。协议与走中继时**完全一致**（同一套帧、
同一套端到端加密），区别只是这一条连接是手机主动连进来的。

内容加密照旧：即使有人在同一个 Wi-Fi 上，没有配对过就拿不到会话密钥。

Origin 只接受「无 Origin」（原生客户端）；浏览器跨站 WebSocket 一律拒绝。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress

from corral import observe
from corral.remote import protocol, ratelimit
from corral.remote.config import RemoteState
from corral.remote.crypto import random_id
from corral.remote.lan import DEFAULT_LOCAL_PORT
from corral.remote.service import RemoteService
from corral.remote.transport.channel import UNCONFIRMED_TTL, HostChannel
from corral.remote.transport.relay import _websockets

# Deprecated: 用 `corral.remote.lan.DEFAULT_LOCAL_PORT`；这里只留别名兼容旧 import。
_DEFAULT_PORT = DEFAULT_LOCAL_PORT
_MAX_LOCAL_CHANNELS = 8


class LocalServer:
    """在局域网上监听的直连服务。每条 WebSocket 连接对应一台手机。"""

    def __init__(self, service: RemoteService, state: RemoteState, static_private: bytes) -> None:
        self.service = service
        self.state = state
        self.static_private = static_private
        self.port = 0
        self._server = None
        self._channels = 0
        self._channels_lock = asyncio.Lock()
        self.listen_failed = False

    async def run(self, stop: asyncio.Event) -> None:
        websockets = _websockets()
        port = self.state.local_port or _DEFAULT_PORT
        try:
            # origins=[None]：只接受没有 Origin 头的握手（iOS/原生客户端）；
            # 浏览器跨站一定会带 Origin，会被库直接拒掉。
            self._server = await websockets.serve(
                self._handle,
                "0.0.0.0",
                port,
                max_size=8 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=60,
                origins=[None],
                subprotocols=[protocol.SUBPROTOCOL],
            )
        except OSError as exc:
            self.listen_failed = True
            observe.event("remote_local_listen_failed", port=port, error=str(exc))
            return
        self.port = next(
            (s.getsockname()[1] for s in self._server.sockets or [] if s.getsockname()), port
        )
        observe.event("remote_local_listening", port=self.port)
        try:
            await stop.wait()
        finally:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    async def _handle(self, socket_conn) -> None:
        peer = ""
        with contextlib.suppress(Exception):
            peer = str(socket_conn.remote_address[0])
        if not _is_loopback(peer) and not ratelimit.LOCAL_CHANNEL_OPENS.allow_request(peer or "unknown"):
            observe.event("remote_local_rate_limited", address=peer)
            with contextlib.suppress(Exception):
                await socket_conn.close()
            return
        async with self._channels_lock:
            if self._channels >= _MAX_LOCAL_CHANNELS:
                observe.event("remote_local_channel_limit")
                with contextlib.suppress(Exception):
                    await socket_conn.close()
                return
            self._channels += 1
        loop = asyncio.get_running_loop()
        channel_id = bytes.fromhex(random_id(protocol.CHANNEL_ID_LEN))
        address = ""
        with contextlib.suppress(Exception):
            address = str(socket_conn.remote_address[0])
        closed = asyncio.Event()

        def _close_transport() -> None:
            asyncio.run_coroutine_threadsafe(_close_ws(socket_conn, closed), loop)

        channel = HostChannel(
            self.service,
            self.static_private,
            channel_id,
            lambda frame_type, payload: asyncio.run_coroutine_threadsafe(
                _send(socket_conn, protocol.encode_frame(frame_type, channel_id, payload)), loop
            ),
            address=address,
            close_transport=_close_transport,
        )
        # 每条 WebSocket 一条通道。同一手机的第二条连接（数据面）在 hello 里
        # 用 data_bind 附着到已有逻辑 Connection，不会再登记成另一台设备。
        # 直连时也先发一次通道分配，让手机端的收包逻辑与走中继时完全一致——
        # 客户端不必为两种连接方式各写一套。
        await _send(socket_conn, protocol.encode_frame(protocol.FRAME_DEVICE_OPEN, channel_id, b""))

        async def _expire_unconfirmed() -> None:
            # Phone-side handshake losers can sit here after HELLO without ever
            # sending a decryptable frame; they must not eat a channel slot.
            await asyncio.sleep(UNCONFIRMED_TTL)
            if not channel.ready:
                observe.event("remote_channel_unconfirmed_expired", transport="local", address=address)
                channel.close()

        watchdog = asyncio.create_task(_expire_unconfirmed())
        try:
            async for raw in socket_conn:
                if closed.is_set():
                    break
                if isinstance(raw, str):
                    continue
                try:
                    frame_type, _cid, payload = protocol.decode_frame(raw)
                except protocol.ProtocolError:
                    break
                if frame_type in (protocol.FRAME_HELLO, protocol.FRAME_DATA):
                    channel.submit(frame_type, payload)
        except Exception:
            pass
        finally:
            watchdog.cancel()
            channel.close()
            async with self._channels_lock:
                self._channels = max(0, self._channels - 1)


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


async def _close_ws(socket_conn, closed: asyncio.Event) -> None:
    if closed.is_set():
        return
    closed.set()
    with contextlib.suppress(Exception):
        await socket_conn.close()


async def _send(socket_conn, frame: bytes) -> None:
    try:
        await socket_conn.send(frame)
    except Exception:
        pass


# Deprecated: 真实现已搬到 `corral.remote.lan`；这里只做 re-export，
# 保持 `from corral.remote.transport.local import lan_addresses` 可用。
from corral.remote.lan import lan_addresses  # noqa: E402,F401

__all__ = ["LocalServer", "lan_addresses"]
