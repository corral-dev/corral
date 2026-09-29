"""Synthetic compatibility baseline for Pi's phone-facing RichReader output.

Question-shaped tool inputs here are Corral extension fixtures. They do not claim
that Pi's official native format defines a question/approval tool.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from corral.remote import richmsg


def _entry(
    entry_id: str,
    parent_id: str | None,
    role: str,
    content: object,
    timestamp: str,
    **message_fields: object,
) -> dict:
    message = {"role": role, "content": content, **message_fields}
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": timestamp,
        "message": message,
    }


def _write(path: Path, entries: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in entries),
        encoding="utf-8",
    )


def _session(path: Path) -> dict:
    return {"source": "pi", "path": str(path), "id": "synthetic-pi-session"}


def _header() -> dict:
    return {
        "type": "session",
        "id": "synthetic-pi-session",
        "timestamp": "2026-09-01T00:00:00Z",
        "cwd": "/tmp/pi-projection-fixture",
    }


def _wire(messages: list[richmsg.RichMessage]) -> list[dict]:
    return [message.to_dict() for message in messages]


class PiRichmsgCompatibilityBaselineTests(unittest.TestCase):
    def test_assistant_text_and_two_tools_share_one_phone_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _header(),
                    _entry(
                        "u1",
                        None,
                        "user",
                        [{"type": "text", "text": "Check the fixture."}],
                        "2026-09-01T00:00:01Z",
                    ),
                    _entry(
                        "a1",
                        "u1",
                        "assistant",
                        [
                            {"type": "text", "text": "I will inspect two files."},
                            {
                                "type": "toolCall",
                                "id": "read-1",
                                "name": "read",
                                "arguments": {"path": "/tmp/demo.txt"},
                            },
                            {
                                "type": "toolCall",
                                "id": "bash-1",
                                "name": "bash",
                                "arguments": {"command": "printf demo"},
                            },
                        ],
                        "2026-09-01T00:00:02Z",
                    ),
                ],
            )
            messages = richmsg.RichReader(_session(path)).read_all()
            wire = _wire(messages)

        self.assertEqual([(item["seq"], item["role"]) for item in wire], [(1, "user"), (2, "assistant")])
        self.assertEqual(wire[1]["text"], "I will inspect two files.")
        self.assertEqual(wire[1]["ts"], 1_788_220_802.0)
        self.assertEqual([tool["id"] for tool in wire[1]["tools"]], ["read-1", "bash-1"])
        read_tool, bash_tool = wire[1]["tools"]
        self.assertEqual(
            (read_tool["name"], read_tool["kind"], read_tool["summary"]), ("read", "read", "read demo.txt")
        )
        self.assertEqual(json.loads(read_tool["detail"])["path"], "/tmp/demo.txt")
        self.assertEqual((bash_tool["kind"], bash_tool["summary"]), ("shell", "printf demo"))
        self.assertEqual(bash_tool["detail"], '{\n  "command": "printf demo"\n}')
        self.assertEqual([tool.status for tool in messages[1].tools], ["running", "running"])

    def test_results_arrive_in_steps_and_explicit_error_reuses_host_seq(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            prefix = [
                _header(),
                _entry("u1", None, "user", [{"type": "text", "text": "Run checks."}], "2026-09-01T00:00:01Z"),
                _entry(
                    "a1",
                    "u1",
                    "assistant",
                    [
                        {"type": "text", "text": "Running both checks."},
                        {"type": "toolCall", "id": "check-ok", "name": "bash", "arguments": {"command": "check-ok"}},
                        {
                            "type": "toolCall",
                            "id": "check-fail",
                            "name": "bash",
                            "arguments": {"command": "check-fail"},
                        },
                    ],
                    "2026-09-01T00:00:02Z",
                ),
            ]
            _write(path, prefix)
            reader = richmsg.RichReader(_session(path))
            first_messages = reader.poll()
            first = _wire(first_messages)
            self.assertEqual(first[1]["seq"], 2)
            self.assertEqual([tool.status for tool in first_messages[1].tools], ["running", "running"])

            _write(
                path,
                prefix
                + [
                    _entry(
                        "tr-ok",
                        "a1",
                        "toolResult",
                        [{"type": "text", "text": "all checks passed"}],
                        "2026-09-01T00:00:03Z",
                        toolCallId="check-ok",
                        toolName="bash",
                        isError=False,
                    ),
                ],
            )
            second_messages = reader.poll()
            second = _wire(second_messages)
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0]["seq"], 2)
            self.assertEqual([tool.status for tool in second_messages[0].tools], ["ok", "running"])
            self.assertEqual(second[0]["tools"][0]["output"], "all checks passed")

            _write(
                path,
                prefix
                + [
                    _entry(
                        "tr-ok",
                        "a1",
                        "toolResult",
                        [{"type": "text", "text": "all checks passed"}],
                        "2026-09-01T00:00:03Z",
                        toolCallId="check-ok",
                        toolName="bash",
                        isError=False,
                    ),
                    _entry(
                        "tr-fail",
                        "tr-ok",
                        "toolResult",
                        [{"type": "text", "text": "Error: synthetic failure"}],
                        "2026-09-01T00:00:04Z",
                        toolCallId="check-fail",
                        toolName="bash",
                        isError=True,
                    ),
                ],
            )
            third_messages = reader.poll()
            third = _wire(third_messages)

        self.assertEqual(len(third), 1)
        self.assertEqual(third[0]["seq"], 2)
        self.assertEqual([tool["id"] for tool in third[0]["tools"]], ["check-ok", "check-fail"])
        self.assertEqual([tool.status for tool in third_messages[0].tools], ["ok", "error"])
        self.assertEqual(third[0]["tools"][1]["output"], "Error: synthetic failure")

    def test_question_extension_fixture_preserves_single_and_grouped_choices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _header(),
                    _entry("u1", None, "user", [{"type": "text", "text": "Choose a plan."}], "2026-09-01T00:00:01Z"),
                    _entry(
                        "a1",
                        "u1",
                        "assistant",
                        [
                            {"type": "text", "text": "I need two decisions."},
                            {
                                "type": "toolCall",
                                "id": "question-one",
                                "name": "AskUserQuestion",
                                "arguments": {
                                    "question": "Choose a color",
                                    "options": [{"label": "Blue"}, {"label": "Green"}],
                                },
                            },
                            {
                                "type": "toolCall",
                                "id": "question-many",
                                "name": "AskUserQuestion",
                                "arguments": {
                                    "questions": [
                                        {
                                            "question": "Choose a shape",
                                            "options": [{"label": "Circle"}, {"label": "Square"}],
                                        },
                                        {"header": "Delivery", "choices": ["Fast", "Careful"]},
                                    ]
                                },
                            },
                        ],
                        "2026-09-01T00:00:02Z",
                    ),
                ],
            )
            messages = richmsg.RichReader(_session(path)).read_all()
            wire = _wire(messages)
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
                {"summary": "Delivery", "options": ["Fast", "Careful"]},
            ],
        )
        self.assertEqual([prompt["id"] for prompt in prompts], ["question-one", "question-many:0", "question-many:1"])
        self.assertEqual(
            [prompt["options"] for prompt in prompts], [["Blue", "Green"], ["Circle", "Square"], ["Fast", "Careful"]]
        )

    def test_only_active_parent_chain_is_projected_and_branch_switch_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            common = [
                _header(),
                _entry("u1", None, "user", [{"type": "text", "text": "Start."}], "2026-09-01T00:00:01Z"),
                _entry(
                    "a-old", "u1", "assistant", [{"type": "text", "text": "Abandoned answer."}], "2026-09-01T00:00:02Z"
                ),
                _entry(
                    "u-new",
                    "u1",
                    "user",
                    [{"type": "text", "text": "Continue on active branch."}],
                    "2026-09-01T00:00:03Z",
                ),
                _entry(
                    "a-new", "u-new", "assistant", [{"type": "text", "text": "Active answer."}], "2026-09-01T00:00:04Z"
                ),
            ]
            _write(path, common)
            active = _wire(richmsg.RichReader(_session(path)).read_all())
            self.assertEqual(
                [item.get("text", "") for item in active], ["Start.", "Continue on active branch.", "Active answer."]
            )
            self.assertNotIn("Abandoned answer.", json.dumps(active))

            switched = common + [
                _entry(
                    "a-switched",
                    "u1",
                    "assistant",
                    [{"type": "text", "text": "Switched answer."}],
                    "2026-09-01T00:00:05Z",
                ),
            ]
            _write(path, switched)
            after_switch = _wire(richmsg.RichReader(_session(path)).read_all())

        self.assertEqual([item.get("text", "") for item in after_switch], ["Start.", "Switched answer."])
        self.assertNotIn("Abandoned answer.", json.dumps(after_switch))
        self.assertNotIn("Active answer.", json.dumps(after_switch))

    def test_empty_body_agent_error_is_preserved_as_assistant_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _header(),
                    _entry("u1", None, "user", [{"type": "text", "text": "Continue."}], "2026-09-01T00:00:01Z"),
                    _entry(
                        "a-error",
                        "u1",
                        "assistant",
                        [],
                        "2026-09-01T00:00:02Z",
                        stopReason="error",
                        errorMessage="Synthetic provider error.",
                    ),
                ],
            )
            wire = _wire(richmsg.RichReader(_session(path)).read_all())

        self.assertEqual(
            [(item["seq"], item["role"], item.get("text")) for item in wire],
            [
                (1, "user", "Continue."),
                (2, "assistant", "Synthetic provider error."),
            ],
        )

    def test_pi_ask_parent_is_not_a_phone_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _header(),
                    _entry(
                        "u1",
                        None,
                        "user",
                        [{"type": "text", "text": "Coordinate with parent."}],
                        "2026-09-01T00:00:01Z",
                    ),
                    _entry(
                        "a1",
                        "u1",
                        "assistant",
                        [
                            {
                                "type": "toolCall",
                                "id": "parent-1",
                                "name": "ask_parent",
                                "arguments": {"task": "Report progress"},
                            }
                        ],
                        "2026-09-01T00:00:02Z",
                    ),
                ],
            )
            messages = richmsg.RichReader(_session(path)).read_all()

        tool = messages[-1].tools[0]
        self.assertEqual(tool.name, "ask_parent")
        self.assertEqual(tool.kind, "other")
        self.assertEqual(richmsg.pending_prompts_from_messages(messages), [])


if __name__ == "__main__":
    unittest.main()
