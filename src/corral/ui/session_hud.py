"""会话小窗：右栏格右上角的悬浮摘要，一眼看清「这个会话在干啥」。

形态与取舍（改之前先读）：

- **默认展开列出全部真人提问**；浮层有高度上限，多出来的靠区域内滚动看。
  不要再砍中间或画「中间省略 N 条」——那会让中间提问不可见。收起态才只留
  「条数 + 最初 + 最近」三行，把遮挡与滚轮穿透代价压到最小（Textual 没有点击穿透）。
- **顺序恒为从上到下、由旧到新**，与右栏完整对话一致。
- **只列真人提问**：`load_conversation` 仍保留完整 user 轮次（导出/预览要看原文），
  但小窗标题是 Your prompts，必须再滤掉 runtime 注入、管家角色提示等。corral 自身的
  跨运行时接力提示词视作真人提问，不在这里过滤。
- **过长提问折叠**：展开态每条最多 `_MAX_PROMPT_LINES` 行，超出末行加 `...`；
  整窗高度靠滚动封顶。条纹按提问块交替，半透明叠在浮层底上，不跟 hover 抢底。
- **实时托管格与静态对话预览格都画，且每个分屏格各自一份**。长对话里完整预览仍
  要翻很久，小窗用来扫提问脉络；多分屏时每一格画自己的提问摘要。
- **底色是格子高光的一部分（2026-09-12）**：选中 / 持焦格用 `$pane-active-background`
  （跟高光条一样）；没选中用 `$panel`（比 `$surface` / 助手深色画布抬一层，避免浮层
  融进会话画面）。斑马纹仍叠在当前底上。
- 用 `dock: right` + `width/height: auto` 把浮层贴到右上角：这样浮层的命中区域**只有
  胶囊本身**。不要改成「整行宽的容器里右对齐」（`UpdateToast` 那种写法）——那会让
  整条横带都吃掉鼠标事件，托管画面顶部一整行都滚不动。
- 浮层挂在 `PaneCell` 的 `hud` 层（见 `PaneCell` 的 `layers`）。同层内单独排版，
  `margin-top: 1` 让它落在标题栏下面第一行，而不是压住分栏标题。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from rich.text import Text
from textual import events
from textual.color import Color as TextualColor
from textual.geometry import Size
from textual.message import Message
from textual.widget import Widget

from corral.i18n import t
from corral.models import ConversationMessage
from corral.textutil import fit_cell, text_width, wrap_preview_text

_MIN_WIDTH = 16
# 内容宽度上限。展开态每条提问会折叠，不必为了读完整句把浮层铺满整格。
# 这只是**上限**：真实宽度仍取 `min(上限, 本格可用宽度 - 4)`，三分屏那种窄格
# 不受影响；收起态还会再按内容实宽收缩。
_MAX_WIDTH = 55
# 窄到这个宽度以下（三分屏 + 窄终端）直接不画：浮层会把整格盖掉。
_MIN_PANE_WIDTH = 24
# 缺时间戳时的占位（Cursor 等历史里没有逐条时间）。必须恰好 5 格，与 `HH:MM` /
# `MM-DD` 同宽，后面再拼两格间距；只用 ASCII，避免 CJK 双宽把列挤歪。
_MISSING_TIME = "N/A  "
# 展开态的高度上下限。单条提问已折叠，整窗仍要封顶——浮层再有用也不能把整格
# 助手画面盖掉；超出部分靠滚动看。
_MIN_EXPANDED_HEIGHT = 6
_MAX_EXPANDED_HEIGHT = 20
_SCROLL_STEP = 3
# 展开态每条提问最多占几行；再长末行加省略号。收起态本来就是一行截断。
_MAX_PROMPT_LINES = 4
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FAILURE_SECONDS = 2.0
# 条纹叠在**当前**浮层底上：选中格是 `$pane-active-background`，没选中是
# `$panel`（抬离助手画布）。叠 `$primary`，不要叠 `$foreground`
# （那是灰，会把激活条的绿洗成泥灰）。40% 才能在绿底上看得出一块更亮的
# 条；灰底上同样看得出斑马纹。hover 切到 `$primary-muted` 时跟着走。
_STRIPE_BLEND = 0.40

# Codex 把粘贴图片收成一对空标签，后面才是用户打的字；小窗只关心那句人话。
_IMAGE_WRAP_RE = re.compile(r"<image\b[^>]*>\s*</image>\s*", re.IGNORECASE | re.DOTALL)

# 本机全量历史抽查（Claude/Cursor/Codex/Kimi/OpenCode/Pi，约 2700 条 user）
# 里会混进 Your prompts 的注入类。只认高置信特征，不碰 `$doc-update`、`/grilling`
# 这种用户自己敲的技能快捷方式。
_INJECTED_PREFIXES = (
    "<turn_aborted>",
    # Codex context it injects as user input but never renders as a prompt
    # (verified on real 0.163.0 sessions, 2026-10-19). Native prompt
    # browsing counts rendered prompts, so these also shift its ordinals.
    "<codex_internal_context",
    "<recommended_plugins>",
    "<external_codex_apps_open_page>",
    "<user_shell_command>",
    "<user_action>",
    "<codex_delegation>",
    "<in-app-browser-context",
    # Claude compaction summary, rendered as a summary, not as a prompt.
    "This session is being continued from a previous conversation",
    "<subagent_notification>",
    "<skill>",
    "<local-command",
    "<command-name>",
    "<command-message>",
    "<system-reminder>",
    "<environment_context>",
    "# AGENTS.md instructions",
    "你是 OpenConductor 的",
    "你将看到一批编程助手会话的摘录",
    "No response requested.",
    "Reply with exactly:",
    "【权威对话账本",
    "先按项目现有规则完成 build/test/lint",
    "对本仓库当前的 git diff 做一次 code review",
)
_INJECTED_SUBSTRINGS = (
    "Implement the plan as specified, it is attached for your reference",
    "Do NOT edit the plan file itself",
    "To-do's from the plan have already been created",
    "Briefly inform the user about the task result",
    "【本轮回复契约】",
)
# 注意：corral 自身的跨运行时接力提示词（「你正在接力一个来自…」「You are picking up
# a session from…」「这是跨运行时接力…」）视作真人提问，不在这里过滤。
_INJECTED_EXACT = {
    "Implement the plan.",
    "只回复 OK。",
    "只回复 RESUMED。",
}


@dataclass(frozen=True)
class HudData:
    """小窗要展示的会话摘要。

    `entries` 是 (时间, 单行正文)，**从旧到新**排——与右栏完整对话、与人读聊天记录
    的方向一致。`entries[0]` 恒为本会话**最早**那条提问（判断"这个会话本来是要干嘛"），
    `entries[-1]` 是最新一条（判断"现在做到哪"）。展开态列出全部条目；收起态只取两头。
    """

    count: int
    entries: tuple[tuple[str, str], ...] = ()
    message_indices: tuple[int, ...] = ()
    message_tokens: tuple[str, ...] = ()

    @property
    def oldest(self) -> tuple[str, str] | None:
        return self.entries[0] if self.entries else None

    @property
    def latest(self) -> tuple[str, str] | None:
        return self.entries[-1] if self.entries else None

    def __bool__(self) -> bool:
        return bool(self.entries)


def _short_time(timestamp: float, now: float | None = None) -> str:
    """小窗自己的时间列：当天只给 `HH:MM`，更早只给 `MM-DD`。

    不复用 `format_message_time`（`MM-DD HH:MM`）：那是右栏完整对话和侧边栏共用的
    格式，它们有整行宽度可用；小窗横向寸土寸金，11 格的时间列会把正文挤掉一截，
    而同一个会话里绝大多数提问都在当天，日期是纯冗余。两种写法都恰好 5 格宽，
    混排时列也不会错位；`:` 与 `-` 足以区分是几点还是哪天。
    """
    stamp = datetime.fromtimestamp(timestamp)
    today = datetime.fromtimestamp(now) if now is not None else datetime.now()
    if stamp.date() == today.date():
        return stamp.strftime("%H:%M")
    return stamp.strftime("%m-%d")


def is_injected_user_prompt(text: str) -> bool:
    """这条 user 轮次是不是 runtime / 管家注入，而不是人敲进去的。

    `load_conversation` 必须保留原文（`corral show`/`export`、右栏预览都还要用）；
    Your prompts 小窗才在这里二次过滤。特征来自本机全量历史抽查，宁可漏一条
    注入，也不要把 `$doc-update`、带图提问这种真人输入误杀。
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    if raw in _INJECTED_EXACT:
        return True
    # corral 跨运行时接力提示词视作真人提问：不再以「任务：」开头做整条过滤。
    if raw.startswith("原始任务：") and (
        "用户最新补充" in raw or "【本轮回复契约】" in raw
    ):
        return True
    if raw.startswith(_INJECTED_PREFIXES):
        return True
    return any(marker in raw for marker in _INJECTED_SUBSTRINGS)


