"""Guarded navigation of an existing agent screen; never submits a prompt."""
from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable, Sequence

from corral import embed
from corral.keepalive import tmux_argv_for_session
from corral.terminal_prompt_location import TerminalTextLine, find_prompt_candidates
from corral.textutil import text_width


class PromptNavigationError(RuntimeError):
    pass


def prompt_query(messages: Sequence, target) -> str:
    """Resolve a short, exact query only when it identifies this message alone.

    Check every role, including quotations. Short/repeated queries are refused
    rather than claiming identity from the first visible text occurrence.
    """
    compact = " ".join((target.text or "").split())
    if target.role != "user" or not compact:
        raise PromptNavigationError("prompt_unavailable")
    if any(ord(c) < 32 or ord(c) == 127 for c in compact):
        raise PromptNavigationError("prompt_unavailable")
    texts = [" ".join((m.text or "").split()) for m in messages]
    # Keep the query inside even the minimum supported terminal/search field.
    # Try later fragments when two prompts share an opening sentence.
    for start in range(0, min(len(compact), 512), 8):
        query = ""
        for char in compact[start:]:
            if text_width(query + char) > 24:
                break
            query += char
        query = query.strip()
        if not query or (start > 0 and len(query) < 8):
            continue
        if sum(text.count(query) for text in texts) == 1:
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


def buffer_offset(name: str, query: str) -> int | None:
    """Current tmux text coordinates, re-resolved after capture/resize races."""
    fmt = "#{pane_id}:#{history_size}:#{pane_width}:#{pane_height}:#{alternate_on}"
    before = _tmux(name, "display-message", "-p", "-t", name, fmt).strip()
    _, history, cols, _, alternate = before.split(":")
    # Alternate screens contain only their current viewport, no older history.
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
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    target = origins[candidate.start_line][candidate.start_column]
    if before != _tmux(name, "display-message", "-p", "-t", name, fmt).strip():
        return None
    return 0 if alternate == "1" else max(0, int(history) - target)


def _screen(name: str) -> str:
    return _tmux(name, "capture-pane", "-p", "-t", name)


def _await_screen(name: str, predicate, timeout: float = 1.5) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
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
    if runtime == "pi":
        separators = [i for i, line in enumerate(lines) if re.match(r"^─{10,}", line)]
        return "\n".join(lines[:separators[-2]]) if len(separators) >= 2 else ""
    return screen


def navigate_native(name: str, runtime: str, query: str, *, is_current: Callable[[], bool] = lambda: True) -> None:
    """Use recognized native search surfaces; text is never sent speculatively.

    Keep the search view at the target. The runtime preserves its own composer.
    A custom keymap that does not open the expected search fails before typing.
    """
    with embed.input_transaction(name):
        def check_current():
            if not is_current():
                raise PromptNavigationError("prompt_stale")

        check_current()
        initial = _screen(name)
        if runtime == "codex":
            if "Find:" in initial or "esc cancel" in initial:
                raise PromptNavigationError("prompt_unavailable")
            embed.send_key(name, "F3")
            _await_screen(name, lambda s: "Find:" in s and "full transcript" in s and "esc cancel" in s)
            finish = "Enter"
            def result(s):
                return query in _transcript_area(runtime, s) and "enter accept" in s
        elif runtime == "pi":
            if "Find in transcript" in initial or "Shift+Enter" in initial:
                raise PromptNavigationError("prompt_unavailable")
            embed.send_bytes(name, b"\x1b[102;6u")
            _await_screen(name, lambda s: "Find in transcript" in s and "Shift+Enter" in s)
            finish = "Escape"
            def result(s):
                return "1/1" in s and _transcript_area(runtime, s).count(query) >= 2
        elif runtime == "claude":
            if "Showing detailed transcript" not in initial:
                embed.send_key(name, "C-o")
                _await_screen(name, lambda s: "Showing detailed transcript" in s and "ctrl+o" in s)
            elif "n/N to navigate" not in initial:
                raise PromptNavigationError("prompt_unavailable")
            check_current()
            embed.send_literal(name, "/")
            # A slash must be visibly in the transcript's search field, not
            # merely in the user's draft or a transcript quotation.
            _await_screen(name, lambda s: any(re.match(r"\s*/\s*[█▌_]?\s*$", line) for line in s.splitlines()[-4:]))
            finish = "Enter"
            def result(s):
                return "1/1" in s and query in s
        else:
            raise PromptNavigationError("prompt_unavailable")
        try:
            check_current()
            embed.send_literal(name, query)
            _await_screen(name, result)
            check_current()
            embed.send_key(name, finish)
            # Verify actual content after the search field has closed.
            _await_screen(name, lambda s: query in _transcript_area(runtime, s) and (
                runtime != "codex" or "Find:" not in s))
        except PromptNavigationError:
            # Escape only after our own search surface was recognized. Never
            # interrupt the agent when opening/search recognition failed.
            screen = _screen(name)
            if ((runtime == "codex" and "Find:" in screen and "esc cancel" in screen)
                or (runtime == "pi" and "Shift+Enter" in screen)
                or (runtime == "claude" and "Showing detailed transcript" in screen)):
                embed.send_key(name, "Escape")
            raise
