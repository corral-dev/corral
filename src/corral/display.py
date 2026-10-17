"""终端展示工具：相对时间、预览排版、会话筛选。"""

from __future__ import annotations

import unicodedata
from datetime import datetime

from rich.cells import cell_len as _rich_cell_len

from corral.models import ConversationMessage, format_message_time, session_key
from corral.projects import (
    UNKNOWN_PROJECT_LABEL,
    disambiguate_labels,
    fuzzy_match,
    normalize_cwd,
    session_project_label,
)
from corral.textutil import fit_cell, text_width

# 侧边栏时间行的亮度梯度：越新越亮，用「多久以前」分档而不是二值 dim。
# 首档（半小时内）与标题同亮，其余逐级压暗；具体颜色由 ui/session_list.py 的
# 组件样式按主题解析，这里只定义分档语义与边界。
JUST_NOW_SECONDS = 180  # 「刚刚」文案上界：3 分钟内（侧边栏对该文案再加粗）
RECENT_HIGHLIGHT_SECONDS = 1800  # 首档上界：半小时内算「刚刚还在动」
# 「一天」只给时间行亮度档用：滚动 24 小时，不是日历午夜。侧边栏日期分隔线
# 按本地日历日切桶，不再共用这条界。
TODAY_SECONDS = 86400
TIME_BRIGHTNESS_TIERS: tuple[tuple[float | None, str], ...] = (
    (RECENT_HIGHLIGHT_SECONDS, "fresh"),  # 半小时内：与标题同色
    (3 * 3600, "recent"),                 # 三小时内
    (TODAY_SECONDS, "today"),             # 一天内
    (None, "old"),                        # 更早（显示绝对日期）
)


def _time_brightness_tier(mtime: float, now: float | None = None) -> str:
    """会话时间落在哪一档亮度上（`TIME_BRIGHTNESS_TIERS` 的档位名）。

    未来时间 / 时钟漂移导致的负差值算最新一档，与 `_format_relative_time` 把
    负值渲染成「刚刚」保持一致；缺时间戳则按最旧一档处理。
    """
    if not mtime:
        return TIME_BRIGHTNESS_TIERS[-1][1]
    if now is None:
        now = datetime.now().timestamp()
    delta = now - mtime
    for upper, tier in TIME_BRIGHTNESS_TIERS:
        if upper is None or delta < upper:
            return tier
    return TIME_BRIGHTNESS_TIERS[-1][1]


