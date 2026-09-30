"""P2: Cursor/OpenCode phone cards project SessKit typed activity.

Covers the Corral-side contract for the migration (fixtures only, no real
history): text+tools grouping, result pairing across polls and pages,
tool success/error, OpenCode question tool as a question card, Cursor
injected-context filtering, error-only turn hiding (OpenCode), and the
prompt-only Cursor fallback (pending SessKit F1 — skipped until the SessKit
reader emits it).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral.remote import richmsg


def _session(source: str, path: Path, session_id: str = "session-1") -> dict:
    return {"source": source, "path": str(path), "id": session_id}


def _cursor_db(path: Path, objects: list[dict]) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    for index, obj in enumerate(objects):
        connection.execute(
            "INSERT INTO blobs VALUES (?, ?)",
            (f"blob-{index:04d}", json.dumps(obj, ensure_ascii=False).encode()),
        )
    connection.commit()
    connection.close()


def _cursor_append(path: Path, objects: list[dict], start: int) -> None:
    connection = sqlite3.connect(path)
    for offset, obj in enumerate(objects):
        connection.execute(
            "INSERT INTO blobs VALUES (?, ?)",
            (
                f"blob-{start + offset:04d}",
                json.dumps(obj, ensure_ascii=False).encode(),
            ),
        )
    connection.commit()
    connection.close()


def _opencode_v2_db(path: Path, session_id: str, rows: list[tuple]) -> None:
    """rows: (row_id, type, seq, data)."""
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT NOT NULL,"
        " title TEXT, time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,"
        " parent_id TEXT, time_archived INTEGER)"
    )
    connection.execute(
        "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
        " type TEXT NOT NULL, seq INTEGER NOT NULL, time_created INTEGER NOT NULL,"
        " time_updated INTEGER NOT NULL, data TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?)",
        (session_id, "/repo", "t", 1000, 9000, None, None),
    )
    for row_id, kind, seq, data in rows:
        connection.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (row_id, session_id, kind, seq, (seq + 1) * 1000, (seq + 1) * 1000,
             json.dumps(data, ensure_ascii=False)),
        )
    connection.commit()
    connection.close()


def _opencode_v1_db(path: Path, session_id: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY);
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, data TEXT);
        """
    )
    connection.execute("INSERT INTO session VALUES (?)", (session_id,))
    connection.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        ("u1", session_id, 1000, json.dumps({"role": "user", "time": {"created": 1000}})),
    )
    connection.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        ("pu", "u1", session_id, 1000,
         json.dumps({"type": "text", "text": "v1 hello"})),
    )
    connection.execute(
        "INSERT INTO message VALUES (?,?,?,?)",
        ("a1", session_id, 2000, json.dumps(
            {"role": "assistant", "finish": "stop", "time": {"created": 2000}})),
    )
    connection.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        ("pt", "a1", session_id, 2000,
         json.dumps({"type": "text", "text": "v1 working"})),
    )
    connection.execute(
        "INSERT INTO part VALUES (?,?,?,?,?)",
        ("pk", "a1", session_id, 2001, json.dumps({
            "type": "tool", "callID": "v1-call", "tool": "bash",
            "state": {"status": "completed",
                      "input": {"command": "ls /tmp"},
                      "output": "ok"},
        })),
    )
    connection.commit()
    connection.close()


def _no_sesskit():
    return mock.patch.object(
        richmsg, "_sesskit_reader_available", lambda _runtime: False
    )


def _wire(messages) -> list[dict]:
    return [item.to_wire_dict() for item in messages]


