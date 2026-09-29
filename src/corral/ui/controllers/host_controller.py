"""内嵌托管控制器：启动计划 → 后台 host worker → 成功收尾挂进右栏。

从 `main_screen.MainScreen` 拆出的方法组（架构整改阶段四）。`embed.host_session`
是真正阻塞的 tmux 子进程调用（`_CREATE_TIMEOUT` 上限 5s），必须甩给后台 worker，
不在 Textual 事件循环所在线程上跑，否则系统负载高/磁盘慢时整个 UI 被冻住那么久。
状态（`_host_pending`）仍挂在 MainScreen 实例上。
"""

from __future__ import annotations

import os

from textual import work

from corral.i18n import t
from corral.models import SHELL_RUNTIME_ID, LaunchPlan
from corral.runtime.base import LaunchError


def _shell_launch_plan(cwd: str) -> LaunchPlan:
    shell = (os.environ.get("SHELL") or "").strip() or "/bin/bash"
    if not os.path.isfile(shell) or not os.access(shell, os.X_OK):
        shell = "/bin/sh"
    return LaunchPlan(argv=(shell,), cwd=cwd)


class HostControllerMixin:
    """依赖宿主提供：`direct`、`embed_ok`、`osc_report`、`store`、`_split_area()`、
    `_can_autofocus`、`_render_detail`、`_show_session_group`、`_persist_split_composition`、
    `_begin_attention_read`、`_rebuild_list`、`_restore_direct_search_focus`。"""

    def _embed_open(self, request, *, add_pane: bool = False) -> None:
        """准备启动计划（不涉及阻塞 I/O）后，把 `embed.host_session` 这个真正阻塞的
        tmux 子进程调用甩给后台 worker（见 `_host_and_focus`），不在 Textual 事件
        循环所在线程上跑——tmux 卡顿（系统负载高/磁盘慢）时 `_CREATE_TIMEOUT` 上限
        有 5s，同步跑会把整个 UI 冻住那么久。"""
        import corral
        from corral import keepalive
        from corral.split_layout import MAX_PANES

        # 原生恢复 = 同助手且未 force_new / copy_session；
        # 高级操作同助手另起 / 复制会话的官方分叉走新建分支（旁挂 + 占位卡）。
        native_resume = isinstance(request, corral.LaunchRequest) and (
            request.session.get("source") == request.target_runtime_id
            and not request.force_new
            and not request.copy_session
        )
        area = self._split_area()
        if isinstance(request, corral.LaunchRequest):
            key = corral.session_key(request.session)
            current = self.store.find_session(key) or request.session
            request = corral.LaunchRequest(
                current,
                request.target_runtime_id,
                request.title,
                force_new=request.force_new,
                copy_session=request.copy_session,
            )
            existing = request.session.get("keepalive_name") if native_resume else None
            if existing:
                # 回车打开已托管会话 = 明确意图，直接把输入交给右栏那一格。
                if add_pane:
                    area.add_hosted_pane(
                        current, str(existing),
                        self._detail_renderer_for(current),
                        focus=True,
                        focus_pane=self._can_autofocus(),
                    )
                else:
                    self._show_session_group(key, focus_pane=True)
                return
            if self._host_pending > 0 and not add_pane:
                self.app.bell()
                return
            if (
                add_pane
                and not getattr(self, "_board_skips_split_cap", lambda: False)()
                and (area.pane_count() + self._host_pending) >= MAX_PANES
            ):
                self.notify(t("split.full", n=MAX_PANES))
                self.app.bell()
                return
            try:
                plan = self.store.registry.build_launch_plan(request)
            except LaunchError as exc:
                # 缺历史路径等是可预期拒绝，不能冒泡成 WorkerFailed 把 TUI 打崩。
                self.notify(t("error.launch_failed", error=exc))
                self.app.bell()
                return
            ident = request.session["id"] if native_resume else keepalive.new_session_ident()
        else:
            if not add_pane and area.pane_count() > 0 and not area.can_add_pane():
                self.notify(t("split.full", n=MAX_PANES))
                self.app.bell()
                return
            if self._host_pending > 0 and not add_pane:
                self.app.bell()
                return
            if (
                add_pane
                and not getattr(self, "_board_skips_split_cap", lambda: False)()
                and (area.pane_count() + self._host_pending) >= MAX_PANES
            ):
                self.notify(t("split.full", n=MAX_PANES))
                self.app.bell()
                return
            try:
                plan = self.store.registry.build_new_session_plan(request)
            except LaunchError as exc:
                self.notify(t("error.launch_failed", error=exc))
                self.app.bell()
                return
            ident = keepalive.new_session_ident()

        width, height = area.host_pane_size()
        self._host_pending += 1
        self._host_and_focus(
            request, plan, ident, native_resume, width, height, add_pane=add_pane,
        )

    @work(thread=True, group="host")
    def _host_and_focus(
        self, request, plan, ident, native_resume, width, height, *, add_pane: bool = False,
    ) -> None:
        import time

        import corral
        from corral import embed, observe

        t0 = time.perf_counter()
        runtime = request.target_runtime_id
        try:
            name = embed.host_session(
                plan, request.target_runtime_id, ident, width, height, osc_report=self.osc_report,
            )
        except Exception as exc:
            observe.event(
                "host_session",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                runtime=runtime,
                ok=False,
            )
            corral._log_embed_error("内嵌会话启动线程", exc)
            self.app.call_from_thread(self._on_host_failed)
            return
        # 新 tmux 会话刚建好：控制通道（`tmux -C attach`，含 `_ready.wait` +
        # pane_id 查询，重启瞬间 tmux 正忙时是主线程卡顿的来源之一）在这里
        # 直接预热。主线程 `focus_session` 随后进来是池命中，只换回调 + LRU，
        # 约 0ms；预热失败也不影响，全部路径仍会自动回退外部 fork。
        try:
            embed.open_channel(name)
        except Exception:  # noqa: BLE001 预热失败不该影响托管成功收尾
            pass
        observe.event(
            "host_session",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            runtime=runtime,
            ok=True,
        )
        self.app.call_from_thread(
            self._on_embed_hosted, request, name, native_resume, add_pane,
        )

    def _on_host_failed(self) -> None:
        """host worker 失败收尾：释放托管计数并给用户终端响铃。"""
        self._host_pending = max(0, self._host_pending - 1)
        self._shell_after_board = False
        self._restore_direct_search_focus()
        self.app.bell()

    def _on_embed_hosted(
        self, request, name: str, native_resume: bool, add_pane: bool = False,
    ) -> None:
        """`_host_and_focus` worker 成功后的收尾：只在主线程操作 Textual/store 状态。

        `request` 可能是 `LaunchRequest`（恢复/接力）或 `NewSessionRequest`（空白新建）。
        后者没有关联会话，不能读 `.session`——空白新建路径曾经因此闪退。

        接力新建（含同助手 force_new）/ 空白新建时目标助手可能尚未落盘历史（例如
        Cursor 卡在 Workspace Trust），扫描器看不到条目；必须立刻插入托管占位卡并
        选中它，否则左栏空白、随后的 `_rebuild_list` 还会按仍选中的源会话把右栏盖回去。
        """
        import corral

        self._host_pending = max(0, self._host_pending - 1)
        area = self._split_area()
        fallback = None
        select_key = None
        if isinstance(request, corral.LaunchRequest):
            current = request.session
            if native_resume:
                key = corral.session_key(request.session)
                marked = self.store.mark_hosted(key, name)
                if marked is None:
                    request.session["keepalive_name"] = name
                current = marked or request.session
            else:
                source_name = self.store.registry.get(
                    str(request.session.get("source") or "")
                ).display_name
                if request.copy_session:
                    title = request.title or t("session.title.copy", name=source_name)
                else:
                    title = request.title or t("session.title.handoff", name=source_name)
                current = self.store.register_hosted_session(
                    runtime_id=request.target_runtime_id,
                    keepalive_name=name,
                    title=title,
                    cwd=str(request.session.get("cwd") or "") or None,
                )
                select_key = corral.session_key(current)

            fallback = self._detail_renderer_for(current)
        else:
            runtime = self.store.registry.get(request.target_runtime_id)
            current = self.store.register_hosted_session(
                runtime_id=request.target_runtime_id,
                keepalive_name=name,
                title=t("session.title.new", name=runtime.display_name),
                cwd=request.cwd,
            )
            select_key = corral.session_key(current)

            fallback = self._detail_renderer_for(current)
        # 新建 / 接力托管成功同样是明确意图：用户就是来跟这个新会话说话的。
        autofocus = self._can_autofocus()
        if getattr(self, "_activity_board_active", False):
            # 看板自己按关注态铺格，不要把新托管塞进持久组或切走侧栏。
            self._begin_attention_read(corral.session_key(current))
            self.call_next(self._rebuild_list, None)
            self._show_activity_board(focus_pane=autofocus)
            return
        if add_pane:
            area.add_hosted_pane(
                current, name, fallback, focus=True, focus_pane=autofocus,
            )
        elif self._split_store.get_group(corral.session_key(current)) is not None:
            # 重启的是会话组里的成员：整组一起摆回去，只是它那一格从静态预览换成
            # 实时画面。退回单格会把用户的分屏组合当场拆掉。
            self._show_session_group(
                corral.session_key(current), focus_pane=True, include_inactive=True
            )
        else:
            import corral as corral_mod

            key = corral.session_key(current)
            project = corral_mod._normalize_cwd(current.get("cwd"))
            area.show_hosted_group(
                project,
                [(current, name, fallback)],
                focus_key=key,
                focus_pane=autofocus,
            )
        self._persist_split_composition()
        self._begin_attention_read(corral.session_key(current))
        self.call_next(self._rebuild_list, select_key)
        # 托管刚成功：pid 快照变化让下一次签名必穿，全量重扫（秒级）会和
        # 首帧抓取抢 GIL。退避一轮，让首帧先上屏（见主屏刷新循环与
        # REFRESH_HOST_COOLDOWN；不是拉长间隔，下一轮自然补扫）。
        try:
            import time as _time

            from corral.ui.main_screen import REFRESH_HOST_COOLDOWN

            self._refresh_cooldown_until = _time.monotonic() + REFRESH_HOST_COOLDOWN
        except Exception:  # noqa: BLE001 退避失败不该影响托管收尾
            pass

    def _embed_open_shell(self, cwd: str) -> None:
        """顶栏「终端」：在当前项目目录下内嵌一个可自由输入的 shell 分屏。"""
        from corral.split_layout import MAX_PANES

        area = self._split_area()
        replace_board = bool(getattr(self, "_shell_after_board", False))
        if not replace_board and not area.can_add_pane():
            self.notify(t("split.full", n=MAX_PANES))
            self.app.bell()
            return
        if self._host_pending > 0:
            self.app.bell()
            return
        if (
            not replace_board
            and area.pane_count() + self._host_pending >= MAX_PANES
        ):
            self.notify(t("split.full", n=MAX_PANES))
            self.app.bell()
            return
        plan = _shell_launch_plan(cwd)
        from corral import keepalive

        ident = keepalive.new_session_ident()
        width, height = area.host_pane_size()
        self._host_pending += 1
        self._host_shell_and_focus(cwd, plan, ident, width, height)

    @work(thread=True, group="host")
    def _host_shell_and_focus(
        self, cwd: str, plan: LaunchPlan, ident: str, width: int, height: int,
    ) -> None:
        import time

        import corral
        from corral import embed, observe

        t0 = time.perf_counter()
        try:
            name = embed.host_session(
                plan, SHELL_RUNTIME_ID, ident, width, height, osc_report=self.osc_report,
            )
        except Exception as exc:
            observe.event(
                "host_session",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                runtime=SHELL_RUNTIME_ID,
                ok=False,
            )
            corral._log_embed_error("内嵌 shell 启动线程", exc)
            self.app.call_from_thread(self._on_host_failed)
            return
        observe.event(
            "host_session",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            runtime=SHELL_RUNTIME_ID,
            ok=True,
        )
        self.app.call_from_thread(self._on_shell_hosted, name, cwd)

    def _on_shell_hosted(self, name: str, cwd: str) -> None:
        self._host_pending = max(0, self._host_pending - 1)
        area = self._split_area()
        current = self.store.register_hosted_session(
            runtime_id=SHELL_RUNTIME_ID,
            keepalive_name=name,
            title=t("shell.pane_title"),
            cwd=cwd,
        )
        if getattr(self, "_shell_after_board", False):
            # 看板格子是异步挂载的，不能往仍占着的四格上再加终端；整页换成这一格。
            self._shell_after_board = False
            import corral

            key = corral.session_key(current)
            project = corral._normalize_cwd(cwd) or cwd
            area.show_hosted_group(
                project,
                [(current, name, None)],
                focus_key=key,
                focus_pane=self._can_autofocus(),
            )
            self._persist_split_composition()
            return
        area.add_hosted_pane(
            current,
            name,
            None,
            focus=True,
            focus_pane=self._can_autofocus(),
            is_shell=True,
        )
        self._persist_split_composition()

    def _host_direct_launch(self) -> None:
        if self._host_pending >= 3:
            self.app.bell()
            return
        direct = self.direct
        area = self._split_area()
        width, height = area.host_pane_size()
        self._host_pending += 1
        self._host_direct_worker(direct, width, height)

    @work(thread=True, group="host")
    def _host_direct_worker(self, direct, width: int, height: int) -> None:
        import time

        import corral
        from corral import embed, observe

        t0 = time.perf_counter()
        runtime = direct.runtime_id
        try:
            name = embed.host_session(
                direct.plan, direct.runtime_id, direct.ident, width, height, osc_report=self.osc_report,
            )
        except Exception as exc:
            observe.event(
                "host_session",
                duration_ms=int((time.perf_counter() - t0) * 1000),
                runtime=runtime,
                ok=False,
            )
            corral._log_embed_error("直启会话启动线程", exc)
            self.app.call_from_thread(self._on_host_failed)
            return
        observe.event(
            "host_session",
            duration_ms=int((time.perf_counter() - t0) * 1000),
            runtime=runtime,
            ok=True,
        )
        self.app.call_from_thread(self._on_direct_hosted, name)

    def _on_direct_hosted(self, name: str) -> None:
        self._host_pending = max(0, self._host_pending - 1)
        area = self._split_area()
        direct = self.direct
        runtime = self.store.registry.get(direct.runtime_id)
        cwd = direct.plan.cwd or os.getcwd()
        session = self.store.register_hosted_session(
            runtime_id=direct.runtime_id,
            keepalive_name=name,
            title=t("session.title.new", name=runtime.display_name),
            cwd=cwd,
            ident=direct.ident,
        )
        from corral import _normalize_cwd, session_key

        key = session_key(session)
        # 直启挂载期间搜索框是关掉的，用户不可能正在里面打字；先按自动聚焦
        # 条件把意图交给右栏，再恢复搜索框可聚焦。挂载是异步的，禁止在
        # pane 尚未创建时 set_focus 失败就放弃——真机默认焦点在侧边栏，
        # 不带 focus_pane 的挂载收尾会把焦点还回去（corral cursor 真机复现）。
        autofocus = self._can_autofocus()
        area.show_hosted_group(
            _normalize_cwd(cwd),
            [(session, name, None)],
            focus_key=key,
            focus_pane=autofocus,
        )
        self._persist_split_composition()
        self._begin_attention_read(key)
        self.call_next(self._rebuild_list, key)
        self._restore_direct_search_focus()
