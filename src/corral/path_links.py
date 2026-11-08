"""Path mentions in terminal text that open Finder (owner request 2026-11-08).

The TUI pane maps a modified click on a screen row to the path under the
pointer and reveals it: a folder opens, a file is shown selected in its folder.
The Mac client has the native equivalent (``MacPathLinks``); both accept the
same forms: absolute, ``~/``, project-relative and ``:line`` / ``#Lline``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from rich.cells import cell_len

_PATH = re.compile(
    r"(?<![\w/.~:@-])"
    r"(?:~?/[\w\-.+@=%,]+(?:/[\w\-.+@=%,]*)*"
    r"|(?:\.{1,2}/)?[\w\-.+@]+(?:/[\w\-.+@]+)+/?"
    r"|[\w\-+@]+\.[A-Za-z][A-Za-z0-9]{0,7})"
    r"(?:(?::\d+){1,2}|#L\d+(?:-L?\d+)?)?"
)
_LOCATION = re.compile(r"(?:(?::\d+){1,2}|#L\d+(?:-L?\d+)?)$")
_TRAILING = ".,;!?)]}'\""


def char_index(text: str, cell: int) -> int | None:
    """String index of the character covering terminal cell ``cell``."""
    if cell < 0:
        return None
    used = 0
    for index, char in enumerate(text):
        used += cell_len(char)
        if used > cell:
            return index
    return None


def path_at(text: str, cell: int) -> str | None:
    """The path mention under terminal cell ``cell`` of one screen row."""
    index = char_index(text, cell)
    if index is None:
        return None
    for match in _PATH.finditer(text):
        start, end = match.span()
        raw = match.group(0)
        while len(raw) > 1 and raw[-1] in _TRAILING:
            raw = raw[:-1]
            end -= 1
        if start <= index < end:
            return raw
    return None


def resolve(raw: str, cwd: str | None) -> str | None:
    """Existing file or folder a mention points to; its folder for a deleted file."""
    path = _LOCATION.sub("", raw)
    if path.startswith("~"):
        full = os.path.expanduser(path)
    elif path.startswith("/"):
        full = path
    elif cwd:
        full = os.path.normpath(os.path.join(os.path.expanduser(cwd), path))
    else:
        return None
    if os.path.exists(full):
        return full
    if "/" not in path:
        return None
    parent = os.path.dirname(full.rstrip("/"))
    return parent if parent not in ("", "/") and os.path.isdir(parent) else None


def reveal(target: str) -> bool:
    """Open a folder, or show a file selected in its folder."""
    if sys.platform == "darwin":
        argv = ["open", target] if os.path.isdir(target) else ["open", "-R", target]
    else:
        argv = ["xdg-open", target if os.path.isdir(target) else os.path.dirname(target)]
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True
