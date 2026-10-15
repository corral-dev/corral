"""本地 TUI 可观测性：结构化事件写到 ~/.cache/corral/events.log。

TUI 占终端时 stderr 不可见，诊断只能落盘。默认只记低基数事件；
CORRAL_DEBUG=1 / CORRAL_LOG=debug 或 init(debug=True) 才写 debug 级。
写失败必须吞掉，不能拖死抓帧/重扫线程。
致命闪退（sys.excepthook / 线程未捕获 / TUI _handle_exception）同样双写，
以便事后用 corral diagnose 的 last_error 取回完整栈。
"""

from __future__ import annotations

import faulthandler
import json
import os
import re
import signal
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from corral.legacy_names import cache_dir as product_cache_dir
from corral.legacy_names import getenv

CACHE_DIR = str(product_cache_dir())
EVENTS_LOG = os.path.join(CACHE_DIR, "events.log")
EMBED_ERROR_LOG = os.path.join(CACHE_DIR, "embed-error.log")
STACKS_LOG = os.path.join(CACHE_DIR, "stacks.log")
_MAX_LOG_BYTES = 256 * 1024
# 按需栈转储的容量上限：单次转储数十 KB，1MB 约容纳几十次现场；超限轮转一代。
_STACKS_LOG_MAX_BYTES = 1 * 1024 * 1024

_REDACT_KEYS = frozenset({
    "text", "prompt", "messages", "message", "content", "body",
    "argv", "command", "token", "api_key", "password", "secret",
})

# embed-error.log 每条以「ISO 时间 [where] ExcType: msg」开头。
_ERROR_HEADER_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}) \[([^\]]+)\] ([^:\n]+): (.*)$",
    re.MULTILINE,
)

_lock = threading.Lock()
_stacks_write_lock = threading.Lock()
_debug = False
_inited = False
_hooks_installed = False
_stacks_installed = False
_stacks_file = None
_stacks_previous_handler = None


def reset_for_tests() -> None:
    """单测专用：清掉进程内开关状态。不卸载已安装的 crash hook（hook 读模块全局路径）。

    按需栈转储的 SIGUSR1 处理器会被复原（避免跨用例互相发信号），已打开的
    转储文件会被关闭；下次 install_stack_dumps() 会重新打开。
    """
    global _debug, _inited, _stacks_installed, _stacks_file, _stacks_previous_handler
    with _lock:
        _debug = False
        _inited = False
        was_stacks = _stacks_installed
        _stacks_installed = False
    if was_stacks:
        signum = getattr(signal, "SIGUSR1", None)
        if signum is not None:
            try:
                previous = _stacks_previous_handler
                signal.signal(
                    signum,
                    previous if previous is not None else signal.SIG_DFL,
                )
            except (OSError, ValueError, RuntimeError):
                pass
        _stacks_previous_handler = None
    fh = _stacks_file
    _stacks_file = None
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass


def init(*, debug: bool | None = None) -> None:
    """启动时调用一次。debug=None 时读 CORRAL_DEBUG / CORRAL_LOG=debug。"""
    global _debug, _inited
    if debug is None:
        env_debug = (getenv("DEBUG") or "").strip() not in ("", "0", "false", "False")
        env_log = (getenv("LOG") or "").strip().lower() == "debug"
        debug = env_debug or env_log
    with _lock:
        _debug = bool(debug)
        _inited = True


def _ensure_inited() -> None:
    if not _inited:
        init()


def _sanitize(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if key in _REDACT_KEYS or key.lower() in _REDACT_KEYS:
            out[key] = "<redacted>"
        else:
            out[key] = value
    return out


def _rotate_if_needed(path: str, incoming_bytes: int) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) + incoming_bytes > _MAX_LOG_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass


def _write_line(path: str, payload: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        _rotate_if_needed(path, len(line.encode("utf-8")))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def event(name: str, **fields: Any) -> None:
    """默认 info 级结构化事件。"""
    _ensure_inited()
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "level": "info",
        "name": name,
        **_sanitize(fields),
        "pid": os.getpid(),
    }
    with _lock:
        _write_line(EVENTS_LOG, payload)


def debug(name: str, **fields: Any) -> None:
    """仅 debug 开启时写入。"""
    _ensure_inited()
    with _lock:
        if not _debug:
            return
        payload = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "level": "debug",
            "name": name,
            **_sanitize(fields),
            "pid": os.getpid(),
        }
        _write_line(EVENTS_LOG, payload)


@contextmanager
def timed(name: str, **fields: Any) -> Iterator[None]:
    """结束时写带 duration_ms 的 event。"""
    start = time.perf_counter()
    try:
        yield
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        event(name, duration_ms=duration_ms, **fields)


