"""E4: replay labeled drafting traces against the proposed X wake-up policy.

This is a design experiment, not product scheduler code. Timestamps are simulated so a
long pause and a busy X can be examined without waiting in real time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Change:
    before: str
    after: str
    ready_at: int


@dataclass
class Replay:
    idle_ms: int
    running_ms: int
    x_ms: int = 20_000
    current: dict[str, str] = field(default_factory=dict)
    pending: dict[str, Change] = field(default_factory=dict)
    worker_events: list[str] = field(default_factory=list)
    rounds: list[dict] = field(default_factory=list)
    busy_until: int = 0

    @staticmethod
    def meaningful(before: str, after: str) -> bool:
        # Whitespace-only edits do not change meaning; strike markers are preserved.
        return " ".join(before.split()) != " ".join(after.split())

    def event(self, e: dict) -> None:
        t, kind = e["at"], e["kind"]
        if kind == "edit":
            block, after = e["block"], e["text"]
            before = self.current.get(block, "")
            self.current[block] = after
            if block in self.pending:
                before = self.pending[block].before
            if self.meaningful(before, after):
                delay = self.running_ms if e.get("running") else self.idle_ms
                self.pending[block] = Change(before, after, t + delay)
            else:
                self.pending.pop(block, None)
        elif kind == "leave":
            if e["block"] in self.pending:
                self.pending[e["block"]].ready_at = t
        elif kind == "worker":
            self.worker_events.append(e["name"])
        else:
            raise ValueError(f"unknown event: {kind}")

    def drain_to(self, end: int) -> None:
        while True:
            ready_at = min((c.ready_at for c in self.pending.values()), default=end + 1)
            if self.worker_events:
                ready_at = min(ready_at, self.busy_until)
            at = max(ready_at, self.busy_until)
            if at > end:
                return
            blocks = {key: vars(c) for key, c in self.pending.items() if c.ready_at <= at}
            if not blocks and not self.worker_events:
                return
            for key in blocks:
                del self.pending[key]
            self.rounds.append({"at": at, "blocks": blocks, "worker_events": self.worker_events[:]})
            self.worker_events.clear()
            self.busy_until = at + self.x_ms


# "complete_at" is an evaluation label, not input to the scheduler. It lets the
# experiment count wake-ups before the owner had finished expressing an idea.
TRACES = [
    {
        "name": "continuous_draft",
        "complete_at": {"a": 11_000},
        "events": [
            {"at": t, "kind": "edit", "block": "a", "text": "Corral 手机端搜索"[:n]}
            for t, n in [(0, 1), (1000, 2), (1800, 3), (2800, 4), (3400, 5),
                         (4200, 6), (6000, 7), (7200, 8), (9000, 9), (11_000, 10)]
        ] + [{"at": 12_000, "kind": "leave", "block": "a"}],
    },
    {
        "name": "thinking_pause_7s",
        "complete_at": {"a": 11_500},
        "events": [
            {"at": 0, "kind": "edit", "block": "a", "text": "Corral"},
            {"at": 4500, "kind": "edit", "block": "a", "text": "Corral 手机"},
            {"at": 11_500, "kind": "edit", "block": "a", "text": "Corral 手机端会话搜索"},
            {"at": 16_000, "kind": "leave", "block": "a"},
        ],
    },
    {
        "name": "thinking_pause_10s",
        "complete_at": {"a": 10_500},
        "events": [
            {"at": 0, "kind": "edit", "block": "a", "text": "Corral"},
            {"at": 10_500, "kind": "edit", "block": "a", "text": "Corral 手机端会话搜索"},
            {"at": 16_000, "kind": "leave", "block": "a"},
        ],
    },
    {
        "name": "running_worker_edit",
        "complete_at": {"a": 2500},
        "events": [
            {"at": 0, "kind": "edit", "block": "a", "text": "加搜索", "running": True},
            {"at": 2500, "kind": "edit", "block": "a", "text": "加搜索，并跟随系统", "running": True},
            {"at": 3500, "kind": "leave", "block": "a"},
        ],
    },
    {
        "name": "busy_x_merges_edits_and_worker_event",
        "complete_at": {"a": 1000, "b": 9000},
        "events": [
            {"at": 0, "kind": "edit", "block": "a", "text": "task one"},
            {"at": 1000, "kind": "leave", "block": "a"},
            {"at": 3000, "kind": "edit", "block": "b", "text": "task two"},
            {"at": 7000, "kind": "edit", "block": "b", "text": "task two revised"},
            {"at": 9000, "kind": "edit", "block": "b", "text": "task two final"},
            {"at": 10_000, "kind": "leave", "block": "b"},
            {"at": 11_000, "kind": "worker", "name": "t1 turn finished"},
        ],
    },
    {
        "name": "whitespace_then_strike",
        "complete_at": {"a": 1000},
        "initial": {"a": "Corral add search"},
        "events": [
            {"at": 0, "kind": "edit", "block": "a", "text": "Corral  add search"},
            {"at": 1000, "kind": "edit", "block": "a", "text": "Corral ~~add~~ search"},
            {"at": 1100, "kind": "leave", "block": "a"},
        ],
    },
]


def replay(trace: dict, idle_ms: int, running_ms: int) -> dict:
    state = Replay(idle_ms, running_ms, current=trace.get("initial", {}).copy())
    for e in sorted(trace["events"], key=lambda value: value["at"]):
        # Input at a deadline wins over the timer at that same timestamp.
        state.drain_to(e["at"] - 1)
        state.event(e)
        state.drain_to(e["at"])
    state.drain_to(trace["events"][-1]["at"] + 60_000)
    premature = [{"at": r["at"], "block": block} for r in state.rounds for block in r["blocks"]
                 if r["at"] < trace["complete_at"][block]]
    latency = {}
    for block, complete_at in trace["complete_at"].items():
        after = [r["at"] for r in state.rounds if block in r["blocks"] and r["at"] >= complete_at]
        latency[block] = after[0] - complete_at if after else None
    return {"rounds": state.rounds, "premature": premature,
            "first_after_complete_ms": latency}


def main() -> None:
    results = {}
    for idle, running in [(3000, 1500), (8000, 1500), (8000, 3000), (12_000, 3000)]:
        key = f"idle_{idle}_running_{running}"
        results[key] = {trace["name"]: replay(trace, idle, running) for trace in TRACES}
        total = sum(len(v["rounds"]) for v in results[key].values())
        early = sum(len(v["premature"]) for v in results[key].values())
        print(f"{key}: {total} X rounds, {early} before labeled idea completion")
    base = results["idle_8000_running_3000"]
    assert len(base["busy_x_merges_edits_and_worker_event"]["rounds"]) == 2
    assert base["busy_x_merges_edits_and_worker_event"]["rounds"][1]["worker_events"] == ["t1 turn finished"]
    assert len(base["whitespace_then_strike"]["rounds"]) == 1
    out = Path(__file__).parent / "results" / "e4-trigger-replay.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(out)


if __name__ == "__main__":
    main()
