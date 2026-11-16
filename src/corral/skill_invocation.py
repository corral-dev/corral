"""Portable, explicit skill invocation text used by remote chat and titles.

Only ``/skill:name`` is a portable invocation marker. Ordinary slash commands,
mentions and dollar-prefixed text are deliberately left alone: without a
runtime's skill catalog, rewriting those would corrupt ordinary user prompts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PREFIX = "/skill:"
_SKILL_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


@dataclass(frozen=True)
class PortableSkillInvocation:
    """One parsed invocation; ``suffix`` retains the original argument bytes."""

    name: str
    suffix: str

    @property
    def arguments(self) -> str:
        return self.suffix.strip()


def parse_skill_invocation(text: str) -> PortableSkillInvocation | None:
    """Parse only a complete, spec-valid ``/skill:name`` command at position zero."""
    if not text.startswith(_PREFIX):
        return None
    body = text[len(_PREFIX):]
    match = re.match(r"\S+", body)
    if match is None:
        return None
    name = match.group(0)
    if len(name) > 64 or _SKILL_NAME_RE.fullmatch(name) is None:
        return None
    return PortableSkillInvocation(name=name, suffix=body[len(name):])


def format_skill_invocation(text: str, style: str) -> str:
    """Render a portable invocation in an adapter-owned native command style."""
    invocation = parse_skill_invocation(text)
    if invocation is None:
        return text
    commands = {
        "slash": "/",
        "dollar": "$",
        "mention": "@",
        "pi": "/skill:",
    }
    try:
        prefix = commands[style]
    except KeyError as exc:
        raise ValueError(f"unknown skill invocation style: {style}") from exc
    # Whitespace-only arguments carry no request. Otherwise preserve the
    # original separator and every argument byte, including newlines/tabs.
    suffix = invocation.suffix if invocation.arguments else ""
    return f"{prefix}{invocation.name}{suffix}"


def skill_invocation_display_text(text: str) -> str:
    """Human task text shared by immediate echoes and title extraction."""
    invocation = parse_skill_invocation(text)
    if invocation is None:
        return text
    return invocation.arguments or f"/skill:{invocation.name}"
