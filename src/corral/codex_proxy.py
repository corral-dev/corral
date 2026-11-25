"""Transparent local Codex app-server bridge for exact hosted-session identity.

Only Corral-managed Codex TUI panes use this bridge. JSON-RPC messages are
forwarded unchanged; successful root thread responses provide the identity
claim consumed by the session scanner.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from corral.codex_identity import write_claim
from corral.legacy_names import cache_dir as product_cache_dir

_THREAD_METHODS = {"thread/start", "thread/resume", "thread/fork"}
_LIVE_STREAM_METHODS = frozenset({
    "item/agentMessage/delta",
    "item/started",
    "item/completed",
    "turn/started",
    "turn/completed",
    "error",
})
_LIVE_RETRY_COOLDOWN = 2.0
_LIVE_SOCKET_ENV = "CORRAL_LIVE_STREAM_SOCKET"
_MAX_RPC_LINE = 64 * 1024 * 1024
_AUTO_APPROVE = "--dangerously-bypass-approvals-and-sandbox"
_NO_DAEMON = "--no-daemon"


def _live_socket_path() -> Path:
    override = os.environ.get(_LIVE_SOCKET_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return product_cache_dir() / "live-stream.sock"


class _LiveStreamTap:
    """Best-effort non-blocking Unix-socket publisher for live Codex notifications.

    Runs on a dedicated background writer thread so a missing/closed daemon
    socket or backpressure can never stall `_forward.to_tui()`.
    """

    def __init__(self, socket_path: Path | None = None) -> None:
        self._socket_path = socket_path
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=256)
        self._stop = False
        self._disabled_until = 0.0
        self._thread: threading.Thread | None = None
        self.root_thread_id: str | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="codex-live-tap")
        self._thread.start()

    def close(self) -> None:
        self._stop = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def observe_outgoing(self, message: dict) -> None:
        method = message.get("method")
        if method not in _LIVE_STREAM_METHODS:
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        thread_id = params.get("threadId") or self.root_thread_id
        if not isinstance(thread_id, str) or not thread_id:
            return
        if self._disabled_until and time.monotonic() < self._disabled_until:
            return
        payload = json.dumps(
            {
                "v": 1,
                "runtime": "codex",
                "session_id": thread_id,
                "event": message,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            # Avoid partial character drops by pausing until daemon catches up.
            self._disabled_until = time.monotonic() + _LIVE_RETRY_COOLDOWN

    def _run(self) -> None:
        sock: socket.socket | None = None
        last_connect_fail = 0.0
        try:
            while not self._stop:
                item = self._queue.get()
                if item is None or self._stop:
                    return
                now = time.monotonic()
                if sock is None:
                    if now - last_connect_fail < _LIVE_RETRY_COOLDOWN:
                        continue
                    path = self._socket_path if self._socket_path is not None else _live_socket_path()
                    if not path.exists():
                        last_connect_fail = now
                        continue
                    try:
                        candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        candidate.settimeout(0.5)
                        candidate.connect(str(path))
                        sock = candidate
                    except OSError:
                        last_connect_fail = now
                        continue
                try:
                    sock.sendall(item)
                except OSError:
                    last_connect_fail = time.monotonic()
                    try:
                        sock.close()
                    except OSError:
                        pass
                    sock = None
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def _remote_launch_args(args: list[str]) -> tuple[list[str], list[str]]:
    """Move local launch policy and workspace roots onto the private server."""
    tui_args: list[str] = []
    roots: list[str] = []
    auto_approve = False
    remaining = iter(args[1:])
    for arg in remaining:
        if arg == "--":
            tui_args.extend((arg, *remaining))
            break
        if arg == _NO_DAEMON:
            continue
        if arg == _AUTO_APPROVE:
            auto_approve = True
        elif arg == "--add-dir":
            root = next(remaining, None)
            if not root:
                raise ValueError("--add-dir requires a directory")
            roots.append(os.path.abspath(root))
        elif arg.startswith("--add-dir="):
            root = arg.partition("=")[2]
            if not root:
                raise ValueError("--add-dir requires a directory")
            roots.append(os.path.abspath(root))
        else:
            tui_args.append(arg)
    server_args = ["app-server", "--stdio"]
    if auto_approve:
        server_args += ["-c", "approval_policy=never", "-c", "sandbox_mode=danger-full-access"]
    if roots:
        # --remote rejects both --add-dir and TUI writable_roots overrides.
        # Keep non-BMP Unicode literal: JSON surrogate escapes are invalid TOML.
        encoded_roots = json.dumps(roots, ensure_ascii=False).replace("\x7f", r"\u007f")
        server_args += ["-c", "sandbox_workspace_write.writable_roots=" + encoded_roots]
    return tui_args, server_args


def _request_key(message: dict) -> str | None:
    request_id = message.get("id")
    if isinstance(request_id, (str, int)) and not isinstance(request_id, bool):
        return str(request_id)
    return None


def _observe_response(
    message: dict,
    pending: dict[str, str],
    *,
    tap: _LiveStreamTap | None = None,
) -> None:
    key = _request_key(message)
    method = pending.pop(key, None) if key is not None else None
    if method is None:
        return
    result = message.get("result")
    thread = result.get("thread") if isinstance(result, dict) else None
    if not isinstance(thread, dict):
        return
    thread_id = thread.get("id")
    path = thread.get("path")
    if isinstance(thread_id, str) and tap is not None:
        tap.root_thread_id = thread_id
    if isinstance(thread_id, str) and isinstance(path, str):
        write_claim(thread_id, path, os.getpid())


async def _forward(
    websocket,
    upstream: asyncio.subprocess.Process,
    *,
    tap: _LiveStreamTap | None = None,
) -> None:
    """Forward one Codex TUI connection without modifying its requests."""
    pending: dict[str, str] = {}

    async def to_upstream() -> None:
        assert upstream.stdin is not None
        async for frame in websocket:
            if not isinstance(frame, str):
                continue
            try:
                message = json.loads(frame)
            except json.JSONDecodeError:
                message = None
            if isinstance(message, dict) and message.get("method") in _THREAD_METHODS:
                key = _request_key(message)
                if key is not None:
                    pending[key] = str(message["method"])
            upstream.stdin.write(frame.encode("utf-8") + b"\n")
            await upstream.stdin.drain()

    async def to_tui() -> None:
        assert upstream.stdout is not None
        while line := await upstream.stdout.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                message = None
            if isinstance(message, dict):
                _observe_response(message, pending, tap=tap)
                if tap is not None:
                    tap.observe_outgoing(message)
            await websocket.send(line.decode("utf-8").rstrip("\r\n"))

    tasks = {asyncio.create_task(to_upstream()), asyncio.create_task(to_tui())}
    done, waiting = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in waiting:
        task.cancel()
    await asyncio.gather(*waiting, return_exceptions=True)
    for task in done:
        task.result()


async def _run(args: list[str]) -> int:
    from websockets.asyncio.server import unix_serve

    if not args:
        raise RuntimeError("Codex launch command is missing")
    codex = shutil.which(args[0])
    if not codex:
        raise RuntimeError("Codex executable is unavailable")
    tui_args, server_args = _remote_launch_args(args)
    tap = _LiveStreamTap()
    tap.start()

    with tempfile.TemporaryDirectory(prefix="corral-codex-") as directory:
        socket_path = str(Path(directory) / "rpc.sock")
        upstream = await asyncio.create_subprocess_exec(
            codex, *server_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=_MAX_RPC_LINE,
        )
        connected = False

        async def handler(websocket) -> None:
            nonlocal connected
            if connected:
                await websocket.close(code=1013, reason="Codex pane already connected")
                return
            connected = True
            try:
                await _forward(websocket, upstream, tap=tap)
            finally:
                connected = False

        try:
            async with unix_serve(handler, socket_path, max_size=None):
                tui = await asyncio.create_subprocess_exec(
                    codex, "--remote", f"unix://{socket_path}", *tui_args,
                )
                return await tui.wait()
        finally:
            tap.close()
            if upstream.returncode is None:
                try:
                    upstream.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(upstream.wait(), timeout=5)
                except TimeoutError:
                    upstream.kill()
                    await upstream.wait()


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--":
        args = args[1:]
    try:
        return asyncio.run(_run(args))
    except (OSError, RuntimeError) as exc:
        print(f"Corral could not start Codex: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
