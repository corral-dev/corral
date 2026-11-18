"""Pass-to-pass state for hosted-session reclaim.

Three things a single pass cannot decide on its own, all cheap and all recorded
so a disputed retirement can be re-decided from the log
(``docs/design/HOSTED_SESSION_LIFECYCLE_DESIGN.md`` §3.1, §3.6, §4.2, §4.4):

* **S6 hysteresis** — a memory-pressure read is discrete and a single sample is
  a coin flip at a boundary, while macOS swap usage is sticky and stays "high"
  for hours after recovery. Pressure therefore *arms* only on two consecutive
  true reads at least 60 s apart, and *disarms* only after ten quiet minutes.
  Swap usage is informational and never a trigger.
* **The budget ladder** (§4.4 step 1) — tightening the idle threshold toward the
  absolute floor happens one step per pass, and only while pressure is armed:
  a bare resident-count overrun must not shorten the owner's 120-minute
  contract.
* **S1 cumulative-CPU delta** — ``ps -eo time`` for the pane tree, compared with
  the previous pass. Zero delta over a full interval proves the tree consumed no
  CPU; a changed process start time, a shrunk tree or an unreadable column is
  ``unknown``, which neither proves nor permits anything.

State lives in one small JSON file under the product cache directory.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from corral.legacy_names import cache_dir

STATE_FILE = "reclaim-pass.json"

# §4.2 knobs. Both are overridable through the product env prefixes.
PRESSURE_SAMPLES = 2
PRESSURE_SAMPLE_GAP_SECONDS = 60.0
PRESSURE_DISARM_SECONDS = 600.0
# 2026-11-18 owner ruling: pressure never shortens the 60-minute threshold
# below 60 minutes; instead it drops the 8-session resident holdback to 0.
BUDGET_LADDER_MINUTES: tuple[float, ...] = (60.0,)

# CPU burned below this per pass is TUI repaint noise, not work. The measured
# incident had exactly 0.0 s of CPU on all 34 parked sessions; a named tolerance
# keeps a blinking status line from pinning a session resident for ever. Set to
# 0.0 to make any nonzero delta block retirement.
CPU_WORK_TOLERANCE_SECONDS = 0.25
# ``etime`` prints whole seconds, so two samples of the same process can derive
# start times that differ by a second.
_START_TIME_TOLERANCE_SECONDS = 2.0
_GAP_EPSILON_SECONDS = 0.5


@dataclass(frozen=True)
class PressureState:
    """The hysteresis and ladder state carried between passes."""

    armed: bool = False
    true_count: int = 0
    first_true_at: float | None = None
    last_true_at: float | None = None
    false_since: float | None = None
    updated_at: float = 0.0
    budget_steps: int = 0
    swap_ratio: float | None = None
    pressure_level: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "armed": self.armed,
            "true_count": self.true_count,
            "first_true_at": self.first_true_at,
            "last_true_at": self.last_true_at,
            "false_since": self.false_since,
            "updated_at": self.updated_at,
            "budget_steps": self.budget_steps,
            "swap_ratio": self.swap_ratio,
            "pressure_level": self.pressure_level,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PressureState:
        def number(key: str) -> float | None:
            value = data.get(key)
            return float(value) if isinstance(value, (int, float)) else None

        return cls(
            armed=bool(data.get("armed")),
            true_count=int(data.get("true_count") or 0),
            first_true_at=number("first_true_at"),
            last_true_at=number("last_true_at"),
            false_since=number("false_since"),
            updated_at=number("updated_at") or 0.0,
            budget_steps=int(data.get("budget_steps") or 0),
            swap_ratio=number("swap_ratio"),
            pressure_level=int(data["pressure_level"]) if isinstance(data.get("pressure_level"), int) else None,
        )


def advance(
    state: PressureState,
    raw: bool,
    now: float,
    *,
    samples: int = PRESSURE_SAMPLES,
    gap_seconds: float = PRESSURE_SAMPLE_GAP_SECONDS,
    disarm_seconds: float = PRESSURE_DISARM_SECONDS,
) -> PressureState:
    """Fold one raw pressure read into the hysteresis state.

    ``samples`` reads, the first and last at least ``gap_seconds`` apart, arm;
    ``disarm_seconds`` of false reads disarm. A false read inside an armed window
    restarts the arming counter, which is what makes "two consecutive reads"
    mean consecutive.
    """
    if raw:
        if state.armed and state.false_since is not None:
            # A quiet read appeared while armed: the run of true reads restarts.
            true_count, first_true_at = 1, now
        elif state.true_count and state.false_since is None:
            true_count = state.true_count + 1
            first_true_at = state.first_true_at if state.first_true_at is not None else now
        else:
            true_count, first_true_at = 1, now
        required_gap = max(0.0, float(gap_seconds) - _GAP_EPSILON_SECONDS)
        gap = max(0.0, now - first_true_at)
        armed = state.armed or (
            true_count >= max(1, samples) and (max(1, samples) == 1 or gap >= required_gap)
        )
        return replace(
            state,
            armed=armed,
            true_count=true_count,
            first_true_at=first_true_at,
            last_true_at=now,
            false_since=None,
            updated_at=now,
        )
    if state.armed:
        false_since = state.false_since if state.false_since is not None else now
        armed = max(0.0, now - false_since) < max(0.0, float(disarm_seconds))
        return replace(
            state, armed=armed, false_since=false_since, updated_at=now,
            true_count=0, first_true_at=None,
        )
    return replace(
        state, armed=False, true_count=0, first_true_at=None,
        false_since=None, updated_at=now,
    )


def escalate(state: PressureState, *, over_budget: bool, steps: int = len(BUDGET_LADDER_MINUTES)) -> PressureState:
    """Count consecutive over-budget passes while pressure is armed (§4.4)."""
    limit = max(1, int(steps))
    return replace(state, budget_steps=min(state.budget_steps + 1, limit - 1) if over_budget else 0)


def ladder_minutes(state: PressureState, *, armed: bool) -> float | None:
    """The ladder's idle threshold for this pass, or None when it does not apply.

    Callers must only ask while ``armed`` is true: budget overrun alone never
    shortens the threshold.
    """
    if not armed:
        return None
    index = min(max(0, state.budget_steps), len(BUDGET_LADDER_MINUTES) - 1)
    return BUDGET_LADDER_MINUTES[index]


def state_path() -> Path:
    return cache_dir() / STATE_FILE


def load_state(path: Path | None = None) -> PressureState:
    """Read the carried state; an unreadable file is a fresh, unarmed state."""
    target = path or state_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return PressureState()
    return PressureState.from_dict(payload) if isinstance(payload, Mapping) else PressureState()


def save_state(state: PressureState, path: Path | None = None) -> bool:
    target = path or state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(state.to_dict(), ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)
        return True
    except OSError:
        return False


def observe(raw: bool, now: float, *, path: Path | None = None, **knobs: Any) -> PressureState:
    """Load, fold one read, save. A failed save still returns the verdict."""
    state = advance(load_state(path), raw, now, **knobs)
    save_state(state, path)
    return state


# ------------------------------------------------------------ S1: CPU time delta


@dataclass(frozen=True)
class CpuSample:
    """One host's cumulative-CPU reading, with the identity that dates it."""

    pane_pid: int
    start_offset: float  # printed ``etime`` seconds at sample time
    cpu_seconds: float
    sampled_at: float

    @property
    def start_at(self) -> float:
        return self.sampled_at - self.start_offset

    def to_dict(self) -> dict[str, float]:
        return {
            "pane_pid": float(self.pane_pid),
            "start_offset": self.start_offset,
            "cpu_seconds": self.cpu_seconds,
            "sampled_at": self.sampled_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CpuSample | None:
        try:
            return cls(
                pane_pid=int(data["pane_pid"]),
                start_offset=float(data["start_offset"]),
                cpu_seconds=float(data["cpu_seconds"]),
                sampled_at=float(data["sampled_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


def cpu_delta(previous: CpuSample, current: CpuSample) -> float | None:
    """CPU consumed between two samples of the *same* process tree, else None.

    None (unknown) for a replaced pane, a respawned process (derived start time
    moved), a non-advancing clock or a shrinking tree — the last one because a
    descendant that exited takes its CPU time out of the sum.
    """
    if previous.pane_pid != current.pane_pid:
        return None
    if current.sampled_at <= previous.sampled_at:
        return None
    if abs(current.start_at - previous.start_at) > _START_TIME_TOLERANCE_SECONDS:
        return None
    delta = current.cpu_seconds - previous.cpu_seconds
    if delta < 0:
        return None
    return delta


def _cpu_path() -> Path:
    return cache_dir() / "reclaim-cpu.json"


def load_samples(path: Path | None = None) -> dict[str, CpuSample]:
    target = path or _cpu_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    samples: dict[str, CpuSample] = {}
    for name, raw in payload.items():
        sample = CpuSample.from_dict(raw) if isinstance(raw, Mapping) else None
        if sample is not None:
            samples[str(name)] = sample
    return samples


def save_samples(samples: Mapping[str, CpuSample], path: Path | None = None) -> bool:
    target = path or _cpu_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({name: sample.to_dict() for name, sample in samples.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return True
    except OSError:
        return False


def observe_cpu(
    readings: Mapping[str, tuple[int, float, str]],
    now: float,
    *,
    path: Path | None = None,
) -> dict[str, float]:
    """(pane pid, CPU seconds, printed etime) per host -> CPU delta since last pass.

    Hosts without a comparable previous sample are absent from the result, which
    reads as ``unknown`` at the decision: it neither vetoes nor permits.
    """
    from corral.busycheck import cpu_seconds as parse_time

    previous = load_samples(path)
    current: dict[str, CpuSample] = {}
    deltas: dict[str, float] = {}
    for name, (pane_pid, cpu_total, etimes) in readings.items():
        offset = parse_time(etimes)
        sample = CpuSample(
            pane_pid=int(pane_pid),
            start_offset=offset if offset is not None else -1.0,
            cpu_seconds=float(cpu_total),
            sampled_at=float(now),
        )
        if offset is None:
            continue  # unreadable elapsed time: no sample, so next pass is unknown too
        current[name] = sample
        before = previous.get(name)
        if before is None:
            continue
        delta = cpu_delta(before, sample)
        if delta is not None:
            deltas[name] = delta
    save_samples(current, path)
    return deltas
