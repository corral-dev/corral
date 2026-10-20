# Sidebar resize by drag

## Goal

Let the TUI sidebar width be adjustable by dragging the gap between the
sidebar (`#list-pane`) and the right pane area, as the owner proposed:
insert one dedicated 1-cell vertical strip that serves as the drag handle.
Persist the width alongside the existing sidebar-visible preference.
No keyboard entry point in this slice.

## Current state (verified 2026-10-15)

- `src/corral/ui/main_screen.py` 的 `LIST_PANE_WIDTH` / `SIDEBAR_WIDTH_DEFAULT`（默认 39），经 `MainScreen._apply_sidebar_visible()` 应用。唯一的几何联动是
  `selftest.sh`（期望列由默认宽 + 1 把手列/空隙推导）。
- The 1-cell gap today is `SplitPaneArea` pane-cell left margin
  (`SplitPaneArea.DEFAULT_CSS` / `PaneCell`); there is no widget in the gap.
- Sidebar content already adapts to width dynamically: `src/corral/ui/session_list.py`
  truncates with `max(10, self.size.width or 40)`, `quick_filters._pack`
  uses `content_size.width`, `_FilterInput` refits its placeholder on
  resize. 39 is a tuned default, not a structural requirement.
- Drag primitive: Textual 8.2.8 `Widget.capture_mouse()` routes subsequent
  mouse events to the capturing widget (`Screen._handle_mouse_move` honors
  `app.mouse_captured`; `ScrollBar.action_grab` / `_on_mouse_capture` /
  `_on_mouse_release` is the in-tree reference). Pilot supports the full
  path (`mouse_down` / `hover`-as-move / `mouse_up`, `double_click`).
- Pointer: Textual `VALID_POINTER` includes `ew-resize`; kitty spec
  (sw.kovidgoyal.net/pointer-shapes, 30 names) includes `ew-resize`;
  iTerm2 `PTYSession.screenSetPointerShape:` maps X11 `sb_h_double_arrow`
  to `resizeLeftRightCursor`. `src/corral/ui/pointer_shape.py` already translates
  CSS names to X11 names for iTerm2/real-xterm and appends the tmux DCS
  passthrough copy; it only lacks the resize entry.
- Right-pane cost is already contained: `src/corral/ui/embed_pane.py`
  `_RESIZE_TMUX_DEBOUNCE = 0.2` defers `resize-window` + refetch until the
  size settles, and `render_line` crops to the current panel width, so live
  drag only needs imperative width updates — no extra deferral layer.
- Click-crash guard (`MainScreen.get_widget_and_offset_at`, regression
  `test_mouse_down_on_detached_widget_does_not_crash`) sits at screen
  level and covers any new child automatically.
- `selftest.sh` runs with isolated `HOME=$TMP/home` (hence an isolated
  `~/.cache` layout DB), so the width under test is always the default.
- Tests isolate the layout DB via `CORRAL_CACHE_DIR` + `reset_default_layout_db`
  (`tests/test_ui.py` sidebar-state isolation block); `ui_prefs` round-trip tests already exist.

## Design

### Layout

`MainScreen.compose`, inside the top-level `Horizontal`, when `embed_ok`:

```
Vertical#list-pane (width N) + _SidebarResizer (width 1) + SplitPaneArea (1fr)
```

- The resizer **replaces** the blank margin gap: when the sidebar is
  visible, `SplitPaneArea.styles.margin-left` becomes 0 (handle supplies
  the column); when hidden, handle `display = False` and margin-left
  returns to 1, preserving today's hidden-state look.
- Net geometry at default width is unchanged: right-pane origin stays at
  column 39 + 1, so the IME cursor-anchor math in `selftest.sh` keeps its
  shape (it must read the width constant instead of hardcoding 40).
- No handle when `embed_ok` is False (pure list mode, nothing to resize
  against).

### `_SidebarResizer` behavior

- `Static`, `width: 1`, `height: 1fr`, `ALLOW_SELECT = False`
  (never enters the text-selection crash branch), non-focusable.
