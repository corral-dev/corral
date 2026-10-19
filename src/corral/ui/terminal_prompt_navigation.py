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


def _codex_landing(messages: Sequence, selected) -> list:
    """Consecutive native user cells from the selected one to a pinnable one.

    Codex pins only the prompt preceding the first visible non-user cell, so a
    prompt directly followed by another prompt never gets a header. Land on
    the last prompt of that run, which owns a reply, then reveal upward.
    """
    from corral.ui.session_hud import is_injected_user_prompt

    index = next((i for i, message in enumerate(messages) if message is selected), -1)
    if index < 0:
        raise PromptNavigationError("prompt_stale")
    run = [selected]
    for message in messages[index + 1:]:
        if message.role != "user":
            return run
        if not is_injected_user_prompt(message.text):
            run.append(message)
    # No reply follows the run yet; keep the immediate user-context fallback.
    return [selected]


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


def _codex_reveal_run(name: str, run: list, is_current: Callable[[], bool]) -> None:
    """Scroll the ordinary transcript up from the landed prompt to the selected one.

    Bounded to the run's own short cells; every visible prompt must appear in
    the run's order, so a repeated text elsewhere cannot satisfy the check.
    """
    for _ in range(16 * len(run)):
        if not is_current():
            raise PromptNavigationError("prompt_stale")
        screen = _screen(name)
        if _codex_find(screen):
            raise PromptNavigationError("prompt_unavailable")
        cells = _codex_user_cells(_transcript_area("codex", screen))
        # Later prompts may follow the run on screen, and the run's tail may
        # be below the viewport; compare only the overlapping prefix.
        if (cells and _codex_cell_is(cells[0][1], run[0])
                and all(_codex_cell_is(cell, message)
                        for (_, cell), message in zip(cells, run, strict=False))):
            return
        embed.send_bytes(name, b"\x1b[<64;2;2M")
        _await_screen(name, lambda value, old=screen: value != old, timeout=0.3, is_current=is_current)
    raise PromptNavigationError("prompt_unavailable")


def _codex_direct_anchor(messages: Sequence, selected) -> tuple[str, bool]:
    """An immediate following entry proves the pinned header's user identity."""
    index = next((i for i, message in enumerate(messages) if message is selected), -1)
    if index < 0:
        raise PromptNavigationError("prompt_stale")
    from corral.ui.session_hud import is_injected_user_prompt

    following = []
    for message in messages[index + 1:]:
        # Injected turns (skills, aborts) have no native user cell or reply.
        if message.role == "user" and is_injected_user_prompt(message.text):
            continue
        following.append(message)
        if message.role == "user":
            break
    # The first reply stays near its user header. A later user may be
    # separated by thousands of rendered rows and is not a local anchor.
    for message in following:
        if message.role == "user":
            break
        try:
            return _query_for_text(messages, _codex_plain_segments(message.text), max_width=240), False
        except PromptNavigationError:
            continue
    # Consecutive user messages can have no intervening assistant entry.
    # In that case only use the immediately following native user as context.
    if following and following[-1].role == "user":
        return _query_for_text(messages, following[-1].text), True
    raise PromptNavigationError("prompt_ambiguous")


def _codex_proof_row(lines: list[str], proof: str) -> int | None:
    # Native fullscreen wrapping is performed by the application; tmux -J
    # cannot recover those source lines. Ignore display whitespace for proof.
    needle = "".join(proof.split())
    for row, line in enumerate(lines):
        if needle[:8] in "".join(line.split()) and needle in "".join("".join(lines[row:row + 8]).split()):
            return row
    return None


def _codex_hit_settled(screen: str, proof: str) -> bool:
    """Find holds the complete pasted query and is not scanning.

    The editor may show either end of a long query, or none of it, so compare
    any visible query as a whitespace-free substring. Settling does not prove the hit:
    native hits inside collapsed tool output show no proof on screen.
    """
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    if (len(lines) < 2 or not lines[-2].startswith("Find:") or "Searching" in lines[-1]
            or lines[-1].startswith("Type to find")):
        return False
    # A narrow editor can scroll the pasted query out of its single row.
    shown = "".join(lines[-2].removeprefix("Find:").split()).strip("…")
    return not shown or shown in "".join(proof.split())


def _codex_exhausted(screen: str) -> bool:
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    return bool(lines) and re.search(r"\bNo (more )?matches\b", lines[-1]) is not None


_CODEX_SEARCH_LIMIT = 15.0


