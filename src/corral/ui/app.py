"""CorralApp：Textual 应用外壳，唯一界面入口。

`run_app()` 是唯一对外函数，返回值语义：
`LaunchRequest | NewSessionRequest` 表示需要外层 execvp 全屏接管，
`None` 表示用户只是退出。
"""

from __future__ import annotations

import asyncio
import atexit
import os
import sys

from textual.app import App, ScreenError
from textual.theme import Theme
from textual.timer import Timer

from corral.ui.main_screen import MainScreen
from corral.ui.pointer_shape import sequence as pointer_shape_sequence
from corral.ui.terminal_theme import (
    BACKGROUND_POLL_INTERVAL,
    TerminalBackgroundReport,
    enhance_driver,
    query_runtime_theme,
    supports_runtime_theme_tracking,
)
from corral.ui.textual_patches import install_textual_patches

# Textual 上游缺口（LRUCache 驱逐 KeyError 等）在进界面前打上，避免长跑闪退。
install_textual_patches()

# 窗口拖动时 SIGWINCH 会连续触发。布局跟随 Textual 自带的 ~120fps 合并即可；
# 整屏全量重绘（用来清掉终端 reflow 残影）必须另行防抖，等尺寸停稳再做一次，
# 否则拖动过程中会疯狂全量刷新、卡顿闪烁。
_RESIZE_FULL_REPAINT_DEBOUNCE = 0.12  # 秒；最后一次尺寸变化后再等这么久才整屏重绘

# Client-green action accents sit on neutral reading surfaces; selected fills use a subtle tint.
_CORRAL_DARK = Theme(
    name="corral-dark",
    primary="#96D5B3",
    secondary="#98A4A0",
    accent="#96D5B3",
    foreground="#D7DCDB",
    background="#171A1B",
    surface="#202426",
    panel="#292E30",
    warning="#D4A017",
    error="#E5534B",
    success="#3F9A6A",
    dark=True,
    variables={
        # Standalone and grouped selections share the same quiet green fill.
        "block-cursor-background": "#353E39",
        "block-cursor-blurred-background": "#353E39CC",
        "pane-active-background": "#343B38",
        # 侧边栏投影分屏组合的四级底色（见 _SIDEBAR_SPLIT_LADDER 的说明）
        "sidebar-split-background": "#2B302E",
        "sidebar-split-cursor-background": "#353E39",
        "sidebar-split-active-background": "#3E4943",
        "sidebar-split-active-cursor-background": "#48544C",
    },
)
_CORRAL_LIGHT = Theme(
    name="corral-light",
    primary="#2E7D5B",
    secondary="#64736B",
    accent="#2E7D5B",
    foreground="#252A2B",
    background="#F7F8F8",
    surface="#EFF1F1",
    panel="#E5E8E8",
    warning="#B8860B",
    error="#CF222E",
    success="#1A7F4B",
    dark=False,
    variables={
        # 与深色同理：选中 = 组内选中档（sidebar-split-cursor），勿再淡一档。
        "block-cursor-background": "#DEE5E0",
        "block-cursor-blurred-background": "#DEE5E0CC",
        "pane-active-background": "#E7ECE8",
        # 浅色下"更显著"是更深更饱和，梯度方向与深色相反，见 _SIDEBAR_SPLIT_LADDER
        "sidebar-split-background": "#EDF0EE",
        "sidebar-split-cursor-background": "#DEE5E0",
        "sidebar-split-active-background": "#D0DBD3",
        "sidebar-split-active-cursor-background": "#C3D0C7",
    },
)


# corral 自有主题变量的兜底值。**凡是在 widget 的 DEFAULT_CSS 里用到、而 Textual
# 内置主题没有的变量，都必须同时登记在这里**，不能只写在上面两个 Theme 里。
# 原因：widget 的 DEFAULT_CSS 是在该类第一次挂载时并入应用样式表并做变量代换的，
# 那一刻当前主题不一定已经切到 corral 自有主题（`on_mount` 里注册与切换，和首屏
# 各控件的挂载时机不是同一件事，且随 Textual 版本、终端探测结果、直启/普通启动
# 路径而变）。主题里缺一个变量的后果不是"颜色不对"，而是**整个应用起不来**：
# Textual 直接报 `reference to undefined variable` 并中止（2026-07-31 真机事故：
# 0.24.29 在 macOS Homebrew 安装下一启动就报 `$pane-inactive-background` 未定义）。
# 这里给的是深色兜底值，具体主题里的同名变量会覆盖它。
_THEME_VARIABLE_DEFAULTS = {
    "pane-active-background": "#343B38",
    "sidebar-split-background": "#2B302E",
    "sidebar-split-cursor-background": "#353E39",
    "sidebar-split-active-background": "#3E4943",
    "sidebar-split-active-cursor-background": "#48544C",
}

