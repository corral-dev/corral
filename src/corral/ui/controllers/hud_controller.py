"""会话小窗控制器：每个右栏格右上角的提问概览浮层。

从 `main_screen.MainScreen` 拆出的方法组（架构整改阶段四）。主线程只做
stat + 内存缓存命中判断（`peek_conversation`），真正解析历史有 HUD_WARM_INTERVAL
节流并放后台 worker。状态（`_hud_expanded` / `_hud_cache` / `_hud_warm_at`）仍
挂在 MainScreen 实例上；`HUD_POLL_INTERVAL` 被 MainScreen.on_mount 引用，
由它从本模块导入。
"""

from __future__ import annotations

import time

from textual import work

from corral.ui.session_hud import summarize_user_messages

# 秒，右上角会话小窗的同步间隔。每次只做一次 stat + 内存缓存命中判断（`peek_conversation`），
# 真正解析历史另有节流（HUD_WARM_INTERVAL），不会因为助手在狂写历史就每秒重解析一遍。
HUD_POLL_INTERVAL = 1.0
HUD_WARM_INTERVAL = 3.0  # 秒，同一会话两次重新解析对话之间的最小间隔


class HudControllerMixin:
    """依赖宿主提供：`embed_ok`、`store`、`_split_area()`。"""

    def _hud_live_targets(self) -> list[tuple[str, dict]]:
        """返回该画小窗的 (会话键, 会话) 列表。

        实时托管格与静态对话预览格都画：长对话里小窗仍能一眼扫到提问脉络，
        不必整篇翻预览。占位卡（直启/空白新建后尚未写出真实历史）在快照里
        找不到会话，先不画。
        """
        if not self.embed_ok:
            return []
        try:
            area = self._split_area()
        except Exception:
            # 内嵌不可用时右栏根本不在 DOM 里（纯列表模式），不能裸 query_one。
            return []
        targets: list[tuple[str, dict]] = []
        for spec in area.pane_specs():
            session = self.store.find_session(spec.session_key)
            if session is None:
                continue
            targets.append((spec.session_key, session))
        return targets

    def _sync_hud(self) -> None:
        """把每个右栏格的小窗刷成各自最新摘要。主线程调用，只做 stat + 内存缓存判定。"""
        if not self.embed_ok:
            return
        try:
            area = self._split_area()
        except Exception:
            return
        payloads: dict[str, object | None] = {}
        to_warm: list[tuple[dict, str]] = []
        for key, session in self._hud_live_targets():
            messages = self.store.peek_conversation(session)
            if messages is None:
                # 助手正在写历史，内存缓存已按 mtime 失效：继续显示上一次的摘要，
                # 同时按节流去后台重解析，避免小窗每秒空一下再闪回来。
                payloads[key] = self._hud_cache.get(key)
                to_warm.append((session, key))
            else:
                data = summarize_user_messages(messages)
                self._hud_cache[key] = data
                payloads[key] = data or None
        area.sync_hud(payloads, expanded=self._hud_expanded)
        if to_warm:
            self._schedule_hud_warm(to_warm)

    def _schedule_hud_warm(self, items: list[tuple[dict, str]]) -> None:
        now = time.monotonic()
        due: list[tuple[dict, str]] = []
        for session, key in items:
            if now - self._hud_warm_at.get(key, 0.0) < HUD_WARM_INTERVAL:
                continue
            self._hud_warm_at[key] = now
            due.append((session, key))
        if due:
            self._warm_hud(due)

    @work(thread=True, exclusive=True, group="hud-warm")
    def _warm_hud(self, items: list[tuple[dict, str]]) -> None:
        """后台解析对话（超大会话可到 200ms 量级），完成后回主线程刷小窗。"""
        for session, _key in items:
            try:
                self.store.get_conversation(session)
            except Exception:
                continue
        self.app.call_from_thread(self._sync_hud)

    def action_toggle_hud(self) -> None:
        """Ctrl+G：展开/收起全部格的会话小窗，并清掉各格单独的选择。"""
        if not self.embed_ok:
            return
        self._hud_expanded = not self._hud_expanded
        try:
            self._split_area().clear_hud_overrides()
        except Exception:
            pass
        self._sync_hud()

    def jump_to_latest(self, key: str, cell) -> None:
        """Cancel prompt location now; return only this pane without blocking UI."""
        from corral import embed

        pane = cell.embed_pane()
        if pane is None or cell.spec.session_key != key:
            return
        self._prompt_navigation_revision = getattr(self, "_prompt_navigation_revision", 0) + 1
        revision = self._prompt_navigation_revision
        previous = getattr(self, "_prompt_navigation_row", None)
        if previous is not None and previous[0] is not None:
            previous[0].set_prompt_status(previous[1], None)
        self._prompt_navigation_row = None
        pane.return_to_latest()
        name = pane.session_name
        if name and not pane._uses_detail_window():
            embed.cancel_navigation(name)
            self._return_terminal_latest(key, name, cell, revision)

    @work(thread=True, group="prompt-navigation")
    def _return_terminal_latest(self, key: str, name: str, cell, revision: int) -> None:
        from corral.ui.terminal_prompt_navigation import return_to_latest

        def current():
            pane = cell.embed_pane()
            return (revision == self._prompt_navigation_revision and cell.spec.session_key == key
                    and pane is not None and pane.session_name == name)

        session = self.store.find_session(key)
        if not session or not current():
            return
        try:
            return_to_latest(name, str(session.get("source") or ""), is_current=current)
            self.app.call_from_thread(self._apply_prompt_jump, key, name, cell, revision, 0)
        except Exception as exc:
            from corral import observe

            observe.event("terminal_latest_failed", runtime=str(session.get("source") or ""),
                          reason=type(exc).__name__)

    def jump_to_terminal_prompt(self, key: str, body: str, cell, message_index: int | None = None,
                                token: str | None = None) -> None:
        hud = self._cell_hud(cell)
        if hud is not None and message_index is not None and hud.prompt_status(message_index) == "locating":
            # The same row is already locating; a repeated click must not restart it.
            return
        self._prompt_navigation_revision = getattr(self, "_prompt_navigation_revision", 0) + 1
        revision = self._prompt_navigation_revision
        pane = cell.embed_pane() if hasattr(cell, "embed_pane") else None
        if pane is not None and pane._uses_detail_window():
            from corral.ui.session_hud import _one_line, prompt_identity

            session = self.store.find_session(key)
            if session is None or cell.spec.session_key != key:
                return
            messages = self.store.peek_conversation(session, stale_ok=True)
            if messages is None or message_index is None or not 0 <= message_index < len(messages):
                return
            selected = messages[message_index]
            if selected.role != "user" or _one_line(selected.text) != body:
                return
            if token is not None and prompt_identity(messages, message_index) != token:
                return
            pane.reveal_detail_prompt(message_index)
            return
        previous = getattr(self, "_prompt_navigation_row", None)
        if previous is not None and previous[0] is not None:
            # A newer selection supersedes the earlier row without a failure message.
            previous[0].set_prompt_status(previous[1], None)
        self._prompt_navigation_row = (hud, message_index) if message_index is not None else None
        if hud is not None and message_index is not None:
            hud.set_prompt_status(message_index, "locating")
        self._navigate_terminal_prompt(key, body, cell, revision, message_index, token)

    @staticmethod
    def _cell_hud(cell):
        from corral.ui.session_hud import SessionHud

        try:
            return cell.query_one(SessionHud)
        except Exception:
            return None

    def _finish_prompt_status(self, revision: int, reason: str | None) -> None:
        """Clear the clicked row, or show a brief failure reason for the current selection."""
        from corral.i18n import t

        row = getattr(self, "_prompt_navigation_row", None)
        if row is None or row[0] is None or revision != self._prompt_navigation_revision:
            return
        hud, index = row
        self._prompt_navigation_row = None
        if reason is None or reason == "prompt_stale":
            hud.set_prompt_status(index, None)
        else:
            hud.set_prompt_status(index, "failed", t("hud.not_in_terminal" if reason == "prompt_absent"
                                                     else "hud.locate_failed"))

    @work(thread=True, group="prompt-navigation")
    def _navigate_terminal_prompt(self, key: str, body: str, cell, revision: int,
                                  message_index: int | None = None, token: str | None = None) -> None:
        started = time.monotonic()
        stage = "session"
        session = None
        try:
            from corral.ui.session_hud import _one_line, prompt_identity
            from corral.ui.terminal_prompt_navigation import (
                PromptNavigationError,
                buffer_offset,
                navigate_native,
                prompt_target,
            )

            session = self.store.find_session(key)
            # This window can be hosting the session before the next liveness
            # scan annotates it; ask the store's hosted map first, including
            # placeholder→real identity hops.
            name = str(self.store.hosted_name_for(key) or (session or {}).get("keepalive_name") or "")
            if not name or cell.spec.session_key != key:
                raise PromptNavigationError("prompt_unavailable")
            stage = "identity"
            # Prefer the HUD's already-cached conversation to avoid a full
            # re-parse on every click; live sessions rewrite history constantly
            # so get_conversation would always miss and cost 100-300ms.
            messages = self.store.peek_conversation(session, stale_ok=True)
            if messages is None:
                messages = self.store.get_conversation(session)
            else:
                # Validate the stale copy still identifies this row; if the
                # history grew or the token mismatches, fall back to a fresh
                # parse instead of landing on the wrong occurrence.
                try:
                    from corral.ui.session_hud import summarize_user_messages as _summarize

                    _hud_check = _summarize(list(messages))
                    _ok = (message_index in _hud_check.message_indices
                           if message_index is not None else True)
                    if not _ok:
                        messages = self.store.get_conversation(session)
                except Exception:
                    messages = self.store.get_conversation(session)
            targets = [m for index, m in enumerate(messages) if m.role == "user" and _one_line(m.text) == body
                       and (message_index is None or index == message_index)]
            if len(targets) != 1:
                raise PromptNavigationError("prompt_ambiguous")
            if token is not None and (message_index is None
                    or prompt_identity(messages, message_index) != token):
                # Stale peek raced a rewrite; retry once with a fresh parse.
                fresh = self.store.get_conversation(session)
                if fresh is not messages:
                    messages = fresh
                    targets = [m for index, m in enumerate(messages) if m.role == "user" and _one_line(m.text) == body
                               and (message_index is None or index == message_index)]
                    if len(targets) != 1:
                        raise PromptNavigationError("prompt_ambiguous")
                    if token is not None and (message_index is None
                            or prompt_identity(messages, message_index) != token):
                        raise PromptNavigationError("prompt_stale")
                else:
                    raise PromptNavigationError("prompt_stale")
            stage = "query"
            from corral.ui.session_hud import summarize_user_messages as _summarize_for_prompts
            from corral.ui.terminal_prompt_navigation import _normalized_texts

            _texts = _normalized_texts(messages)
            try:
                target = prompt_target(messages, targets[0], _texts=_texts)
            except PromptNavigationError:
                # Fullscreen native paths locate by ordinal and context; only
                # the retained-buffer lookup needs a text query.
                target = None
            try:
                _prompts = list(_summarize_for_prompts(list(messages)).message_indices)
            except Exception:
                _prompts = None
            if revision != self._prompt_navigation_revision:
                return
            stage = "buffer"
            offset = (buffer_offset(name, target.query, occurrence=target.occurrence, total=target.total)
                      if target is not None else None)
            if offset is None:
                stage = "native"
                # Escape interrupts a working agent; never use Escape-based
                # native navigation while this session is working or asking.
                try:
                    busy = self.store.attention_for(key).kind in ("working", "waiting")
                except Exception:
                    busy = False  # Screen evidence still gates Escape-based paths.
                navigate_native(name, str(session.get("source") or ""), target.query if target else body,
                                occurrence=target.occurrence if target else 0,
                                total=target.total if target else 1,
                                target=target, messages=messages, selected=targets[0],
                                is_current=lambda: revision == self._prompt_navigation_revision
                                and cell.spec.session_key == key, allow_escape=not busy,
                                prompts=_prompts)
                offset = 0
            stage = "presentation"
            self.app.call_from_thread(self._apply_prompt_jump, key, name, cell, revision, offset)
            self.app.call_from_thread(self._finish_prompt_status, revision, None)
            # Content-free success record, so real-use success rate and latency
            # can be compared with `prompt_navigation_failed`.
            from corral import observe

            observe.event("prompt_navigation_done", runtime=str((session or {}).get("source") or ""),
                          native=offset == 0, duration_ms=round((time.monotonic() - started) * 1000))
        except Exception as exc:
            # Contain internal failures at this optional UI boundary. The clicked
            # row shows a brief reason (owner decision 2026-10-19); never a popup,
            # focus change or input impact. Diagnostics stay content-free.
            from corral import observe

            reason = str(exc) if type(exc).__name__ == "PromptNavigationError" else type(exc).__name__
            observe.event("prompt_navigation_failed", runtime=str((session or {}).get("source") or ""),
                          stage=stage, reason=reason, duration_ms=round((time.monotonic() - started) * 1000))
            try:
                self.app.call_from_thread(self._finish_prompt_status, revision, reason)
            except Exception:
                pass
            return

    def _apply_prompt_jump(self, key: str, name: str, cell, revision: int, offset: int) -> None:
        if revision != self._prompt_navigation_revision or cell.spec.session_key != key:
            return
        pane = cell.embed_pane()
        if pane is None or pane.session_name != name:
            return
        pane.history_offset = offset
        pane._request_immediate_capture()
        self._sync_hud()
