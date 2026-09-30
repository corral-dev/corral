"""Synthetic compatibility baseline for Codex's phone-facing RichReader output.

Fixtures use real Codex rollout shapes (response_item message/function_call/
custom_tool_call/outputs, event_msg agent_message/user_message/task_complete).
They pin the SessKit-projection wire contract; a legacy-vs-new wire
comparison on the same fixtures guards the migration parity gate.
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
    return {"source": "codex", "path": str(path), "id": "synthetic-codex-session"}


def _user_item(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _assistant_item(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _function_call(name: str, call_id: str, args: dict) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": name,
            "call_id": call_id,
            "arguments": json.dumps(args, ensure_ascii=False),
        },
    }


def _function_output(call_id: str, output: str) -> dict:
    return {
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": call_id, "output": output},
    }


def _custom_call(name: str, call_id: str, raw_input: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call",
            "name": name,
            "call_id": call_id,
            "input": raw_input,
        },
    }


def _custom_output(call_id: str, output: str) -> dict:
    return {
        "type": "response_item",
        "payload": {"type": "custom_tool_call_output", "call_id": call_id, "output": output},
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


class CodexRichmsgCompatibilityBaselineTests(unittest.TestCase):
    def test_text_and_calls_share_turn_cards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _user_item("Fix the bug."),
                    _assistant_item("On it."),
                    _function_call("apply_patch", "edit-1", {"path": "/proj/main.py"}),
                    _custom_call("shell", "shell-1", 'tools.exec_command({"cmd":"npm test"})'),
                ],
            )
            messages = richmsg.RichReader(_session(path)).read_all()
            wire = [item.to_wire_dict() for item in messages]

        self.assertEqual(
            [(item["role"], len(item.get("tools", []))) for item in wire],
            [("user", 0), ("assistant", 2)],
        )
        edit, shell = wire[1]["tools"]
        self.assertEqual((edit["id"], edit["kind"]), ("edit-1", "edit"))
        self.assertIn("main.py", edit["summary"])
        self.assertEqual((shell["id"], shell["kind"]), ("shell-1", "shell"))
        self.assertIn("npm test", shell["summary"])
        self.assertEqual([tool.status for tool in messages[1].tools], ["running", "running"])

    def test_results_pair_and_reuse_host_seq(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            prefix = [
                _user_item("Run checks."),
                _assistant_item("Running."),
                _function_call("apply_patch", "edit-1", {"path": "/proj/main.py"}),
                _custom_call("shell", "shell-1", 'tools.exec_command({"cmd":"npm test"})'),
            ]
            _write(path, prefix)
            reader = richmsg.RichReader(_session(path))
            first = reader.poll()
            host_seq = first[1].seq

            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(_function_output("edit-1", "patched")) + "\n")
            second = reader.poll()
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0].seq, host_seq)
            self.assertEqual([tool.status for tool in second[0].tools], ["ok", "running"])

            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(_custom_output("shell-1", "exit code: 1\nfailed")) + "\n"
                )
            third = reader.poll()

        self.assertEqual(len(third), 1)
        self.assertEqual(third[0].seq, host_seq)
        self.assertEqual([tool.status for tool in third[0].tools], ["ok", "error"])
        [detail] = third[0].tool_detail_page(tool_id="shell-1")["tools"]
        self.assertIn("exit code: 1", detail["output"])

    def test_result_pairs_across_backward_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            entries: list[dict] = []
            for index in range(6):
                entries.append(_user_item(f"Task {index}."))
                entries.append(_assistant_item(f"Working on {index}."))
                entries.append(
                    _function_call("apply_patch", f"call-{index}", {"path": f"/proj/{index}.py"})
                )
                entries.append(_function_output(f"call-{index}", "patched"))
            _write(path, entries)
            messages = _full_history(path, limit=2)

        self.assertEqual(len(messages), 12)
        for item in messages:
            if item.role == "assistant" and item.tools:
                self.assertEqual([tool.status for tool in item.tools], ["ok"])

    def test_task_complete_error_is_preserved_as_assistant_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _user_item("Continue."),
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "last_agent_message": None,
                            "error": {
                                "message": "Quota exceeded for the day.",
                                "codex_error_info": "usage_limit_exceeded",
                            },
                        },
                    },
                ],
            )
            messages = richmsg.RichReader(_session(path)).read_all()
            wire = [item.to_wire_dict() for item in messages]

        self.assertEqual(
            [(item["role"], item.get("text")) for item in wire],
            [("user", "Continue."), ("assistant", "Quota exceeded for the day.")],
        )

    def test_turn_aborted_and_injected_rows_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "# AGENTS.md instructions\nsecret"}
                            ],
                        },
                    },
                    _user_item("Real question."),
                    {"type": "event_msg", "payload": {"type": "turn_aborted", "reason": "stop"}},
                    {
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": "Done soon."},
                    },
                ],
            )
            messages = richmsg.RichReader(_session(path)).read_all()

        self.assertEqual(
            [(item.role, item.text) for item in messages],
            [("user", "Real question."), ("assistant", "Done soon.")],
        )

    def test_request_user_input_sync_and_async_questions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _user_item("Deploy?"),
                    _function_call(
                        "request_user_input",
                        "ask-1",
                        {
                            "questions": [
                                {
                                    "id": "q1",
                                    "question": "Proceed?",
                                    "header": "Deploy",
                                    "options": [
                                        {"label": "Yes", "description": "Ship it"},
                                        {"label": "No"},
                                    ],
                                }
                            ]
                        },
                    ),
                    _function_call(
                        "request_user_input_async",
                        "ask-2",
                        {
                            "questions": [
                                {"title": "Mode", "options": ["Fast", "Careful"]},
                            ]
                        },
                    ),
                ],
            )
            messages = richmsg.RichReader(_session(path)).read_all()
            wire = [item.to_wire_dict() for item in messages]
            prompts = richmsg.pending_prompts_from_messages(messages)

        tools = wire[1]["tools"]
        self.assertEqual([tool["id"] for tool in tools], ["ask-1", "ask-2"])
        self.assertEqual(tools[0]["kind"], "question")
        self.assertEqual(tools[0]["options"], ["Yes", "No"])
        self.assertEqual(tools[0]["status"], "running")
        self.assertEqual(tools[1]["kind"], "question")
        self.assertEqual(tools[1]["options"], ["Fast", "Careful"])
        self.assertEqual([prompt["id"] for prompt in prompts], ["ask-1", "ask-2"])

    def test_fixture_wire_matches_legacy_parser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            _write(
                path,
                [
                    _user_item("Go."),
                    _assistant_item("Working."),
                    _function_call("apply_patch", "edit-1", {"path": "/proj/main.py"}),
                    _custom_call(
                        "shell", "shell-1", 'tools.exec_command({"cmd":"npm test"})'
                    ),
                    _function_output("edit-1", "patched"),
                    _custom_output("shell-1", "ok"),
                    _function_call(
                        "request_user_input",
                        "ask-1",
                        {"questions": [{"id": "q", "question": "More?", "options": ["Yes"]}]},
                    ),
                ],
            )
            new_wire = _sig(_full_history(path))
            legacy_wire = _legacy_wire(path)

        self.assertEqual(new_wire, legacy_wire)


if __name__ == "__main__":
    unittest.main()
