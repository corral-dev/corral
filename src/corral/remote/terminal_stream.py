"""Raw terminal stream for desktop clients that run their own emulator.

The phone draws host snapshots (`screen.py`). The Mac terminal view instead
runs a real emulator (SwiftTerm) and needs the bytes the agent wrote, in order,
so scrollback, scrolling and redraws are local and smooth.

Source: the tmux control-mode client of the hosted session (`embed` channel
pool, so input and resizes in this process go through the same client).
``%output`` lines carry the pane's raw output; command responses and output
share one ordered stream, which lets a snapshot (capture-pane) be stitched to
the live stream without losing or doubling bytes: output counted at or below
the snapshot's barrier is already in the capture.

Wire events on channel ``term:<session>`` (all carry ``seq``, consecutive per
stream): ``snapshot`` (reset the emulator to ``cols`` x ``rows`` and feed
``data``), ``output`` (feed ``data``), ``ended`` (the pane is gone). A client
that sees a gap asks for ``terminal.resync``.

Sizing: desktop viewers vote in the same widest-viewer registry the TUI windows
use (`embed.desired_host_size`); the pane is resized to the winner. Read-only
viewers watch without voting. The phone never uses this module.
"""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Callable

from corral import embed

HISTORY_LINES = 1500
_COALESCE_SECONDS = 0.012
_TICK_SECONDS = 1.0
_MAX_EVENT_BYTES = 256 * 1024
# A flusher that cannot keep up drops the backlog and resnapshots instead.
_MAX_PENDING_BYTES = 8 * 1024 * 1024

_STATE_FORMAT = "|".join(
    (
        "#{pane_width}", "#{pane_height}", "#{cursor_x}", "#{cursor_y}",
        "#{cursor_flag}", "#{alternate_on}", "#{keypad_cursor_flag}",
        "#{keypad_flag}", "#{mouse_standard_flag}", "#{mouse_button_flag}",
        "#{mouse_all_flag}", "#{mouse_sgr_flag}", "#{mouse_utf8_flag}",
        "#{insert_flag}", "#{wrap_flag}", "#{scroll_region_upper}",
        "#{scroll_region_lower}", "#{cursor_shape}", "#{cursor_blinking}",
    )
)


class PaneState:
    """Parsed `_STATE_FORMAT`; only what a fresh emulator needs to match tmux."""

    __slots__ = (
        "cols", "rows", "cursor_x", "cursor_y", "cursor_visible", "alternate",
        "app_cursor", "app_keypad", "mouse_standard", "mouse_button",
        "mouse_all", "mouse_sgr", "mouse_utf8", "insert", "wrap",
        "region_upper", "region_lower", "cursor_shape", "cursor_blinking",
    )

    def __init__(self, line: str) -> None:
        parts = line.strip().split("|")
        if len(parts) != 19:
            raise ValueError("unexpected pane state")
        ints = [int(p) if p.lstrip("-").isdigit() else 0 for p in parts[:17]]
        (self.cols, self.rows, self.cursor_x, self.cursor_y) = ints[:4]
        flags = [v == 1 for v in ints[4:15]]
        (self.cursor_visible, self.alternate, self.app_cursor, self.app_keypad,
         self.mouse_standard, self.mouse_button, self.mouse_all, self.mouse_sgr,
         self.mouse_utf8, self.insert, self.wrap) = flags
        self.region_upper, self.region_lower = ints[15], ints[16]
        self.cursor_shape = parts[17]
        self.cursor_blinking = parts[18] == "1"

    def modes(self) -> bytes:
        """Escape sequences that put a reset emulator into this pane's modes."""
        out: list[str] = []
        if self.rows > 0 and (self.region_upper, self.region_lower) != (0, self.rows - 1):
            out.append(f"\x1b[{self.region_upper + 1};{self.region_lower + 1}r")
        out.append(f"\x1b[{self.cursor_y + 1};{self.cursor_x + 1}H")
        if not self.cursor_visible:
            out.append("\x1b[?25l")
        if self.app_cursor:
            out.append("\x1b[?1h")
        if self.app_keypad:
            out.append("\x1b=")
        if self.insert:
            out.append("\x1b[4h")
        if not self.wrap:
            out.append("\x1b[?7l")
        for enabled, mode in (
            (self.mouse_standard, 1000), (self.mouse_button, 1002), (self.mouse_all, 1003),
            (self.mouse_utf8, 1005), (self.mouse_sgr, 1006),
        ):
            if enabled:
                out.append(f"\x1b[?{mode}h")
        shape = {"block": 1, "underline": 3, "bar": 5}.get(self.cursor_shape)
        if shape is not None:
            out.append(f"\x1b[{shape if self.cursor_blinking else shape + 1} q")
        return "".join(out).encode()


