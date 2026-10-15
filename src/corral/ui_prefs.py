"""壳层 UI 偏好：侧边栏显隐等。

存储与会话组、置顶共用同一份多进程安全的侧边栏记忆库（见 `split_layout`），不再各自
持有一个整份覆盖写的 JSON 文件。侧边栏显隐是**启动时套用的偏好**，不跨窗口实时同步：
正在用的窗口的侧栏被别处收起来是惊吓，不是功能。
"""

from __future__ import annotations

from corral.split_layout import default_layout_db


def load_sidebar_visible(*, default: bool = True) -> bool:
    """读取侧边栏是否可见；没有记录或存储不可用时回落 default。"""
    return default_layout_db().sidebar_visible(default=default)


def save_sidebar_visible(visible: bool) -> None:
    """保存侧边栏可见性。"""
    default_layout_db().set_sidebar_visible(bool(visible))


SIDEBAR_WIDTH_MIN = 28
SIDEBAR_WIDTH_DEFAULT = 39
SIDEBAR_WIDTH_MAX = 72


def load_sidebar_width(*, default: int = SIDEBAR_WIDTH_DEFAULT) -> int:
    """读取侧边栏宽度；没有记录、损坏或存储不可用时回落 default 并做钳制。"""
    try:
        width = int(default_layout_db().sidebar_width(default=default))
    except (TypeError, ValueError):
        return default
    return max(SIDEBAR_WIDTH_MIN, min(SIDEBAR_WIDTH_MAX, width))


def save_sidebar_width(width: int) -> None:
    """保存侧边栏宽度（先钳制到允许范围）。"""
    try:
        value = int(width)
    except (TypeError, ValueError):
        return
    default_layout_db().set_sidebar_width(
        max(SIDEBAR_WIDTH_MIN, min(SIDEBAR_WIDTH_MAX, value))
    )
