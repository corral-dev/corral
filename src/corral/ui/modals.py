"""通用选择/确认弹窗：取代旧版 curses 手绘的 _pick_menu / _draw_runtime_menu /
_confirm_kill_keepalive。业务规则（运行时可用性、默认高亮项、文案）保持不变，
只是从「手画方框 + 内部按键循环」换成 Textual 的 ModalScreen + ListView。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from rich.text import Text
from textual import errors, events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Label, ListView, Static

from corral.i18n import t
from corral.projects import fuzzy_match
from corral.textutil import fit_cell, text_width
from corral.ui.session_list import NoSelectListItem


class OutsideClickDismiss:
    """点弹窗主体以外的空白处＝取消，和 Esc 等价。

    弹窗铺满整屏、内容居中，中间那块框之外全是背景。鼠标用户对这块区域的直觉
    就是「点一下关掉」；只留 Esc 一条出口，用鼠标操作时会觉得界面卡住了。

    判定必须**现查落点控件**（`get_widget_at`），不能只看事件有没有到这里：
    Click 会从列表项、输入框一路冒泡上来，光凭「收到了」会把点在弹窗内容上的
    每一下都当成点在外面，弹窗一点就关。

    子类用 `outside_click_result` 声明取消时回给调用方的值（默认 `None`，
    确认框这种返回布尔的要改成 `False`）。放在 `ModalScreen` 之前继承。
    """

    outside_click_result = None

    def on_click(self, event: events.Click) -> None:
        try:
            hit, _ = self.get_widget_at(event.screen_x, event.screen_y)
        except errors.NoWidget:
            return
        if hit is self:
            event.stop()
            self.dismiss(self.outside_click_result)


class _ChoiceItem(Static):
    # 菜单项文字没有选择/复制的使用场景，关掉避免和 SessionCard 同类的潜在风险
    # （见 ui/app.py 里 CorralApp 的说明）。
    ALLOW_SELECT = False

    DEFAULT_CSS = """
    _ChoiceItem {
        pointer: pointer;
    }
    """

    def __init__(self, main: str, hint: str, available: bool) -> None:
        style = "" if available else "dim"
        text = Text(main, style=style)
        if hint:
            text.append("  " + hint, style="dim")
        super().__init__(text)
        self.available = available


_MENU_CSS = """
    RuntimePickerModal {
        align: center middle;
    }
    RuntimePickerModal > Vertical {
        width: auto;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 0 1;
    }
    RuntimePickerModal ListView {
        height: auto;
        max-height: 20;
    }
    """


@dataclass
class RuntimeChoice:
    id: str
    label: str
    action_text: str
    available: bool
    # 不可用时的提示文案；默认用「未安装」，但不可用原因不是没装（如重启会话
    # 需要会话正被 corral 托管）时得换说法，不能误导用户去装东西。
    unavailable_text: str | None = None


class RuntimePickerModal(OutsideClickDismiss, ModalScreen[str | None]):
    """运行时选择弹窗（高级操作接力用）。返回 runtime id；Esc 或点框外空白返回 None。"""

    DEFAULT_CSS = _MENU_CSS

    def __init__(self, title: str, choices: list[RuntimeChoice], default_index: int = 0) -> None:
        super().__init__()
        self._title = title
        self._choices = choices
        self._default_index = default_index

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f" {self._title} ", classes="title")
            items = []
            for choice in self._choices:
                if choice.available:
                    action = choice.action_text
                else:
                    action = choice.unavailable_text or t(
                        "modal.not_installed", action=choice.action_text
                    )
                items.append(NoSelectListItem(_ChoiceItem(f"{choice.label:<10}", action, choice.available)))
            yield ListView(*items, initial_index=self._default_index)
            yield Label(t("modal.menu_hint"), classes="hint")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        list_view = self.query_one(ListView)
        index = list_view.index
        if index is None:
            return
        choice = self._choices[index]
        if not choice.available:
            self.app.bell()
            return
        self.dismiss(choice.id)

    def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.dismiss(None)


def _tail(text: str, width: int) -> str:
    """路径这类「越靠后越关键」的文案：放不下时砍开头，保住结尾那几级目录。"""
    if text_width(text) <= width or width <= 1:
        return text
    body = text
    while body and text_width(body) > width - 1:
        body = body[1:]
    return "…" + body


def _short_path(path: str) -> str:
    home = os.path.expanduser("~")
    if home and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


class _ColumnRow(Widget):
    """分栏弹窗里的一行：主文案 + 灰色补充说明，按当前栏宽截成单行。

    行高写死 1：`Static` 默认按内容折行，项目路径一长就把行撑成两行，整列的
    高度和滚动位置跟着抖；这里自己按栏宽截断（与会话卡、搜索结果同一套做法）。
    """

    ALLOW_SELECT = False  # 同 SessionCard：点击是选中动作，不是选文本

    DEFAULT_CSS = """
    _ColumnRow {
        width: 1fr;
        height: 1;
    }
    """

    def __init__(self, value: str, main: str, hint: str = "", available: bool = True) -> None:
        super().__init__()
        self.value = value
        self.main = main
        self.hint = hint
        self.available = available

    def render(self) -> Text:
        width = max(4, self.size.width or 24)
        main_width = min(text_width(self.main), width)
        text = Text(
            fit_cell(self.main, main_width, ellipsis=True),
            style="" if self.available else "dim",
        )
        rest = width - main_width - 2
        if self.hint and rest >= 4:
            text.append("  " + _tail(self.hint, rest), style="dim")
        return text


class NewSessionModal(OutsideClickDismiss, ModalScreen[tuple[str, str] | None]):
    """新建会话：左栏选项目、右栏选运行时，一个弹窗里一次选完。

    左栏更宽——项目名后面还要跟路径，信息量远大于右栏的助手名。交互上
    ←→ 换栏、↑↓ 选行；左栏回车表示「项目定了，去选助手」，右栏回车才真正
    确认。左栏顶有本地筛选框：**打开即聚焦，立刻可打字**收窄项目名 / 路径
    （`projects.fuzzy_match`）；项目列表持焦时 `/` 仍可回到筛选框。查询串
    不写回侧边栏 `NavState.project_query`，但打开时可带入侧边栏当前筛选作初值。

    返回 (项目目录, 运行时 id)；Esc 或点框外空白返回 None。
    """

    DEFAULT_CSS = """
    NewSessionModal {
        align: center middle;
    }
    NewSessionModal > Vertical {
        width: 92;
        max-width: 94%;
        height: 24;
        max-height: 86%;
        border: round $primary;
        background: $surface;
        padding: 0 1;
    }
    NewSessionModal .title {
        height: 1;
    }
    NewSessionModal #ns-columns {
        height: 1fr;
    }
    NewSessionModal #ns-project-column {
        width: 2fr;
        height: 1fr;
    }
    /* 同全文搜索弹窗：压住 Input 默认 tall 边框，避免外框套内框。 */
    NewSessionModal #ns-project-filter,
    NewSessionModal #ns-project-filter:focus {
        border: none;
        padding: 0 1;
        margin: 0;
        height: 1;
        background: $panel;
    }
    NewSessionModal #ns-projects {
        height: 1fr;
    }
    NewSessionModal #ns-runtimes {
        width: 1fr;
    }
    NewSessionModal ListView {
        height: 1fr;
        background: $surface;
        border: round $foreground 20%;
        padding: 0 1;
        scrollbar-size-vertical: 0;
    }
    /* 哪一栏正在接受方向键，只能靠边框颜色告诉用户；未聚焦那栏的高亮行仍要
       看得见（Textual 默认就会画成较淡的一档），不要整栏压暗。 */
    NewSessionModal ListView:focus {
        border: round $primary;
    }
    NewSessionModal .hint {
        height: 1;
        color: $foreground 60%;
    }
    """

    def __init__(
        self,
        projects: list[tuple[str, str, str]],
        runtimes: list[RuntimeChoice],
        project_index: int = 0,
        runtime_index: int = 0,
        initial_query: str = "",
    ) -> None:
        super().__init__()
        self._projects = projects
        self._runtimes = runtimes
        self._runtime_index = runtime_index
        self._initial_query = initial_query
        preferred = (
            projects[project_index][0]
            if projects and 0 <= project_index < len(projects)
            else None
        )
        self._visible = self._matching_projects(initial_query)
        self._project_index = self._index_of_cwd(self._visible, preferred)

    @staticmethod
    def _index_of_cwd(
        projects: list[tuple[str, str, str]], cwd: str | None
    ) -> int:
        if not cwd:
            return 0
        for i, (cwd_key, _, _) in enumerate(projects):
            if cwd_key == cwd:
                return i
        return 0

    def _matching_projects(self, query: str) -> list[tuple[str, str, str]]:
        needle = (query or "").strip()
        if not needle:
            return list(self._projects)
        out: list[tuple[str, str, str]] = []
        for cwd_key, label, hint in self._projects:
            if fuzzy_match(needle, label, cwd_key, _short_path(hint)):
                out.append((cwd_key, label, hint))
        return out

    @staticmethod
    def _project_items(
        projects: list[tuple[str, str, str]],
    ) -> list[NoSelectListItem]:
        return [
            NoSelectListItem(_ColumnRow(cwd_key, label, _short_path(hint)))
            for cwd_key, label, hint in projects
        ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f" {t('modal.new_session_title')} ", classes="title")
            with Horizontal(id="ns-columns"):
                with Vertical(id="ns-project-column"):
                    yield Input(
                        value=self._initial_query,
                        placeholder=t("modal.project_filter_placeholder"),
                        id="ns-project-filter",
                        select_on_focus=False,
                    )
                    yield ListView(
                        *self._project_items(self._visible),
                        id="ns-projects",
                        initial_index=self._project_index,
                    )
                yield ListView(
                    *[
                        NoSelectListItem(
                            _ColumnRow(
                                choice.id,
                                choice.label,
                                "" if choice.available else t("modal.not_installed_tag"),
                                choice.available,
                            )
                        )
                        for choice in self._runtimes
                    ],
                    id="ns-runtimes",
                    initial_index=self._runtime_index,
                )
            yield Label(t("modal.two_column_hint"), classes="hint")

    def on_mount(self) -> None:
        projects = self.query_one("#ns-projects", ListView)
        projects.border_title = t("modal.column_project")
        self.query_one("#ns-runtimes", ListView).border_title = t("modal.column_runtime")
        # 打开即钉在筛选框：用户立刻打字搜项目。Input 虽排在左栏最前，Textual
        # 仍可能把默认焦点给 ListView；用 Screen.set_focus 同步钉住，避免
        # Widget.focus()/call_later 被抢走。有初值时把光标放到末尾，方便接着打。
        filt = self.query_one("#ns-project-filter", Input)
        self.set_focus(filt)
        if filt.value:
            filt.cursor_position = len(filt.value)

    # ---- 筛选 ----

    def _selected_cwd(self) -> str | None:
        row = self._row("#ns-projects")
        return row.value if row is not None else None

    def _rebuild_projects(self, query: str) -> None:
        """按查询重建左栏；尽量保住当前选中的 cwd，否则落到第一项。"""
        keep_cwd = self._selected_cwd()
        self._visible = self._matching_projects(query)
        projects = self.query_one("#ns-projects", ListView)
        filter_input = self.query_one("#ns-project-filter", Input)
        filter_focused = filter_input.has_focus
        list_focused = projects.has_focus
        projects.clear()
        if self._visible:
            projects.extend(self._project_items(self._visible))
            projects.index = self._index_of_cwd(self._visible, keep_cwd)
        if list_focused and self._visible:
            projects.focus()
        elif filter_focused:
            filter_input.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "ns-project-filter":
            return
        # 以控件现值为准（不要信可能滞后的 event.value）：带初值挂载时 Textual
        # 可能先派发空串 Changed，若按事件值重建会把 __init__ 已收窄的列表冲宽，
        # 高负载下断言/导航都会偶发失败。列表已与「按现值应收」一致则跳过，
        # 避免无意义 clear/extend 抢走筛选框焦点。
        query = event.input.value
        new_visible = self._matching_projects(query)
        if [item[0] for item in new_visible] == [item[0] for item in self._visible]:
            return
        self._rebuild_projects(query)

    # ---- 选择 ----

    def _row(self, list_id: str) -> _ColumnRow | None:
        item = self.query_one(list_id, ListView).highlighted_child
        if item is None:
            return None
        rows = item.query(_ColumnRow)
        return rows.first() if rows else None

    def _confirm(self) -> None:
        project = self._row("#ns-projects")
        runtime = self._row("#ns-runtimes")
        if project is None or runtime is None or not runtime.available:
            self.app.bell()
            return
        self.dismiss((project.value, runtime.value))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """回车 / 点击：在项目栏是「选好了，去挑助手」，在运行时栏才是确认。"""
        if event.list_view.id == "ns-projects":
            self.query_one("#ns-runtimes", ListView).focus()
            return
        self._confirm()

    def _filter_focused(self) -> bool:
        return self.query_one("#ns-project-filter", Input).has_focus

    def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            filt = self.query_one("#ns-project-filter", Input)
            if filt.has_focus and filt.value:
                filt.value = ""
                return
            self.dismiss(None)
            return
        if event.key == "slash" and self.query_one("#ns-projects", ListView).has_focus:
            # 与侧边栏「/ 聚焦筛选」同手感；项目列表持焦时 `/` 不当成可打印字符。
            event.stop()
            event.prevent_default()
            self.query_one("#ns-project-filter", Input).focus()
            return
        if self._filter_focused():
            # 筛选框持焦时 ←→ 无效，避免误跳过项目栏直接进运行时。
            if event.key in ("left", "right"):
                event.stop()
                event.prevent_default()
                return
            if event.key in ("down", "enter"):
                event.stop()
                event.prevent_default()
                projects = self.query_one("#ns-projects", ListView)
                if self._visible:
                    projects.focus()
                else:
                    self.app.bell()
                return
            return
        if event.key in ("left", "right"):
            event.stop()
            event.prevent_default()
            target = "#ns-projects" if event.key == "left" else "#ns-runtimes"
            self.query_one(target, ListView).focus()


class ConfirmModal(OutsideClickDismiss, ModalScreen[bool]):
    """confirm_key 确认 / 其他键取消的确认框，取代 _confirm_kill_keepalive。

    打开瞬间会短暂忽略按键：触发弹窗的动作键（结束会话是 `q`，删除会话是 `x`）
    若同一按键落到弹窗里会立刻被当成确认。挂载后等一帧再接收确认/取消。

    点框外空白同样算取消（返回 False）。
    """

    outside_click_result = False

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > Vertical {
        /* 固定基准宽 + 窄终端时按比例收，配合下面的 1fr 让长文案折行。
           曾经是 width: auto：auto 宽按最长一行算出来，再被 max-width 裁掉，
           于是超长确认文案（如"会话正在别的窗口运行"那条）直接被截断半句。 */
        width: 64;
        max-width: 90%;
        height: auto;
        border: round $warning;
        padding: 1 2;
    }
    ConfirmModal Label {
        width: 1fr;
    }
    """

    def __init__(self, message: str, confirm_key: str = "q") -> None:
        super().__init__()
        self._message = message
        self._confirm_key = confirm_key
        self._armed = False

    def compose(self) -> ComposeResult:
        from corral.i18n import t

        with Vertical():
            yield Label(self._message)
            yield Label(t("modal.confirm_hint", confirm_key=self._confirm_key), classes="hint")

    def on_mount(self) -> None:
        self.call_after_refresh(self._arm)

    def _arm(self) -> None:
        self._armed = True

    def _on_key(self, event: events.Key) -> None:
        event.stop()
        if not self._armed:
            return
        self.dismiss(event.key in (self._confirm_key, self._confirm_key.upper()))

    def on_click(self, event: events.Click) -> None:
        # 武装期同样挡住鼠标：和按键一个道理，别让触发这个框的那一下顺手把它关掉。
        if not self._armed:
            return
        super().on_click(event)