# 侧边栏投影右栏分屏组合时的四级底色阶梯，从弱到强：
#   组合内 → 组合内且键盘光标停在它上面 → 当前激活格 → 激活格且光标停在它上面
# 光标停在组卡上时，整组（组卡+成员）走第二档（`-group-selected`），激活成员走第四档。
# 第二档必须与主题 `block-cursor-background` 同色：独立会话选中走 ListView 的
# `.-highlight`（block-cursor），组内成员选中走 `-in-split.-highlight` /
# `-group-selected`（split-cursor）；两色不一致时侧栏选中会「组内深、组外淡」。
# 必须整体单调（深色越来越亮、浅色越来越深），否则会出现「光标移到激活行上，
# 整行反而变暗」的倒挂——列表自身的选中底色比激活格底色弱，光标一旦落到组合
# 行上就会把它压回去，看着像状态丢了。四级都用主题变量而不是写死 hex。
_SIDEBAR_SPLIT_LADDER = (
    "sidebar-split-background",
    "sidebar-split-cursor-background",
    "sidebar-split-active-background",
    "sidebar-split-active-cursor-background",
)


class CorralApp(App):
    """corral 的主应用；具体界面全部在 MainScreen 里，这里只挂屏幕。"""

    TITLE = "corral"
    # 关掉 Textual 自带的 Ctrl+P 命令面板：底部会画出 `^p palette`，而且占掉
    # 这个键。corral 用 Ctrl+P 做全局置顶（与 Ctrl+F 全文搜索同级）。
    ENABLE_COMMAND_PALETTE = False
    # 不整体关闭 Textual 内置的鼠标拖拽文本选择：EmbedPane 需要它来实现"划词
    # 选中托管会话画面里的文字，抬起后自动 OSC 52 复制"。真机实测过的崩溃出在
    # 会被后台重扫动态增删的列表项上（选择过程中控件被移除，container 变 None
    # 后访问 .region 崩溃）——子卡片、外层 NoSelectListItem、SessionListView
    # 和弹窗列表项都要关 ALLOW_SELECT；只留 EmbedPane 开着用于划词复制。
    CSS = """
    /* 机主裁定：TUI 里不出现 I 型指针；Textual 自带输入控件默认 pointer: text。 */
    Input, TextArea { pointer: default; }
    #project-search-row {
        height: 1; width: 1fr; margin: 0; padding: 0;
        background: transparent;
    }
    #project-search-row > Static, #project-search {
        height: 1; margin: 0; border: none; padding: 0;
        color: $text-muted; background: $panel;
    }
    #project-search { width: 1fr; }
    #filter-prefix { width: 2; }
    #project-search-row #filter-count { width: auto; padding: 0 1; }
    #project-search-row > .filter-cap { width: 1; color: $panel; background: transparent; }
    #project-search-row:focus-within > Static,
    #project-search-row:focus-within #project-search {
        background: $primary-muted; color: $foreground;
    }
    #project-search-row.-active > Static,
    #project-search-row.-active #project-search,
    #project-search-row.-active #project-search:focus {
        background: $primary-muted; color: $warning;
    }
    #project-search-row:focus-within > .filter-cap,
    #project-search-row.-active > .filter-cap {
        color: $primary-muted; background: transparent;
    }
    #project-search-row #filter-count { color: $text-muted; }
    #project-search-row.-active #project-search-clear { background: $primary-muted; color: $warning; }
    #project-search-row.-active #project-search-clear:hover { color: $error; }
    /* 新版本浮层的定位锚：UpdateToast 是内容宽的 overlay 盒子，靠父级右下对齐
       钉到右下角。注意这条不能写进 update_toast.py 的 DEFAULT_CSS——控件的
       DEFAULT_CSS 作用不到别的控件（MainScreen 规则会被静默丢弃，真机回归过）。
       对其它子控件是 no-op：列表区/龙动画/页脚都是填满的，只有内容宽的浮层
       会被 align 挪动。 */
    MainScreen {
        align: right bottom;
    }
    """

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """corral 自有主题变量的兜底值，见 `_THEME_VARIABLE_DEFAULTS` 的说明。"""
        return dict(_THEME_VARIABLE_DEFAULTS)

    def get_driver_class(self):
        """默认 Unix 驱动增加终端主题应答解析，其他驱动保持 Textual 原行为。"""
        return enhance_driver(super().get_driver_class())

    def _set_pointer_shape(self, shape: str) -> None:
        """覆写 Textual 原实现：同样的 OSC 22，但套 tmux 时带 DCS 穿透。"""
        if self._driver is not None:
            self._driver.write(pointer_shape_sequence(shape))

    def __init__(self, store, embed_ok: bool, direct=None, osc_report: bytes | None = None) -> None:
        super().__init__()
        self._store = store
        self._embed_ok = embed_ok
        self._direct = direct
        self._osc_report = osc_report
        self._resize_full_repaint_timer: Timer | None = None
        # Textual compositor 在窗口高度骤变时偶发 chops/spans 行数不一致（IndexError）；
        # 连续恢复有上限，避免真故障时死循环刷屏。
        self._compositor_recovery_budget = 0
        self._stall_watchdog = None

    def _on_terminal_supports_synchronized_output(self, message) -> None:
        # Textual 默认在终端支持时把每帧包进同步输出（`\e[?2026h…l`，原子提交整帧防撕裂）。
        # 排查"iTerm2 + SSH 下输入法候选词小窗跑到左上角"时怀疑过这层：SSH 有网络
        # 延迟/分片，合成期间 iTerm2 有概率读到同步块里尚未提交的旧光标位置（初始
        # 左上角）。设 CORRAL_DISABLE_SYNC_OUTPUT=1 可保持 _sync_available=False、
        # 不再包同步块，用于验证/规避该现象（代价是大面积重绘可能有轻微闪烁）。
        from corral.legacy_names import getenv

        if getenv("DISABLE_SYNC_OUTPUT") == "1":
            return
        super()._on_terminal_supports_synchronized_output(message)

    def _check_resize(self) -> None:
        """布局立即跟随；整屏清残影防抖后再做。

        Textual 默认只差分刷新布局变化的区域。iTerm2 等终端会在应用重绘前自行
        reflow 备用屏幕，差分重绘会留下残影/错位。必须整屏全量重绘才能清干净，
        但不能在拖动过程中每次尺寸变化都全量刷——这里只重置防抖计时器，等
        `_RESIZE_FULL_REPAINT_DEBOUNCE` 内不再有新尺寸后再 `_force_full_repaint`。
        """
        super()._check_resize()
        # 每次尺寸变化给一次恢复额度：缩放过程中 compositor IndexError 可自愈。
        self._compositor_recovery_budget = 2
        if self._resize_full_repaint_timer is not None:
            self._resize_full_repaint_timer.stop()
        self._resize_full_repaint_timer = self.set_timer(
            _RESIZE_FULL_REPAINT_DEBOUNCE,
            self._force_full_repaint,
        )

    def _force_full_repaint(self) -> None:
        """尺寸停稳后：把当前屏整屏标脏，触发一次 compositor 全量重绘。"""
        self._resize_full_repaint_timer = None
        try:
            screens = [self.screen, *self._background_screens]
        except ScreenError:
            return
        for screen in screens:
            compositor = getattr(screen, "_compositor", None)
            if compositor is None:
                continue
            region = screen.size.region
            if region:
                compositor._dirty_regions.add(region)
            screen.refresh()

    def _handle_exception(self, error: Exception) -> None:
        """缩放期 compositor 行数竞态：整屏重绘一次自愈，不退出整个界面。

        Textual 默认 `_handle_exception` 一律退出。真机在拖动窗口时偶发
        `ChopsUpdate` 的 `chops[y]` IndexError（spans 仍引用旧高度）。
        自愈失败或其它未捕获异常：落盘后再走默认退出，供事后 diagnose。
        """
        if isinstance(error, IndexError) and self._compositor_recovery_budget > 0:
            self._compositor_recovery_budget -= 1
            try:
                self._force_full_repaint()
                return
            except Exception:
                pass
        try:
            from corral.observe import log_exception

            log_exception("TUI 未捕获异常", error)
        except Exception:
            pass
        super()._handle_exception(error)

    def on_mount(self) -> None:
        # corral 自己的界面色也要跟随外层终端的深浅色，不能只管注入托管 pane
        # 那一份——两者是分开的：这里管 corral 自身列表/页头/弹窗的配色，
        # embed_pane.py 的 report_theme 管托管会话自己的深浅色检测。此前完全
        # 没接这一段，Textual 默认主题在浅色终端下会显得配色不对（真机实测发现）。
        # 深/浅都用 corral 自有「冷静工作台」主题，不用 Textual 默认高饱和主色。
        import corral
        from corral.observe import TuiStallWatchdog

        self._stall_watchdog = TuiStallWatchdog(asyncio.get_running_loop())
        self._stall_watchdog.start()

        self.register_theme(_CORRAL_DARK)
        self.register_theme(_CORRAL_LIGHT)
        is_light = corral._background_is_light(self._osc_report)
        self.theme = "corral-light" if is_light is True else "corral-dark"
        self.push_screen(MainScreen(self._store, self._embed_ok, self._direct, self._osc_report))
        if supports_runtime_theme_tracking(self._driver):
            self.set_interval(BACKGROUND_POLL_INTERVAL, self._query_runtime_theme)

    def on_unmount(self) -> None:
        if self._stall_watchdog is not None:
            self._stall_watchdog.stop()
            self._stall_watchdog = None

    def _query_runtime_theme(self) -> None:
        query_runtime_theme(self._driver)

    def on_terminal_background_report(self, message: TerminalBackgroundReport) -> None:
        """终端运行中换配色时，同步 corral 壳层、当前面板和后续新会话。"""
        import corral

        is_light = message.is_light
        if message.osc_report is not None:
            self._osc_report = corral.theme._replace_background_report(
                self._osc_report,
                message.osc_report,
            )
            is_light = corral._background_is_light(message.osc_report)
            try:
                screen = self.screen
            except ScreenError:
                screen = None
            if isinstance(screen, MainScreen):
                screen.update_terminal_background(self._osc_report)
        if is_light is not None:
            target = "corral-light" if is_light else "corral-dark"
            if self.theme != target:
                self.theme = target