def _format_relative_time(mtime: float, now: float | None = None) -> str:
    """把时间戳渲染成人性化相对时间；超过一天退回绝对日期时间。

    展示层专用，只在 TUI 渲染时现算，不写回 display_time（后者保持绝对格式，
    供 --json 与单测稳定消费）。
    """
    if now is None:
        now = datetime.now().timestamp()
    delta = now - mtime
    from corral.i18n import t

    if delta < JUST_NOW_SECONDS:  # 含未来时间 / 时钟漂移导致的负值
        return t("time.just_now")
    if delta < 3600:
        return t("time.minutes_ago", n=int(delta // 60))
    if delta < TODAY_SECONDS:
        return t("time.hours_ago", n=int(delta // 3600))
    return datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")



def _char_width(ch: str) -> int:
    # 与 `embed._char_width`、Rich/Textual 的渲染宽度表保持一致（`rich.cells.cell_len`）：
    # 自实现的 `unicodedata.east_asian_width` 在 emoji、组合字符、ambiguous-width
    # 字符上会跟 Rich 的排版结果不一致，本项目同时用这两套计算（列表卡片排版
    # 和内嵌画面渲染各自有一份），会导致 CJK/emoji 对齐错位。
    return _rich_cell_len(ch)


# 预览正文的 Markdown 配色：**刻意只用 bold/dim/italic，不引入任何颜色**。
# Rich 自带的 markdown.* 默认主题是洋红标题 + 青色代码，而且内联代码与代码块
# 都写死了 `on black` 底色——套进右栏既跟面板底色打架（浅色终端下就是一块黑），
# 满屏高饱和色也正是机主 2026-08-05 说的「看起来很刺眼」。颜色在这份预览里只
# 承担一件事：区分「谁说的」（角色抬头与分隔线）。
_MARKDOWN_QUIET_THEME_STYLES = {
    "markdown.h1": "bold",
    "markdown.h2": "bold",
    "markdown.h3": "bold",
    "markdown.h4": "bold",
    "markdown.h5": "bold",
    "markdown.h6": "bold",
    "markdown.code": "bold",
    "markdown.code_block": "none",
    "markdown.item.bullet": "dim",
    "markdown.item.number": "dim",
    "markdown.list": "none",
    "markdown.block_quote": "dim",
    "markdown.link": "underline",
    "markdown.link_url": "dim underline",
    "markdown.table.border": "dim",
    "markdown.table.header": "bold",
    "markdown.hr": "dim",
}


def _markdown_renderable(text: str, width: int):
    """把一条消息正文渲染成可直接交给右栏的 Rich 可渲染对象。

    两处必须保留的偏差，改动前先看清楚：

    1. **关掉 HTML 解析**（`{"html": False}`）。CommonMark 默认把 `<foo>…</foo>`
       当 HTML 块，而 Rich 的 markdown 没有 HTML 元素处理器，整段会被**静默丢弃**
       ——助手和用户消息里 `<system-reminder>`、`<Thinking>`、`<urlopen error>`
       这类尖括号内容极常见，实测整条消息会变成空白。关掉之后它们按普通文字原样
       显示。历史查看器丢内容比不支持 markdown 严重得多，这条不能省。
    2. **代码块不要 Rich 默认的 monokai**：那是写死的深色背景块，浅色终端下突兀，
       也盖掉了面板自己的底色。这里换成跟随终端调色板的 ANSI 主题 + 透明背景。

    已知且接受的取舍：`__init__.py` 这类没包反引号的双下划线标识符，会按
    CommonMark 语义被解析成加粗的 `init.py`（GitHub 上同样如此）。文字不会丢，
    只是下划线被当成了标记；要根治只能整体关掉强调语法，代价更大。
    """
    from rich.console import Console
    from rich.theme import Theme

    width = max(1, width)
    console = Console(
        width=width,
        theme=Theme(_MARKDOWN_QUIET_THEME_STYLES, inherit=True),
        color_system="truecolor",
        legacy_windows=False,
    )
    markdown = _QuietMarkdown(text)
    return _PreRenderedLines(
        console.render_lines(markdown, console.options.update(width=width), pad=False)
    )


class _PreRenderedLines:
    """把「已经渲染好的 Segment 行」原样交给上层（Textual 的 Visual 管线接受它）。

    预览的 markdown 必须用我们自己的 Theme 渲染（见 `_markdown_renderable`），
    而那份 Theme 只能挂在自己的 Console 上；渲染结果用这个壳带出来，Textual
    再照常把它编译成 Strip，不需要在界面层特判。
    """

    def __init__(self, lines) -> None:
        self._lines = lines

    def __rich_console__(self, console, options):
        from rich.segment import Segment

        newline = Segment.line()
        for line in self._lines:
            yield from line
            yield newline


def _quiet_markdown_class():
    """惰性构造 Markdown 子类：`rich.markdown` 导入不便宜，只有真渲染时才付。"""
    from markdown_it import MarkdownIt
    from rich.markdown import CodeBlock, Markdown
    from rich.syntax import Syntax

    class _CodeBlock(CodeBlock):
        def __rich_console__(self, console, options):
            yield Syntax(
                str(self.text).rstrip(),
                self.lexer_name,
                theme="ansi_dark",
                background_color="default",  # 透明：跟随面板/终端底色
                word_wrap=True,
                padding=0,
            )

    class _Markdown(Markdown):
        elements = {**Markdown.elements, "fence": _CodeBlock, "code_block": _CodeBlock}

        def __init__(self, markup: str) -> None:
            super().__init__(markup)
            self.parsed = (
                MarkdownIt("commonmark", {"html": False})
                .enable("strikethrough")
                .enable("table")
                .parse(markup)
            )

    return _Markdown


class _LazyQuietMarkdown:
    """`_QuietMarkdown(text)` 首次调用时才真正建类（见 `_quiet_markdown_class`）。"""

    _cls = None

    def __call__(self, markup: str):
        if _LazyQuietMarkdown._cls is None:
            _LazyQuietMarkdown._cls = _quiet_markdown_class()
        return _LazyQuietMarkdown._cls(markup)


_QuietMarkdown = _LazyQuietMarkdown()


def _rule_style(role_style: str) -> str:
    """消息分隔线的样式：取角色色但压暗，别让整条横线抢过正文。"""
    color = role_style.replace("bold", "").strip()
    return f"dim {color}" if color else "dim"


def _preview_blocks(
    messages: list[ConversationMessage],
    runtime_name: str,
    width: int,
    *,
    user_style: str = "bold cyan",
    assistant_style: str = "dim",
):
    """把真实会话消息整理成右栏可直接渲染的块序列（Rich 可渲染对象列表）。

    每条消息三块：**角色色的分隔横线**（第一条不画）→ **角色抬头**（着色，右挂
    淡色时间）→ **Markdown 正文**（顶格、吃满整格宽、不着角色色）。

    这套版式是 2026-08-05 按机主看真机截图后的反馈定下的，几条都别改回去：
    正文跟在「角色: 」后面会被前缀吃掉一大截行宽，长消息在窄格里几乎排不下，
    时间戳还会被挤到抬头行末折下来；正文整段套品牌色读长对话很刺眼；消息之间
    用空行分隔在长对话里几乎看不出边界，换成角色色的横线才一眼分得清谁在说话。
    """
    from rich.style import Style
    from rich.text import Text

    from corral.i18n import t

    content_width = max(1, width - 2)
    if not messages:
        return [Text(t("detail.empty_preview"), style="dim")]

    blocks: list[object] = []
    for message_index, message in enumerate(messages):
        if message.role == "user":
            role, role_style = t("preview.you"), user_style
        else:
            role, role_style = f"◆ {runtime_name}", assistant_style
        if blocks:
            blocks.append(Text("─" * content_width, style=_rule_style(role_style)))
        head = Text(fit_cell(role, content_width).rstrip(), style=role_style)
        if message.timestamp:
            head.append(f"  · {format_message_time(message.timestamp)}", style="dim")
        head.stylize(Style(meta={"corral_message_index": message_index}))
        blocks.append(head)
        blocks.append(_markdown_renderable(_clean_preview_text(message.text), content_width))
    # 三行尾：空行 → 居中 ── END ── → 空行。`content_width` 是面板内容宽；
    # 居中线段左右用 `─` 补到整行宽——`str.center` 把 `─` 拆成 bytes，宽度会错。
    blocks.append(Text(""))
    end_text = t("detail.preview_end")
    end_w = text_width(end_text)
    left_dashes = (content_width - end_w) // 2
    end_line = Text("─" * left_dashes + end_text, style="dim")
    end_line.append("─" * (content_width - text_width(end_line.plain)), style="dim")
    blocks.append(end_line)
    blocks.append(Text(""))
    return blocks


def _clean_preview_text(text: str) -> str:
    """去掉会破坏 TUI 的控制字符；制表符按四空格展开后再交给 markdown。

    ZWNJ/ZWJ 虽属 Cf，但是文字连写和 emoji grapheme 的有效组成字符，不能像其他
    控制字符一样替换成空格（与 `wrap_preview_text` 同一条约束）。
    """
    return "".join(
        ch if ch in "\n\t\u200c\u200d" or unicodedata.category(ch)[0] != "C" else " "
        for ch in text.strip()
    ).replace("\t", "    ")


SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"  # braille 转圈圈，每帧占 1 列宽


def _project_groups(sessions_by_source: dict[str, list[dict]]) -> list[dict]:
    """合并所有来源的会话，按工作目录分组统计，用于侧边栏展示。

    每项：{"cwd_key": 完整归一化路径（过滤用，"" 表示未知目录）,
           "label": 去歧义后的显示名, "count": 会话数, "latest_mtime": 最近会话时间}。
    排序：会话数倒序 → 最近会话时间倒序 → 显示名字典序（稳定兜底）。
    """
    groups: dict[str, dict] = {}
    for bucket in sessions_by_source.values():
        for session in bucket:
            key = normalize_cwd(session.get("cwd"))
            entry = groups.setdefault(key, {"cwd_key": key, "count": 0, "latest_mtime": 0.0})
            entry["count"] += 1
            mtime = session.get("mtime") or 0
            if mtime > entry["latest_mtime"]:
                entry["latest_mtime"] = mtime

    named_keys = [key for key in groups if key]
    labels = disambiguate_labels(named_keys)
    for key in named_keys:
        groups[key]["label"] = labels[key]
    if "" in groups:
        groups[""]["label"] = UNKNOWN_PROJECT_LABEL

    return sorted(groups.values(), key=lambda p: (-p["count"], -p["latest_mtime"], p["label"]))


def _filter_sessions(sessions: list[dict], cwd_key: str | None) -> list[dict]:
    """按归一化工作目录精确匹配过滤；cwd_key 为 None 时原样返回（不过滤）。"""
    if cwd_key is None:
        return sessions
    return [s for s in sessions if normalize_cwd(s.get("cwd")) == cwd_key]


def _filter_sessions_by_query(
    sessions: list[dict],
    query: str,
    *,
    titles: dict[str, str] | None = None,
) -> list[dict]:
    """按项目名/路径/会话标题做大小写无关模糊过滤；空查询不过滤。"""
    needle = (query or "").strip()
    if not needle:
        return sessions
    titles = titles or {}
    out: list[dict] = []
    for session in sessions:
        cwd_key = normalize_cwd(session.get("cwd"))
        title = titles.get(session_key(session), "")
        fallback = str(session.get("fallback_title") or "")
        if fuzzy_match(
            needle,
            session_project_label(session),
            cwd_key,
            str(session.get("cwd_display") or ""),
            title,
            fallback,
        ):
            out.append(session)
    return out
