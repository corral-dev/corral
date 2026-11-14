"""Per-pid mock adapters for SessKit's batched process probes.

SessKit 0.3.x scanners call ``process_environs(pids)`` and
``process_start_times(pids)`` once per scan instead of the per-pid helpers.
Tests still describe fixtures per pid; these wrappers turn a per-pid
``side_effect`` / ``return_value`` into the batch shape.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_UNSET = object()


def batch_environ(side_effect: Callable[..., dict] | None = None, return_value: Any = _UNSET):
    """``process_environs`` stand-in built from a per-pid environ fixture."""

    def _environs(pids, **kwargs) -> dict[int, dict[str, str]]:
        result: dict[int, dict[str, str]] = {}
        for pid in dict.fromkeys(pids):
            if side_effect is not None:
                result[pid] = dict(side_effect(pid, **kwargs) or {})
            else:
                result[pid] = dict(return_value or {}) if return_value is not _UNSET else {}
        return result

    return _environs


def batch_start(side_effect: Callable[[int], float | None] | None = None, return_value: Any = _UNSET):
    """``process_start_times`` stand-in built from a per-pid start-time fixture."""

    def _starts(pids) -> dict[int, float]:
        result: dict[int, float] = {}
        for pid in dict.fromkeys(pids):
            if side_effect is not None:
                started = side_effect(pid)
            else:
                started = None if return_value is _UNSET else return_value
            if started is not None:
                result[pid] = started
        return result

    return _starts
