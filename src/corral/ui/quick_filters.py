"""Fixed, presentation-only runtime/project filters over the unfiltered catalog."""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.style import Style
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


class FilterChip(Button):
    """Native button behavior with the ToggleButton endcap treatment."""

    COMPONENT_CLASSES = {"filter-chip--fill"}
    DEFAULT_CSS = """
    FilterChip.-style-default {
        width: 1fr; min-width: 2; height: 1; padding: 0;
        border: none; margin: 0; background: transparent; color: $text-muted;
        text-style: none; tint: transparent; background-tint: transparent;
    }
    FilterChip.-style-default:hover,
    FilterChip.-style-default:focus,
    FilterChip.-style-default.-active {
        background: transparent; color: $text-muted; border: none;
        text-style: none; tint: transparent; background-tint: transparent;
    }
    FilterChip .filter-chip--fill { background: $panel; color: $text-muted; }
    FilterChip:hover .filter-chip--fill,
    FilterChip:focus .filter-chip--fill { background: $panel-lighten-1; color: $foreground; }
    FilterChip:light:hover .filter-chip--fill,
    FilterChip:light:focus .filter-chip--fill { background: $panel-darken-1; }
    FilterChip.-chosen .filter-chip--fill {
        background: $primary-muted; color: $warning; text-style: bold;
    }
    FilterChip.-chosen:hover .filter-chip--fill,
    FilterChip.-chosen:focus .filter-chip--fill { background: $primary-muted; background-tint: $foreground 8%; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Textual 8.2's CSS integer parser rejects zero; the public style accepts it.
        self.styles.line_pad = 0

    def render(self) -> Content:
        fill = self.get_visual_style("filter-chip--fill")
        inline = self.has_class("-inline")
        width = max(0, self.size.width - (0 if inline else 2))
        label = self.label.truncate(width, ellipsis=True)
        label = label.pad_right(max(0, width - label.cell_length))
        body = label.stylize_before(fill)
        if inline:
            return body
        if self.app.native_ansi_color or self.app.console.color_system == "standard":
            return body.pad(1, 1).stylize_before(fill)
        side = Style(foreground=fill.background, background=self.background_colors[1])
        return Content.assemble(("▐", side), body, ("▌", side))


class QuickFilters(Vertical):
    DEFAULT_CSS = """
    QuickFilters { height: auto; padding: 0 0 1 0; }
    QuickFilters > Horizontal { height: auto; }
    QuickFilters .filter-dimension {
        width: auto; height: 1; margin-right: 1; color: $text-muted;
    }
    QuickFilters Grid { grid-rows: 1; grid-columns: 1; height: auto; }
    QuickFilters .filter-gap { height: 1; }
    """

    def __init__(self, store, nav: NavState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.store = store
        self.nav = nav
        self._recent: list[str] = []
        self._project_signature = None
        self._sync_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Label(t("filter.agent_label"), classes="filter-dimension")
            with Grid(id="runtime-quick-row"):
                for runtime in active_runtimes(self.store.registry):
                    label = "Claude" if runtime.id == "claude" else runtime.display_name
                    yield Static("", id=f"runtime-{runtime.id}-gap", classes="filter-gap")
                    yield FilterChip(Text(label, style=runtime_label_style(runtime.id)),
                                     id=f"runtime-{runtime.id}", classes="-inline", compact=True)
        with Horizontal():
            yield Label(t("filter.project_label"), classes="filter-dimension")
            with Grid(id="project-quick-row"):
                yield Static("", id="project-more-gap", classes="filter-gap")
                yield FilterChip(t("filter.more"), id="project-more", classes="-inline", compact=True)

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
                    FilterChip(Text(fit_cell(labels[path], 16, ellipsis=True).rstrip()), name=path,
                           id=f"project-choice-{i}", classes="project-choice -inline",
                           tooltip=path or t("filter.no_project"), compact=True),
                ])
            await row.mount(*choices, before=self.query_one("#project-more-gap"))
            self._project_signature = signature
        count = len(self.nav.project_filters)
        more.label = t("filter.more") + ("(99+)" if count > 99 else f" ({count})" if count else "")
        for button in row.query(".project-choice"):
            self._mark(button, button.name in self.nav.project_filters)
        self._pack(self.query_one("#runtime-quick-row", Grid), spacing=1)
        self._pack_projects(row)

    def on_resize(self, event: events.Resize) -> None:
        self.call_next(self.sync)

    @staticmethod
    def _pack_projects(row: Grid) -> None:
        """Keep one project line, reserving the picker before exposing shortcuts."""
        width = max(1, row.content_size.width)
        row.styles.grid_size_columns = width
        row.styles.grid_rows = "1"
        row.styles.height = 1
        more = row.query_one("#project-more", Button)
        more_width = min(width, text_width(t("filter.more") + " (99+)"))
        cursor = 0
        overflow = False
        for button in row.query(".project-choice"):
            cells = text_width(str(button.label))
            gap = row.query_one(f"#{button.id}-gap")
            separation = 1 if cursor else 0
            overflow = overflow or cursor + separation + cells + 1 + more_width > width
            button.display = not overflow
            gap.display = not overflow and bool(separation)
            if not overflow:
                gap.styles.column_span = 1
                button.styles.column_span = cells
                button.styles.width = cells
                cursor += separation + cells
            elif button.has_focus:
                more.focus()
        gap = row.query_one("#project-more-gap")
        gap.display = bool(cursor)
        gap.styles.column_span = 1
        more.styles.column_span = more_width
        more.styles.width = more_width

    @staticmethod
    def _pack(row: Grid, *, spacing: int = 0, row_spacing: int = 0) -> None:
        """Pack native grid cells by label width without remounting controls."""
        width = max(1, row.content_size.width)
        row.styles.grid_size_columns = width
        cursor = 0
        lines = 1
        for button in row.query(Button):
            cells = min(width, text_width(str(button.label)) + (0 if button.has_class("-inline") else 2))
            gap = row.query_one(f"#{button.id}-gap")
            if cursor == width:
                cursor = 0
                lines += 1
            separation = spacing if cursor else 0
            wrapping = cursor and cursor + separation + cells > width
            if wrapping:
                separation = width - cursor
                cursor = 0
                lines += 1
            else:
                cursor += separation
            gap.styles.display = "block" if separation else "none"
            if separation:
                gap.styles.column_span = separation
                gap.styles.width = separation
            button.styles.column_span = cells
            button.styles.width = cells
            cursor += cells
        if lines > 1:
            # Textual 8.2.8 applies grid-gutter's vertical axis to columns. Taller
            # native rows leave an owned blank line without shrinking cell spans.
            row.styles.grid_rows = str(1 + row_spacing)
            row.styles.height = lines + (lines - 1) * row_spacing + row.styles.padding.top + row.styles.padding.bottom
        else:
            # Single line keeps zero blank rows (Quick Start compact layout,
            # owner correction 2026-10-15); the gap row exists only between
            # wrapped lines. Zero spacing values are unchanged either way.
            row.styles.grid_rows = "1"
            row.styles.height = 1 + row.styles.padding.top + row.styles.padding.bottom

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
            buttons = [button for button in self.query("#project-quick-row Button") if button.display]
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
