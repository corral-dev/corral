"""Sidebar filter row: Agent and Project pickers plus the Active checkbox.

One row at the default 39-cell width (owner redesign 2026-10-25). Filters are
presentation-only over the unfiltered catalog and use neutral colors; brand
colors appear only inside the Agent picker, where they identify assistants.
"""

from __future__ import annotations

import asyncio

from rich.segment import Segment
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.style import Style
from textual.widgets import Button, Checkbox, Input, SelectionList, Static
from textual.widgets._toggle_button import ToggleButton

from corral.i18n import t
from corral.projects import disambiguate_labels, fuzzy_match, normalize_cwd
from corral.runtime.registry import active_runtimes
from corral.textutil import fit_cell, text_width
from corral.theme import runtime_label_style
from corral.ui.dismiss import OutsideClickDismiss
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


class _PickerSelectionList(SelectionList[str]):
    """SelectionList with a check mark for selected rows.

    Textual's native inner glyph is ``X``, which reads as "remove" in a filter
    menu. Replace it with ``✓`` for selected rows and leave unselected rows
    unmarked; the row fill stays as the highlight.
    """

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        _, scroll_y = self.scroll_offset
        index = scroll_y + y
        if not 0 <= index < self.option_count:
            return strip
        selected = self.get_option_at_index(index).value in self.selected
        for offset, segment in enumerate(strip):
            if segment.text == ToggleButton.BUTTON_INNER:
                segments = list(strip)
                segments[offset] = Segment("✓" if selected else " ", style=segment.style)
                return Strip(segments, strip.cell_length)
        return strip


