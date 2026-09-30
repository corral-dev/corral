"""会话列表：左栏会话卡片 + 顶部新建项，取代旧版 curses 手绘表格。

侧边栏布局硬约定（凡往左栏加控件都必须遵守，见 AGENTS.md / MAINTAINER_GUIDE）：
搜索框/新建项最后一行是间隔空行，画在控件自身高度内并算进命中区；禁止用 margin
或兄弟空隙做分隔。当前：搜索框高 2、新建项高 2、活跃会话看板高 3（首行名称 /
第二行上一页·下一页 / 第三行留白）、会话卡高 3（标题 / 运行时 /
时间；首行最左是关注状态圆点、随后是「项目 标题」，运行时与时间各自靠右，
无末行空行）。筛选框在列表外固定；`＋ 新建` 和活跃会话看板在 `#sidebar-sticky` 里也不随
列表滚。置顶块、Pinned 分隔线与未置顶日期段都在 `#sidebar-scroll`
里一起滚——置顶只改变排序，不冻在视口里。鼠标在固定头（含筛选框）上滚轮
仍带动会话列表，顶部位置不变。
置顶块与未置顶块都非空时，中间插一行居中 `Pinned↑`/`置顶↑` 的
`$primary` 蓝横线；未置顶按本地日历日切桶（今天 / 昨天 / 近 7 日内其余各日用星期几
/ 更早合成一桶），桶内不重排。命名桶后面还有内容时才在该桶末尾插线（标签为
`Today↑` 这种「名字 + 向上箭头」，标明上面这一段）。分隔高 1、disabled、键盘跳过；
禁止在日期分隔线上写 Older/其他标签。默认只展开今天与昨天；更早收成高 3 的
「三层叠卡」（`OlderStackCard`：带框的正面卡片 + 右侧两道叠边，内含 ▶/▼ 与数量），
点击或回车展开。项目筛选命中时
自动展开，避免搜到的旧会话被藏住。斑马纹按**块**交替，
不是按卡片：独立会话一块，会话组（组卡 + 全部成员）一块，三层叠卡也是一块；
`＋ 新建`、活跃会话看板与分隔线不参与、不计入相位，分隔线之后相位重置（其后一区从无条纹
起头）。条纹画在 `SessionCard` / `SessionGroupCard` / `OlderStackCard` 上，用
`$foreground` 的半透明底与下层选中/分屏底色合成；禁止写到 `ListItem` 上——子类
DEFAULT_CSS 会压过 ListView 自带的 `.-highlight`，把选中底色吃掉。光标停在会话组卡上时，
组卡和全部成员贴 `-group-selected`（整组高光），激活格对应成员再叠 `-split-active`。

业务格式化逻辑（相对时间、宽字符对齐、标题兜底）直接复用 corral.py 里已测试的
纯函数，这里只负责「怎么在 Textual 里画卡片、怎么响应选择」。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import ListItem, ListView

if TYPE_CHECKING:
    import corral
    from corral.split_layout import SplitGroup, SplitLayoutStore

from corral.activity_board import (
    BoardSnapshot,
    active_marker_style,
    resolve_active_marker,
)
from corral.i18n import t

NEW_SESSION_ID = "__new_session__"
ACTIVITY_BOARD_ID = "__activity_board__"
STICKY_IDS = (NEW_SESSION_ID, ACTIVITY_BOARD_ID)
_STICKY_ID_SET = frozenset(STICKY_IDS)
PIN_SEP_ID = "__pin_sep__"
TODAY_SEP_ID = "__today_sep__"
YESTERDAY_SEP_ID = "__yesterday_sep__"
DATE_SEP_PREFIX = "__date_sep_"
OLDER_STACK_ID = "__older_stack__"
SEP_ABOVE_MARK = "↑"
GROUP_ID_PREFIX = "__group__-"
# Named calendar buckets: 0=today, 1=yesterday, 2–6=weekday; 7+ is unlabeled.
# Buckets at index >= _RECENT_BUCKET_LIMIT are hidden behind OlderStackCard by default.
_OLDER_BUCKET = 7
_RECENT_BUCKET_LIMIT = 2
_WEEKDAY_LABEL_KEYS = (
    "list.sep_monday",
    "list.sep_tuesday",
    "list.sep_wednesday",
    "list.sep_thursday",
    "list.sep_friday",
    "list.sep_saturday",
    "list.sep_sunday",
)


def _is_separator_id(ident: str | None) -> bool:
    """Pinned divider or a named date-bucket trailing line."""
    if not ident:
        return False
    return ident in {PIN_SEP_ID, TODAY_SEP_ID, YESTERDAY_SEP_ID} or ident.startswith(
        DATE_SEP_PREFIX
    )


def _named_date_sep_id(days_ago: int, now: float) -> str:
    """Stable widget id for the trailing label of a named calendar bucket."""
    if days_ago <= 0:
        return TODAY_SEP_ID
    if days_ago == 1:
        return YESTERDAY_SEP_ID
    day = datetime.fromtimestamp(now).date() - timedelta(days=days_ago)
    return f"{DATE_SEP_PREFIX}{day.weekday()}__"


def _sep_label_key(identity: str) -> str:
    if identity == PIN_SEP_ID:
        return "list.sep_pinned"
    if identity == TODAY_SEP_ID:
        return "list.sep_today"
    if identity == YESTERDAY_SEP_ID:
        return "list.sep_yesterday"
    if identity.startswith(DATE_SEP_PREFIX) and identity.endswith("__"):
        body = identity[len(DATE_SEP_PREFIX) : -2]
        try:
            weekday = int(body)
        except ValueError:
            return "list.sep_today"
        if 0 <= weekday < len(_WEEKDAY_LABEL_KEYS):
            return _WEEKDAY_LABEL_KEYS[weekday]
    return "list.sep_pinned"


# Fallback time-row styles when the widget is not mounted yet (unit tests call
# SessionCard.render() without a theme). Dim all but the freshest tier rather
# than failing the whole render.
_TIME_FALLBACK_STYLES = {
    "fresh": Style(),
    "recent": Style(dim=True),
    "today": Style(dim=True),
    "old": Style(dim=True),
}


class NoSelectListItem(ListItem):
    """禁止拖选文本的 ListItem。

    子卡片即便设了 ALLOW_SELECT=False，外层 ListItem 默认仍是 True。后台重扫
    clear/extend 时若 Textual 正巧把选区挂在这个 ListItem 上，parent 变 None
    后访问 .region 会直接打崩整个 TUI（真机 2026-08-03：启动后闪退，
    AttributeError: 'NoneType' object has no attribute 'region'）。
    """

    ALLOW_SELECT = False


def _focused_live_session_key(focused) -> str | None:
    """焦点控件若是右栏某个「活着的实时终端」，返回它此刻绑定的会话键。

    必须在鼠标按下的当帧解析成会话键，不能只记下控件对象事后再反查：紧随点击的
    选择跟随会把同一个面板控件**就地改绑**到刚点的那个会话（`PaneCell.rebind`
    复用控件不重建），事后按控件身份比对，会把「点了另一张卡」误判成「点了正
    持有输入的那张卡」，焦点被撤回侧边栏——真机表现就是连续点不同会话时焦点
    在侧边栏和右栏之间来回跳。
    """
    if focused is None or getattr(focused, "dead", True):
        # 只有 EmbedPane 有 dead；其它控件（列表、搜索框）一律不算持有右栏输入
        return None
    return _pane_session_key(getattr(focused, "parent", None), require_live=True)


def _focused_board_session_key(focused) -> str | None:
    """看板用：焦点所在格的会话键，格子刚结束也算。

    `_focused_live_session_key` 要求格子还活着：会话一结束 `dead` 或托管名清空，
    钉住会被清掉，格子当场从看板消失。看板里用户可能还在看收尾。
    """
    return _pane_session_key(focused, require_live=False)


def _pane_session_key(focused, *, require_live: bool) -> str | None:
    node = focused
    while node is not None:
        spec = getattr(node, "spec", None)
        session_key = getattr(spec, "session_key", None)
        if session_key and not str(session_key).startswith("__"):
            if require_live and not getattr(spec, "keepalive_name", None):
                return None
            return str(session_key)
        node = getattr(node, "parent", None)
    return None


class SessionMultiToggleRequested(Message):
    """Ctrl/Cmd+点击会话卡：切换侧边栏多选集（不触发 ListView Selected）。"""

    bubble = True

    def __init__(self, session_key: str) -> None:
        super().__init__()
        self.session_key = session_key


class SessionGroupToggleRequested(Message):
    """点击组卡三角：切换展开状态，不触发打开会话组。"""

    bubble = True

    def __init__(self, group_id: str) -> None:
        super().__init__()
        self.group_id = group_id


class OlderStackToggleRequested(Message):
    """点击三层叠卡：展开或收起比昨天更早的会话。"""

    bubble = True


@dataclass(frozen=True)
class _SidebarRow:
    """侧边栏的一行逻辑条目；组卡与会话卡共用同一套重建顺序。"""

    kind: str
    identity: str
    session: dict | None = None
    group: SplitGroup | None = None
    member_sessions: tuple[dict, ...] = ()
    tree_position: str | None = None
    pinned: bool = False
    stripe: bool = False
    # older_stack only: how many top-level blocks (sessions/groups) are folded away.
    count: int = 0
    expanded: bool = False


_MAX_SPLICE_REGION = 8

# 首铺分片：一次全量重建同步只挂这么多行（可视区+缓冲），剩余行每批间隔
# _TAIL_MOUNT_INTERVAL 秒补齐。218 卡规模的一次性挂载实测主线程冻结约 0.8~1 秒，
# 分片后首帧只需挂首批（几十毫秒），期间可交互。批间间隔必须 > 0（Textual
# Timer 用间隔做除法，0 会在停表时抛 ZeroDivisionError）。
_MOUNT_CHUNK = 40
_TAIL_MOUNT_INTERVAL = 0.01


def _region_splice(
    old: list[str], new: list[str], *, max_region: int = _MAX_SPLICE_REGION
) -> tuple[int, int, int] | None:
    """new 相对 old 只在一段连续区段内变化时，返回 (起点, 旧长, 新长)。

    公共前缀 + 公共后缀夹出唯一变化区段，区段外的行原样保留，DOM 只动中间。
    单行插/删是它的特例；更重要的是「独立会话卡 -> 会话组（同一位置删 1 插 3）」
    这类组合变化--旧版只能退回整表全量重建，200 卡量级实测约 1 秒，是
    新开分屏卡顿的主因。变化超出一段小区段（前后缀夹不干净且超过 max_region）时
    返回 None 仍走全量重建，与旧版搜索/展开类行为一致。区段内部可能夹着
    未变行，它们会随区段一起重建（丢 widget 身份但不丢内容），受 max_region 约束。
    """
    if old == new:
        return None
    common = min(len(old), len(new))
    start = 0
    while start < common and old[start] == new[start]:
        start += 1
    end_old, end_new = len(old), len(new)
    while (
        end_old > start and end_new > start and old[end_old - 1] == new[end_new - 1]
    ):
        end_old -= 1
        end_new -= 1
    old_len, new_len = end_old - start, end_new - start
    if old_len <= 0 and new_len <= 0:
        return None
    if old_len + new_len > max_region:
        return None
    if old and old_len == len(old) and new_len == len(new):
        # 一行都没保留（整体换血/重排）：区段替换退化为全量重建，直接不命中。
        return None
    return (start, old_len, new_len)


def _stripe_block_start(row: _SidebarRow) -> bool:
    return row.kind in {"group", "older_stack"} or (
        row.kind == "session" and row.tree_position is None
    )


def _count_top_level_blocks(rows: list[_SidebarRow]) -> int:
    """Count independent sessions + group cards (members do not add extra)."""
    return sum(1 for row in rows if _stripe_block_start(row))


def _assign_block_stripes(rows: list[_SidebarRow]) -> list[_SidebarRow]:
    """给侧边栏行打块级斑马纹相位。

    一块 = 一张独立会话卡，或「组卡 + 它全部成员」，块内共享同一相位；分隔线
    不着色、不计相位，跨过分隔线相位重置，展开/收起不会翻转其后块的条纹。

    相位锚定在段尾（从后往前分配）：新会话/新分屏总是插在段首，段内已有块的
    「下方块数」不变、条纹不翻转——若锚在段首，顶部插一块会让下方全部块翻转，
    每次翻转都是一次 Textual 全量样式重匹配（200 卡实测约 0.7 秒，主线程冻结）。
    段尾变更（删最旧一条）才翻转其上方块，与高频路径错开。
    """
    striped = list(rows)

    def assign_section(start: int, end: int) -> None:
        starts = [i for i in range(start, end) if _stripe_block_start(rows[i])]
        count = len(starts)
        for pos, block_start in enumerate(starts):
            # 从段尾数起：最后一个块无条纹，往前交替
            stripe = (count - 1 - pos) % 2 == 1
            block_end = starts[pos + 1] if pos + 1 < count else end
            for i in range(block_start, block_end):
                striped[i] = replace(rows[i], stripe=stripe)

    section_start = 0
    for i, row in enumerate(rows):
        if row.kind == "separator":
            assign_section(section_start, i)
            striped[i] = replace(row, stripe=False)
            section_start = i + 1
    assign_section(section_start, len(rows))
    return striped


def _session_days_ago(session: dict | None, now: float) -> int:
    """Local calendar days between session activity and `now`.

    0 = today (also live or future mtime). Missing/invalid timestamps fall into
    the unlabeled older bucket so they never steal a named date line.
    """
    if not session:
        return _OLDER_BUCKET
    if session.get("live"):
        return 0
    try:
        mtime = float(session.get("mtime") or 0)
    except (TypeError, ValueError):
        return _OLDER_BUCKET
    if mtime <= 0:
        return _OLDER_BUCKET
    now_date = datetime.fromtimestamp(now).date()
    then_date = datetime.fromtimestamp(mtime).date()
    delta = (now_date - then_date).days
    return 0 if delta < 0 else delta


def _session_in_today_window(session: dict | None, now: float) -> bool:
    """Independent session on the local calendar day (or currently running)."""
    return _session_days_ago(session, now) == 0


def _block_days_ago(block: list[_SidebarRow], now: float) -> int:
    """Groups stay intact: the newest member (live counts as today) wins."""
    newest = _OLDER_BUCKET
    for row in block:
        newest = min(newest, _session_days_ago(row.session, now))
        for member in row.member_sessions:
            newest = min(newest, _session_days_ago(member, now))
    return newest


class SessionCard(Widget):
    """会话卡片：三行正文（总高 3）——关注圆点+项目+标题 / 运行时 / 时间。"""

    COMPONENT_CLASSES = {
        "session-card--time-fresh",
        "session-card--time-recent",
        "session-card--time-today",
        "session-card--time-old",
        "session-card--tree",
    }

    # Textual 默认所有 Widget 都允许鼠标拖拽文本选择（ALLOW_SELECT=True）；这类
    # 卡片是"点击=选中该会话"的列表项，不是可选文本内容，必须关掉——否则鼠标
    # 点击会触发 Textual 内置的 SelectStart 逻辑，在 ListView 卡片这种没有常规
    # 可滚动祖先的场景下，container 解析为 None 后访问 .region 直接崩溃退出
    # （真机实测复现：点击会话卡直接闪退，AttributeError: 'NoneType' object
    # has no attribute 'region'）。
    ALLOW_SELECT = False

    DEFAULT_CSS = """
    SessionCard {
        height: 3;
        width: 1fr;
        pointer: pointer;
        /* 标题行统一吃这里的基础色：满亮前景整栏铺开太扎眼，压到 8 成
           （alpha 与当前背景混合，深浅色主题各自成立）。关注状态只由首行最左
           的圆点表达，避免整行标题变色压过真正需要用户处理的状态。 */
        color: $foreground 80%;
        &.-stripe {
            /* 半透明叠在 ListItem 底上，不跟选中/分屏高亮抢 background。 */
            background: $foreground 8%;
        }
    }
    /* 第三行时间按新鲜度分档着色：半小时内与标题同亮（=卡片基础色，着重显示），
       之后逐级压暗到几乎只剩轮廓。全部用 $foreground + alpha 表达，深浅色主题
       各自与背景混合成立，不写死具体颜色。 */
    SessionCard > .session-card--time-fresh {
        color: $foreground 80%;
    }
    SessionCard > .session-card--time-recent {
        color: $foreground 58%;
    }
    SessionCard > .session-card--time-today {
        color: $foreground 42%;
    }
    SessionCard > .session-card--time-old {
        color: $foreground 30%;
    }
    /* 组内树线：与卡片基础色同亮，靠「非 bold」让标题仍跳一层；绝不用终端
       dim——那一档在深色底上几乎看不见。 */
    SessionCard > .session-card--tree {
        color: $foreground 80%;
    }
    """

    def __init__(
        self,
        session: dict,
        store: corral.SessionStore,
        *,
        display_title: str | None = None,
        tree_position: str | None = None,
        pinned: bool = False,
    ) -> None:
        super().__init__()
        self.session = session
        self._store = store
        # 展示标题由外部（rebuild()/_update_cards_in_place）注入并按需更新，不在
        # render() 里自己调用 store.snapshot()——那个方法要拿锁、拷贝整个
        # display_titles dict，卡片一多就是重复的拷贝开销。
        self.display_title = (
            display_title
            if display_title is not None
            else session["fallback_title"]
        )
        self.tree_position = tree_position
        self.pinned = pinned
        self._multi_selected = False
        self._render_signature = self._compute_signature()

    def set_multi_selected(self, selected: bool) -> None:
        if selected == self._multi_selected:
            return
        self._multi_selected = selected
        self.refresh()

    def on_click(self, event: events.Click) -> None:
        if not (event.ctrl or event.meta):
            return
        import corral

        event.stop()
        self.post_message(SessionMultiToggleRequested(corral.session_key(self.session)))

    def _time_tier(self) -> str:
        import corral

        return corral._time_brightness_tier(self.session.get("mtime") or 0)

    def _tree_style(self) -> Style:
        """组内树线配色；未挂载时用默认前景，挂载后吃 `$foreground 80%`。

        只要前景、丢掉组件底色，避免盖住列表选中/分屏高亮。
        """
        style = self.get_component_rich_style("session-card--tree", default=Style())
        if style.color is None:
            return Style()
        return Style(color=style.color)

    def _time_style(self, tier: str) -> Style:
        """时间行的档位配色；未挂载（单测直接调 render）时退回 dim 兜底。

        只取组件样式里混色后的**前景**，丢掉它带出来的背景色——否则时间那一行
        会用卡片自己的底色盖住列表选中高亮/分屏底色，整行看着缺一块。
        """
        fallback = _TIME_FALLBACK_STYLES[tier]
        style = self.get_component_rich_style(
            f"session-card--time-{tier}", default=fallback
        )
        if style is fallback or style.color is None:
            return fallback
        return Style.from_color(style.color)

    def _compute_signature(self) -> tuple:
        """渲染相关字段的轻量快照，用来判定"内容是否真的变了"、要不要 refresh()。"""
        import corral

        session = self.session
        return (
            self.display_title,
            self._multi_selected,
            session.get("attention_kind"),
            session.get("attention_token"),
            session.get("attention_updated_at"),
            session.get("mtime"),
            # 「刚刚」绿点依赖本窗口托管；托管标记变化必须触发重绘。
            bool(session.get("keepalive_name")),
            # mtime 不变但会话「变旧」跨过档位线时，时间行要跟着压暗，所以档位
            # 本身也得进签名，否则原地更新路径不会重绘。
            self._time_tier(),
            # 相对时间文案（含「刚刚」↔「Xm ago」）随墙钟变化，必须进签名，
            # 否则「刚刚」加粗/文案切换与绿点消退不会在原地更新路径里重绘。
            corral._format_relative_time(session.get("mtime") or 0),
            self.tree_position,
            self.pinned,
        )

    def apply_update(
        self,
        session: dict,
        display_title: str,
        *,
        tree_position: str | None = None,
        pinned: bool = False,
    ) -> bool:
        """原地更新路径专用：替换会话引用与展示态，仅当渲染相关字段确实变化
        时才 refresh()。返回是否触发了 refresh，供调用方按需断言/统计。"""
        self.session = session
        self.display_title = display_title
        self.tree_position = tree_position
        self.pinned = pinned
        signature = self._compute_signature()
        changed = signature != self._render_signature
        self._render_signature = signature
        if changed:
            self.refresh()
        return changed

    def render(self) -> Text:
        import corral  # 延迟导入：ui 包只在 corral.main() 运行期才加载，届时模块已就绪

        session = self.session
        store = self._store
        title = self.display_title
        from corral.i18n import t

        # 组内子项挂在项目已知的会话组下，标题前再写项目名是重复噪音；
        # 独立会话卡仍用「项目 标题」前缀做定位。
        show_project = self.tree_position is None
        project = ""
        if show_project:
            project_path = corral._normalize_cwd(session.get("cwd"))
            project = (
                os.path.basename(project_path)
                if project_path
                else str(session.get("cwd_display") or t("project.unknown"))
            )
        multi_prefix = "▸ " if self._multi_selected else ""
        # 终端字体对彩色图钉 emoji 的覆盖很差，真实截图会变成方框；用单格上箭头。
        pin_prefix = "↑ " if self.pinned else ""
        title_prefix = f"{multi_prefix}{pin_prefix}"
        if show_project:
            title_prefix = f"{title_prefix}{project} "
        width = max(10, self.size.width or 40)
        # 树线贴左缘，必须用同一套半角框线（`│├└─`）：全角 `｜`/`－` 与 `├`
        # 对不齐，三行卡片之间竖线会断开。第 0 列始终是竖向框线，续行同列填
        # `│`，末项续行改空格，线才连续收住。
        first_prefix = ""
        continuation_prefix = ""
        if self.tree_position == "middle":
            first_prefix = "├─ "
            continuation_prefix = "│  "
        elif self.tree_position == "last":
            first_prefix = "└─ "
            continuation_prefix = "   "
        content_width = max(5, width - corral._text_width(first_prefix))

        runtime = store.registry.get(str(session.get("source") or ""))
        runtime_name = runtime.display_name
        runtime_id = getattr(runtime, "id", None) or str(session.get("source") or "")

        # 与 Active sessions 同源：含「刚刚」托管会话的绿点，不得只认三态待办。
        dot_style = active_marker_style(resolve_active_marker(session))
        # 有圆点才让出「圆点 + 空格」这两列；没有圆点的卡片不留占位空格，标题
        # 直接顶到最左并吃满整行宽度。
        dot_width = 0 if dot_style is None else 2
        # 放不下就直接截断，不留 `...`：省略号本身要占 3 格，等于把最后几个
        # 有效字符换成没有信息量的符号，宁可多显示几个字。
        title_cell = corral._fit_cell(
            title_prefix + title, max(1, content_width - dot_width)
        )
        runtime_cell = corral._fit_cell_right(runtime_name, content_width)

        relative_time = corral._format_relative_time(session.get("mtime") or 0)
        time_cell = corral._fit_cell_right(relative_time, content_width)
        # 时间按新鲜度取一档亮度：半小时内与标题同亮，越旧越暗，让「刚刚还在动」
        # 的会话在一列时间里一眼可见。「刚刚」文案再加粗，与「Xm ago」拉开层级。
        time_style = self._time_style(self._time_tier())
        if relative_time == t("time.just_now"):
            time_style = time_style + Style(bold=True)

        # 首行整体 bold（与下面两行拉开层级）；独立卡的项目名再 dim 一档，
        # 避免和标题抢视线。组内子项不写项目名，也就没有这段 dim。
        # 进行状态只由首行最左的圆点表达，标题本身不随运行状态变色。
        tree_style = self._tree_style() if first_prefix else Style()
        out = Text()
        out.append(first_prefix, style=tree_style)
        content_start = len(first_prefix)
        if dot_style is not None:
            out.append("●", style=dot_style)
            out.append(" ")
        content_len = len(title_cell.rstrip(" "))
        out.append(title_cell)
        if content_len > 0:
            out.stylize(
                "bold", content_start + dot_width, content_start + dot_width + content_len
            )
            if show_project:
                # 窄栏时截断可能吃掉部分项目名，取两者较小值，别把 dim 涂到标题上。
                project_end = min(len(title_prefix), content_len)
                project_start = min(len(multi_prefix), project_end)
                if project_end > project_start:
                    out.stylize(
                        "dim",
                        content_start + dot_width + project_start,
                        content_start + dot_width + project_end,
                    )
        out.append("\n")
        out.append(continuation_prefix, style=tree_style)
        out.append(runtime_cell, style=corral.runtime_label_style(runtime_id))
        out.append("\n")
        out.append(continuation_prefix, style=tree_style)
        out.append(time_cell, style=time_style)
        return out


class SessionGroupCard(Widget):
    """会话组三行卡：展开三角+水果名 / 项目与数量 / 空白行（高度与会话卡统一为 3）。

    第三行故意留空：成员各自已有时间，组卡再写「最近活动」是重复噪音。
    """

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    SessionGroupCard {
        height: 3;
        width: 1fr;
        pointer: pointer;
        color: $foreground 88%;
        &.-stripe {
            background: $foreground 8%;
        }
    }
    """

    def __init__(
        self,
        group: SplitGroup,
        member_sessions: tuple[dict, ...],
        *,
        pinned: bool = False,
    ) -> None:
        super().__init__()
        self.group = group
        self.member_sessions = member_sessions
        self.pinned = pinned
        self._render_signature = self._compute_signature()

    def _compute_signature(self) -> tuple:
        # 收起时汇总含「刚刚」绿点，须随成员关注态 / 托管 / 新鲜度更新。
        return (
            self.group.name,
            self.group.project_cwd,
            self.group.collapsed,
            self.pinned,
            tuple(
                (
                    session.get("source"),
                    session.get("id"),
                    resolve_active_marker(session),
                )
                for session in self.member_sessions
            ),
        )

    def _tree_style(self) -> Style:
        """让组卡延伸的树干与成员卡使用同一条树线颜色。"""
        style = self.get_component_rich_style("session-card--tree", default=Style())
        if style.color is None:
            return Style()
        return Style(color=style.color)

    def _collapsed_attention_summary(self, width: int, indent: str) -> Text:
        """汇总收起组内仍需关注的会话，避免收起后把状态一起藏掉。"""
        import corral

        statuses = (
            ("waiting", "group.attention_waiting"),
            ("working", "group.attention_working"),
            ("unread", "group.attention_unread"),
        )
        markers = [resolve_active_marker(session) for session in self.member_sessions]
        counts = {kind: 0 for kind, _ in statuses}
        for marker in markers:
            summary_kind = "working" if marker == "recent" else marker
            if summary_kind in counts:
                counts[summary_kind] += 1
        out = Text(indent)
        has_status = False
        for kind, label_key in statuses:
            count = counts[kind]
            style = active_marker_style(kind)
            if count == 0 or style is None:
                continue
            if has_status:
                out.append(" · ", style="dim")
            out.append("●", style=style)
            out.append(f" {t(label_key, count=count)}")
            has_status = True
        if not has_status:
            count = len(self.member_sessions)
            out.append(t("group.all_read", count=count), style="dim")
        out.truncate(width, overflow="crop")
        out.append(" " * max(0, width - corral._text_width(out.plain)))
        return out

    def apply_update(
        self,
        group: SplitGroup,
        member_sessions: tuple[dict, ...],
        *,
        pinned: bool = False,
    ) -> bool:
        self.group = group
        self.member_sessions = member_sessions
        self.pinned = pinned
        signature = self._compute_signature()
        changed = signature != self._render_signature
        self._render_signature = signature
        if changed:
            self.refresh()
        return changed

    def on_click(self, event: events.Click) -> None:
        # 只有三角本身负责折叠；点击卡片其它位置仍是「打开这个会话组」。
        if event.x > 1:
            return
        event.stop()
        self.post_message(SessionGroupToggleRequested(self.group.group_id))

    def render(self) -> Text:
        import corral
        from corral.split_layout import group_emoji

        width = max(10, self.size.width or 40)
        arrow = "▶" if self.group.collapsed else "▼"
        pin = " ↑" if self.pinned else ""
        emoji = group_emoji(self.group.name)
        emoji_prefix = f"{emoji} " if emoji else ""
        # 前缀宽度固定：第二行项目名从同一列起笔，和水果 emoji/Group xxx 左对齐。
        name_prefix = f"{arrow}{pin} {emoji_prefix}"
        title = corral._fit_cell(f"{name_prefix}{self.group.name}", width)
        project = os.path.basename(self.group.project_cwd.rstrip(os.sep))
        if not project:
            project = t("project.unknown")
        count = len(self.member_sessions)
        count_key = "group.session_count_one" if count == 1 else "group.session_count"
        indent = " " * corral._text_width(name_prefix)
        # 展开时让树干从组标题的三角正下方一路接到成员分叉；收起后没有成员，
        # 不画这条线，避免把已隐藏的内容误画成还在列表里。
        branch_prefix = "│" if not self.group.collapsed else " "
        lower_indent = branch_prefix + indent[1:]
        count_cell = f" · {t(count_key, count=count)}"
        project_cell = corral._fit_cell(
            f"{lower_indent}{project}",
            max(1, width - corral._text_width(count_cell)),
        )
        title = title.rstrip()
        out = Text()
        if emoji and emoji in title:
            before, _, after = title.partition(emoji)
            out.append(before, style="bold")
            # emoji 本身天然是彩色图形，不需要再加粗；单独成 span 方便
            # capture.py 把水果字形换成 Twemoji PNG（Cairo 画不出 Color Emoji）。
            out.append(emoji)

            out.append(after, style="bold")
        else:
            out.append(title, style="bold")
        out.append(" " * max(0, width - corral._text_width(title)))
        out.append("\n")
        out.append(project_cell.rstrip(), style="bold")
        out.append(count_cell.rstrip(), style="dim")
        out.append(" " * max(0, width - corral._text_width(project_cell.rstrip() + count_cell.rstrip())))
        out.append("\n")
        # 收起后成员卡不可见，第三行改为状态汇总；展开时仍留白，避免与成员卡重复。
        if self.group.collapsed:
            out.append_text(self._collapsed_attention_summary(width, indent))
        else:
            out.append(branch_prefix + " " * (width - 1), style=self._tree_style())
        return out