def _one_line(text: str) -> str:
    """把多行提问压成一行：换行/制表一律折成单空格，连续空白合并。

    Codex 图片粘贴会在正文前塞一对空的 `<image …></image>`，小窗里只留人话；
    纯图片没有配文时落成 `[image]`，避免整条被当成空消息丢掉。
    """
    raw = str(text or "")
    stripped = _IMAGE_WRAP_RE.sub("", raw)
    compact = " ".join(stripped.split())
    if compact:
        return compact
    if _IMAGE_WRAP_RE.search(raw):
        return "[image]"
    return ""


def _hud_stripe_color(background: TextualColor, accent: TextualColor) -> TextualColor:
    """把 `$primary` 叠进当前浮层底，得到斑马纹。

    选中底是 `$pane-active-background`（绿），没选中是 `$panel`（跟未选中顶底条同色）。
    调用方禁止传入 `$foreground`：那是灰，会把激活条的绿洗脏。
    """
    return background.blend(accent, _STRIPE_BLEND)


def _single_prompt_identity(message, index: int) -> str:
    digest = hashlib.blake2b(digest_size=16)
    for value in (index, message.role, message.text, message.timestamp):
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def prompt_identity(messages, index: int) -> str:
    return _single_prompt_identity(messages[index], index)


def _prompt_identities(messages) -> list[str]:
    return [_single_prompt_identity(message, index) for index, message in enumerate(messages)]


