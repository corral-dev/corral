"""One ordered, byte-accounted writer per physical transport lane.

Every physical socket — a LAN websocket, the relay interaction socket, the relay
bulk socket — owns exactly one :class:`LaneWriter`, and every logical channel
sharing that socket submits its frames through it. A single drain coroutine per
lane sends one frame at a time in submission order, so the counter that
``HostChannel`` assigned under its encryption lock reaches the wire in that same
order, and a stalled peer cannot accumulate an unbounded number of pending send
tasks.

Admission is explicit and follows the Go relay's per-destination queue
(``relay/internal/hub/queue.go``): byte accounting, not item counts alone. The
per-channel share, the lane budget and the shared transport budget all count
queued *and* in-flight frames, and an idle destination always admits one legal
maximum-sized frame. A frame that cannot be admitted is never silently dropped:
its channel is reset (the client rebinds with a fresh handshake and fresh
counters) and the reset is recorded as an observation event. Frames that belong
to no channel carry no per-channel counter to desynchronise, so they are refused
with an event instead of resetting every device on the lane.

Three limits exist because each has a different owner:

* ``CHANNEL_MAX_BYTES`` — one device may hold at most one legal maximum frame, so
  a single slow phone cannot occupy the whole socket.
* ``LANE_MAX_BYTES`` — one socket keeps ~4 MiB of headroom beyond a maximum
  frame. Two real large pages (~4 MiB each, see ``protocol``'s uncompressed
  limit) therefore coexist without either device being reset.
* ``AGGREGATE_MAX_BYTES`` — every lane of one transport draws from one ceiling,
  so eight stalled LAN sockets cannot each hold a full lane budget.

Normal traffic never reaches these ceilings: over loopback/LAN nothing queues,
and over a slow link the client fetches one page at a time.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from corral import observe
from corral.remote import protocol

# Largest frame the peer accepts, header included (see `protocol.decode_frame`).
MAX_FRAME_BYTES = int(getattr(protocol, "_MAX_FRAME_BYTES", 8 * 1024 * 1024))

CHANNEL_MAX_BYTES = MAX_FRAME_BYTES
CHANNEL_MAX_FRAMES = 128
LANE_MAX_BYTES = CHANNEL_MAX_BYTES + 4 * 1024 * 1024
LANE_MAX_FRAMES = 256
AGGREGATE_MAX_BYTES = 32 * 1024 * 1024
AGGREGATE_MAX_FRAMES = 512


class DestinationReset(RuntimeError):
    """The frame was not delivered because its destination was reset."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LaneBudget:
    """Byte and frame ceiling shared by every lane of one transport."""

    def __init__(
        self,
        max_bytes: int = AGGREGATE_MAX_BYTES,
        max_frames: int = AGGREGATE_MAX_FRAMES,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_frames = max_frames
        self._lock = threading.Lock()
        self._bytes = 0
        self._frames = 0

    @property
    def pending_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def pending_frames(self) -> int:
        with self._lock:
            return self._frames

    def charge(self, size: int) -> bool:
        with self._lock:
            if self._bytes + size > self.max_bytes or self._frames + 1 > self.max_frames:
                return False
            self._bytes += size
            self._frames += 1
            return True

    def release(self, size: int) -> None:
        with self._lock:
            self._bytes = max(0, self._bytes - size)
            self._frames = max(0, self._frames - 1)


@dataclass(eq=False)
class _Frame:
    """One admitted frame and its pending delivery outcome."""

    frame: bytes
    channel_id: bytes | None
    outcome: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    queued: bool = False
    started: bool = False
    released: bool = False
    settled: bool = False


class LaneWriter:
    """Ordered, byte-accounted outbound writer for one physical socket.

    ``submit`` is thread-safe and never blocks on the socket; callers (channel
    worker threads) only hand the already-encrypted frame over. Exactly one
    drain coroutine sends at a time. Reset and shutdown bookkeeping runs on the
    owning event loop when it is available, so cancelling a queued frame can
    never interrupt a socket send that already started — cancelling a partially
    written frame would corrupt the transport, so that one frame is allowed to
    finish and release its own accounting.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        socket,
        *,
        lane: str = "lane",
        budget: LaneBudget | None = None,
        on_channel_reset: Callable[[bytes, str], None] | None = None,
        on_lane_reset: Callable[[str], None] | None = None,
    ) -> None:
        self._loop = loop
        self._socket = socket
        self.lane = lane
        self._budget = budget if budget is not None else LaneBudget()
        self._on_channel_reset = on_channel_reset
        self._on_lane_reset = on_lane_reset
        self._lock = threading.Lock()
        self._closed = False
        self._reason = ""
        self._draining = False
        self._queue: deque[_Frame] = deque()
        self._bytes = 0
        self._frames = 0
        self._refused = 0
        self._cancelled = 0
        self._channel_bytes: dict[bytes, int] = {}
        self._channel_frames: dict[bytes, int] = {}
        self._pending: dict[bytes | None, list[_Frame]] = {}

    # -- observable state ----------------------------------------------------

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    @property
    def pending_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def pending_frames(self) -> int:
        with self._lock:
            return self._frames

    def pending_channel_bytes(self, channel_id: bytes | None) -> int:
        with self._lock:
            return self._channel_bytes.get(channel_id, 0)

    @property
    def refused_frames(self) -> int:
        with self._lock:
            return self._refused

    @property
    def cancelled_outcomes(self) -> int:
        with self._lock:
            return self._cancelled

    # -- admission and submission -------------------------------------------

    def submit(self, frame: bytes, channel_id: bytes | None = None) -> bool:
        """Admit ``frame`` for ordered delivery. Thread-safe.

        Returns ``False`` when the frame was not accepted. A channel-scoped
        refusal resets that channel, because its counter has already advanced
        and continuing to use it would desynchronise the client.
        """
        size = len(frame)
        if size > MAX_FRAME_BYTES:
            self._refuse_oversized(size, channel_id)
            return False
        entry = _Frame(frame=frame, channel_id=channel_id)
        with self._lock:
            if self._closed:
                return False
            direct = not self._draining
            if direct:
                self._draining = True
            reason = self._try_admit_locked(entry, enqueue=not direct)
            if reason:
                self._refused += 1
                if direct:
                    self._draining = False
                pending_bytes = self._bytes
                pending_frames = self._frames
        if not reason:
            # The hand-off is scheduled for every submitted frame so the
            # transport's ordering contract is visible at the call site; only the
            # drain owner sends, every other hand-off returns without touching the
            # socket. Frames enqueued by others are drained by that owner in FIFO
            # order, so this scheduling order cannot reorder the wire.
            self._schedule_drain(frame, entry if direct else None)
            return True
        if reason:
            observe.event(
                "remote_writer_refused",
                lane=self.lane,
                channel=channel_id.hex()[:8] if channel_id is not None else "",
                reason=reason,
                bytes=size,
                pending_bytes=pending_bytes,
                pending_frames=pending_frames,
            )
            if channel_id is not None:
                self._schedule(self._settle_reset, channel_id, reason)
        return False

    def _refuse_oversized(self, size: int, channel_id: bytes | None) -> None:
        # The peer rejects such a frame outright, so it must never reach the
        # wire. Its channel already advanced its counter, so that channel still
        # has to reset rather than continue out of sequence.
        with self._lock:
            self._refused += 1
            pending_bytes = self._bytes
            pending_frames = self._frames
        observe.event(
            "remote_writer_refused",
            lane=self.lane,
            channel=channel_id.hex()[:8] if channel_id is not None else "",
            reason="frame_too_large",
            bytes=size,
            pending_bytes=pending_bytes,
            pending_frames=pending_frames,
        )
        if channel_id is not None:
            self._schedule(self._settle_reset, channel_id, "frame_too_large")

    def _try_admit_locked(self, entry: _Frame, *, enqueue: bool) -> str:
        size = len(entry.frame)
        channel_id = entry.channel_id
        if channel_id is not None:
            if self._channel_bytes.get(channel_id, 0) + size > CHANNEL_MAX_BYTES:
                return "channel_bytes"
            if self._channel_frames.get(channel_id, 0) + 1 > CHANNEL_MAX_FRAMES:
                return "channel_frames"
        if self._bytes + size > LANE_MAX_BYTES:
            return "lane_bytes"
        if self._frames + 1 > LANE_MAX_FRAMES:
            return "lane_frames"
        if not self._budget.charge(size):
            return "aggregate_budget"
        self._bytes += size
        self._frames += 1
        self._pending.setdefault(channel_id, []).append(entry)
        if enqueue:
            self._queue.append(entry)
            entry.queued = True
        if channel_id is not None:
            self._channel_bytes[channel_id] = self._channel_bytes.get(channel_id, 0) + size
            self._channel_frames[channel_id] = self._channel_frames.get(channel_id, 0) + 1
        return ""

    def _release_locked(self, entry: _Frame) -> None:
        if entry.released:
            return
        entry.released = True
        size = len(entry.frame)
        self._bytes = max(0, self._bytes - size)
        self._frames = max(0, self._frames - 1)
        pending = self._pending.get(entry.channel_id)
        if pending is not None:
            try:
                pending.remove(entry)
            except ValueError:  # pragma: no cover - defensive, identity-tracked
                pass
            if not pending:
                self._pending.pop(entry.channel_id, None)
        if entry.channel_id is not None:
            remaining = self._channel_bytes.get(entry.channel_id, 0) - size
            if remaining > 0:
                self._channel_bytes[entry.channel_id] = remaining
            else:
                self._channel_bytes.pop(entry.channel_id, None)
            frames = self._channel_frames.get(entry.channel_id, 0) - 1
            if frames > 0:
                self._channel_frames[entry.channel_id] = frames
            else:
                self._channel_frames.pop(entry.channel_id, None)
        if entry.queued:
            entry.queued = False
            try:
                self._queue.remove(entry)
            except ValueError:  # pragma: no cover - defensive, popped by drainer
                pass
        self._budget.release(size)

    # -- delivery ------------------------------------------------------------

    def _schedule_drain(self, frame: bytes, entry: _Frame | None) -> None:
        coro = self._pump(self._socket, frame, entry)
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError:  # loop closed between admission and hand-off
            coro.close()
            with self._lock:
                self._draining = False
            self._schedule(self._discard_pending, "loop_closed", True)

    async def _pump(self, socket, frame: bytes, entry: _Frame | None) -> None:
        """Send the triggering frame, then drain the lane while it owns it.

        ``frame`` is the frame whose submission started this drain; frames
        submitted while a drain is running are already in the lane queue and are
        sent by that owner in FIFO order.
        """
        if entry is None:
            return
        try:
            error = await self._deliver(socket, entry, frame)
            if error is not None:
                if not isinstance(error, DestinationReset):
                    self._fail_lane(error)
                self._stop_draining()
                return
            while True:
                with self._lock:
                    if self._closed or not self._queue:
                        self._draining = False
                        return
                    following = self._queue.popleft()
                    following.queued = False
                error = await self._deliver(socket, following, following.frame)
                if error is not None:
                    if not isinstance(error, DestinationReset):
                        self._fail_lane(error)
                    self._stop_draining()
                    return
        except asyncio.CancelledError:
            self._stop_draining()
            self._schedule(self._discard_pending, "lane_cancelled", True)
            raise

    async def _deliver(self, socket, entry: _Frame, frame: bytes) -> BaseException | None:
        with self._lock:
            refused = DestinationReset(self._reason or "lane_closed") if self._closed else None
            if entry.released:
                return None
            if refused is None:
                entry.started = True
        if refused is not None:
            self._settle_frame(entry, refused)
            return refused
        error: BaseException | None = None
        try:
            await socket.send(frame)
        except asyncio.CancelledError:
            self._settle_frame(entry, DestinationReset("lane_cancelled"))
            raise
        except BaseException as exc:  # socket failures are expected
            error = exc
        self._settle_frame(entry, error)
        return error

    def _settle_frame(self, entry: _Frame, error: BaseException | None) -> None:
        with self._lock:
            if entry.settled:
                return
            entry.settled = True
            self._release_locked(entry)
            outcome = entry.outcome
        if outcome.cancelled():
            return
        if error is None:
            outcome.set_result(None)
        else:
            outcome.set_exception(error)

    def _stop_draining(self) -> None:
        with self._lock:
            self._draining = False

    # -- resets and shutdown -------------------------------------------------

    def _fail_lane(self, error: BaseException) -> None:
        observe.event(
            "remote_writer_send_failed",
            lane=self.lane,
            error_type=type(error).__name__,
            error=str(error)[:200],
        )
        with self._lock:
            self._closed = True
            self._reason = "send_failed"
        self._schedule(self._discard_pending, "send_failed", True)

    def reset(self, reason: str = "lane_reset") -> None:
        """Reset the whole lane: refuse new frames, drop queued ones, notify."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._reason = reason
        self._schedule(self._discard_pending, reason, True)

    def close(self, reason: str = "lane_closed") -> None:
        """Shut the lane down without notifying anybody (transport teardown)."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._reason = reason
        self._schedule(self._discard_pending, reason, False)

    def _settle_reset(self, channel_id: bytes, reason: str) -> None:
        outcomes: list[concurrent.futures.Future] = []
        with self._lock:
            for entry in list(self._pending.get(channel_id, ())):
                if entry.started:
                    # A frame already inside socket.send() is allowed to finish.
                    continue
                self._release_locked(entry)
                entry.settled = True
                outcomes.append(entry.outcome)
            pending_bytes = self._bytes
            pending_frames = self._frames
        cancelled = self._cancel_outcomes(outcomes)
        with self._lock:
            self._cancelled += cancelled
        if self._on_channel_reset is not None:
            try:
                self._on_channel_reset(channel_id, reason)
            except Exception as exc:  # pragma: no cover - callback owns its errors
                observe.event("remote_writer_callback_failed", error=str(exc)[:200])
        observe.event(
            "remote_writer_channel_reset",
            lane=self.lane,
            channel=channel_id.hex()[:8],
            reason=reason,
            cancelled=cancelled,
            pending_bytes=pending_bytes,
            pending_frames=pending_frames,
        )

    def _discard_pending(self, reason: str, notify: bool) -> None:
        outcomes: list[concurrent.futures.Future] = []
        with self._lock:
            self._closed = True
            self._reason = reason
            for entries in list(self._pending.values()):
                for entry in list(entries):
                    if entry.started:
                        continue
                    self._release_locked(entry)
                    entry.settled = True
                    outcomes.append(entry.outcome)
            pending_bytes = self._bytes
            pending_frames = self._frames
        cancelled = self._cancel_outcomes(outcomes)
        with self._lock:
            self._cancelled += cancelled
        if notify and self._on_lane_reset is not None:
            try:
                self._on_lane_reset(reason)
            except Exception as exc:  # pragma: no cover - callback owns its errors
                observe.event("remote_writer_callback_failed", error=str(exc)[:200])
        observe.event(
            "remote_writer_closed" if not notify else "remote_writer_reset",
            lane=self.lane,
            reason=reason,
            cancelled=cancelled,
            pending_bytes=pending_bytes,
            pending_frames=pending_frames,
        )

    @staticmethod
    def _cancel_outcomes(outcomes: list[concurrent.futures.Future]) -> int:
        cancelled = 0
        for outcome in outcomes:
            if outcome.cancel():
                cancelled += 1
        return cancelled

    def _schedule(self, callback, *args) -> None:
        """Run ``callback`` on the lane's loop, so frame cancellation is safe."""
        loop = self._loop
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            callback(*args)
            return
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:  # loop already closed; nothing can be running
            callback(*args)