class NewSessionCard(Widget):
    """列表顶部「新建会话」：一行正文 + 末行间隔（总高 2）。"""

    ALLOW_SELECT = False  # 原因同 SessionCard：点击这项是选中动作，不是选文本

    DEFAULT_CSS = """
    NewSessionCard {
        height: 2;
        width: 1fr;
        pointer: pointer;
    }
    """

    def render(self) -> Text:
        from corral.i18n import t

        # 第二行空行：与会话卡同样把分隔算进本项命中区
        return Text(t("list.new_session"), style="bold") + Text("\n")


def activity_board_label(snap: BoardSnapshot | None) -> str:
    """侧栏入口首行文案：没人盯时只写名称；有人时写成「活跃会话 · N 个会话」。

    页码不写进这段：只有一页时保持干净；多于一页时由第二行「上一页 / 下一页」承担。
    """
    if snap is None or not snap.total:
        return t("list.activity_board")
    count = (
        t("group.session_count_one")
        if snap.total == 1
        else t("group.session_count", count=snap.total)
    )
    return t("list.activity_board_count", count=count)


def activity_board_pager_text(snap: BoardSnapshot | None) -> str:
    """多于一页时返回 ``1/2``；只有一页或没人时为空。"""
    if snap is None or snap.page_count <= 1:
        return ""
    return f"{snap.page + 1}/{snap.page_count}"