class FilterPickerModal(OutsideClickDismiss, ModalScreen[None]):
    """In-place multi-select dropdown for one filter dimension (Agent or Project).

    Opens directly below the clicked chip (owner correction 2026-10-25: a centered
    dialog made the pointer travel), over an undimmed screen. Selections apply
    immediately; Clear empties the dimension (= All); Done, Escape or a click
    outside closes. Project lists are searchable; selections hidden by the search
    are kept.
    """

    BINDINGS = [("escape", "dismiss", "Close")]
    DEFAULT_CSS = """
    FilterPickerModal { align: left top; background: transparent; }
    FilterPickerModal > Vertical {
        width: auto; min-width: 24; height: auto; max-height: 80%;
        background: $surface; padding: 0 1;
    }
    FilterPickerModal Input { height: 1; width: 1fr; border: none; padding: 0; }
    FilterPickerModal Input:focus { border: none; }
    FilterPickerModal SelectionList {
        height: auto; max-height: 12; width: auto; min-width: 22;
        border: none; padding: 0;
    }
    FilterPickerModal SelectionList:focus { border: none; }
    FilterPickerModal #picker-box.-searchable { width: 40; }
    /* Selection is shown by a ✓ in the built-in button (unselected rows leave
       the centre blank); never use Textual's X, which reads as "remove". */
    FilterPickerModal SelectionList > .selection-list--button,
    FilterPickerModal SelectionList > .selection-list--button-highlighted {
        color: $panel;
    }
    FilterPickerModal SelectionList > .selection-list--button-selected,
    FilterPickerModal SelectionList > .selection-list--button-selected-highlighted {
        color: $text-success;
    }
    FilterPickerModal Horizontal { height: 1; width: auto; }
    FilterPickerModal Button { height: 1; min-width: 6; border: none; margin-right: 2; }
    """

    def __init__(self, owner: QuickFilters, dimension: str, anchor=None) -> None:
        super().__init__()
        self.owner = owner
        self.dimension = dimension
        self.searchable = dimension == "project"
        self.options = owner.options(dimension)
        self._visible_values = {value for value, _ in self.options}
        # Screen region of the chip that opened the dropdown.
        self._anchor = anchor

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box", classes="-searchable" if self.searchable else ""):
            if self.searchable:
                yield Input(placeholder=t("filter.projects_search"), id="picker-query")
            yield _PickerSelectionList(*self._selections(""), id="picker-choices")
            with Horizontal():
                yield Button(t("filter.done"), compact=True, id="picker-done")
                yield Button(t("filter.clear_all"), compact=True, id="picker-clear")

    def on_mount(self) -> None:
        if self.searchable:
            self.query_one(Input).focus()
        else:
            self.query_one(SelectionList).focus()
        # Place before the first paint: a call_after_refresh-only placement
        # would show the box at the top-left and then visibly jump under the
        # chip (owner report 2026-10-25). The refresh pass below only corrects
        # clamping when the box would overflow the screen.
        self._place()
        self.call_after_refresh(self._place)

    def _place(self) -> None:
        """Pin the box under the anchor chip, shifted left/up only to stay on screen."""
        if self._anchor is None:
            return
        box = self.query_one("#picker-box")
        width, height = box.outer_size
        # Before layout, use the known CSS width so the first frame is already
        # horizontally anchored; height is only needed for the bottom clamp.
        if not width:
            width = 40 if self.searchable else 24
        screen_w, screen_h = self.size
        x = max(0, min(self._anchor.x, screen_w - width))
        y = self._anchor.bottom
        if height and y + height > screen_h:
            y = max(0, self._anchor.y - height)
        box.styles.offset = (x, y)

    def _selected(self) -> set[str]:
        return self.owner.selected(self.dimension)

    def _selections(self, query: str) -> list[tuple[Text, str, bool]]:
        visible = [(value, label) for value, label in self.options
                   if not self.searchable or fuzzy_match(query, str(label), value)]
        self._visible_values = {value for value, _ in visible}
        selected = self._selected()
        return [(label if isinstance(label, Text) else Text(label), value, value in selected)
                for value, label in visible]

    def _reload(self, query: str) -> None:
        choices = self.query_one(SelectionList)
        # Replacing visible options must never clear selected hidden values.
        with choices.prevent(SelectionList.SelectedChanged):
            choices.clear_options()
            choices.add_options(self._selections(query))
            choices.highlighted = 0 if choices.option_count else None

    def on_input_changed(self, event: Input.Changed) -> None:
        self._reload(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one(SelectionList).focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "down" and self.searchable and self.query_one(Input).has_focus:
            event.stop()
            self.query_one(SelectionList).focus()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        selected = self._selected()
        next_selected = (selected - self._visible_values) | set(event.selection_list.selected)
        if next_selected != selected:
            self.owner.set_selected(self.dimension, next_selected)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "picker-done":
            self.dismiss(None)
        elif event.button.id == "picker-clear":
            self.owner.set_selected(self.dimension, set())
            query = self.query_one(Input).value if self.searchable else ""
            self._reload(query)


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
        background: $primary-muted; color: $foreground; text-style: bold;
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


class QuickFilters(Horizontal):
    """`Agent: All ▾  Project: All ▾  [ ] Active` plus its owned blank row."""

    DEFAULT_CSS = """
    /* Chips start at the left edge like the cards; the checkbox's trailing cell
       is the right inset. At 39 cells: 12 + gap + 14 + gap + 11 fit exactly. */
    QuickFilters { height: 2; width: 1fr; padding: 0; }
    QuickFilters > FilterChip { width: auto; }
    QuickFilters > .filter-gap { width: 1; height: 1; }
    QuickFilters > Checkbox {
        dock: right; height: 1; width: auto; margin: 0; padding: 0;
        background: transparent; border: none;
    }
    QuickFilters > Checkbox:focus { border: none; background-tint: transparent; }
    QuickFilters > Checkbox > .toggle--label { color: $text-muted; }
    QuickFilters > Checkbox:hover > .toggle--label,
    QuickFilters > Checkbox:focus > .toggle--label {
        color: $foreground; background: $panel-lighten-1; text-style: none;
    }
    QuickFilters > Checkbox:light:hover > .toggle--label,
    QuickFilters > Checkbox:light:focus > .toggle--label { background: $panel-darken-1; }
    QuickFilters > Checkbox.-on > .toggle--label { color: $foreground; text-style: bold; }
    /* 未勾选时框内不画字（Textual 默认画一个淡 X，终端里容易被看成已勾选）。 */
    QuickFilters > Checkbox > .toggle--button { color: $panel; }
    QuickFilters > Checkbox.-on > .toggle--button { color: $foreground; }
    """

    def __init__(self, store, nav: NavState, **kwargs) -> None:
        super().__init__(**kwargs)
        self.store = store
        self.nav = nav
        self._sync_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield FilterChip("", id="agent-filter", classes="-inline", compact=True)
        yield Static("", classes="filter-gap")
        yield FilterChip("", id="project-filter", classes="-inline", compact=True)
        checkbox = Checkbox(t("filter.active"), self.nav.active_only,
                            id="active-filter", compact=True)
        yield checkbox

    def on_mount(self) -> None:
        self.call_next(self.sync)

    # ---- dimension data ----

    def all_projects(self) -> list[tuple[str, str]]:
        options = project_options(self.store.all_sessions())
        paths = {path for path, _ in options}
        missing = self.nav.project_filters - paths
        labels = disambiguate_labels([path for path in paths | missing if path])
        return [(path, labels.get(path, label)) for path, label in options] + [
            (path, labels.get(path, t("filter.no_project"))) for path in sorted(missing)
        ]

    def _runtime_names(self) -> list[tuple[str, str]]:
        return [(runtime.id, "Claude" if runtime.id == "claude" else runtime.display_name)
                for runtime in active_runtimes(self.store.registry)]

    def options(self, dimension: str) -> list[tuple[str, Text | str]]:
        if dimension == "agent":
            # Brand color identifies the assistant inside its own picker.
            return [(rid, Text(name, style=runtime_label_style(rid)))
                    for rid, name in self._runtime_names()]
        return self.all_projects()

    def selected(self, dimension: str) -> set[str]:
        return self.nav.runtime_filters if dimension == "agent" else self.nav.project_filters

    def set_selected(self, dimension: str, values: set[str]) -> None:
        if dimension == "agent":
            self.nav.runtime_filters = set(values)
        else:
            self.nav.project_filters = set(values)
        self.changed()

    def _value_text(self, dimension: str) -> str:
        selected = self.selected(dimension)
        if not selected:
            return t("filter.all")
        names = self._runtime_names() if dimension == "agent" else self.all_projects()
        chosen = [label for value, label in names if value in selected]
        if not chosen:
            return t("filter.all")
        if len(chosen) == 1:
            return chosen[0]
        return t("filter.more_selected", name=chosen[0], count=len(chosen) - 1)

    def _chip_label(self, dimension: str, width: int | None = None) -> Text:
        """Dimension + value + dropdown arrow, fitting ``width`` exactly.

        The arrow must survive truncation: it is the only affordance that says
        the chip opens a picker. At a tight sidebar the value (and, below the
        default width, the dimension prefix) gives way first.
        """
        key = "filter.agent_label" if dimension == "agent" else "filter.project_label"
        prefix = t(key) + t("filter.dimension_sep")
        suffix = " ▾"
        value = self._value_text(dimension)
        if width is not None:
            budget = max(0, width - text_width(prefix) - text_width(suffix))
            # If the dimension prefix would leave only dots for the value, drop
            # the prefix instead: the fixed chip order still identifies it, and
            # the real value is more useful than "Agent: ...".
            if text_width(value) > budget and budget < 4:
                prefix = ""
                budget = max(0, width - text_width(suffix))
            value = fit_cell(value, budget, ellipsis=True).rstrip() if budget else ""
        label = Text()
        label.append(prefix)
        label.append(value)
        label.append(suffix)
        return label

    # ---- layout ----

    async def sync(self) -> None:
        async with self._sync_lock:
            self._sync()

    def _sync(self) -> None:
        if not self.is_mounted:
            return
        agent = self.query_one("#agent-filter", FilterChip)
        project = self.query_one("#project-filter", FilterChip)
        agent.label = self._chip_label("agent")
        project.label = self._chip_label("project")
        self._mark(agent, bool(self.nav.runtime_filters))
        self._mark(project, bool(self.nav.project_filters))
        checkbox = self.query_one("#active-filter", Checkbox)
        if checkbox.value != self.nav.active_only:
            with checkbox.prevent(Checkbox.Changed):
                checkbox.value = self.nav.active_only
        self._layout(agent, project, checkbox)

    def _layout(self, agent: FilterChip, project: FilterChip, checkbox: Checkbox) -> None:
        """Fit both pickers before the checkbox; long values truncate, never wrap."""
        width = self.content_size.width
        if width <= 0:
            return
        # Native Checkbox renders "▐X▌ " + label + one trailing cell.
        check_width = 5 + text_width(t("filter.active"))
        # Gaps: between the two pickers and before the checkbox (breathing room).
        room = max(2, width - check_width - 2)
        natural_agent = text_width(str(agent.label))
        natural_project = text_width(str(project.label))
        agent_selected = bool(self.nav.runtime_filters)
        project_selected = bool(self.nav.project_filters)
        if natural_agent + natural_project <= room:
            agent_width, project_width = natural_agent, natural_project
        elif not agent_selected and not project_selected:
            # Both say All but the row is extremely narrow: split the room.
            agent_width = max(1, room // 2)
            project_width = max(1, room - agent_width)
        else:
            # Keep the unfiltered dimension readable ("Project: All ▾") and
            # truncate the active filter's value; its arrow still survives.
            if agent_selected and project_selected:
                agent_width = max(1, min(natural_agent, room // 2))
                project_width = max(1, room - agent_width)
            elif project_selected:
                project_width = max(1, room - natural_agent)
                agent_width = max(1, room - project_width)
            else:
                project_width = min(natural_project, max(1, room - 1))
                agent_width = max(1, room - project_width)
        agent.styles.width = max(1, agent_width)
        project.styles.width = max(1, project_width)
        # Rebuild labels against the final widths so the dropdown arrow is never
        # the part that gets truncated.
        agent.label = self._chip_label("agent", max(1, agent_width))
        project.label = self._chip_label("project", max(1, project_width))

    def on_resize(self, event: events.Resize) -> None:
        self.call_next(self.sync)

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
        delta = {"left": -1, "right": 1}.get(event.key)
        focused = self.screen.focused
        if delta is None or focused is None or focused.parent is not self:
            return
        controls = [self.query_one("#agent-filter"), self.query_one("#project-filter"),
                    self.query_one("#active-filter")]
        if focused in controls:
            event.stop()
            controls[(controls.index(focused) + delta) % len(controls)].focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        dimension = "agent" if event.button.id == "agent-filter" else "project"
        self.app.push_screen(FilterPickerModal(self, dimension, anchor=event.button.region))

    async def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        event.stop()
        if event.value != self.nav.active_only:
            await self.screen._toggle_active_filter()
