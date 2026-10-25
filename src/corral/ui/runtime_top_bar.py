"""右侧助手顶栏：侧栏显隐开关 + 「新分屏」加格按钮（终端与已安装助手）。

2026-10-25 改版：加格按钮前有淡色「＋ 新分屏」标签说明用途；按钮一律中性色
（助手品牌色只用来标明会话 / 格子属于哪个助手，不用于动作按钮）；彩蛋 Dragon
淡显。放不下时先藏 Dragon，仍放不下就收成一个「＋ 新分屏 ▾」，点开是同一组选项。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from rich.text import Text
from textual import events
from textual.containers import Horizontal
from textual.widget import Widget

from corral.i18n import t

_CHIP_STYLE = "bold"
_SHELL_CHIP_ID = "__shell__"

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
        color: $text-muted;
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
        return Text("Dragon")

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
        return Text(t("shell.chip_label"), style=_CHIP_STYLE)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_pick()


class _NewPaneLabel(Widget):
    """加格按钮前的淡色标签：说明右边这排按钮是「开一个新分屏」。"""

    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    _NewPaneLabel {
        height: 1;
        width: auto;
        margin: 0 1 0 0;
        color: $text-muted;
    }
    """

    def render(self) -> Text:
        return Text(t("topbar.new_pane"))


class _NewPaneMenuChip(Widget):
    """窄栏收起态：一个「＋ 新分屏 ▾」，点开列出终端与已安装助手。"""

    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    _NewPaneMenuChip {
        height: 1;
        width: auto;
        padding: 0 1;
        content-align: center middle;
        pointer: pointer;
    }
    _NewPaneMenuChip:hover {
        background: $boost;
    }
    """

    def __init__(self, on_open: Callable[[], None], **kwargs) -> None:
        super().__init__(**kwargs)
        self._on_open = on_open

    def render(self) -> Text:
        return Text(f"{t('topbar.new_pane')} ▾", style=_CHIP_STYLE)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_open()


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
        yield _SidebarToggleChip(
            self._sidebar_visible,
            id="sidebar-toggle",
        )
        yield _TopBarSpacer()
        yield _NewPaneLabel(id="new-pane-label")
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
                _CHIP_STYLE,
                self._on_runtime_pick,
                id=f"runtime-chip-{runtime.id}",
            )
        yield _NewPaneMenuChip(self._open_new_pane_menu, id="new-pane-menu")
        if self._on_dragon_click is not None:
            yield _DragonChip(self._on_dragon_click, id="dragon-chip")

    # ---- 窄栏收起 ----

    def _pane_chips(self) -> list[Widget]:
        return [*self.query(_ShellChip), *self.query(_RuntimeChip)]

    @staticmethod
    def _chip_cells(text: str, *, padding: int = 2, margin: int = 1) -> int:
        from corral.textutil import text_width

        return text_width(text) + padding + margin

    def _fit(self) -> None:
        """按可用宽度决定：全展开 / 藏 Dragon / 收成一个菜单。"""
        if not self.is_mounted:
            return
        width = self.content_size.width
        if width <= 0:
            return
        toggle = 4  # ◀ + padding + margin
        label = self._chip_cells(t("topbar.new_pane"), padding=0)
        chips = sum(
            self._chip_cells(
                t("shell.chip_label") if isinstance(chip, _ShellChip) else chip._label
            )
            for chip in self._pane_chips()
        )
        dragon = self._chip_cells("Dragon") if self._on_dragon_click is not None else 0
        expanded = toggle + label + chips <= width
        show_dragon = expanded and toggle + label + chips + dragon <= width
        for widget in self._pane_chips():
            widget.display = expanded
        for widget in self.query(_NewPaneLabel):
            widget.display = expanded
        for widget in self.query(_NewPaneMenuChip):
            widget.display = not expanded
        for widget in self.query(_DragonChip):
            widget.display = show_dragon

    def on_mount(self) -> None:
        self.call_after_refresh(self._fit)

    def on_resize(self, event: events.Resize) -> None:
        self._fit()

    def _open_new_pane_menu(self) -> None:
        from corral.runtime.registry import ACTIVE_RUNTIME_IDS
        from corral.ui.modals import RuntimeChoice, RuntimePickerModal

        choices: list[RuntimeChoice] = []
        if self._on_shell_pick is not None:
            choices.append(RuntimeChoice(_SHELL_CHIP_ID, t("shell.chip_label"), "", True))
        for runtime in self._registry:
            if runtime.id in ACTIVE_RUNTIME_IDS and runtime.is_available():
                choices.append(RuntimeChoice(runtime.id, runtime.display_name, "", True))
        if not choices:
            return

        def picked(choice: str | None) -> None:
            if choice == _SHELL_CHIP_ID and self._on_shell_pick is not None:
                self._on_shell_pick()
            elif choice:
                self._on_runtime_pick(choice)

        self.app.push_screen(RuntimePickerModal(t("topbar.new_pane"), choices), picked)
