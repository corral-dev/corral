"""分屏布局控制器：右栏分屏组合 / 会话组 / 置顶 / 焦点记忆的读写与轮询。

从 `main_screen.MainScreen` 拆出的方法组（架构整改阶段四）。侧边栏记忆
（`split_layout`）是多窗口共享的：`_layout_db` 是唯一写入口（事务内重读最新再
叠加，多窗口不互相覆盖），`_split_store` 只是给界面渲染用的本地快照，靠
revision 轮询跟上别的窗口。状态（`_layout_db` / `_split_store` /
`_layout_revision`）仍挂在 MainScreen 实例上。
"""

from __future__ import annotations

from textual import work

from corral.ui.session_list import SessionListView


def _preserve_missing_group_members(
    stored, target_keys: list[str], *, include_inactive: bool,
) -> bool:
    """浏览既有分组时，暂未扫描到的成员不能被当作用户移出分组。"""
    if not include_inactive or stored is None:
        return False
    return bool(set(stored.session_keys) - set(target_keys))


class LayoutControllerMixin:
    """依赖宿主提供：`embed_ok`、`direct`、`store`、`_split_area()`、
    `_is_session_active`、`_can_autofocus`、`_begin_attention_read`、
    `_render_detail`、`_warm_conversation`、`_rebuild_list`、`_update_header`。"""

    def _reconcile_split_session_keys(self) -> dict[str, str]:
        """占位卡转正后 session_key 会变；按 keepalive 对齐分屏并返回键迁移。

        不只看当前右栏格：sqlite 里的会话组 / 独立置顶若还挂着占位键，右栏已经
        切走时也必须迁。否则侧栏按正式键找不到第二人，会把整组藏成两张独立卡
        （2026-08-30 真机：刚在分屏里开的两个会话自己拆开）。
        """
        import corral
        from corral.keepalive import session_name

        key_by_keepalive: dict[str, str] = {}
        owners: dict[str, str] = {}
        ambiguous: set[str] = set()
        for bucket in self.store.sessions.values():
            for session in bucket:
                kname = session.get("keepalive_name")
                if not kname:
                    continue
                key = corral.session_key(session)
                key_by_keepalive[str(kname)] = key
                if owners.setdefault(str(kname), key) != key:
                    # 两条会话挂了同一个托管名（扫描串台时真实发生过）：谁都不
                    # 迁，否则会把分屏格改绑到错的会话上，把会话组拆掉。
                    ambiguous.add(str(kname))
        for key, kname in getattr(self.store, "hosted", {}).items():
            if kname and str(kname) not in ambiguous:
                key_by_keepalive.setdefault(str(kname), key)
        for kname in ambiguous:
            key_by_keepalive.pop(kname, None)

        migrated: dict[str, str] = {}

        def migrate(old_key: str, new_key: str | None) -> None:
            if not old_key or not new_key or old_key == new_key or old_key in migrated:
                return
            self._apply_layout_change(
                lambda store, o=old_key, n=new_key: store.migrate_session_key(o, n)
            )
            migrated[old_key] = new_key

        pending = getattr(self.store, "session_key_migrations", None)
        recorded = pending() if callable(pending) else pending
        if isinstance(recorded, dict):
            for old_key, new_key in recorded.items():
                migrate(old_key, new_key)

        area = self._split_area()
        for spec in area.pane_specs():
            kname = spec.keepalive_name
            if not kname:
                continue
            migrate(spec.session_key, key_by_keepalive.get(kname))

        split_store = getattr(self, "_split_store", None)
        find_session = getattr(self.store, "find_session", None)
        stale_keys: list[str] = []
        if split_store is not None:
            for group in split_store.groups.values():
                stale_keys.extend(group.session_keys)
            stale_keys.extend(split_store.pinned_session_keys)
            if split_store.last_focus_key:
                stale_keys.append(split_store.last_focus_key)
        for old_key in stale_keys:
            if old_key in migrated:
                continue
            if callable(find_session) and find_session(old_key) is not None:
                continue
            runtime_id, sep, ident = old_key.partition(":")
            if not sep or not runtime_id or not ident:
                continue
            migrate(old_key, key_by_keepalive.get(session_name(runtime_id, ident)))

        area.reconcile_session_keys(key_by_keepalive)
        return migrated

    def _sync_split_marks(self) -> None:
        """把右栏当前分屏组合投影到侧边栏：整组铺底，激活会话再重一档。

        右栏格数、格内绑定的会话、激活格都可能变；这里统一取一次现状交给列表，
        列表内部会跟上次比对，没变就不动 DOM。

        选中会话组时仍然保留激活格：Group 行和全部成员都铺组底色，当前持有输入
        的那一格对应成员再叠一层更重的激活底色。
        """
        if not self.embed_ok:
            return
        try:
            area = self._split_area()
            session_list = self.query_one(SessionListView)
        except Exception:  # noqa: BLE001 分栏/列表重建中间态查不到，下一轮兜底同步会补上
            return
        session_list.set_split_marks(area.ordered_session_keys(), area.focus_key)

    def _apply_layout_change(self, mutate):
        """把一次侧边栏记忆改动交给记忆库，并把返回的最新快照就地采纳。

        **所有写侧边栏记忆的路径都必须走这里**，不要直接改 `self._split_store`：
        本地快照可能已经落后于别的窗口，直接改再整份写盘就是当初那个「多开窗口互相
        抹掉对方置顶和分组」的缺陷。库会在事务里重读最新状态再重放这次改动。
        """
        snapshot = self._layout_db.apply(mutate)
        self._adopt_layout(snapshot)
        return self._split_store

    def _adopt_layout(self, snapshot) -> None:
        # 就地更新：SessionListView.group_store 持有的是同一个引用，换实例会让侧边栏渲染旧对象。
        self._split_store.adopt(snapshot)
        self._layout_revision = snapshot.revision

    def _poll_layout_state(self) -> None:
        """跟上别的 corral 窗口对侧边栏记忆的改动。

        只读一个版本号；版本没变直接返回。版本变了才读整份快照，并且只有「看得见的
        部分」（组成员/组名/折叠/置顶）真的变了才重建列表——只切焦点不该触发秒级重建。
        """
        from corral import split_layout

        try:
            revision = self._layout_db.read_revision()
        except Exception:  # noqa: BLE001 记忆库是可降级能力，任何异常都不该打断界面
            return
        if revision == self._layout_revision:
            return
        before = split_layout.sidebar_fingerprint(self._split_store)
        self._adopt_layout(self._layout_db.read())
        if split_layout.sidebar_fingerprint(self._split_store) == before:
            return
        self._sync_split_marks()
        self.call_next(self._rebuild_sidebar_projection)

    def _persist_split_composition(self) -> None:
        """右栏格数/成员变了：把当前组合断言进侧边栏记忆。

        已结束成员仍属于会话组，因此持久化不按活跃状态裁剪。顺手把侧边栏的当前组与
        激活会话底色对齐。
        """
        if getattr(self, "_activity_board_active", False):
            self._sync_split_marks()
            return
        if not self.embed_ok:
            return
        self._sync_split_marks()
        area = self._split_area()
        keys = [
            k for k in area.ordered_session_keys()
            if not k.startswith("__")
        ]
        if len(keys) < 2:
            # 单格不构成会话组；组的解散由 _on_pane_close / 删除会话那条路负责。
            return
        focus = area.focus_key if area.focus_key in keys else keys[0]
        project = area.current_project
        self._apply_layout_change(
            lambda store: store.set_group(project, keys, focus_key=focus)
        )

    def _persist_split_focus(self) -> None:
        """只换了焦点格：更新焦点记忆，**不重写会话组**。

        这里绝不能退回 `set_group()`：那会把当前右栏组合整份重新断言一遍，另一个窗口
        刚把某个成员移出去时，这边一切焦点就又把组重建回来（组名还会重新随机），两个
        窗口来回打架。
        """
        if getattr(self, "_activity_board_active", False):
            return
        if not self.embed_ok:
            return
        self._sync_split_marks()
        area = self._split_area()
        focus = area.focus_key
        if not focus or focus.startswith("__"):
            return
        if self._split_store.get_group(focus) is None:
            return
        project = area.current_project
        self._apply_layout_change(lambda store: store.set_focus(project, focus))

    def _on_pane_close(self, session_key: str) -> None:
        if getattr(self, "_activity_board_active", False):
            self._dismiss_board_pane(session_key)
            return
        remaining_focus = None
        if self.embed_ok:
            remaining_focus = self._split_area().focus_key
        self._cancel_follow_selection()
        self._suppress_selection_follow = getattr(self, "_suppress_selection_follow", 0) + 1
        self.query_one(SessionListView).begin_preserve_scroll()
        self._apply_layout_change(lambda store: store.remove_session(session_key))
        self._sync_split_marks()
        self.call_next(self._rebuild_sidebar_projection, remaining_focus, True)
        # 焦点由 SplitPaneArea 收尾：还有剩余实时格就接着用，最后一格被关掉才
        # 回列表。这里再调一次 _focus_list() 会把焦点提前抢走，让接力落空。

    async def _rebuild_sidebar_projection(
        self, select_key: str | None = None, preserve_scroll: bool = False,
    ) -> None:
        """只重建会话组树，不触发右栏跟随，避免关格时重挂仍存活的同伴格。"""
        try:
            session_list = self.query_one(SessionListView)
            await session_list.rebuild(select_key=select_key)
            self._update_header()
            self._sync_split_marks()
        finally:
            if preserve_scroll:
                self.call_after_refresh(
                    self.query_one(SessionListView).end_preserve_scroll,
                )
            self.call_after_refresh(self._release_selection_follow_suppress)

    def _try_restore_startup_layout(self) -> None:
        """启动时从持久会话组中恢复仍活跃/托管的成员。"""
        if not self.embed_ok or self.direct is not None:
            return
        # 扫描未完成时 _is_session_active 全假；此时 prune+save 会把磁盘上的
        # 分屏记忆整份清空，且后续首屏也不会再恢复（真机：重启后组合丢失）。
        if not self.store.loaded:
            return
        # 首扫期间别的窗口可能已经改过记忆，恢复前先取一份最新的。
        self._adopt_layout(self._layout_db.read())
        self._reconcile_split_session_keys()
        focus = self._split_store.last_focus_key
        if focus and self._is_session_active(focus):
            self._show_session_group(focus)
            return
        project = self._split_store.last_project
        if not project:
            return
        for group in self._split_store.groups.values():
            if group.project_cwd != project:
                continue
            alive = [k for k in group.session_keys if self._is_session_active(k)]
            if alive:
                self._show_session_group(alive[0])
                return

    def _show_session_group(
        self,
        focus_key: str,
        *,
        focus_pane: bool = False,
        include_inactive: bool = False,
    ) -> None:
        if not self.embed_ok:
            return
        import corral
        from corral import split_layout

        group = self._split_store.get_group(focus_key)
        if include_inactive and group is not None:
            project = group.project_cwd
            keys = [
                key
                for key in group.session_keys
                if self.store.find_session(key) is not None
            ]
        else:
            project, keys = split_layout.resolve_active_group(
                self._split_store,
                focus_key,
                is_active=self._is_session_active,
                find_session=self.store.find_session,
            )
        entries = self._build_hosted_entries(keys)
        if not entries:
            return
        area = self._split_area()
        # 浏览已有会话组只换焦点记忆，禁止 set_group：后者会抬 updated_at、
        # 整表写盘，还会在多窗口下把对方刚改过的组合整份断言回来。
        stored = self._split_store.get_group(focus_key)
        target_keys = [corral.session_key(session) for session, _, _ in entries]
        preserve_missing_members = _preserve_missing_group_members(
            stored, target_keys, include_inactive=include_inactive,
        )
        composition_unchanged = (
            preserve_missing_members
            or (stored is not None and list(stored.session_keys) == target_keys)
        )
        if preserve_missing_members:
            from corral import observe

            observe.event(
                "split_group_member_missing",
                missing_count=len(set(stored.session_keys) - set(target_keys)),
            )
        area.show_hosted_group(
            project, entries, focus_key=focus_key, focus_pane=focus_pane and self._can_autofocus(),
        )
        if composition_unchanged:
            self._persist_split_focus()
        else:
            self._persist_split_composition()
        self._begin_attention_read(focus_key)
        self._prefetch_group_screens(entries)

    def _prefetch_group_screens(self, entries: list[tuple[dict, str | None, object]]) -> None:
        """空闲时给当前组缺屏缓存的托管会话抓一帧，下次切回少闪。"""
        names = [str(kname) for _session, kname, _renderer in entries if kname]
        if not names:
            return
        width, height = 0, 0
        try:
            area = self._split_area()
            for cell in area.cells():
                pane = cell.embed_pane()
                if pane is not None and pane.size.width > 0 and pane.size.height > 0:
                    width, height = int(pane.size.width), int(pane.size.height)
                    break
            if width <= 0:
                width, height = area.host_pane_size()
        except Exception:  # noqa: BLE001 尺寸探测失败仍用默认解析宽高
            width, height = 0, 0
        self._prefetch_screens_worker(names, width, height)

    @work(thread=True, group="screen-prefetch", exclusive=True)
    def _prefetch_screens_worker(
        self, names: list[str], width: int = 0, height: int = 0,
    ) -> None:
        from corral.ui import embed_pane as embed_pane_mod

        for name in names:
            embed_pane_mod.prefetch_cached_screen(name, width=width, height=height)

    def _build_hosted_entries(
        self, keys: list[str],
    ) -> list[tuple[dict, str | None, object]]:
        entries: list[tuple[dict, str | None, object]] = []
        seen_names: set[str] = set()
        for key in keys:
            resolve = getattr(self.store, "canonical_session_key", None)
            canonical = resolve(key) if callable(resolve) else key
            session = self.store.find_session(canonical)
            if session is None and canonical != key:
                session = self.store.find_session(key)
            if session is None:
                continue
            kname = session.get("keepalive_name")
            if not kname:
                hosted_of = getattr(self.store, "hosted_name_for", None)
                if callable(hosted_of):
                    kname = hosted_of(canonical)
            if kname:
                name = str(kname)
                if name in seen_names:
                    # 扫描串台后两条会话会挂同一个托管名。第二格若再抓同一份
                    # tmux 画面，分屏就会看起来一模一样；改走这份会话自己的
                    # 静态预览，实时画面只留给先占到这个名字的那一格。
                    entries.append((session, None, self._detail_renderer_for(session)))
                    if session.get("live"):
                        self._warm_conversation(session, self._preview_gen)
                    continue
                seen_names.add(name)
                # Keep the transcript renderer as an ended fallback only. The
                # live pane still mounts without it so capture gaps cannot
                # flash chat; once the host is confirmed gone the pane
                # switches to this preview instead of a blank "session ended".
                entries.append((session, name, self._detail_renderer_for(session)))
                continue
            entries.append((session, None, self._detail_renderer_for(session)))
            if session.get("live"):
                # 在别的窗口跑的会话没有实时画面，右栏就是这份对话；助手还在写
                # 历史文件，mtime 一变 peek_conversation 就失效（正文会空掉），
                # 必须每轮重扫后台补读一次，"下方对话持续更新"才成立。
                self._warm_conversation(session, self._preview_gen)
        return entries

    def _open_split_from_selection(self, keys: list[str]) -> None:
        """按侧边栏多选组合开分屏（活跃会话内嵌，已结束会话预览）。"""
        if not self.embed_ok or len(keys) < 2:
            return
        import corral
        from corral.split_layout import MAX_PANES

        keys = keys[:MAX_PANES]
        focus_key = keys[0]
        focus_session = self.store.find_session(focus_key)
        project = corral._normalize_cwd(focus_session.get("cwd")) if focus_session else ""
        entries = self._build_hosted_entries(keys)
        if len(entries) < 2:
            self.app.bell()
            return
        area = self._split_area()
        area.show_hosted_group(
            project, entries, focus_key=focus_key, focus_pane=self._can_autofocus(),
        )
        self._begin_attention_read(focus_key)
        self._sync_split_marks()
        self._apply_layout_change(
            lambda store: store.set_group(project, keys, focus_key=focus_key)
        )
        self.call_next(self._rebuild_list, focus_key)
        self._preview_gen += 1
        for key in keys:
            session = self.store.find_session(key)
            if session is not None:
                self._warm_conversation(session, self._preview_gen)
