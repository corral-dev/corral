"""Corral workflow orchestration: Python DAG runner, capability routing and pure-CLI control.

Top-level imports stay lightweight so ``corral`` fast paths never pay workflow
or Textual startup costs.
"""

from __future__ import annotations

__all__ = [
    "WorkflowError",
    "WorkflowStore",
    "is_workflow_session",
    "workflow_root",
]


def __getattr__(name: str):
    if name == "WorkflowError":
        from corral_workflow.errors import WorkflowError

        return WorkflowError
    if name in {"WorkflowStore", "workflow_root"}:
        from corral_workflow import store as _store

        return getattr(_store, name)
    if name == "is_workflow_session":
        from corral_workflow.visibility import is_workflow_session

        return is_workflow_session
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
