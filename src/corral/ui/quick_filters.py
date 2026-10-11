"""Fixed, presentation-only runtime/project filters over the unfiltered catalog."""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, SelectionList, Static

from corral.i18n import t
from corral.projects import disambiguate_labels, fuzzy_match, normalize_cwd
from corral.runtime.registry import active_runtimes
from corral.textutil import fit_cell, text_width
from corral.theme import runtime_label_style
from corral.ui.nav import NavState


def project_options(sessions: list[dict]) -> list[tuple[str, str]]:
    """Canonical full-path identities, recent first, with unambiguous labels."""
    latest: dict[str, float] = {}
    for session in sessions:
        path = normalize_cwd(session.get("cwd"))
        latest[path] = max(latest.get(path, 0), session.get("mtime") or 0)
    labels = disambiguate_labels([path for path in latest if path])
    return [(path, labels[path] if path else t("filter.no_project"))
            for path in sorted(latest, key=lambda path: (-latest[path], path))]


class FilterChanged(Message):
    """Selections changed; rebuild rows without following selection into a pane."""


class ProjectFilterModal(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss", "Close")]
    DEFAULT_CSS = """
    ProjectFilterModal { align: center middle; }
    ProjectFilterModal > Vertical {
        width: 64; max-width: 90%; height: auto; max-height: 85%;
        background: $surface; padding: 1 2;
    }
    ProjectFilterModal Label { height: 2; text-style: bold; }
    ProjectFilterModal Input { height: 2; border: none; padding: 0 0 1 0; }
    ProjectFilterModal Input:focus { border: none; }
    ProjectFilterModal SelectionList { height: auto; min-height: 3; max-height: 12; border: none; }
    ProjectFilterModal SelectionList:focus { border: none; }
    ProjectFilterModal Horizontal { height: 1; }
    ProjectFilterModal Button { height: 1; min-width: 8; border: none; }
    """

    def __init__(self, owner: QuickFilters) -> None:
        super().__init__()
        self.owner = owner
        self.options = owner.all_projects()
        self._visible_paths = {path for path, _ in self.options}

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(t("filter.projects"))
            yield Input(placeholder=t("filter.projects_search"), id="projects-query")
            yield SelectionList(*self._selections(""), id="projects-choices")
            with Horizontal():
                yield Button(t("filter.done"), compact=True, id="projects-done")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def _selections(self, query: str) -> list[tuple[Text, str, bool]]:
        visible = [(path, label) for path, label in self.options if fuzzy_match(query, label, path)]
        self._visible_paths = {path for path, _ in visible}
        return [(Text(label), path, path in self.owner.nav.project_filters) for path, label in visible]

    def on_input_changed(self, event: Input.Changed) -> None:
        choices = self.query_one(SelectionList)
        # Replacing visible options must never clear selected hidden projects.
        with choices.prevent(SelectionList.SelectedChanged):
            choices.clear_options()
            choices.add_options(self._selections(event.value))
            choices.highlighted = 0 if choices.option_count else None

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one(SelectionList).focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "down" and self.query_one(Input).has_focus:
            event.stop()
            self.query_one(SelectionList).focus()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        selected = self.owner.nav.project_filters
        next_selected = (selected - self._visible_paths) | set(event.selection_list.selected)
        if next_selected != selected:
            self.owner.nav.project_filters = next_selected
            self.owner.changed()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "projects-done":
            self.dismiss(None)


class QuickFilters(Vertical):
    DEFAULT_CSS = """
    QuickFilters { height: auto; }
    QuickFilters Grid { grid-rows: 1; grid-columns: 1; height: auto; }
    QuickFilters .filter-gap { height: 1; }
    QuickFilters Button.-style-default {
        width: 1fr; min-width: 2; height: 1; padding: 0;
        border: none; margin: 0; background: $surface; color: $text-muted;
        text-style: none; tint: transparent; background-tint: transparent;
    }
    QuickFilters Button.-style-default:hover,
    QuickFilters Button.-style-default:focus,
    QuickFilters Button.-style-default.-active {
        background: $surface; color: $text-muted;
        border: none; tint: transparent; background-tint: transparent;
    }
    QuickFilters Button.-style-default:focus { text-style: none; }
    QuickFilters Button.-chosen,
    QuickFilters Button.-chosen.-style-default:hover,
    QuickFilters Button.-chosen.-style-default:focus,
    QuickFilters Button.-chosen.-style-default.-active {
        background: $primary-muted; color: $warning; text-style: bold;
    }
    QuickFilters Button.-chosen.-style-default:focus { text-style: bold; }
    """

    def __init__(self, store, nav: NavState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.store = store
        self.nav = nav
        self._recent: list[str] = []
        self._project_signature = None
        self._sync_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        with Grid(id="runtime-quick-row"):
            for runtime in active_runtimes(self.store.registry):
                label = "Claude" if runtime.id == "claude" else runtime.display_name
                yield Static("", id=f"runtime-{runtime.id}-gap", classes="filter-gap")
                yield Button(Text(label, style=runtime_label_style(runtime.id)),
                             id=f"runtime-{runtime.id}", compact=True)
        with Grid(id="project-quick-row"):
            yield Static("", id="project-more-gap", classes="filter-gap")
            yield Button(t("filter.more"), id="project-more", compact=True)

    def on_mount(self) -> None:
        self.call_next(self.sync)

    def all_projects(self) -> list[tuple[str, str]]:
        options = project_options(self.store.all_sessions())
        paths = {path for path, _ in options}
        missing = self.nav.project_filters - paths
        labels = disambiguate_labels([path for path in paths | missing if path])
        return [(path, labels.get(path, label)) for path, label in options] + [
            (path, labels.get(path, t("filter.no_project"))) for path in sorted(missing)
        ]

    async def sync(self) -> None:
        async with self._sync_lock:
            await self._sync()

    async def _sync(self) -> None:
        for button in self.query("#runtime-quick-row Button"):
            runtime = button.id.removeprefix("runtime-")
            self._mark(button, runtime in self.nav.runtime_filters)
        options = self.all_projects()
        labels = dict(options)
        # Keep surviving choices in place while filling vacancies from recent data.
        self._recent = [path for path in self._recent if path in labels]
        self._recent += [path for path, _ in options if path not in self._recent][:max(0, 3 - len(self._recent))]
        paths = self._recent
        signature = tuple((path, labels[path]) for path in paths)
        row = self.query_one("#project-quick-row", Grid)
        more = self.query_one("#project-more", Button)
        if signature != self._project_signature:
            await row.query(".project-choice, .project-gap").remove()
            choices = []
            for i, path in enumerate(paths):
                choices.extend([
                    Static("", id=f"project-choice-{i}-gap", classes="filter-gap project-gap"),
                    Button(Text(fit_cell(labels[path], 16, ellipsis=True).rstrip()), name=path,
                           id=f"project-choice-{i}", classes="project-choice",
                           tooltip=path or t("filter.no_project"), compact=True),
                ])
            await row.mount(*choices, before=self.query_one("#project-more-gap"))
            self._project_signature = signature
        count = len(self.nav.project_filters)
        more.label = t("filter.more") + (f" ({count})" if count else "")
        for button in row.query(".project-choice"):
            self._mark(button, button.name in self.nav.project_filters)
        self._pack(self.query_one("#runtime-quick-row", Grid))
        self._pack(row)

    def on_resize(self, event: events.Resize) -> None:
        self.call_next(self.sync)

    @staticmethod
    def _pack(row: Grid) -> None:
        """Pack native grid cells by label width without remounting controls."""
        width = max(1, row.content_size.width)
        row.styles.grid_size_columns = width
        cursor = 0
        lines = 1
        for button in row.query(Button):
            cells = min(width, 10 if button.id == "project-more" else text_width(str(button.label)) + 2)
            gap = row.query_one(f"#{button.id}-gap")
            if cursor == width:
                cursor = 0
                lines += 1
            remainder = width - cursor if cursor and cursor + cells > width else 0
            gap.styles.display = "block" if remainder else "none"
            if remainder:
                gap.styles.column_span = remainder
                gap.styles.width = remainder
                cursor = 0
                lines += 1
            button.styles.column_span = cells
            button.styles.width = cells
            cursor += cells
        row.styles.height = lines + row.styles.padding.bottom

    @staticmethod
    def _mark(button: Button, selected: bool) -> None:
        button.set_class(selected, "-chosen")

    def changed(self) -> None:
        self.call_next(self.sync)
        self.post_message(FilterChanged())

    def on_key(self, event: events.Key) -> None:
        focused = self.screen.focused
        if not isinstance(focused, Button):
            return
        if focused.parent.id == "runtime-quick-row":
            buttons = list(self.query("#runtime-quick-row Button"))
            delta = {"left": -1, "right": 1}.get(event.key)
        elif focused.parent.id == "project-quick-row":
            buttons = list(self.query("#project-quick-row Button"))
            delta = {"left": -1, "right": 1}.get(event.key)
        else:
            return
        if event.key in ("up", "down"):
            direction = -1 if event.key == "up" else 1
            candidates = [b for b in buttons if (b.region.y - focused.region.y) * direction > 0]
            if candidates:
                target = min(candidates, key=lambda b: (abs(b.region.y - focused.region.y),
                                                       abs(b.region.center.x - focused.region.center.x)))
                event.stop()
                target.focus()
        if delta is not None:
            event.stop()
            buttons[(buttons.index(focused) + delta) % len(buttons)].focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button = event.button
        if button.id == "project-more":
            self.app.push_screen(ProjectFilterModal(self))
            return
        if button.id.startswith("runtime-"):
            selected = self.nav.runtime_filters
            value = button.id.removeprefix("runtime-")
        else:
            selected = self.nav.project_filters
            value = button.name
        if value in selected:
            selected.remove(value)
        else:
            selected.add(value)
        self.changed()
