"""Watch assistant history roots and wake session refresh on change.

Idle TUI / remote loops used to re-scan every few seconds even when nothing
changed. This module turns "something may have changed" into an event so the
refresh loop can sleep until FSEvents/inotify fire, with a slow reconcile
timeout as a safety net.

No third-party dependency: Darwin uses CoreServices FSEvents via ctypes,
Linux uses inotify via ctypes, other platforms fall back to reconcile-only
(no early wake). Failures never block startup — callers treat a dead watcher
as "always rely on the reconcile timeout".
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

# Coalesce bursty writes (Cursor WAL / JSONL appends) into one wake.
DEFAULT_DEBOUNCE_SECONDS = 0.35
# How long the CFRunLoop / inotify thread may block before checking stop.
_LOOP_SLICE_SECONDS = 0.5


def pressure_cadence(
    normal_reconcile: float, normal_min_gap: float, pressured: bool,
) -> tuple[float, float]:
    """Return the (reconcile, min_gap) cadence for a refresh loop.

    Under memory pressure (see ``reclaim.memory_pressure``) polling floors move
    out — reconcile triples (at least 60 s) and the minimum scan gap quadruples —
    so cold rescans stop compounding swap thrash. FS-event wakes are preserved
    by the callers; only the polling floor moves, and the normal cadence
    (``REFRESH_MIN_GAP`` et al.) is never changed globally.
    """
    if not pressured:
        return normal_reconcile, normal_min_gap
    return max(float(normal_reconcile) * 3.0, 60.0), max(float(normal_min_gap) * 4.0, 1.0)


def memory_pressured() -> bool:
    """Best-effort memory-pressure probe for refresh loops (never raises)."""
    try:
        from corral import reclaim

        return bool(reclaim.memory_pressure())
    except Exception:  # noqa: BLE001 — probe failure means normal cadence
        return False


def _history_watch_enabled() -> bool:
    """Unit tests isolate managed hosts / cache; do not watch the developer's real dirs."""
    if os.environ.get("CORRAL_ISOLATE_MANAGED_HOSTS") == "1":
        return False
    if os.environ.get("CORRAL_CACHE", "1").strip() == "0":
        return False
    return True


def default_history_roots() -> list[Path]:
    """Known on-disk history locations for the shipped assistants.

    Missing paths are skipped; callers may pass an explicit list in tests.
    """
    if not _history_watch_enabled():
        return []
    home = Path.home()
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    share = Path(xdg).expanduser() if xdg else home / ".local" / "share"
    candidates = [
        home / ".claude" / "projects",
        home / ".claude" / "sessions",
        home / ".codex" / "sessions",
        home / ".cursor" / "chats",
        home / ".kimi-code",
        home / ".pi" / "agent",
        share / "opencode",
    ]
    # OPENCODE_DATA_DIR may be a comma-separated override.
    for part in (os.environ.get("OPENCODE_DATA_DIR", "") or "").split(","):
        part = part.strip()
        if part:
            candidates.append(Path(part).expanduser())
    seen: set[str] = set()
    roots: list[Path] = []
    for path in candidates:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen or not resolved.is_dir():
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


