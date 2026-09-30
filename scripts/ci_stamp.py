"""完整检查戳：同一工作区产品代码未改时，发版推送和收尾不再整套重跑。"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

FINGERPRINT_DIRS = ("src", "tests", "scripts", ".githooks", "rust")
FINGERPRINT_FILES = ("pyproject.toml", "Cargo.toml", "Cargo.lock", "uv.lock")
_SKIP_DIR_NAMES = {"__pycache__", "target", ".egg-info"}
_SKIP_SUFFIXES = {".pyc", ".so", ".dylib"}
_ENV_FINGERPRINT_PROBE = (
    "import importlib.metadata, sys;",
    "names = [(str(d.metadata['Name'] or '').lower(), d.version) for d in importlib.metadata.distributions()];",
    "print(sys.version.split()[0]);",
    "print('\\n'.join(sorted(f'{n}=={v}' for n, v in names)))",
)


def stamp_path(root: Path) -> Path:
    """戳写在 git 目录里，不进工作区、不进提交。"""
    override = os.environ.get("CORRAL_CI_STAMP")
    if override:
        return Path(override)
    git_dir = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "--git-dir"],
        text=True,
    ).strip()
    path = Path(git_dir)
    if not path.is_absolute():
        path = (root / path).resolve()
    return path / "corral-ci-stamp"


def worktree_fingerprint(root: Path) -> str:
    """按文件内容指纹，不看提交号——先测再提交后仍能对上。"""
    files: list[Path] = []
    for name in FINGERPRINT_FILES:
        path = root / name
        if path.is_file():
            files.append(path)
    for dirname in FINGERPRINT_DIRS:
        base = root / dirname
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIR_NAMES for part in path.parts):
                continue
            if path.suffix in _SKIP_SUFFIXES:
                continue
            files.append(path)
    hasher = hashlib.sha256()
    for path in sorted(files, key=lambda p: path_key(p, root)):
        rel = path_key(path, root).encode()
        hasher.update(rel)
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\n")
    return hasher.hexdigest()


def path_key(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def environment_fingerprint(root: Path) -> str:
    """Identify the checkout venv's interpreter plus installed distribution set.

    Local-only and offline: hashes the uv.lock bytes (or a missing marker) plus
    the venv interpreter version and its installed distributions, probed with
    stdlib only. A stale, deleted, or missing environment yields a different
    digest, so it can never reuse a full-pass stamp.
    """
    hasher = hashlib.sha256()
    lock = root / "uv.lock"
    try:
        hasher.update(b"lock:")
        hasher.update(lock.read_bytes())
    except OSError:
        hasher.update(b"lock:missing")
    hasher.update(b"\n")
    python = root / ".venv" / "bin" / "python"
    if os.name == "nt":
        python = root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        hasher.update(b"venv:missing")
        return hasher.hexdigest()
    # Hermetic probe: ambient PYTHONPATH (e.g. ci-test prepending src/) must
    # not leak stale egg-info such as src/pickup.egg-info into the listing.
    probe_env = os.environ.copy()
    for key in ("PYTHONPATH", "VIRTUAL_ENV", "UV_PROJECT", "UV_PROJECT_ENVIRONMENT", "__PYVENV_LAUNCHER__"):
        probe_env.pop(key, None)
    probe_env["PYTHONNOUSERSITE"] = "1"
    try:
        result = subprocess.run(
            [str(python), "-c", "\n".join(_ENV_FINGERPRINT_PROBE)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=probe_env,
        )
    except (OSError, subprocess.TimeoutExpired):
        hasher.update(b"venv:unreadable")
        return hasher.hexdigest()
    if result.returncode != 0:
        hasher.update(b"venv:unreadable")
        return hasher.hexdigest()
    hasher.update(b"venv:")
    hasher.update(result.stdout.encode())
    return hasher.hexdigest()


def write_stamp(root: Path, fingerprint: str | None = None) -> Path:
    path = stamp_path(root)
    digest = fingerprint if fingerprint is not None else worktree_fingerprint(root)
    path.write_text(
        f"fingerprint={digest}\nenv={environment_fingerprint(root)}\n",
        encoding="utf-8",
    )
    return path


def read_stamp(path: Path) -> tuple[str | None, str | None]:
    if not path.is_file():
        return None, None
    fingerprint: str | None = None
    env: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("fingerprint="):
            fingerprint = line.split("=", 1)[1].strip()
        elif line.startswith("env="):
            env = line.split("=", 1)[1].strip()
    return fingerprint, env


def stamp_matches(root: Path) -> bool:
    recorded, recorded_env = read_stamp(stamp_path(root))
    if not recorded or not recorded_env:
        # Fingerprint-only stamps from before the environment binding predate
        # this contract; they never match, so one full run re-baselines.
        return False
    if recorded != worktree_fingerprint(root):
        return False
    return recorded_env == environment_fingerprint(root)
