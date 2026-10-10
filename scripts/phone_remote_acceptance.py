#!/usr/bin/env python3
"""按手机 App 真实请求验收远程列表与详情。

旧探测脚本默认只拉 5 条会话摘要，且不订阅、不打开详情。手机实际会：
1. 并发抢出一条加密通道（这里只走中继，与出门/蜂窝同一条路）
2. 20 秒内必须拿到 ``sessions.watch`` 的整表首包
3. 再对点开的那条发 ``session.watch``，同样 20 秒超时
4. 同一部手机的第二条控制面确认后，旧控制面须被开发机关闭（一台设备一条控制面）

本脚本把上述路径拆成具名用例。可叠加往返延迟与带宽上限，用来暴露
「本机回包很快、经中继加上限速就超时」的问题。失败以非零退出。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import time
from dataclasses import dataclass, field

_CLI_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, _CLI_SRC)

from corral.remote import crypto, protocol  # noqa: E402

_IDENTITY_FILE = os.path.expanduser("~/.cache/corral/remote/device-probe.key")
PHONE_RPC_TIMEOUT = 20.0


def _identity() -> bytes:
    try:
        data = open(_IDENTITY_FILE, "rb").read()
        if len(data) == 32:
            return data
    except OSError:
        pass
    key = crypto.generate_private_key_bytes()
    os.makedirs(os.path.dirname(_IDENTITY_FILE), exist_ok=True)
    with open(_IDENTITY_FILE, "wb") as handle:
        handle.write(key)
    os.chmod(_IDENTITY_FILE, 0o600)
    return key


def _pick_runtime_samples(sessions: list[dict]) -> list[dict]:
    """每个助手各留一条：有真人最后一句的优先，其次体积大的。"""
    picked: dict[str, dict] = {}
    for item in sessions:
        runtime = str(item.get("runtime") or "")
        if not runtime:
            continue
        current = picked.get(runtime)
        size = float(item.get("size_kb") or 0)
        has_user = bool(str(item.get("last_user") or "").strip())
        if current is None:
            picked[runtime] = item
            continue
        cur_size = float(current.get("size_kb") or 0)
        cur_user = bool(str(current.get("last_user") or "").strip())
        if has_user and not cur_user:
            picked[runtime] = item
        elif has_user == cur_user:
            # 不要专挑最大历史：手机 20s 超时，中等体积更能代表日常点开。
            def _score(kb: float) -> float:
                if 80 <= kb <= 2500:
                    return abs(kb - 400)
                if kb < 80:
                    return 5000 + (80 - kb)
                return 8000 + kb / 100

            if _score(size) < _score(cur_size):
                picked[runtime] = item
    return list(picked.values()) or list(sessions[:1])


def _decode_key(value: str) -> bytes:
    import base64

    try:
        return bytes.fromhex(value)
    except ValueError:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded)


@dataclass
class CaseResult:
    name: str
    ok: bool
    detail: str
    seconds: float = 0.0


@dataclass
class Report:
    cases: list[CaseResult] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, seconds: float = 0.0) -> None:
        self.cases.append(CaseResult(name, ok, detail, seconds))
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}  {seconds:.3f}s  {detail}")

    @property
    def failed(self) -> bool:
        return any(not item.ok for item in self.cases)


class PhoneLikeClient:
    """模仿手机：20 秒 RPC 超时、可注入延迟/限速、可同时开第二条连接。"""

    def __init__(
        self,
        socket,
        secure,
        channel_id: bytes,
        *,
        extra_rtt: float,
        bytes_per_sec: float,
        rpc_timeout: float,
    ) -> None:
        self.socket = socket
        self.secure = secure
        self.channel_id = channel_id
        self.extra_rtt = extra_rtt
        self.bytes_per_sec = bytes_per_sec
        self.rpc_timeout = rpc_timeout
        self.pending: dict[int, asyncio.Future] = {}
        self.next_id = 0
        self.events: list[dict] = []
        self.closed = False
        self.bytes_in = 0
        self.bytes_out = 0

    async def _pace(self, nbytes: int) -> None:
        if self.extra_rtt:
            await asyncio.sleep(self.extra_rtt)
        if self.bytes_per_sec > 0 and nbytes > 0:
            await asyncio.sleep(nbytes / self.bytes_per_sec)

    async def send_frame(self, kind: int, payload: bytes) -> None:
        frame = protocol.encode_frame(kind, self.channel_id, payload)
        self.bytes_out += len(frame)
        await self._pace(len(frame))
        await self.socket.send(frame)

    async def call(self, method: str, params: dict | None = None) -> tuple[float, dict]:
        self.next_id += 1
        request_id = self.next_id
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending[request_id] = future
        payload = self.secure.encrypt(protocol.dumps(protocol.request(request_id, method, params)))
        started = time.perf_counter()
        await self.send_frame(protocol.FRAME_DATA, payload)
        try:
            message = await asyncio.wait_for(future, timeout=self.rpc_timeout)
        except TimeoutError as exc:
            self.pending.pop(request_id, None)
            raise TimeoutError(f"{method} 超过手机 {self.rpc_timeout:.0f}s 超时") from exc
        elapsed = time.perf_counter() - started
        if message.get("e"):
            err = message["e"]
            raise RuntimeError(f"{method} 失败：{err.get('code')} {err.get('message')}")
        return elapsed, message.get("d") or {}

    async def reader(self) -> None:
        try:
            async for raw in self.socket:
                self.bytes_in += len(raw) if isinstance(raw, (bytes, bytearray)) else 0
                kind, _cid, payload = protocol.decode_frame(raw)
                if kind != protocol.FRAME_DATA:
                    continue
                await self._pace(len(payload))
                message = protocol.loads(self.secure.decrypt(payload))
                if message.get("t") == "res":
                    future = self.pending.pop(int(message.get("id") or 0), None)
                    if future and not future.done():
                        future.set_result(message)
                elif message.get("t") == "evt":
                    self.events.append(message)
        except Exception:
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("通道在等待回包时断开"))
            self.pending.clear()


async def _open_client(args, host_key: bytes) -> PhoneLikeClient:
    import websockets

    handshake = crypto.Handshake(_identity())
    host = args.host or crypto.routing_id_from_x25519(host_key)
    url = f"{args.relay.rstrip('/')}/v2/device?host={host}"
    socket = await websockets.connect(
        url,
        max_size=8 * 1024 * 1024,
        subprotocols=[protocol.SUBPROTOCOL],
        ping_interval=20,
        ping_timeout=20,
    )
    kind, channel_id, _ = protocol.decode_frame(await socket.recv())
    if kind != protocol.FRAME_DEVICE_OPEN:
        raise RuntimeError(f"中继未分配通道，收到 {kind:#x}")
    await socket.send(
        protocol.encode_frame(
            protocol.FRAME_HELLO,
            channel_id,
            handshake.static_public + handshake.ephemeral_public,
        )
    )
    kind, _cid, payload = protocol.decode_frame(await socket.recv())
    if kind != protocol.FRAME_HELLO:
        raise RuntimeError(f"握手失败，收到 {kind:#x}")
    secure = handshake.complete(host_key, payload)
    client = PhoneLikeClient(
        socket,
        secure,
        channel_id,
        extra_rtt=args.rtt_ms / 2000.0,
        bytes_per_sec=args.bytes_per_sec,
        rpc_timeout=args.timeout,
    )
    asyncio.create_task(client.reader())
    # 密钥确认：第一条可解密请求。
    confirm = protocol.request(0, "hello", {"name": args.name, "ts": int(time.time() * 1000)})
    confirm["id"] = 0
    await client.send_frame(protocol.FRAME_DATA, secure.encrypt(protocol.dumps(confirm)))
    return client


async def run(args) -> int:
    report = Report()
    host_key = _decode_key(args.key)
    print(
        f"中继 {args.relay}  超时 {args.timeout:.0f}s  "
        f"附加往返 {args.rtt_ms}ms  带宽 {args.bytes_per_sec or '不限'}B/s"
    )

    connect_started = time.perf_counter()
    try:
        client = await asyncio.wait_for(_open_client(args, host_key), timeout=args.timeout)
    except Exception as exc:
        report.add("连接中继并完成握手", False, str(exc), time.perf_counter() - connect_started)
        return 1
    report.add(
        "连接中继并完成握手",
        True,
        f"通道 {client.channel_id.hex()[:8]}…",
        time.perf_counter() - connect_started,
    )

    try:
        elapsed, hello = await client.call("hello", {"name": args.name})
        report.add(
            "hello",
            True,
            f"配对={hello.get('paired')} 中继在线配置={hello.get('relay_enabled')}",
            elapsed,
        )
    except Exception as exc:
        report.add("hello", False, str(exc))
        return 1

    if args.code:
        try:
            elapsed, paired = await client.call(
                "pair",
                {"code": args.code, "name": args.name, "platform": "probe"},
            )
            report.add("配对", True, str(paired.get("device") or paired)[:80], elapsed)
        except Exception as exc:
            report.add("配对", False, str(exc))
            return 1

    try:
        elapsed, listing = await client.call("sessions.watch")
        sessions = listing.get("sessions") or []
        ok = elapsed < args.timeout and isinstance(sessions, list)
        largest = max(sessions, key=lambda item: float(item.get("size_kb") or 0), default=None)
        report.add(
            "sessions.watch 整表首包（手机同款，不限条数）",
            ok,
            f"{len(sessions)} 条  约 {client.bytes_in}B 入站"
            + (f"  最大 {largest.get('size_kb')}KB {largest.get('title','')[:24]}" if largest else ""),
            elapsed,
        )
        if not ok:
            return 1
    except Exception as exc:
        report.add("sessions.watch 整表首包（手机同款，不限条数）", False, str(exc))
        return 1

    if not sessions:
        report.add("session.watch 详情首包", False, "列表为空，无法打开详情")
        return 1

    try:
        _elapsed, catalog = await client.call("sessions.list", {"limit": 400})
        pool = catalog.get("sessions") or sessions
    except Exception:
        pool = sessions
    targets = _pick_runtime_samples(pool)
    target = targets[0]
    try:
        elapsed, page = await client.call("session.watch", {"key": target["key"]})
        messages = page.get("messages") or []
        users = [item for item in messages if item.get("role") == "user"]
        assistants = [item for item in messages if item.get("role") == "assistant"]
        first_user = str((users[0].get("text") if users else "") or "")
        leaked = first_user.lstrip().startswith(
            ("# AGENTS.md instructions", "<environment_context>", "<user_info>")
        )
        report.add(
            f"session.watch {target.get('runtime')} 抽样",
            not leaked,
            (
                "首条用户句是系统说明，不是真人问题"
                if leaked
                else f"{len(users)} 问/{len(assistants)} 答  首句={(first_user[:36] or '（无）')!r}"
            ),
            elapsed,
        )
    except Exception as err:
        report.add(f"session.watch {target.get('runtime')} 抽样", False, str(err))

    try:
        elapsed, prompts = await client.call("session.prompts", {"key": target["key"]})
        report.add(
            "session.prompts 提问列表",
            True,
            f"{len(prompts.get('prompts') or [])} 项",
            elapsed,
        )
        await client.call("session.unwatch", {"key": target["key"]})
    except Exception as err:
        report.add("session.prompts 提问列表", False, str(err))

    for extra in targets[1:]:
        runtime = str(extra.get("runtime") or "?")
        try:
            elapsed, page = await client.call("session.watch", {"key": extra["key"]})
            messages = page.get("messages") or []
            users = [item for item in messages if item.get("role") == "user"]
            assistants = [item for item in messages if item.get("role") == "assistant"]
            first_user = str((users[0].get("text") if users else "") or "")
            leaked = first_user.lstrip().startswith(
                ("# AGENTS.md instructions", "<environment_context>", "<user_info>")
            )
            empty_but_expected = not messages and bool(str(extra.get("last_user") or "").strip())
            ok = (not leaked) and (not empty_but_expected) and elapsed < args.timeout
            detail = f"{len(users)} 问/{len(assistants)} 答  首句={(first_user[:36] or '（无）')!r}"
            if leaked:
                detail = "首条用户句是系统说明，不是真人问题"
            if empty_but_expected:
                detail = "列表里有最后一句，详情却是空的"
            report.add(f"session.watch {runtime} 抽样", ok, detail, elapsed)
            await client.call("session.unwatch", {"key": extra["key"]})
        except Exception as err:
            report.add(f"session.watch {runtime} 抽样", False, str(err))

    # 连接取代：同一部手机的第二条控制面握手并确认后，开发机必须让新通道工作，
    # 并在短时间内关掉旧通道（一台设备只保留一条控制面；旧通道是手机已放弃的僵尸）。
    try:
        rival = await asyncio.wait_for(_open_client(args, host_key), timeout=args.timeout)
        elapsed, _listing = await rival.call("sessions.watch")
        old_closed = False
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            # websockets ≥ 13 暴露 `state`；旧版本用 `closed`。
            state = getattr(client.socket, "state", None)
            if state is not None and getattr(state, "name", "") in {"CLOSING", "CLOSED"}:
                old_closed = True
                break
            if getattr(client.socket, "closed", False):
                old_closed = True
                break
            await asyncio.sleep(0.2)
        report.add(
            "控制面取代：新通道 watch 成功且旧通道被开发机关闭",
            old_closed,
            "旧通道已关闭" if old_closed else "旧通道 5 秒内仍未被关闭（会累积僵尸通道）",
            elapsed,
        )
        # 之后的步骤全部走新通道。
        with contextlib.suppress(Exception):
            await client.socket.close()
        client = rival
    except Exception as exc:
        report.add("控制面取代：新通道 watch 成功且旧通道被开发机关闭", False, str(exc))

    idle = min(25.0, args.idle_seconds)
    if idle > 0:
        await asyncio.sleep(idle)
        try:
            elapsed, _hello = await client.call("hello", {"name": args.name})
            report.add(
                f"空闲 {idle:.0f}s 后心跳仍通（覆盖中继 ping）",
                True,
                "连接仍在",
                elapsed,
            )
        except Exception as extra:
            report.add(f"空闲 {idle:.0f}s 后心跳仍通（覆盖中继 ping）", False, str(extra))

    print(
        f"累计入站 {client.bytes_in}B 出站 {client.bytes_out}B  "
        f"{'全部通过' if not report.failed else '存在失败'}"
    )
    try:
        await client.socket.close()
    except Exception:
        pass
    return 1 if report.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--relay", required=True, help="Your self-hosted relay URL, for example wss://relay.example.com")
    parser.add_argument("--key", required=True, help="开发机长期公钥")
    parser.add_argument("--host", default="", help="开发机路由标识；省略时从公钥派生")
    parser.add_argument("--code", default="", help="配对码；已配对过的探针钥匙不用给")
    parser.add_argument("--name", default="验收探针")
    parser.add_argument("--timeout", type=float, default=PHONE_RPC_TIMEOUT, help="单次 RPC 超时，默认与手机 20s 对齐")
    parser.add_argument("--rtt-ms", type=float, default=0, help="额外往返延迟毫秒，模拟蜂窝")
    parser.add_argument("--bytes-per-sec", type=float, default=0, help="单方向带宽上限，0 为不限")
    parser.add_argument("--idle-seconds", type=float, default=25, help="空闲后再发 hello；0 跳过")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
