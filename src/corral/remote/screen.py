"""终端画面的线上表示：把 tmux 抓到的一屏编码成手机可以直接绘制的行。

**为什么不在手机上跑终端模拟器**：corral 从一开始就是「tmux 当终端模拟器，
自己只把画面解析成字符网格」。这条线延伸到手机上最省事——电脑侧把已经解析好的
行发过去，手机只负责画格子。反过来，把抓到的整屏喂给 SwiftTerm / libghostty 之类的
模拟器只能靠「清屏 + 全量重绘」，既费电又丢滚动历史，得不偿失。Mac 终端视图另走
`terminal_stream.py`：喂的是程序写出的原始字节流，不是抓屏。

另一个被动的好处：因为走的是画面抓取而不是接入 tmux 当客户端，**手机永远不会
改变会话窗口的尺寸**，电脑端正在看的同一个会话不会被手机挤窄。

编码约定（尽量少的 JSON 体积）：

- 颜色是一个整数：``-1`` 表示终端默认色，``0..255`` 是 256 色索引，
  其余取 ``0x1000000 | r<<16 | g<<8 | b`` 表示真彩色。
- 属性是位掩码：1 粗体、2 变暗、4 下划线、8 反显。
- 一行 = ``[行号, 文本, [起始, 结束, 前景, 背景, 属性] ...]``，
  起止下标按文本字符计数（宽字符只占一个下标），与桌面端渲染同一套口径。
- 只有内容变化的行才会出现在增量帧里；``full`` 为真时是整屏重置。
"""

from __future__ import annotations

from dataclasses import dataclass

ATTR_BOLD = 1
ATTR_DIM = 2
ATTR_UNDERLINE = 4
ATTR_REVERSE = 8

_TRUECOLOR_FLAG = 0x1000000


def encode_colour(value: int | tuple[int, int, int]) -> int:
    if isinstance(value, tuple):
        r, g, b = (max(0, min(255, int(c))) for c in value)
        return _TRUECOLOR_FLAG | (r << 16) | (g << 8) | b
    return int(value)


def _attrs(cell) -> int:
    mask = 0
    if cell.bold:
        mask |= ATTR_BOLD
    if cell.dim:
        mask |= ATTR_DIM
    if cell.underline:
        mask |= ATTR_UNDERLINE
    if cell.reverse:
        mask |= ATTR_REVERSE
    return mask


def encode_row(cells) -> tuple[str, list[list[int]]]:
    """把一行单元格编码成 (文本, 样式段列表)。

    与桌面端 `embed.row_text_and_spans` 保持同一套语义：跳过宽字符的占位格，
    相邻同样式合并成一段，下标按文本字符计数。这里不能直接复用那个函数——它
    产出的是 Rich 的 Style 对象，反解回原始色值既绕又会丢真彩色精度。
    """
    chars: list[str] = []
    spans: list[list[int]] = []
    run_start = 0
    run_key: tuple[int, int, int] | None = None
    for cell in cells:
        if cell.wide_cont:
            continue
        key = (encode_colour(cell.fg), encode_colour(cell.bg), _attrs(cell))
        if run_key is None:
            run_key = key
        elif key != run_key:
            if run_key != (-1, -1, 0):
                spans.append([run_start, len(chars), *run_key])
            run_start = len(chars)
            run_key = key
        chars.append(cell.ch or " ")
    if run_key is not None and run_key != (-1, -1, 0) and len(chars) > run_start:
        spans.append([run_start, len(chars), *run_key])
    return "".join(chars), spans


@dataclass(frozen=True)
class ScreenFrame:
    """一帧的完整描述，直接 JSON 序列化后发给手机。"""

    cols: int
    rows: int
    full: bool
    lines: list[list]
    cursor_x: int
    cursor_y: int
    cursor_visible: bool
    history_size: int
    history_offset: int
    status: str

    def to_dict(self) -> dict:
        return {
            "cols": self.cols,
            "rows": self.rows,
            "full": self.full,
            "lines": self.lines,
            "cursor": [self.cursor_x, self.cursor_y, 1 if self.cursor_visible else 0],
            "history": [self.history_size, self.history_offset],
            "status": self.status,
        }


def status_line(grid) -> str:
    """取画面上最后一行有内容的文本，当作「助手正在干什么」的实时状态。

    这是原生聊天流拿到实时反馈的唯一可靠来源：各助手的历史文件是按消息落盘的，
    一个任务跑五分钟中途一条新消息都不会写，光看历史文件聊天流会像死掉一样。
    助手自己会在画面底部打「Esc to interrupt · 47s」这类进度，取过来即可。
    """
    for cells in reversed(grid):
        # 跳过宽字符的占位格，与 encode_row 同一口径；用空格顶替会让中文被拆成
        # 「来 自 手 机」，贴到聊天流底部时非常刺眼。
        text = "".join(c.ch or " " for c in cells if not c.wide_cont).strip()
        if text:
            return text[:200]
    return ""


class ScreenEncoder:
    """按会话维护上一帧的行指纹，只把变化的行发出去。

    尺寸变化、滚动位置变化都会强制发一整屏——行号在这两种情况下不再对得上，
    继续做增量会让手机端画面错位。
    """

    def __init__(self) -> None:
        self._fingerprints: list[int] = []
        self._cols = 0
        self._rows = 0
        self._offset = -1
        self._cursor = (0, 0, False)
        self._history_size = -1

    def reset(self) -> None:
        self._fingerprints = []
        self._cols = 0
        self._rows = 0
        self._offset = -1
        self._cursor = (0, 0, False)
        self._history_size = -1

    def encode(
        self,
        grid,
        *,
        cursor: tuple[int, int, bool] = (0, 0, False),
        history_size: int = 0,
        history_offset: int = 0,
    ) -> ScreenFrame | None:
        """编码一帧；与上一帧完全一致时返回 None，调用方不必发送。"""
        rows = len(grid)
        cols = len(grid[0]) if rows else 0
        full = (
            cols != self._cols
            or rows != self._rows
            or history_offset != self._offset
            or len(self._fingerprints) != rows
        )
        metadata_changed = cursor != self._cursor or history_size != self._history_size
        lines: list[list] = []
        fingerprints: list[int] = []
        for index, cells in enumerate(grid):
            text, spans = encode_row(cells)
            fingerprint = hash((text, tuple(tuple(s) for s in spans)))
            fingerprints.append(fingerprint)
            if full or self._fingerprints[index] != fingerprint:
                lines.append([index, text, spans])
        self._fingerprints = fingerprints
        self._cols = cols
        self._rows = rows
        self._offset = history_offset
        self._cursor = cursor
        self._history_size = history_size
        if not full and not lines and not metadata_changed:
            return None
        return ScreenFrame(
            cols=cols,
            rows=rows,
            full=full,
            lines=lines,
            cursor_x=cursor[0],
            cursor_y=cursor[1],
            cursor_visible=cursor[2],
            history_size=history_size,
            history_offset=history_offset,
            status=status_line(grid),
        )
