"""ui/update_toast.py：右下角新版本浮层的状态机与点击行为。

Pilot 驱动真实 Textual 事件循环（与 test_ui.py 同一模式），不 mock 渲染。
"""

from __future__ import annotations

import os
import unittest

# Before any textual import (isort places textual ahead of first-party corral).
os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")

from textual.app import App, ComposeResult

from corral import i18n
from corral.ui.update_toast import UpdateToast

i18n.set_lang("en")


def _make_app(calls: list):
    class T(App):
        def compose(self) -> ComposeResult:
            yield UpdateToast(
                on_update=lambda: calls.append("update"),
                on_restart=lambda: calls.append("restart"),
                on_retry=lambda: calls.append("retry"),
                on_dismiss=lambda v: calls.append(("dismiss", v)),
                id="update-toast",
            )

    return T()


def _make_aligned_app(calls: list):
    """父级带 `align: right bottom`——复刻 MainScreen 的定位上下文。"""
    class T(App):
        CSS = "Screen { align: right bottom; }"

        def compose(self) -> ComposeResult:
            yield UpdateToast(
                on_update=lambda: calls.append("update"),
                on_restart=lambda: calls.append("restart"),
                on_retry=lambda: calls.append("retry"),
                on_dismiss=lambda v: calls.append(("dismiss", v)),
                id="update-toast",
            )

    return T()


class UpdateToastTests(unittest.IsolatedAsyncioTestCase):
    async def test_starts_hidden(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)):
            toast = app.query_one(UpdateToast)
            self.assertFalse(toast.has_class("-visible"))

    async def test_show_available_reveals_toast_with_version_text(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_available("0.21.0")
            await pilot.pause()
            self.assertTrue(toast.has_class("-visible"))
            body_text = toast.query_one("#toast-body").render().plain
            self.assertIn("0.21.0", body_text)

    async def test_click_body_in_available_state_triggers_update(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_available("0.21.0")
            await pilot.pause()
            await pilot.click("#toast-body")
            await pilot.pause()
            self.assertEqual(calls, ["update"])

    async def test_click_close_in_available_state_dismisses_with_version(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_available("0.22.0")
            await pilot.pause()
            await pilot.click("#toast-close")
            await pilot.pause()
            self.assertEqual(calls, [("dismiss", "0.22.0")])

    async def test_close_hitbox_hidden_only_while_updating(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_updating()
            await pilot.pause()
            close = toast.query_one("#toast-close")
            self.assertFalse(close.display)

    async def test_failed_state_can_be_closed(self) -> None:
        # 真机事故复现：升级失败后浮层关不掉，一直横在界面底部
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_available("0.22.0")
            await pilot.pause()
            toast.show_failed("boom")
            await pilot.pause()
            self.assertTrue(toast.query_one("#toast-close").display)
            await pilot.click("#toast-close")
            await pilot.pause()
            self.assertEqual(calls, [("dismiss", "0.22.0")])

    async def test_done_state_can_be_closed_without_restarting(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_done("0.22.0")
            await pilot.pause()
            self.assertTrue(toast.query_one("#toast-close").display)
            await pilot.click("#toast-close")
            await pilot.pause()
            self.assertEqual(calls, [])
            self.assertFalse(toast.has_class("-visible"))

    async def test_failed_state_shows_reason_summary(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_failed("Downloading...\n/path/python: No module named pip")
            await pilot.pause()
            body_text = toast.query_one("#toast-body").render().plain
            self.assertIn("No module named pip", body_text)

    async def test_click_body_while_done_triggers_restart(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_done("0.21.0")
            await pilot.pause()
            await pilot.click("#toast-body")
            await pilot.pause()
            self.assertEqual(calls, ["restart"])

    async def test_click_body_while_failed_triggers_retry(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_failed()
            await pilot.pause()
            await pilot.click("#toast-body")
            await pilot.pause()
            self.assertEqual(calls, ["retry"])

    async def test_click_body_while_updating_is_a_no_op(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_updating()
            await pilot.pause()
            await pilot.click("#toast-body")
            await pilot.pause()
            self.assertEqual(calls, [])

    async def test_hide_returns_to_hidden_state(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_available("0.21.0")
            await pilot.pause()
            toast.hide()
            await pilot.pause()
            self.assertFalse(toast.has_class("-visible"))

    async def test_show_ready_swaps_copy_to_ready(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_available("0.21.0")
            await pilot.pause()
            self.assertNotIn("downloaded", toast.query_one("#toast-body").render().plain)
            toast.show_ready("0.21.0")
            await pilot.pause()
            body_text = toast.query_one("#toast-body").render().plain
            self.assertIn("0.21.0", body_text)
            self.assertIn("downloaded", body_text)
            # ready 不改变点击语义：还是更新
            await pilot.click("#toast-body")
            await pilot.pause()
            self.assertEqual(calls, ["update"])

    async def test_show_ready_noop_when_hidden(self) -> None:
        # 用户已忽略后再备好也不许把浮层顶出来
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_ready("0.21.0")
            await pilot.pause()
            self.assertFalse(toast.has_class("-visible"))

    async def test_show_ready_ignores_stale_version(self) -> None:
        calls: list = []
        app = _make_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_available("0.21.0")
            await pilot.pause()
            toast.show_ready("0.22.0")
            await pilot.pause()
            self.assertNotIn("downloaded", toast.query_one("#toast-body").render().plain)

    async def test_toast_only_covers_bottom_right_content_box(self) -> None:
        # 回归：浮层只能占右下角内容宽，底部其余整行必须能点到下面的控件
        calls: list = []
        app = _make_aligned_app(calls)
        async with app.run_test(size=(80, 24)) as pilot:
            toast = app.query_one(UpdateToast)
            toast.show_available("0.21.0")
            await pilot.pause()
            region = toast.region
            self.assertGreater(region.x, 0)
            self.assertLess(region.right, 80)
            # 底部留 1 行 margin 间隙（右下但不贴边），不是贴着底
            self.assertEqual(region.bottom, 23)
            self.assertLess(region.width, 80)
            bottom_widget, _ = app.screen.get_widget_at(2, 23)
            self.assertIsNot(toast, bottom_widget)
            self.assertNotIn(toast, bottom_widget.ancestors)
            top_widget, _ = app.screen.get_widget_at(region.x + 1, region.y)
            self.assertTrue(top_widget is toast or toast in top_widget.ancestors)


if __name__ == "__main__":
    unittest.main()