def build_snapshot(state: PaneState, main_lines: list[str], alt_lines: list[str] | None) -> bytes:
    """Bytes that redraw the captured pane on a freshly reset emulator.

    ``main_lines`` are history plus the visible normal screen (capture-pane
    keeps blank lines, so the last ``rows`` lines are the screen). With the
    alternate screen on, ``main_lines`` is the saved normal screen and
    ``alt_lines`` what is visible.
    """
    reset_line = "\x1b[0m\r\n"
    parts = ["\x1b[0m\x1b[H\x1b[2J", reset_line.join(main_lines), "\x1b[0m"]
    if alt_lines is not None:
        parts += ["\x1b[?1049h\x1b[H\x1b[2J", reset_line.join(alt_lines), "\x1b[0m"]
    data = "".join(parts).encode("utf-8", "replace")
    return data + state.modes()


class TerminalStream:
    """One hosted pane streamed to its desktop viewers.

    ``emit(payload)`` publishes on the session's ``term:`` channel.
    ``resolve_name()`` returns the current tmux name (empty when the session is
    no longer hosted), so a native restart rebinds the stream.
    """

    def __init__(self, key: str, name: str, emit: Callable[[dict], None],
                 resolve_name: Callable[[], str]) -> None:
        self.key = key
        self.name = name
        self._emit = emit
        self._resolve_name = resolve_name
        self._buf_lock = threading.Lock()
        self._pending: list[tuple[int, bytes]] = []
        self._pending_bytes = 0
        self._barrier = 0
        self._snap_lock = threading.Lock()
        self._seq = 0
        self._votes: dict[str, tuple[int, int]] = {}
        self._votes_lock = threading.Lock()
        self._size: tuple[int, int] = (0, 0)
        self._channel: embed.ControlChannel | None = None
        self._ended = False
        self._need_snapshot = threading.Event()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"terminal-stream-{key[:16]}")

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        self._need_snapshot.set()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._votes_lock:
            viewers = list(self._votes)
            self._votes.clear()
        for viewer in viewers:
            embed.release_host_view(self.name, viewer)
        channel = self._channel
        if channel is not None and channel.on_data == self._on_data:
            channel.on_data = None
            # Nothing else in this process needs the control client once the
            # last desktop viewer left; do not keep a tmux client per session.
            embed.close_channel(self.name)

    # -- viewers -------------------------------------------------------

    def vote(self, viewer: str, cols: int, rows: int) -> tuple[int, int]:
        """Record a desktop viewer's fitting size; resize the pane to the winner."""
        cols, rows = max(1, int(cols)), max(1, int(rows))
        with self._votes_lock:
            self._votes[viewer] = (cols, rows)
        effective = embed.desired_host_size(self.name, viewer, cols, rows)
        self._apply_size(effective)
        return effective

    def withdraw(self, viewer: str) -> None:
        with self._votes_lock:
            had = self._votes.pop(viewer, None) is not None
        if had:
            embed.release_host_view(self.name, viewer)

    def request_snapshot(self) -> None:
        self._need_snapshot.set()
        self._wake.set()

    def send_input(self, data: bytes) -> bool:
        return embed.send_bytes(self.name, data)

    # -- worker --------------------------------------------------------

    def _run(self) -> None:
        from corral.schedprio import boost_ui_worker

        boost_ui_worker()
        next_tick = 0.0
        while not self._stop.is_set():
            self._wake.wait(_TICK_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                now = time.monotonic()
                if now >= next_tick:
                    next_tick = now + _TICK_SECONDS
                    self._tick()
                if self._need_snapshot.is_set():
                    self._need_snapshot.clear()
                    self._snapshot()
                else:
                    time.sleep(_COALESCE_SECONDS)
                    self._flush()
            except Exception:
                continue

    def _tick(self) -> None:
        """Keep votes fresh, follow resizes by other viewers, rebind on restart."""
        channel = self._live_channel()
        if channel is None:
            return
        with self._votes_lock:
            votes = dict(self._votes)
        effective = None
        for viewer, (cols, rows) in votes.items():
            effective = embed.desired_host_size(self.name, viewer, cols, rows)
        if effective is not None:
            self._apply_size(effective)
        result = channel.request("display-message", "-p", "-t", self.name, "#{pane_width}|#{pane_height}")
        if result:
            try:
                width, height = (int(v) for v in result[0].split("|"))
            except ValueError:
                return
            if (width, height) != self._size:
                self._need_snapshot.set()

    def _apply_size(self, effective: tuple[int, int]) -> None:
        if effective == self._size or not embed.should_resize_host(*effective):
            return
        embed.resize(self.name, effective[0], effective[1])
        self.request_snapshot()

    def _live_channel(self) -> embed.ControlChannel | None:
        channel = self._channel
        if channel is not None and not channel.dead and embed.active_channel(self.name) is channel:
            return channel
        name = self._resolve_name()
        if not name:
            self._end()
            return None
        if name != self.name:
            with self._votes_lock:
                votes = dict(self._votes)
            for viewer in votes:
                embed.release_host_view(self.name, viewer)
            self.name = name
            for viewer, (cols, rows) in votes.items():
                embed.desired_host_size(name, viewer, cols, rows)
        channel = embed.open_channel(name, on_data=self._on_data)
        if channel is None:
            self._end()
            return None
        self._channel = channel
        self._ended = False
        self._need_snapshot.set()
        return channel

    def _end(self) -> None:
        if self._ended:
            return
        self._ended = True
        with self._snap_lock:
            self._seq += 1
            self._emit({"kind": "ended", "seq": self._seq})

    def _on_data(self, _pane: str, data: bytes, seq: int) -> None:
        with self._buf_lock:
            self._pending.append((seq, data))
            self._pending_bytes += len(data)
            overflow = self._pending_bytes > _MAX_PENDING_BYTES
            if overflow:
                self._pending.clear()
                self._pending_bytes = 0
        if overflow:
            self._need_snapshot.set()
        self._wake.set()

    def _flush(self) -> None:
        with self._snap_lock:
            with self._buf_lock:
                chunks = [data for seq, data in self._pending if seq > self._barrier]
                self._pending.clear()
                self._pending_bytes = 0
            if not chunks:
                return
            blob = b"".join(chunks)
            for start in range(0, len(blob), _MAX_EVENT_BYTES):
                self._seq += 1
                self._emit({
                    "kind": "output",
                    "seq": self._seq,
                    "data": base64.b64encode(blob[start:start + _MAX_EVENT_BYTES]).decode("ascii"),
                })

    def _snapshot(self) -> None:
        channel = self._live_channel()
        if channel is None:
            return
        with self._snap_lock:
            captured = self._capture(channel)
            if captured is None:
                self._need_snapshot.set()
                return
            state, main, alt, barrier = captured
            with self._buf_lock:
                kept = [(seq, data) for seq, data in self._pending if seq > barrier]
                self._pending = kept
                self._pending_bytes = sum(len(data) for _, data in kept)
                self._barrier = barrier
            self._size = (state.cols, state.rows)
            self._seq += 1
            self._emit({
                "kind": "snapshot",
                "seq": self._seq,
                "cols": state.cols,
                "rows": state.rows,
                "data": base64.b64encode(build_snapshot(state, main, alt)).decode("ascii"),
            })
        self._wake.set()

    def _capture(self, channel: embed.ControlChannel):
        """State + screen with no output in between (retried), plus its barrier."""
        name = self.name
        for _attempt in range(4):
            before = channel.request_ordered("display-message", "-p", "-t", name, _STATE_FORMAT)
            if before is None or not before[0]:
                return None
            try:
                state = PaneState(before[0][0])
            except ValueError:
                return None
            if state.alternate:
                main = channel.request_ordered("capture-pane", "-p", "-e", "-a", "-q", "-t", name)
                alt = channel.request_ordered("capture-pane", "-p", "-e", "-t", name)
            else:
                main = channel.request_ordered(
                    "capture-pane", "-p", "-e", "-S", f"-{HISTORY_LINES}", "-t", name,
                )
                alt = None
            after = channel.request_ordered("display-message", "-p", "-t", name, _STATE_FORMAT)
            if main is None or after is None or (state.alternate and alt is None):
                return None
            if after[1] == before[1] and after[0] == before[0]:
                return state, main[0], (alt[0] if alt is not None else None), after[1]
        return None
