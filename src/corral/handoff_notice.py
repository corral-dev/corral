"""Handoff source notice: tell the original agent its session was handed off.

Corral owns this product behavior (SessKit only reads history). Every handoff
builds the target prompt from the source summary plus the raw history file, but
the source agent keeps running unaware. This module sends one short English
sentence into the still-hosted source pane: handoff happened, this is
notification only, stop all work immediately, reply with only ``received``.

English is deliberate (not an i18n key): the reader is an agent, and a fixed
string keeps the token cost predictable. The reply is pinned to a single word
so the original agent does not burn tokens re-thinking the handoff.

Best-effort only: no keepalive name, dead pane, missing tmux, or any injection
failure returns False and never raises, so a dead source can never block or
fail the new session.
"""

from __future__ import annotations

import time

# Single sentence on purpose: inform + notification-only + stop now + ack word.
HANDOFF_SOURCE_NOTICE = (
    "This session has been handed off to another assistant \u2014 this is just "
    "a notification, please stop all work immediately and reply with only: received."
)

_INTERRUPT_PRESS_GAP = 0.3


def notify_handoff_source(session: dict | None, runtime=None) -> bool:
    """Nudge a still-hosted handoff source to stop; True when the note went out.

    ``session`` is the pre-handoff source snapshot (needs ``keepalive_name``).
    ``runtime`` is the source adapter (only its ``interrupt_keys`` are used);
    None skips the interrupt and still delivers the note. Shell placeholders
    are never notified.
    """
    if not isinstance(session, dict):
        return False
    if str(session.get("source") or "") == "shell":
        return False
    name = str(session.get("keepalive_name") or "").strip()
    if not name:
        return False
    try:
        from corral import embed, liveness
    except Exception:
        return False
    try:
        if not liveness.is_alive(name):
            return False
    except Exception:
        return False
    try:
        keys: tuple[str, ...] = ()
        if runtime is not None:
            try:
                keys = tuple(getattr(runtime, "interrupt_keys", ()) or ())
            except Exception:
                keys = ()
        for index, key in enumerate(keys):
            if not str(key).strip():
                continue
            if index:
                time.sleep(_INTERRUPT_PRESS_GAP)
            try:
                embed.send_key_detailed(name, str(key))
            except Exception:
                break
        pasted = embed.paste_detailed(name, HANDOFF_SOURCE_NOTICE)
        if not getattr(pasted, "ok", False):
            return False
        entered = embed.send_key_detailed(name, "Enter")
        return bool(getattr(entered, "ok", False))
    except Exception:
        return False
