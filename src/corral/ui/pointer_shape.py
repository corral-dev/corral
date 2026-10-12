"""OSC 22 鼠标指针形状。

终端里指针形状归模拟器管，程序只能发 ``OSC 22``（``ESC ] 22 ; 形状 BEL``）
请求。Textual 8 已内置 CSS ``pointer:`` 与 ``App._set_pointer_shape``，但裸
序列在 tmux 里会被吞掉、形状未变时不再补发、退出也不复位。本模块集中构造
序列、tmux DCS 穿透和 pane 级 ``allow-passthrough``。
"""

from __future__ import annotations

import os
import subprocess

# 真 xterm 与 iTerm2 只认 X11 cursorfont 名（iTerm2 3.6 的 ``screenSetPointerShape:``
# 只有 X11 名表，CSS 名一律被忽略并退回带圈的 I 型）。只对这两种终端替换，避免
# 给只认 CSS 名的终端（kitty / Ghostty / WezTerm）发无效名。
_XTERM_NAMES = {
    "default": "left_ptr",
    "pointer": "hand2",
    "text": "xterm",
    "wait": "watch",
}


def _wants_x11_names() -> bool:
    if os.environ.get("XTERM_VERSION"):
        return True
    # ``LC_TERMINAL`` 在 tmux / SSH 里仍保留，``TERM_PROGRAM`` 会被 tmux 改写。
    return (
        os.environ.get("TERM_PROGRAM") == "iTerm.app"
        or os.environ.get("LC_TERMINAL") == "iTerm2"
    )


def _shape_name(shape: str) -> str:
    if _wants_x11_names():
        return _XTERM_NAMES.get(shape, shape)
    return shape


def _osc(shape: str) -> str:
    return f"\x1b]22;{shape}\x07"


def _with_tmux(raw: str) -> str:
    """裸序列之后追加一份 DCS 穿透；无 ``$TMUX`` 时原样返回。

    ESC 必须双写（``\\x1bPtmux;\\x1b`` + 原序列里每个 ESC 再写一次 + ``ST``），
    与 ``terminal_theme.py`` 主题查询同一套写法。裸序列留给 tmux 自身（万一
    它也认 OSC 22），穿透副本给外层真实终端。
    """
    if not os.environ.get("TMUX"):
        return raw
    inner = raw.replace("\x1b", "\x1b\x1b")
    return raw + f"\x1bPtmux;{inner}\x1b\\"


def sequence(shape: str) -> str:
    """构造设置指针形状的 OSC 22；套 tmux 时带穿透副本。"""
    return _with_tmux(_osc(_shape_name(shape)))


def reset_sequence() -> str:
    """空形状名 = kitty 规范的「复位到终端默认」。套 tmux 时同样带穿透。"""
    return _with_tmux(_osc(""))


def _tmux_set(*args: str) -> None:
    if not os.environ.get("TMUX") or not os.environ.get("TMUX_PANE"):
        return
    try:
        subprocess.run(
            ["tmux", "set", *args],
            check=False,
            timeout=1,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - 改指针形状失败不能掀掉 TUI
        return


def enable_tmux_passthrough() -> None:
    """当前 pane 打开 ``allow-passthrough``，让 OSC 22 / OSC 11 能穿到外层终端。"""
    _tmux_set("-p", "allow-passthrough", "on")


def restore_tmux_passthrough() -> None:
    """退出时清掉 pane 级 ``allow-passthrough``，还原用户自己的设置。"""
    _tmux_set("-pu", "allow-passthrough")
