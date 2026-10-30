#!/usr/bin/env python3
"""Install Corral into a clean virtualenv and smoke-check imports.

Proves a stranger machine can install without a local SessKit editable checkout.
Used by publish-release.sh before updating the public download / Homebrew tap.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
import tempfile

_SCRIPTS = pathlib.Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

ROOT = _SCRIPTS.parent


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT, env=env)


def _clean_env() -> dict[str, str]:
    """Drop PYTHONPATH so a maintainer checkout cannot satisfy sesskit for pip."""
    return {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}


def _resolve_python() -> str:
    """Prefer a Python whose ensurepip works (Homebrew / CORRAL_CLEAN_PYTHON)."""
    candidates = [
        os.environ.get("CORRAL_CLEAN_PYTHON"),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        sys.executable,
    ]
    for candidate in candidates:
        if not candidate or not pathlib.Path(candidate).exists():
            continue
        probe = subprocess.run(
            [candidate, "-c", "import ensurepip, venv"],
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    return sys.executable


def main(argv: list[str] | None = None) -> int:
    import sesskit_dep

    try:
        import control_dep
    except ImportError:
        control_dep = None  # type: ignore[assignment]

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-remote",
        action="store_true",
        help="Also install optional [remote] extras and import them",
    )
    args = parser.parse_args(argv)

    sesskit_dep.verify_published_digests()
    if control_dep is not None:
        try:
            control_requirement = control_dep.wheel_requirement()
        except RuntimeError:
            control_requirement = None  # unpublished: skipped below, never invented
    else:
        control_requirement = None
    python = _resolve_python()

    env = _clean_env()
    with tempfile.TemporaryDirectory(prefix="corral-clean-install-") as tmp:
        tmp_path = pathlib.Path(tmp)
        venv_dir = tmp_path / "venv"
        _run([python, "-m", "venv", str(venv_dir)], env=env)
        py = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        pip = [str(py), "-m", "pip"]
        _run([*pip, "install", "--upgrade", "pip"], env=env)
        _run([*pip, "install", sesskit_dep.wheel_requirement()], env=env)
        if control_requirement is not None:
            _run([*pip, "install", control_requirement], env=env)
        else:
            print("control artifact unpublished: clean install skips agent-session-control")
        # Install from the working tree like a source user would after cloning.
        extra = ".[remote]" if args.with_remote else "."
        _run([*pip, "install", extra], env=env)
        check = (
            "import corral, corral._native, sesskit; "
            "assert corral.__version__; "
            "print(corral.__version__, getattr(sesskit, '__version__', 'sesskit'))"
        )
        if control_requirement is not None:
            check = (
                "import corral, corral._native, sesskit, agent_session_control; "
                "assert corral.__version__; "
                "print(corral.__version__, getattr(sesskit, '__version__', 'sesskit'), "
                "agent_session_control.__version__)"
            )
        if args.with_remote:
            check += "; import cryptography, websockets, segno"
            # NOTE: zeroconf (mDNS broadcast) is an optional part of [remote]:
            # the daemon must keep working without it, so it stays out of the
            # hard clean-install check.
        _run([str(py), "-c", check], env=env)
        _run([str(py), "-m", "corral", "--version"], env=env)
    print("ok clean install")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
