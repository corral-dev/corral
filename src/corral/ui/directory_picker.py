"""Desktop-only directory selection for New Session."""

from pathlib import Path

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Input, Label

from corral.i18n import t
from corral.projects import resolve_directory
from corral.ui.dismiss import OutsideClickDismiss


class _Directories(DirectoryTree):
    # Terminal cells, not emoji: the screenshot font and many terminals draw
    # Textual's 📁/📂 as tofu.
    ICON_NODE = "▶ "
    ICON_NODE_EXPANDED = "▼ "
    ICON_FILE = "  "

    def filter_paths(self, paths):
        return (path for path in paths if path.is_dir())


class DirectoryPickerModal(OutsideClickDismiss, ModalScreen[str | None]):
    DEFAULT_CSS = """
    DirectoryPickerModal { align: center middle; }
    DirectoryPickerModal > Vertical {
        width: 64; max-width: 94%; height: 18; max-height: 86%;
        border: round $primary; background: $surface; padding: 0 1;
    }
    DirectoryPickerModal Input, DirectoryPickerModal Input:focus {
        height: 1; border: none; padding: 0 1; background: $panel;
    }
    DirectoryPickerModal DirectoryTree { height: 1fr; }
    DirectoryPickerModal #directory-error { height: auto; max-height: 2; color: $error; }
    DirectoryPickerModal Horizontal { height: 1; align-horizontal: right; }
    DirectoryPickerModal Button { height: 1; min-width: 10; border: none; margin-left: 2; }
    """

    def __init__(self, initial: str) -> None:
        super().__init__()
        self._initial = initial if initial and Path(initial).is_dir() else str(Path.home())

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(t("modal.open_directory"))
            yield Input(value=self._initial, id="directory-path", select_on_focus=False)
            yield _Directories(self._initial, id="directory-tree")
            yield Label("", id="directory-error")
            with Horizontal():
                yield Button(t("modal.cancel"), id="directory-cancel", compact=True)
                yield Button(t("modal.open_directory"), id="directory-open", compact=True)

    def on_mount(self) -> None:
        self.set_focus(self.query_one(Input))
        self.query_one(Input).cursor_position = len(self._initial)

    def on_directory_tree_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        event.stop()
        self.query_one(Input).value = str(event.path)
        self.query_one("#directory-error", Label).update("")

    def _choose(self) -> None:
        try:
            path = resolve_directory(self.query_one(Input).value)
        except ValueError as exc:
            self.query_one("#directory-error", Label).update(str(exc))
            self.query_one(Input).focus()
            return
        self.dismiss(path)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._choose()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "directory-cancel":
            self.dismiss(None)
        else:
            self._choose()

    def _on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.dismiss(None)