def summarize_user_messages(messages: list[ConversationMessage]) -> HudData:
    """从会话对话里挑出真人提问，按从旧到新整理成小窗摘要。

    只认 `role == "user"`，并丢掉 `is_injected_user_prompt` 命中的注入轮次。
    扫描层已经挡掉 Claude/Kimi 的 `origin.kind` 系统事件；Cursor 计划附件、
    Codex `<skill>`/`<turn_aborted>`、OpenConductor 角色提示仍会以 user 身份进
    `load_conversation`，必须在这里再滤一次。corral 自己的跨运行时接力提示词
    视作真人提问，不在这里过滤。

    Native duplicate records are normalized upstream. Every actual user turn,
    including consecutive identical prompts, retains its own navigation identity.

    过滤后的提问**全部**进入 `entries`；遮挡面积只靠展开态高度上限 + 滚动控制，
    禁止砍中间。
    """
    users = [
        (index, m) for index, m in enumerate(messages)
        if m.role == "user" and _one_line(m.text) and not is_injected_user_prompt(m.text)
    ]
    if not users:
        return HudData(0, ())

    def _entry(message: ConversationMessage) -> tuple[str, str]:
        stamp = _short_time(message.timestamp) if message.timestamp else ""
        return stamp, _one_line(message.text)

    identities = _prompt_identities(messages)
    return HudData(len(users), tuple(_entry(m) for _, m in users), tuple(i for i, _ in users),
                   tuple(identities[i] for i, _ in users))


def _plural(key: str, count: int, **kwargs: object) -> str:
    """英文单复数：中文不需要但同名 key 也必须给，否则中文界面会冒出英文。"""
    if count == 1:
        return t(f"{key}_one", count=count, **kwargs)
    return t(key, count=count, **kwargs)


