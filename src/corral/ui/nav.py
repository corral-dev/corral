"""Window-scoped navigation and presentation-only session filter state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from corral.activity_board import needs_attention, resolve_active_marker


class SessionQuickFilter(str, Enum):
    ALL = "all"
    FOCUS = "focus"
    ACTIVE = "active"

    @property
    def next(self) -> SessionQuickFilter:
        return {
            self.ALL: self.FOCUS,
            self.FOCUS: self.ACTIVE,
            self.ACTIVE: self.ALL,
        }[self]

    @property
    def label_key(self) -> str:
        return "filter.active" if self is self.ACTIVE else "filter.attention"

    def matches(self, session: dict, *, now: float | None = None) -> bool:
        if self is self.FOCUS:
            return needs_attention(session, now=now)
        if self is self.ACTIVE:
            return resolve_active_marker(session, now=now) in {"waiting", "working", "recent"}
        return True


@dataclass
class NavState:
    source: str
    project_query: str = ""
    quick_filter: SessionQuickFilter = SessionQuickFilter.ALL
    runtime_filters: set[str] = field(default_factory=set)
    project_filters: set[str] = field(default_factory=set)

    @property
    def narrowing(self) -> bool:
        return bool(self.project_query.strip() or self.quick_filter is not SessionQuickFilter.ALL
                    or self.runtime_filters or self.project_filters)
