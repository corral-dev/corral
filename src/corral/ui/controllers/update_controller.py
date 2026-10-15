"""客户端自动更新浮层控制器：右下角浮层的检查 / 预热 / 升级 / 重启 / 重试 / 忽略。

从 `main_screen.MainScreen` 拆出的方法组（架构整改阶段四）。每次打开 corral
都后台查一次最新版本；源码/开发安装（无法一键升级）时直接跳过，不弹窗打扰。
检查与预热都跑在 worker 线程，任何异常都不能拖垮 UI 或阻塞首屏。真正升级前
必须先退出界面：Homebrew / pipx / pip 都可能替换当前进程正在使用的安装目录，
旧界面继续渲染会在下一次惰性导入时随机缺模块。状态（`_update_channel` /
`_update_latest` / `_update_spec`）仍挂在 MainScreen 实例上。

两段式体验：先弹"发现新版"（能点，但点了要等下载），预热线程把安装包下到
本地后再把文案翻成"已备好"（点了只是本地安装 + 重启）。预热只做网络 I/O，
不碰现役安装目录；失败就当没预热过，旧路径照走。
"""

from __future__ import annotations

from textual import work
from textual.worker import get_current_worker

from corral import updater
from corral.ui.update_toast import UpdateToast


class UpdateControllerMixin:
    """依赖宿主提供：`app`、`query_one`。"""

    @work(thread=True, group="update-check")
    def _check_for_update(self) -> None:
        channel = updater.detect_channel()
        if not updater.is_updatable(channel):
            return
        latest = updater.fetch_latest()
        if latest is None or not updater.should_prompt(latest):
            return
        self._update_channel = channel
        self._update_latest = latest
        self._update_spec = None
        worker = get_current_worker()
        if not worker.is_cancelled:
            self.app.call_from_thread(lambda: self.query_one(UpdateToast).show_available(latest))
        # 预热：下载等网络活在后台做完，用户点更新时只剩本地安装。用户等不及
        # 先点了也无妨——spec 为 None 时走旧的在线路径，只是慢一点。
        ready = self._prewarm_update(latest, channel)
        if ready and not worker.is_cancelled:
            self.app.call_from_thread(lambda: self.query_one(UpdateToast).show_ready(latest))

    def _prewarm_update(self, latest: str, channel: str) -> bool:
        """后台预热，返回是否备好；pip/pipx 备好时把本地包路径存进实例。

        写 `self._update_spec` 的是 worker 线程、读的是点更新那一刻的主循环：
        GIL 下赋值本身原子，最坏只是读到 None 走旧路径，不会错装。自身绝不抛
        异常（updater 侧已全兜底，这里再兜一层）。
        """
        try:
            if channel == "brew":
                return updater.prewarm_brew()
            staged = updater.prepare_stage(latest, channel)
            if staged is not None:
                self._update_spec = staged
                return True
            return False
        except Exception:  # noqa: BLE001 - 预热失败只降级，不阻断提示
            return False

    def _take_update_spec(self) -> str | None:
        """取出预热好的本地安装目标；只有文件还在才算数。"""
        import os

        spec = getattr(self, "_update_spec", None)
        return spec if spec and os.path.isfile(spec) else None

    def _on_update_toast_update(self) -> None:
        self.app.exit(
            result=updater.RestartRequest(
                self._update_latest, self._update_channel, self._take_update_spec()
            )
        )

    def _on_update_toast_restart(self) -> None:
        # 兼容已进入完成态的浮层；正常路径点击「更新」时已经直接退出界面。
        self._on_update_toast_update()

    def _on_update_toast_retry(self) -> None:
        self._on_update_toast_update()

    def _on_update_toast_dismiss(self, version: str) -> None:
        updater.mark_dismissed(version)
        self.query_one(UpdateToast).hide()