# 强停时直接写给终端的关闭序列。必须同步 os.write，不能走 Textual 写线程：
# Ctrl+C 常打断在 asyncio 等后台线程收尾（Python 3.14 默认可等 300 秒），那时
# 写线程可能已经停了，正常退出路径发不出「关掉鼠标跟踪 / 离开备用屏」。
# 序列对齐 Textual linux_driver.stop_application_mode 与 Gemini CLI 的
# TERMINAL_CLEANUP_SEQUENCE：鼠标 1000/1002/1003/1006/1015、焦点 1004、
# 粘贴 2004、Kitty 键盘弹出、同步输出 2026、带内尺寸 2048、备用屏 1049、
# 显示光标、恢复自动换行。
_TERMINAL_RESTORE = (
    b"\x1b[<u"
    b"\x1b[?2004l"
    b"\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l"
    b"\x1b[?1004l"
    b"\x1b[?2026l"
    b"\x1b[?2048l"
    b"\x1b[?1049l"
    b"\x1b[?25h"
    b"\x1b[?7h"
)

_atexit_restore_registered = False


def restore_terminal() -> None:
    """强停或进程退出时同步恢复终端模式，不经过 Textual 写线程。

    正常 Esc 退出仍由 Textual 自己收尾；本函数只保证强停路径不会把鼠标跟踪
    留给外壳——否则一点击就会打出 ``^[[<0;21;2M`` 这类 SGR 鼠标码。
    """
    written = False
    for stream in (sys.__stdout__, sys.stdout, sys.__stderr__):
        try:
            fd = stream.fileno()
        except Exception:
            continue
        try:
            if not os.isatty(fd):
                continue
            os.write(fd, _TERMINAL_RESTORE)
            written = True
            break
        except OSError:
            continue
    if not written:
        try:
            fd = os.open("/dev/tty", os.O_WRONLY)
        except OSError:
            fd = -1
        else:
            try:
                os.write(fd, _TERMINAL_RESTORE)
            except OSError:
                pass
            finally:
                os.close(fd)
    try:
        import termios

        infd = sys.__stdin__.fileno()
        if os.isatty(infd):
            termios.tcflush(infd, termios.TCIFLUSH)
    except Exception:
        pass


