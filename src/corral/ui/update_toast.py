"""右下角新版本浮层：内容宽的 overlay 盒子，只占右下角那一小块。

定位：外层 UpdateToast 用 `layer: overlay` 出流（不挤占布局，显隐不抖动底栏），
靠父级对齐（`CorralApp.CSS` 里的 `MainScreen { align: right bottom; }`）钉到右下角，
自身 `width/height: auto` 缩到内容宽——命中区域只有浮层本身，底部其余整行照常可点。
不要改回 `dock: bottom + width: 1fr` 的整宽写法：那会让透明的外层横跨整个底条，
底下那几行全都点不到（session_hud 的注释里点过名；Textual 原生 ToastRack 也是
整宽，但它是_transient_通知，无所谓）。

`MainScreen{align}` 对其它子控件是 no-op：列表区 Horizontal(1fr×1fr)、龙动画
(100%×100% dock top)、页脚(dock bottom 整宽) 都是填满的，只有内容宽的浮层
会被 align 挪动。加新 overlay 浮层时先确认它是不是内容宽，否则会被一起甩到
右下角。

状态机：hidden → available(version) → updating → done(version) / failed(reason)。
点击主体按当前状态触发不同回调；除 updating（正在跑子进程，关掉会让用户以为
取消了）外都露出"忽略"命中区——**失败态尤其必须能关**：升级失败又关不掉的浮层
会一直横在界面底部，是真机上被用户直接骂过的体验事故。

真机调试踩坑记录：本文件早期版本把状态刷新方法命名为 `_render`，与 Textual
`Widget` 基类自身用来计算可绘制内容的内部方法 `_render()` 同名，被悄悄覆盖后
框架在需要自绘（如 dock+align 容器里子节点未占满的空白区域）时拿到 `None`
而不是真正的 Visual，导致 `Visual.to_strips` 内部崩溃（`None.render_strips`）。
教训：自定义 Widget 子类的私有方法一律避免 `_render`/`render`/其它任何与
Textual 基类同名的下划线前缀方法。
"""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual import events
from textual.containers import Container, Horizontal
from textual.widgets import Static

from corral.display import SPINNER_FRAMES
from corral.i18n import t

_SPIN_INTERVAL = 0.08  # 秒，与列表转圈圈一致的观感
_REASON_MAX = 80  # 字符；浮层最宽 60 列，原因最多占两行


def _short_reason(output: str) -> str:
    """把升级命令的整段输出压成一行可读原因：取最后一条非空行并截断。

    子进程输出常有几十行（pip 的下载进度、brew 的 tap 刷新），真正的失败信息
    几乎总在末尾；全量输出留在事件日志里，浮层只负责让用户看懂发生了什么。

    超长时**保留末尾**：这类行往往形如「<很长的解释器路径>: No module named pip」，
    砍掉尾巴恰好砍掉唯一有用的那半句。
    """
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    if not lines:
        return ""
    reason = lines[-1]
    return reason if len(reason) <= _REASON_MAX else "…" + reason[-(_REASON_MAX - 1):]


class _ToastBody(Static):
    """浮层主体：展示文案，点击触发当前状态对应的主动作。"""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    _ToastBody {
        pointer: pointer;
    }
    """

    def __init__(self, on_click: Callable[[], None], **kwargs) -> None:
        super().__init__("", **kwargs)
        self._on_click_cb = on_click

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_click_cb()


class _ToastClose(Static):
    """浮层的"忽略"小命中区，只在 available 状态下可见。"""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    _ToastClose {
        width: 3;
        height: auto;
        border: round $primary;
        border-left: none;
        content-align: center middle;
        color: $text-muted;
        pointer: pointer;
    }
    _ToastClose:hover {
        color: $text;
        background: $primary-darken-1;
    }
    """

    def __init__(self, on_click: Callable[[], None], **kwargs) -> None:
        super().__init__(t("update.dismiss"), **kwargs)
        self._on_click_cb = on_click

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self._on_click_cb()


