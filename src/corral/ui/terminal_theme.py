"""运行中终端主题变化监听。

Textual 目前只负责应用自身主题切换，不会把终端返回的 OSC 11 背景色或
DEC 2031 深浅色通知转成事件。本模块在 Unix 终端驱动的输入解析入口补一层：
保留 Textual 原有解析行为，同时把这两类应答变成应用消息。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable

from textual import events
from textual.message import Message

from corral.ui.pointer_shape import (
    enable_tmux_passthrough,
    reset_sequence,
    restore_tmux_passthrough,
    sequence,
)

_OSC_BACKGROUND_MARKER = "\x1b]11;rgb:"
_MODE_MARKER = "\x1b[?997;"
_OSC_BACKGROUND_RE = re.compile(
    r"\x1b\]11;rgb:[0-9a-fA-F]+/[0-9a-fA-F]+/[0-9a-fA-F]+(?:\x07|\x1b\\)"
)
_MODE_RE = re.compile(r"\x1b\[\?997;([12])n")
_THEME_MARKERS = (_OSC_BACKGROUND_MARKER, _MODE_MARKER)

# iTerm2 不支持 DEC 2031 主动通知，只能定期查询 OSC 11。两秒足够贴近日落切换，
# 同时查询只是很短的终端控制序列，不起进程、不访问网络。
BACKGROUND_POLL_INTERVAL = 2.0

# 未确认的主题前缀最多扣这么久（秒）：真实的应答拆包会在下一个驱动 tick
#（约 0.1s，且余下字节早已在 pty 缓冲里）内补齐；超过这个窗龄还没等到余下
# 字节，基本可以断定它是用户的独立按键（首当其冲就是单独的 Esc 键），
# 必须放行给原解析器/直接生成 Esc 键，否则真实终端上 Esc 要等下一个按键
# 才生效（清空搜索/退出都得按两次；Pilot 注入按键绕过字节解析，测不出来）。
_PENDING_FLUSH_AGE = 0.05


class TerminalBackgroundReport(Message):
    """终端报告了新的背景色或深浅模式。"""

    def __init__(
        self,
        *,
        osc_report: bytes | None = None,
        is_light: bool | None = None,
    ) -> None:
        super().__init__()
        self.osc_report = osc_report
        self.is_light = is_light


if os.name != "nt":
    from textual._xterm_parser import XTermParser
    from textual.drivers import linux_driver
    from textual.drivers.linux_driver import LinuxDriver

    class TerminalThemeParser(XTermParser):
        """从 Textual 输入流中提取主题应答，其余输入原样交回框架。"""

        def __init__(self, debug: bool = False) -> None:
            super().__init__(debug)
            self._theme_pending = ""
            self._theme_pending_at = 0.0
            # 已确认是主题应答开头的暂存（如 \x1b]11;rgb: 缺终结符）不参与超时
            # 放行：那不是用户能敲出来的输入，等下一次读取补齐即可。
            self._theme_pending_confirmed = False

        @staticmethod
        def _trailing_marker_prefix(data: str) -> str:
            """返回可能在下一次读取中补全的主题消息开头。"""
            for size in range(min(len(data), max(map(len, _THEME_MARKERS))), 0, -1):
                suffix = data[-size:]
                if any(marker.startswith(suffix) for marker in _THEME_MARKERS):
                    return suffix
            return ""

        def _hold_pending(self, pending: str, *, confirmed: bool) -> None:
            self._theme_pending = pending
            self._theme_pending_at = time.monotonic()
            self._theme_pending_confirmed = confirmed

        def tick(self) -> Iterable[Message]:
            """驱动每轮 select 后调用；顺带放行扣得太久的未确认前缀。

            孤立 Esc 键会被 `_trailing_marker_prefix` 当成可能的应答开头扣住，
            而原解析器的 ESCAPE_DELAY 超时根本看不到这个字节——不放行的话，
            真实终端上 Esc 要等下一个按键才生效。真实的应答拆包余下字节早就
            在 pty 缓冲里，下一个 tick（约 0.1s）就会经 feed 补齐；超过
            `_PENDING_FLUSH_AGE` 还没等到，就断定它是用户按键：单独的 Esc
            直接生成 Esc 键事件（免去再等一轮 ESCAPE_DELAY），其余交给
            原解析器按序列超时自行处理。
            """
            pending = self._theme_pending
            if (
                pending
                and not self._theme_pending_confirmed
                and time.monotonic() - self._theme_pending_at >= _PENDING_FLUSH_AGE
            ):
                self._theme_pending = ""
                if pending == "\x1b":
                    yield events.Key("escape", "\x1b")
                    yield from super().tick()
                    return
                yield from super().feed(pending)
            yield from super().tick()

        def feed(self, data: str) -> Iterable[Message]:
            # 驱动关闭时会用空串让原解析器收尾；未完整的主题应答也交回去，
            # 避免我们吞掉无法确认的普通输入。
            if not data:
                pending, self._theme_pending = self._theme_pending, ""
                if pending:
                    yield from super().feed(pending)
                yield from super().feed(data)
                return

            pending = self._theme_pending + data
            self._theme_pending = ""
            while pending:
                osc_at = pending.find(_OSC_BACKGROUND_MARKER)
                mode_at = pending.find(_MODE_MARKER)
                starts = [index for index in (osc_at, mode_at) if index >= 0]
                if not starts:
                    # TTY 读取不保证控制消息一次到齐。若 ESC / ESC[ / ESC[?997;
                    # 这类前缀被先交给原解析器，后半段会变成普通按键并泄漏到
                    # 当前托管助手，Cursor 会把它画成 ^[[?997;2n。
                    prefix = self._trailing_marker_prefix(pending)
                    if prefix:
                        body = pending[:-len(prefix)]
                        if body:
                            yield from super().feed(body)
                        self._hold_pending(prefix, confirmed=False)
                        return
                    yield from super().feed(pending)
                    return

                start = min(starts)
                if start:
                    yield from super().feed(pending[:start])
                    pending = pending[start:]

                if pending.startswith(_OSC_BACKGROUND_MARKER):
                    match = _OSC_BACKGROUND_RE.match(pending)
                    if match is None:
                        if (
                            "\x07" in pending
                            or "\x1b\\" in pending
                            or len(pending) > 128
                        ):
                            yield from super().feed(pending)
                            return
                        # 已确认是 OSC 11 应答开头，但结尾可能在下一次 read 才到。
                        self._hold_pending(pending, confirmed=True)
                        return
                    raw = match.group(0)
                    yield TerminalBackgroundReport(osc_report=raw.encode("ascii"))
                    pending = pending[match.end():]
                    continue

                match = _MODE_RE.match(pending)
                if match is None:
                    if len(pending) >= len(_MODE_MARKER) + 2:
                        yield from super().feed(pending)
                        return
                    self._hold_pending(pending, confirmed=True)
                    return
                yield TerminalBackgroundReport(is_light=match.group(1) == "2")
                pending = pending[match.end():]


    class TerminalThemeLinuxDriver(LinuxDriver):
        """Reuse Textual's parser/writer, with bounded paint backpressure."""

        @property
        def output_backpressured(self) -> bool:
            # Textual owns the writer and its queue; if either is absent (a
            # future driver or an unbounded queue) paint stays on the native
            # path instead of guessing a threshold.
            queue = getattr(getattr(self, "_writer_thread", None), "_queue", None)
            if queue is None:
                return False
            maxsize = int(getattr(queue, "maxsize", 0) or 0)
            if maxsize <= 0:
                return False
            # A display uses three ordered writes with synchronized output.
            # Reserve headroom for input/cursor and lifecycle controls.
            return queue.qsize() >= max(1, maxsize // 3)

        def try_write_theme(self, data: str) -> bool:
            from queue import Full

            queue = getattr(getattr(self, "_writer_thread", None), "_queue", None)
            if queue is None or self.output_backpressured:
                return False
            try:
                queue.put_nowait(data)
            except Full:
                return False
            return True

        def start_application_mode(self) -> None:
            super().start_application_mode()
            enable_runtime_theme_tracking(self)
            enable_tmux_passthrough()
            # Textual 只在形状变化时发 OSC 22；终端被重置后会回落到 I 型光标。
            # 进应用模式时主动补一次箭头，tmux 穿透也在 sequence() 里带上。
            _write(self, sequence("default"))

        def stop_application_mode(self) -> None:
            _write(self, reset_sequence())
            restore_tmux_passthrough()
            disable_runtime_theme_tracking(self)
            super().stop_application_mode()

        def run_input_thread(self) -> None:
            original_parser = linux_driver.XTermParser
            linux_driver.XTermParser = TerminalThemeParser
            try:
                super().run_input_thread()
            finally:
                linux_driver.XTermParser = original_parser

else:  # pragma: no cover - Windows 使用 Textual 原生驱动，当前不发送主题查询
    TerminalThemeParser = None  # type: ignore[assignment,misc]
    TerminalThemeLinuxDriver = None  # type: ignore[assignment,misc]


def enhance_driver(driver_class):
    """Unix 默认驱动加上主题应答解析；自定义/Windows 驱动保持原样。"""
    if os.name == "nt":
        return driver_class
    return TerminalThemeLinuxDriver if driver_class is LinuxDriver else driver_class


def supports_runtime_theme_tracking(driver) -> bool:
    """当前真实终端驱动是否支持运行中主题应答。"""
    return os.name != "nt" and isinstance(driver, TerminalThemeLinuxDriver)


def _write(driver, data: str) -> None:
    try:
        driver.write(data)
        driver.flush()
    except Exception:  # noqa: BLE001 - 终端查询失败不能影响主界面
        return


def enable_runtime_theme_tracking(driver) -> None:
    """订阅支持 DEC 2031 的终端，并立即查询一次当前背景。"""
    query = "\x1b[?2031h\x1b[?996n\x1b]11;?\x07"
    if os.environ.get("TMUX"):
        # tmux 的缓存色可能仍是启动时的旧值；同时穿透查询真实外层终端。
        query += (
            "\x1bPtmux;\x1b\x1b[?2031h\x1b\\"
            "\x1bPtmux;\x1b\x1b[?996n\x1b\\"
            "\x1bPtmux;\x1b\x1b]11;?\x07\x1b\\"
        )
    _write(driver, query)


def query_runtime_theme(driver) -> None:
    """查询当前深浅模式与实际背景色；不阻塞等待应答。"""
    query = "\x1b[?996n\x1b]11;?\x07"
    if os.environ.get("TMUX"):
        query += (
            "\x1bPtmux;\x1b\x1b[?996n\x1b\\"
            "\x1bPtmux;\x1b\x1b]11;?\x07\x1b\\"
        )
    try_write = getattr(driver, "try_write_theme", None)
    if callable(try_write):
        try_write(query)
    else:
        _write(driver, query)


def disable_runtime_theme_tracking(driver) -> None:
    """退出前关闭 DEC 2031 主动通知。"""
    sequence = "\x1b[?2031l"
    if os.environ.get("TMUX"):
        sequence += "\x1bPtmux;\x1b\x1b[?2031l\x1b\\"
    _write(driver, sequence)