def _ensure_atexit_restore() -> None:
    """进程被强杀到只剩 atexit 时仍尝试关掉鼠标跟踪；重复注册无害。"""
    global _atexit_restore_registered
    if _atexit_restore_registered:
        return
    atexit.register(restore_terminal)
    _atexit_restore_registered = True


def run_app(store, embed_ok: bool, direct=None, osc_report: bytes | None = None):
    """启动 Textual 界面并阻塞直至用户退出或选择启动某个会话。

    返回 `LaunchRequest | NewSessionRequest | None`。osc_report 是启动前探测到的
    外层终端 OSC 10/11 应答（见 corral._probe_osc_colours），用于内嵌面板聚焦托管
    会话时注入真实背景色。
    """
    from corral import i18n

    i18n.init()  # 按 CORRAL_LANG / 系统 locale 选定界面语言
    _ensure_atexit_restore()
    app = CorralApp(store, embed_ok, direct, osc_report)
    try:
        return app.run()
    except KeyboardInterrupt:
        # Warp 等终端会把 Ctrl+C 直接打成 SIGINT，绕过界面自己的按键处理。
        # asyncio.run 收尾再等默认线程池（最长 300 秒），第二次 Ctrl+C 就会把
        # Python 堆栈打到壳层。这里吞掉中断、同步恢复终端，当作正常退出。
        restore_terminal()
        return None
    finally:
        if app._stall_watchdog is not None:
            app._stall_watchdog.stop()