def _unwrap_exception(exc: BaseException) -> tuple[BaseException, BaseException | None]:
    """展开包装异常，返回 (根因, 外层包装或 None)。

    优先跟 Textual ``WorkerFailed.error``，再跟 ``__cause__`` / ``__context__``。
    根因与外层相同时第二项为 None。
    """
    seen: set[int] = set()
    root = exc
    while id(root) not in seen:
        seen.add(id(root))
        inner = getattr(root, "error", None)
        if isinstance(inner, BaseException):
            root = inner
            continue
        if root.__cause__ is not None:
            root = root.__cause__
            continue
        if root.__context__ is not None and not root.__suppress_context__:
            root = root.__context__
            continue
        break
    return root, (None if root is exc else exc)


def _format_exception_block(exc: BaseException) -> str:
    if exc.__traceback__ is not None:
        return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return f"{type(exc).__name__}: {exc}\n"


def log_exception(where: str, exc: BaseException) -> None:
    """异常：events.log 一条无 traceback 的 error；embed-error.log 留完整栈。

    Textual WorkerFailed 等包装异常会展开到根因再落盘，避免 diagnose 只看到
    「Worker raised exception: NameError(...)」短消息而丢掉真实栈。
    """
    _ensure_inited()
    root, wrapper = _unwrap_exception(exc)
    fields: dict[str, Any] = {
        "where": where,
        "exc_type": type(root).__name__,
        "exc_msg": str(root)[:200],
    }
    if wrapper is not None:
        fields["via"] = type(wrapper).__name__
    event("error", **fields)
    try:
        os.makedirs(os.path.dirname(EMBED_ERROR_LOG) or ".", exist_ok=True)
        header = (
            f"{datetime.now().isoformat(timespec='seconds')} [{where}] "
            f"{type(root).__name__}: {root}"
        )
        if wrapper is not None:
            header += f" (via {type(wrapper).__name__})"
        tb = _format_exception_block(root)
        if wrapper is not None and wrapper is not root:
            # 外层包装本身往往没有有用栈；补一行说明即可。根因栈才是定位关键。
            tb = f"via {type(wrapper).__name__}: {wrapper}\n{tb}"
        block = f"{header}\n{tb}\n"
        _rotate_if_needed(EMBED_ERROR_LOG, len(block.encode("utf-8")))
        with open(EMBED_ERROR_LOG, "a", encoding="utf-8") as fh:
            fh.write(block)
    except OSError:
        pass


def read_last_error() -> dict[str, str] | None:
    """解析 embed-error.log 末条记录；无文件或无法解析时返回 None。

    返回字段：ts / where / exc_type / exc_msg / traceback（整段原文，含头行与栈）。
    """
    try:
        for path in (EMBED_ERROR_LOG, EMBED_ERROR_LOG + ".1"):
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
            matches = list(_ERROR_HEADER_RE.finditer(body))
            if matches:
                last = matches[-1]
                chunk = body[last.start():].strip()
                return {
                    "ts": last.group(1),
                    "where": last.group(2),
                    "exc_type": last.group(3),
                    "exc_msg": last.group(4),
                    "traceback": chunk,
                }
    except OSError:
        return None
    return None


def install_crash_hooks() -> None:
    """安装进程级未捕获异常钩子，把致命闪退写入 embed-error.log。

    幂等；不拦截 KeyboardInterrupt / SystemExit。Textual 路径另由
    CorralApp._handle_exception 在退出前调用 log_exception。
    """
    global _hooks_installed
    with _lock:
        if _hooks_installed:
            return
        _hooks_installed = True

    previous = sys.excepthook

    def _excepthook(exc_type, exc, tb) -> None:  # noqa: ANN001
        if exc_type is not None and issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            previous(exc_type, exc, tb)
            return
        try:
            if exc is not None and tb is not None and getattr(exc, "__traceback__", None) is None:
                exc = exc.with_traceback(tb)
            if exc is not None:
                log_exception("进程未捕获异常", exc)
        except Exception:
            pass
        previous(exc_type, exc, tb)

    sys.excepthook = _excepthook

    if hasattr(threading, "excepthook"):
        previous_thread = threading.excepthook

        def _thread_excepthook(args) -> None:  # noqa: ANN001
            exc_type = args.exc_type
            if exc_type is not None and issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
                previous_thread(args)
                return
            try:
                exc = args.exc_value
                tb = args.exc_traceback
                if exc is not None and tb is not None and getattr(exc, "__traceback__", None) is None:
                    exc = exc.with_traceback(tb)
                thread_name = getattr(args.thread, "name", "?")
                if exc is not None:
                    log_exception(f"线程未捕获异常({thread_name})", exc)
            except Exception:
                pass
            previous_thread(args)

        threading.excepthook = _thread_excepthook


def _stacks_handler(signum, frame) -> None:  # noqa: ANN001
    """SIGUSR1 处理器：头行（时间 + pid）后追加全线程栈。

    只写转储专用文件（平时无其它写者），失败一律吞掉：诊断能力不得
    影响被观测进程。轮转在处理器内就地做（关→改名→下次写时重开）。
    """
    _dump_stacks("signal")


