"""Host live-stream broker for Option A hybrid chat streaming.

Aggregates volatile native live events from:
1. Managed Codex panes (`codex_proxy.py` tap via user-private Unix socket);
2. Managed Pi panes (`corral-session-identity.ts` tap via the same Unix socket);
3. Running OpenCode sessions (lazy read-only SSE observer on the running v2 server).

All raw events are normalized by `sesskit.stream_native.NativeEventNormalizer`.
Updates coalesce at ~65 ms (with immediate first-chunk flush) into ephemeral
`kind: "stream"` events for opted-in remote clients, without touching durable
`seq > 0` messages, `_DeltaBuffer`, or `TranscriptCache`. Any oversize draft,
missing tap, or stopped external server silently falls back to persisted history.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sesskit.stream_native import NativeEventNormalizer, NativeUpdate

from corral import observe
from corral.legacy_names import cache_dir as product_cache_dir

_SOCKET_ENV = "CORRAL_LIVE_STREAM_SOCKET"
_SOCKET_NAME = "live-stream.sock"
_FLUSH_INTERVAL = 0.065
_COMPLETE_GRACE_SECONDS = 5.0
_IDLE_DRAFT_SECONDS = 45.0
_MAX_DRAFT_BYTES = 256 * 1024
_MAX_SESSIONS = 32
_MAX_CONNECTIONS = 64
_MAX_IPC_LINE_BYTES = 128 * 1024


def live_stream_socket_path() -> Path:
    """Path to the user-private local IPC socket for host live events."""
    override = os.environ.get(_SOCKET_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return product_cache_dir() / _SOCKET_NAME


@dataclass
class _DraftState:
    stream_id: str
    after_seq: int
    timestamp: float
    text: str = ""
    sent_text: str = ""
    rev: int = 0
    complete: bool = False
    dirty: bool = False
    overflowed: bool = False
    updated_at: float = 0.0
    completed_at: float | None = None


@dataclass
class _SessionStream:
    key: str
    normalizer: NativeEventNormalizer
    drafts: dict[str, _DraftState] = field(default_factory=dict)
    pending_clears: list[str] = field(default_factory=list)
    last_activity: float = field(default_factory=time.monotonic)


def _opencode_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".local" / "state")
    return base / "opencode"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _resolve_opencode_service() -> tuple[str, str | None] | None:
    """Discover a running local OpenCode v2 server URL and password, if available."""
    env_url = (
        os.environ.get("CORRAL_OPENCODE_SERVER_URL", "").strip()
        or os.environ.get("OPENCODE_SERVER_URL", "").strip()
    )
    env_password = (
        os.environ.get("OPENCODE_PASSWORD")
        or os.environ.get("OPENCODE_SERVER_PASSWORD")
    )
    if env_url:
        return env_url.rstrip("/"), env_password
    service_file = _opencode_state_dir() / "service.json"
    try:
        data = json.loads(service_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    url = str(data.get("url") or "").strip().rstrip("/")
    if not url:
        return None
    pid = data.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and not _pid_alive(pid):
        return None
    password = env_password if env_password is not None else (
        str(data["password"]) if isinstance(data.get("password"), str) else None
    )
    return url, password


def _opencode_event_session_id(payload: dict) -> str | None:
    """Extract sessionID from an OpenCode v2/v1 SSE event envelope."""
    candidates: list[Any] = [
        payload.get("sessionID"),
        payload.get("session_id"),
    ]
    for key in ("data", "properties", "payload"):
        nested = payload.get(key)
        if not isinstance(nested, dict):
            continue
        candidates.append(nested.get("sessionID"))
        candidates.append(nested.get("session_id"))
        for inner_key in ("part", "info", "data", "properties"):
            inner = nested.get(inner_key)
            if isinstance(inner, dict):
                candidates.append(inner.get("sessionID"))
                candidates.append(inner.get("session_id"))
    for value in candidates:
        if isinstance(value, str) and value:
            return value
    return None


class _OpenCodeSSEObserver:
    """Lazy read-only SSE consumer active only while live OpenCode sessions are watched."""

    def __init__(
        self,
        ingest: Callable[[str, str, dict], None],
        *,
        resolve_service: Callable[[], tuple[str, str | None] | None] = _resolve_opencode_service,
    ) -> None:
        self._ingest = ingest
        self._resolve_service = resolve_service
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._watched: set[str] = set()
        self._thread: threading.Thread | None = None
        self._active_response = None

    def set_watched(self, session_ids: set[str]) -> None:
        cleaned = {sid for sid in session_ids if isinstance(sid, str) and sid}
        with self._lock:
            if cleaned == self._watched:
                return
            self._watched = cleaned
            need_thread = bool(cleaned) and not self._stop and (
                self._thread is None or not self._thread.is_alive()
            )
            if not cleaned:
                self._close_active_locked()
            if need_thread:
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name="corral-opencode-sse",
                )
                self._thread.start()
        self._wake.set()

    def stop(self) -> None:
        with self._lock:
            self._stop = True
            self._watched = set()
            self._close_active_locked()
            thread = self._thread
            self._thread = None
        self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _close_active_locked(self) -> None:
        resp = self._active_response
        self._active_response = None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def _has_watched(self) -> bool:
        with self._lock:
            return not self._stop and bool(self._watched)

    def _is_watched(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._watched

    def _run(self) -> None:
        backoff = 1.0
        while True:
            self._wake.clear()
            with self._lock:
                if self._stop or not self._watched:
                    self._thread = None
                    return
            service = self._resolve_service()
            if service is None:
                # Server stopped or unavailable: fail quietly and retry with backoff.
                self._wake.wait(timeout=backoff)
                backoff = min(backoff * 2.0, 10.0)
                continue
            url, password = service
            ok = self._consume_once(url, password)
            if not self._has_watched():
                continue
            if ok:
                backoff = 1.0
                self._wake.wait(timeout=0.5)
            else:
                self._wake.wait(timeout=backoff)
                backoff = min(backoff * 2.0, 10.0)

    def _consume_once(self, base_url: str, password: str | None) -> bool:
        headers = {"Accept": "text/event-stream"}
        if password:
            token = base64.b64encode(f"opencode:{password}".encode()).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        req = urllib.request.Request(f"{base_url}/api/event", headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=20.0)
        except (urllib.error.URLError, OSError, ValueError):
            return False
        with self._lock:
            if self._stop or not self._watched:
                try:
                    resp.close()
                except Exception:
                    pass
                return True
            self._active_response = resp
        data_lines: list[str] = []
        received_any = False
        try:
            while self._has_watched():
                try:
                    raw_line = resp.readline()
                except TimeoutError:
                    # Re-check watchers on idle read timeout (heartbeats usually keep alive).
                    continue
                except OSError:
                    break
                if not raw_line:
                    break
                received_any = True
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    if data_lines:
                        self._handle_sse_data("\n".join(data_lines))
                        data_lines.clear()
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
            if data_lines and self._has_watched():
                self._handle_sse_data("\n".join(data_lines))
        finally:
            with self._lock:
                if self._active_response is resp:
                    self._active_response = None
            try:
                resp.close()
            except Exception:
                pass
        return received_any

    def _handle_sse_data(self, raw_json: str) -> None:
        try:
            payload = json.loads(raw_json)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        sid = _opencode_event_session_id(payload)
        if sid and self._is_watched(sid):
            self._ingest("opencode", sid, payload)


class LiveStreamBroker:
    """Aggregates, coalesces, and reconciles volatile agent output streams."""

    def __init__(
        self,
        *,
        emit_event: Callable[[str, dict], None],
        on_turn_activity: Callable[[str], None] | None = None,
        after_seq_provider: Callable[[str], int] | None = None,
        socket_path: Path | None = None,
        enable_socket: bool = True,
        enable_opencode_sse: bool = True,
        flush_interval: float = _FLUSH_INTERVAL,
        complete_grace_seconds: float = _COMPLETE_GRACE_SECONDS,
        idle_draft_seconds: float = _IDLE_DRAFT_SECONDS,
        max_draft_bytes: int = _MAX_DRAFT_BYTES,
    ) -> None:
        self._emit_event = emit_event
        self._on_turn_activity = on_turn_activity
        self._after_seq_provider = after_seq_provider
        self._socket_path = socket_path
        self._enable_socket = enable_socket
        self._enable_opencode_sse = enable_opencode_sse
        self._flush_interval = max(0.01, float(flush_interval))
        self._complete_grace = max(0.2, float(complete_grace_seconds))
        self._idle_draft = max(1.0, float(idle_draft_seconds))
        self._max_draft_bytes = max(1024, int(max_draft_bytes))

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._sessions: OrderedDict[str, _SessionStream] = OrderedDict()
        self._stop = False
        self._started = False
        self._flush_thread: threading.Thread | None = None
        self._accept_thread: threading.Thread | None = None
        self._server_sock: socket.socket | None = None
        self._bound_socket_path: Path | None = None
        self._client_socks: set[socket.socket] = set()
        self._conn_slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        self._opencode = _OpenCodeSSEObserver(self.ingest) if enable_opencode_sse else None

    @property
    def socket_path(self) -> Path | None:
        return self._bound_socket_path

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._stop = False
            self._started = True
            if self._sessions:
                self._ensure_flush_thread_locked()
        if self._enable_socket:
            self._start_socket()

    def _ensure_flush_thread_locked(self) -> None:
        if self._flush_thread is not None or self._stop or not self._started:
            return
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name="corral-live-flush",
        )
        self._flush_thread.start()

    def stop(self) -> None:
        if self._opencode is not None:
            self._opencode.stop()
        with self._lock:
            self._stop = True
            self._started = False
            server = self._server_sock
            self._server_sock = None
            bound = self._bound_socket_path
            self._bound_socket_path = None
            clients = list(self._client_socks)
            self._client_socks.clear()
            flush_thread = self._flush_thread
            self._flush_thread = None
            accept_thread = self._accept_thread
            self._accept_thread = None
            self._cond.notify_all()
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        if bound is not None:
            try:
                bound.unlink(missing_ok=True)
            except OSError:
                pass
        for thread in (flush_thread, accept_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1.0)

    def _start_socket(self) -> None:
        path = self._socket_path if self._socket_path is not None else live_stream_socket_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, stat.S_IRWXU)
            if path.exists() or path.is_symlink():
                path.unlink(missing_ok=True)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(path))
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            srv.listen(32)
            srv.settimeout(1.0)
        except OSError as exc:
            observe.event("remote_live_stream_socket_failed", error=str(exc))
            return
        with self._lock:
            if self._stop:
                try:
                    srv.close()
                except OSError:
                    pass
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                return
            self._server_sock = srv
            self._bound_socket_path = path
            self._accept_thread = threading.Thread(
                target=self._accept_loop, args=(srv,), daemon=True, name="corral-live-accept",
            )
            self._accept_thread.start()

    def _accept_loop(self, srv: socket.socket) -> None:
        while True:
            with self._lock:
                if self._stop or self._server_sock is not srv:
                    return
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._conn_slots.acquire(blocking=False):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            with self._lock:
                if self._stop:
                    self._conn_slots.release()
                    try:
                        conn.close()
                    except OSError:
                        pass
                    return
                self._client_socks.add(conn)
            threading.Thread(
                target=self._read_ipc_connection, args=(conn,), daemon=True, name="corral-live-ipc",
            ).start()

    def _read_ipc_connection(self, conn: socket.socket) -> None:
        buf = bytearray()
        try:
            conn.settimeout(2.0)
            while True:
                with self._lock:
                    if self._stop:
                        return
                try:
                    chunk = conn.recv(16384)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                buf.extend(chunk)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        if len(buf) > _MAX_IPC_LINE_BYTES:
                            # Drop oversized malformed frame buffer safely.
                            buf.clear()
                        break
                    raw_line = bytes(buf[:nl])
                    del buf[: nl + 1]
                    if raw_line and len(raw_line) <= _MAX_IPC_LINE_BYTES:
                        self._handle_ipc_line(raw_line)
        finally:
            with self._lock:
                self._client_socks.discard(conn)
            try:
                conn.close()
            except OSError:
                pass
            self._conn_slots.release()

    def _handle_ipc_line(self, raw_line: bytes) -> None:
        try:
            envelope = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(envelope, dict):
            return
        if envelope.get("v") not in (None, 1):
            return
        runtime = str(envelope.get("runtime") or "").strip().lower()
        session_id = str(envelope.get("session_id") or "").strip()
        event = envelope.get("event")
        if runtime not in {"codex", "pi"} or not session_id or not isinstance(event, dict):
            return
        self.ingest(runtime, session_id, event)

    def set_watched_opencode(self, session_ids: set[str]) -> None:
        if self._opencode is not None:
            self._opencode.set_watched(session_ids)

    def ingest(self, runtime: str, session_id: str, raw_event: dict) -> None:
        """Feed one native live event for `runtime:session_id`."""
        runtime = str(runtime or "").strip().lower()
        session_id = str(session_id or "").strip()
        if not runtime or not session_id or not isinstance(raw_event, dict):
            return
        canonical_key = f"{runtime}:{session_id}"
        now = time.monotonic()
        wall_now = time.time()
        immediate_emits: list[dict] = []
        turn_activity = False

        with self._lock:
            state = self._sessions.get(canonical_key)
            if state is None:
                try:
                    normalizer = NativeEventNormalizer(runtime, session_id)
                except ValueError:
                    return
                state = _SessionStream(key=canonical_key, normalizer=normalizer)
                self._sessions[canonical_key] = state
                while len(self._sessions) > _MAX_SESSIONS:
                    self._sessions.popitem(last=False)
            else:
                self._sessions.move_to_end(canonical_key)
            state.last_activity = now

            try:
                updates: list[NativeUpdate] = state.normalizer.normalize(raw_event)
            except Exception:
                return

            has_lifecycle = False
            for update in updates:
                event = update.event
                if event is None:
                    continue
                if event.type == "lifecycle":
                    has_lifecycle = True
                    continue
                if event.type != "assistant_message":
                    continue
                text = event.text or ""
                draft = state.drafts.get(update.key)
                if draft is None:
                    if not text.strip():
                        # Wait until non-empty content starts before opening a draft.
                        if update.complete is True:
                            has_lifecycle = True
                        continue
                    after_seq = 0
                    if self._after_seq_provider is not None:
                        try:
                            after_seq = int(self._after_seq_provider(canonical_key))
                        except Exception:
                            after_seq = 0
                    draft = _DraftState(
                        stream_id=update.key,
                        after_seq=after_seq,
                        timestamp=wall_now,
                        updated_at=now,
                    )
                    state.drafts[update.key] = draft
                if draft.overflowed:
                    continue
                encoded_bytes = len(text.encode("utf-8"))
                if encoded_bytes > self._max_draft_bytes:
                    draft.overflowed = True
                    draft.dirty = False
                    draft.complete = True
                    draft.completed_at = now
                    if draft.rev > 0:
                        state.pending_clears.append(draft.stream_id)
                    continue
                if text != draft.text:
                    draft.text = text
                    draft.dirty = True
                    draft.updated_at = now
                if update.complete is True and not draft.complete:
                    draft.complete = True
                    draft.completed_at = now
                    draft.dirty = True
                    draft.updated_at = now
                    turn_activity = True

            if has_lifecycle and state.normalizer.outcome.status in {"done", "aborted"}:
                turn_activity = True
                for draft in state.drafts.values():
                    if not draft.overflowed and not draft.complete:
                        draft.complete = True
                        draft.completed_at = now
                        draft.dirty = True
                        draft.updated_at = now

            # Flush first chunk (rev == 0) and completion immediately for minimal latency;
            # subsequent mid-stream tokens wait for the coalescing window.
            immediate_emits = self._collect_emits_locked(state, now=now, only_immediate=True)
            has_dirty = any(d.dirty for d in state.drafts.values()) or bool(state.pending_clears)
            if has_dirty or state.drafts:
                self._ensure_flush_thread_locked()
            if has_dirty:
                self._cond.notify_all()

        for payload in immediate_emits:
            self._emit_event(canonical_key, payload)
        if turn_activity and self._on_turn_activity is not None:
            self._on_turn_activity(canonical_key)

    def _collect_emits_locked(
        self,
        state: _SessionStream,
        *,
        now: float,
        only_immediate: bool,
    ) -> list[dict]:
        emits: list[dict] = []
        while state.pending_clears:
            sid = state.pending_clears.pop(0)
            state.drafts.pop(sid, None)
            emits.append({"op": "clear", "stream_id": sid})

        for sid in list(state.drafts.keys()):
            draft = state.drafts[sid]
            if draft.overflowed:
                continue
            if not draft.dirty:
                continue
            is_immediate = draft.rev == 0 or draft.complete
            if only_immediate and not is_immediate:
                continue
            draft.dirty = False
            draft.rev += 1
            if draft.rev == 1 or not draft.text.startswith(draft.sent_text):
                payload: dict[str, Any] = {
                    "op": "replace",
                    "stream_id": draft.stream_id,
                    "rev": draft.rev,
                    "role": "assistant",
                    "text": draft.text,
                    "after_seq": draft.after_seq,
                    "complete": draft.complete,
                    "timestamp": draft.timestamp,
                }
            else:
                delta = draft.text[len(draft.sent_text) :]
                payload = {
                    "op": "delta",
                    "stream_id": draft.stream_id,
                    "rev": draft.rev,
                    "delta": delta,
                    "after_seq": draft.after_seq,
                    "complete": draft.complete,
                    "timestamp": draft.timestamp,
                }
            draft.sent_text = draft.text
            emits.append(payload)
        return emits

    def _flush_loop(self) -> None:
        while True:
            pending_out: list[tuple[str, dict]] = []
            with self._lock:
                if self._stop:
                    return
                self._cond.wait(timeout=self._flush_interval)
                if self._stop:
                    return
                now = time.monotonic()
                for key in list(self._sessions.keys()):
                    state = self._sessions[key]
                    for payload in self._collect_emits_locked(state, now=now, only_immediate=False):
                        pending_out.append((key, payload))
                    for sid in list(state.drafts.keys()):
                        draft = state.drafts[sid]
                        expired_complete = (
                            draft.completed_at is not None
                            and (now - draft.completed_at) >= self._complete_grace
                        )
                        expired_idle = (
                            draft.completed_at is None
                            and (now - draft.updated_at) >= self._idle_draft
                        )
                        if expired_complete or expired_idle:
                            state.drafts.pop(sid, None)
                            if draft.rev > 0 and not draft.overflowed:
                                pending_out.append((key, {"op": "clear", "stream_id": sid}))
                    if not state.drafts and not state.pending_clears and (now - state.last_activity) > 120.0:
                        self._sessions.pop(key, None)
            for key, payload in pending_out:
                self._emit_event(key, payload)

    def active_streams(self, canonical_key: str) -> list[dict]:
        """Return currently active non-empty drafts for mid-turn `session.watch` snapshots."""
        with self._lock:
            state = self._sessions.get(canonical_key)
            if state is None:
                return []
            snapshots: list[dict] = []
            for draft in state.drafts.values():
                if draft.overflowed or not draft.text.strip():
                    continue
                if draft.rev == 0:
                    draft.rev = 1
                draft.sent_text = draft.text
                draft.dirty = False
                snapshots.append(
                    {
                        "stream_id": draft.stream_id,
                        "rev": draft.rev,
                        "role": "assistant",
                        "text": draft.text,
                        "after_seq": draft.after_seq,
                        "complete": draft.complete,
                        "timestamp": draft.timestamp,
                    }
                )
            return snapshots

    def reconcile_official(self, canonical_key: str, new_messages: list) -> list[str]:
        """Drop ephemeral drafts superseded by newly arrived durable `RichMessage`s."""
        assistant_msgs = [
            msg for msg in new_messages
            if getattr(msg, "role", "") == "assistant" and int(getattr(msg, "seq", 0) or 0) > 0
        ]
        if not assistant_msgs:
            return []
        max_seq = max(int(getattr(msg, "seq", 0) or 0) for msg in assistant_msgs)
        eligible = [
            msg for msg in assistant_msgs
            if str(getattr(msg, "text", "") or "").strip()
        ]
        cleared: list[str] = []
        with self._lock:
            state = self._sessions.get(canonical_key)
            if state is None:
                return []
            for sid in list(state.drafts.keys()):
                draft = state.drafts[sid]
                draft_text = draft.text.strip()
                text_match = bool(draft_text) and any(
                    (int(getattr(msg, "seq", 0) or 0) > draft.after_seq or draft.after_seq == 0)
                    and (
                        official == draft_text
                        or official.startswith(draft_text)
                        or (len(draft_text) >= 16 and draft_text in official)
                    )
                    for msg in eligible
                    for official in (str(getattr(msg, "text", "") or "").strip(),)
                )
                complete_match = draft.complete and max_seq > draft.after_seq
                if text_match or complete_match:
                    state.drafts.pop(sid, None)
                    if draft.rev > 0 and not draft.overflowed:
                        cleared.append(sid)
        return cleared

    def clear_session(self, canonical_key: str) -> list[str]:
        """Clear all ephemeral drafts for a reset/stopped session."""
        with self._lock:
            state = self._sessions.pop(canonical_key, None)
            if state is None:
                return []
            return [
                draft.stream_id
                for draft in state.drafts.values()
                if draft.rev > 0 and not draft.overflowed
            ]
