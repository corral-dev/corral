"""右侧分屏区：助手顶栏 + 最多四格均分内嵌终端。"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction

from rich.text import Text
from textual import events
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from corral.i18n import t
from corral.models import session_key as make_session_key
from corral.split_layout import MAX_PANES
from corral.ui.dragon_splash import DragonSplash
from corral.ui.embed_pane import EmbedPane, ModeChanged
from corral.ui.runtime_top_bar import RuntimeTopBar
from corral.ui.session_hud import SessionHud

_LIST_FOCUS_IDS = frozenset({
    "session-list", "sidebar-sticky", "sidebar-scroll",
})
_LIST_FOCUS_TYPES = frozenset({"SessionListView", "_SidebarList"})


def _session_list_has_focus(focused) -> bool:
    """焦点是否在侧边栏会话列表（含固定头 / 会话列表两段内层 ListView）。"""
    node = focused
    while node is not None:
        if getattr(node, "id", None) in _LIST_FOCUS_IDS:
            return True
        if type(node).__name__ in _LIST_FOCUS_TYPES:
            return True
        node = getattr(node, "parent", None)
    return False


def projected_embed_sizes(
    row_width: int, row_height: int, count: int,
) -> list[tuple[int, int]]:
    """按 Textual HorizontalLayout 的 1fr 取整规则，算出每个内嵌画面的内容尺寸。

    `PaneCell` 只有非首格占一列左间距（`margin-left: 1`），剩余列按 `1fr` 从左
    到右均分。Textual 不是「余数给最后几格」，而是 Fraction 累加后再
    `floor(next) - floor(x)`；四格且行宽为 4k+1 时实际是 `[n, n+1, n, n+1]`，
    不是 `[n, n, n+1, n+1]`。顶、底栏各占一行。
    """
    if count <= 0:
        return []
    gutters = max(0, count - 1)
    remaining = max(0, int(row_width) - gutters)
    unit = Fraction(remaining, count)
    height = max(1, int(row_height) - 2)
    x = Fraction(0)
    sizes: list[tuple[int, int]] = []
    for index in range(count):
        nxt = x + unit
        width = max(1, math.floor(nxt) - math.floor(x))
        sizes.append((width, height))
        x = nxt + (1 if index < count - 1 else 0)
    return sizes


@dataclass
class PaneSpec:
    """单格绑定的会话。"""

    session_key: str
    keepalive_name: str | None = None
    cell_id: str = ""
    is_shell: bool = False


class _PaneClose(Static):
    ALLOW_SELECT = False
    can_focus = False

    DEFAULT_CSS = """
    _PaneClose {
        width: 3;
        height: 1;
        content-align: center middle;
        color: $text-muted;
        pointer: pointer;
    }
    _PaneClose:hover {
        color: $error;
        background: $error-darken-3;
    }
    """

    def __init__(self, on_close: Callable[[], None], **kwargs) -> None:
        super().__init__("✕", **kwargs)
        self._on_close = on_close

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # 点 ✕ 不得让本格抢到焦点：否则延迟 DescendantFocus 会把侧栏切到被关会话，
        # 节流的选择跟随再把它当单格打开。
        event.stop()
        event.prevent_default()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        event.prevent_default()
        self._on_close()


# 活跃格顶/底高亮色：主题变量 = $primary-muted 再提亮约 10%（见 app.py），
# 比纯 muted 更好辨认，仍避免高饱和蓝条抢过内嵌内容。
_ACTIVE_PANE_BG = "$pane-active-background"
# 未选中顶/底条与 Your prompts：比 $surface 略亮一档的深灰，浮在助手画布上才分得清。
_INACTIVE_PANE_BG = "$panel"


class _PaneHeader(Horizontal):
    ALLOW_SELECT = False

    DEFAULT_CSS = f"""
    _PaneHeader {{
        height: 1;
        width: 1fr;
        margin: 0;
        padding: 0;
        color: auto 90%;
        background: {_INACTIVE_PANE_BG};
    }}
    _PaneHeader.-active {{
        color: auto 90%;
        background: {_ACTIVE_PANE_BG};
    }}
    _PaneHeader.-active _PaneClose {{
        color: auto 90%;
    }}
    _PaneHeader Static.title {{
        width: 1fr;
        height: 1;
        content-align: left middle;
        margin: 0;
        padding: 0;
        text-overflow: ellipsis;
    }}
    _PaneHeader Static.restart-hint {{
        width: auto;
        height: 1;
        content-align: right middle;
        margin: 0;
        padding: 0 1;
        color: auto 70%;
        text-overflow: ellipsis;
    }}
    """

    def __init__(
        self,
        title: str,
        on_close: Callable[[], None],
        *,
        closable: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._title = title
        self._title_widget = Static(title, classes="title")
        # 默认藏起：空 hint 若仍占 padding，会在标题和 ✕ 之间留出空隙。
        self._hint_widget = Static("", classes="restart-hint")
        self._hint_widget.display = False
        self._close_widget = _PaneClose(on_close)
        self._closable = closable
        self._close_widget.display = closable

    def compose(self):
        yield self._title_widget
        yield self._hint_widget
        yield self._close_widget

    def set_title(self, title: str) -> None:
        self._title = title
        self._title_widget.update(title)

    def set_restart_hint(self, text: str) -> None:
        """预览/已结束格在标题旁常驻短提示；非预览态传空串清空。"""
        self._hint_widget.update(text)
        self._hint_widget.display = bool(text)

    def set_closable(self, closable: bool) -> None:
        """水果组分屏可关格；活跃会话看板只被动展示，不露 ✕。"""
        self._closable = closable
        self._close_widget.display = closable

    def set_active(self, active: bool) -> None:
        self.set_class(active, "-active")


class _PaneFooter(Static):
    """分栏底条：与标题栏同步高亮当前激活格。

    预览/已结束格常驻 Enter 重启——详情头里的同款提示会随钉底滚动滚出视野，底条不滚。
    实时格持有输入时底条留空：**禁止**再写 `Ctrl+\\` / Back to list / 返回列表
    （2026-09-12：这类描述不得出现在界面上）。非激活的实时格仍提示输入未接管。
    """

    ALLOW_SELECT = False

    DEFAULT_CSS = f"""
    _PaneFooter {{
        height: 1;
        width: 1fr;
        margin: 0;
        padding: 0 1;
        color: auto 70%;
        background: {_INACTIVE_PANE_BG};
        text-overflow: ellipsis;
    }}
    _PaneFooter.-active {{
        background: {_ACTIVE_PANE_BG};
    }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)

    def set_state(
        self, active: bool, masked: bool, *, restart_target: bool = False
    ) -> None:
        """active=本格持有输入；masked=本格是实时会话但输入在别处。"""
        self.set_class(active, "-active")
        if restart_target:
            self.update(t("pane.restart_hint"))
        elif masked:
            self.update(t("pane.masked_hint"))
        else:
            self.update("")