def _await_codex_hit(name: str, proof: str, previous: str, is_current: Callable[[], bool]) -> str:
    """Wait for the next settled native hit, observed twice unchanged.

    Two hits in one viewport differ only by highlight, which plain capture
    omits; an unchanged settled screen is accepted after a short grace.
    """
    started = time.monotonic()
    deadline = started + 5.0
    last = None
    while time.monotonic() < deadline:
        if not is_current():
            raise PromptNavigationError("prompt_stale")
        screen = _screen(name)
        # Find scans a bounded chunk per frame, so long tool output after the
        # target takes seconds. Keep waiting only while Codex reports progress.
        if "Searching" in (screen.rstrip().splitlines() or [""])[-1]:
            deadline = min(started + _CODEX_SEARCH_LIMIT, max(deadline, time.monotonic() + 1.0))
        if (screen == last and _codex_hit_settled(screen, proof)
                and (screen != previous or time.monotonic() - started >= 0.4)):
            return screen
        if _codex_exhausted(screen) and screen == last:
            raise PromptNavigationError("prompt_unavailable")
        last = screen
        time.sleep(0.015)
    raise PromptNavigationError("prompt_unavailable")


def _codex_top_user_cell(area: str) -> str | None:
    """Whitespace-free text of the user cell at the top of the transcript area.

    A question-reply cell renders `> question` and its answer on separate
    rows, while the pinned header joins them; compare without whitespace.
    """
    lines = area.splitlines()
    start = next((row for row, line in enumerate(lines) if line.strip()), None)
    if start is None or not lines[start].startswith("› "):
        return None
    cell = [lines[start][2:]]
    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            break
        cell.append(line)
    return "".join("".join(cell).split())


def _codex_label_matches(prefix: str, label: str) -> bool:
    return bool(label and (prefix.startswith(label)
                          or (label.startswith("> ") and label.endswith(" " + prefix))))


def _navigate_codex_direct(name: str, messages: Sequence, selected,
                           is_current: Callable[[], bool]) -> None:
    """Search, then activate Codex's existing pinned prompt header.

    Cancel restores the old viewport; accepting Find retains ownership of
    Enter. The native header is the public interaction that leaves Find and
    jumps to its exact user entry without taking the ordinary composer.
    """
    run = _codex_landing(messages, selected)
    proof, proof_is_user = _codex_direct_anchor(messages, run[-1])
    prefix = " ".join((run[-1].text or "").split())
    if not prefix:
        raise PromptNavigationError("prompt_unavailable")

    def check():
        if not is_current():
            raise PromptNavigationError("prompt_stale")

    initial = _screen(name)
    if _codex_find(initial) or not _transcript_area("codex", initial):
        raise PromptNavigationError("prompt_unavailable")
    try:
        check()
        embed.send_key(name, "F3")
        _await_screen(name, _codex_find, is_current=is_current)
        check()
        # One bracketed paste starts one scan. Typed characters each restart
        # Find, and long histories reload older pages for every restart.
        embed.send_bytes(name, b"\x1b[200~" + proof.encode() + b"\x1b[201~")
        screen = _await_codex_hit(name, proof, "", is_current)
        # Native Find also searches tool output and handoff quotations, which
        # are absent from the normalized conversation used to choose the query.
        # A wrong native hit must advance to the next match, never page forward.
        for _match in range(32):
            for _ in range(max(1, len(screen.splitlines()))):
                check()
                lines = _transcript_area("codex", screen).splitlines()
                header = lines[0].strip() if lines else ""
                label = header.removeprefix("›").strip().removesuffix("…").rstrip()
                proof_row = _codex_proof_row(lines, proof)
                # Native assistant cells start at the transcript gutter. A
                # matching string inside a quoted user/tool cell is not proof.
                native_proof = False
                visible_users = []
                if proof_row is not None:
                    native_proof = (lines[proof_row].startswith("› ") if proof_is_user else
                                    any(lines[row].startswith("• ")
                                        for row in range(max(0, proof_row - 4), proof_row + 1)))
                    visible_users = [line[1:].strip() for line in lines[1:proof_row + (not proof_is_user)]
                                     if line.startswith("› ")]
                visible_target = bool(visible_users and _codex_label_matches(prefix, visible_users[-1]))
                if header.startswith("› ") and label and not _codex_label_matches(prefix, label) and not visible_target:
                    break
                if proof_row is None or not native_proof:
                    break
                if visible_users and not visible_target:
                    break
                body_started = any(line.strip() for line in lines[1:3])
                complete_label = label == prefix or header.endswith("…") or label.startswith("> ")
                if (header.startswith("› ") and _codex_label_matches(prefix, label)
                        and body_started and complete_label):
                    check()
                    embed.send_bytes(name, b"\x1b[<0;2;1M\x1b[<0;2;1m")
                    try:
                        candidate = _await_screen(name, lambda value: not _codex_find(value),
                                                  timeout=0.12, is_current=is_current)
                    except PromptNavigationError as exc:
                        if str(exc) != "prompt_unavailable":
                            raise
                        candidate = _screen(name)
                    if not _codex_find(candidate):
                        # The header and its proof were verified above. Tool
                        # cells can push the reply below the viewport, so the
                        # landing proof is the header's own user cell on top.
                        cell = _codex_top_user_cell(_transcript_area("codex", candidate))
                        if cell and cell.startswith("".join(label.split())):
                            if len(run) > 1:
                                _codex_reveal_run(name, run, is_current)
                            return
                        raise PromptNavigationError("prompt_unavailable")
                    screen = candidate
                check()
                previous = screen
                steps = min(4, max(1, proof_row - 1))
                embed.send_bytes(name, b"\x1b[<65;2;2M" * steps)
                try:
                    screen = _await_screen(name, lambda value, old=previous: value != old,
                                           timeout=0.15, is_current=is_current)
                except PromptNavigationError as exc:
                    if str(exc) != "prompt_unavailable":
                        raise
                    break
            check()
            previous = _screen(name)
            if _codex_exhausted(previous):
                raise PromptNavigationError("prompt_unavailable")
            embed.send_key(name, "C-p")
            screen = _await_codex_hit(name, proof, previous, is_current)
        raise PromptNavigationError("prompt_unavailable")
    finally:
        # Cancel only the search this operation actually opened. Waiting
        # ordinary input owns the lock next, after its editor has been restored.
        if _codex_find(_screen(name)):
            embed.send_key(name, "Escape")


