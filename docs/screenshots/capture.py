#!/usr/bin/env python3
"""用 Textual Pilot 渲染真实 TUI 并导出截图（SVG → PNG），供 README / 改动验收。

不读真实用户历史：夹具会话内容是虚构的。依赖：textual；SVG→PNG 优先 cairosvg
（`pip install cairosvg`），否则回退 ImageMagick `convert`（对 Rich 的 clipPath
文字支持很差，通常会出空白图，不推荐）。

用法（在 cli/ 目录）：

    python3 docs/screenshots/capture.py

macOS Homebrew 已装 cairo 时，cairocffi 仍可能报 ``no library called "cairo-2"``
（它找的名字对不上 ``libcairo.2.dylib``）。先把 brew 的 lib 放进
``DYLD_FALLBACK_LIBRARY_PATH``，再补一个 ``cairo-2`` 软链：

    mkdir -p /tmp/cairo-libs
    ln -sf "$(brew --prefix cairo)/lib/libcairo.2.dylib" /tmp/cairo-libs/libcairo-2.dylib
    export DYLD_FALLBACK_LIBRARY_PATH="/tmp/cairo-libs:$(brew --prefix cairo)/lib"
    python3 docs/screenshots/capture.py

产物写入本目录：list.png（主界面静图）、search.png（全文搜索）、demo.gif（短操作演示）。

**NO_COLOR：** 许多 CI / Agent 环境默认 `NO_COLOR=1`。Textual 会启用 Monochrome
滤镜，整屏真彩变灰阶。本脚本在创建 App 前清除该变量；不要在带着 NO_COLOR 的
壳里另写绕过路径。

真机运行中的 TUI 请用 **F12**（`MainScreen.action_save_screenshot`）导出到
`~/.cache/corral/screenshots/`；勿把含真实对话的截图提交进仓库。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Textual 在 App.__init__ 里若见到 NO_COLOR 会启用 Monochrome；必须在创建
# CorralApp 之前清掉。setdefault 不覆盖调用方已显式设置的真彩 / 语言。
os.environ.pop("NO_COLOR", None)
os.environ.setdefault("COLORTERM", "truecolor")
os.environ.setdefault("CORRAL_LANG", "en")
# 侧边栏记忆（会话组/置顶/显隐）只读临时库，绝不读写机主真实 ~/.cache/corral。
# 库路径认 CORRAL_CACHE_DIR > XDG > ~/.cache，且设了该变量时旧 JSON 迁移也只在
# 这个目录里找，正好顺带隔离。
_CAPTURE_CACHE_DIR = tempfile.mkdtemp(prefix="corral-capture-cache-")
os.environ["CORRAL_CACHE_DIR"] = _CAPTURE_CACHE_DIR
# 截图夹具不得认领本机保活 socket 里的真窗格，否则会把真实会话灌进 README。
os.environ["CORRAL_ISOLATE_MANAGED_HOSTS"] = "1"
# Homebrew cairo is outside uv's Python rpath; without this cairosvg raises OSError.
_homebrew_lib = Path("/opt/homebrew/lib")
if (_homebrew_lib / "libcairo.2.dylib").is_file():
    for _key in ("DYLD_FALLBACK_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        _cur = os.environ.get(_key, "")
        if str(_homebrew_lib) not in _cur.split(":"):
            os.environ[_key] = (
                str(_homebrew_lib) if not _cur else f"{_homebrew_lib}:{_cur}"
            )

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

import corral
from corral.models import ConversationMessage
from corral import session_key
from corral.ui.app import CorralApp


OUT_DIR = Path(__file__).resolve().parent

# 演示用外层终端底色：对齐左栏列表空区实测色 (#1e242b)，避免右栏垫成
# corral-dark $background (#0d1117) 后出现「半边深半边浅」的割裂感。
# osc_report=None 时 EmbedPane 不设 styles.background，空白格会透成纯黑。
_DEMO_BG_HEX = "#1e242b"
_DEMO_OSC_REPORT = b"\x1b]11;rgb:1e1e/2424/2b2b\x07"


# 演示会话的时间：按「刚刚 / 快一小时前 / 大半天前」铺开，让侧边栏时间行的
# 亮度梯度（越新越亮）在截图里能看出来。取相对时间而不是写死时间戳，否则三条
# 会话永远落在最旧那一档，改动验收时看不出差别。
_DEMO_AGES = (90.0, 55 * 60.0, 7 * 3600.0)


def _demo_store():
    import time as _time

    now = _time.time()
    demo = [
        ("claude", "Fix the login race", "Fix the login race", "Serialize cookie updates and add a regression test."),
        ("cursor", "Export reports to CSV", "Export reports to CSV", "Include headers and quote values containing commas."),
        ("codex", "Review login changes", "Review the login changes", "Check cancellation, retries, and cookie expiry."),
    ]
    sessions = []
    conversations = {}
    for index, (runtime, title, request, reply) in enumerate(demo):
        ident = f"demo-{runtime}-1"
        sessions.append({
            "source": runtime, "id": ident, "short_id": ident,
            "mtime": now - _DEMO_AGES[index], "size_bytes": 4096, "size_kb": 4.0,
            "native_title": title, "fallback_title": title,
            "cwd": "/work/webapp", "cwd_display": "/work/webapp", "live": False,
            "path": f"/tmp/{ident}.jsonl", "first_user_msg": request,
            "last_user_msg": "Add a regression test", "last_agent_msg": reply,
        })
        conversations[f"{runtime}:{ident}"] = [
            ConversationMessage("user", request),
            ConversationMessage("assistant", f"## Findings\n\n{reply}\n\n### Next steps\n\n- Make the change.\n- Cover the edge cases.\n- Run the regression tests."),
            ConversationMessage("user", "Add a regression test"),
            ConversationMessage("assistant", "The regression test now reproduces the issue reliably.\n\n```text\nTests: 24 passed\nFailures: 0\n```\n\nThe change is ready for review."),
        ]
    # 截图用稳定「已生成」标题，避免转圈兜底文案进 README
    demo_titles = {
        "claude:demo-claude-1": "Fix the login race",
        "cursor:demo-cursor-1": "Export reports to CSV",
        "codex:demo-codex-1": "Review login changes",
    }

    from unittest import mock
    from corral.attention import AttentionState
    from corral.runtime import RuntimeRegistry

    runtimes = []
    for rid, name in (("claude", "Claude"), ("cursor", "Cursor"), ("codex", "Codex")):
        rt = mock.Mock()
        rt.id = rid
        rt.display_name = name
        rt.is_available.return_value = True
        rt.scan_signature.return_value = None
        own = [s for s in sessions if s["source"] == rid]
        rt.scan_sessions.return_value = own
        rt.load_conversation.side_effect = (
            lambda session, _rid=rid: list(conversations.get(f"{session['source']}:{session['id']}", []))
        )
        runtimes.append(rt)

    registry = RuntimeRegistry(tuple(runtimes))
    # 截图夹具不得读写真实用户的关注状态库；用内存 mock 固定三种演示状态。
    attention_store = mock.Mock()
    attention_store.reconcile.return_value = {}
    with mock.patch.object(corral.titles, "load_cache", return_value={}):
        store = corral.SessionStore(
            limit=20,
            registry=registry,
            attention_store=attention_store,
        )
        store.load()
    # 注意：all_sessions() 自己会拿 store.lock；不可在持锁时再调，否则死锁。
    sessions_now = store.all_sessions()
    with store.lock:
        store.generating.clear()
        for session in sessions_now:
            key = session_key(session)
            store.display_titles[key] = demo_titles[key]
        # 三张卡分别覆盖：黄点等待回答、绿点执行中、红点新结果。
        attention_kinds = {
            "claude:demo-claude-1": "waiting",
            "cursor:demo-cursor-1": "working",
            "codex:demo-codex-1": "unread",
        }
        for session in sessions_now:
            key = session_key(session)
            kind = attention_kinds[key]
            token = f"demo-{kind}-token"
            session["attention_kind"] = kind
            session["attention_token"] = token
            session["attention_updated_at"] = session["mtime"]
            store.attention_states[key] = AttentionState(
                kind=kind,
                activity_token=token,
                updated_at=session["mtime"],
            )
    for session in sessions_now:
        store.get_conversation(session)
    return store


def _terminal_background_hex(svg_text: str) -> str:
    """终端底色：优先演示 OSC / corral-dark，否则用 SVG 里已出现的同系深色。

    Rich 只给「有内容的格子」画背景 rect；空白格透明。去掉假窗口铬后若不再
    垫一层底，cairosvg 会把透明渲成纯黑，右栏大片空洞 vs 左栏石板色——半边黑。
    """
    lowered = svg_text.lower()
    for candidate in (_DEMO_BG_HEX, "#161b22", "#1c2430"):
        if candidate in lowered:
            return candidate
    chrome = re.search(
        r'<rect\b[^>]*\bfill="(#[0-9A-Fa-f]{6})"[^>]*\brx="8"',
        svg_text,
    )
    if chrome:
        return chrome.group(1).lower()
    return _DEMO_BG_HEX


def _strip_window_chrome(svg_text: str) -> str:
    """去掉 Rich SVG 假 macOS 标题栏，只留终端内容区，并垫上不透明终端底色。"""
    bg = _terminal_background_hex(svg_text)
    # 三色点
    svg_text = re.sub(
        r'<g transform="translate\(26,\s*22\)">\s*'
        r"(?:<circle\b[^/]*/>\s*){3}"
        r"</g>\s*",
        "",
        svg_text,
        flags=re.S,
    )
    # 标题「corral」
    svg_text = re.sub(
        r'<text class="[^"]*-title"[^>]*>.*?</text>\s*',
        "",
        svg_text,
        flags=re.S,
    )
    # 圆角外框（其 fill 曾充当整窗底；删掉后必须另垫，见下方）
    svg_text = re.sub(
        r'<rect\b[^>]*\brx="8"\s*/>\s*',
        "",
        svg_text,
        count=1,
    )
    # 内容组移到原点（Rich 默认 translate(terminal_x, terminal_y)≈(9,41)）
    svg_text = re.sub(
        r'(<g transform=")translate\([^"]+\)(" clip-path="url\(#[^"]*-clip-terminal\)">)',
        r"\1translate(0, 0)\2",
        svg_text,
        count=1,
    )
    clip = re.search(
        r'id="[^"]*-clip-terminal"\s*>\s*<rect\b[^>]*?\bwidth="([^"]+)"[^>]*?\bheight="([^"]+)"',
        svg_text,
        flags=re.S,
    )
    if clip is None:
        clip = re.search(
            r'id="[^"]*-clip-terminal"\s*>\s*<rect\b[^>]*?\bheight="([^"]+)"[^>]*?\bwidth="([^"]+)"',
            svg_text,
            flags=re.S,
        )
        if clip is not None:
            height, width = clip.group(1), clip.group(2)
        else:
            width = height = None
    else:
        width, height = clip.group(1), clip.group(2)
    if width is not None and height is not None:
        svg_text = re.sub(
            r'viewBox="0 0 [^"]+"',
            f'viewBox="0 0 {width} {height}"',
            svg_text,
            count=1,
        )
        # 垫在内容组之前：空白格不再透出纯黑
        backdrop = (
            f'<rect fill="{bg}" x="0" y="0" width="{width}" height="{height}" '
            f'shape-rendering="crispEdges"/>\n'
        )
        svg_text = re.sub(
            r'(<g transform="translate\(0, 0\)" clip-path="url\(#[^"]*-clip-terminal\)">)',
            backdrop + r"\1",
            svg_text,
            count=1,
        )
    return svg_text


# Bold Menlo under cairo lacks box-drawing glyphs → tofu for │├└.
# Prefer a mono face that still draws those when font-weight is bold.
_SCREENSHOT_FONT_CANDIDATES = (
    "Hack Nerd Font Mono",
    "Hack Nerd Font",
    "Hack",
    "DejaVu Sans Mono",
    "Menlo",
)

# Fruit group emoji → Twemoji 14 stem (vendored under emoji/).
# Cairo cannot paint Apple Color Emoji; embed PNGs instead. session_list
# already puts each fruit glyph in its own Rich span for this swap.
_FRUIT_EMOJI_TWEMOJI = {
    0x1F34E: "1f34e",  # 🍎
    0x1F951: "1f951",  # 🥑
    0x1F34C: "1f34c",  # 🍌
    0x1FAD0: "1fad0",  # 🫐
    0x1F352: "1f352",  # 🍒
    0x1F965: "1f965",  # 🥥
    0x1F347: "1f347",  # 🍇
    0x1F95D: "1f95d",  # 🥝
    0x1F34B: "1f34b",  # 🍋
    0x1F96D: "1f96d",  # 🥭
    0x1F348: "1f348",  # 🍈
    0x1F34A: "1f34a",  # 🍊
    0x1F351: "1f351",  # 🍑
    0x1F350: "1f350",  # 🍐
    0x1F34D: "1f34d",  # 🍍
    0x1F353: "1f353",  # 🍓
    0x1F349: "1f349",  # 🍉
}


def _screenshot_font() -> str:
    """Pick an installed mono font that still draws │├└ when bold."""
    try:
        out = subprocess.run(
            ["fc-list", ":family", "family"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout
    except OSError:
        out = ""
    families = {line.strip() for line in out.splitlines() if line.strip()}
    for name in _SCREENSHOT_FONT_CANDIDATES:
        if name in families:
            return name
    return _SCREENSHOT_FONT_CANDIDATES[-1]


def _embed_fruit_emoji(svg_text: str) -> str:
    """Replace lone fruit-emoji <text> nodes with vendored Twemoji PNGs."""
    import base64
    import html as html_module

    emoji_dir = OUT_DIR / "emoji"
    cache: dict[str, str] = {}

    def data_uri(stem: str) -> str:
        if stem in cache:
            return cache[stem]
        path = emoji_dir / f"{stem}.png"
        if not path.is_file():
            raise RuntimeError(
                f"missing Twemoji asset {path}; restore docs/screenshots/emoji/"
            )
        uri = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
        cache[stem] = uri
        return uri

    def repl(match: re.Match[str]) -> str:
        attrs, inner = match.group(1), match.group(2)
        plain = html_module.unescape(inner)
        if len(plain) != 1:
            return match.group(0)
        stem = _FRUIT_EMOJI_TWEMOJI.get(ord(plain))
        if stem is None:
            return match.group(0)
        uri = data_uri(stem)
        xm = re.search(r'\bx="([^"]+)"', attrs)
        ym = re.search(r'\by="([^"]+)"', attrs)
        x = float(xm.group(1)) if xm else 0.0
        y = float(ym.group(1)) if ym else 0.0
        size = 18.0
        return (
            f'<image x="{x}" y="{y - size + 2:.1f}" width="{size}" height="{size}" '
            f'href="{uri}"/>'
        )

    return re.sub(r"<text([^>]*)>([^<]*)</text>", repl, svg_text)


def _assert_no_fruit_text_nodes(svg_text: str) -> None:
    """Fail capture if a fruit glyph is still a <text> node (would render as tofu)."""
    import html as html_module

    for match in re.finditer(r"<text[^>]*>([^<]*)</text>", svg_text):
        plain = html_module.unescape(match.group(1))
        if len(plain) == 1 and ord(plain) in _FRUIT_EMOJI_TWEMOJI:
            raise RuntimeError(
                f"fruit emoji U+{ord(plain):04X} still in <text>; Twemoji embed failed"
            )


def _prepare_svg(svg_text: str) -> str:
    """Strip fake window chrome, use a bold-safe mono font, embed fruit emoji."""
    svg_text = _strip_window_chrome(svg_text)
    svg_text = re.sub(r"@font-face\s*\{.*?\}", "", svg_text, flags=re.S)
    font = _screenshot_font()
    svg_text = svg_text.replace("Fira Code", font)
    svg_text = _embed_fruit_emoji(svg_text)
    _assert_no_fruit_text_nodes(svg_text)
    return svg_text


def _svg_to_png(svg_path: Path, png_path: Path) -> None:
    prepared = _prepare_svg(svg_path.read_text(encoding="utf-8"))
    prepared_path = svg_path.with_suffix(".prepared.svg")
    prepared_path.write_text(prepared, encoding="utf-8")
    renderer = os.environ.get("CORRAL_SCREENSHOT_RENDERER")
    if renderer:
        subprocess.run(["node", renderer, str(prepared_path), str(png_path)], check=True)
        return
    from cairosvg import svg2png
    svg2png(bytestring=prepared.encode(), write_to=str(png_path))


def _save_frame(app, png_path: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        svg = app.save_screenshot(png_path.name.replace(".png", ".svg"), path=td)
        _svg_to_png(Path(td) / Path(svg).name, png_path)
        _assert_png_sane(png_path)


def _write_gif(frame_paths: list[Path], dest: Path, durations_ms: list[int]) -> None:
    from PIL import Image

    if len(frame_paths) != len(durations_ms):
        raise ValueError("each GIF frame needs a duration")
    opened = [Image.open(path).convert("RGBA") for path in frame_paths]
    width = min(960, opened[0].width)
    if opened[0].width != width:
        ratio = width / opened[0].width
        opened = [
            frame.resize(
                (width, max(1, int(frame.height * ratio))),
                Image.Resampling.LANCZOS,
            )
            for frame in opened
        ]
    quantized = [
        frame.convert("P", palette=Image.ADAPTIVE, colors=256) for frame in opened
    ]
    quantized[0].save(
        dest,
        save_all=True,
        append_images=quantized[1:],
        duration=durations_ms,
        loop=0,
        optimize=True,
        disposal=2,
    )
    if dest.stat().st_size > 2_000_000:
        raise RuntimeError(f"demo GIF is {dest.stat().st_size} bytes; shorten the clip")


async def _capture() -> None:
    """Capture the real terminal UI with isolated English demo conversations."""
    from corral import split_layout

    store = _demo_store()
    with tempfile.TemporaryDirectory() as layout_td:
        with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": layout_td}, clear=False):
            split_layout.reset_default_layout_db()
            try:
                app = CorralApp(store, embed_ok=True, osc_report=_DEMO_OSC_REPORT)
                async with app.run_test(size=(120, 32)) as pilot:
                    await pilot.pause(delay=0.6)
                    await _collapse_hud(pilot)
                    _save_frame(app, OUT_DIR / "list.png")
                    print(f"wrote {OUT_DIR / 'list.png'}")
            finally:
                split_layout.reset_default_layout_db()


def _assert_png_sane(png_path: Path) -> None:
    """出图后自检：拒绝整栏纯黑空洞 / 灰阶（上次 README 翻车点）。"""
    try:
        from PIL import Image
    except ImportError:
        print("warning: Pillow 不可用，跳过 PNG 自检", file=sys.stderr)
        return
    im = Image.open(png_path).convert("RGB")
    w, h = im.size
    right_black = 0
    right_total = 0
    chromatic = 0
    for y in range(0, h, 4):
        for x in range(w // 3, w, 4):
            r, g, b = im.getpixel((x, y))
            right_total += 1
            if r == 0 and g == 0 and b == 0:
                right_black += 1
            if abs(r - g) > 20 or abs(g - b) > 20:
                chromatic += 1
    if right_total and right_black / right_total > 0.05:
        raise RuntimeError(
            f"截图右栏纯黑占比 {right_black}/{right_total}="
            f"{100 * right_black / right_total:.0f}%（空白格未垫底），拒绝写入"
        )
    if chromatic < 10:
        raise RuntimeError(
            f"截图几乎无彩色像素（chromatic={chromatic}），可能仍被 NO_COLOR 灰阶化"
        )


async def _capture_search() -> None:
    """全文搜索弹窗（Ctrl+F）：命中行 + 关键词高亮。"""
    from corral.ui.search_modal import FullTextSearchModal

    from corral import split_layout

    store = _demo_store()
    with tempfile.TemporaryDirectory() as layout_td:
        with mock.patch.dict(
            os.environ, {"CORRAL_CACHE_DIR": str(layout_td)}, clear=False,
        ):
            split_layout.reset_default_layout_db()
            try:
                app = CorralApp(store, embed_ok=True, osc_report=_DEMO_OSC_REPORT)
                async with app.run_test(size=(140, 36)) as pilot:
                    await pilot.pause(delay=0.4)
                    await _collapse_hud(pilot)
                    await pilot.press("ctrl+f")
                    await _wait_search_ready(app)
                    modal = app.screen
                    modal.query_one("#search-query").load_text("regression")
                    await pilot.pause(delay=0.5)
                    if not isinstance(modal, FullTextSearchModal) or not modal._matches:
                        raise RuntimeError("演示查询没有命中，截图会是空列表")
                    _save_frame(app, OUT_DIR / "search.png")
                    print(f"wrote {OUT_DIR / 'search.png'}")
            finally:
                split_layout.reset_default_layout_db()


async def _capture_new_session() -> None:
    """新建会话弹窗：项目筛选、Quick Start 与「更多…」任意文件夹入口。"""
    from corral import recent_starts, split_layout
    from corral.ui.modals import NewSessionModal, RuntimeChoice

    store = _demo_store()
    sessions = store.all_sessions()
    projects = []
    for session in sessions:
        cwd = session.get("cwd") or ""
        if cwd and all(cwd != item[0] for item in projects):
            projects.append((cwd, cwd.rstrip("/").split("/")[-1], cwd))
    choices = [
        RuntimeChoice(rid, name, "", True)
        for rid, name in (("claude", "Claude"), ("codex", "Codex"), ("opencode", "OpenCode"),
                          ("cursor", "Cursor"), ("pi", "Pi"))
    ]
    with tempfile.TemporaryDirectory() as layout_td:
        with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": layout_td}, clear=False):
            split_layout.reset_default_layout_db()
            try:
                app = CorralApp(store, embed_ok=False, osc_report=_DEMO_OSC_REPORT)
                async with app.run_test(size=(120, 30)) as pilot:
                    await pilot.pause(delay=0.5)
                    combos = [
                        recent_starts.RecentStart("claude", projects[0][0], projects[0][1]),
                        recent_starts.RecentStart("codex", projects[0][0], projects[0][1]),
                    ] if projects else []
                    modal = NewSessionModal(projects, choices, recent_combos=combos)
                    await app.push_screen(modal, lambda *_: None)
                    await pilot.pause(delay=0.5)
                    _save_frame(app, OUT_DIR / "new-session.png")
                    print(f"wrote {OUT_DIR / 'new-session.png'}")
            finally:
                split_layout.reset_default_layout_db()


async def _collapse_hud(pilot) -> None:
    await pilot.press("ctrl+g")
    await pilot.pause(delay=0.2)


async def _wait_search_ready(app, *, tries: int = 100) -> None:
    from corral.ui.search_modal import FullTextSearchModal

    for _ in range(tries):
        if isinstance(app.screen, FullTextSearchModal) and not app.screen._indexing:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError("全文搜索弹窗没有就绪")


async def _capture_demo_gif() -> None:
    """Short sanitized walkthrough: switch sessions, search, then split."""
    from corral import split_layout
    from corral.ui.search_modal import FullTextSearchModal

    store = _demo_store()
    with tempfile.TemporaryDirectory() as layout_td:
        with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": layout_td}, clear=False):
            split_layout.reset_default_layout_db()
            try:
                app = CorralApp(store, embed_ok=True, osc_report=_DEMO_OSC_REPORT)
                async with app.run_test(size=(120, 32)) as pilot:
                    await pilot.pause(delay=0.6)
                    await _collapse_hud(pilot)
                    with tempfile.TemporaryDirectory() as td:
                        frame_dir = Path(td)
                        list_frame = frame_dir / "01-list.png"
                        switch_frame = frame_dir / "02-switch.png"
                        search_frame = frame_dir / "03-search.png"
                        split_frame = frame_dir / "04-split.png"
                        _save_frame(app, list_frame)
                        await pilot.press("down")
                        await pilot.pause(delay=0.4)
                        _save_frame(app, switch_frame)
                        await pilot.press("ctrl+f")
                        await _wait_search_ready(app)
                        app.screen.query_one("#search-query").load_text("regression")
                        await pilot.pause(delay=0.5)
                        if (
                            not isinstance(app.screen, FullTextSearchModal)
                            or not app.screen._matches
                        ):
                            raise RuntimeError("演示查询没有命中，动图会是空列表")
                        _save_frame(app, search_frame)
                        await pilot.press("escape")
                        await pilot.pause(delay=0.3)
                        keys = [session_key(session) for session in store.all_sessions()[:2]]
                        app.screen._open_split_from_selection(keys)
                        await pilot.pause(delay=0.6)
                        _save_frame(app, split_frame)
                        gif_path = OUT_DIR / "demo.gif"
                        _write_gif(
                            [list_frame, switch_frame, search_frame, split_frame],
                            gif_path,
                            [2000, 2200, 2800, 3200],
                        )
                        print(f"wrote {gif_path}")
            finally:
                split_layout.reset_default_layout_db()


def main() -> None:
    asyncio.run(_capture())
    asyncio.run(_capture_search())
    asyncio.run(_capture_new_session())
    asyncio.run(_capture_demo_gif())


if __name__ == "__main__":
    main()