class UpdateToast(Container):
    """右下角新版本提示；MainScreen 通过 show_*/hide 驱动状态，通过构造参数里的
    四个回调获知用户点击了什么。"""

    DEFAULT_CSS = """
    UpdateToast {
        layer: overlay;
        width: auto;
        height: auto;
        margin: 0 2 1 0;
        display: none;
    }
    UpdateToast.-visible {
        display: block;
    }
    UpdateToast Horizontal {
        width: auto;
        height: auto;
    }
    UpdateToast _ToastBody {
        width: auto;
        height: auto;
        max-width: 60;
        border: round $primary;
        background: $panel;
        padding: 0 1;
        color: $text;
    }
    UpdateToast _ToastBody.-failed {
        border: round $error;
    }
    UpdateToast _ToastBody.-done {
        border: round $success;
    }
    UpdateToast _ToastBody:hover {
        background: $primary-darken-1;
    }
    UpdateToast #toast-close {
        display: none;
    }
    UpdateToast.-closable #toast-close {
        display: block;
    }
    """

    def __init__(
        self,
        *,
        on_update: Callable[[], None],
        on_restart: Callable[[], None],
        on_retry: Callable[[], None],
        on_dismiss: Callable[[str], None],
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._on_update = on_update
        self._on_restart = on_restart
        self._on_retry = on_retry
        self._on_dismiss = on_dismiss
        self._state = "hidden"  # hidden | available | updating | done | failed
        self._version = ""
        self._reason = ""
        # 后台预热完成（安装包已下到本地 / brew bottle 已备好）：available 文案
        # 换成"已备好"，让用户敢点——点下去只是本地安装 + 重启，不是漫长等待。
        self._ready = False
        self._spin_index = 0
        self._spin_timer = None

    def compose(self):
        with Horizontal():
            yield _ToastBody(self._handle_body_click, id="toast-body")
            yield _ToastClose(self._handle_close_click, id="toast-close")

    # ---- 对外状态切换入口，MainScreen 在后台 worker 回调里调用 ----

    def show_available(self, version: str) -> None:
        self._version = version
        self._ready = False
        self._set_state("available")

    def show_ready(self, version: str) -> None:
        """预热完成：还是 available 态，只换"已备好"文案。

        用户可能已经点了更新（已退出）或忽略了（hidden）：那两种情况什么都不做，
        绝不把浮层重新顶出来。
        """
        if self._state != "available" or self._version != version:
            return
        self._ready = True
        if self.is_mounted:
            self._sync_display()

    def show_updating(self) -> None:
        self._set_state("updating")
        self._start_spin()

    def show_done(self, version: str) -> None:
        self._version = version
        self._stop_spin()
        self._set_state("done")

    def show_failed(self, reason: str = "") -> None:
        self._reason = _short_reason(reason)
        self._stop_spin()
        self._set_state("failed")

    def hide(self) -> None:
        self._stop_spin()
        self._set_state("hidden")

    # ---- 内部 ----
    # 注意：下面这个刷新方法不能叫 `_render`——见文件顶部踩坑记录，会和 Textual
    # Widget 基类的同名内部方法冲突导致崩溃。

    def _set_state(self, state: str) -> None:
        self._state = state
        if not self.is_mounted:
            return
        self._sync_display()

    def _sync_display(self) -> None:
        self.set_class(self._state != "hidden", "-visible")
        self.set_class(self._state in ("available", "failed", "done"), "-closable")
        body = self.query_one("#toast-body", _ToastBody)
        body.remove_class("-failed", "-done")
        if self._state == "available":
            key = "update.ready" if self._ready else "update.available"
            body.update(Text(t(key, version=self._version)))
        elif self._state == "updating":
            frame = SPINNER_FRAMES[self._spin_index % len(SPINNER_FRAMES)]
            body.update(Text(f"{frame} {t('update.updating')}"))
        elif self._state == "done":
            body.add_class("-done")
            body.update(Text(t("update.done_restart", version=self._version)))
        elif self._state == "failed":
            body.add_class("-failed")
            text = Text(t("update.failed_retry"))
            if self._reason:
                # 失败原因单独一行且淡一档：主行是可点的动作，原因只是佐证
                text.append("\n" + self._reason, style="dim")
            body.update(text)

    def _start_spin(self) -> None:
        self._stop_spin()
        self._spin_index = 0
        self._spin_timer = self.set_interval(_SPIN_INTERVAL, self._tick_spin)

    def _tick_spin(self) -> None:
        self._spin_index += 1
        if self._state == "updating":
            self._sync_display()

    def _stop_spin(self) -> None:
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None

    def _handle_body_click(self) -> None:
        if self._state == "available":
            self._on_update()
        elif self._state == "done":
            self._on_restart()
        elif self._state == "failed":
            self._on_retry()
        # updating 状态点击无动作，避免重复触发

    def _handle_close_click(self) -> None:
        if self._state in ("available", "failed"):
            # 失败后关闭同样记为「今天不再提醒这个版本」：升不上去还每次开都弹
            # 才是骚扰；次日仍会重新提醒，不会让用户永远错过新版本。
            self._on_dismiss(self._version)
        elif self._state == "done":
            # 已经装好了，用户只是不想立刻重启；下次启动版本已是最新，不会再弹
            self.hide()