class HistoryWatcher:
    """Background watcher: ``wait`` returns True when history may have changed."""

    def __init__(
        self,
        roots: Iterable[Path | str] | None = None,
        *,
        debounce: float = DEFAULT_DEBOUNCE_SECONDS,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._roots = [Path(p) for p in (roots if roots is not None else default_history_roots())]
        self._debounce = max(0.05, float(debounce))
        self._on_change = on_change
        self._changed = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backend = "none"
        self._last_fire = 0.0
        self._lock = threading.Lock()

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(self._roots)

    def start(self) -> None:
        if self._thread is not None or not self._roots:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="corral-history-watch",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        self._changed.set()  # unblock waiters
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def clear(self) -> None:
        self._changed.clear()

    def is_set(self) -> bool:
        return self._changed.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until a change is signaled or ``timeout`` elapses.

        Returns True when a change woke the wait (caller should refresh).
        """
        return self._changed.wait(timeout)

    def _signal(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_fire < self._debounce:
                return
            self._last_fire = now
        self._changed.set()
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:  # noqa: BLE001 — watcher must not die on callback bugs
                pass

    def _run(self) -> None:
        if sys.platform == "darwin":
            if self._run_fsevents():
                return
        elif sys.platform.startswith("linux"):
            if self._run_inotify():
                return
        self._backend = "none"
        # No native backend: sit until stop. Callers still reconcile on timeout.
        self._stop.wait()

    def _run_fsevents(self) -> bool:
        try:
            import ctypes
            import ctypes.util
        except Exception:
            return False
        try:
            core = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
            services = ctypes.CDLL(ctypes.util.find_library("CoreServices"))
        except Exception:
            return False

        # CF types we need as opaque pointers.
        CFIndex = ctypes.c_long
        CFStringRef = ctypes.c_void_p
        CFArrayRef = ctypes.c_void_p
        CFAllocatorRef = ctypes.c_void_p
        CFTimeInterval = ctypes.c_double
        FSEventStreamRef = ctypes.c_void_p
        CFRunLoopRef = ctypes.c_void_p

        kCFStringEncodingUTF8 = 0x08000100
        kFSEventStreamCreateFlagFileEvents = 0x00000010
        kFSEventStreamCreateFlagNoDefer = 0x00000002
        kCFRunLoopDefaultMode = ctypes.c_void_p.in_dll(core, "kCFRunLoopDefaultMode")

        core.CFStringCreateWithCString.restype = CFStringRef
        core.CFStringCreateWithCString.argtypes = [CFAllocatorRef, ctypes.c_char_p, ctypes.c_uint32]
        core.CFArrayCreate.restype = CFArrayRef
        core.CFArrayCreate.argtypes = [
            CFAllocatorRef,
            ctypes.POINTER(ctypes.c_void_p),
            CFIndex,
            ctypes.c_void_p,
        ]
        core.CFRelease.argtypes = [ctypes.c_void_p]
        core.CFRunLoopGetCurrent.restype = CFRunLoopRef
        core.CFRunLoopRunInMode.restype = ctypes.c_int32
        core.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, CFTimeInterval, ctypes.c_bool]
        core.CFRunLoopStop.argtypes = [CFRunLoopRef]

        Callback = ctypes.CFUNCTYPE(
            None,
            FSEventStreamRef,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint64),
        )

        @Callback
        def _callback(stream, info, num_events, event_paths, event_flags, event_ids):  # noqa: ARG001
            if num_events:
                self._signal()

        self._fsevents_callback = _callback  # keep alive

        cf_strings = []
        values = (ctypes.c_void_p * len(self._roots))()
        for index, root in enumerate(self._roots):
            cf_str = core.CFStringCreateWithCString(
                None, str(root).encode("utf-8"), kCFStringEncodingUTF8,
            )
            if not cf_str:
                return False
            cf_strings.append(cf_str)
            values[index] = cf_str
        # kCFTypeArrayCallBacks
        callbacks = ctypes.c_void_p.in_dll(core, "kCFTypeArrayCallBacks")
        cf_array = core.CFArrayCreate(None, values, len(self._roots), callbacks)
        if not cf_array:
            for item in cf_strings:
                core.CFRelease(item)
            return False

        services.FSEventStreamCreate.restype = FSEventStreamRef
        services.FSEventStreamCreate.argtypes = [
            CFAllocatorRef,
            Callback,
            ctypes.c_void_p,
            CFArrayRef,
            ctypes.c_uint64,  # sinceWhen: kFSEventStreamEventIdSinceNow
            CFTimeInterval,
            ctypes.c_uint32,
        ]
        kFSEventStreamEventIdSinceNow = 0xFFFFFFFFFFFFFFFF
        stream = services.FSEventStreamCreate(
            None,
            _callback,
            None,
            cf_array,
            kFSEventStreamEventIdSinceNow,
            self._debounce,
            kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagNoDefer,
        )
        core.CFRelease(cf_array)
        for item in cf_strings:
            core.CFRelease(item)
        if not stream:
            return False

        services.FSEventStreamScheduleWithRunLoop.argtypes = [
            FSEventStreamRef, CFRunLoopRef, ctypes.c_void_p,
        ]
        services.FSEventStreamStart.argtypes = [FSEventStreamRef]
        services.FSEventStreamStop.argtypes = [FSEventStreamRef]
        services.FSEventStreamInvalidate.argtypes = [FSEventStreamRef]
        services.FSEventStreamRelease.argtypes = [FSEventStreamRef]

        run_loop = core.CFRunLoopGetCurrent()
        services.FSEventStreamScheduleWithRunLoop(stream, run_loop, kCFRunLoopDefaultMode)
        if not services.FSEventStreamStart(stream):
            services.FSEventStreamInvalidate(stream)
            services.FSEventStreamRelease(stream)
            return False

        self._backend = "fsevents"
        try:
            while not self._stop.is_set():
                # 0 = finished, 1 = stopped, 2 = timed out
                core.CFRunLoopRunInMode(kCFRunLoopDefaultMode, _LOOP_SLICE_SECONDS, False)
        finally:
            services.FSEventStreamStop(stream)
            services.FSEventStreamInvalidate(stream)
            services.FSEventStreamRelease(stream)
        return True

    def _run_inotify(self) -> bool:
        try:
            import ctypes
            import ctypes.util
            import select
            from ctypes import c_char, c_int, c_uint32
        except Exception:
            return False

        libc_name = ctypes.util.find_library("c")
        if not libc_name:
            return False
        libc = ctypes.CDLL(libc_name, use_errno=True)
        libc.inotify_init1.restype = c_int
        libc.inotify_add_watch.restype = c_int
        libc.inotify_add_watch.argtypes = [c_int, ctypes.c_char_p, c_uint32]
        libc.read.restype = ctypes.c_ssize_t

        IN_NONBLOCK = 0x800
        IN_CLOEXEC = 0x80000
        IN_CREATE = 0x00000100
        IN_DELETE = 0x00000200
        IN_MODIFY = 0x00000002
        IN_MOVED_FROM = 0x00000040
        IN_MOVED_TO = 0x00000080
        IN_ATTRIB = 0x00000004
        mask = IN_CREATE | IN_DELETE | IN_MODIFY | IN_MOVED_FROM | IN_MOVED_TO | IN_ATTRIB

        fd = libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            return False

        def add_tree(path: Path, depth: int = 0) -> int:
            count = 0
            try:
                wd = libc.inotify_add_watch(fd, str(path).encode("utf-8"), c_uint32(mask))
            except Exception:
                return 0
            if wd < 0:
                return 0
            count += 1
            if depth >= 4:
                return count
            try:
                children = list(path.iterdir())
            except OSError:
                return count
            for child in children:
                if child.is_dir():
                    count += add_tree(child, depth + 1)
            return count

        watches = 0
        for root in self._roots:
            watches += add_tree(root)
        if watches == 0:
            os.close(fd)
            return False

        self._backend = "inotify"
        buf = (c_char * 65536)()
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([fd], [], [], _LOOP_SLICE_SECONDS)
                if not ready:
                    continue
                while True:
                    n = libc.read(fd, buf, len(buf))
                    if n <= 0:
                        break
                    self._signal()
        finally:
            os.close(fd)
        return True
