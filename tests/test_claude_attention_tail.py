from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from corral.attention_signals import _JSONL_TAIL_BYTES, inspect_session


def _assistant(*parts: dict) -> dict:
    return {
        "type": "assistant", "uuid": json.dumps(parts)[:24],
        "message": {"role": "assistant", "content": list(parts)},
    }


def _tool_result(tool_id: str, payload: str = "ok") -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": payload}]},
    }


class ClaudeAttentionTailTests(unittest.TestCase):
    def _inspect(self, entries: list[dict], *, live: bool = True) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")
            return inspect_session({"source": "claude", "id": "s", "path": str(path), "live": live}).phase

    def test_tool_only_round_is_working(self) -> None:
        entries = [
            {"type": "user", "message": {"role": "user", "content": "run the build"}},
            _assistant({"type": "thinking", "thinking": "plan"}),
            _assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
            _tool_result("t1"),
            _assistant({"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}),
        ]
        self.assertEqual(self._inspect(entries), "working")

    def test_huge_image_result_does_not_evict_working_evidence(self) -> None:
        image = "A" * (_JSONL_TAIL_BYTES + 64 * 1024)
        entries = [
            {"type": "user", "message": {"role": "user", "content": "look at the screenshot"}},
            _assistant({"type": "text", "text": "Reading it."}),
            _assistant({"type": "tool_use", "id": "t1", "name": "Read", "input": {}}),
            _tool_result("t1", image),
        ]
        self.assertEqual(self._inspect(entries), "working")

    def test_turn_end_still_settles_idle(self) -> None:
        entries = [
            _assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}),
            _tool_result("t1"),
            _assistant({"type": "text", "text": "Done."}),
            {"type": "system", "subtype": "turn_duration", "durationMs": 1000},
        ]
        self.assertEqual(self._inspect(entries), "idle")

    def test_dead_process_is_not_working(self) -> None:
        entries = [_assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {}})]
        self.assertEqual(self._inspect(entries, live=False), "idle")


if __name__ == "__main__":
    unittest.main()
