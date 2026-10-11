"""关注已读跟踪器：红点会话在右侧真实可见（内容就绪）后立即标已读。

从 `main_screen.MainScreen` 拆出的方法组（架构整改阶段四）：只依赖屏幕提供的
`embed_ok` / `_app_focused` / `_split_area()` / `store` / `set_timer` / `query_one`。
状态（`_attention_read_keys` / `_app_focused`）仍挂在 MainScreen 实例上，这里只是
方法容器。
"""

from __future__ import annotations

from corral.ui.session_list import SessionListView

# 红点只要右侧内容真实可见（托管格首帧写出、静态预览对话缓存就绪）就立即清除，
# 不再要求连续停留时长；等待首帧或对话缓存时做轻量轮询，不能把「选中过」误当成
# 「看过了」。
_ATTENTION_READY_POLL = 0.1


class AttentionReaderMixin:
    """依赖宿主提供：`embed_ok`、`_app_focused`、`_split_area()`、`store`。"""

    def _on_app_focus_changed(self, focused: bool) -> None:
        """终端应用失焦即停止观察；重新聚焦后重新开始。"""
        self._app_focused = bool(focused)
        self._cancel_attention_read()
        if focused:
            self.call_next(self._begin_selected_attention_read)

    def _cancel_attention_read(self) -> None:
        timer = self._attention_read_timer
        self._attention_read_timer = None
        if timer is not None:
            timer.stop()
        self._attention_read_keys = set()

    def _begin_selected_attention_read(self) -> None:
        if not self.embed_ok:
            return
        key = self.query_one(SessionListView)._displayed_selected_key()
        if key is None:
            return
        self._begin_attention_read(key)

    def _begin_attention_read(self, key: str | None = None) -> None:
        """开始观察右侧当前可见的红点会话；内容真实就绪后立即标已读。

        多分屏下所有可见格同屏可见，一起纳入观察——用户既然看到了每一格，
        红点就该一起清。`key` 只是调用方触发观察的入口（选中 / 聚焦格），
        真正观察集合以分屏区当前规格为准，避免换页残留控件把已切走的会话
        误判成仍在看。
        """
        self._cancel_attention_read()
        if not self.embed_ok or not self._app_focused:
            return
        self._attention_read_keys = {key} if key else set()
        self._attention_read_timer = self.set_timer(
            _ATTENTION_READY_POLL, self._check_attention_read,
        )

    def _visible_attention_keys(self) -> set[str]:
        """右栏当前规格里的真实会话键；占位提示和残留控件都不算。"""
        keys: set[str] = set()
        for spec in self._split_area().pane_specs():
            key = spec.session_key
            if key and not key.startswith("__"):
                keys.add(key)
        return keys

    def _attention_cell_for(self, key: str):
        """会话当前是否以可见格呈现在右栏；返回那格，不在返回 None。"""
        if key not in self._visible_attention_keys():
            return None
        for cell in self._split_area().cells():
            if cell.spec.session_key == key:
                return cell
        return None

    def _attention_pane_ready(self, cell) -> bool:
        """这一格是否已画出可读内容：托管格要真实首帧，静态格要对话缓存有效。"""
        session = self.store.find_session(cell.spec.session_key)
        if session is None:
            return False
        pane = cell.embed_pane()
        if pane is None or pane.size.width <= 0 or pane.size.height <= 0:
            return False
        keepalive_name = cell.spec.keepalive_name
        if keepalive_name:
            # 仅挂上控件或正在显示静态回退都不算；首帧成功写入网格后，用户
            # 才真正看到了这个托管终端。
            return (
                pane.session_name == keepalive_name
                and not pane.dead
                and pane._grid is not None  # noqa: SLF001
            )
        # 静态详情只有对话缓存已成功填充后才算就绪；加载异常会一直保持 None，
        # 因而不会因为右栏只出现标题或空白回退而误清红点。peek 走严格模式：
        # 会话又写了新结果时缓存版本失效，红点保持到新内容真实渲染出来。
        return (
            pane.session_name is None
            and self.store.peek_conversation(session) is not None
        )

    def _check_attention_read(self) -> None:
        """轮询可见格内容就绪状态；红点会话一旦真实可见立即标已读。"""
        self._attention_read_timer = None
        if not self.embed_ok or not self._app_focused:
            self._attention_read_keys = set()
            return
        watched = self._visible_attention_keys()
        cleared = False
        pending = False
        for key in sorted(watched):
            session = self.store.find_session(key)
            if session is None or session.get("attention_kind") != "unread":
                continue
            cell = self._attention_cell_for(key)
            if cell is None or not self._attention_pane_ready(cell):
                # 画面尚未挂上（跟随节流未跑完 / 抓帧未出）或内容未就绪：
                # 继续等，不能把「选中过」误当成「看过了」。
                pending = True
                continue
            self.store.mark_session_read(key)
            cleared = True
        if cleared:
            # mark_session_read 已原地更新会话快照；重建让红点与详情头的
            # 可访问文字一起消失。不传 select_key：分屏下同时清多条时不得
            # 强行移动列表选中。
            self.call_next(self._rebuild_list)
        if pending:
            self._attention_read_timer = self.set_timer(
                _ATTENTION_READY_POLL, self._check_attention_read,
            )
            return
        self._attention_read_keys = set()
