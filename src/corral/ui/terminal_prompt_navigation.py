"""Guarded navigation of an existing agent screen; never submits a prompt."""
from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from corral import embed
from corral.keepalive import tmux_argv_for_session
from corral.terminal_prompt_location import TerminalTextLine, find_prompt_candidates
from corral.textutil import text_width


class PromptNavigationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PromptTarget:
    query: str
    occurrence: int = 0
    total: int = 1
    anchor: str | None = None
    direction: str = "next"


def _count(text: str, query: str) -> int:
    count, start = 0, 0
    while query and (start := text.find(query, start)) >= 0:
        count += 1
        start += 1
    return count


def prompt_target(messages: Sequence, target) -> PromptTarget:
    """Keep the selected occurrence, including user repeats and quoted replies."""
    try:
        return PromptTarget(prompt_query(messages, target))
    except PromptNavigationError as exc:
        if str(exc) != "prompt_ambiguous":
            raise
    compact = " ".join((target.text or "").split())
    query = ""
    for char in compact:
        if text_width(query + char) > 24:
            break
        query += char
    query = query.strip()
    occurrence = total = 0
    found_target = False
    for message in messages:
        text = " ".join((message.text or "").split())
        count = _count(text, query)
        if message is target:
            occurrence, found_target = total, True
        total += count
    if not found_target or not query or not total:
        raise PromptNavigationError("prompt_unavailable")
    index = next(i for i, message in enumerate(messages) if message is target)
    # A unique adjacent message is a stronger native anchor than cycling a
    # short query. No other user message may lie between anchor and target.
    for direction, neighbors in [("next", reversed(messages[:index])), ("previous", messages[index + 1:])]:
        for neighbor in neighbors:
            if (neighbor.text or "").strip():
                try:
                    anchor = _query_for_text(messages, neighbor.text)
                    return PromptTarget(query, occurrence, total, anchor, direction)
                except PromptNavigationError:
                    pass
            if neighbor.role == "user":
                break
    return PromptTarget(query, occurrence, total)


def prompt_query(messages: Sequence, target) -> str:
    """Resolve a short, exact query only when it identifies this message alone.

    Check every role, including quotations. Short/repeated queries are refused
    rather than claiming identity from the first visible text occurrence.
    """
    if target.role != "user":
        raise PromptNavigationError("prompt_unavailable")
    return _query_for_text(messages, target.text)


def _query_for_text(messages: Sequence, text: str, *, max_width: int = 24) -> str:
    compact = " ".join((text or "").split())
    if not compact:
        raise PromptNavigationError("prompt_unavailable")
    if any(ord(c) < 32 or ord(c) == 127 for c in compact):
        raise PromptNavigationError("prompt_unavailable")
    texts = [" ".join((m.text or "").split()) for m in messages]
    # Native search is line-oriented. Never invent a query by joining two
    # source lines (reply anchors commonly start with a short heading).
    lines = [" ".join(line.split()) for line in (text or "").splitlines() if line.strip()]
    for line in lines[:32]:
        for start in range(0, min(len(line), 512), 8):
            query = ""
            for char in line[start:]:
                if text_width(query + char) > max_width:
                    break
                query += char
            query = query.strip()
            if not query or (start > 0 and len(query) < 8):
                continue
            if sum(_count(text, query) for text in texts) == 1:
                return query
    raise PromptNavigationError("prompt_ambiguous")


def _tmux(name: str, *args: str) -> str:
    try:
        result = subprocess.run(
            [*tmux_argv_for_session(name), *args], capture_output=True,
            text=True, timeout=2, check=True,
        )
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise PromptNavigationError("prompt_unavailable") from exc


def buffer_offset(name: str, query: str, *, occurrence: int = 0, total: int = 1) -> int | None:
    """Current tmux text coordinates, re-resolved after capture/resize races."""
    fmt = "#{pane_id}:#{history_size}:#{pane_width}:#{pane_height}:#{alternate_on}"
    before = _tmux(name, "display-message", "-p", "-t", name, fmt).strip()
    _, history, cols, _, alternate = before.split(":")
    if alternate == "1":
        # Fullscreen programs own message history and the viewport's native
        # pinned prompt header is not the message itself. A local text hit
        # cannot establish that the selected user entry was actually revealed.
        return None
    raw = _tmux(name, "capture-pane", "-p", "-S", "-", "-t", name)
    if before != _tmux(name, "display-message", "-p", "-t", name, fmt).strip():
        return None
    texts = raw.splitlines()
    joined = _tmux(name, "capture-pane", "-p", "-J", "-S", "-", "-t", name).splitlines()
    # tmux supplies the actual wrap boundaries with -J. Reconcile its logical
    # rows with the physical capture before assigning coordinates; never infer
    # wrapping merely because a hard-broken row happens to fill the width.
    origins: list[list[int]] = []
    physical: list[str] = []
    for logical in joined:
        row_map: list[int] = []
        part = ""
        width = 0
        for char in logical:
            cell_width = text_width(char)
            if width + cell_width > int(cols):
                physical.append(part.rstrip())
                part, width = "", 0
            row_map.append(len(physical))
            part += char
            width += cell_width
        physical.append(part.rstrip())
        origins.append(row_map)
    if physical != [line.rstrip() for line in texts]:
        return None
    candidates = find_prompt_candidates([TerminalTextLine(line) for line in joined], query)
    if len(candidates) != total or not 0 <= occurrence < total:
        return None
    candidate = candidates[occurrence]
    target = origins[candidate.start_line][candidate.start_column]
    if before != _tmux(name, "display-message", "-p", "-t", name, fmt).strip():
        return None
    return max(0, int(history) - target)


def _screen(name: str) -> str:
    return _tmux(name, "capture-pane", "-p", "-t", name)


