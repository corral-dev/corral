"""Regression guard for the real-terminal selftest interpreter contract.

`selftest.sh` captures the outer interpreter's `sys.path` into `PYTHONPATH`
and then replaces `PATH` inside its fixture environment. Every TUI launch
inside tmux must therefore reuse the same absolute interpreter: a bare
`python3` under the replaced `PATH` can resolve to a different installation
(e.g. Homebrew 3.14 reading a 3.12 standard-library tree), which aborts
before startup with `AssertionError: SRE module mismatch`.

Contract (see docs/TEST_ENVIRONMENT_GUIDE.md): one selected absolute
interpreter, shell-quoted, used for the `sys.path` capture and for all
three launches (main/direct/cursor). No hardcoded interpreter, no bare
`python3` in a tmux launch line.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SELFTEST = REPO / "selftest.sh"
BASH = shutil.which("bash") or "/bin/bash"


def _script_text() -> str:
    return SELFTEST.read_text(encoding="utf-8")


def _selection_block() -> str:
    """Return the script's own interpreter-selection lines for execution.

    Anchored on the variable assignments rather than line numbers so the
    block tracks the script as it evolves; the functional tests below run
    this block under an adversarial PATH instead of comparing its text.
    """
    text = _script_text()
    start = text.index('SELFTEST_PYTHON="')
    end = text.index("SELFTEST_PYTHON_Q=", start)
    end = text.index("\n", end)
    lines = text[start:end].splitlines()
    # Include the preceding `if` line that opens the selection.
    if_index = text.rindex("if [[ -n", 0, start)
    block = text[if_index:end].strip()
    assert "SELFTEST_PYTHON_Q=" in block, "selection must also build the quoted form"
    assert len(lines) >= 2
    return block


def _run_selection(*, virtual_env: str | None, path: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if virtual_env is None:
        env.pop("VIRTUAL_ENV", None)
    else:
        env["VIRTUAL_ENV"] = virtual_env
    env["PATH"] = path
    probe = _selection_block() + '\nprintf "%s" "$SELFTEST_PYTHON"\n'
    return subprocess.run(
        [BASH, "-c", probe],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
        timeout=30,
        check=False,
    )


class SelftestInterpreterTests(unittest.TestCase):
    def test_tmux_launches_share_one_quoted_interpreter(self) -> None:
        launches = [line for line in _script_text().splitlines() if "send-keys" in line and "-m corral" in line]
        # Main, direct-launch and cursor windows: dropping one must fail loudly,
        # not silently shrink coverage.
        self.assertGreaterEqual(len(launches), 3, f"expected all TUI launches, got: {launches}")
        for line in launches:
            self.assertNotRegex(line, r"(?<![\w$}])python3\s+-m corral", f"bare interpreter in launch: {line}")
            self.assertIn("SELFTEST_PYTHON_Q", line, f"launch must use the quoted selected interpreter: {line}")

    def test_sys_path_capture_uses_selected_interpreter(self) -> None:
        for line in _script_text().splitlines():
            if "SELFTEST_PYTHON" in line or "command -v python3" in line:
                continue
            self.assertNotRegex(line, r"(?<![\w$}])python3\s+-c", f"bare interpreter capture: {line}")

    def test_selection_prefers_outer_venv_over_poisoned_path(self) -> None:
        # A different-version `python3` earlier on PATH (the Homebrew 3.14 vs
        # venv 3.12 shape from the real failure) must not win while the outer
        # environment already selected an interpreter via VIRTUAL_ENV.
        with tempfile.TemporaryDirectory() as temp:
            venv_bin = Path(temp) / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            venv_python = venv_bin / "python3"
            venv_python.symlink_to(Path(sys.executable))
            poison = Path(temp) / "poison"
            poison.mkdir()
            fake = poison / "python3"
            fake.write_text('#!/usr/bin/env bash\necho "wrong-interpreter"\n', encoding="utf-8")
            fake.chmod(0o755)
            result = _run_selection(
                virtual_env=str(venv_bin.parent),
                path=os.pathsep.join([str(poison), os.environ.get("PATH", "")]),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(venv_python))

    def test_selection_falls_back_to_outer_path_python(self) -> None:
        # Without VIRTUAL_ENV the selection tracks the invoking shell's
        # `python3` — the same interpreter whose sys.path gets captured —
        # instead of inventing its own.
        with tempfile.TemporaryDirectory() as temp:
            outer = Path(temp) / "outer"
            outer.mkdir()
            fake = outer / "python3"
            fake.write_text('#!/usr/bin/env bash\necho "outer-python"\n', encoding="utf-8")
            fake.chmod(0o755)
            result = _run_selection(virtual_env=None, path=os.pathsep.join([str(outer), os.environ.get("PATH", "")]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(fake))

    def test_quoted_interpreter_survives_shell_reparse_with_spaces(self) -> None:
        # tmux pane shells re-parse the launch string, so the quoted form
        # must round-trip even when the checkout path contains spaces.
        with tempfile.TemporaryDirectory() as temp:
            venv_bin = Path(temp) / "my venv" / "bin"
            venv_bin.mkdir(parents=True)
            (venv_bin / "python3").symlink_to(Path(sys.executable))
            env = dict(os.environ)
            env["VIRTUAL_ENV"] = str(venv_bin.parent)
            probe = _selection_block() + '\neval "$SELFTEST_PYTHON_Q --version"\n'
            result = subprocess.run(
                [BASH, "-c", probe],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env=env,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Python", result.stdout)


if __name__ == "__main__":
    unittest.main()
