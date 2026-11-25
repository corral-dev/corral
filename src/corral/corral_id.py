"""Stateless short Corral session IDs (<2-char runtime prefix><6-char Crockford Base32>)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

CROCKFORD_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_CROCKFORD_NORMALIZE = str.maketrans({"i": "1", "l": "1", "o": "0"})
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Explicit 2-character prefixes per runtime so claude/codex/cursor remain distinct.
RUNTIME_PREFIXES: dict[str, str] = {
    "claude": "cc",
    "codex": "cx",
    "opencode": "oc",
    "cursor": "cu",
    "pi": "pi",
    "antigravity": "ag",
    "kimi": "km",
}


def runtime_prefix(runtime_id: str | None) -> str:
    """Return the fixed 2-character lowercase prefix for ``runtime_id``."""
    rt = str(runtime_id or "").strip().lower()
    if rt in RUNTIME_PREFIXES:
        return RUNTIME_PREFIXES[rt]
    cleaned = _NON_ALNUM_RE.sub("", rt)
    if len(cleaned) >= 2:
        return cleaned[:2]
    if len(cleaned) == 1:
        return f"{cleaned}x"
    return "un"


def derive_corral_id(runtime_id: str | None, session_id: str | None) -> str:
    """Derive the deterministic 8-char Corral ID from ``(runtime_id, session_id)``.

    Returns an empty string when either side is missing or ``runtime_id == "shell"``.
    Uses the top 30 bits of SHA-256(``"<runtime>:<id>"``) encoded in lowercase
    Crockford Base32 so Python and Swift clients produce identical values offline.
    """
    rt = str(runtime_id or "").strip().lower()
    sid = str(session_id or "").strip()
    if not rt or not sid or rt == "shell":
        return ""
    digest = hashlib.sha256(f"{rt}:{sid}".encode()).digest()
    value = int.from_bytes(digest[:4], "big") >> 2
    suffix = "".join(
        CROCKFORD_ALPHABET[(value >> shift) & 0x1F]
        for shift in (25, 20, 15, 10, 5, 0)
    )
    return f"{runtime_prefix(rt)}{suffix}"


def corral_id_for_session(session: Mapping[str, object]) -> str:
    """Return the Corral ID for a scanned/hosted session dict, or ``""`` if unavailable.

    Provisional placeholders (before the runtime writes a real native session ID)
    and free shell panes return ``""`` so users never copy an ephemeral ID.
    """
    if session.get("provisional"):
        return ""
    rt = session.get("source") or session.get("runtime")
    sid = session.get("id")
    return derive_corral_id(
        str(rt) if rt is not None else None,
        str(sid) if sid is not None else None,
    )


def normalize_corral_id(raw: str | None) -> str:
    """Normalize user input for Corral ID matching (lowercase, Crockford aliases)."""
    text = str(raw or "").strip().lower().replace("-", "")
    if len(text) != 8:
        return text
    return text[:2] + text[2:].translate(_CROCKFORD_NORMALIZE)


def matches_corral_id(session: Mapping[str, object], ident: str | None) -> bool:
    """Return True when ``ident`` matches the session's derived Corral ID."""
    cid = corral_id_for_session(session)
    if not cid:
        return False
    norm = normalize_corral_id(ident)
    return bool(norm) and cid == norm