def _dump_stacks(reason: str) -> bool:
    """Write one bounded all-thread sample without delaying the observed UI."""
    global _stacks_file
    if not _stacks_write_lock.acquire(blocking=False):
        return False
    try:
        path = STACKS_LOG
        try:
            oversized = os.path.getsize(path) > _STACKS_LOG_MAX_BYTES
        except OSError:
            oversized = False
        if oversized:
            fh = _stacks_file
            _stacks_file = None
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
            try:
                os.replace(path, path + ".1")
            except OSError:
                pass
        if _stacks_file is None:
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            except OSError:
                pass
            _stacks_file = open(path, "a", encoding="utf-8", buffering=1)
        _stacks_file.write(
            f"\n=== stacks {datetime.now().isoformat(timespec='seconds')} "
            f"pid={os.getpid()} reason={reason} ===\n"
        )
        _stacks_file.flush()
        faulthandler.dump_traceback(file=_stacks_file, all_threads=True)
        _stacks_file.flush()
        return True
    except Exception:
        return False
    finally:
        _stacks_write_lock.release()


def install_stack_dumps() -> bool:
    """安装按需栈转储：活进程收到 SIGUSR1 时把全线程栈追加到 stacks.log。

    用自有处理器调 ``faulthandler.dump_traceback`` 而不用
    ``faulthandler.register``：后者无法在每次转储前写入时间 + pid 头行。
    幂等；必须在主线程调用（非主线程时 signal.signal 会抛 ValueError，
    此时返回 False）。无 SIGUSR1 的平台直接返回 False。
    """
    global _stacks_installed, _stacks_previous_handler
    signum = getattr(signal, "SIGUSR1", None)
    if signum is None:
        return False
    with _lock:
        if _stacks_installed:
            return True
        _stacks_installed = True
    try:
        _stacks_previous_handler = signal.getsignal(signum)
        signal.signal(signum, _stacks_handler)
    except (OSError, ValueError, RuntimeError):
        with _lock:
            _stacks_installed = False
        return False
    return True


class TuiStallWatchdog:
    """Probe the Textual event loop from a daemon thread; sample long stalls once."""

    def __init__(self, loop: Any, *, interval: float = 0.5, threshold: float = 3.0) -> None:
        self._loop = loop
        self._interval = interval
        self._threshold = threshold
        self._stop = threading.Event()
        self._state_lock = threading.Lock()
        self._pending_at: float | None = None
        self._reported = False
        self._thread = threading.Thread(target=self._run, name="corral-tui-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=0.2)

    def _ack(self, sent_at: float) -> None:
        """Runs on the UI loop; clears the outstanding probe without blocking."""
        now = time.monotonic()
        with self._state_lock:
            if self._stop.is_set() or self._pending_at != sent_at:
                return
            reported = self._reported
            self._pending_at = None
        elapsed = now - sent_at
        if elapsed >= self._threshold:
            event(
                "tui_stall_recovered", stall_id=int(sent_at * 1_000_000_000),
                duration_ms=int(elapsed * 1000), stack_attempted=reported,
            )

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            with self._state_lock:
                sent_at = self._pending_at
                new_probe = sent_at is None
                should_sample = (
                    sent_at is not None
                    and not self._reported
                    and time.monotonic() - sent_at >= self._threshold
                )
                if should_sample:
                    self._reported = True
                if new_probe:
                    sent_at = time.monotonic()
                    self._pending_at = sent_at
                    self._reported = False
            if should_sample:
                captured = _dump_stacks("tui_stall")
                event(
                    "tui_stall", stall_id=int(sent_at * 1_000_000_000),
                    elapsed_ms=int((time.monotonic() - sent_at) * 1000),
                    stack_captured=captured,
                )
            elif new_probe:
                try:
                    self._loop.call_soon_threadsafe(self._ack, sent_at)
                except RuntimeError:
                    self._stop.set()


SCREENSHOTS_DIR_NAME = "screenshots"


def save_tui_screenshot(app) -> str:
    """把当前 Textual App 导出到 ~/.cache/corral/screenshots/；返回 SVG 路径。

    用户主动触发=知情同意；勿把含真实对话的截图提交进仓库。
    SVG 真彩色可能被压成灰阶，配色验收仍以真机为准。
    """
    from datetime import datetime as _dt

    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(CACHE_DIR, SCREENSHOTS_DIR_NAME)
    os.makedirs(out_dir, exist_ok=True)
    filename = f"tui-{stamp}.svg"
    path = app.save_screenshot(filename, path=out_dir)
    event("screenshot", path=str(path), format="svg")
    return str(path)


def log_embed_error(where: str, exc: BaseException) -> None:
    """内嵌后台线程的异常记录：TUI 接管终端期间 stderr 不可见，写文件留证。

    events.log 一条结构化 error，embed-error.log 留 traceback。
    """
    log_exception(where, exc)


# 兼容旧名
_log_embed_error = log_embed_error

