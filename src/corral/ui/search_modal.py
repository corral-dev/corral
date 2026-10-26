"""全文搜索弹窗：在所有会话的对话正文里找关键词，并把命中的那一行显示出来。

和侧边栏筛选框的分工：筛选框负责「按项目 / 标题收窄当前列表」，是常驻的浏览
工具；本弹窗负责「我记得在某个会话里聊过某件事，但想不起是哪个项目」，是一次性
的检索动作，选中后跳回主列表定位到那个会话。

交互上刻意让输入框始终持有焦点：查询框是两行软换行的 TextArea，↑↓ /
PageUp / PageDown 会被转发给结果列表，Enter 直接打开当前高亮项（不会在框里换
行）。用户从头到尾只跟一个输入框打交道，不需要在输入框和列表之间来回切焦点。
"""

from __future__ import annotations

import asyncio
import os

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import ListView, Static, TextArea

from corral.i18n import t
from corral.search import DEFAULT_MAX_LINES, MatchLine, SessionMatch, split_keywords
from corral.ui.modals import OutsideClickDismiss
from corral.ui.session_list import NoSelectListItem

# 输入防抖：正文匹配本身在毫秒级，但每次都要重建结果列表里的控件，连打时没必要
# 每个中间态都重建一遍。窗口取得比选择跟随（120ms）稍长，打字手感更稳。
_DEBOUNCE = 0.15
# 结果列表最多渲染多少条会话，超出的在状态行如实说明，不做静默截断。
_MAX_RESULTS = 60
# 建索引进度回调的节流步长：每处理这么多个会话才更新一次状态行。
_PROGRESS_STEP = 8


def _plural(key: str, count: int) -> str:
    """数量文案：英文要区分单复数，中文不用但也得有同名 key（见 i18n 里的说明）。"""
    if count == 1:
        return t(f"{key}_one")
    return t(key, count=count)


class SearchResultRow(Widget):
    """一条搜索结果：会话抬头两行 + 命中行若干 + 末行间隔。

    高度按内容算死（不用 auto）：这个列表每次查询都会整体重建，固定高度能省掉
    Textual 的自动高度测量，也让滚动位置稳定。
    """

    ALLOW_SELECT = False  # 同 SessionCard：点击是选中动作，不是选文本

    COMPONENT_CLASSES = {"search-result--hit"}

    DEFAULT_CSS = """
    SearchResultRow {
        width: 1fr;
        color: $foreground 80%;
        pointer: pointer;
    }
    SearchResultRow .search-result--hit {
        color: $warning;
        text-style: bold;
    }
    """

    def __init__(self, match: SessionMatch, store) -> None:
        super().__init__()
        self.match = match
        self._store = store
        # 抬头 2 行 + 命中行 + 末行间隔（间隔画在本控件高度内，见侧边栏末行间隔约定）
        self.styles.height = 2 + len(match.lines) + 1

    def render(self) -> Text:
        import corral
        from corral.models import SHELL_RUNTIME_ID, is_shell_session

        match = self.match
        session = match.session
        width = max(10, self.size.width or 60)
        if is_shell_session(session):
            # 终端 pane 不挂运行时；搜索可能按标题命中它（如搜「终端」），
            # 这里直接用「终端」标签渲染，不查注册表。
            runtime_id = SHELL_RUNTIME_ID
            runtime_name = t("shell.pane_title")
            runtime_style = "dim"
        else:
            runtime = self._store.registry.get(str(session.get("source") or ""))
            runtime_id = getattr(runtime, "id", None) or str(session.get("source") or "")
            runtime_name = runtime.display_name
            runtime_style = corral.runtime_label_style(runtime_id)
        is_running = bool(session.get("keepalive_name")) or bool(session.get("live"))

        project_path = corral._normalize_cwd(session.get("cwd"))
        project = (
            os.path.basename(project_path)
            if project_path
            else str(session.get("cwd_display") or t("project.unknown"))
        )

        out = Text()
        heading = corral._fit_cell(f"{project}: {match.title}", width)
        content_len = len(heading.rstrip(" "))
        out.append(heading)
        if content_len > 0:
            out.stylize("bold #3F9A6A" if is_running else "bold", 0, content_len)

        out.append("\n")
        meta = Text()
        meta.append(runtime_name, style=runtime_style)
        meta.append(" · ", style="dim")
        meta.append(corral._format_relative_time(session.get("mtime") or 0), style="dim")
        if match.total_hits:
            meta.append(" · ", style="dim")
            meta.append(_plural("search.hit_count", match.total_hits), style="dim")
        elif match.meta_only:
            meta.append(" · ", style="dim")
            meta.append(t("search.title_only"), style="dim")
        out.append(meta)

        hit_style = self.get_component_rich_style("search-result--hit")
        for line in match.lines:
            out.append("\n")
            out.append(self._render_line(line, width, hit_style))
        out.append("\n")  # 末行间隔，算进本项命中区
        return out

    def _render_line(self, line: MatchLine, width: int, hit_style) -> Text:
        """单条命中行：角色符号 + 正文，关键词按主题色高亮。

        角色只用 ● / ◆ 区分（与对话预览同一套符号），不重复写运行时名——抬头那
        行已经给过了，这里每行再挂一遍只会把有限的宽度吃光。
        """
        import corral

        marker = "  ● " if line.role == "user" else "  ◆ "
        body_width = max(4, width - corral._text_width(marker))
        body = corral._fit_cell(line.text, body_width)
        text = Text(marker, style="dim")
        text.append(body, style="dim" if line.role == "assistant" else "")
        offset = len(marker)
        for start, end in line.spans:
            if start >= len(body):
                break
            text.stylize(hit_style, offset + start, offset + min(end, len(body)))
        return text