def _navigate_opencode(name: str, messages: Sequence, target, is_current: Callable[[], bool]) -> None:
    """Use the inspected v2 timeline's message-ID selection, without submitting.

    First-message navigation loads all native pages. Timeline rows are newest
    first; without filtering there is no fuzzy-sort ambiguity for repeated text.
    A unique following message proves that the chosen row is the selected turn.
    """
    users = [message for message in messages if message.role == "user" and (message.text or "").strip()]
    index = next((i for i, message in enumerate(users) if message is target), None)
    if index is None:
        raise PromptNavigationError("prompt_unavailable")
    target_index = next(i for i, message in enumerate(messages) if message is target)

    def following_proof(start):
        for message in messages[start:]:
            if message.role == "user":
                break
            if message.role == "assistant" and (message.text or "").strip():
                return _query_for_text(messages, message.text)
        raise PromptNavigationError("prompt_unavailable")

    first_index = next(i for i, message in enumerate(messages) if message is users[0])
    first_proof = following_proof(first_index + 1)
    proof = following_proof(target_index + 1)

    def wait(predicate):
        return _await_screen(name, predicate, timeout=15, is_current=is_current)

    def key(value):
        if not is_current():
            raise PromptNavigationError("prompt_stale")
        embed.send_key(name, value)

    initial = _screen(name)
    if not re.search(r"ctrl\+p commands\s*$", initial, re.MULTILINE):
        raise PromptNavigationError("prompt_unavailable")
    key("C-g")
    wait(lambda s: first_proof in s)
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
        if not is_current():
            raise PromptNavigationError("prompt_stale")
        # Batched public keys keep long timelines practical. Final arrival is
        # verified by the following message, not by assuming key delivery.
        steps = len(users) - 1 - index
        if steps:
            embed.send_key(name, *(["Down"] * steps))
        wait(lambda s: proof in s)
        key("Escape")
        wait(lambda s: re.search(r"Timeline\s+esc", s) is None and proof in s)
    except PromptNavigationError:
        screen = _screen(name)
        if re.search(r"(?:Commands|Timeline)\s+esc", screen):
            embed.send_key(name, "Escape")
        raise


def navigate_native(name: str, runtime: str, query: str, *, occurrence: int = 0, total: int = 1,
                    target: PromptTarget | None = None,
                    messages: Sequence = (), selected=None,
                    is_current: Callable[[], bool] = lambda: True) -> None:
    """Navigate only the existing native viewport; never submit user input."""
    if not query or total < 1 or not 0 <= occurrence < total:
        raise PromptNavigationError("prompt_unavailable")
    # Codex Find scans long tool output between hits; any user input still
    # cancels at once, so its bound only limits an abandoned search.
    with embed.navigation_transaction(name, timeout=_CODEX_SEARCH_LIMIT if runtime == "codex" else 5.0) as owns_input:
        selected_current = is_current
        def operation_current():
            return owns_input() and selected_current()
        is_current = operation_current
        if not is_current():
            raise PromptNavigationError("prompt_stale")
        if runtime == "codex":
            _navigate_codex_direct(name, messages, selected, is_current)
            return
        if runtime == "opencode":
            _navigate_opencode(name, messages, selected, is_current)
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
