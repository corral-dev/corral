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


def _query_for_text(messages: Sequence, text: str) -> str:
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
                if text_width(query + char) > 24:
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


def _codex_direct_anchor(messages: Sequence, selected) -> tuple[str, bool]:
    """An immediate following entry proves the pinned header's user identity."""
    index = next((i for i, message in enumerate(messages) if message is selected), -1)
    if index < 0:
        raise PromptNavigationError("prompt_stale")
    following = []
    for message in messages[index + 1:]:
        following.append(message)
        if message.role == "user":
            break
    # User text is rendered literally; prefer it over styled assistant Markdown.
    for message in sorted(following, key=lambda message: message.role != "user"):
        try:
            return _query_for_text(messages, message.text), message.role == "user"
        except PromptNavigationError:
            continue
    raise PromptNavigationError("prompt_ambiguous")


def _navigate_codex_direct(name: str, messages: Sequence, selected,
                           is_current: Callable[[], bool]) -> None:
    """Search, then activate Codex's existing pinned prompt header.

    Cancel restores the old viewport; accepting Find retains ownership of
    Enter. The native header is the public interaction that leaves Find and
    jumps to its exact user entry without taking the ordinary composer.
    """
    proof, next_user = _codex_direct_anchor(messages, selected)
    prefix = " ".join((selected.text or "").split())
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
        embed.send_literal(name, proof)
        screen = _await_screen(name, lambda value: proof in " ".join(_transcript_area("codex", value).split())
                               and "Searching…" not in value.splitlines()[-1], is_current=is_current)
        # Find adds only a small context margin. Move at most one viewport,
        # never traverse the session page by page.
        for _ in range(max(1, len(screen.splitlines()))):
            check()
            lines = _transcript_area("codex", screen).splitlines()
            header = lines[0].strip() if lines else ""
            label = header.removeprefix("›").strip().removesuffix("…").rstrip()
            normalized = [" ".join(line.split()) for line in lines]
            proof_row = next((i for i, line in enumerate(normalized) if proof in line), None)
            before = ([line for line in normalized[1:proof_row + 1] if line.startswith("› ")]
                      if proof_row is not None else [])
            belongs = (len(before) == 1 and proof in before[0]) if next_user else not before
            body_started = any(line.strip() for line in lines[1:3])
            if (header.startswith("› ") and label and prefix.startswith(label)
                    and proof_row is not None and belongs and body_started):
                check()
                # SGR input is delivered to the existing native header, not
                # the system pointer or the message composer. No Enter is sent.
                embed.send_bytes(name, b"\x1b[<0;2;1M\x1b[<0;2;1m")
                try:
                    candidate = _await_screen(name, lambda value: not _codex_find(value),
                                              timeout=0.12, is_current=is_current)
                except PromptNavigationError as exc:
                    if str(exc) != "prompt_unavailable":
                        raise
                    # A real user row can also occupy row zero on a tall
                    # viewport. It has no header action; advance the bounded
                    # search context until the actual pinned header exists.
                    candidate = _screen(name)
                if not _codex_find(candidate):
                    area = _transcript_area("codex", candidate)
                    first = next((line.strip() for line in area.splitlines() if line.strip()), "")
                    if first.startswith("› ") and prefix.startswith(first[1:].strip().removesuffix("…").rstrip()):
                        return
                    raise PromptNavigationError("prompt_unavailable")
                screen = candidate
            check()
            previous = screen
            embed.send_bytes(name, b"\x1b[<65;2;2M")
            screen = _await_screen(name, lambda value, old=previous: value != old, is_current=is_current)
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
    with embed.navigation_transaction(name) as owns_input:
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
