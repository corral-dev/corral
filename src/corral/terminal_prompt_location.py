"""Read-only terminal text location kernel for Your-prompts navigation.

Pure text-matching utility: given plain terminal-screen text rows and a
caller-supplied prompt fragment, return every normalized occurrence as
physical code-point coordinates. No terminal parsing, ANSI handling, history
interpretation, identity resolution, or navigation lives here; the caller owns
all of that.

Contract summary:

- ``TerminalTextLine.text`` is already plain Unicode text supplied by the
  caller, without ANSI sequences or terminal padding. Supplied spaces are
  significant and retained.
- ``TerminalTextLine.wrapped=True`` means this physical line continues the
  previous physical line by terminal soft wrapping, so the two join with no
  separator. Otherwise a hard line break separates this line from the
  previous one. The flag on the first physical line has no previous line to
  continue, so it is ignored (no error is raised).
- The logical text projection joins wrapped lines directly and inserts a
  single ``"\\n"`` between hard-broken lines. Runs of Unicode whitespace
  (``str.isspace``) in both the projection and the query collapse to one
  ASCII space; the query is additionally trimmed. Matching is
  case-sensitive, exact, and Unicode-preserving: no case folding,
  transliteration, punctuation stripping, or Unicode normalization is
  applied, so distinct sequences (including precomposed versus combining
  forms) never compare equal.
- ``find_prompt_candidates`` returns every exact normalized occurrence in
  stable physical order, including overlapping occurrences, and never picks
  a single "best" one. An empty or whitespace-only query, or no match,
  yields an empty list.
- Coordinates are zero-based physical line indices and Unicode code-point
  columns (plain Python string indices), end-exclusive. A cross-row match
  ends at the last matched code point + 1 on its row.
- A collapsed whitespace run maps its start to the first original
  whitespace position and its end to the end of the last original
  whitespace position. A synthetic hard-newline separator alone maps its
  start to the previous-line end and its end to the next-line column 0.
  Because a trimmed query cannot begin or end with whitespace, matches
  produced here always start and end on visible characters; the separator
  mapping only defines interior consistency.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalTextLine:
    """One physical terminal text row supplied by the caller."""

    text: str
    wrapped: bool = False


@dataclass(frozen=True)
class PromptTextMatch:
    """Physical text coordinates of one normalized occurrence, end-exclusive."""

    start_line: int
    start_column: int
    end_line: int
    end_column: int

    def __post_init__(self) -> None:
        for name in ("start_line", "start_column", "end_line", "end_column"):
            value = getattr(self, name)
            if not isinstance(value, int):
                raise TypeError(f"{name} must be an int, got {type(value).__name__}")
            if value < 0:
                raise ValueError(f"{name} must be non-negative, got {value}")
        if (self.end_line, self.end_column) < (self.start_line, self.start_column):
            raise ValueError(f"end must not precede start: {self!r}")


def _normalize_query(prompt: str) -> str:
    """Collapse whitespace runs to one ASCII space and trim the ends."""
    parts: list[str] = []
    pending_space = False
    for char in prompt:
        if char.isspace():
            pending_space = True
        else:
            if pending_space and parts:
                parts.append(" ")
            pending_space = False
            parts.append(char)
    return "".join(parts)


def _build_projection(lines: Sequence[TerminalTextLine]) -> tuple[str, list[tuple[int, int]], list[tuple[int, int]]]:
    """Build the normalized projection plus per-character origin mapping.

    Returns the projected text, the origin (line, column) of each projected
    character's first original position, and the exclusive end of each
    projected character's last original position.
    """
    chars: list[str] = []
    starts: list[tuple[int, int]] = []
    ends: list[tuple[int, int]] = []

    run_start: tuple[int, int] | None = None
    run_end: tuple[int, int] | None = None

    def push_whitespace(start: tuple[int, int], end: tuple[int, int]) -> None:
        nonlocal run_start, run_end
        if run_start is None:
            run_start = start
        run_end = end

    def flush_whitespace() -> None:
        nonlocal run_start, run_end
        if run_start is not None and run_end is not None:
            chars.append(" ")
            starts.append(run_start)
            ends.append(run_end)
            run_start = None
            run_end = None

    previous_length = 0
    for index, line in enumerate(lines):
        if index > 0 and not line.wrapped:
            # Synthetic hard-newline separator: spans previous-line end to
            # next-line column 0, and joins any adjacent whitespace run.
            push_whitespace((index - 1, previous_length), (index, 0))
        for column, char in enumerate(line.text):
            if char.isspace():
                push_whitespace((index, column), (index, column + 1))
            else:
                flush_whitespace()
                chars.append(char)
                starts.append((index, column))
                ends.append((index, column + 1))
        previous_length = len(line.text)
    flush_whitespace()
    return "".join(chars), starts, ends


def find_prompt_candidates(lines: Sequence[TerminalTextLine], prompt: str) -> list[PromptTextMatch]:
    """Find every normalized occurrence of ``prompt`` in ``lines``.

    Returns matches in stable physical order, including overlapping
    occurrences. Never raises for an empty input: empty ``lines`` or an
    empty/whitespace-only ``prompt`` yields an empty list. The input lines
    are never modified.
    """
    if not isinstance(prompt, str):
        raise TypeError(f"prompt must be a str, got {type(prompt).__name__}")
    query = _normalize_query(prompt)
    if not query or len(lines) == 0:
        return []
    projection, starts, ends = _build_projection(lines)
    matches: list[PromptTextMatch] = []
    search_from = 0
    while True:
        found = projection.find(query, search_from)
        if found < 0:
            return matches
        last = found + len(query) - 1
        start_line, start_column = starts[found]
        end_line, end_column = ends[last]
        matches.append(
            PromptTextMatch(
                start_line=start_line,
                start_column=start_column,
                end_line=end_line,
                end_column=end_column,
            )
        )
        search_from = found + 1