class PaneCell(Vertical):
    """单格：标题栏 + EmbedPane + 底条高亮，右上角可浮一个会话小窗。"""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    PaneCell {
        layers: default hud;
        width: 1fr;
        height: 1fr;
        border: none;
        margin: 0 0 0 1;
        padding: 0;
    }
    PaneCell.-leading {
        margin-left: 0;
    }
    PaneCell.-spare {
        display: none;
        width: 0;
        margin: 0;
    }
    PaneCell EmbedPane {
        height: 1fr;
        margin: 0;
        padding: 0;
    }
    """

    def __init__(
        self,
        spec: PaneSpec,
        *,
        title: str,
        # 收 PaneSpec 而不是无参回调：格子可以就地改绑到另一个会话，关闭时必须
        # 用「此刻绑着的」spec，不能是构造时闭包捕获的那一个。
        on_close: Callable[[PaneSpec], None],
        on_focus_list: Callable[[], None],
        osc_report: bytes | None,
        detail_renderer: Callable[[], Text | str] | None = None,
        on_pane_focused: Callable[[str], None] | None = None,
        on_restart: Callable[[str, bool], None] | None = None,
        on_sync_mask: Callable[[], None] | None = None,
        on_hud_toggle: Callable[[], None] | None = None,
        closable: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.spec = spec
        self._on_close = on_close
        self._on_focus_list = on_focus_list
        self._on_pane_focused = on_pane_focused
        self._on_restart = on_restart
        self._on_sync_mask = on_sync_mask
        self._on_hud_toggle = on_hud_toggle
        self._osc_report = osc_report
        self._title = title
        self._closable = closable
        # 重启中占位标记：`set_restarting(True)` 立起，收尾时清掉。`sync_chrome`
        # 按格子当前态重算标题栏提示时会先看它——占位期间不许被 Enter 重启
        # 提示盖掉，否则用户会再按一次回车触发重复重启。
        self._restarting_hint = False
        # Live panes must not *display* a transcript renderer (capture gaps
        # would flash chat). Keep it as an ended fallback so a confirmed-dead
        # host can switch to the same preview path as a history card.
        self._bind_renderers(detail_renderer, hosted=bool(spec.keepalive_name))
        self._input_masked = False
        self._pooled = False

    def set_pooled(self, pooled: bool) -> None:
        """闲置格：隐藏且不占宽，保留控件供下次改绑（跨组切屏免 remount）。"""
        self._pooled = pooled
        self.set_class(pooled, "-spare")
        if pooled:
            self.set_class(False, "-leading")
            self.display = False
        else:
            self.display = True

    def set_leading(self, leading: bool) -> None:
        if not self._pooled:
            self.set_class(leading, "-leading")

    def park(self) -> None:
        """收回进格池：清画面、关通道绑定，不销毁控件。"""
        pane = self.embed_pane()
        if pane is not None:
            pane.clear()
        self.spec = PaneSpec(session_key="__spare__", cell_id=self.spec.cell_id)
        self._bind_renderers(None, hosted=False)
        self.set_title("")
        self.set_pooled(True)

    def _close_self(self) -> None:
        self._on_close(self.spec)

    def _restart_self(self, dead: bool) -> None:
        # 必须读此刻绑着的 spec：格子会就地改绑到别的会话（见 rebind）。
        if self._on_restart is not None:
            self._on_restart(self.spec.session_key, dead)

    def set_closable(self, closable: bool) -> None:
        """同步标题栏是否露出关格 ✕（看板被动展示时关掉）。"""
        self._closable = closable
        try:
            self.query_one(_PaneHeader).set_closable(closable)
        except Exception:
            pass

    def compose(self):
        yield _PaneHeader(
            self._title, self._close_self, closable=self._closable, classes="header",
        )
        yield EmbedPane(
            on_focus_list=self._on_focus_list,
            on_restart=self._restart_self,
            osc_report=self._osc_report,
            id=f"embed-{self.spec.cell_id}",
        )
        yield _PaneFooter(classes="footer")
        # 浮层放在最后：同层内单独排版，dock 到右上角，不参与上面三个的纵向堆叠。
        yield SessionHud(self._on_hud_toggle, classes="hud")

    def on_mount(self) -> None:
        # 格池闲置格挂上时不启动会话；真正绑定走 rebind → _start_session。
        if self._pooled:
            return
        self.call_after_refresh(self._start_session)

    def rebind(
        self,
        spec: PaneSpec,
        *,
        title: str,
        detail_renderer: Callable[[], Text | str] | None,
        target_size: tuple[int, int] | None = None,
        discard_stale_screen: bool = False,
    ) -> None:
        """把这一格改绑到另一个会话，不销毁重建。

        销毁重建的代价（实测）是控件重建约 30ms + 重铺回退内容约 55ms + 重新建
        控制通道约 18ms，还会连带丢掉上一格的实时画面；就地改绑把这些全省掉，
        `EmbedPane.focus_session` 本身已经支持切换会话（提升抓帧代次、拦旧回调）。
        `cell_id` 必须沿用旧的：格子里的 EmbedPane 的 DOM id 是 compose 时按它
        生成的，换了会和 `_cell_for_spec` 的匹配对不上。
        """
        spec.cell_id = self.spec.cell_id
        self.spec = spec
        self._bind_renderers(detail_renderer, hosted=bool(spec.keepalive_name))
        self.set_pooled(False)
        self.set_title(title)
        # 单格与多格之间切换时，格子此刻还保留旧宽。用分栏区计算出的最终尺寸立即
        # 调整托管终端，同时丢掉旧宽画面；不能等布局后的防抖 resize，否则新单格会
        # 在约 200ms 内把旧半宽缓存贴在左侧。
        self._start_session(
            target_size=target_size,
            discard_stale_screen=discard_stale_screen,
        )

    def _start_session(
        self,
        *,
        target_size: tuple[int, int] | None = None,
        discard_stale_screen: bool = False,
    ) -> None:
        pane = self.embed_pane()
        if pane is None:
            return
        if self.spec.keepalive_name:
            pane.focus_session(
                self.spec.keepalive_name,
                target_size=target_size,
                discard_stale_screen=discard_stale_screen,
            )
            # 画面刚接上就要按当前焦点决定压不压暗，别等下一次焦点变化。
            if self._on_sync_mask is not None:
                self._on_sync_mask()
        else:
            # 改绑过来的格子可能还留着上一个会话的实时画面，renderer 为空也必须
            # 显式切回静态视图；新挂载的格子本来就是这个状态，重复调用无副作用。
            pane.show_detail(self._detail_renderer)
        # show_detail/focus_session 会发 ModeChanged；此处再钉一次，覆盖 compose
        # 后尚未挂齐顶底条、消息早到的竞态。高光跟当前会话走，要整排一起刷。
        self._request_chrome_sync()

    def _bind_renderers(
        self,
        renderer: Callable[[], Text | str] | None,
        *,
        hosted: bool,
    ) -> None:
        self._ended_fallback = renderer
        self._detail_renderer = None if hosted else renderer

    def _reveal_ended_preview(self) -> None:
        """Hosted pane confirmed gone: drop hosting and show the transcript."""
        pane = self.embed_pane()
        if pane is None or not pane.dead:
            return
        if self.spec.is_shell or self.spec.session_key.startswith("__"):
            return
        if self.spec.keepalive_name:
            area = self._split_area()
            if area is not None:
                area.store.mark_hosted(self.spec.session_key, None)
            # Spec still said "hosted" after the store dropped it, selection
            # follow would remount a live embed and wipe the transcript again.
            self.spec.keepalive_name = None
        if self._ended_fallback is not None:
            pane.show_detail(self._ended_fallback)

    def embed_pane(self) -> EmbedPane | None:
        for child in self.children:
            if isinstance(child, EmbedPane):
                return child
        return None

    def session_hud(self) -> SessionHud | None:
        """与标题栏同：分栏重建的中间态里浮层可能尚未挂上或已卸下。"""
        for child in self.children:
            if isinstance(child, SessionHud):
                return child
        return None

    def update_hud(self, data, *, expanded: bool) -> None:
        hud = self.session_hud()
        if hud is None:
            return
        if data is None:
            hud.hide()
        else:
            hud.update_data(data, expanded=expanded)

    def update_terminal_background(self, osc_report: bytes) -> None:
        self._osc_report = osc_report
        pane = self.embed_pane()
        if pane is not None:
            pane.update_terminal_background(osc_report)

    def _pane_header(self) -> _PaneHeader | None:
        """分栏重建/卸载过程中标题栏可能尚未挂上或已卸下。"""
        for child in self.children:
            if isinstance(child, _PaneHeader):
                return child
        return None

    def _pane_footer(self) -> _PaneFooter | None:
        """与标题栏同：重建中间态可能尚未挂上或已卸下。"""
        for child in self.children:
            if isinstance(child, _PaneFooter):
                return child
        return None

    def set_title(self, title: str) -> None:
        self._title = title
        header = self._pane_header()
        if header is not None:
            header.set_title(title)

    def focus_embed(self) -> None:
        pane = self.embed_pane()
        if pane is not None:
            pane.focus()

    def set_input_masked(self, masked: bool) -> None:
        pane = self.embed_pane()
        if pane is not None:
            pane.input_masked = masked
        self._input_masked = masked
        self._sync_active_marker()

    def _split_area(self) -> SplitPaneArea | None:
        node = self.parent
        while node is not None:
            if isinstance(node, SplitPaneArea):
                return node
            node = getattr(node, "parent", None)
        return None

    def _request_chrome_sync(self) -> None:
        """顶底条和 Your prompts 是整排一套高光，不能只刷本格。"""
        area = self._split_area()
        if area is not None:
            area.sync_chrome()
        else:
            self._sync_active_marker()

    def _on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self.call_after_refresh(self._request_chrome_sync)
        self.call_after_refresh(self._notify_pane_focused)

    def _on_descendant_blur(self, event: events.DescendantBlur) -> None:
        self.call_after_refresh(self._request_chrome_sync)

    def on_mode_changed(self, event: ModeChanged) -> None:
        """静态预览 ↔ 托管 ↔ 已结束：顶底 Enter 重启提示要跟着变。"""
        event.stop()
        if self.spec.is_shell:
            pane = self.embed_pane()
            if pane is not None and pane.dead:
                self._close_self()
                return
        self._reveal_ended_preview()
        self._sync_active_marker()

    def _notify_pane_focused(self) -> None:
        if self._pooled or not self.has_focus_within or self._on_pane_focused is None:
            return
        key = self.spec.session_key
        if key.startswith("__"):
            return
        self._on_pane_focused(key)

    def _is_restart_chrome_target(self) -> bool:
        """预览/已结束格才在顶底 chrome 写 Enter 重启；占位格与托管中不算。

        重启中占位（`_restarting_hint`）的格子也不算：后台 worker 正在杀旧起新，
        再写 Enter 提示会诱导用户按第二次回车触发重复重启。"""
        if self.spec.is_shell or self.spec.session_key.startswith("__"):
            return False
        if self._restarting_hint:
            return False
        pane = self.embed_pane()
        return pane is not None and pane._is_restart_target()  # noqa: SLF001

    def _chrome_active(self) -> bool:
        """顶底条和 Your prompts 跟当前会话走，不是只跟键盘焦点。

        这一格正在打字 → 高光。列表选中这一格对应的会话、且没有别的实时格
        在打字 → 同样高光（已结束预览也算）。占位格 / 闲置格不高光。
        """
        if self._pooled:
            return False
        key = self.spec.session_key
        if not key or key.startswith("__"):
            return False
        if self.has_focus_within:
            return True
        area = self._split_area()
        if area is None:
            return False
        if area.any_embed_focused():
            return False
        return key == area.focus_key

    def _sync_active_marker(self) -> None:
        # 双击顶栏助手、快速增删分栏时，焦点回调可能落在「标题栏/底条尚未 compose
        # / 旧格已卸下」的中间态；真机复现：NoMatches: '_PaneHeader'。缺件时
        # 静默跳过即可，下一轮焦点事件会再同步。
        active = self._chrome_active()
        restart_target = self._is_restart_chrome_target()
        header = self._pane_header()
        if header is not None:
            header.set_active(active)
            if self._restarting_hint:
                # 重启中占位：标题栏常驻「正在重启…」，不许被常规重算盖掉。
                header.set_restart_hint(t("pane.restarting"))
            else:
                header.set_restart_hint(
                    t("pane.restart_hint") if restart_target else ""
                )
        footer = self._pane_footer()
        if footer is not None:
            footer.set_state(
                active,
                self._input_masked and not active,
                restart_target=restart_target,
            )
        hud = self.session_hud()
        if hud is not None:
            hud.set_active(active)


class SplitPaneArea(Vertical):
    """右侧：顶栏 + 动态 1~4 格。"""

    DEFAULT_CSS = """
    SplitPaneArea {
        width: 1fr;
        height: 1fr;
        margin: 0 0 0 1;
    }
    SplitPaneArea #pane-row {
        width: 1fr;
        height: 1fr;
    }
    SplitPaneArea #pane-row-empty {
        width: 1fr;
        height: 1fr;
        content-align: center middle;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        store,
        *,
        on_runtime_pick: Callable[[str], None],
        on_pane_close: Callable[[str], None],
        on_focus_list: Callable[[], None],
        on_shell_pick: Callable[[], None] | None = None,
        on_pane_focused: Callable[[str], None] | None = None,
        on_pane_restart: Callable[[str, bool], None] | None = None,
        on_hud_toggle: Callable[[], None] | None = None,
        on_dragon_click: Callable[[], None] | None = None,
        osc_report: bytes | None = None,
        render_detail: Callable[[dict], Text] | None = None,
        sidebar_visible: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.store = store
        self._on_runtime_pick = on_runtime_pick
        self._on_shell_pick = on_shell_pick
        self._on_pane_close = on_pane_close
        self._on_focus_list = on_focus_list
        self._on_pane_focused = on_pane_focused
        self._on_pane_restart = on_pane_restart
        self._on_hud_toggle = on_hud_toggle
        self._on_dragon_click = on_dragon_click
        self._osc_report = osc_report
        self._render_detail = render_detail
        self._sidebar_visible = sidebar_visible
        self.current_project: str = ""
        self.title_with_project = False
        self.allow_cross_project = False
        # 水果组 / 手动分屏可关格；活跃会话看板只被动展示，不露 ✕、不响应关格。
        self.panes_closable = True
        self._panes: list[PaneSpec] = []
        self._focus_key: str | None = None
        # 「把输入交给某一格」的待兑现意图，以及尚未执行完的整排挂载数量。
        # 两者配合让意图能跨过一次异步 remount，见 _request_pane_focus。
        self._focus_intent_key: str | None = None
        # 焦点请求已发出、但 Textual 尚未真正把焦点送进内嵌终端时，右栏已经
        # 拥有输入。这个声明必须保留到 DescendantFocus 落下，不能在调用
        # Widget.focus() 后马上撤掉，否则此前排队的蒙版同步仍会灰掉一帧。
        self._input_claim_key: str | None = None
        self._mount_pending = 0
        self._focus_intent_serial = 0

    def compose(self):
        yield RuntimeTopBar(
            self.store.registry,
            self._on_runtime_pick,
            sidebar_visible=self._sidebar_visible,
            on_dragon_click=self._on_dragon_click,
            on_shell_pick=self._on_shell_pick,
            id="runtime-top-bar",
        )
        with Horizontal(id="pane-row"):
            yield DragonSplash(t("split.empty_hint"), id="pane-row-empty")

    def pane_count(self) -> int:
        return len(self._panes)

    def can_add_pane(self) -> bool:
        return len(self._panes) < MAX_PANES

    @property
    def focus_key(self) -> str | None:
        return self._focus_key

    def pane_specs(self) -> list[PaneSpec]:
        return list(self._panes)

    def sync_chrome(self) -> None:
        """按当前选中会话 / 持焦格重刷每一格的顶底条和 Your prompts 底色。"""
        for cell in self._pool_cells():
            cell._sync_active_marker()  # noqa: SLF001

    def mark_selected(self, session_key: str | None) -> None:
        """登记当前会话（列表高亮），不抢键盘；高光仍跟这条走。"""
        self._focus_key = session_key
        self.sync_chrome()

    def set_restarting(self, session_key: str, restarting: bool) -> None:
        """重启中占位：标题栏常驻「正在重启…」，顶掉 Enter 重启提示。

        重启的杀旧 + 起新现在跑在后台 worker（见 MainScreen._restart_and_focus）：
        前台不能再用旧的 Enter 提示装作「这一格没活干」，否则用户会再按一次回车
        触发重复重启。占位只改标题栏展示，不动格子绑定与焦点。退出占位时只清
        自己立的提示：`sync_chrome` 会按格子当前态重算 Enter 提示，直接调它
        会把「正在重启…」盖掉，所以清占位走「只清重启提示」的回写。"""
        for cell in self._cells():
            if cell.spec.session_key != session_key:
                continue
            header = cell._pane_header()  # noqa: SLF001
            if header is None:
                continue
            if restarting:
                cell._restarting_hint = True  # noqa: SLF001
                header.set_restart_hint(t("pane.restarting"))
            else:
                cell._restarting_hint = False  # noqa: SLF001
                pane = cell.embed_pane()
                restart_target = (
                    pane is not None and pane._is_restart_target()  # noqa: SLF001
                )
                header.set_restart_hint(
                    t("pane.restart_hint") if restart_target else ""
                )

    def rebind_keepalive(self, old_name: str, new_name: str) -> None:
        """重启换绑：把绑着旧 tmux 名的格子改到新名，不销毁重建。

        杀旧起新是同名复用失败后才换名（极少）：这时右栏还有格子绑着已死的
        旧名。逐格改 `spec.keepalive_name` 并让 pane 重新 `focus_session`，
        复用现有的改绑路径（保画面缓存、保通道池、保焦点），不走整排 remount。"""
        for cell in self._cells():
            if cell.spec.keepalive_name != old_name:
                continue
            pane = cell.embed_pane()
            if pane is not None:
                try:
                    pane.clear_stale_screen(old_name)
                except Exception:  # noqa: BLE001 清画面失败仍继续改绑
                    pass
            cell.spec.keepalive_name = new_name
            cell._bind_renderers(cell._ended_fallback, hosted=True)  # noqa: SLF001
            pane = cell.embed_pane()
            if pane is not None:
                pane.focus_session(new_name)
        self.sync_chrome()

    def cells(self) -> list[PaneCell]:
        return self._cells()

    def pane_width_for(self, session_key: str) -> int | None:
        """某条会话此刻挂在哪一格、那一格多宽（列）。找不到或尚未布局则 None。

        静态预览的 Markdown 是**按宽度预排好**再交出去的，不会再被上层重新折行，
        所以必须拿"真正要渲染它的那一格"的宽度——早先图省事取第一格，多分屏且
        各格不等宽时，分隔线和正文就会按别人的宽度排（真机上表现为横线长短对不上
        格子）。
        """
        for cell in self._cells():
            if cell.spec.session_key != session_key:
                continue
            pane = cell.embed_pane()
            width = pane.size.width if pane is not None else 0
            return width or None
        return None

    def any_embed_focused(self) -> bool:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None and pane.has_focus:
                return True
        return False

    def live_embed_focused(self) -> bool:
        """当前持有键盘焦点的格是不是「活着的实时终端」。

        为真时按键都被转发给托管会话，列表侧的单字母/翻页快捷键必须让路（见
        `MainScreen.check_action`）；静态对话预览格聚焦时不算，那些键仍归列表。
        """
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is None or not pane.has_focus:
                continue
            return bool(cell.spec.keepalive_name) and bool(pane.session_name) and not pane.dead
        return False

    def sync_input_mask(self) -> None:
        """焦点不在右栏时，把活着的实时终端整格压暗（输入无效的视觉提示）。

        只看「右栏是否持有输入」这一件事：焦点在另一格时不压暗其余格——那时输入
        是有效的，只是去了别的格，激活格自己的高亮条已经说明了这点。已经登记的
        自动聚焦已声明输入归属也视为右栏持有输入：用户点开会话的瞬间就已决定把
        后续输入交给它，直到真实焦点落下前都不能撤掉这个声明，否则此前排队的
        蒙版同步会在中间插入一帧灰色。
        """
        try:
            cells = self._cells()
        except Exception:  # noqa: BLE001 分栏重建中间态查不到 #pane-row
            return
        area_has_input = self.any_embed_focused() or self._input_claim_key is not None
        for cell in cells:
            pane = cell.embed_pane()
            live = (
                pane is not None
                and bool(cell.spec.keepalive_name)
                and bool(pane.session_name)
                and not pane.dead
            )
            cell.set_input_masked(live and not area_has_input)

    def update_terminal_background(self, osc_report: bytes) -> None:
        """保存新背景供后续分栏使用，并刷新所有已挂载面板。"""
        self._osc_report = osc_report
        for cell in self._cells():
            cell.update_terminal_background(osc_report)

    def set_window_focused(self, focused: bool) -> None:
        """整扇窗口失焦/聚焦：全部格子慢抓或恢复全速（见 EmbedPane.set_window_focused）。

        只调取样间隔，不改尺寸/通道/镜像；分栏重建中间态缺件时静默跳过。
        """
        try:
            cells = self._pool_cells()
        except Exception:  # noqa: BLE001 分栏重建中间态查不到 #pane-row
            return
        for cell in cells:
            pane = cell.embed_pane()
            if pane is not None:
                pane.set_window_focused(focused)

    def host_pane_size(self) -> tuple[int, int]:
        """新建托管会话用的单格尺寸（主线程调用）。

        按「加上这一格之后」的最终布局取自己这一格（末格）的宽高，让
        `host_session` 直接按最终尺寸建窗，避免 agent 启动窗口期再挨一次
        差几列的 resize。
        """
        from corral import embed as embed_mod

        row = self.query_one("#pane-row", Horizontal)
        count = max(1, len(self._panes) + 1)
        row_w = row.size.width or 120
        row_h = row.size.height or 24
        sizes = projected_embed_sizes(row_w, row_h, count)
        w, h = sizes[-1]
        return embed_mod.normalize_host_size(w, h)

    def _projected_embed_sizes(self, count: int) -> list[tuple[int, int]] | None:
        """计算本次分栏布局最终会给每个实时画面的内容尺寸。

        不能在旧格上读取 `pane.size`，那正是闪跳来源。算法与 Textual
        HorizontalLayout 对齐，见模块级 `projected_embed_sizes`。
        """
        if count <= 0:
            return []
        row = self.query_one("#pane-row", Horizontal)
        if row.size.width <= 0 or row.size.height <= 0:
            return None
        return projected_embed_sizes(row.size.width, row.size.height, count)

    def sync_hud(self, payloads: dict[str, object] | None, *, expanded: bool) -> None:
        """每个右栏格画自己的会话小窗；payloads 里没有的格一律收掉。

        数据来源与展开状态都由 `MainScreen` 决定，本类不查 store。展开/收起状态
        所有格共用一份。
        """
        payloads = payloads or {}
        for cell in self._cells():
            key = cell.spec.session_key
            if key in payloads:
                cell.update_hud(payloads[key], expanded=expanded)
            else:
                cell.update_hud(None, expanded=False)

    def invalidate_all_details(self) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None:
                pane.invalidate_detail()

    def invalidate_visible_previews(self) -> None:
        """只失效非托管预览格，内嵌终端不被重扫连带刷新。"""
        for cell in self._cells():
            if cell.spec.keepalive_name:
                continue
            pane = cell.embed_pane()
            if pane is not None:
                pane.invalidate_detail()

    def scroll_preview_home(self) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None:
                pane.scroll_detail_home()

    def scroll_preview_end(self) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None:
                pane.scroll_detail_end()

    def scroll_preview_page(self, delta: int) -> None:
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None:
                pane.scroll_detail_page(delta)

    def close_focused_pane(self) -> None:
        if not self.panes_closable:
            return
        for cell in self._cells():
            pane = cell.embed_pane()
            if pane is not None and pane.has_focus:
                self._close_spec(cell.spec)
                return
        if self._panes:
            self._close_spec(self._panes[-1])

    def remove_by_keepalive(self, keepalive_name: str) -> None:
        for spec in list(self._panes):
            if spec.keepalive_name == keepalive_name:
                self._close_spec(spec, notify=False)

    def show_new_session_hint(self) -> None:
        self.title_with_project = False
        self.allow_cross_project = False
        self.panes_closable = True
        spec = PaneSpec(session_key="__hint__", cell_id=self._new_cell_id())
        self._panes = []
        self._schedule_mount(
            [
                (
                    spec,
                    {"source": "", "id": "__hint__", "fallback_title": ""},
                    lambda: Text(t("detail.new_session_hint")),
                ),
            ],
        )

    def show_activity_board_empty(self) -> None:
        self.title_with_project = True
        self.allow_cross_project = True
        self.panes_closable = False
        spec = PaneSpec(session_key="__board_empty__", cell_id=self._new_cell_id())
        self._panes = []
        self._schedule_mount(
            [
                (
                    spec,
                    {"source": "", "id": "__board_empty__", "fallback_title": ""},
                    lambda: Text(t("detail.activity_board_empty")),
                ),
            ],
        )

    def show_single_preview(
        self,
        session: dict,
        renderer: Callable[[], Text | str],
    ) -> None:
        key = make_session_key(session)
        import corral

        project = corral._normalize_cwd(session.get("cwd"))
        self.current_project = project
        self.title_with_project = False
        self.allow_cross_project = False
        spec = PaneSpec(session_key=key, cell_id=self._new_cell_id())
        self._schedule_mount(
            [(spec, session, renderer)],
            focus_key=key,
        )

    def ordered_session_keys(self) -> list[str]:
        return [p.session_key for p in self._panes]

    def hosted_identity(self) -> list[tuple[str, str | None]]:
        """当前挂载的有序 (session_key, keepalive_name)，用于判断是否可跳过 remount。"""
        return [(p.session_key, p.keepalive_name) for p in self._panes]

    def show_hosted_group(
        self,
        project: str,
        entries: list[tuple[dict, str | None, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
        focus_pane: bool = False,
        title_with_project: bool = False,
        allow_cross_project: bool = False,
        closable: bool = True,
    ) -> None:
        """entries: (session, keepalive_name, detail_renderer)。

        托管会话的首帧只能是运行时画面或空白底色，不能把 renderer 交给
        EmbedPane 去画（抓帧空档会闪聊天）。renderer 只作为结束后的对话
        回退留在格子上。若 (session_key, keepalive_name) 有序身份与当前
        一致，只就地更新标题，禁止整排 remount（否则会清掉 live `_grid`）。

        `focus_pane`=True 表示调用方带着明确意图（回车打开 / 新建托管成功），
        此时把键盘焦点交给 `focus_key` 那一格；单纯的选择跟随不得传 True。

        `closable`=False 用于活跃会话看板：右栏只被动展示，不露关格 ✕。
        """
        self.current_project = project
        self.title_with_project = title_with_project
        self.allow_cross_project = allow_cross_project
        self.panes_closable = closable
        target_identity = [
            (make_session_key(session), kname) for session, kname, _ in entries
        ]
        if (
            entries
            and self._cells()
            and self.hosted_identity() == target_identity
        ):
            self._update_hosted_group_inplace(
                entries, focus_key=focus_key, focus_pane=focus_pane,
            )
            return
        from corral.models import SHELL_RUNTIME_ID

        specs: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]] = []
        for session, kname, renderer in entries:
            key = make_session_key(session)
            spec = PaneSpec(
                session_key=key,
                keepalive_name=kname,
                cell_id=self._new_cell_id(),
                is_shell=str(session.get("source") or "") == SHELL_RUNTIME_ID,
            )
            specs.append((spec, session, renderer))
        self._panes = [s for s, _, _ in specs]
        self._focus_key = focus_key or (self._panes[0].session_key if self._panes else None)
        self._schedule_mount(
            [(s, sess, r) for s, sess, r in specs],
            focus_key=self._focus_key,
            focus_pane=focus_pane,
        )

    def _update_hosted_group_inplace(
        self,
        entries: list[tuple[dict, str | None, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
        focus_pane: bool = False,
    ) -> None:
        """同身份：更新标题，并确保活跃格不残留静态预览。"""
        cells = self._cells()
        for cell, (session, kname, renderer) in zip(cells, entries, strict=False):
            cell.set_title(self._pane_title(session))
            cell.set_closable(self.panes_closable)
            cell._bind_renderers(renderer, hosted=bool(kname))  # noqa: SLF001
            pane = cell.embed_pane()
            if pane is None:
                continue
            # Live panes must keep _detail_renderer empty so a capture gap
            # cannot flash the transcript. Ended panes already on preview
            # stay on preview.
            if kname:
                live = pane.session_name is not None and not pane.dead
                if live:
                    pane._detail_renderer = None  # noqa: SLF001
                    pane.invalidate_detail()
                elif renderer is not None:
                    self.store.mark_hosted(make_session_key(session), None)
                    cell.spec.keepalive_name = None
                    pane.show_detail(renderer)
                else:
                    pane._detail_renderer = None  # noqa: SLF001
                    pane.invalidate_detail()
            else:
                pane._detail_renderer = renderer  # noqa: SLF001
                pane.invalidate_detail()
        if focus_key:
            self._focus_key = focus_key
        elif self._panes:
            self._focus_key = self._panes[0].session_key
        if focus_pane and self._focus_key:
            # 身份未变不会 remount，焦点不会自己跑过来，必须显式交过去。
            self._request_pane_focus(self._focus_key)
        else:
            self.sync_chrome()

    def add_hosted_pane(
        self,
        session: dict,
        keepalive_name: str,
        renderer: Callable[[], Text | str] | None,
        *,
        focus: bool = False,
        focus_pane: bool = False,
        is_shell: bool = False,
    ) -> None:
        import corral

        key = make_session_key(session)
        project = corral._normalize_cwd(session.get("cwd"))
        if (
            not self.allow_cross_project
            and self.current_project
            and project
            and project != self.current_project
        ):
            self.current_project = project
            self._panes = []
        elif not self.current_project:
            self.current_project = project
        spec = PaneSpec(
            session_key=key,
            keepalive_name=keepalive_name,
            cell_id=self._new_cell_id(),
            is_shell=is_shell,
        )
        existing = [(p, self._find_session(p.session_key)) for p in self._panes]
        rebuild: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]] = []
        for p, sess in existing:
            if sess is None:
                continue
            cell = self._cell_for_spec(p)
            renderer_fn = None
            if cell is not None:
                renderer_fn = cell._ended_fallback  # noqa: SLF001
                if renderer_fn is None and not p.keepalive_name:
                    pane = cell.embed_pane()
                    if pane is not None:
                        renderer_fn = pane._detail_renderer  # noqa: SLF001
            rebuild.append((p, sess, renderer_fn))
        rebuild.append((spec, session, renderer))
        self._panes = [s for s, _, _ in rebuild]
        focus_key = key if focus else self._focus_key
        self._schedule_mount(rebuild, focus_key=focus_key, focus_pane=focus_pane)

    def focus_session_key(self, session_key: str, *, only_live: bool = False) -> bool:
        """把键盘焦点交给指定会话所在格；`only_live` 时只认活着的实时终端。

        自动聚焦一律带 `only_live=True`：静态预览格、已结束的格拿到焦点后用户
        敲的字会直接丢掉，比让他多点一下鼠标糟得多。
        """
        for cell in self._cells():
            if cell.spec.session_key != session_key:
                continue
            pane = cell.embed_pane()
            if only_live and (
                pane is None or not cell.spec.keepalive_name or pane.dead
            ):
                return False
            cell.focus_embed()
            self._focus_key = session_key
            return True
        return False

    def _handle_pane_focused(self, session_key: str) -> None:
        if session_key.startswith("__"):
            return
        if session_key not in {p.session_key for p in self._panes}:
            # 关格后才送到的 DescendantFocus：格子已不在当前分屏里，写回
            # _focus_key 会让选择跟随把右栏切到刚被关掉的会话。
            return
        self._focus_key = session_key
        # 收到真实焦点事件才结束输入归属声明。Widget.focus() 本身是延迟落地的，
        # 在这里之前的任何蒙版同步都必须继续认为右栏可输入。
        self._input_claim_key = None
        self.sync_input_mask()
        self.sync_chrome()
        if self._on_pane_focused is not None:
            self._on_pane_focused(session_key)

    def reconcile_session_keys(self, key_by_keepalive: dict[str, str]) -> None:
        """按 keepalive 名把格子的 session_key 对齐到最新扫描快照（占位→真实）。"""
        for spec in self._panes:
            kname = spec.keepalive_name
            if not kname:
                continue
            mapped = key_by_keepalive.get(kname)
            if mapped:
                spec.session_key = mapped

    def _cells(self) -> list[PaneCell]:
        """当前绑定中的可见格（不含格池闲置格）。"""
        return [c for c in self._pool_cells() if not c._pooled]  # noqa: SLF001

    def _pool_cells(self) -> list[PaneCell]:
        """右栏格子池：含闲置隐藏格，跨组切换时复用。"""
        row = self.query_one("#pane-row", Horizontal)
        return [c for c in row.children if isinstance(c, PaneCell)]

    def _cell_for_spec(self, spec: PaneSpec) -> PaneCell | None:
        for cell in self._pool_cells():
            if cell.spec.cell_id == spec.cell_id:
                return cell
        return None

    def _find_session(self, key: str) -> dict | None:
        return self.store.find_session(key)

    def _new_cell_id(self) -> str:
        import uuid

        return uuid.uuid4().hex[:8]

    def _pane_title(self, session: dict) -> str:
        from corral.models import SHELL_RUNTIME_ID

        source = str(session.get("source") or "")
        if not source:
            return ""
        if source == SHELL_RUNTIME_ID:
            return t("shell.pane_title")
        runtime = self.store.registry.get(source)
        title = self.store.get_title(session)
        if self.title_with_project:
            project = str(session.get("cwd_display") or t("project.unknown"))
            return f"{project} · {title}"
        return f"{runtime.display_name} · {title}"

    def _sync_leading_cells(self) -> None:
        """可见格里最左一格去左边距；闲置格不参与 :first-child，故用手写标记。"""
        active = self._cells()
        for index, cell in enumerate(active):
            cell.set_leading(index == 0)

    def _make_cell(
        self,
        spec: PaneSpec,
        *,
        title: str,
        renderer: Callable[[], Text | str] | None,
    ) -> PaneCell:
        return PaneCell(
            spec,
            title=title,
            on_close=self._close_spec,
            on_focus_list=self._on_focus_list,
            on_pane_focused=self._handle_pane_focused,
            on_restart=self._on_pane_restart,
            on_sync_mask=self.sync_input_mask,
            on_hud_toggle=self._on_hud_toggle,
            osc_report=self._osc_report,
            detail_renderer=renderer,
            closable=self.panes_closable,
        )

    def _remaining_focus_key(self, closed: PaneSpec) -> str | None:
        """关格后右栏该停在哪一格：未关的当前焦点优先，否则左侧邻居。"""
        remaining = [p for p in self._panes if p.cell_id != closed.cell_id]
        if not remaining:
            return None
        if (
            self._focus_key
            and self._focus_key != closed.session_key
            and any(p.session_key == self._focus_key for p in remaining)
        ):
            return self._focus_key
        try:
            idx = next(i for i, p in enumerate(self._panes) if p.cell_id == closed.cell_id)
        except StopIteration:
            return remaining[-1].session_key
        if idx > 0:
            return self._panes[idx - 1].session_key
        return remaining[0].session_key

    def _close_spec(self, spec: PaneSpec, *, notify: bool = True) -> None:
        remaining_focus = self._remaining_focus_key(spec)
        self._panes = [p for p in self._panes if p.cell_id != spec.cell_id]
        self._focus_key = remaining_focus
        if notify:
            self._on_pane_close(spec.session_key)
        # 只把该格收回池里，勿销毁——同伴格与格池本身都要留下来供下次改绑。
        self.call_next(self._park_cell_async, spec)

    async def _park_cell_async(self, spec: PaneSpec) -> None:
        row = self.query_one("#pane-row", Horizontal)
        closing_had_focus = False
        target: PaneCell | None = None
        for cell in list(self._pool_cells()):
            if cell.spec.cell_id == spec.cell_id:
                closing_had_focus = cell.has_focus_within
                target = cell
                break
        if target is not None:
            target.park()
        if not self._panes:
            for cell in self._pool_cells():
                if not cell._pooled:  # noqa: SLF001
                    cell.park()
            if row.query("#pane-row-empty"):
                pass
            else:
                await row.mount(DragonSplash(t("split.empty_hint"), id="pane-row-empty"))
            self.call_after_refresh(self._on_focus_list)
            return
        empty = row.query("#pane-row-empty")
        if empty:
            await empty[0].remove()
        self._sync_leading_cells()
        if closing_had_focus:
            self.call_after_refresh(self._focus_after_close)
        self.call_after_refresh(self.sync_input_mask)

    def _focus_after_close(self) -> None:
        """关掉持有输入的那格后：交给剩余的实时终端，都不行就退回列表。"""
        for key in [self._focus_key, *[p.session_key for p in self._panes]]:
            if key and self.focus_session_key(key, only_live=True):
                return
        self._on_focus_list()

    def _request_pane_focus(self, key: str | None) -> None:
        """登记一次「把输入交给某一格」的明确意图（回车 / 点击 / 托管成功）。

        意图必须能跨过一次还没执行完的整排 remount：鼠标点会话卡时，选择跟随
        先排了一次异步挂载（收尾会把焦点还给列表），紧接着才轮到「打开」事件；
        若这里直接 call_after_refresh 去聚焦，会被后落地的挂载收尾抢回列表——
        真机表现就是「点已托管的会话卡不进右栏，还得再点一下右栏」。
        """
        if not key:
            return
        self._claim_pane_input(key)
        self.call_after_refresh(self._apply_focus_intent)

    def _claim_pane_input(self, key: str) -> None:
        """登记自动聚焦意图，并立刻移除“输入无效”的视觉状态。"""
        self._focus_intent_key = key
        self._input_claim_key = key
        # 焦点组件实际落地要等下一轮刷新；先同步撤去「输入无效」蒙版，避免用户
        # 已点击打开却在这段等待里看到整格灰一下再恢复。
        self.sync_input_mask()

    def clear_focus_intent(self) -> None:
        """用户已经主动决定焦点去哪（回列表 / 点了别处），丢弃待兑现意图。"""
        self._focus_intent_key = None
        self._input_claim_key = None

    def _apply_focus_intent(self) -> bool:
        """兑现待办意图；还有挂载在路上时保留意图，等挂载收尾再试。"""
        key = self._focus_intent_key
        if not key:
            return False
        if self.focus_session_key(key, only_live=True):
            self._focus_intent_key = None
            self._focus_intent_serial += 1
            return True
        if not self._mount_pending:
            # 目标不是实时格（预览 / 已结束 / 托管失败），放弃，不留到下次挂载诈尸
            self._focus_intent_key = None
            self._input_claim_key = None
            self.sync_input_mask()
        return False

    def _settle_focus_intent(self, restore_list: bool, serial: int) -> None:
        """挂载收尾：兑现意图，兑现不了就按挂载前的样子把焦点还给列表。

        两道闸门缺一不可，各管一种时序，都不能单独扛：

        - `serial` 是排这次挂载时的兑现计数。计数变了说明挂载排队期间已经有一次
          明确意图被兑现（点击/回车打开），此时绝不能再把焦点还回列表。这道闸门
          管的是**焦点还没落地**的情况——Textual 的 `Widget.focus()` 走
          `call_later` 延迟生效，那一刻 `any_embed_focused()` 现查还是 False。
        - `any_embed_focused()` 管的是**焦点已经落地**的情况：用户直接点进某个
          内嵌会话、或代码直接调 `EmbedPane.focus()`，都绕过了意图机制、推不动
          serial，于是这里迟到执行时会把焦点从人家刚点进去的格子抢回侧边栏。
          用户表现是「点进内嵌会话，键盘却还在侧边栏」；连带
          `PaneCell._notify_pane_focused` 读到焦点已不在本格而静默丢弃通知，
          侧边栏高亮和右上角会话小窗都停在旧格不动。

        为什么不能只靠在 `DescendantFocus` 上推 serial：那个事件冒泡到本区域是
        **异步**的，实测常常排在本方法之后才送达（`PaneCell` 自己的处理器倒是先
        跑，但它够不着这里的计数）。只有「此刻焦点是不是真在格子里」这个现查是
        可靠的。2026-07-31 由 CI 上确定性失败的
        `test_only_the_active_pane_draws_the_hud` 暴露，同一竞态也是
        `test_focusing_split_pane_highlights_matching_sidebar_session` 长期偶发的根因。
        """
        if self._apply_focus_intent() or self._focus_intent_key:
            return
        if (
            restore_list
            and self._focus_intent_serial == serial
            and not self.any_embed_focused()
        ):
            self._on_focus_list()

    def _schedule_mount(
        self,
        entries: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
        focus_pane: bool = False,
    ) -> None:
        if focus_key:
            self._focus_key = focus_key
        if focus_pane:
            if focus_key:
                self._claim_pane_input(focus_key)
        # 格池已够用且无需新建控件时同步改绑，少一帧「旧画面停住 / 空一帧」。
        # 首次建池或空态清场仍走 async（要 await mount/remove）。
        pool = self._pool_cells()
        need_async = (
            not entries
            or len(pool) < MAX_PANES
            or bool(self.query("#pane-row-empty"))
        )
        if not need_async and entries:
            self._mount_pending += 1
            self._apply_pane_bindings(
                entries, focus_key=focus_key, focus_pane=focus_pane, sync=True,
            )
            return
        self._mount_pending += 1
        self.call_next(
            self._mount_panes_async, entries, focus_key=focus_key, focus_pane=focus_pane,
        )

    def _apply_pane_bindings(
        self,
        entries: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
        focus_pane: bool = False,
        sync: bool = False,
    ) -> None:
        """在已有格池上改绑 / 显隐。`sync=True` 表示同步路径（已计入 _mount_pending）。"""
        focused = getattr(self.app, "focused", None)
        list_had_focus = (
            not focus_pane and focused is not None and _session_list_has_focus(focused)
        )
        serial = self._focus_intent_serial
        self._mount_pending = max(0, self._mount_pending - 1)
        pool = self._pool_cells()
        visible_before = sum(not cell._pooled for cell in pool)  # noqa: SLF001
        composition_changed = visible_before != len(entries)
        target_sizes = self._projected_embed_sizes(len(entries)) if composition_changed else None
        # 先把要用的前缀解冻并改绑，其余收回池里。
        self._panes = [s for s, _, _ in entries]
        for index, (spec, session, renderer) in enumerate(entries):
            title = self._pane_title(session)
            pool[index].rebind(
                spec,
                title=title,
                detail_renderer=renderer,
                target_size=target_sizes[index] if target_sizes is not None else None,
                discard_stale_screen=composition_changed,
            )
            pool[index].set_closable(self.panes_closable)
        for spare in pool[len(entries):]:
            if not spare._pooled:  # noqa: SLF001
                spare.park()
            else:
                spare.set_pooled(True)
        self._sync_leading_cells()
        if self._focus_intent_key or list_had_focus:
            self.call_after_refresh(
                lambda: self._settle_focus_intent(list_had_focus, serial)
            )
        elif focus_key:
            self.call_after_refresh(
                lambda: self.focus_session_key(focus_key, only_live=False)
            )
        self.call_after_refresh(self.sync_input_mask)
        self.call_after_refresh(self.sync_chrome)

    async def _mount_panes_async(
        self,
        entries: list[tuple[PaneSpec, dict, Callable[[], Text | str] | None]],
        *,
        focus_key: str | None = None,
        focus_pane: bool = False,
    ) -> None:
        # remount 会弄丢焦点；列表原先有焦点时挂载后交回，避免 Enter 选不中。
        # 但 focus_pane 是调用方的明确意图（回车打开 / 新建托管成功），优先级
        # 高于「把焦点还回列表」，否则自动聚焦会被这段逻辑立刻撤销。
        focused = getattr(self.app, "focused", None)
        list_had_focus = (
            not focus_pane and focused is not None and _session_list_has_focus(focused)
        )
        serial = self._focus_intent_serial
        self._mount_pending = max(0, self._mount_pending - 1)
        row = self.query_one("#pane-row", Horizontal)
        if not entries:
            for cell in self._pool_cells():
                cell.park()
            if not row.query("#pane-row-empty"):
                # 清掉非格子子节点（旧空态），保留格池控件。
                for child in list(row.children):
                    if not isinstance(child, PaneCell):
                        await child.remove()
                await row.mount(DragonSplash(t("split.empty_hint"), id="pane-row-empty"))
            self._panes = []
            self._focus_intent_key = None
            self._input_claim_key = None
            if list_had_focus:
                self.call_after_refresh(self._on_focus_list)
            return
        # 格池：一次挂满 MAX_PANES，之后跨组只改绑/显隐，2↔4 也不再 remove/mount。
        for child in list(row.children):
            if not isinstance(child, PaneCell):
                await child.remove()
        pool = self._pool_cells()
        while len(pool) < MAX_PANES:
            spare_spec = PaneSpec(
                session_key="__spare__", cell_id=self._new_cell_id(),
            )
            cell = self._make_cell(spare_spec, title="", renderer=None)
            cell.set_pooled(True)
            await row.mount(cell)
            pool = self._pool_cells()
        self._panes = [s for s, _, _ in entries]
        for index, (spec, session, renderer) in enumerate(entries):
            title = self._pane_title(session)
            pool[index].rebind(spec, title=title, detail_renderer=renderer)
            pool[index].set_closable(self.panes_closable)
        for spare in pool[len(entries):]:
            spare.park()
        self._sync_leading_cells()
        if self._focus_intent_key or list_had_focus:
            # 明确意图（可能是这次挂载排队期间才发生的点击/回车）压过「把焦点还回
            # 列表」；意图已在挂载期间兑现时也不能再抢回来，两种情况都由 settle 判。
            self.call_after_refresh(
                lambda: self._settle_focus_intent(list_had_focus, serial)
            )
        elif focus_key:
            self.call_after_refresh(
                lambda: self.focus_session_key(focus_key, only_live=False)
            )
        # 首帧要么已经压暗、要么已经交出焦点，不能等下一次焦点事件才同步。
        self.call_after_refresh(self.sync_input_mask)
        self.call_after_refresh(self.sync_chrome)