- `pointer: ew-resize` in CSS; visually transparent by default (identical
  to today's blank gap, no screenshot churn), subtle highlight on
  `:hover`, stronger highlight while dragging (class-driven).
- `tooltip` with new `sidebar.resize_hint` i18n key.
- `MouseDown` (left button): `stop()` + `prevent_default()`,
  `capture_mouse()`, record `screen_x` + start width. Focus is untouched.
- `MouseMove` while dragging: `new = start_width + (screen_x - start_x)`,
  clamp, apply imperatively to `#list-pane.styles.width`. No persistence
  yet, no tmux traffic beyond the existing embed debounce.
- `MouseUp` / `MouseRelease`: `release_mouse()`, persist the clamped width
  via `ui_prefs.save_sidebar_width`.
- `Click` with `chain == 2` (double-click) and zero drag movement since
  MouseDown: reset to default 39 and persist. A drag ending over the
  handle must not reset: track a `_drag_moved` flag and swallow the
  trailing Click when set.
- Wheel/scroll events are never stopped: the strip is 1 cell wide, but a
  wheel over it should still reach the list.

### Width policy

- `SIDEBAR_WIDTH_DEFAULT = 39` (`LIST_PANE_WIDTH` kept as alias),
  `SIDEBAR_WIDTH_MIN = 28`, `SIDEBAR_WIDTH_MAX = 72`,
  `_SIDEBAR_MIN_RIGHT = 24` (right pane keeps at least 24 columns).
- Effective clamp on every apply:
  `max(MIN, min(MAX, screen_width - 1 - MIN_RIGHT))`.
- Below 28 the `● project title` first line and the fixed-11 `More`
  button degrade; above 72 the sidebar eats hosted panes on common
  120–200 column windows. Representative acceptance widths: 28 / 39 /
  60 / 72.

### Persistence

- New `layout_meta` key `sidebar_width`, mirroring `sidebar_visible`:
  `SidebarLayoutDB.sidebar_width(*, default)` / `set_sidebar_width(int)`,
  same degraded-mode memory fallback, no schema change.
- `ui_prefs.load_sidebar_width` / `save_sidebar_width` (clamp + int()).
- Startup-applied preference, **not** cross-window live synced — same rule
  as `sidebar_visible` (a width change in another window must not yank the
  current one). `LAYOUT_POLL` never touches width.

### Pointer mapping

- Add `"ew-resize": "sb_h_double_arrow"` to `_XTERM_NAMES` in
  `src/corral/ui/pointer_shape.py`. Real xterm also owns `sb_h_double_arrow` in
  cursorfont; kitty/Ghostty/WezTerm keep the CSS name. tmux passthrough
  and exit-reset paths are reused unchanged.

### What deliberately does not change

- No keyboard resize entry (per earlier recommendation; track as
  follow-up if the owner asks).
- No live cross-window width sync.
- iPhone / Mac clients: native split views, no shared code path with the
  TUI strip — that end has no such feature (N/A), no client change.

## Plan

1. Source: resizer widget + compose + visible/width apply paths in
   `src/corral/ui/main_screen.py`; pointer entry; DB + prefs width accessors; i18n key.
2. `selftest.sh`: derive the base column from
   `corral.ui.main_screen.SIDEBAR_WIDTH_DEFAULT` instead of hardcoded 40.
3. Tests (`tests/test_ui.py`, new `SidebarResizeTests` + pointer-mapping
   unit): handle presence/absence per state; Pilot drag
   (`mouse_down`→`hover`→`mouse_up`) changes width; min/max clamp;
   release persists, mount restores; double-click resets; post-drag Click
   does not reset; hidden sidebar hides handle and restores margin;
   hover pointer is `ew-resize`; X11 name maps to `sb_h_double_arrow`.
4. Update the one-cell-gap test for the margin-left 0-visible / 1-hidden
   split, and the `cli/AGENTS.md` fixed-39 hard-convention line.
5. Acceptance: `compileall`, full `scripts/ci-test.py`, `selftest.sh`;
   Pilot/text exports at 28/39/60/72 in dark + light (isolated fixtures,
   no real session content); real-terminal drag + double-click + iTerm2 /
   kitty pointer check; tmux-attached drag check.
6. No commit in this pass: a parallel release verification is running on
   a committed snapshot — ship (version bump + publish) on owner go-ahead.

## Follow-ups (out of scope)

- Keyboard width adjustment.
- Per-project widths (width is global today, like visibility).

<!-- 该文档整理/压缩于 2026-10-20 -->