class FullTextSearchModal(OutsideClickDismiss, ModalScreen[str | None]):
    """返回选中的会话键；Esc 或点框外空白返回 None。"""

    # Enter / ↑↓ 必须 priority：否则两行 TextArea 会先吃掉这些键（换行、移光标），
    # 用户就无法在输入框持焦时直接打开结果或挪高亮。
    BINDINGS = [
        Binding("escape", "cancel", show=False, priority=True),
        Binding("enter", "confirm", show=False, priority=True),
        Binding("up", "results_up", show=False, priority=True),
        Binding("down", "results_down", show=False, priority=True),
        Binding("pageup", "results_page_up", show=False, priority=True),
        Binding("pagedown", "results_page_down", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    FullTextSearchModal {
        align: center middle;
    }
    FullTextSearchModal > Vertical {
        width: 84;
        max-width: 92%;
        height: 32;
        max-height: 86%;
        border: round $primary;
        background: $surface;
        padding: 0 1;
    }
    /* Textual 的 TextArea 默认 tall 边框，且 :focus 权重更高——两个状态都清掉，
       避免弹窗顶部「外框套内框」。compact 去掉边框类，这里再钉死高度与底色。 */
    FullTextSearchModal TextArea,
    FullTextSearchModal TextArea:focus {
        border: none;
        padding: 0 1;
        margin: 0;
        height: 2;
        background: $panel;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
    }
    FullTextSearchModal TextArea .text-area--cursor-line {
        background: $panel;
    }
    FullTextSearchModal #search-status {
        height: 1;
        color: $foreground 60%;
    }
    FullTextSearchModal ListView {
        height: 1fr;
        background: $surface;
        scrollbar-size-vertical: 0;
    }
    FullTextSearchModal .hint {
        height: 1;
        color: $foreground 60%;
    }
    """

    def __init__(self, store, index, initial_query: str = "") -> None:
        super().__init__()
        self.store = store
        self.index = index
        self._initial_query = initial_query
        self._debounce_timer = None
        self._matches: list[SessionMatch] = []
        self._indexing = False
        self._index_failed = False
        self._progress: tuple[int, int] | None = None
        # 结果列表重建的串行闸门：请求来自两条互不相让的消息泵——防抖定时器跑在
        # Screen 泵，建索引完成经 call_from_thread 跑在 App 泵。`clear()` 与
        # `extend()` 交错执行会把同一批结果重复挂上去（会话列表那边同样的并发
        # 曾经真机崩过，见 ui/session_list.py 的 `_rebuild_lock`）。
        self._results_lock = asyncio.Lock()
        self._rebuild_seq = 0
        self._total = 0

    def compose(self) -> ComposeResult:
        with Vertical():
            yield TextArea(
                self._initial_query,
                id="search-query",
                soft_wrap=True,
                show_line_numbers=False,
                compact=True,
                highlight_cursor_line=False,
                placeholder=t("search.placeholder"),
            )
            yield Static("", id="search-status")
            yield ListView(id="search-results")
            yield Static(t("search.hint"), classes="hint")

    def on_mount(self) -> None:
        self.query_one("#search-query", TextArea).focus()
        # A recent-window index is not complete history. Once the full corpus
        # has a prepared snapshot, show it immediately while refreshing in the
        # background; changed histories must not delay repeated queries.
        catalog = getattr(self.store, "search_catalog", None)
        sessions = getattr(self.store, "search_sessions", self.store.all_sessions)()
        self._indexing = not (
            (catalog is None or catalog.ready) and self.index.covers(sessions)
        )
        if self._indexing:
            self._update_status()
        else:
            self._run_search()
        self._build_index()

    # ---- 建索引 ----

    @work(thread=True, group="search-index")
    def _build_index(self) -> None:
        """Prepare historical metadata and body entries outside the UI thread."""
        from textual.worker import get_current_worker

        worker = get_current_worker()

        def progress(done: int, total: int) -> None:
            if not self._indexing or worker.is_cancelled:
                return  # 已就绪时这轮是静默增量刷新，不打扰状态行
            if done % _PROGRESS_STEP and done != total:
                return
            self.app.call_from_thread(self._on_index_progress, done, total)

        failed = False
        try:
            prepare = getattr(self.store, "prepare_search", None)
            if callable(prepare):
                prepare()
            if worker.is_cancelled:
                return
            sessions = getattr(self.store, "search_sessions", self.store.all_sessions)()
            self.index.refresh(self.store, sessions, progress=progress)
        except Exception as exc:
            import corral

            corral._log_embed_error("Full-text search preparation", exc)
            failed = True
        if not worker.is_cancelled:
            self.app.call_from_thread(self._on_index_done, failed)

    def _on_index_progress(self, done: int, total: int) -> None:
        if not self.is_attached:
            return
        self._progress = (done, total)
        self._update_status()

    def _on_index_done(self, failed: bool = False) -> None:
        # 弹窗可能在建索引期间就被关掉了；此时控件已卸载，再去 query_one 会抛错，
        # 而且这次刷新的结果也没人看。后台线程无法感知这一点，只能在回调里挡。
        if not self.is_attached:
            return
        self._indexing = False
        self._index_failed = failed
        self._progress = None
        self._run_search()

    # ---- 查询 ----

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "search-query":
            return
        if self._index_failed and not self._indexing:
            self._indexing = True
            self._index_failed = False
            self._update_status()
            self._build_index()
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
        self._debounce_timer = self.set_timer(_DEBOUNCE, self._run_search)

    def _query(self) -> str:
        return self.query_one("#search-query", TextArea).text

    def _run_search(self) -> None:
        self._debounce_timer = None
        if self._indexing:
            return
        outcome = self.index.search(
            getattr(self.store, "search_sessions", self.store.all_sessions)(),
            self._query(),
            titles=self.store.snapshot(),
            max_lines=DEFAULT_MAX_LINES,
            top=_MAX_RESULTS,
        )
        self._matches = list(outcome.matches)
        self._total = outcome.total
        self._update_status()
        self.call_next(self._rebuild_results)

    async def _rebuild_results(self) -> None:
        """重建结果列表。

        `clear()` / `extend()` 必须 await：Textual 的 `ListView.clear()` 是投递
        Prune 消息异步移除，而挂载是同步进 DOM 的。不等它落地就设 `index = 0`，新
        旧子项会共存一小段时间，而 `index` 指向的还是**旧**子项——实测这个窗口在
        168 个会话时 70~191ms、461 个会话时最长到 983ms，用户"打完字立刻回车"正好
        落在里面，打开的会是一个他没在看的会话。

        锁 + 序号让位的原因同 `SessionListView.rebuild()`：重建请求来自两条互不相
        让的消息泵（防抖定时器在 Screen 泵、建索引完成经 call_from_thread 在 App
        泵），交错执行会把结果重复挂上去。排队期间来了更新的查询就直接放弃本次。
        """
        self._rebuild_seq += 1
        seq = self._rebuild_seq
        async with self._results_lock:
            if seq != self._rebuild_seq or not self.is_attached:
                return
            results = self.query_one("#search-results", ListView)
            await results.clear()
            rows = [NoSelectListItem(SearchResultRow(match, self.store)) for match in self._matches]
            if rows:
                await results.extend(rows)
                results.index = 0

    def _update_status(self) -> None:
        status = self.query_one("#search-status", Static)
        if self._indexing:
            if self._progress is None:
                status.update(t("filter.searching"))
            else:
                done, total = self._progress
                status.update(t("search.indexing", done=done, total=total))
            return
        if self._index_failed:
            status.update(t("search.failed"))
            return
        if not split_keywords(self._query()):
            status.update(_plural("search.idle", self.index.indexed_count))
            return
        if self._total > len(self._matches):
            status.update(t("search.truncated", shown=len(self._matches), total=self._total))
        elif not self._total:
            status.update(t("search.result_count_zero"))
        else:
            status.update(_plural("search.result_count", self._total))

    # ---- 按键 ----

    def _selected_key(self) -> str | None:
        """回车会打开哪个会话——**以高亮那一行控件自己持有的会话为准**。

        不要改成「拿 `ListView.index` 去索引 `self._matches`」：那是两份可能不同步
        的数据。`ListView.clear()` 是投递 Prune 消息异步移除的，重建期间 DOM 里可能
        还留着上一批结果，而 `_matches` 已经是新的了——同一个下标就指向了两个不同
        的会话，用户看到高亮在 A、回车却打开 B。从控件本身取就没有这个缝。
        """
        results = self.query_one("#search-results", ListView)
        item = results.highlighted_child
        if item is None:
            return None
        rows = item.query(SearchResultRow)
        return rows.first().match.key if rows else None

    def _results(self) -> ListView:
        return self.query_one("#search-results", ListView)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_confirm(self) -> None:
        key = self._selected_key()
        if key is None:
            self.app.bell()
        else:
            self.dismiss(key)

    def action_results_up(self) -> None:
        self._results().action_cursor_up()

    def action_results_down(self) -> None:
        self._results().action_cursor_down()

    def action_results_page_up(self) -> None:
        self._results().action_page_up()

    def action_results_page_down(self) -> None:
        self._results().action_page_down()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """鼠标点选结果直接打开。"""
        key = self._selected_key()
        if key is not None:
            self.dismiss(key)
