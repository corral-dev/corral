"""Shared modal dismissal: a click on the backdrop cancels, like Escape.

Lives apart from `ui/modals.py` so lower-level widgets (quick filters) can build
dialogs without importing the modal catalogue that depends on them.
"""

from __future__ import annotations

from textual import errors, events
from textual.await_complete import AwaitComplete


class OutsideClickDismiss:
    """点弹窗主体以外的空白处＝取消，和 Esc 等价。

    弹窗铺满整屏、内容居中，中间那块框之外全是背景。鼠标用户对这块区域的直觉
    就是「点一下关掉」；只留 Esc 一条出口，用鼠标操作时会觉得界面卡住了。

    判定必须**现查落点控件**（`get_widget_at`），不能只看事件有没有到这里：
    Click 会从列表项、输入框一路冒泡上来，光凭「收到了」会把点在弹窗内容上的
    每一下都当成点在外面，弹窗一点就关。

    子类用 `outside_click_result` 声明取消时回给调用方的值（默认 `None`，
    确认框这种返回布尔的要改成 `False`）。放在 `ModalScreen` 之前继承。
    """

    outside_click_result = None

    def dismiss(self, result=None) -> AwaitComplete:
        # pop_screen updates the stack before queued input and DOM removal finish.
        # An old modal must never pop the main screen or a newer dialog.
        if not self.is_active:
            return AwaitComplete.nothing()
        return super().dismiss(result)

    def on_click(self, event: events.Click) -> None:
        try:
            hit, _ = self.get_widget_at(event.screen_x, event.screen_y)
        except errors.NoWidget:
            return
        if hit is self:
            event.stop()
            self.dismiss(self.outside_click_result)