# ---------------------------------------------------------------------------
# 业务流程封装：project/runtime 选择 + 新建会话组合流程
# ---------------------------------------------------------------------------

# 高级操作里非运行时选项的哨兵 id。
EXPORT_SESSION_CHOICE = "__export_session__"
COPY_SESSION_CHOICE = "__copy_session__"
RESTART_SESSION_CHOICE = "__restart_session__"
_ADVANCED_SENTINELS = frozenset(
    {EXPORT_SESSION_CHOICE, COPY_SESSION_CHOICE, RESTART_SESSION_CHOICE}
)


def _handoff_default_index(choices: list[RuntimeChoice], source: str) -> int:
    """默认高亮第一个已安装的其他助手；没有则回来源助手；都没有才 0。"""
    other = next(
        (
            i for i, choice in enumerate(choices)
            if choice.id not in _ADVANCED_SENTINELS and choice.id != source and choice.available
        ),
        None,
    )
    if other is not None:
        return other
    same = next((i for i, choice in enumerate(choices) if choice.id == source), None)
    return 0 if same is None else same


async def choose_target_runtime(app, store, source: str, restart_available: bool = False) -> str | None:
    """高级操作：导出会话、复制会话、重启会话，或选择接力目标运行时。

    列表第一项是「导出会话」（写 share transcript 并把路径复制到剪贴板，不启动）；
    第二项是「复制会话」（同助手完整克隆）；第三项是「重启会话」（结束托管进程后
    按原会话原地恢复，仅对 corral 正托管、非占位的会话可用）；其后每一个助手
    （含来源自身）都是「读取源历史后新建会话」--同助手另起用于原会话卡住 / 出 bug 时；
    真正的原生恢复走侧边栏回车，不走本入口。
    仅展示 ACTIVE_RUNTIME_IDS 内的目标，dormant 兼容不出现在接力菜单。
    """
    from corral.runtime.registry import active_runtimes

    runtimes = active_runtimes(store.registry)
    source_runtime = store.registry.get(source)
    source_name = source_runtime.display_name
    restart_action = t("modal.restart_session_action")
    choices = [
        RuntimeChoice(
            EXPORT_SESSION_CHOICE,
            t("modal.export_session"),
            t("modal.export_session_action"),
            True,
        ),
        RuntimeChoice(
            COPY_SESSION_CHOICE,
            t("modal.copy_session"),
            t("modal.copy_session_action"),
            source_runtime.is_available(),
        ),
        RuntimeChoice(
            RESTART_SESSION_CHOICE,
            t("modal.restart_session"),
            restart_action,
            restart_available,
            unavailable_text=t("modal.not_hosted", action=restart_action),
        ),
    ]
    for runtime in runtimes:
        action = t("modal.read_history_new", source=source_name)
        choices.append(RuntimeChoice(runtime.id, runtime.display_name, action, runtime.is_available()))
    return await app.push_screen_wait(
        RuntimePickerModal(
            t("modal.handoff_title"), choices, _handoff_default_index(choices, source),
        )
    )


