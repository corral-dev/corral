"""`corral flex`: run the agentflex card (installed binary, else `npx`).

agentflex is a separate Rust program distributed through npm
(https://github.com/corral-dev/agentflex). Corral only launches it and passes
the arguments through; it parses nothing itself.
Design: docs/design/SHAREABLE_WRAP_CARD_DESIGN.md §10.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from corral.i18n import t

PACKAGE = "agentflex"


def command(argv: list[str]) -> list[str] | None:
    """The command line that runs agentflex, or None when neither route exists."""
    binary = shutil.which(PACKAGE)
    if binary:
        return [binary, *argv]
    npx = shutil.which("npx")
    if npx:
        return [npx, "--yes", f"{PACKAGE}@latest", *argv]
    return None


def main(argv: list[str]) -> int:
    from corral import i18n

    i18n.init()
    cmd = command(argv)
    if cmd is None:
        print(t("flex.unavailable"), file=sys.stderr)
        return 1
    if os.name != "nt":
        # Hand the terminal to agentflex directly (animation, keys, exit code).
        try:
            os.execv(cmd[0], cmd)
        except OSError as exc:
            print(t("flex.launch_failed", error=exc.strerror or str(exc)), file=sys.stderr)
            return 1
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        print(t("flex.launch_failed", error=exc.strerror or str(exc)), file=sys.stderr)
        return 1
