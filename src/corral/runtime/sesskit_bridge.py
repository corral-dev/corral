"""Helpers that keep Corral runtime adapters aligned with SessKit APIs."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from sesskit.models import ConversationMessage, SessionInfo
from sesskit.registry import ConversationLoadError, load_session_conversation


def load_runtime_conversation(session: SessionInfo) -> list[ConversationMessage]:
    """Same session-dict adapter SessKit CLI uses.

    Corral UI/export keep the historical soft-fail behavior (missing history →
    empty list) so a deleted file never crashes the sidebar. SessKit CLI still
    raises ``ConversationLoadError`` for explicit callers.
    """
    try:
        return load_session_conversation(dict(session))
    except ConversationLoadError:
        return []


def call_scan(
    scan_fn: Callable[..., list[SessionInfo]],
    *,
    limit: int,
    keep_ids: set[str] | None = None,
    include_missing_cwd: bool = False,
) -> list[SessionInfo]:
    """Forward only kwargs the SessKit scanner actually accepts."""
    try:
        target = inspect.unwrap(scan_fn)
    except (TypeError, ValueError):
        target = scan_fn
    try:
        params = inspect.signature(scan_fn).parameters
    except (TypeError, ValueError):
        params = {}

    kwargs: dict[str, Any] = {"limit": limit}
    if keep_ids is not None and "keep_ids" in params:
        kwargs["keep_ids"] = keep_ids
    if include_missing_cwd and "include_missing_cwd" in params:
        kwargs["include_missing_cwd"] = True

    accepts_host_provider = "host_claim_provider" in params or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in params.values()
    )
    if (
        accepts_host_provider
        and getattr(target, "__module__", None) == "sesskit.parsers.codex"
        and getattr(target, "__name__", None) == "scan_sessions"
    ):
        from corral.codex_identity import live_claims

        kwargs["host_claim_provider"] = live_claims

    return scan_fn(**kwargs)