def _project_entries(store) -> list[tuple[str, str, str]]:
    """新建会话可选的项目：(目录, 项目名, 目录)，当前目录不在列表里时补到最前。"""
    entries: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for project in store.projects():
        cwd_key = project["cwd_key"]
        if not cwd_key or cwd_key in seen:
            continue
        seen.add(cwd_key)
        entries.append((cwd_key, project["label"], cwd_key))
    current = os.getcwd()
    if current not in seen:
        entries.insert(0, (current, t("project.current_dir"), current))
    return entries


async def new_session_flow(app, store, nav, session: dict | None):
    import corral

    entries = _project_entries(store)
    if not entries:
        return None
    preferred = corral._new_session_cwd(store, nav, session)
    project_index = next(
        (i for i, (cwd, _, _) in enumerate(entries) if preferred and cwd == preferred), 0
    )

    from corral.runtime.registry import active_runtimes

    runtimes = active_runtimes(store.registry)
    choices = [
        RuntimeChoice(runtime.id, runtime.display_name, "", runtime.is_available())
        for runtime in runtimes
    ]
    runtime_index = next(
        (i for i, runtime in enumerate(runtimes) if runtime.id == nav.source and runtime.is_available()),
        next((i for i, runtime in enumerate(runtimes) if runtime.is_available()), 0),
    )

    picked = await app.push_screen_wait(
        NewSessionModal(
            entries,
            choices,
            project_index,
            runtime_index,
            initial_query=getattr(nav, "project_query", "") or "",
        )
    )
    if picked is None:
        return None
    cwd_key, target = picked
    cwd = corral.usable_cwd(cwd_key)
    if cwd is None:
        # 项目目录已经不在这台机器上（换机 / 被删）——新建会话没有落脚点。
        app.bell()
        return None
    return corral.NewSessionRequest(target, cwd)
