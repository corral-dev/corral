"""Safe Git worktree isolation for parallel mutating workflow steps.

Creates detached worktrees from ``HEAD`` inside the workflow state directory so
concurrent coding agents never overwrite each other or mutate uncommitted files
in the user's primary working tree.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from corral_workflow.errors import EXIT_ERROR, EXIT_USAGE, WorkflowError


def _run_git(args: list[str], cwd: Path, *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkflowError("git_failed", f"Git invocation failed: {exc}", EXIT_ERROR) from exc


def resolve_git_root(cwd: str | Path) -> Path:
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        raise WorkflowError("usage_error", f"Working directory does not exist: {path}", EXIT_USAGE)
    proc = _run_git(["rev-parse", "--show-toplevel"], path)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise WorkflowError(
            "usage_error",
            f"workspace='worktree' requires a Git repository: {path}",
            EXIT_USAGE,
            hint="Use workspace='inherit' for non-Git directories or run inside a Git repository.",
        )
    return Path(proc.stdout.strip()).resolve()


def create_worktree(repo_cwd: str | Path, worktree_dir: Path, *, ref: str = "HEAD") -> dict[str, Any]:
    """Create an isolated detached worktree at ``worktree_dir`` without touching the main tree."""
    repo_root = resolve_git_root(repo_cwd)
    head_proc = _run_git(["rev-parse", ref], repo_root)
    if head_proc.returncode != 0 or not head_proc.stdout.strip():
        raise WorkflowError(
            "git_failed",
            f"Cannot resolve base ref '{ref}' in {repo_root}: {head_proc.stderr.strip()}",
            EXIT_ERROR,
        )
    base_commit = head_proc.stdout.strip()
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    if worktree_dir.exists():
        remove_worktree(repo_root, worktree_dir)
    add_proc = _run_git(["worktree", "add", "--detach", str(worktree_dir), base_commit], repo_root)
    if add_proc.returncode != 0:
        raise WorkflowError(
            "git_failed",
            f"Failed to create git worktree at {worktree_dir}: {add_proc.stderr.strip()}",
            EXIT_ERROR,
        )
    return {
        "repo_root": str(repo_root),
        "worktree_path": str(worktree_dir),
        "base_commit": base_commit,
    }


def worktree_diff_summary(worktree_dir: str | Path, *, base_commit: str = "HEAD") -> dict[str, Any]:
    path = Path(worktree_dir)
    status_proc = _run_git(["status", "--porcelain"], path)
    diff_stat = _run_git(["diff", "--stat", base_commit], path)
    dirty_files = [line for line in status_proc.stdout.splitlines() if line.strip()]
    return {
        "dirty": bool(dirty_files),
        "changed_files_count": len(dirty_files),
        "status_lines": dirty_files[:50],
        "diff_stat": diff_stat.stdout.strip()[:4000],
    }


def reset_worktree_clean(worktree_dir: str | Path, *, base_commit: str = "HEAD") -> None:
    """Reset an isolated disposable worktree back to ``base_commit`` before a clean fallback retry."""
    path = Path(worktree_dir)
    _run_git(["reset", "--hard", base_commit], path)
    _run_git(["clean", "-fd"], path)


def remove_worktree(repo_root: str | Path, worktree_dir: str | Path) -> None:
    """Remove a disposable worktree safely without affecting the main working tree."""
    repo = Path(repo_root)
    target = Path(worktree_dir)
    proc = _run_git(["worktree", "remove", "--force", str(target)], repo)
    if proc.returncode != 0 and target.exists():
        shutil.rmtree(target, ignore_errors=True)
        _run_git(["worktree", "prune"], repo)
