"""Unified share transcript — implemented by SessKit.

``SCHEMA_ID`` is ``sesskit.transcript/v1``. Corral readers additionally accept
the host's legacy ``corral.share/v1`` alias, supplied via the host extension
(``legacy_schema_ids``); neutral SessKit readers accept only ``SCHEMA_ID``.
"""

from __future__ import annotations

from sesskit.transcript import *  # noqa: F403
from sesskit.transcript import EVENT_TYPES, SCHEMA_ID, count_events, load_events

from corral.runtime.host_extension import LEGACY_SCHEMA_IDS as _HOST_LEGACY_IDS

try:
    from sesskit.transcript import accepted_schema_ids as _accepted_schema_ids

    LEGACY_SCHEMA_IDS = tuple(
        ident for ident in _accepted_schema_ids(legacy_ids=_HOST_LEGACY_IDS) if ident != SCHEMA_ID
    )
except ImportError:
    # Older SessKit without the seam: fall back to its bundled legacy tuple.
    from sesskit.transcript import LEGACY_SCHEMA_IDS

__all__ = [
    "EVENT_TYPES",
    "LEGACY_SCHEMA_IDS",
    "SCHEMA_ID",
    "count_events",
    "load_events",
]
