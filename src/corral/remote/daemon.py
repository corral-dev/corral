"""常驻服务的组装与主循环。

把会话中枢、方法路由、两种连接方式和推送串起来，跑一个 asyncio 事件循环。
业务侧仍然是线程模型（沿用 corral 已有的 `SessionStore`），事件循环只负责网络。
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time

from corral import observe
from corral.i18n import t
from corral.remote import config as remote_config
from corral.remote.lan import DEFAULT_LOCAL_PORT, local_hints
from corral.remote.mdns import MdnsAdvertiser
from corral.remote.push import PushNotifier
from corral.remote.service import RemoteService
from corral.remote.sessions import SessionHub, default_title_spawn_fn
from corral.remote.transport.local import LocalServer
from corral.remote.transport.relay import RelayClient

_RECONCILE_INTERVAL = 2.0


class RemoteDaemon:
    def __init__(self, state: remote_config.RemoteState) -> None:
        self.state = state
        self.static_private = remote_config.load_or_create_identity()
        self.hub = SessionHub(title_spawn_fn=default_title_spawn_fn)
        self.service = RemoteService(self.hub)
        self.relay = RelayClient(self.service, state, self.static_private) if state.relay_enabled else None
        self.local = LocalServer(self.service, state, self.static_private) if state.local_enabled else None
        self.mdns = (
            MdnsAdvertiser(
                state.host_id,
                state.host_name,
                lambda: self.local.port if self.local is not None else 0,
            )
            if state.local_enabled
            else None
        )
        self.push = PushNotifier(state, self.static_private)
        if self.relay is not None:
            self.push.set_sender(self.relay.send_push)
        self.hub.set_attention_hook(self.push.on_attention_change)
        self.hub.set_status_hook(self.push.on_status_change)

    async def run(self) -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, stop.set)

        # 开关语义：`on` 要立刻看到 pid。先落盘再扫盘，首轮扫描再久也不挡住开。
        remote_config.write_pid()
        observe.event("remote_started", host=self.state.host_name)

        # The first scan can take tens of seconds on a loaded machine. Open the
        # LAN listener and the relay first so clients connect at once; requests
        # that need session data wait for the scan (`SessionHub.wait_ready`).
        self.hub.mark_starting()
        tasks = []
        if self.relay is not None:
            tasks.append(asyncio.create_task(self.relay.run(stop)))
        if self.local is not None:
            tasks.append(asyncio.create_task(self.local.run(stop)))
        if self.mdns is not None:
            # zeroconf 注册是阻塞式（短暂等待冲突探测），放线程里别堵事件循环。
            # 端口此时可能还是 0：MdnsAdvertiser 内部会等 LocalServer listen 完。
            tasks.append(asyncio.create_task(asyncio.to_thread(self.mdns.start)))
        if not tasks:
            raise RuntimeError(t("remote.err.no_entry"))
        await asyncio.to_thread(self.hub.start)
        tasks.append(asyncio.create_task(self._reconcile_loop(stop)))
        try:
            await stop.wait()
            # 局域网若宣称开启却监听失败，给用户一个看得见的事件（进程不退出，中继仍可用）
            if self.local is not None and self.local.listen_failed:
                observe.event("remote_local_unavailable", port=self.state.local_port)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.mdns is not None:
                await asyncio.to_thread(self.mdns.stop)
            await asyncio.to_thread(self.hub.stop)
            remote_config.clear_pid()
            remote_config.clear_status_snapshot()
            observe.event("remote_stopped")

    async def _reconcile_loop(self, stop: asyncio.Event) -> None:
        """定期把磁盘上的设备清单对到在线连接：unpair 后最多两秒内踢掉。"""
        while not stop.is_set():
            try:
                await asyncio.to_thread(self._reconcile_once)
            except Exception:
                pass
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_RECONCILE_INTERVAL)

    def _reconcile_once(self) -> None:
        self.service.reconcile_devices()
        # 给另一进程的 `corral remote status` 看：谁在线、最近干了啥、中继是否真连上
        relay_online = False
        relay_connected_at = None
        relay_error = ""
        if self.relay is not None:
            relay_online = self.relay.is_connected
            relay_connected_at = self.relay.connected_at
            relay_error = self.relay.last_error
        port = self.local.port if self.local is not None else 0
        if not port:
            port = DEFAULT_LOCAL_PORT if self.state.local_enabled else 0
        remote_config.write_status_snapshot(
            {
                "updated_at": time.time(),
                "online": self.service.online_devices(),
                "recent": self.service.recent_audit(12),
                "relay_online": relay_online,
                "relay_connected_at": relay_connected_at,
                "relay_error": relay_error,
                "local_port": port,
                "local_hints": local_hints(port) if self.state.local_enabled and port else [],
                "mdns": bool(self.mdns is not None and self.mdns.advertising),
            }
        )

    @property
    def local_port(self) -> int:
        return self.local.port if self.local is not None else 0
