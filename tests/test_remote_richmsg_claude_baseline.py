"""Synthetic compatibility baseline for Claude's phone-facing RichReader output.

Fixtures use real Claude Code history shapes (user/assistant entries with
list content, tool_use/tool_result parts, system upstream errors). They pin
the SessKit-projection wire contract; a legacy-vs-new wire comparison on the
same fixtures guards the migration parity gate.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from corral.remote import richmsg


def _write(path: Path, entries: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in entries),
        encoding="utf-8",
    )


def _session(path: Path) -> dict:
    return {"source": "claude", "path": str(path), "id": "synthetic-claude-session"}


def _user(text: str, ts: str, **extra: object) -> dict:
    return {
        "type": "user",
        "uuid": f"u-{ts}",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
        **extra,
    }


def _assistant(parts: list, ts: str) -> dict:
    return {
        "type": "assistant",
        "uuid": f"a-{ts}",
        "timestamp": ts,
        "message": {"role": "assistant", "content": parts},
    }


def _result(call_id: str, content: object, ts: str, *, is_error: bool | None = None) -> dict:
    part: dict = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if is_error is not None:
        part["is_error"] = is_error
    return {
        "type": "user",
        "uuid": f"r-{ts}",
        "timestamp": ts,
        "message": {"role": "user", "content": [part]},
    }


def _sig(messages: list[richmsg.RichMessage]) -> list:
    out = []
    for item in messages:
        wire = item.to_wire_dict()
        tools = tuple(
            (
                tool.get("id"),
                tool.get("name"),
                tool.get("kind"),
                tool.get("summary"),
                tool.get("status"),
                str(tool.get("options")),
                str(tool.get("questions")),
            )
            for tool in wire.get("tools", [])
        )
        out.append((wire.get("role"), wire.get("text"), tools))
    return out


def _full_history(path: Path, *, limit: int = 80) -> list[richmsg.RichMessage]:
    reader = richmsg.RichReader(_session(path))
    messages = reader.read_all(limit=limit)
    while reader.has_earlier():
        older_than = min(item.seq for item in messages)
        earlier = reader.read_earlier(limit, before_seq=older_than)
        if not earlier:
            break
        messages = earlier + messages
    return messages


def _legacy_wire(path: Path) -> list:
    saved = dict(richmsg._SESSKIT_READER_AVAILABLE)
    richmsg._SESSKIT_READER_AVAILABLE.update({"claude": False, "codex": False})
    try:
        return _sig(_full_history(path))
    finally:
        richmsg._SESSKIT_READER_AVAILABLE.clear()
        richmsg._SESSKIT_READER_AVAILABLE.update(saved)


class ClaudeRichmsgCompatibilityBaselineTests(unittest.TestCase):
    def test_assistant_text_and_two_tools_share_one_phone_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _user("Check the fixture.", "2026-09-01T00:00:01Z"),
                    _assistant(
                        [
                            {"type": "text", "text": "I will inspect two files."},
                            {
                                "type": "tool_use",
                                "id": "read-1",
                                "name": "Read",
                                "input": {"file_path": "/tmp/demo.txt"},
                            },
                            {
                                "type": "tool_use",
                                "id": "bash-1",
                                "name": "Bash",
                                "input": {"command": "printf demo"},
                            },
                        ],
                        "2026-09-01T00:00:02Z",
                    ),
                ],
            )
            messages = richmsg.RichReader(_session(path)).read_all()
            wire = [item.to_wire_dict() for item in messages]

        self.assertEqual(
            [(item["seq"], item["role"]) for item in wire], [(1, "user"), (2, "assistant")]
        )
        self.assertEqual(wire[1]["text"], "I will inspect two files.")
        self.assertEqual([tool["id"] for tool in wire[1]["tools"]], ["read-1", "bash-1"])
        read_tool, bash_tool = wire[1]["tools"]
        self.assertEqual(
            (read_tool["name"], read_tool["kind"], read_tool["summary"]),
            ("Read", "read", "Read demo.txt"),
        )
        self.assertNotIn("status", read_tool)
        self.assertTrue(read_tool["has_detail"])
        self.assertEqual((bash_tool["kind"], bash_tool["summary"]), ("shell", "printf demo"))
        self.assertEqual([tool.status for tool in messages[1].tools], ["running", "running"])

    def test_results_arrive_in_steps_and_reuse_host_seq(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            prefix = [
                _user("Run checks.", "2026-09-01T00:00:01Z"),
                _assistant(
                    [
                        {"type": "text", "text": "Running both checks."},
                        {
                            "type": "tool_use",
                            "id": "check-ok",
                            "name": "Bash",
                            "input": {"command": "check-ok"},
                        },
                        {
                            "type": "tool_use",
                            "id": "check-fail",
                            "name": "Bash",
                            "input": {"command": "check-fail"},
                        },
                    ],
                    "2026-09-01T00:00:02Z",
                ),
            ]
            _write(path, prefix)
            reader = richmsg.RichReader(_session(path))
            first = reader.poll()
            self.assertEqual([tool.status for tool in first[1].tools], ["running", "running"])

            _write(path, prefix + [_result("check-ok", "all checks passed", "2026-09-01T00:00:03Z")])
            second = reader.poll()
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0].seq, first[1].seq)
            self.assertEqual([tool.status for tool in second[0].tools], ["ok", "running"])
            [ok_detail] = second[0].tool_detail_page(tool_id="check-ok")["tools"]
            self.assertEqual(ok_detail["output"], "all checks passed")

            _write(
                path,
                prefix
                + [_result("check-ok", "all checks passed", "2026-09-01T00:00:03Z")]
                + [
                    _result(
                        "check-fail",
                        "Error: synthetic failure",
                        "2026-09-01T00:00:04Z",
                        is_error=True,
                    )
                ],
            )
            third = reader.poll()

        self.assertEqual(len(third), 1)
        self.assertEqual(third[0].seq, first[1].seq)
        self.assertEqual([tool.status for tool in third[0].tools], ["ok", "error"])
        [error_detail] = third[0].tool_detail_page(tool_id="check-fail")["tools"]
        self.assertEqual(error_detail["output"], "Error: synthetic failure")

    def test_result_pairs_across_backward_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            entries: list[dict] = []
            for index in range(6):
                entries.append(_user(f"Task {index}.", f"2026-09-01T00:00:{index:02d}Z"))
                entries.append(
                    _assistant(
                        [
                            {"type": "text", "text": f"Working on {index}."},
                            {
                                "type": "tool_use",
                                "id": f"call-{index}",
                                "name": "Bash",
                                "input": {"command": f"run-{index}"},
                            },
                        ],
                        f"2026-09-01T00:01:{index:02d}Z",
                    )
                )
                entries.append(
                    _result(f"call-{index}", f"done {index}", f"2026-09-01T00:02:{index:02d}Z")
                )
            _write(path, entries)
            messages = _full_history(path, limit=2)

        self.assertEqual(len(messages), 12)
        for item in messages:
            if item.role == "assistant" and item.tools:
                self.assertEqual([tool.status for tool in item.tools], ["ok"])

    def test_upstream_system_error_is_preserved_as_assistant_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _user("Continue.", "2026-09-01T00:00:01Z"),
                    {
                        "type": "system",
                        "timestamp": "2026-09-01T00:00:02Z",
                        "error": {"formatted": "401 API key is invalid.", "status": 401},
                    },
                ],
            )
            messages = richmsg.RichReader(_session(path)).read_all()
            wire = [item.to_wire_dict() for item in messages]

        self.assertEqual(
            [(item["role"], item.get("text")) for item in wire],
            [("user", "Continue."), ("assistant", "401 API key is invalid.")],
        )

    def test_aborted_user_turn_and_injected_rows_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _user("# AGENTS.md instructions\nDo secret things.", "2026-09-01T00:00:01Z"),
                    _user("Real question.", "2026-09-01T00:00:02Z"),
                    _user("[Request interrupted by user]", "2026-09-01T00:00:03Z"),
                    _assistant([{"type": "text", "text": "Answer."}], "2026-09-01T00:00:04Z"),
                ],
            )
            messages = richmsg.RichReader(_session(path)).read_all()

        self.assertEqual(
            [(item.role, item.text) for item in messages],
            [("user", "Real question."), ("assistant", "Answer.")],
        )

    def test_ask_user_question_options_and_pending_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _user("Choose a plan.", "2026-09-01T00:00:01Z"),
                    _assistant(
                        [
                            {"type": "text", "text": "I need two decisions."},
                            {
                                "type": "tool_use",
                                "id": "question-one",
                                "name": "AskUserQuestion",
                                "input": {
                                    "questions": [
                                        {
                                            "question": "Choose a color",
                                            "header": "Color",
                                            "options": [
                                                {"label": "Blue", "description": "Calm"},
                                                {"label": "Green"},
                                            ],
                                            "multiSelect": False,
                                        }
                                    ]
                                },
                            },
                            {
                                "type": "tool_use",
                                "id": "question-many",
                                "name": "AskUserQuestion",
                                "input": {
                                    "questions": [
                                        {
                                            "question": "Choose a shape",
                                            "options": [{"label": "Circle"}, {"label": "Square"}],
                                        },
                                        {"header": "Delivery", "options": [{"label": "Fast"}]},
                                    ]
                                },
                            },
                        ],
                        "2026-09-01T00:00:02Z",
                    ),
                ],
            )
            messages = richmsg.RichReader(_session(path)).read_all()
            wire = [item.to_wire_dict() for item in messages]
            prompts = richmsg.pending_prompts_from_messages(messages)

        tools = wire[1]["tools"]
        self.assertEqual([tool["id"] for tool in tools], ["question-one", "question-many"])
        self.assertEqual(tools[0]["kind"], "question")
        self.assertEqual(tools[0]["status"], "running")
        self.assertEqual(tools[0]["options"], ["Blue", "Green"])
        self.assertNotIn("questions", tools[0])
        self.assertNotIn("options", tools[1])
        self.assertEqual(
            tools[1]["questions"],
            [
                {"summary": "Choose a shape", "options": ["Circle", "Square"]},
                {"summary": "Delivery", "options": ["Fast"]},
            ],
        )
        self.assertEqual(
            [prompt["id"] for prompt in prompts],
            ["question-one", "question-many:0", "question-many:1"],
        )

    def test_fixture_wire_matches_legacy_parser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _user("Hello.", "2026-09-01T00:00:01Z"),
                    _assistant(
                        [
                            {"type": "text", "text": "Hi there."},
                            {
                                "type": "tool_use",
                                "id": "q-1",
                                "name": "AskUserQuestion",
                                "input": {
                                    "questions": [
                                        {
                                            "question": "Proceed?",
                                            "options": [{"label": "Yes"}, {"label": "No"}],
                                        }
                                    ]
                                },
                            },
                        ],
                        "2026-09-01T00:00:02Z",
                    ),
                    _result("q-1", "Yes", "2026-09-01T00:00:03Z"),
                    {
                        "type": "system",
                        "timestamp": "2026-09-01T00:00:04Z",
                        "error": {"formatted": "Connection dropped.", "status": 0},
                    },
                    _assistant([{"type": "text", "text": "Done."}], "2026-09-01T00:00:05Z"),
                ],
            )
            new_wire = _sig(_full_history(path))
            legacy_wire = _legacy_wire(path)

        self.assertEqual(new_wire, legacy_wire)


if __name__ == "__main__":
    unittest.main()