def _await_screen(name: str, predicate, timeout: float = 5.0, *, is_current: Callable[[], bool] = lambda: True) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_current():
            raise PromptNavigationError("prompt_stale")
        text = _screen(name)
        if predicate(text):
            return text
        time.sleep(0.04)
    raise PromptNavigationError("prompt_unavailable")


def _transcript_area(runtime: str, screen: str) -> str:
    lines = screen.splitlines()
    if runtime == "codex":
        # The bottommost chevron is the unsent composer, not a search hit.
        composers = [i for i, line in enumerate(lines) if re.match(r"^\s*›", line)]
        return "\n".join(lines[:composers[-1]]) if composers else ""
    if runtime in {"pi", "claude"}:
        separators = [i for i, line in enumerate(lines) if re.match(r"^─{10,}", line)]
        return "\n".join(lines[:separators[-2]]) if len(separators) >= 2 else ""
    return screen


def _navigate_scrollback(name: str, runtime: str, resolved: PromptTarget, messages: Sequence,
                          is_current: Callable[[], bool]) -> None:
    """Read the native normal viewport without taking the composer's input.

    Accepted Codex Find still consumes Enter, and Claude's detailed transcript
    owns typing. Ordinary page navigation preserves the normal input path.
    Unique adjacent history context disambiguates repeated prompt text.
    """
    def current():
        if not is_current():
            raise PromptNavigationError("prompt_stale")

    def key(value):
        current()
        embed.send_key(name, value)

    screen = _screen(name)
    if runtime == "codex" and _codex_find(screen):
        key("Escape")
        screen = _await_screen(name, lambda s: not _codex_find(s), is_current=is_current)
    elif runtime == "claude" and _claude_transcript(screen):
        key("C-o")
        screen = _await_screen(name, lambda s: not _claude_transcript(s), is_current=is_current)

    if not _transcript_area(runtime, screen):
        raise PromptNavigationError("prompt_unavailable")
    if resolved.total > 1 and resolved.anchor is None:
        raise PromptNavigationError("prompt_ambiguous")
    proof = resolved.query if resolved.total == 1 else resolved.anchor
    # Reading from the beginning holds old content while the live tail changes.
    # No fixed row coordinates are retained across resize or native pagination.
    if resolved.total == 1 and proof in " ".join(_transcript_area(runtime, screen).split()):
        return
    first_proof = None
    for message in messages:
        try:
            first_proof = _query_for_text(messages, message.text)
            break
        except PromptNavigationError:
            pass
    before = screen
    if first_proof is None or first_proof not in " ".join(_transcript_area(runtime, screen).split()):
        key("C-Home")
        screen = _await_screen(name, lambda s: first_proof in " ".join(_transcript_area(runtime, s).split())
                               if first_proof else s != before, is_current=is_current)
    deadline = time.monotonic() + 90
    previous = None
    seen_anchor = False
    direction = "PageDown"

    def arrived(value):
        if runtime == "claude":
            area = _transcript_area(runtime, value)
            lines = area.splitlines()
            row = next((i for i, line in enumerate(lines) if resolved.query in line), 0)
            if row >= max(1, len(lines) // 2):
                key("PageDown")
                _await_screen(name, lambda s: s != value and resolved.query in _transcript_area(runtime, s),
                              is_current=is_current)

    for _ in range(4096):
        current()
        screen = _await_screen(name, lambda s, previous=previous: s != previous, is_current=is_current)
        area = _transcript_area(runtime, screen)
        normalized = " ".join(area.split())
        if proof in normalized:
            if resolved.total == 1:
                arrived(screen)
                return
            seen_anchor = True
            marker = "❯" if runtime == "claude" else "›"
            matches = list(re.finditer(r"(?m)^\s*" + marker + r"\s*" + re.escape(resolved.query) + r"(?:\s|$)", area))
            anchor = area.find(proof)
            if any((match.start() > anchor if resolved.direction == "next" else match.start() < anchor)
                   for match in matches):
                arrived(screen)
                return
            direction = "PageDown" if resolved.direction == "next" else "PageUp"
        elif seen_anchor:
            marker = "❯" if runtime == "claude" else "›"
            if re.search(r"(?m)^\s*" + marker + r"\s*" + re.escape(resolved.query) + r"(?:\s|$)", area):
                arrived(screen)
                return
        if time.monotonic() >= deadline:
            break
        previous = screen
        key(direction)
    raise PromptNavigationError("prompt_unavailable")


def _codex_find(screen: str) -> bool:
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    if not lines:
        return False
    return (bool(re.fullmatch(r"Find ·.*esc latest", lines[-1]))
            or (len(lines) > 1 and lines[-2].startswith("Find:")
                and "full transcript" in lines[-1] and lines[-1].endswith("esc cancel")))


def _claude_transcript(screen: str) -> bool:
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    return bool(lines and lines[-1].startswith("Showing detailed transcript")
                and "ctrl+o" in lines[-1].lower())


def restore_composer(name: str, runtime: str, *, screen: str | None = None) -> bool:
    """A high-level message targets the composer, including legacy Find states."""
    embed.cancel_navigation(name)
    if runtime not in {"codex", "claude"}:
        return False
    with embed.input_transaction(name):
        screen = _screen(name) if screen is None else screen
        changed = False
        if runtime == "codex" and _codex_find(screen):
            changed = True
            embed.send_key(name, "Escape")
            screen = _await_screen(name, lambda s: not _codex_find(s))
        elif runtime == "claude" and _claude_transcript(screen):
            changed = True
            embed.send_key(name, "C-o")
            screen = _await_screen(name, lambda s: not _claude_transcript(s))
        # The normal composer accepts input while reading older output. Its
        # viewport position is not an admission condition, and message/draft
        # text may quote navigation labels indefinitely.
        return changed


def _codex_plain_segments(text: str) -> str:
    """Source segments that Codex renders verbatim, one per line.

    Find matches rendered Markdown, where emphasis, code, link and heading
    syntax is gone. Split at that syntax instead of guessing its rendering.
    """
    segments = []
    for line in (text or "").splitlines():
        line = re.sub(r"^\s*(?:#{1,6}|[-*+]|\d+[.)])\s+", "", line)
        segments.extend(part.strip() for part in re.split(r"[*_`~\[\]()<>|#\\]+", line))
    # Short fragments such as an opening "Yes," recur throughout native output.
    long = [part for part in segments if text_width(part) >= 8]
    return "\n".join(long or [part for part in segments if part])


def _codex_cell_is(cell: str, message) -> bool:
    want = "".join((message.text or "").split())
    return bool(want) and (cell == want or (cell.startswith(">") and cell.endswith(want))
                           or (len(cell) >= 16 and want.startswith(cell)))


def _codex_user_cells(area: str) -> list[tuple[int, str]]:
    cells: list[tuple[int, list[str]]] = []
    current = None
    for row, line in enumerate(area.splitlines()):
        if line.startswith("› "):
            current = [line[2:]]
            cells.append((row, current))
        elif line and not line.startswith(" "):
            current = None
        elif current is not None:
            current.append(line)
    return [(row, "".join("".join(parts).split())) for row, parts in cells]


_ANSI = re.compile(r"\x1b\[[0-9;:?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _visible_all_dim(line: str) -> bool:
    """Use the terminal renderer's SGR parser, including extended colors."""
    cells = embed.parse_screen(line, max(1, text_width(_ANSI.sub("", line))), 1)[0]
    return all(cell.dim for cell in cells if cell.ch.strip())


def _codex_backtrack_ready(styled: str) -> bool:
    """Codex is idle with an empty composer, so Escape cannot interrupt or edit.

    Escape interrupts a running turn, and backtracking requires an empty
    composer. Require positive evidence of both: the composer row shows only
    its dim placeholder, and no spinner or interrupt/queue hint is visible.
    """
    lines = [_ANSI.sub("", line) for line in styled.splitlines()]
    composers = [row for row, line in enumerate(lines) if re.match(r"^\s*›", line)]
    if not composers:
        return False
    row = composers[-1]
    styled_rows = styled.splitlines()
    if not _visible_all_dim(styled_rows[row].split("›", 1)[-1]):
        return False
    # A multi-line draft continues directly below; the status line follows a blank row.
    if row + 1 < len(lines) and lines[row + 1].strip():
        return False
    footer = "\n".join(lines[row + 1:])
    if re.search(r"[\u2800-\u28ff]|interrupt|tab to queue|Find|Browsing|esc again", footer, re.IGNORECASE):
        return False
    # The running status row sits just above the composer: "• Working (12s • esc …)".
    above = "\n".join(lines[max(0, row - 3):row])
    if re.search(r"\(\d+[smh]\b|esc[^\n]*\binterrupt\b|[\u2800-\u28ff]", above, re.IGNORECASE):
        return False
    return not _codex_find("\n".join(lines))


def _codex_footer(screen: str) -> str:
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _await_settled(name: str, is_current: Callable[[], bool], timeout: float = 5.0,
                   before: str | None = None) -> str:
    """Wait for the screen to stop changing, after it first differs from `before`."""
    deadline = time.monotonic() + timeout
    if before is not None:
        try:
            _await_screen(name, lambda value: value != before, timeout=1.0, is_current=is_current)
        except PromptNavigationError as exc:
            if str(exc) != "prompt_unavailable":
                raise
    last, stable = None, 0
    while time.monotonic() < deadline:
        if not is_current():
            raise PromptNavigationError("prompt_stale")
        screen = _screen(name)
        stable = stable + 1 if screen == last else 0
        if stable >= 3:
            return screen
        last = screen
        time.sleep(0.025)
    raise PromptNavigationError("prompt_unavailable")


def _codex_visible_prompts(screen: str, messages: Sequence, prompts: list[int]) -> dict[int, int]:
    """Map native user rows only when their ordered history alignment is unique."""
    area = _transcript_area("codex", screen)
    cells = _codex_user_cells(area)
    if not cells:
        return {}
    starts = [start for start in range(len(prompts) - len(cells) + 1)
              if all(_codex_cell_is(cell, messages[prompts[start + offset]])
                     for offset, (_, cell) in enumerate(cells))]
    if len(starts) > 1:
        lines = area.splitlines()
        proved = []
        for start in starts:
            for offset, (row, _) in enumerate(cells):
                ordinal = start + offset
                end = prompts[ordinal + 1] if ordinal + 1 < len(prompts) else len(messages)
                replies = [message for message in messages[prompts[ordinal] + 1:end]
                           if message.role == "assistant" and message.text.strip()]
                stop_row = cells[offset + 1][0] if offset + 1 < len(cells) else len(lines)
                block = lines[row + 1:stop_row]
                if any((proof := "".join(segment.split())[:40]) and len(proof) >= 8
                       and any(line.startswith("• ") and proof[:8] in "".join(line.split())
                               and proof in "".join("".join(block[at:at + 8]).split())
                               for at, line in enumerate(block))
                       for reply in replies for segment in _codex_plain_segments(reply.text).splitlines()[:1]):
                    proved.append(start)
                    break
        starts = proved or starts
    if len(starts) != 1:
        return {}
    return {row: starts[0] + offset for offset, (row, _) in enumerate(cells)}


def _codex_prompt_row(screen: str, messages: Sequence, prompts: list[int], ordinal: int) -> int | None:
    """Return a requested row only after ruling out other repeated occurrences."""
    return next((row for row, found in _codex_visible_prompts(screen, messages, prompts).items()
                 if found == ordinal), None)


def _codex_bring_to_top(name: str, screen: str, messages: Sequence, prompts: list[int], ordinal: int,
                        is_current: Callable[[], bool]) -> bool:
    """Scroll a visible prompt to the top one transcript row at a time.

    Returns False when the prompt is not provably visible. At the end of the
    conversation the view cannot scroll further; the visible prompt is kept.
    """
    for _ in range(screen.count("\n") + 2):
        row = _codex_prompt_row(screen, messages, prompts, ordinal)
        if row is None:
            return False
        area = _transcript_area("codex", screen).splitlines()
        if row == next((index for index, line in enumerate(area) if line.strip()), row):
            return True
        if not is_current():
            raise PromptNavigationError("prompt_stale")
        embed.send_bytes(name, b"\x1b[<65;2;2M")
        try:
            screen = _await_screen(name, lambda value, old=screen: value != old,
                                   timeout=0.3, is_current=is_current)
        except PromptNavigationError as exc:
            if str(exc) != "prompt_unavailable":
                raise
            return True
    return False


def _codex_close_modes(name: str) -> None:
    """Leave no native browsing, primed Escape or Find behind, then show the latest output.

    Runs before input ownership returns to the user, ignoring cancellation:
    keys already sent may not be drawn yet, so wait until the screen settles.
    """
    deadline = time.monotonic() + 1.5
    last, stable = None, 0
    while time.monotonic() < deadline:
        screen = _screen(name)
        footer = _codex_footer(screen)
        if "Browsing" in footer or _codex_find(screen):
            embed.send_key(name, "Escape")  # Restores the origin; never Enter.
            last, stable = None, 0
        elif "esc again" in footer:
            embed.send_key(name, "C-End")  # Any other key cancels the primed Escape.
            last, stable = None, 0
        else:
            stable = stable + 1 if screen == last else 0
            if stable >= 3:
                break
            last = screen
        time.sleep(0.04)
    embed.send_key(name, "C-End")


def _navigate_codex_backtrack(name: str, messages: Sequence, selected,
                              is_current: Callable[[], bool]) -> bool:
    """Select the prompt by ordinal in Codex's native prompt browsing.

    Browsing steps between rendered user prompts without scanning tool output,
    so its cost does not grow with session size. `q` closes browsing and keeps
    the selected prompt at the top; Escape restores the origin, and Enter would
    rewind, so Enter is never sent. Returns False when preconditions fail
    before any input, so the caller can use ordinary scrolling instead.
    """
    from corral.ui.session_hud import summarize_user_messages

    prompts = list(summarize_user_messages(list(messages)).message_indices)
    index = next((i for i, message in enumerate(messages) if message is selected), -1)
    if index not in prompts:
        return False
    target = prompts.index(index)
    try:
        styled = _tmux(name, "capture-pane", "-p", "-e", "-t", name)
    except PromptNavigationError:
        return False
    if not _codex_backtrack_ready(styled):
        return False

    def check():
        if not is_current():
            raise PromptNavigationError("prompt_stale")

    def step(count: int) -> str:
        before = _screen(name)
        keys = ["Left" if count > 0 else "Right"] * abs(count)
        for start in range(0, len(keys), 64):
            check()
            embed.send_key(name, *keys[start:start + 64])
        return _await_settled(name, is_current, before=before)

    def locate(screen: str) -> int:
        """Prove the top prompt using the same unique alignment as visible shortcuts."""
        area = _transcript_area("codex", screen)
        cells = _codex_user_cells(area)
        lines = area.splitlines()
        first = next((row for row, line in enumerate(lines) if line.strip()), None)
        banner = cells and ">_ OpenAI Codex" in "\n".join(lines[:cells[0][0]]) and not any(
            line.startswith(("• ", "› ")) for line in lines[:cells[0][0]])
        if not cells or (cells[0][0] != first and not banner):
            raise PromptNavigationError("prompt_unavailable")
        found = _codex_visible_prompts(screen, messages, prompts).get(cells[0][0])
        if found is None:
            raise PromptNavigationError("prompt_ambiguous")
        return found

    def visible_row(screen: str, ordinal: int) -> int | None:
        return _codex_prompt_row(screen, messages, prompts, ordinal)

    try:
        check()
        # Load every older page first: selection pauses at the oldest loaded
        # prompt while a page loads and drops the rest of a key burst.
        embed.send_key(name, "C-Home")
        try:
            _await_screen(name, lambda value: "Earlier messages" not in value
                          and "Loading" not in _codex_footer(value), timeout=3.0, is_current=is_current)
        except PromptNavigationError as exc:
            if str(exc) != "prompt_unavailable":
                raise
        check()
        embed.send_key(name, "Escape")
        _await_screen(name, lambda value: "esc again" in _codex_footer(value), timeout=0.8, is_current=is_current)
        check()
        embed.send_key(name, "Escape")
        _await_screen(name, lambda value: "Browsing" in _codex_footer(value), timeout=1.5, is_current=is_current)
        current = len(prompts) - 1
        screen = step(current - target) if current != target else _await_settled(name, is_current)
        for _ in range(3):
            try:
                current = locate(screen)
            except PromptNavigationError as exc:
                # A selected prompt already on screen is not scrolled to the top.
                if str(exc) != "prompt_unavailable" or visible_row(screen, target) is None:
                    raise
                break
            if current == target:
                break
            if current > target:
                # Dropped keys leave the selection newer; older steps scroll it to the top.
                screen = step(current - target)
                continue
            # Newer steps do not scroll a visible prompt to the top. Restart
            # browsing from the latest prompt instead.
            embed.send_key(name, "Escape")
            _await_screen(name, lambda value: "Browsing" not in _codex_footer(value),
                          timeout=1.0, is_current=is_current)
            embed.send_key(name, "Escape")
            _await_screen(name, lambda value: "esc again" in _codex_footer(value), timeout=0.8, is_current=is_current)
            embed.send_key(name, "Escape")
            _await_screen(name, lambda value: "Browsing" in _codex_footer(value), timeout=1.5, is_current=is_current)
            screen = step(len(prompts) - 1 - target)
        else:
            raise PromptNavigationError("prompt_unavailable")
        check()
        embed.send_key(name, "q")
        screen = _await_screen(name, lambda value: "Browsing" not in _codex_footer(value),
                               timeout=1.0, is_current=is_current)
        screen = _await_settled(name, is_current)
        try:
            if locate(screen) == target:
                return True
        except PromptNavigationError as exc:
            if str(exc) != "prompt_unavailable":
                raise
        if _codex_bring_to_top(name, screen, messages, prompts, target, is_current):
            return True
        raise PromptNavigationError("prompt_unavailable")
    except BaseException:
        _codex_close_modes(name)
        raise


def _navigate_codex_direct(name: str, messages: Sequence, selected,
                           is_current: Callable[[], bool], allow_escape: bool = True) -> None:
    """Reach a Codex prompt without opening Find.

    Owner rule (2026-10-19): native Find must never appear. A visible prompt
    needs no input; an idle agent with an empty composer uses prompt browsing;
    otherwise ordinary scrolling and the pinned prompt header are used, which
    leave drafts and running turns untouched.
    """
    from corral.ui.session_hud import summarize_user_messages

    prompts = list(summarize_user_messages(list(messages)).message_indices)
    index = next((i for i, message in enumerate(messages) if message is selected), -1)
    initial = _screen(name)
    if (index in prompts and not _codex_find(initial)
            and _codex_bring_to_top(name, initial, messages, prompts, prompts.index(index), is_current)):
        return
    if allow_escape and _navigate_codex_backtrack(name, messages, selected, is_current):
        return
    _navigate_by_scroll(_SCROLL_PROFILES["codex"], name, messages, selected, is_current)


_OPENCODE_COMPOSER = re.compile(r"^\s*╹▀{8,}\s*$", re.MULTILINE)
_OPENCODE_DIALOG = re.compile(r"^\s*\S.*\s{2,}esc\s*$", re.MULTILINE)


def _opencode_first_line(message) -> str:
    return next(("".join(line.split()) for line in (message.text or "").splitlines() if line.strip()), "")


def _opencode_opening(users: Sequence, message) -> str | None:
    """Shortest first-line opening that no other prompt's first line shares."""
    text = _opencode_first_line(message)
    others = [_opencode_first_line(user) for user in users if user is not message]
    for size in (16, 32, 64):
        opening = text[:size]
        if opening and not any(other.startswith(opening) for other in others):
            return opening
    return None


def _opencode_top(screen: str, rows: int = 8) -> str:
    """Whitespace-free transcript text of the rows where a jumped-to prompt starts.

    Each row keeps only its left column: wide panes put the session sidebar
    to the right after a wide gap, and the tab bar starts with "‹".
    """
    left = [re.split(r"\s{3,}", line.replace("┃", " ").strip())[0]
            for line in screen.splitlines()[:rows] if not line.lstrip().startswith("‹")]
    return "".join("".join(left).split())


def _navigate_opencode(name: str, messages: Sequence, target, is_current: Callable[[], bool]) -> None:
    """Use the inspected v2 timeline's message-ID selection, without submitting.

    First-message navigation loads all native pages. Timeline rows are newest
    first; moving the selection scrolls the selected prompt to the top, and
    Escape keeps that position. The dialog backdrop drops wide characters, so
    arrival is proven only after it closes: a prompt opening no other prompt
    shares at the top, or else the turn's unique reply anywhere on screen.
    """
    users = [message for message in messages if message.role == "user" and (message.text or "").strip()]
    index = next((i for i, message in enumerate(users) if message is target), None)
    if index is None:
        raise PromptNavigationError("prompt_unavailable")
    target_index = next(i for i, message in enumerate(messages) if message is target)
    opening = _opencode_opening(users, target)
    first = _opencode_first_line(users[0])[:16]

    def following_proof():
        for message in messages[target_index + 1:]:
            if message.role == "user":
                break
            if message.role == "assistant" and (message.text or "").strip():
                try:
                    # Replies render as Markdown and wrap at the pane width.
                    return "".join(_query_for_text(messages, _codex_plain_segments(message.text)).split())
                except PromptNavigationError:
                    return None
        return None

    proof = None if opening else following_proof()
    if not opening and not proof:
        raise PromptNavigationError("prompt_ambiguous")

    def arrived(screen):
        if opening:
            return opening in _opencode_top(screen)
        return proof in "".join(screen.split())

    def wait(predicate):
        return _await_screen(name, predicate, timeout=15, is_current=is_current)

    def key(value):
        if not is_current():
            raise PromptNavigationError("prompt_stale")
        embed.send_key(name, value)

    # Narrow panes drop the "ctrl+p commands" hint; the composer's bottom edge
    # stays. Any open dialog (title row ending in "esc") must not be disturbed.
    initial = _screen(name)
    if not _OPENCODE_COMPOSER.search(initial) or _OPENCODE_DIALOG.search(initial):
        raise PromptNavigationError("prompt_unavailable")
    key("C-g")
    wait(lambda s: first in _opencode_top(s))
    key("C-p")
    wait(lambda s: re.search(r"Commands\s+esc", s) is not None and "Search" in s)
    try:
        if not is_current():
            raise PromptNavigationError("prompt_stale")
        embed.send_literal(name, "Jump to message")
        wait(lambda s: "Jump to message" in s and "Session" in s)
        key("Enter")
        wait(lambda s: re.search(r"Timeline\s+esc", s) is not None and "Search" in s)
        key("Home")
        before = _await_settled(name, is_current)
        # Batched public keys keep long timelines practical. Final arrival is
        # verified after the dialog closes, not by assuming key delivery.
        steps = len(users) - 1 - index
        if steps:
            if not is_current():
                raise PromptNavigationError("prompt_stale")
            embed.send_key(name, *(["Down"] * steps))
            _await_settled(name, is_current, before=before)
        key("Escape")
        screen = wait(lambda s: _OPENCODE_DIALOG.search(s) is None)
        if not arrived(screen):
            screen = _await_settled(name, is_current, before=screen)
            if not arrived(screen):
                raise PromptNavigationError("prompt_unavailable")
    except PromptNavigationError:
        screen = _screen(name)
        if re.search(r"(?:Commands|Timeline)\s+esc", screen):
            embed.send_key(name, "Escape")
        raise


_CLAUDE_COMPACT = "This session is being continued from a previous conversation"


@dataclass(frozen=True)
class _ScrollProfile:
    """What the scroll navigator needs to know about one fullscreen runtime."""
    runtime: str
    marker: str        # Prompt row prefix, also used by the pinned header.
    banner: str        # Visible only at the start of the rendered conversation.
    header_column: int  # 1-based SGR column of a click on the pinned header row.


_SCROLL_PROFILES = {
    "claude": _ScrollProfile("claude", "❯ ", "Claude Code v", 3),
    "codex": _ScrollProfile("codex", "› ", ">_ OpenAI Codex", 2),
}


def _scroll_prompt_rows(profile: _ScrollProfile, screen: str) -> list[tuple[int, str]]:
    """Prompt rows of a fullscreen transcript: first line, whitespace-free.

    Only the first line is used: the pinned header is a single row, and the
    indented rows below a prompt can be its command output, not its text.
    """
    return [(row, "".join(line[len(profile.marker):].split()).removesuffix("…"))
            for row, line in enumerate(_transcript_area(profile.runtime, screen).splitlines())
            if line.startswith(profile.marker)]


def _scroll_at_top(profile: _ScrollProfile, screen: str) -> bool:
    """Recognize native startup chrome, never its quotation inside a tool cell."""
    lines = _transcript_area(profile.runtime, screen).splitlines()[:3]
    if profile.runtime == "codex":
        return any(re.match(r"^ {0,2}>_ OpenAI Codex \(v[0-9.]", line) for line in lines)
    return any(re.match(r"^(?: {0,2}|╭─+ )Claude Code v[0-9.]", line) for line in lines)


def _claude_prompt_rows(screen: str) -> list[tuple[int, str]]:
    return _scroll_prompt_rows(_SCROLL_PROFILES["claude"], screen)


def _claude_matches(cell: str, message) -> bool:
    want = "".join((message.text or "").split())
    # Slash commands render as "/command args" while history keeps only the args.
    return bool(cell and want) and (cell == want or (len(cell) >= 4 and want.startswith(cell))
                                    or (cell.startswith("/") and cell.endswith(want)))


def _navigate_claude(name: str, messages: Sequence, selected, is_current: Callable[[], bool]) -> None:
    prompts = [i for i, message in enumerate(messages) if not (message.text or "").startswith(_CLAUDE_COMPACT)]
    _navigate_by_scroll(_SCROLL_PROFILES["claude"], name, messages, selected, is_current, keep=set(prompts))


def _navigate_by_scroll(profile: _ScrollProfile, name: str, messages: Sequence, selected,
                        is_current: Callable[[], bool], keep: set[int] | None = None) -> None:
    """Reach a prompt in a fullscreen view and click its pinned prompt header.

    The top row names the prompt that owns the viewport: either the dim header
    of the latest prompt above the view, or a prompt row itself. Ownership is
    monotonic in scroll position, so a galloping search with PgUp/PgDn bursts
    finds the target's turn; clicking the header lands its prompt at the top.
    No Escape, Enter or transcript mode is used, so drafts and running turns are
    unaffected. Resumed processes omit prompts before a compaction: reaching the
    top without the target reports prompt_absent.
    """
    from corral.ui.session_hud import summarize_user_messages

    if not is_current():
        raise PromptNavigationError("prompt_stale")
    prompts = [i for i in summarize_user_messages(list(messages)).message_indices if keep is None or i in keep]
    index = next((i for i, message in enumerate(messages) if message is selected), -1)
    if index not in prompts:
        raise PromptNavigationError("prompt_unavailable")
    target = prompts.index(index)

    def check():
        if not is_current():
            raise PromptNavigationError("prompt_stale")

    def settle(before: str) -> str:
        """Claude applies key bursts asynchronously and lags on heavy turns.

        Wait up to 3 s for the first change, then for 200 ms without change.
        """
        try:
            screen = _await_screen(name, lambda value: value != before, timeout=3.0, is_current=is_current)
        except PromptNavigationError as exc:
            if str(exc) != "prompt_unavailable":
                raise
            return _screen(name)
        quiet = time.monotonic()
        deadline = quiet + 3.0
        while time.monotonic() < deadline:
            time.sleep(0.03)
            check()
            value = _screen(name)
            if value != screen:
                screen, quiet = value, time.monotonic()
            elif time.monotonic() - quiet >= 0.2:
                break
        return screen

    def at_top(screen: str) -> bool:
        return _scroll_at_top(profile, screen)

    def layout(screen: str, near: int) -> tuple[int | None, dict[int, int]]:
        """The viewport owner and the ordinal of each visible recognised prompt row.

        Row 0 is the pinned header (latest prompt above the view) or a prompt
        row at the top; the recognised rows below it must then be the next
        prompts in order. Rows outside the list (slash commands, attachments)
        are skipped. When the header lags the content and no consistent
        reading exists, the visible rows alone decide.
        """
        rows = _scroll_prompt_rows(profile, screen)
        known = [(row, cell) for row, cell in rows
                 if any(_claude_matches(cell, messages[i]) for i in prompts)]
        below = [(row, cell) for row, cell in known if row > 0]

        def follows(start: int) -> bool:
            return all(start + k < len(prompts) and _claude_matches(cell, messages[prompts[start + k]])
                       for k, (_, cell) in enumerate(below))

        options: list[int] = []
        if known and known[0][0] == 0:
            options = [h for h in range(len(prompts)) if _claude_matches(known[0][1], messages[prompts[h]])
                       and follows(h + 1)]
        if not options and below:
            options = [v - 1 for v in range(len(prompts)) if follows(v)]
        if not options:
            return (-1 if at_top(screen) else None), {}
        own = min(options, key=lambda o: abs(o - near))
        if len(options) > 1:
            return own, {}
        mapping = {row: own + 1 + k for k, (row, _) in enumerate(below)}
        if known and known[0][0] == 0:
            mapping[0] = own
        return own, mapping

    def owner(screen: str, near: int) -> int | None:
        return layout(screen, near)[0]

    def target_row(screen: str, own: int | None) -> int | None:
        _, mapping = layout(screen, target if own is None else own)
        return next((row for row, ordinal in mapping.items() if ordinal == target and row > 0), None)

    def click_header(screen: str) -> str:
        check()
        embed.send_bytes(name, f"\x1b[<0;{profile.header_column};1M\x1b[<0;{profile.header_column};1m".encode())
        clicked = settle(screen)
        if not _scroll_prompt_rows(profile, clicked):
            # Claude can redraw a jumped viewport again once it measures rows.
            clicked = settle(clicked)
        return clicked

    def tick(button: int, screen: str) -> str:
        """One wheel line. Claude accelerates fast spins, so lines are sent singly."""
        check()
        embed.send_bytes(name, f"\x1b[<{button};3;3M".encode())
        try:
            return _await_screen(name, lambda value: value != screen, timeout=0.4, is_current=is_current)
        except PromptNavigationError as exc:
            if str(exc) != "prompt_unavailable":
                raise
            return screen

    clicks = [True]

    def land(screen: str, own: int | None) -> str | None:
        """Put the target at the top: scroll it just above the view, then click its header.

        Returns None when landed, or the current screen when a header click
        jumped elsewhere (Claude can mis-measure compacted transcripts); the
        search then resumes from there without header clicks.
        """
        half = max(1, len(_transcript_area(profile.runtime, screen).splitlines()) // 2)
        for _ in range(4 * half + 8):
            rows = _scroll_prompt_rows(profile, screen)
            top_is_target = bool(rows) and rows[0][0] == 0 and _claude_matches(rows[0][1], messages[prompts[target]])
            if own == target and not clicks[0]:
                # Without header clicks: page up until the target row is visible.
                before = screen
                check()
                embed.send_key(name, "PageUp")
                screen = settle(screen)
                if screen == before:
                    raise PromptNavigationError("prompt_unavailable")
                own = owner(screen, target)
                continue
            if own == target and top_is_target:
                clicked = click_header(screen)
                rows_after = _scroll_prompt_rows(profile, clicked)
                if clicked == screen or (rows_after and rows_after[0][0] == 0
                                         and _claude_matches(rows_after[0][1], messages[prompts[target]])):
                    if profile.runtime == "codex":
                        proven = _codex_visible_prompts(clicked, messages, prompts)
                        if not proven or proven.get(min(proven)) != target:
                            raise PromptNavigationError("prompt_ambiguous")
                    return None  # Landed, or the prompt row itself was already at the top.
                clicks[0] = False
                return clicked
            if own == target:
                # The target is just above the view but the header still names
                # an older prompt; bring the target's own row down to the top.
                screen = tick(64, screen)
                own = owner(screen, target)
                continue
            row = target_row(screen, own)
            if row is None:
                if own is not None and own > target:
                    screen = tick(64, screen)  # Overshot; come back one line.
                    own = owner(screen, target)
                    continue
                if own is not None and own < target:
                    # The header row is drawn over row 0 and can hide the
                    # target there; one more line moves it under the header.
                    before = screen
                    screen = tick(65, screen)
                    if screen == before:
                        raise PromptNavigationError("prompt_unavailable")
                    own = owner(screen, target)
                    continue
                raise PromptNavigationError("prompt_unavailable")
            if not clicks[0] and row <= 1:
                return None  # Top row, or just below the pinned header drawn over row 0.
            before = screen
            if row > half + 1:
                # PgDn moves exactly half the viewport.
                check()
                embed.send_key(name, "PageDown")
                screen = settle(screen)
            else:
                screen = tick(65, screen)
            if screen == before:
                return None  # The conversation end cannot scroll further; the prompt stays visible.
            own = owner(screen, target)
        raise PromptNavigationError("prompt_unavailable")

    initial = _screen(name)
    try:
        _scroll_search(profile, name, messages, prompts, target, is_current, settle, owner, target_row,
                       land, at_top, check)
    except BaseException:
        if _screen(name) == initial:
            raise
        # Return to the latest output; PageDown works with a draft as well.
        for _ in range(12):
            before = _screen(name)
            embed.send_key(name, *["PageDown"] * 20)
            try:
                _await_screen(name, lambda value, old=before: value != old, timeout=0.5)
            except PromptNavigationError:
                break
        raise


def _scroll_search(profile, name, messages, prompts, target, is_current, settle, owner, target_row,
                   land, at_top, check) -> None:
    screen = _screen(name)
    if not _transcript_area(profile.runtime, screen):
        raise PromptNavigationError("prompt_unavailable")
    own = owner(screen, len(prompts) - 1)
    if target_row(screen, own) is not None:
        lost = land(screen, own)
        if lost is None:
            return
        screen = lost
    # Leave live follow so the header appears.
    check()
    embed.send_key(name, "PageUp")
    screen = settle(screen)
    position, own = -1, owner(screen, len(prompts) - 1)
    above = below = None  # Positions known to be past / before the target.
    oldest = own if own is not None and own >= 0 else None
    step = 1
    for _ in range(40):
        if own == target or target_row(screen, own) is not None:
            lost = land(screen, own)
            if lost is None:
                return
            # A header click jumped elsewhere: search again from there.
            screen, position, above, below, step = lost, 0, None, None, 1
            own = owner(screen, target)
            continue
        if own is None:
            # Large tool cells may hide the pinned header for many pages. Keep
            # galloping until known user cells bracket the target.
            delta = -step if below is None else (below - position) // 2 or -1
            if below is None:
                step *= 2
        elif own > target:
            above = position
            delta = -step if below is None else (below - position) // 2 or -1
            if below is None:
                step *= 2
        else:
            below = position
            delta = step if above is None else (above - position) // 2 or 1
            if above is None:
                step *= 2
        if above is not None and below is not None and above - below <= 1:
            # Half a page apart the target row was on screen at `below`; it was
            # not recognised (attachments, commands), so it cannot be verified.
            raise PromptNavigationError("prompt_unavailable")
        check()
        before = screen
        embed.send_key(name, *(["PageUp"] * -delta if delta < 0 else ["PageDown"] * delta))
        screen = settle(before)
        position += delta
        new_owner = owner(screen, target if own is None else own)
        own = new_owner
        if own is not None and own >= 0:
            oldest = own if oldest is None else min(oldest, own)
        if at_top(screen) or (screen == before and delta < 0):
            if own is None or own > target:
                # The oldest rendered prompt is newer than the target, e.g. a
                # resumed process that only shows prompts after a compaction.
                if oldest is None or oldest > target:
                    raise PromptNavigationError("prompt_absent")
                raise PromptNavigationError("prompt_unavailable")
            # Bursts may stop at the top; restart bounds from this known edge.
            position, above, below, step = 0, None, 0, 1
        elif screen == before:
            raise PromptNavigationError("prompt_unavailable")
    raise PromptNavigationError("prompt_unavailable")


_NATIVE_LIMIT = 15.0


def navigate_native(name: str, runtime: str, query: str, *, occurrence: int = 0, total: int = 1,
                    target: PromptTarget | None = None,
                    messages: Sequence = (), selected=None,
                    is_current: Callable[[], bool] = lambda: True, allow_escape: bool = True) -> None:
    """Navigate only the existing native viewport; never submit user input.

    `allow_escape=False` reports that the caller knows the agent is working or
    waiting; Escape-based native paths are then skipped entirely.
    """
    if not query or total < 1 or not 0 <= occurrence < total:
        raise PromptNavigationError("prompt_unavailable")
    # Codex Find scans long tool output between hits; any user input still
    # cancels at once, so its bound only limits an abandoned search.
    with embed.navigation_transaction(name, timeout=_NATIVE_LIMIT if runtime in ("codex", "claude", "opencode")
                                      else 5.0) as owns_input:
        selected_current = is_current
        def operation_current():
            return owns_input() and selected_current()
        is_current = operation_current
        if not is_current():
            raise PromptNavigationError("prompt_stale")
        if runtime == "codex":
            _navigate_codex_direct(name, messages, selected, is_current, allow_escape)
            return
        if runtime == "opencode":
            _navigate_opencode(name, messages, selected, is_current)
            return
        if runtime == "claude":
            _navigate_claude(name, messages, selected, is_current)
            return
        if runtime != "pi":
            raise PromptNavigationError("prompt_unavailable")

        def wait(predicate):
            return _await_screen(name, predicate, is_current=is_current)

        def key(value):
            if not is_current():
                raise PromptNavigationError("prompt_stale")
            embed.send_key(name, value)

        def counter(screen):
            values = re.findall(r"│\s*" + re.escape(query) + r"\s+(\d+)\s*/\s*(\d+)\s*│", screen)
            return tuple(map(int, values[-1])) if values else None

        initial = _screen(name)
        if re.search(r"└[─ ]*.*Shift\+Enter.*┘", initial):
            raise PromptNavigationError("prompt_unavailable")
        embed.send_bytes(name, b"\x1b[102;6u")
        wait(lambda s: "Shift+Enter" in s)
        try:
            key("C-a")
            key("C-k")
            if not is_current():
                raise PromptNavigationError("prompt_stale")
            embed.send_literal(name, query)
            screen = wait(lambda s: counter(s) is not None and query in _transcript_area(runtime, s))
            value = counter(screen)
            if value[1] != total:
                raise PromptNavigationError("prompt_ambiguous")
            for _ in range(total):
                if value[0] == occurrence + 1:
                    break
                key("Enter")
                screen = wait(lambda s, old=value: counter(s) is not None and counter(s) != old)
                value = counter(screen)
                if value[1] != total:
                    raise PromptNavigationError("prompt_stale")
            if value[0] != occurrence + 1:
                raise PromptNavigationError("prompt_unavailable")
            key("Escape")
            wait(lambda s: "Shift+Enter" not in s and query in _transcript_area(runtime, s))
        except PromptNavigationError:
            if "Shift+Enter" in _screen(name):
                embed.send_key(name, "Escape")
            raise