class CursorSesskitTests(unittest.TestCase):
    def test_text_tools_and_question_cards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _cursor_db(path, [
                {"role": "user", "content": "查一下"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "正在搜索"},
                    {"type": "tool-call", "toolName": "grep",
                     "toolCallId": "g1", "args": {"pattern": "TODO"}},
                ]},
                {"role": "tool", "content": [
                    {"type": "tool-result", "toolCallId": "g1",
                     "result": "src/main.py:42"},
                ]},
                {"role": "assistant", "content": [
                    {"type": "tool-call", "toolName": "AskQuestion",
                     "toolCallId": "q1",
                     "args": {"question": "继续？", "choices": ["好", "停"]}},
                ]},
            ])
            messages = richmsg.RichReader(_session("cursor", path)).read_all()
            tools = [t for m in messages if m.role == "assistant" for t in m.tools]

            grep = next(t for t in tools if t.call_id == "g1")
            self.assertEqual(grep.kind, "search")
            self.assertEqual(grep.status, "ok")
            self.assertIn("main.py:42", grep.output)
            host = next(m for m in messages if any(
                t.call_id == "g1" for t in m.tools))
            self.assertEqual(host.text, "正在搜索")

            ask = next(t for t in tools if t.call_id == "q1")
            self.assertEqual(ask.kind, "question")
            self.assertEqual(ask.options, ["好", "停"])
            self.assertEqual(ask.status, "running")

    def test_grouped_questions_stay_grouped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _cursor_db(path, [
                {"role": "assistant", "content": [
                    {"type": "tool-call", "toolName": "AskQuestion",
                     "toolCallId": "q2",
                     "args": {"questions": [
                         {"question": "第一题", "options": ["A", "B"]},
                         {"question": "第二题", "options": ["C", "D"]},
                     ]}},
                ]},
            ])
            messages = richmsg.RichReader(_session("cursor", path)).read_all()
            tool = messages[0].tools[0]
            self.assertEqual(tool.kind, "question")
            self.assertEqual(tool.options, [])
            self.assertEqual(len(tool.question_groups), 2)

    def test_injected_context_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _cursor_db(path, [
                {"role": "user", "content": (
                    "<user_info>\nName: Tester\n</user_info>\n"
                    "<rules>\nAlways be verbose.\n</rules>\n"
                    "<user_query>\n把登录改成验证码\n</user_query>"
                )},
                {"role": "user", "content": "<user_info>\n只剩上下文\n</user_info>"},
            ])
            messages = richmsg.RichReader(_session("cursor", path)).read_all()
            self.assertEqual(
                [item.text for item in messages if item.role == "user"],
                ["把登录改成验证码"],
            )

    def test_failed_tool_result_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _cursor_db(path, [
                {"role": "assistant", "content": [
                    {"type": "tool-call", "toolName": "bash",
                     "toolCallId": "b1", "args": {"command": "make test"}},
                ]},
                {"role": "tool", "content": [
                    {"type": "tool-result", "toolCallId": "b1",
                     "result": "exit code: 1\nboom"},
                ]},
            ])
            messages = richmsg.RichReader(_session("cursor", path)).read_all()
            self.assertEqual(messages[0].tools[0].status, "error")

    def test_pairing_across_polls(self) -> None:
        """Call and result land in different polls; the card re-emits paired."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _cursor_db(path, [
                {"role": "assistant", "content": [
                    {"type": "tool-call", "toolName": "bash",
                     "toolCallId": "c1", "args": {"command": "echo hi"}},
                ]},
            ])
            reader = richmsg.RichReader(_session("cursor", path))
            first = reader.poll()
            self.assertEqual(first[0].tools[0].status, "running")
            seq = first[0].seq
            _cursor_append(path, [
                {"role": "tool", "content": [
                    {"type": "tool-result", "toolCallId": "c1", "result": "hi\n"},
                ]},
            ], start=1)
            second = reader.poll()
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0].seq, seq)
            self.assertEqual(second[0].tools[0].status, "ok")
            self.assertIn("hi", second[0].tools[0].output)

    def test_pairing_across_pages(self) -> None:
        """Backward pages reassemble the full projection; call/result on
        different pages still pair by call_id when fed in order."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _cursor_db(path, [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": [
                    {"type": "tool-call", "toolName": "bash",
                     "toolCallId": "p1", "args": {"command": "uptime"}},
                ]},
                {"role": "tool", "content": [
                    {"type": "tool-result", "toolCallId": "p1", "result": "up"},
                ]},
            ])
            from sesskit import get_adapter

            session = _session("cursor", path)
            wire = get_adapter("cursor").open_reader(session, None)
            first = wire.poll()
            self.assertTrue(first.reset)
            # Backward pages cover the same events oldest-first when joined.
            seen: list = []
            before = None
            while True:
                page = wire.page(before=before, limit=1)
                seen = list(page.events) + seen
                if not page.has_more or not page.before:
                    break
                before = page.before
            self.assertEqual([e.seq for e in seen], [e.seq for e in first.events])
            # Feeding the paged events in order pairs the split call/result.
            reader = richmsg.RichReader(session)
            batch = richmsg._project_typed_batch(reader, "cursor", seen, {})
            by_call = {
                tool.call_id: tool
                for item in batch for tool in item.tools
            }
            self.assertEqual(by_call["p1"].status, "ok")

    def test_legacy_parity_on_fixture(self) -> None:
        """Old native parser vs SessKit projection: byte-equal wire output."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _cursor_db(path, [
                {"role": "user", "content": "<user_query>\n改登录\n</user_query>"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "收到"},
                    {"type": "tool-call", "toolName": "Edit",
                     "toolCallId": "e1", "args": {"file_path": "/r/login.py"}},
                ]},
                {"role": "tool", "content": [
                    {"type": "tool-result", "toolCallId": "e1", "result": "done"},
                ]},
            ])
            new = richmsg.RichReader(_session("cursor", path)).read_all()
            with _no_sesskit():
                old_reader = richmsg.RichReader(_session("cursor", path))
                old = old_reader.read_all()
            self.assertEqual(_wire(new), _wire(old))

    def test_prompt_only_fallback(self) -> None:
        """Prompt-only sessions fall back to prompt_history.json (SessKit F1)."""
        with tempfile.TemporaryDirectory() as directory:
            chat_dir = Path(directory) / "chat"
            chat_dir.mkdir()
            (chat_dir / "prompt_history.json").write_text(
                json.dumps(["newest prompt", "older prompt"], ensure_ascii=False),
                encoding="utf-8",
            )
            session = _session("cursor", chat_dir)
            from sesskit import get_adapter

            try:
                wire = get_adapter("cursor").open_reader(session, None)
                result = wire.poll()
            except Exception as exc:  # pragma: no cover - reader shape guard
                self.skipTest(f"cursor reader unavailable: {exc}")
            if not result.events:
                self.skipTest("requires SessKit F1 prompt fallback in the reader")
            messages = richmsg.RichReader(session).read_all()
            self.assertEqual(
                [item.text for item in messages if item.role == "user"],
                ["older prompt", "newest prompt"],
            )


class OpenCodeSesskitTests(unittest.TestCase):
    def _v2_session(self, directory: str, rows: list[tuple]) -> dict:
        path = Path(directory) / "opencode.db"
        _opencode_v2_db(path, "ses-1", rows)
        return _session("opencode", path, "ses-1")

    def test_v2_text_tools_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._v2_session(directory, [
                ("u1", "user", 0, {"time": {"created": 1001},
                                   "text": "run ls please"}),
                ("a1", "assistant", 1, {"time": {"created": 1002},
                 "content": [
                     {"type": "text", "text": "on it"},
                     {"type": "tool", "id": "call_1", "name": "bash",
                      "state": {"status": "completed",
                                "input": {"command": "ls"},
                                "content": [{"type": "text",
                                             "text": "file.txt"}]}},
                     {"type": "tool", "id": "call_2", "name": "edit",
                      "state": {"status": "error",
                                "input": {"path": "/x"},
                                "error": {"message": "boom"}}},
                 ],
                 "finish": "tool-calls"}),
            ])
            messages = richmsg.RichReader(session).read_all()
            assistant = [m for m in messages if m.role == "assistant"]
            self.assertEqual([m.text for m in messages if m.role == "user"],
                             ["run ls please"])
            self.assertEqual(assistant[0].text, "on it")
            by_call = {t.call_id: t for t in assistant[0].tools}
            self.assertEqual(by_call["call_1"].status, "ok")
            self.assertIn("file.txt", by_call["call_1"].output)
            self.assertEqual(by_call["call_2"].status, "error")

    def test_v2_question_tool_is_question_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._v2_session(directory, [
                ("u1", "user", 0, {"time": {"created": 1001}, "text": "go"}),
                ("a1", "assistant", 1, {"time": {"created": 1002},
                 "content": [
                     {"type": "tool", "id": "q-1", "name": "question",
                      "state": {"status": "pending",
                                "input": {"questions": [
                                    {"question": "Pick?",
                                     "options": ["A", "B"]},
                                ]}}},
                 ],
                 "finish": "tool-calls"}),
            ])
            messages = richmsg.RichReader(session).read_all()
            tool = messages[-1].tools[0]
            self.assertEqual(tool.kind, "question")
            self.assertEqual(tool.options, ["A", "B"])
            self.assertEqual(tool.status, "running")
            prompts = richmsg.pending_prompts_from_messages(messages)
            self.assertEqual(prompts[0]["options"], ["A", "B"])

    def test_v2_error_only_turn_hidden(self) -> None:
        """Error-only assistant turns stay out of the phone cards, like the
        plain-text fallback (SessKit conversation default)."""
        with tempfile.TemporaryDirectory() as directory:
            session = self._v2_session(directory, [
                ("u1", "user", 0, {"time": {"created": 1001}, "text": "go"}),
                ("a1", "assistant", 1, {"time": {"created": 1006},
                 "content": [],
                 "error": {"type": "provider.quota", "message": "nope"}}),
            ])
            messages = richmsg.RichReader(session).read_all()
            self.assertEqual(
                [(m.role, m.text) for m in messages], [("user", "go")]
            )

    def test_v1_text_and_tool_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            _opencode_v1_db(path, "ses-v1")
            messages = richmsg.RichReader(
                _session("opencode", path, "ses-v1")).read_all()
            texts = [(m.role, m.text) for m in messages if m.text]
            self.assertIn(("user", "v1 hello"), texts)
            self.assertIn(("assistant", "v1 working"), texts)
            tools = [t for m in messages for t in m.tools]
            self.assertEqual(len(tools), 1)
            self.assertEqual(tools[0].status, "ok")
            self.assertIn("ls /tmp", tools[0].summary)

    def test_text_parity_with_plain_fallback(self) -> None:
        """User/assistant text sequence matches the old plain fallback; the
        only addition is tool cards."""
        with tempfile.TemporaryDirectory() as directory:
            session = self._v2_session(directory, [
                ("u1", "user", 0, {"time": {"created": 1001}, "text": "hi"}),
                ("a1", "assistant", 1, {"time": {"created": 1002},
                 "content": [
                     {"type": "text", "text": "hello"},
                     {"type": "tool", "id": "c1", "name": "bash",
                      "state": {"status": "completed",
                                "input": {"command": "pwd"},
                                "content": [{"type": "text", "text": "/r"}]}},
                 ],
                 "finish": "stop"}),
            ])
            new = richmsg.RichReader(dict(session)).read_all()
            with _no_sesskit():
                old = richmsg.RichReader(dict(session)).read_all()
            self.assertEqual(
                [(m.role, m.text) for m in new],
                [(m.role, m.text) for m in old],
            )
            self.assertEqual(
                sum(len(m.tools) for m in old), 0,
            )
            self.assertEqual(sum(len(m.tools) for m in new), 1)

    def test_pairing_across_polls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            _opencode_v2_db(path, "ses-p", [
                ("u1", "user", 0, {"time": {"created": 1001}, "text": "go"}),
                ("a1", "assistant", 1, {"time": {"created": 1002},
                 "content": [
                     {"type": "tool", "id": "w1", "name": "bash",
                      "state": {"status": "running", "input": {"command": "sleep 5"}}},
                 ],
                 "finish": "tool-calls"}),
            ])
            session = _session("opencode", path, "ses-p")
            reader = richmsg.RichReader(session)
            first = reader.poll()
            self.assertEqual(first[-1].tools[0].status, "running")
            seq = first[-1].seq
            connection = sqlite3.connect(path)
            data = json.loads(connection.execute(
                "SELECT data FROM session_message WHERE id='a1'").fetchone()[0])
            data["content"][0]["state"] = {
                "status": "completed", "input": {"command": "sleep 5"},
                "content": [{"type": "text", "text": "slept"}]}
            connection.execute(
                "UPDATE session_message SET data=? WHERE id='a1'",
                (json.dumps(data, ensure_ascii=False),))
            connection.commit()
            connection.close()
            second = reader.poll()
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0].seq, seq)
            self.assertEqual(second[0].tools[0].status, "ok")


class SesskitStateRoundTripTests(unittest.TestCase):
    def test_export_restore_keeps_cursor_quiet(self) -> None:
        """Persisted opaque cursor resumes without re-pushing history."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _cursor_db(path, [
                {"role": "user", "content": "hi"},
            ])
            session = _session("cursor", path)
            reader = richmsg.RichReader(session)
            first = reader.read_all()
            self.assertEqual(len(first), 1)
            state = json.loads(json.dumps(reader.export_state()))
            restored = richmsg.RichReader(dict(session))
            restored.restore_state(state, list(first))
            self.assertEqual(restored.poll(), [])
            self.assertFalse(restored.has_earlier())


if __name__ == "__main__":
    unittest.main()
