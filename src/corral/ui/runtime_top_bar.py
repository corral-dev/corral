"""右侧助手顶栏：侧栏显隐开关 + 已安装运行时加格按钮。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from rich.text import Text
from textual import events
from textual.containers import Horizontal
from textual.widget import Widget

from corral.i18n import t

_SHELL_CHIP_STYLE = "dim bold"

if TYPE_CHECKING:
    from corral.runtime.registry import RuntimeRegistry


class _SidebarToggleChip(Widget):
    """侧边栏显隐开关：藏起后仍留在右栏顶栏，才能点回来。"""

    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    _SidebarToggleChip {
        height: 1;
        width: auto;
        min-width: 3;
        padding: 0 1;
        margin: 0 1 0 0;
        content-align: center middle;
        color: $text-muted;
        pointer: pointer;
    }
    _SidebarToggleChip:hover {
        background: $boost;
        color: $foreground;
    }
    """

    def __init__(self, sidebar_visible: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self._sidebar_visible = sidebar_visible

    def set_sidebar_visible(self, visible: bool) -> None:
        if self._sidebar_visible == visible:
            return
        self._sidebar_visible = visible
        self.refresh()

    def render(self) -> Text:
        # 可见时 ◀ 表示「收起左侧」；已藏时 ▶ 表示「展开」。
        return Text("◀" if self._sidebar_visible else "▶")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        screen = self.screen
        action = getattr(screen, "action_toggle_sidebar", None)
        if callable(action):
            action()


class _TopBarSpacer(Widget):
    """把助手 chip 顶到右侧。"""

    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    _TopBarSpacer {
        width: 1fr;
        height: 1;
    }
    """

    def render(self) -> Text:
        return Text("")


class _DragonChip(Widget):
    """彩蛋：点击触发全屏中国龙横飞动画。"""

    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    _DragonChip {
        height: 1;
        width: auto;
        min-width: 6;
        padding: 0 1;
        margin: 0 0 0 1;
        content-align: center middle;
        color: $error;
        pointer: pointer;
    }
    _DragonChip:hover {
        background: $boost;
        color: $error;
    }
    """

    def __init__(self, on_click: Callable[[], None], **kwargs) -> None:
        super().__init__(**kwargs)
        self._on_click = on_click

    def render(self) -> Text:
        return Text("Dragon", style="bold red")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_click()


class _ShellChip(Widget):
    """内嵌自由 shell 分屏入口。"""

    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    _ShellChip {
        height: 1;
        width: auto;
        min-width: 6;
        padding: 0 1;
        margin: 0 1 0 0;
        content-align: center middle;
        pointer: pointer;
    }
    _ShellChip:hover {
        background: $boost;
    }
    """

    def __init__(self, on_pick: Callable[[], None], **kwargs) -> None:
        super().__init__(**kwargs)
        self._on_pick = on_pick

    def render(self) -> Text:
        return Text(t("shell.chip_label"), style=_SHELL_CHIP_STYLE)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_pick()


class _RuntimeChip(Widget):
    """单个助手按钮。"""

    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    _RuntimeChip {
        height: 1;
        width: auto;
        min-width: 8;
        padding: 0 1;
        margin: 0 1 0 0;
        content-align: center middle;
        pointer: pointer;
    }
    _RuntimeChip:hover {
        background: $boost;
    }
    """

    def __init__(
        self,
        runtime_id: str,
        label: str,
        style: str,
        on_pick: Callable[[str], None],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.runtime_id = runtime_id
        self._label = label
        self._style = style
        self._on_pick = on_pick

    def render(self) -> Text:
        return Text(self._label, style=self._style)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_pick(self.runtime_id)


class RuntimeTopBar(Horizontal):
    """右侧顶栏：左侧侧栏开关，右侧已安装助手均可点击。"""

    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    RuntimeTopBar {
        height: 1;
        width: 1fr;
        padding: 0 1;
        align: left middle;
        background: $footer-background;
    }
    """

    def __init__(
        self,
        registry: RuntimeRegistry,
        on_runtime_pick: Callable[[str], None],
        *,
        sidebar_visible: bool = True,
        on_dragon_click: Callable[[], None] | None = None,
        on_shell_pick: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._registry = registry
        self._on_runtime_pick = on_runtime_pick
        self._sidebar_visible = sidebar_visible
        self._on_dragon_click = on_dragon_click
        self._on_shell_pick = on_shell_pick

    def set_sidebar_visible(self, visible: bool) -> None:
        self._sidebar_visible = visible
        try:
            self.query_one("#sidebar-toggle", _SidebarToggleChip).set_sidebar_visible(visible)
        except Exception:
            pass

    def compose(self):
        import corral

        yield _SidebarToggleChip(
            self._sidebar_visible,
            id="sidebar-toggle",
        )
        yield _TopBarSpacer()
        if self._on_shell_pick is not None:
            yield _ShellChip(self._on_shell_pick, id="shell-chip")
        from corral.runtime.registry import ACTIVE_RUNTIME_IDS

        for runtime in self._registry:
            if runtime.id not in ACTIVE_RUNTIME_IDS:
                continue
            if not runtime.is_available():
                continue
            yield _RuntimeChip(
                runtime.id,
                runtime.display_name,
                corral.runtime_label_style(runtime.id),
                self._on_runtime_pick,
                id=f"runtime-chip-{runtime.id}",
            )
        if self._on_dragon_click is not None:
            yield _DragonChip(self._on_dragon_click, id="dragon-chip")
