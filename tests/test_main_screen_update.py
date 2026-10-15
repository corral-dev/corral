"""MainScreen ↔ UpdateToast 的接线：后台检查 worker、点击回调、忽略状态持久化。

真实 detect_channel() 在开发树里恒为 "dev"（见 test_updater.py），所以这里全程
mock updater 的对外函数，既不发真实网络请求，也能覆盖"有新版本"这条路径。
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

import corral
from corral import i18n, updater
from corral.ui.app import CorralApp
from corral.ui.update_toast import UpdateToast

i18n.set_lang("en")


def _make_store():
    sessions = [
        {
            "source": "claude", "id": "s0", "short_id": "s0",
            "mtime": time.time(), "size_bytes": 1, "size_kb": 1,
            "native_title": None, "fallback_title": "会话0",
            "cwd": "/tmp", "live": False,
        }
    ]
    claude = mock.Mock()
    claude.id = "claude"
    claude.display_name = "Claude"
    claude.is_available.return_value = True
    claude.scan_sessions.return_value = sessions
    registry = corral.RuntimeRegistry((claude,))
    with mock.patch.object(corral.titles, "load_cache", return_value={}):
        store = corral.SessionStore(limit=20, registry=registry)
        store.load()
    return store


async def _wait_until(predicate, *, tries: int = 100, interval: float = 0.02) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


class UpdateCheckWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_updatable_channel_with_newer_version_reveals_toast(self) -> None:
        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with mock.patch.object(updater, "detect_channel", return_value="pip"), \
             mock.patch.object(updater, "is_updatable", return_value=True), \
             mock.patch.object(updater, "fetch_latest", return_value="9.9.9"), \
             mock.patch.object(updater, "should_prompt", return_value=True), \
             mock.patch.object(updater, "prepare_stage", return_value=None):
            async with app.run_test(size=(100, 30)):
                toast = app.screen.query_one(UpdateToast)
                await _wait_until(lambda: toast.has_class("-visible"))
                body_text = toast.query_one("#toast-body").render().plain
                self.assertIn("9.9.9", body_text)

    async def test_dev_channel_never_shows_toast(self) -> None:
        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with mock.patch.object(updater, "detect_channel", return_value="dev"), \
             mock.patch.object(updater, "fetch_latest") as fetch:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await asyncio.sleep(0.1)
                toast = app.screen.query_one(UpdateToast)
                self.assertFalse(toast.has_class("-visible"))
                fetch.assert_not_called()

    async def test_not_newer_version_never_shows_toast(self) -> None:
        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with mock.patch.object(updater, "detect_channel", return_value="pip"), \
             mock.patch.object(updater, "is_updatable", return_value=True), \
             mock.patch.object(updater, "fetch_latest", return_value="0.1.0"), \
             mock.patch.object(updater, "should_prompt", return_value=False):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await asyncio.sleep(0.1)
                toast = app.screen.query_one(UpdateToast)
                self.assertFalse(toast.has_class("-visible"))

    async def test_click_update_exits_before_installation_starts(self) -> None:
        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with mock.patch.object(updater, "detect_channel", return_value="pip"), \
             mock.patch.object(updater, "is_updatable", return_value=True), \
             mock.patch.object(updater, "fetch_latest", return_value="9.9.9"), \
             mock.patch.object(updater, "should_prompt", return_value=True), \
             mock.patch.object(updater, "prepare_stage", return_value=None), \
             mock.patch.object(updater, "run_update") as run_update:
            async with app.run_test(size=(100, 30)) as pilot:
                toast = app.screen.query_one(UpdateToast)
                await _wait_until(lambda: toast.has_class("-visible"))
                await pilot.click("#toast-body")
                await _wait_until(lambda: app._exit)
        run_update.assert_not_called()
        self.assertIsInstance(app.return_value, updater.RestartRequest)
        self.assertEqual(app.return_value.latest, "9.9.9")
        self.assertEqual(app.return_value.channel, "pip")
        self.assertIsNone(app.return_value.spec)

    async def test_staged_spec_reaches_restart_request(self) -> None:
        import os
        import tempfile

        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with tempfile.TemporaryDirectory() as td:
            staged = os.path.join(td, "corral-9.9.9-py3-none-any.whl")
            with open(staged, "wb") as fh:
                fh.write(b"fake")
            with mock.patch.object(updater, "detect_channel", return_value="pipx"), \
                 mock.patch.object(updater, "is_updatable", return_value=True), \
                 mock.patch.object(updater, "fetch_latest", return_value="9.9.9"), \
                 mock.patch.object(updater, "should_prompt", return_value=True), \
                 mock.patch.object(updater, "prepare_stage", return_value=staged):
                async with app.run_test(size=(100, 30)) as pilot:
                    toast = app.screen.query_one(UpdateToast)
                    await _wait_until(lambda: toast.has_class("-visible"))
                    screen = app.screen
                    await _wait_until(lambda: screen._update_spec == staged)
                    body_text = toast.query_one("#toast-body").render().plain
                    self.assertIn("downloaded", body_text)
                    await pilot.click("#toast-body")
                    await _wait_until(lambda: app._exit)
        self.assertEqual(app.return_value.spec, staged)

    async def test_brew_prewarm_flips_copy_without_spec(self) -> None:
        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with mock.patch.object(updater, "detect_channel", return_value="brew"), \
             mock.patch.object(updater, "is_updatable", return_value=True), \
             mock.patch.object(updater, "fetch_latest", return_value="9.9.9"), \
             mock.patch.object(updater, "should_prompt", return_value=True), \
             mock.patch.object(updater, "prewarm_brew", return_value=True), \
             mock.patch.object(
                 updater, "prepare_stage",
                 side_effect=AssertionError("brew uses prewarm_brew"),
             ):
            async with app.run_test(size=(100, 30)):
                toast = app.screen.query_one(UpdateToast)
                await _wait_until(lambda: toast.has_class("-visible"))
                await _wait_until(
                    lambda: "downloaded" in toast.query_one("#toast-body").render().plain
                )

    async def test_bottom_row_clicks_reach_widgets_below_toast(self) -> None:
        # 回归：浮层可见时，底部左半行点下去必须落在页脚/列表上，而不是浮层容器上
        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with mock.patch.object(updater, "detect_channel", return_value="pip"), \
             mock.patch.object(updater, "is_updatable", return_value=True), \
             mock.patch.object(updater, "fetch_latest", return_value="9.9.9"), \
             mock.patch.object(updater, "should_prompt", return_value=True), \
             mock.patch.object(updater, "prepare_stage", return_value=None):
            async with app.run_test(size=(100, 30)) as pilot:
                toast = app.screen.query_one(UpdateToast)
                await _wait_until(lambda: toast.has_class("-visible"))
                await pilot.pause()
                region = toast.region
                self.assertLess(region.width, 100)
                self.assertGreater(region.x, 0)
                top, _ = app.screen.get_widget_at(2, 29)
                self.assertIsNot(toast, top)
                self.assertNotIn(toast, top.ancestors)
                await pilot.click(None, offset=(2, 29))
                await pilot.pause()
                self.assertFalse(app._exit)

    async def test_dismiss_calls_mark_dismissed_and_hides_toast(self) -> None:
        store = _make_store()
        app = CorralApp(store, embed_ok=False)
        with mock.patch.object(updater, "detect_channel", return_value="pip"), \
             mock.patch.object(updater, "is_updatable", return_value=True), \
             mock.patch.object(updater, "fetch_latest", return_value="9.9.9"), \
             mock.patch.object(updater, "should_prompt", return_value=True), \
             mock.patch.object(updater, "prepare_stage", return_value=None), \
             mock.patch.object(updater, "mark_dismissed") as mark_dismissed:
            async with app.run_test(size=(100, 30)) as pilot:
                toast = app.screen.query_one(UpdateToast)
                await _wait_until(lambda: toast.has_class("-visible"))
                await pilot.click("#toast-close")
                await _wait_until(lambda: not toast.has_class("-visible"))
        mark_dismissed.assert_called_once_with("9.9.9")


if __name__ == "__main__":
    unittest.main()