class SessionHud(Widget):
    """右上角悬浮小窗本体。数据与展开状态都由 `MainScreen` 统一喂，自己不查 store。"""

    class PromptSelected(Message):
        def __init__(self, body: str, message_index: int | None = None, token: str | None = None) -> None:
            super().__init__()
            self.body = body
            self.message_index = message_index
            self.token = token

    ALLOW_SELECT = False
    can_focus = False
    COMPONENT_CLASSES = {"session-hud--pressed", "session-hud--status"}

    # 底色用分栏激活条那个变量：小窗本来就是「当前这一格」的附属信息，视觉上跟着
    # 该格的高亮条走，一眼看出是 corral 的浮层而不是助手自己画的。
    # **不要加边框**：托管画面底色跟着用户终端走，边框线在某些终端字体下会连成
    # 实心方块（分栏分隔线就是为此不画的）；整块实底本身已经足够"浮起来"，还省下
    # 边框那两行两列的遮挡。
    DEFAULT_CSS = """
    SessionHud {
        layer: hud;
        dock: right;
        width: auto;
        height: auto;
        margin: 1 1 0 0;
        padding: 0 1;
        background: $panel;
        color: auto 90%;
        display: none;
    }
    SessionHud.-visible {
        display: block;
    }
    SessionHud.-active {
        background: $pane-active-background;
    }
    SessionHud:hover {
        background: $primary-muted;
    }
    SessionHud > .session-hud--pressed {
        background: $primary;
        color: $text;
    }
    SessionHud > .session-hud--status {
        text-style: bold;
    }
    """

    def __init__(self, on_toggle: Callable[[], None] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._data = HudData(0, ())
        self._expanded = False
        self._pressed_entry: int | None = None
        self._press_timer = None
        # Clicked-row navigation status keyed by source message index:
        # ("locating", "") or ("failed", reason text).
        self._status: dict[int, tuple[str, str]] = {}
        self._spinner_frame = 0
        self._spinner_timer = None
        self._status_timers: dict[int, object] = {}
        self._on_toggle = on_toggle
        # 展开态正文的滚动位置（行）。`lines()` 每次按实际可见高度重算上限并夹紧，
        # 所以窗口变小、内容变少都不会滚出界。默认钉在最底（最新提问）；用户上滚
        # 后取消钉底，新内容到来不再强行跳回；滚回底部或重新展开后恢复钉底。
        self._scroll = 0
        self._max_scroll = 0
        self._stick_bottom = True

    # ---- 外部驱动 ----

    def update_data(self, data: HudData | None, *, expanded: bool) -> None:
        """喂新数据；data 为空（没有提问）时整个小窗不出现。"""
        changed = (data or HudData(0, ())) != self._data or expanded != self._expanded
        if changed:
            self._pressed_entry = None
        if expanded != self._expanded:
            # 每次重新展开都钉在最新；沿用上次的上滚位置会让人以为最新提问丢了。
            self._stick_bottom = True
            self._scroll = 0
        self._data = data or HudData(0, ())
        self._expanded = expanded
        self.set_class(bool(self._data), "-visible")
        self.set_class(bool(self._data) and expanded, "-expanded")
        if changed:
            # 行数/宽度都可能变，必须连布局一起刷新。
            self.refresh(layout=True)

    def hide(self) -> None:
        if not self._data and not self.has_class("-visible"):
            return
        self._data = HudData(0, ())
        self.set_class(False, "-visible")
        self.set_class(False, "-expanded")
        self.refresh(layout=True)

    def set_active(self, active: bool) -> None:
        """浮层底色跟格子顶底高光走：选中高光条，没选中跟灰条同色（`$panel`）。"""
        if self.has_class("-active") == active:
            return
        self.set_class(active, "-active")
        self.refresh()

    @property
    def expanded(self) -> bool:
        return self._expanded

    @property
    def data(self) -> HudData:
        return self._data

    # ---- 尺寸与内容 ----

    def _inner_width(self, available: int) -> int:
        """按所在格的可用宽度算内容宽度；留出右边距（1）与左右内边距（2）。"""
        return max(_MIN_WIDTH, min(_MAX_WIDTH, available - 4))

    def _max_height(self, available: int) -> int:
        """展开态的最高行数：不超过本格能给的高度，也不允许无限长到盖满整格。"""
        return max(_MIN_EXPANDED_HEIGHT, min(_MAX_EXPANDED_HEIGHT, available - 3))

    def _stripe_on(self) -> str:
        """当前浮层底上叠 `$primary`，得到斑马纹。

        禁止叠 `$foreground`：那是灰，会把激活条的绿洗脏。必须从
        `styles.background` 混合：选中是绿底、没选中是面板灰、hover 切
        `$primary-muted` 时条纹都跟着走。
        """
        try:
            app = self.app
        except Exception:
            return ""
        background = self.styles.background
        if not isinstance(background, TextualColor) or background.ansi is not None:
            return ""
        try:
            primary_raw = app.get_css_variables().get("primary") or ""
            primary = TextualColor.parse(str(primary_raw))
        except Exception:
            return ""
        mixed = _hud_stripe_color(background, primary)
        hexcol = getattr(mixed, "hex", "") or ""
        return f"on {hexcol}" if hexcol else ""

    @staticmethod
    def _paint_stripe(line: Text, stripe_on: str) -> Text:
        if stripe_on:
            line.stylize(stripe_on)
        return line

    def _prefixed(self, prefix: str, body: str, width: int) -> Text:
        """收起态用：前缀（最初/最近标签）淡色，正文一行内截断。"""
        line = Text(no_wrap=True)
        line.append(prefix, style="dim")
        line.append(fit_cell(body, max(1, width - text_width(prefix)), ellipsis=True))
        return line

    def _entry_lines(self, stamp: str, body: str, width: int, status: tuple[str, str] | None = None) -> list[Text]:
        """展开态的一条提问：最多 `_MAX_PROMPT_LINES` 行，超出末行加 `...`。

        续行缩进到与首行正文对齐。时间列固定 5 格 + 2 格间距，续行前缀用等宽
        空白顶齐。缺时间戳时用 `N/A` 占位，不留空白缩进——否则 Cursor 这类没有
        逐条时间的会话看起来像列坏了。
        """
        cell = stamp if stamp else _MISSING_TIME
        prefix = f"{cell}  "
        indent = " " * text_width(prefix)
        body_width = max(1, width - text_width(prefix))
        wrapped = wrap_preview_text(body, body_width) or [""]
        if status is not None:
            # The status replaces the time column. Wrap with the ordinary
            # prefix so the row keeps its height and rows never shift under
            # the pointer; only the first line is shortened to fit.
            kind, reason = status
            label = (f"{_SPINNER[self._spinner_frame % len(_SPINNER)]} {t('hud.locating')}"
                     if kind == "locating" else reason)
            prefix = f"{label}  "
        truncated = len(wrapped) > _MAX_PROMPT_LINES
        if truncated:
            wrapped = wrapped[:_MAX_PROMPT_LINES]
        out: list[Text] = []
        last = len(wrapped) - 1
        for index, part in enumerate(wrapped):
            line = Text(no_wrap=True)
            text = part if not (truncated and index == last) else f"{part}..."
            if index == 0 and status is not None:
                line.append(prefix, style=self._status_style())
                line.append(fit_cell(text, max(1, width - text_width(prefix)), ellipsis=True))
            else:
                line.append(prefix if index == 0 else indent, style="dim")
                line.append(fit_cell(text, body_width, ellipsis=truncated and index == last))
            out.append(line)
        return out

    def _expanded_body(self, width: int, stripe_on: str = "") -> list[Text]:
        """展开态除页眉/页脚外的全部内容行（未按可见高度裁切）。"""
        out: list[Text] = []
        for index, (stamp, body) in enumerate(self._data.entries):
            paint = stripe_on if index % 2 else ""
            status = self._entry_status(index)
            for line in self._entry_lines(stamp, body, width, status):
                self._paint_stripe(line, paint)
                if index == self._pressed_entry or (status is not None and status[0] == "locating"):
                    line.pad_right(max(0, width - text_width(line.plain)))
                    line.stylize(self.get_component_rich_style("session-hud--pressed"))
                out.append(line)
        return out

    def lines(self, width: int, max_height: int | None = None) -> list[Text]:
        """按给定内容宽度生成每一行；所有行补齐到同宽，浮层底色才是规整的矩形。

        两种形态都是**从上到下、从旧到新**：收起态只留两头（最初一条 + 最近一条），
        展开态列出全部提问。方向和右栏完整对话一致，不要改成最新在最前。

        展开态超过 `max_height` 时不截断内容，而是**只画一个窗口**（页眉与页脚始终
        可见，中间正文按 `_scroll` 滚动）——页脚是唯一写着"点击收起"的地方，被滚出去
        用户就找不到出口了。默认窗口钉在最底，保证最新提问始终在视野里。
        """
        data = self._data
        if not data:
            return []

        stripe_on = self._stripe_on()
        if not self._expanded:
            out: list[Text] = [Text(
                fit_cell(_plural("hud.count", data.count), width, ellipsis=True),
                style="bold", no_wrap=True,
            )]
            label_width = max(
                text_width(t("hud.label_first")), text_width(t("hud.label_latest")),
            ) + 2
            oldest, latest = data.oldest, data.latest
            # 收起态两行提问也按块斑马：最初不涂、最近涂一层，和展开态 index%2 同相。
            if data.count > 1 and oldest is not None:
                out.append(self._prefixed(
                    fit_cell(t("hud.label_first"), label_width), oldest[1], width,
                ))
            if latest is not None:
                out.append(self._paint_stripe(
                    self._prefixed(
                        fit_cell(t("hud.label_latest"), label_width), latest[1], width,
                    ),
                    stripe_on if data.count > 1 else "",
                ))
            return out

        body = self._expanded_body(width, stripe_on)
        capacity = len(body) if max_height is None else max(1, max_height - 2)
        self._max_scroll = max(0, len(body) - capacity)
        if self._stick_bottom:
            self._scroll = self._max_scroll
        else:
            self._scroll = max(0, min(self._scroll, self._max_scroll))
        window = body[self._scroll:self._scroll + capacity]
        hint = t("hud.collapse_hint_scroll") if self._max_scroll else t("hud.collapse_hint")
        return [
            Text(fit_cell(t("hud.title", count=data.count), width, ellipsis=True),
                 style="bold", no_wrap=True),
            *window,
            Text(fit_cell(hint, width, ellipsis=True), style="dim", no_wrap=True),
        ]

    def get_content_width(self, container: Size, viewport: Size) -> int:
        if not self._data or container.width < _MIN_PANE_WIDTH:
            return 0
        width = self._inner_width(container.width)
        if self._expanded:
            return width
        # 收起态按内容实宽收缩，短提问不必画成一整条长条。
        rendered = self.lines(width)
        if not rendered:
            return 0
        # 各行等宽是浮层底色画成规整矩形的前提，所以取最宽那行，再由 lines() 统一补齐。
        return min(width, max(text_width(line.plain.rstrip()) for line in rendered))

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        if not self._data or container.width < _MIN_PANE_WIDTH:
            return 0
        return len(self.lines(
            width or self._inner_width(container.width),
            self._max_height(container.height) if self._expanded else None,
        ))

    def render(self) -> Text:
        width = self.content_size.width or self._inner_width(self.container_size.width)
        # 高度必须取**已经分配给自己的** content 高度，不能在这里拿 container 高度
        # 再算一遍 `_max_height()`：布局阶段（`get_content_height`）和渲染阶段看到的
        # container 尺寸不保证一致（首帧、resize 中间态都可能差一拍），两边各算各的
        # 就会出现"底色框 20 行、正文只有 6 行"这种背景高度与文字对不上的现象。
        # 按分配高度开窗，行数与框高恒等。
        max_height = None
        if self._expanded:
            max_height = self.content_size.height or self._max_height(
                self.container_size.height,
            )
        rendered = self.lines(width, max_height)
        out = Text(no_wrap=True)
        for index, line in enumerate(rendered):
            if index:
                out.append("\n")
            out.append_text(line)
        return out

    # ---- Interaction: rows navigate; header/footer collapse without taking focus. ----

    def _entry_at(self, y: int) -> int | None:
        if not self._expanded:
            return None
        local_y = y - self.styles.padding.top
        if not 0 < local_y < self.content_size.height - 1:
            return None
        width = self.content_size.width or self._inner_width(self.container_size.width)
        self.lines(width, self.content_size.height or None)
        row = local_y - 1 + self._scroll
        for index, (stamp, body) in enumerate(self._data.entries):
            count = len(self._entry_lines(stamp, body, width))
            if 0 <= row < count:
                return index
            row -= count
        return None

    def _status_style(self):
        try:
            return self.get_component_rich_style("session-hud--status")
        except Exception:
            return "bold"  # Component styles exist only once the widget is mounted.

    def _entry_status(self, index: int) -> tuple[str, str] | None:
        if not self._data.message_indices or index >= len(self._data.message_indices):
            return None
        return self._status.get(self._data.message_indices[index])

    def set_prompt_status(self, message_index: int, kind: str | None, reason: str = "") -> None:
        """Show a clicked row's navigation state in its time column.

        `kind` is "locating", "failed" (cleared after a short delay) or None.
        Rows keep their height in every state.
        """
        timer = self._status_timers.pop(message_index, None)
        if timer is not None:
            timer.stop()
        if kind is None:
            self._status.pop(message_index, None)
        else:
            self._status[message_index] = (kind, reason)
            if kind == "failed":
                self._status_timers[message_index] = self.set_timer(
                    _FAILURE_SECONDS, lambda: self.set_prompt_status(message_index, None))
        locating = any(value[0] == "locating" for value in self._status.values())
        if locating and self._spinner_timer is None:
            self._spinner_timer = self.set_interval(0.1, self._advance_spinner)
        elif not locating and self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self.refresh()

    def prompt_status(self, message_index: int) -> str | None:
        status = self._status.get(message_index)
        return status[0] if status else None

    def _advance_spinner(self) -> None:
        self._spinner_frame += 1
        self.refresh()

    def _clear_pressed(self) -> None:
        self._pressed_entry = None
        self.refresh()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        event.stop()
        event.prevent_default()
        if event.button == 1:
            if self._press_timer is not None:
                self._press_timer.stop()
            self._pressed_entry = self._entry_at(event.y)
            self.refresh()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button == 1:
            self._press_timer = self.set_timer(0.2, self._clear_pressed)

    def on_leave(self, event: events.Leave) -> None:
        self._clear_pressed()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        if event.button != 1:
            return
        index = self._entry_at(event.y)
        if index is not None:
            self._pressed_entry = index
            if self._press_timer is not None:
                self._press_timer.stop()
            self._press_timer = self.set_timer(0.2, self._clear_pressed)
            self.refresh()
            identity = self._data.message_indices[index] if self._data.message_indices else None
            token = self._data.message_tokens[index] if self._data.message_tokens else None
            self.post_message(self.PromptSelected(self._data.entries[index][1], identity, token))
            return
        if self._on_toggle is not None:
            self._on_toggle()

    def _scroll_by(self, delta: int) -> bool:
        """展开态滚动正文；返回是否真的动了（没得滚就不用重画）。"""
        if not self._expanded or self._max_scroll <= 0:
            return False
        target = max(0, min(self._scroll + delta, self._max_scroll))
        if target == self._scroll:
            return False
        self._scroll = target
        self._stick_bottom = target >= self._max_scroll
        self.refresh()
        return True

    # 滚轮：展开态滚小窗自己的正文；其余情况就地吃掉——浮层盖住的那几行本来
    # 就传不到托管画面（Textual 没有点击穿透），别让事件跑去滚别的控件。
    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self._scroll_by(_SCROLL_STEP)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        self._scroll_by(-_SCROLL_STEP)
