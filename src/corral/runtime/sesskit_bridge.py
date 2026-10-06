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
    **extra: Any,
) -> list[SessionInfo]:
    """Forward ``limit`` plus only the kwargs the SessKit scanner accepts.

    Each runtime adapter passes its own provider explicitly (the Corral host
    extension and, for Codex, the legacy claim provider for older SessKit
    builds). Unknown kwargs are dropped so Corral keeps working against both
    newer scanners (``host``) and older ones (``host_claim_provider`` only).
    """
    try:
        params = inspect.signature(scan_fn).parameters
    except (TypeError, ValueError):
        params = {}

    accepts_extra = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in params.values()
    )
    kwargs: dict[str, Any] = {"limit": limit}
    if keep_ids is not None and "keep_ids" in params:
        kwargs["keep_ids"] = keep_ids
    if include_missing_cwd and "include_missing_cwd" in params:
        kwargs["include_missing_cwd"] = True
    for key, value in extra.items():
        if value is None:
            continue
        if key in params or accepts_extra:
            kwargs[key] = value

    return scan_fn(**kwargs)


def refresh_runtime_session(session: SessionInfo) -> SessionInfo | None:
    """SessKit single-session refresh with the Corral host extension.

    Older SessKit builds without ``refresh_session`` and any read failure
    return None, so callers fall back to the full-scan result.
    """
    try:
        from sesskit.registry import refresh_session
    except ImportError:
        return None
    from corral.runtime.host_extension import corral_host_extension

    try:
        return refresh_session(dict(session), host=corral_host_extension())
    except Exception:  # noqa: BLE001 — a refresh must never break the state probe
        return None