@dataclass(frozen=True)
class BoardRowLayout:
    """活跃会话入口三行：首行名称、第二行上一页/下一页、第三行留白。"""

    label: str
    waiting: bool
    show_pager: bool
    prev_label: str
    next_label: str
    page_text: str
    prev_start: int
    prev_end: int
    next_start: int
    next_end: int
    page_start: int

    def hit(self, x: int, y: int = 0) -> int | None:
        """点在第二行上一页 / 下一页上返回 -1 / 1；点在第二行其余位置返回 0；其它行返回 None。"""
        if y != 1 or not self.show_pager:
            return None
        if self.prev_start <= x < self.prev_end:
            return -1
        if self.next_start <= x < self.next_end:
            return 1
        return 0


def layout_activity_board_row(
    snap: BoardSnapshot | None, width: int
) -> BoardRowLayout:
    """按侧栏宽度排入口三行，给绘制和点击命中共用。"""
    import corral

    width = max(10, width)
    label = activity_board_label(snap)
    waiting = bool(snap is not None and snap.waiting_off_page)
    extra = 2 if waiting else 0
    fitted = corral._fit_cell(label, max(1, width - extra)).rstrip()
    show_pager = bool(snap is not None and snap.page_count > 1)
    prev_label = t("action.board_prev") if show_pager else ""
    next_label = t("action.board_next") if show_pager else ""
    page_text = activity_board_pager_text(snap) if show_pager else ""
    prev_w = corral._text_width(prev_label)
    next_w = corral._text_width(next_label)
    page_w = corral._text_width(page_text)
    min_gap = 1 + page_w + (1 if page_w else 0)
    if show_pager and prev_w + next_w + min_gap > width:
        budget = max(2, width - min_gap)
        prev_budget = max(1, budget // 2)
        next_budget = max(1, budget - prev_budget)
        prev_label = corral._fit_cell(prev_label, prev_budget).rstrip()
        next_label = corral._fit_cell(next_label, next_budget).rstrip()
        prev_w = corral._text_width(prev_label)
        next_w = corral._text_width(next_label)
    prev_start = 0
    prev_end = prev_w
    next_start = width - next_w if next_label else width
    next_end = width if next_label else width
    gap_start = prev_end
    gap_end = next_start
    gap = gap_end - gap_start
    if page_text and gap >= page_w + 2:
        page_start = gap_start + (gap - page_w) // 2
    else:
        page_text = ""
        page_start = -1
    return BoardRowLayout(
        label=fitted,
        waiting=waiting,
        show_pager=show_pager,
        prev_label=prev_label,
        next_label=next_label,
        page_text=page_text,
        prev_start=prev_start,
        prev_end=prev_end,
        next_start=next_start,
        next_end=next_end,
        page_start=page_start,
    )


class ActivityBoardCard(Widget):
    """列表顶部「活跃会话」看板入口：名称 + 翻页 + 末行间隔（总高 3）。"""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    ActivityBoardCard {
        height: 3;
        width: 1fr;
        pointer: pointer;
    }
    """

    def __init__(self, owner: SessionListView, **kwargs) -> None:
        super().__init__(**kwargs)
        self._owner = owner
        self._row_layout: BoardRowLayout | None = None

    def render(self) -> Text:
        import corral

        snap = self._owner.board_snapshot
        width = max(10, self.size.width or 39)
        layout = layout_activity_board_row(snap, width)
        self._row_layout = layout
        out = Text(layout.label, style="bold")
        used = corral._text_width(layout.label)
        if layout.waiting:
            out.append(" ")
            out.append("●", style="bold yellow")
            used += 2
        pad = max(0, width - used)
        if pad:
            out.append(" " * pad)
        out.append("\n")
        if layout.show_pager:
            cur = 0
            if layout.prev_label:
                out.append(layout.prev_label)
                cur = layout.prev_end
            if layout.page_text and layout.page_start >= 0:
                gap = max(0, layout.page_start - cur)
                if gap:
                    out.append(" " * gap)
                out.append(layout.page_text, style="dim")
                cur = layout.page_start + corral._text_width(layout.page_text)
            if layout.next_label:
                gap = max(0, layout.next_start - cur)
                if gap:
                    out.append(" " * gap)
                out.append(layout.next_label)
                cur = layout.next_end
            pad = max(0, width - cur)
            if pad:
                out.append(" " * pad)
        out.append("\n")
        return out

    def on_click(self, event: events.Click) -> None:
        layout = self._row_layout
        if layout is None:
            layout = layout_activity_board_row(
                self._owner.board_snapshot, max(10, self.size.width or 39)
            )
        delta = layout.hit(event.x, event.y)
        if delta is None:
            return
        event.stop()
        screen = self.screen
        page = getattr(screen, "_page_activity_board", None)
        if callable(page):
            page(delta, from_click=True)


class PinSeparatorCard(Widget):
    """Trailing divider: `─` on both sides, centered label, whole row `$primary`.

    Labels the section above as `{name}↑` (Pinned / Today / Yesterday /
    weekday). The arrow is appended in render, not in i18n strings. Never
    Older/Other — that would mark sessions below as second-class. Narrow
    columns clip by display width, not `len()`, and keep the arrow when
    there is room.
    """

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    PinSeparatorCard {
        height: 1;
        width: 1fr;
        color: $primary;
    }
    """

    def __init__(self, label_key: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.label_key = label_key

    def render(self) -> Text:
        import corral

        width = max(10, self.size.width or 40)
        name = t(self.label_key)
        # Reserve ─␠label↑␠─ when possible; keep the up-arrow (section above)
        # unless the column is too narrow even for the mark.
        max_label = max(1, width - 4)
        arrow_w = corral._text_width(SEP_ABOVE_MARK)
        if max_label > arrow_w:
            text = corral._fit_cell(name, max_label - arrow_w).rstrip(" ")
            fitted = f"{text}{SEP_ABOVE_MARK}"
        else:
            fitted = corral._fit_cell(name, max_label).rstrip(" ")
        inner = f" {fitted} "
        inner_w = corral._text_width(inner)
        if inner_w >= width:
            line = corral._fit_cell(fitted, width).rstrip(" ")
        else:
            leftover = width - inner_w
            left = leftover // 2
            right = leftover - left
            line = ("─" * left) + inner + ("─" * right)
        # 颜色吃 CSS `$primary`，不写死也不 dim。
        return Text(line)


def _append_calendar_buckets(
    rows: list[_SidebarRow],
    buckets: list[list[_SidebarRow]],
    start: int,
    end: int,
    now: float,
    *,
    with_separators: bool,
    separator_through: int | None = None,
) -> None:
    """Append unpinned rows for calendar bucket indices ``[start, end)``."""
    scan_end = separator_through if separator_through is not None else end
    for index in range(start, end):
        bucket_rows = buckets[index]
        if not bucket_rows:
            continue
        rows.extend(bucket_rows)
        if not with_separators:
            continue
        has_later = any(buckets[later] for later in range(index + 1, scan_end))
        if has_later and index < _OLDER_BUCKET:
            rows.append(
                _SidebarRow(
                    kind="separator",
                    identity=_named_date_sep_id(index, now),
                )
            )


class OlderStackCard(Widget):
    """Folded gate for sessions older than yesterday: a three-deep card stack.

    Draws as a bordered front card with two peeks on the right edge so it reads
    as a clickable pile of sessions, not as decorative ``─`` rules. Chevron +
    label live inside the front card (same language as SessionGroupCard).
    """

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    OlderStackCard {
        height: 3;
        width: 1fr;
        pointer: pointer;
        color: $foreground 88%;
        &.-stripe {
            background: $foreground 8%;
        }
    }
    """

    def __init__(self, count: int, *, expanded: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.count = count
        self.expanded = expanded
        self._render_signature = (count, expanded)

    def apply_update(self, count: int, *, expanded: bool) -> None:
        signature = (count, expanded)
        if signature == self._render_signature:
            return
        self.count = count
        self.expanded = expanded
        self._render_signature = signature
        self.refresh()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(OlderStackToggleRequested())

    def render(self) -> Text:
        import corral

        # ┌────────────────┐┐┐
        # │ ▶ Older · N …  │││
        # └────────────────┘┘┘
        # Front card holds the affordance; the doubled right edge is the stack.
        width = max(10, self.size.width or 40)
        peek = 2  # two cards behind the front face
        inner_w = max(1, width - 2 - peek)
        arrow = "▼" if self.expanded else "▶"
        label = t("list.older_stack")
        count_text = t("list.older_stack_count", count=self.count)
        body = corral._fit_cell(f" {arrow} {label} · {count_text} ", inner_w)
        body_pad = max(0, inner_w - corral._text_width(body))
        if body_pad:
            body = body + (" " * body_pad)
        top = "┌" + ("─" * inner_w) + "┐" + ("┐" * peek)
        mid = "│" + body + "│" + ("│" * peek)
        bot = "└" + ("─" * inner_w) + "┘" + ("┘" * peek)
        out = Text()
        out.append(corral._fit_cell(top, width) + "\n", style="bold")
        out.append(corral._fit_cell(mid, width) + "\n", style="bold")
        out.append(corral._fit_cell(bot, width), style="bold")
        return out


class _SidebarList(ListView):
    """侧边栏一段列表：固定头（新建 + 活动看板）或会话滚动区（置顶与未置顶一起滚）。"""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    _SidebarList {
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 0;
        width: 1fr;
        height: auto;
        padding: 0;
        margin: 0;
        background: transparent;
    }
    _SidebarList > ListItem.-group-selected {
        background: $block-cursor-background;
    }
    _SidebarList > ListItem.-in-split {
        background: $sidebar-split-background;
    }
    _SidebarList > ListItem.-split-active {
        background: $sidebar-split-active-background;
    }
    _SidebarList > ListItem.-in-split.-group-selected,
    _SidebarList:focus > ListItem.-in-split.-highlight {
        background: $sidebar-split-cursor-background;
    }
    _SidebarList > ListItem.-split-active.-group-selected,
    _SidebarList:focus > ListItem.-split-active.-highlight {
        background: $sidebar-split-active-cursor-background;
    }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Select", show=False),
        Binding("k", "cursor_up", "Select", show=False),
        Binding("down", "cursor_down", "Select", show=False),
        Binding("up", "cursor_up", "Select", show=False),
        Binding("space", "toggle_multi", t("action.toggle_multi"), show=False),
        Binding("p", "toggle_pin", t("action.toggle_pin"), show=False),
    ]

    def __init__(self, owner: SessionListView, *, sticky: bool, **kwargs) -> None:
        super().__init__(**kwargs)
        self._owner = owner
        self._sticky = sticky

    def focus_on_click(self) -> bool:
        return self._owner.focus_on_click()

    def scroll_to_widget(self, widget: Widget, **kwargs) -> bool:
        if self._owner._preserve_scroll_depth:
            return False
        return super().scroll_to_widget(widget, **kwargs)

    def action_select_cursor(self) -> None:
        self._owner._selected_by_key = True
        super().action_select_cursor()

    def action_toggle_multi(self) -> None:
        self._owner.action_toggle_multi()

    def action_toggle_pin(self) -> None:
        self._owner.action_toggle_pin()

    def action_cursor_down(self) -> None:
        self._owner.clear_multi()
        old = self.index
        super().action_cursor_down()
        if self._sticky and self.index == old:
            self._owner.enter_scroll_first()

    def action_cursor_up(self) -> None:
        self._owner.clear_multi()
        old = self.index
        super().action_cursor_up()
        if not self._sticky and self.index == old:
            self._owner.enter_sticky_last()

    def watch_index(self, old_index: int | None, new_index: int | None) -> None:
        if self._owner._syncing_index:
            super().watch_index(old_index, new_index)
            return
        if (
            new_index is not None
            and 0 <= new_index < len(self._nodes)
            and (
                        self._nodes[new_index].disabled
                        or _is_separator_id(getattr(self._nodes[new_index], "id", None))
            )
        ):
            direction = 1
            if old_index is not None and new_index < old_index:
                direction = -1
            target = self._nearest_selectable_index(new_index, direction)
            if target is not None and target != new_index:
                self.index = target
                self._owner.sync_index_from_inner(self)
                return
        super().watch_index(old_index, new_index)
        self._owner.sync_index_from_inner(self)

    def _nearest_selectable_index(
        self, from_index: int, direction: int
    ) -> int | None:
        step = 1 if direction >= 0 else -1
        index = from_index + step
        while 0 <= index < len(self._nodes):
            if not self._nodes[index].disabled:
                return index
            index += step
        index = from_index - step
        while 0 <= index < len(self._nodes):
            if not self._nodes[index].disabled:
                return index
            index -= step
        return None

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if self._sticky:
            self._owner.scroll_unpinned(3)
            event.stop()
            return
        super()._on_mouse_scroll_down(event)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if self._sticky:
            self._owner.scroll_unpinned(-3)
            event.stop()
            return
        super()._on_mouse_scroll_up(event)


class SessionListView(Vertical):
    """会话列表外壳：固定头（新建 + 活动看板）+ 会话滚动区（置顶与未置顶一起滚）。

    对外仍是一个控件：统一 `index` 从「＋ 新建」起算。真正滚动的只有
    `#sidebar-scroll`。鼠标在固定头上滚轮转发到会话列表，顶部不动。
    """

    can_focus = False
    ALLOW_SELECT = False

    DEFAULT_CSS = """
    SessionListView {
        height: 1fr;
        width: 1fr;
        layout: vertical;
        padding: 0;
        margin: 0;
        overflow: hidden hidden;
    }
    SessionListView > #sidebar-sticky {
        height: auto;
        overflow: hidden hidden;
        padding: 0;
        margin: 0;
    }
    SessionListView > #sidebar-scroll {
        height: 1fr;
        overflow-x: hidden;
        overflow-y: auto;
        padding: 0;
        margin: 0;
    }
    """

    def __init__(
        self,
        store: corral.SessionStore,
        nav,
        *,
        group_store: SplitLayoutStore | None = None,
        on_layout_change: Callable[[Callable[[SplitLayoutStore], object]], SplitLayoutStore] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._index: int | None = None
        self._index_gen = 0
        self._syncing_index = False
        self._preserve_scroll_depth = 0
        self._preserved_scroll_y = 0.0
        self._sticky_list: _SidebarList | None = None
        self._scroll_list: _SidebarList | None = None
        self.store = store
        self.nav = nav
        self.group_store = group_store
        self.on_layout_change = on_layout_change
        self._multi_keys: list[str] = []
        self._split_keys: list[str] = []
        self._split_active_key: str | None = None
        self.focus_before_click = None
        self._selected_by_key = False
        self._rebuild_lock = asyncio.Lock()
        self._rebuild_seq = 0
        # 首铺分片：全量重建只同步挂首批（可视区+缓冲），剩余行按空闲帧分批补齐。
        # 批次间界面可交互；新重建请求靠 _rebuild_seq 递增让旧批次立即作废。
        self._tail_items: list[ListItem] = []
        self._tail_rows: list[_SidebarRow] | None = None
        self._tail_token = -1
        self.board_snapshot: BoardSnapshot | None = None
        self._older_stack_expanded = False

    def compose(self) -> ComposeResult:
        self._sticky_list = _SidebarList(self, sticky=True, id="sidebar-sticky")
        self._scroll_list = _SidebarList(self, sticky=False, id="sidebar-scroll")
        yield self._sticky_list
        yield self._scroll_list

    def begin_preserve_scroll(self) -> None:
        """Hold the viewport through group removal, selection and deferred focus."""
        if not self._preserve_scroll_depth and self._scroll_list is not None:
            self._preserved_scroll_y = self._scroll_list.scroll_y
        self._preserve_scroll_depth += 1

    def end_preserve_scroll(self) -> None:
        self._preserve_scroll_depth = max(0, self._preserve_scroll_depth - 1)
        if not self._preserve_scroll_depth and self._scroll_list is not None:
            self._scroll_list.scroll_to(
                y=self._preserved_scroll_y, animate=False, immediate=True,
            )

    def _list_items(self) -> list[ListItem]:
        items: list[ListItem] = []
        for inner in (self._sticky_list, self._scroll_list):
            if inner is None:
                continue
            items.extend(
                child for child in inner.children if isinstance(child, ListItem)
            )
        return items

    @property
    def list_children(self) -> list[ListItem]:
        return self._list_items()

    def _sticky_count(self) -> int:
        if self._sticky_list is None:
            return 0
        return sum(1 for child in self._sticky_list.children if isinstance(child, ListItem))

    def _scroll_count(self) -> int:
        if self._scroll_list is None:
            return 0
        return sum(1 for child in self._scroll_list.children if isinstance(child, ListItem))

    def _inner_for_index(self, index: int | None) -> _SidebarList | None:
        if self._sticky_list is None:
            return None
        sticky_n = self._sticky_count()
        if index is None or index < sticky_n:
            return self._sticky_list
        return self._scroll_list

    def focus_target(self) -> Widget:
        inner = self._inner_for_index(self._index)
        if inner is not None:
            return inner
        return self._sticky_list or self

    def focus(self, scroll_visible: bool = True) -> None:
        screen = getattr(self.app, "screen", None)
        if screen is not None:
            screen.set_focus(self.focus_target())

    @property
    def has_focus(self) -> bool:
        try:
            return bool(self.has_focus_within)
        except Exception:
            return False

    @has_focus.setter
    def has_focus(self, _value: bool) -> None:
        return

    @property
    def index(self) -> int | None:
        return self._index

    @index.setter
    def index(self, value: int | None) -> None:
        self._index_gen += 1
        self._index = value
        self._apply_inner_indices(value)
        self._apply_split_marks()

    def _focus_inner(self, inner: _SidebarList) -> None:
        screen = getattr(self.app, "screen", None)
        if screen is None or screen is not self.screen:
            return
        screen.set_focus(inner)

    def _apply_inner_indices(self, value: int | None) -> None:
        if self._sticky_list is None or self._scroll_list is None:
            return
        sticky_n = self._sticky_count()
        was_syncing = self._syncing_index
        self._syncing_index = True
        try:
            if value is None:
                self._sticky_list.index = None
                self._scroll_list.index = None
                return
            if value < sticky_n:
                self._sticky_list.index = value
                self._scroll_list.index = None
                if self.has_focus_within and not self._sticky_list.has_focus:
                    self._focus_inner(self._sticky_list)
            else:
                self._sticky_list.index = None
                self._scroll_list.index = value - sticky_n
                if self.has_focus_within and not self._scroll_list.has_focus:
                    self._focus_inner(self._scroll_list)
        finally:
            if not was_syncing:
                self._syncing_index = False

    def sync_index_from_inner(self, inner: _SidebarList) -> None:
        if self._syncing_index or inner.index is None:
            return
        sticky_n = self._sticky_count()
        if inner._sticky:
            self._index = inner.index
            other = self._scroll_list
        else:
            self._index = sticky_n + inner.index
            other = self._sticky_list
        if other is not None and other.index is not None:
            was_syncing = self._syncing_index
            self._syncing_index = True
            try:
                other.index = None
            finally:
                if not was_syncing:
                    self._syncing_index = False
        self._apply_split_marks()

    def enter_scroll_first(self) -> None:
        scroll = self._scroll_list
        if scroll is None:
            return
        target = scroll._nearest_selectable_index(-1, 1)
        if target is None:
            return
        self._index_gen += 1
        self._index = self._sticky_count() + target
        was_syncing = self._syncing_index
        self._syncing_index = True
        try:
            scroll.index = target
            if self._sticky_list is not None:
                self._sticky_list.index = None
        finally:
            if not was_syncing:
                self._syncing_index = False
        if self.has_focus_within:
            self._focus_inner(scroll)
        self._apply_split_marks()

    def enter_sticky_last(self) -> None:
        sticky = self._sticky_list
        if sticky is None:
            return
        target = None
        for i in range(len(sticky._nodes) - 1, -1, -1):
            if not sticky._nodes[i].disabled:
                target = i
                break
        if target is None:
            return
        self._index_gen += 1
        self._index = target
        was_syncing = self._syncing_index
        self._syncing_index = True
        try:
            sticky.index = target
            if self._scroll_list is not None:
                self._scroll_list.index = None
        finally:
            if not was_syncing:
                self._syncing_index = False
        if self.has_focus_within:
            self._focus_inner(sticky)
        self._apply_split_marks()

    def scroll_unpinned(self, delta: int) -> None:
        """滚动会话列表（置顶 + 未置顶）。固定头上的滚轮转发到这里。"""
        scroll = self._scroll_list
        if scroll is None or delta == 0:
            return
        scroll.scroll_relative(y=delta, animate=False)

    @property
    def highlighted_child(self) -> ListItem | None:
        items = self._list_items()
        idx = self._index
        if idx is None or not (0 <= idx < len(items)):
            return None
        return items[idx]

    def _partition_items(
        self, items: list[ListItem]
    ) -> tuple[list[ListItem], list[ListItem]]:
        """固定头只留「＋ 新建」和活动看板；置顶与未置顶都进滚动区。"""
        sticky_n = len(STICKY_IDS)
        return items[:sticky_n], items[sticky_n:]

    async def _replace_list_items(
        self, sticky_items: list[ListItem], scroll_items: list[ListItem]
    ) -> None:
        if self._sticky_list is None or self._scroll_list is None:
            return
        self._syncing_index = True
        try:
            await self._sticky_list.clear()
            await self._scroll_list.clear()
            if sticky_items:
                await self._sticky_list.extend(sticky_items)
            if scroll_items:
                await self._scroll_list.extend(scroll_items)
        finally:
            self._syncing_index = False

    async def clear(self) -> None:
        """清空两段列表，兼容调用方要求在重建前重置侧栏。"""
        # clear 不走 rebuild 的 seq 递增，这里手动作废分片尾部，防止空闲帧
        # 又把刚清掉的行挂回来。
        self._rebuild_seq += 1
        self._tail_items = []
        self._tail_rows = None
        await self._replace_list_items([], [])
        self._index = None
        self._selected_by_key = False

    def focus_on_click(self) -> bool:
        self.focus_before_click = _focused_live_session_key(
            getattr(self.app, "focused", None)
        )
        self._selected_by_key = False
        return True

    def take_focus_before_click(self):
        before = self.focus_before_click
        self.focus_before_click = None
        return before

    def action_select_cursor(self) -> None:
        self._selected_by_key = True
        inner = self._inner_for_index(self._index)
        if inner is not None:
            inner.action_select_cursor()

    def action_cursor_down(self) -> None:
        inner = self._inner_for_index(self._index)
        if inner is not None:
            inner.action_cursor_down()

    def action_cursor_up(self) -> None:
        inner = self._inner_for_index(self._index)
        if inner is not None:
            inner.action_cursor_up()

    def take_selected_by_key(self) -> bool:
        by_key = self._selected_by_key
        self._selected_by_key = False
        return by_key

    async def on_mount(self) -> None:
        await self.rebuild()

    def _session_items(self) -> list[tuple[ListItem, SessionCard]]:
        """按当前显示顺序返回 (列表项, 会话卡)（跳过顶部固定项）。

        底色类标在 ListItem 上而不是卡片上：整行铺满、且不会盖掉卡片自身的文字
        样式，也才能和 Textual 内置的选中高亮按 CSS 优先级正常分胜负。
        """
        items = []
        for item in self._list_items():
            if item.id in _STICKY_ID_SET:
                continue
            card = item.children[0] if item.children else None
            if isinstance(card, SessionCard):
                items.append((item, card))
        return items

    def _session_cards(self) -> list[SessionCard]:
        """按当前显示顺序返回全部 SessionCard（跳过顶部固定项）。"""
        return [card for _, card in self._session_items()]

    def _group_items(self) -> list[tuple[ListItem, SessionGroupCard]]:
        """按当前显示顺序返回全部会话组卡。"""
        items = []
        for item in self._list_items():
            card = item.children[0] if item.children else None
            if isinstance(card, SessionGroupCard):
                items.append((item, card))
        return items

    def _current_row_identities(self) -> list[str]:
        """返回当前 DOM 的组/会话/分隔身份，用于判断能否原地刷新。"""
        import corral

        identities: list[str] = []
        for item in self._list_items():
            if item.id in _STICKY_ID_SET or not item.children:
                continue
            card = item.children[0]
            if isinstance(card, PinSeparatorCard) or _is_separator_id(item.id):
                if item.id:
                    identities.append(item.id)
            elif isinstance(card, SessionGroupCard):
                identities.append(f"{GROUP_ID_PREFIX}{card.group.group_id}")
            elif isinstance(card, OlderStackCard):
                identities.append(OLDER_STACK_ID)
            elif isinstance(card, SessionCard):
                identities.append(corral.session_key(card.session))
        return identities

    def _update_rows_in_place(self, rows: list[_SidebarRow]) -> None:
        """条目身份与顺序不变时只换展示数据，不改 DOM。"""
        import corral

        display_titles = self.store.snapshot()
        widgets = [
            item.children[0]
            for item in self._list_items()
            if item.id not in _STICKY_ID_SET and item.children
        ]
        for widget, row in zip(widgets, rows, strict=False):
            if row.kind == "separator" or isinstance(widget, PinSeparatorCard):
                continue
            if isinstance(widget, OlderStackCard):
                widget.apply_update(row.count, expanded=row.expanded)
            elif isinstance(widget, SessionGroupCard) and row.group is not None:
                widget.apply_update(
                    row.group, row.member_sessions, pinned=row.pinned
                )
            elif isinstance(widget, SessionCard) and row.session is not None:
                key = corral.session_key(row.session)
                widget.apply_update(
                    row.session,
                    display_titles.get(key, row.session["fallback_title"]),
                    tree_position=row.tree_position,
                    pinned=row.pinned,
                )
        self._apply_stripes(rows)

    def visible_sessions(self) -> list[dict]:
        import corral
        from corral.models import SHELL_RUNTIME_ID

        display_titles = self.store.snapshot()
        sessions = self.store.all_sessions()
        visible = corral._filter_sessions_by_query(
            self.store.all_sessions(),
            self.nav.project_query,
            titles=display_titles,
        )
        visible = [
            session for session in visible if session.get("source") != SHELL_RUNTIME_ID
        ]
        query = self.nav.project_query.strip().casefold()
        if not query or self.group_store is None:
            return visible
        visible_keys = {corral.session_key(session) for session in visible}
        by_key = {corral.session_key(session): session for session in sessions}
        for group in self.group_store.groups.values():
            if query not in group.name.casefold():
                continue
            for key in group.session_keys:
                session = by_key.get(key)
                if session is not None and key not in visible_keys:
                    visible.append(session)
                    visible_keys.add(key)
        return visible

    def _sidebar_rows(self) -> list[_SidebarRow]:
        """把持久会话组投影成「组卡 + 缩进子会话」，其余会话保持扁平。

        未置顶区先按 `SessionStore.all_sessions()` 的稳定顺序走一遍，再按本地
        日历日切桶（今天 / 昨天 / 近 7 日内其余各日 / 更早；桶内相对顺序不变）：
        进入 corral 后已有项位置固定，后台重扫只因 mtime/标题更新而整列重排的「飘」
        不再发生；新会话仍由 store 插到最前（或冷会话追加末尾）。置顶块仍按置顶
        时间单独排在最上。
        """
        import corral

        sessions = self.store.all_sessions()
        by_key = {corral.session_key(session): session for session in sessions}
        filtered = self.visible_sessions()
        filtered_keys = {corral.session_key(session) for session in filtered}
        query = self.nav.project_query.strip().casefold()
        pinned_blocks: list[tuple[float, list[_SidebarRow]]] = []
        unpinned_by_id: dict[str, list[_SidebarRow]] = {}
        grouped_keys: set[str] = set()
        group_for_key: dict[str, SplitGroup] = {}

        if self.group_store is not None:
            from corral.models import is_shell_session

            for group in self.group_store.ordered_groups():
                # 终端 pane 会随分屏组合一起持久化成组员，但它不挂任何运行时；
                # 成员卡渲染要读运行时显示名，这里必须整组过滤掉，否则列表重建
                # 渲染组卡成员时直接抛「未注册的运行时：shell」。
                all_members = tuple(
                    by_key[key]
                    for key in group.session_keys
                    if key in by_key and not is_shell_session(by_key[key])
                )
                # 历史记录缺失或会话已被明确删除后，侧边栏不显示空壳组。
                if len(all_members) < 2:
                    continue
                group_matches = bool(query and query in group.name.casefold())
                members = (
                    all_members
                    if not query or group_matches
                    else tuple(
                        session
                        for session in all_members
                        if corral.session_key(session) in filtered_keys
                    )
                )
                if query and not members:
                    continue
                # 当前筛选下看得见的成员不足 2：解散为独立会话，好让单会话置顶
                # 重新生效（跨项目组分屏 + 按项目名筛选时的真实事故）。
                if len(members) < 2:
                    continue
                for session in members:
                    key = corral.session_key(session)
                    grouped_keys.add(key)
                    group_for_key[key] = group
                group_row = _SidebarRow(
                    kind="group",
                    identity=f"{GROUP_ID_PREFIX}{group.group_id}",
                    group=group,
                    member_sessions=members,
                    pinned=group.group_id in self.group_store.pinned_group_ids,
                )
                child_rows: list[_SidebarRow] = []
                if not group.collapsed or query:
                    for index, session in enumerate(members):
                        child_rows.append(
                            _SidebarRow(
                                kind="session",
                                identity=corral.session_key(session),
                                session=session,
                                tree_position=(
                                    "last"
                                    if index == len(members) - 1
                                    else "middle"
                                ),
                            )
                        )
                block = [group_row, *child_rows]
                block_id = f"{GROUP_ID_PREFIX}{group.group_id}"
                pinned_at = self.group_store.pinned_group_ids.get(group.group_id)
                if pinned_at is not None:
                    # 组卡与子会话是不可拆散的一个排序块。
                    pinned_blocks.append((pinned_at, block))
                else:
                    unpinned_by_id[block_id] = block

        for session in filtered:
            key = corral.session_key(session)
            if key not in grouped_keys:
                row = _SidebarRow(
                    kind="session",
                    identity=key,
                    session=session,
                    pinned=(
                        self.group_store is not None
                        and key in self.group_store.pinned_session_keys
                    ),
                )
                pinned_at = (
                    self.group_store.pinned_session_keys.get(key)
                    if self.group_store is not None
                    else None
                )
                if pinned_at is not None:
                    pinned_blocks.append((pinned_at, [row]))
                else:
                    unpinned_by_id[key] = [row]

        # 置顶块始终在最上（按置顶时间）。
        pinned_rows: list[_SidebarRow] = []
        pinned_blocks.sort(key=lambda item: item[0], reverse=True)
        for _, block in pinned_blocks:
            pinned_rows.extend(block)

        # 未置顶区：按 store 稳定顺序走一遍，组卡落在其「最先出现的成员」位置。
        # 禁止再按当前 mtime 整列重排——否则运行中会话一写盘整组就会在侧边栏里上下飘。
        # 只按本地日历日分桶：桶内相对顺序不变；live 或当天的块整块进今天。
        buckets: list[list[_SidebarRow]] = [[] for _ in range(_OLDER_BUCKET + 1)]
        now = time.time()

        def _emit_unpinned(block: list[_SidebarRow]) -> None:
            days = _block_days_ago(block, now)
            index = days if days < _OLDER_BUCKET else _OLDER_BUCKET
            buckets[index].extend(block)

        emitted: set[str] = set()
        for session in filtered:
            key = corral.session_key(session)
            group = group_for_key.get(key)
            if group is not None:
                if group.group_id in (
                    self.group_store.pinned_group_ids if self.group_store else {}
                ):
                    continue
                block_id = f"{GROUP_ID_PREFIX}{group.group_id}"
            else:
                if (
                    self.group_store is not None
                    and key in self.group_store.pinned_session_keys
                ):
                    continue
                block_id = key
            if block_id in emitted:
                continue
            block = unpinned_by_id.get(block_id)
            if block is None:
                continue
            _emit_unpinned(block)
            emitted.add(block_id)

        # 搜索把组名命中、成员却不在 filtered 主序里时的兜底（visible_sessions
        # 已尽量补齐；这里只防止漏块）。
        for block_id, block in unpinned_by_id.items():
            if block_id not in emitted:
                _emit_unpinned(block)
                emitted.add(block_id)

        rows: list[_SidebarRow] = list(pinned_rows)
        unpinned_visible = any(buckets)
        # 两侧都有可见项时才画分隔，避免「只剩置顶」或「没有置顶」时多出一条空线。
        if pinned_rows and unpinned_visible:
            rows.append(_SidebarRow(kind="separator", identity=PIN_SEP_ID))
        _append_calendar_buckets(
            rows,
            buckets,
            0,
            _RECENT_BUCKET_LIMIT,
            now,
            with_separators=True,
            separator_through=len(buckets),
        )
        older_slice = buckets[_RECENT_BUCKET_LIMIT:]
        if not any(older_slice):
            return _assign_block_stripes(rows)
        older_rows: list[_SidebarRow] = []
        _append_calendar_buckets(
            older_rows,
            buckets,
            _RECENT_BUCKET_LIMIT,
            len(buckets),
            now,
            with_separators=True,
        )
        has_recent = any(buckets[i] for i in range(_RECENT_BUCKET_LIMIT))
        # Collapse only when today/yesterday already give the user something to
        # look at. An all-older list should open flat — a lone stack gate with
        # no recent sessions above it is just an extra click for no gain.
        expand_older = (
            self._older_stack_expanded or bool(query) or not has_recent
        )
        count = _count_top_level_blocks(older_rows)
        if count > 0 and has_recent:
            rows.append(
                _SidebarRow(
                    kind="older_stack",
                    identity=OLDER_STACK_ID,
                    count=count,
                    expanded=expand_older,
                )
            )
        if expand_older:
            rows.extend(older_rows)
        return _assign_block_stripes(rows)

    def selected_session(self) -> dict | None:
        idx = self.index
        if idx is None or idx < 0 or idx >= len(self._list_items()):
            return None
        item = self._list_items()[idx]
        card = item.children[0] if item.children else None
        return card.session if isinstance(card, SessionCard) else None

    def selected_group(self) -> SplitGroup | None:
        """返回当前选中的会话组；普通会话或新建项返回 None。"""
        idx = self.index
        if idx is None or idx < 0 or idx >= len(self._list_items()):
            return None
        item = self._list_items()[idx]
        card = item.children[0] if item.children else None
        return card.group if isinstance(card, SessionGroupCard) else None

    def _selected_item_id(self) -> str | None:
        idx = self.index
        items = self._list_items()
        if idx is None or idx < 0 or idx >= len(items):
            return None
        return items[idx].id

    def is_new_session_selected(self) -> bool:
        return self._selected_item_id() == NEW_SESSION_ID

    def is_activity_board_selected(self) -> bool:
        return self._selected_item_id() == ACTIVITY_BOARD_ID

    def is_older_stack_selected(self) -> bool:
        return self._selected_item_id() == OLDER_STACK_ID

    def toggle_older_stack(self) -> None:
        self._older_stack_expanded = not self._older_stack_expanded
        self.call_next(self.rebuild)

    def _expand_older_stack_for(self, session_key: str) -> None:
        """Open the Older stack when a forced selection targets a session behind it."""
        if self._older_stack_expanded or self.nav.project_query.strip():
            return
        session = self.store.find_session(session_key)
        if session is not None and _session_days_ago(session, time.time()) >= _RECENT_BUCKET_LIMIT:
            self._older_stack_expanded = True

    def _collapsed_group_identity(
        self, session_key: str, identities: list[str],
    ) -> str | None:
        """Group card identity standing in for a member hidden by a collapsed group."""
        if self.group_store is None:
            return None
        group = self.group_store.get_group(session_key)
        if group is None:
            return None
        identity = f"{GROUP_ID_PREFIX}{group.group_id}"
        return identity if identity in identities else None

    def select_activity_board(self) -> None:
        """把高亮挪到活跃会话入口（固定头第二项）。"""
        target = STICKY_IDS.index(ACTIVITY_BOARD_ID)
        if self.index != target:
            self.index = target

    def set_board_snapshot(self, snapshot: BoardSnapshot | None) -> None:
        self.board_snapshot = snapshot
        self.refresh_board_card()

    def refresh_board_card(self) -> None:
        for item in self._list_items():
            if item.id != ACTIVITY_BOARD_ID or not item.children:
                continue
            card = item.children[0]
            if isinstance(card, ActivityBoardCard):
                card.refresh()
            return

    def multi_count(self) -> int:
        return len(self._multi_keys)

    def multi_keys(self) -> list[str]:
        return list(self._multi_keys)

    def clear_multi(self) -> None:
        if not self._multi_keys:
            return
        self._multi_keys.clear()
        self._apply_multi_markers()

    def _prune_multi_keys(self, valid_keys: set[str]) -> None:
        if not self._multi_keys:
            return
        self._multi_keys = [key for key in self._multi_keys if key in valid_keys]
        self._apply_multi_markers()

    def set_split_marks(self, pane_keys: list[str], active_key: str | None) -> None:
        """把右栏分屏投影到侧边栏：整组铺底，激活会话再重一档。

        只有真正分屏（≥2 格）才标。单格时列表光标本身就指着那一格，再叠一层
        底色只会和光标高亮互相打架，反而看不出焦点在哪。光标停在组卡上时由
        `_apply_split_marks` 给整组贴 `-group-selected`，与分屏键是否变化无关。
        """
        keys = [key for key in pane_keys if not key.startswith("__")]
        if len(keys) < 2:
            keys = []
        active = active_key if active_key in keys else None
        if keys == self._split_keys and active == self._split_active_key:
            return
        self._split_keys = keys
        self._split_active_key = active
        self._apply_split_marks()

    def split_marks(self) -> tuple[list[str], str | None]:
        """当前生效的分屏标记（组合内会话键，激活会话键），供测试与同步比对。"""
        return list(self._split_keys), self._split_active_key

    def _apply_split_marks(self) -> None:
        import corral

        keys = set(self._split_keys)
        active = self._split_active_key
        selected = self.selected_group()
        selected_id = selected.group_id if selected is not None else None
        selected_members = set(selected.session_keys) if selected is not None else set()
        for item, card in self._group_items():
            group_keys = set(card.group.session_keys)
            is_current_group = bool(keys) and keys.issubset(group_keys)
            item.set_class(is_current_group, "-in-split")
            item.set_class(False, "-split-active")
            item.set_class(
                selected_id is not None and card.group.group_id == selected_id,
                "-group-selected",
            )
        for item, card in self._session_items():
            key = corral.session_key(card.session)
            in_group = key in keys
            is_active = active is not None and key == active
            item.set_class(in_group, "-in-split")
            item.set_class(is_active, "-split-active")
            item.set_class(key in selected_members, "-group-selected")

    def _apply_stripes(self, rows: list[_SidebarRow]) -> None:
        """把块级斑马纹贴到卡片上；＋新建、活动看板与分隔线不参与。幂等。"""
        widgets = [
            item.children[0]
            for item in self._list_items()
            if item.id not in _STICKY_ID_SET and item.children
        ]
        for widget, row in zip(widgets, rows, strict=False):
            if isinstance(widget, (SessionCard, SessionGroupCard, OlderStackCard)):
                widget.set_class(row.stripe, "-stripe")

    def _apply_multi_markers(self) -> None:
        import corral

        selected = set(self._multi_keys)
        for card in self._session_cards():
            key = corral.session_key(card.session)
            card.set_multi_selected(key in selected)

    def _index_for_session_key(self, session_key: str) -> int | None:
        import corral

        for i, item in enumerate(self._list_items()):
            card = item.children[0] if item.children else None
            if (
                isinstance(card, SessionCard)
                and corral.session_key(card.session) == session_key
            ):
                return i
        return None

    def _toggle_multi_key(self, session_key: str) -> None:
        from corral.split_layout import MAX_PANES

        if session_key in self._multi_keys:
            self._multi_keys.remove(session_key)
        else:
            if len(self._multi_keys) >= MAX_PANES:
                self.notify(t("split.multi_full", n=MAX_PANES))
                self.app.bell()
                return
            self._multi_keys.append(session_key)
        target = self._index_for_session_key(session_key)
        if target is not None:
            self.index = target
        self._apply_multi_markers()

    def action_toggle_multi(self) -> None:
        if self.is_older_stack_selected():
            self.toggle_older_stack()
            return
        group = self.selected_group()
        if group is not None:
            self._toggle_group(group.group_id)
            return
        session = self.selected_session()
        if session is None:
            return
        import corral

        self._toggle_multi_key(corral.session_key(session))

    def action_toggle_pin(self) -> None:
        """用 p / Ctrl+P 切换独立会话或整个会话组的置顶状态。

        翻转结果以记忆库里的最新状态为准（返回的快照），不看本地那份可能已被别的
        窗口改过的旧快照。光标在组内成员上时改为整组置顶，与手机端一致。
        """
        group = self.selected_group()
        if group is not None:
            self._toggle_group_pin(group.group_id)
            return
        session = self.selected_session()
        if session is None:
            return
        import corral

        self.toggle_pin_key(corral.session_key(session))

    def toggle_pin_key(self, key: str) -> None:
        """按会话键置顶：独立会话单独置顶，组成员改为整组置顶。"""
        if self.group_store is None or self.on_layout_change is None:
            return
        if not key or key.startswith("__"):
            return
        from corral.models import SHELL_RUNTIME_ID

        group = self.group_store.get_group(key)
        if group is not None:
            self._toggle_group_pin(group.group_id)
            return
        if key.startswith(f"{SHELL_RUNTIME_ID}:"):
            return
        snapshot = self.on_layout_change(
            lambda store: store.toggle_session_pin(key)
        )
        self._notify_pin(key in snapshot.pinned_session_keys)

    def _toggle_group_pin(self, group_id: str) -> None:
        if self.group_store is None or self.on_layout_change is None:
            return
        snapshot = self.on_layout_change(
            lambda store: store.toggle_group_pin(group_id)
        )
        self._notify_pin(group_id in snapshot.pinned_group_ids)

    def _notify_pin(self, pinned: bool) -> None:
        self.notify(t("pin.enabled" if pinned else "pin.disabled"))
        self.call_next(self.rebuild)

    def on_session_multi_toggle_requested(self, event: SessionMultiToggleRequested) -> None:
        event.stop()
        self._toggle_multi_key(event.session_key)

    def on_session_group_toggle_requested(
        self, event: SessionGroupToggleRequested,
    ) -> None:
        event.stop()
        self._toggle_group(event.group_id)

    def on_older_stack_toggle_requested(
        self, event: OlderStackToggleRequested,
    ) -> None:
        event.stop()
        self.toggle_older_stack()

    def _toggle_group(self, group_id: str) -> None:
        if self.group_store is None or self.on_layout_change is None:
            return
        group = self.group_store.groups.get(group_id)
        if group is None:
            return
        collapsed = not group.collapsed
        snapshot = self.on_layout_change(
            lambda store: store.set_collapsed(group_id, collapsed)
        )
        target = snapshot.groups.get(group_id)
        if target is None or target.collapsed != collapsed:
            return
        self.call_next(self.rebuild)

    def select_session_key(self, session_key: str) -> bool:
        """按会话键设置列表高亮；找不到对应项时返回 False。

        用于右栏分屏焦点 → 侧边栏同步。`__hint__` 对应顶部「＋ 新建」项。
        """
        import corral

        if session_key == "__hint__":
            if self.index != 0:
                self.index = 0
            return True
        for i, item in enumerate(self._list_items()):
            card = item.children[0] if item.children else None
            if isinstance(card, SessionCard) and corral.session_key(card.session) == session_key:
                target = i
                if self.index != target:
                    self.index = target
                return True
        if not self._older_stack_expanded:
            self._expand_older_stack_for(session_key)
            if self._older_stack_expanded:
                self.call_next(self.rebuild)
        if self.group_store is not None:
            group = self.group_store.get_group(session_key)
            if group is not None:
                target_identity = f"{GROUP_ID_PREFIX}{group.group_id}"
                for i, item in enumerate(self._list_items()):
                    card = item.children[0] if item.children else None
                    if (
                        isinstance(card, SessionGroupCard)
                        and target_identity
                        == f"{GROUP_ID_PREFIX}{card.group.group_id}"
                    ):
                        if self.index != i:
                            self.index = i
                        return True
        return False

    def _displayed_selected_identity(self) -> str | None:
        """按当前已渲染的 DOM 卡片（而非刚重算过的 `visible_sessions()`）取回
        「用户此刻实际选中的会话或会话组」身份。

        `self.index` 是 DOM 子项下标；只有在 DOM 与 store 同步时它才等价于
        `visible_sessions()` 的下标。后台重扫时 store 先于 DOM 更新（新会话
        按 mtime 置顶插入），此时若仍用 `selected_session()`（内部按新算出的
        `visible_sessions()` 索引 `self.index`）推导原选中会话，会因列表顺序
        已变而错指到相邻会话——真实复现过：聚焦第三条时后台刷出新会话，
        高亮和右栏跟着串到第二条。`rebuild()` 必须用这个方法取原选中键，
        `selected_session()` 仍保留给用户交互期（回车/删除/结束会话等），
        那些时刻 DOM 与 store 本就同步，不受影响。
        """
        import corral

        idx = self.index
        items = self._list_items()
        if idx is None or idx < 0 or idx >= len(items):
            return None
        item = items[idx]
        if item.id in _STICKY_ID_SET:
            return item.id
        card = item.children[0] if item.children else None
        if isinstance(card, SessionGroupCard):
            return f"{GROUP_ID_PREFIX}{card.group.group_id}"
        if isinstance(card, OlderStackCard):
            return OLDER_STACK_ID
        if isinstance(card, SessionCard):
            return corral.session_key(card.session)
        return None

    def _displayed_selected_key(self) -> str | None:
        """兼容只关心会话的调用方；组卡选中时返回 None。"""
        identity = self._displayed_selected_identity()
        if (
            identity is None
            or identity.startswith(GROUP_ID_PREFIX)
            or identity in _STICKY_ID_SET
        ):
            return None
        return identity

    def _apply_index_after_rebuild(self, index: int) -> None:
        """在 clear()+extend() 后设置高亮，并在下一帧再钉一次。

        Textual ListView 在首次填充后会把 index 异步打回 0（落到「＋ 新建」），
        立刻赋值当场看起来对，refresh 之后就丢了。只在仍停在 0、而目标不是
        0 时纠正，避免抢掉用户在两帧之间已经手动挪走的光标。
        """
        self.index = index
        gen = self._index_gen

        def _reapply() -> None:
            self._syncing_index = False
            if self._index_gen == gen and self._index != index:
                self.index = index

        self.call_after_refresh(_reapply)

    def _item_for_row(self, row: _SidebarRow, display_titles: dict) -> ListItem | None:
        """把一行逻辑条目做成 ListItem；分隔线带稳定 id，会话/组卡不设 id。"""
        import corral

        if row.kind == "separator":
            return NoSelectListItem(
                PinSeparatorCard(_sep_label_key(row.identity)),
                id=row.identity,
                disabled=True,
            )
        if row.kind == "older_stack":
            return NoSelectListItem(
                OlderStackCard(row.count, expanded=row.expanded),
                id=OLDER_STACK_ID,
            )
        if row.kind == "group" and row.group is not None:
            card: Widget = SessionGroupCard(
                row.group, row.member_sessions, pinned=row.pinned
            )
        elif row.session is not None:
            key = corral.session_key(row.session)
            card = SessionCard(
                row.session,
                self.store,
                display_title=display_titles.get(
                    key, row.session["fallback_title"]
                ),
                tree_position=row.tree_position,
                pinned=row.pinned,
            )
        else:
            return None
        return NoSelectListItem(card)

    def _target_index_after_rows(
        self,
        previous_identity: str | None,
        new_identities: list[str],
        *,
        had_rows: bool,
    ) -> int | None:
        """集合变化后按原选中身份定位下标；找不到则停在固定头。"""
        sticky_n = self._sticky_count()
        new_index = 0
        if previous_identity in _STICKY_ID_SET:
            for i, item in enumerate(self._list_items()):
                if item.id == previous_identity:
                    new_index = i
                    break
        else:
            for i, identity in enumerate(new_identities):
                if previous_identity is not None and identity == previous_identity:
                    new_index = i + sticky_n
                    break
        if previous_identity is not None:
            return new_index
        if not had_rows:
            return sticky_n if new_identities else 0
        return None

    def _sticky_intact(self) -> bool:
        """固定头（＋新建 / 活动看板）还在：不在则只能走全量重建回补它们。

        clear() 会把固定头一并清掉；区段路径只动滚动区，若在此时命中，
        会留下一张没有固定头的侧栏。"""
        if self._sticky_list is None:
            return False
        ids = {item.id for item in self._sticky_list.children}
        return _STICKY_ID_SET <= ids

    async def _splice_region(
        self,
        *,
        start: int,
        old_len: int,
        new_rows: list[_SidebarRow],
        display_titles: dict,
    ) -> bool:
        """只替换滚动区里一段连续行；失败返回 False，调用方改走全量重建。

        单行插/删是特例（old_len 或 new_rows 为 0）；「独立卡 -> 会话组」这类
        同位置删 N 插 M 也走这里，避免整表 clear()+extend()（200 卡实测约 1 秒）。
        """
        scroll = self._scroll_list
        if scroll is None:
            return False
        new_items: list[ListItem] = []
        for row in new_rows:
            item = self._item_for_row(row, display_titles)
            if item is None:
                return False
            new_items.append(item)
        self._syncing_index = True
        try:
            with self.app.batch_update():
                if old_len:
                    nodes = list(scroll._nodes)  # noqa: SLF001  ListView 未提供区间删除
                    if start + old_len > len(nodes):
                        return False
                    for node in nodes[start : start + old_len]:
                        await node.remove()
                if new_items:
                    if start >= len(scroll._nodes):
                        await scroll.extend(new_items)
                    else:
                        await scroll.insert(start, new_items)
        finally:
            self._syncing_index = False
        return True

    async def rebuild(
        self,
        *,
        keep_selection: bool = True,
        select_key: str | None = None,
    ) -> None:
        """按当前筛选重建条目；尽量保持原有选中的会话不变（后台重扫后调用）。

        会话集合（顺序+成员）没变时走原地更新——只换 SessionCard 手上的
        session 引用、按需 refresh()，不碰 ListView 子项结构；只插入或删除
        一条时在滚动区就地 splice，避免 clear()+extend() 把整表拆挂一遍；
        多处变化才走批量清空重建。见 docs/MAINTAINER_GUIDE.md「界面」节。

        `select_key`：跨运行时接力 / 空白新建后强制选中刚插入的托管占位卡。

        **必须串行**：调用方分布在两条互不相让的 Textual 消息泵上——后台重扫
        worker 走 `app.call_from_thread(_rebuild_list)`（App 泵，且 MainScreen
        自己那把锁只挡得住同泵的重入），搜索框输入走 `on_input_changed`
        （Screen 泵）。全量重建里的 `clear()` / `extend()` 都会 await 让出，
        两条泵一旦交错，前一次的 extend 会把新建项插到后一次已经填好的列表上，
        Textual 直接抛 DuplicateIds 打崩整个 TUI（2026-07-26 真机复现：连续退格
        清空搜索词，命中数 50→57→71 连做全量重建，单次耗时已到 2s 量级，撞上
        后台重扫必崩）。这把锁是唯一的进 DOM 闸门，禁止绕过它直接改子项结构。
        """
        self._rebuild_seq += 1
        seq = self._rebuild_seq
        async with self._rebuild_lock:
            # 排队期间又来了更新的请求：本次没有强制选中语义就直接让位，避免
            # 连续输入把每个中间态都全量重建一遍（只认最后一次的筛选结果）。
            if select_key is None and seq != self._rebuild_seq:
                return
            await self._rebuild_locked(
                keep_selection=keep_selection, select_key=select_key
            )

    async def _rebuild_locked(
        self,
        *,
        keep_selection: bool,
        select_key: str | None,
    ) -> None:
        """rebuild() 的实现体；只允许持 `_rebuild_lock` 时调用。"""
        if select_key is not None:
            self._expand_older_stack_for(select_key)
        previous_identity = select_key
        if previous_identity is None and keep_selection:
            previous_identity = self._displayed_selected_identity()

        rows = self._sidebar_rows()
        new_identities = [row.identity for row in rows]
        if select_key is not None and select_key not in new_identities:
            previous_identity = self._collapsed_group_identity(
                select_key, new_identities
            ) or previous_identity
        self._prune_multi_keys(
            {row.identity for row in rows if row.kind == "session"}
        )
        t0 = time.perf_counter()
        current_identities = self._current_row_identities()
        had_rows = bool(current_identities)

        # 固定头（＋新建/看板）还没挂过或被 clear() 清掉时，即便会话集合没变
        # （全新环境首次重建前后都是空列表）也不能走原地刷新捷径直接返回，
        # 否则「＋ 新建」永远不会出现（2026-08-18 空 HOME 启动真机复现）。
        if new_identities == current_identities and self._sticky_intact():
            self._update_rows_in_place(rows)
            self.refresh_board_card()
            if select_key is not None:
                target_index = self._target_index_after_rows(
                    previous_identity, new_identities, had_rows=True
                )
                if target_index is not None:
                    self._apply_index_after_rebuild(target_index)
            elif previous_identity is None and self.index is None:
                self.index = self._sticky_count() if rows else 0
            from corral import observe
            observe.event(
                "list_rebuild",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                mode="in_place",
                card_count=len(rows),
            )
            return

        splice = None
        if self._sticky_intact():
            splice = _region_splice(current_identities, new_identities)
        if splice is not None:
            start, old_len, new_len = splice
            display_titles = self.store.snapshot()
            spliced = await self._splice_region(
                start=start,
                old_len=old_len,
                new_rows=rows[start : start + new_len],
                display_titles=display_titles,
            )
            if spliced:
                self._update_rows_in_place(rows)
                target_index = self._target_index_after_rows(
                    previous_identity, new_identities, had_rows=had_rows
                )
                if target_index is not None:
                    self._apply_index_after_rebuild(target_index)
                else:
                    self.call_after_refresh(
                        lambda: setattr(self, "_syncing_index", False)
                    )
                self._apply_split_marks()
                # 区段增删会翻转后续块的斑马纹相位，同步重贴一遍（只改 class，不动 DOM）。
                self._apply_stripes(rows)
                self.refresh_board_card()
                from corral import observe
                observe.event(
                    "list_rebuild",
                    duration_ms=int((time.perf_counter() - t0) * 1000),
                    mode="splice",
                    card_count=len(rows),
                    region_old=old_len,
                    region_new=new_len,
                )
                return

        display_titles = self.store.snapshot()
        items = [
            NoSelectListItem(NewSessionCard(), id=NEW_SESSION_ID),
            NoSelectListItem(ActivityBoardCard(self), id=ACTIVITY_BOARD_ID),
        ]
        for row in rows:
            item = self._item_for_row(row, display_titles)
            if item is not None:
                items.append(item)

        # batch_update() 抑制 clear()+extend() 中间那次多余重绘；两步都要 await
        # 完成（DOM 真正更新），批量 API 本身已经把"多次 mount"合成一轮。
        # 首铺分片：只同步挂首批（可视区+缓冲），剩余行空闲帧补齐--一次性挂
        # 200+ 卡实测冻结主线程约 1 秒，分片后首帧几十毫秒、期间可交互。
        sticky_items, scroll_items = self._partition_items(items)
        first_count = _MOUNT_CHUNK
        if previous_identity in new_identities:
            # ListView clamps its index to mounted rows. Include the selected
            # row before applying highlight so visible and logical selection
            # cannot point to different sessions while the tail is mounting.
            first_count = max(first_count, new_identities.index(previous_identity) + 1)
        first_batch, tail_items = (
            scroll_items[:first_count],
            scroll_items[first_count:],
        )
        with self.app.batch_update():
            await self._replace_list_items(sticky_items, first_batch)
        if tail_items:
            self._begin_tail_mount(tail_items, rows, self._rebuild_seq)

        target_index = self._target_index_after_rows(
            previous_identity, new_identities, had_rows=had_rows
        )
        if target_index is not None:
            self._apply_index_after_rebuild(target_index)
        else:
            self.call_after_refresh(lambda: setattr(self, "_syncing_index", False))
        # 全量重建换掉了全部 ListItem，分屏底色标记要重新贴一遍（原地更新那条
        # 路径不动列表项结构，标记还在，不必重贴）。
        self._apply_split_marks()
        self._apply_stripes(rows)
        self.refresh_board_card()
        # Textual 已知问题（issue #6300）：clear()+extend() 后紧接着设置 index，
        # 高亮理论上可能只在内部状态里正确、要等用户交互才真正刷新到屏幕。在当前
        # 锁定版本（8.2.8）下用 Pilot 直接探查过 compositor 的增量重绘路径，没有
        # 复现出"选中但不刷新"的现象——但探查手段本身有局限（无法完全模拟真实
        # 终端的部分重绘时序），显式 refresh() 成本几乎为零，保留作为兜底不会有
        # 副作用，直接加上。
        self.refresh()
        from corral import observe
        observe.event(
            "list_rebuild",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            mode="full",
            card_count=len(rows),
            chunked=bool(tail_items),
        )

    def _begin_tail_mount(
        self, items: list[ListItem], rows: list[_SidebarRow], token: int,
    ) -> None:
        """登记分片尾部并排第一批补齐定时器（复用 _rebuild_seq 作废旧批次）。"""
        self._tail_items = items
        self._tail_rows = rows
        self._tail_token = token
        self.set_timer(_TAIL_MOUNT_INTERVAL, partial(self._mount_tail_batch, token))

    async def _mount_tail_batch(self, token: int | None = None) -> None:
        """空闲帧补挂一批尾部行；任何新重建请求（seq 变化）立即让位。

        DOM 变更必须持 `_rebuild_lock`（与 rebuild 同一把闸门，防两条消息泵
        交错）；持锁后还要再验一次 token，排队期间可能已进来新重建。
        """
        if token is None:
            token = self._tail_token
        if token != self._tail_token:
            return
        if token != self._rebuild_seq or self._sticky_list is None:
            self._tail_items = []
            return
        rows = self._tail_rows
        async with self._rebuild_lock:
            if token != self._tail_token:
                return
            if token != self._rebuild_seq or self._sticky_list is None:
                self._tail_items = []
                return
            scroll = self._scroll_list
            if scroll is None:
                self._tail_items = []
                return
            batch, self._tail_items = (
                self._tail_items[:_MOUNT_CHUNK],
                self._tail_items[_MOUNT_CHUNK:],
            )
            try:
                await scroll.extend(batch)
            except Exception:  # noqa: BLE001 中间态异常就放弃尾部，下一轮重建兑底
                self._tail_items = []
                return
            # 新挂的行补贴分屏标与斑马纹（幂等；已挂前缀是 set_class no-op）。
            self._apply_split_marks()
            if rows is not None:
                self._apply_stripes(rows)
        if token == self._tail_token and self._tail_items:
            self.set_timer(_TAIL_MOUNT_INTERVAL, partial(self._mount_tail_batch, token))
