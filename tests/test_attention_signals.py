from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral.attention_signals import _DB_TAIL_ROWS, inspect_session


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _session(source: str, path: Path, *, live: bool = True, **extra) -> dict:
    return {"source": source, "path": str(path), "id": "session-1", "live": live, **extra}


class ClaudeAttentionSignalTests(unittest.TestCase):
    def test_question_is_waiting_and_matching_result_resumes_working(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            prompt = {
                "type": "user",
                "uuid": "user-1",
                "origin": {"kind": "human"},
                "message": {"role": "user", "content": "开始"},
            }
            question = {
                "type": "assistant",
                "uuid": "assistant-1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "AskUserQuestion", "id": "ask-1", "input": {}}],
                },
            }
            _write_jsonl(path, [prompt, question])
            evidence = inspect_session(_session("claude", path))
            self.assertEqual(evidence.phase, "waiting")
            self.assertIsNotNone(evidence.question_token)

            answer = {
                "type": "user",
                "uuid": "user-2",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "ask-1", "content": "已选择"}],
                },
            }
            # 起始 prompt 可能已落在有界尾部之外，配对回答本身也必须恢复 working。
            _write_jsonl(path, [question, answer])
            evidence = inspect_session(_session("claude", path))
            self.assertEqual(evidence.phase, "working")
            self.assertIsNone(evidence.question_token)

    def test_turn_duration_is_idle_and_agent_token_does_not_hash_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            base = [
                {
                    "type": "assistant",
                    "uuid": "assistant-fixed",
                    "message": {"content": [{"type": "text", "text": "第一份正文"}]},
                }
            ]
            _write_jsonl(path, base)
            first = inspect_session(_session("claude", path)).activity_token
            base[0]["message"]["content"][0]["text"] = "完全不同的正文"
            _write_jsonl(path, base)
            second = inspect_session(_session("claude", path)).activity_token
            self.assertEqual(first, second)

            base.append({"type": "system", "subtype": "turn_duration", "uuid": "stop-1"})
            _write_jsonl(path, base)
            self.assertEqual(inspect_session(_session("claude", path)).phase, "idle")

    def test_live_user_prompt_without_agent_work_is_idle(self) -> None:
        """常驻进程还在、历史上只有用户开口时，不能把绿点当成执行中。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "uuid": "u1",
                        "origin": {"kind": "human"},
                        "message": {"role": "user", "content": "开始"},
                    }
                ],
            )
            self.assertEqual(inspect_session(_session("claude", path)).phase, "idle")

    def test_non_live_session_never_reports_working_or_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "uuid": "u1",
                        "message": {"role": "user", "content": "开始"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "content": [{"type": "tool_use", "name": "AskUserQuestion", "id": "ask-1"}]
                        },
                    },
                ],
            )
            self.assertEqual(inspect_session(_session("claude", path, live=False)).phase, "idle")


class CodexAttentionSignalTests(unittest.TestCase):
    def test_native_response_tail_preserves_working_without_start(self):
        for kind in ("reasoning", "custom_tool_call", "function_call", "custom_tool_call_output"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "codex.jsonl"
                entries = [{"type": "response_item", "payload": {"type": kind, "name": "exec"}}]
                _write_jsonl(path, entries)
                self.assertEqual(inspect_session(_session("codex", path)).phase, "working")
                self.assertEqual(inspect_session(_session("codex", path, live=False)).phase, "idle")
                entries.append({"type": "response_item", "payload": {
                    "type": "message", "role": "assistant", "channel": "final",
                }})
                _write_jsonl(path, entries)
                self.assertEqual(inspect_session(_session("codex", path)).phase, "idle")

    def test_task_lifecycle_and_structured_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            entries = [
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "call-1",
                    },
                },
            ]
            _write_jsonl(path, entries)
            waiting = inspect_session(_session("codex", path))
            self.assertEqual(waiting.phase, "waiting")
            self.assertIsNotNone(waiting.question_token)

            answer = {
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "call-1"},
            }
            entries.append(answer)
            _write_jsonl(path, [entries[1], answer])
            self.assertEqual(inspect_session(_session("codex", path)).phase, "working")

            entries = [
                *entries,
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}},
            ]
            _write_jsonl(path, entries)
            completed = inspect_session(_session("codex", path))
            self.assertEqual(completed.phase, "idle")
            self.assertIsNotNone(completed.activity_token)

    def test_non_live_task_started_is_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(path, [{"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t"}}])
            self.assertEqual(inspect_session(_session("codex", path, live=False)).phase, "idle")

    def test_tail_agent_activity_keeps_live_turn_working_without_visible_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "timestamp": "2026-07-01T10:00:05Z",
                        "type": "response_item",
                        "payload": {"type": "agent_message", "id": "message-1", "message": "进度"},
                    }
                ],
            )
            self.assertEqual(inspect_session(_session("codex", path)).phase, "working")
            self.assertEqual(inspect_session(_session("codex", path, live=False)).phase, "idle")

    def test_live_user_message_without_task_start_is_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "timestamp": "2026-08-16T10:00:00Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "请继续"},
                    }
                ],
            )
            self.assertEqual(inspect_session(_session("codex", path)).phase, "idle")
            self.assertEqual(inspect_session(_session("codex", path, live=False)).phase, "idle")


class KimiAttentionSignalTests(unittest.TestCase):
    def test_prompt_question_answer_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wire.jsonl"
            entries = [
                {"type": "turn.prompt", "time": 1000, "turnId": "turn-1"},
                {
                    "type": "context.append_loop_event",
                    "time": 1100,
                    "event": {
                        "type": "tool.call",
                        "name": "AskUserQuestion",
                        "toolCallId": "ask-1",
                        "uuid": "event-1",
                    },
                },
            ]
            _write_jsonl(path, entries)
            self.assertEqual(inspect_session(_session("kimi", path)).phase, "waiting")

            answer = {
                "type": "context.append_loop_event",
                "time": 1200,
                "event": {"type": "tool.result", "toolCallId": "ask-1", "uuid": "event-2"},
            }
            entries.append(answer)
            _write_jsonl(path, [entries[1], answer])
            self.assertEqual(inspect_session(_session("kimi", path)).phase, "working")

            entries.append({"type": "turn.cancel", "time": 1300, "turnId": "turn-1"})
            _write_jsonl(path, entries)
            cancelled = inspect_session(_session("kimi", path))
            self.assertEqual(cancelled.phase, "idle")
            self.assertIsNotNone(cancelled.activity_token)

    def test_stable_latest_step_end_is_conservatively_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wire.jsonl"
            _write_jsonl(
                path,
                [
                    {"type": "turn.prompt", "time": 1000},
                    {
                        "type": "context.append_loop_event",
                        "time": 1200,
                        "event": {"type": "step.end", "uuid": "step-1", "turnId": "turn-1"},
                    },
                ],
            )
            evidence = inspect_session(_session("kimi", path))
            self.assertEqual(evidence.phase, "idle")
            self.assertIsNotNone(evidence.activity_token)


class PiAttentionSignalTests(unittest.TestCase):
    def test_prompt_question_answer_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.jsonl"
            prompt = {
                "type": "message", "id": "u1", "timestamp": "2026-08-14T10:00:00Z",
                "message": {"role": "user", "content": "请继续"},
            }
            question = {
                "type": "message", "id": "a1", "timestamp": "2026-08-14T10:00:01Z",
                "message": {
                    "role": "assistant", "stopReason": "toolUse",
                    "content": [{"type": "toolCall", "id": "ask-1", "name": "request_user_input"}],
                },
            }
            _write_jsonl(path, [prompt, question])
            waiting = inspect_session(_session("pi", path))
            self.assertEqual(waiting.phase, "waiting")
            self.assertIsNotNone(waiting.question_token)

            answer = {
                "type": "message", "id": "r1", "timestamp": "2026-08-14T10:00:02Z",
                "message": {"role": "toolResult", "toolCallId": "ask-1", "content": []},
            }
            _write_jsonl(path, [question, answer])
            self.assertEqual(inspect_session(_session("pi", path)).phase, "working")

            completed = {
                "type": "message", "id": "a2", "timestamp": "2026-08-14T10:00:03Z",
                "message": {
                    "role": "assistant", "stopReason": "stop",
                    "content": [{"type": "text", "text": "已完成"}],
                },
            }
            _write_jsonl(path, [question, answer, completed])
            evidence = inspect_session(_session("pi", path))
            self.assertEqual(evidence.phase, "idle")
            self.assertIsNotNone(evidence.activity_token)

    def test_non_live_prompt_never_reports_working(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.jsonl"
            _write_jsonl(path, [{
                "type": "message", "id": "u1", "timestamp": "2026-08-14T10:00:00Z",
                "message": {"role": "user", "content": "请继续"},
            }])
            self.assertEqual(inspect_session(_session("pi", path, live=False)).phase, "idle")

    def test_live_completed_turn_stays_idle_while_process_resident(self) -> None:
        """进程仍在、最后一轮已 stop：常驻 TUI 不得亮执行中绿点。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "message", "id": "u1", "timestamp": "2026-08-16T13:00:00Z",
                        "message": {"role": "user", "content": "请继续"},
                    },
                    {
                        "type": "message", "id": "a1", "timestamp": "2026-08-16T13:01:00Z",
                        "message": {
                            "role": "assistant", "stopReason": "stop",
                            "content": [{"type": "text", "text": "做完了"}],
                        },
                    },
                ],
            )
            self.assertEqual(inspect_session(_session("pi", path)).phase, "idle")

    def test_live_user_prompt_after_quota_error_is_not_working(self) -> None:
        """额度挂掉后最后一条常常是 user；进程还在也不能亮绿。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "message", "id": "u1", "timestamp": "2026-08-16T13:15:00Z",
                        "message": {"role": "user", "content": "下一问"},
                    }
                ],
            )
            self.assertEqual(inspect_session(_session("pi", path)).phase, "idle")

    def test_live_tool_use_without_result_stays_working(self) -> None:
        """进程仍在且明确在跑工具时，绿点必须保留。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "message", "id": "a1", "timestamp": "2026-08-16T15:00:00Z",
                        "message": {
                            "role": "assistant", "stopReason": "toolUse",
                            "content": [{"type": "toolCall", "id": "bash-1", "name": "bash"}],
                        },
                    }
                ],
            )
            self.assertEqual(inspect_session(_session("pi", path)).phase, "working")
            self.assertEqual(inspect_session(_session("pi", path, live=False)).phase, "idle")

    def test_live_claim_working_covers_user_tail_before_assistant_lands(self) -> None:
        """TUI shows Working before jsonl gets an assistant row — claim phase must green."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "message", "id": "u1", "timestamp": "2026-09-13T10:00:00Z",
                        "message": {"role": "user", "content": "请继续"},
                    }
                ],
            )
            without_claim = inspect_session(_session("pi", path))
            self.assertEqual(without_claim.phase, "idle")
            evidence = inspect_session(
                _session(
                    "pi",
                    path,
                    agent_phase="working",
                    agent_phase_event="agent_start",
                    agent_phase_at="2026-09-13T10:00:01Z",
                )
            )
            self.assertEqual(evidence.phase, "working")
            self.assertEqual(evidence.source, "observer")

    def test_live_claim_waiting_from_ui_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "message", "id": "u1", "timestamp": "2026-09-13T10:00:00Z",
                        "message": {"role": "user", "content": "请继续"},
                    }
                ],
            )
            evidence = inspect_session(
                _session(
                    "pi",
                    path,
                    agent_phase="waiting",
                    agent_phase_event="ui_prompt_start",
                    agent_phase_at="2026-09-13T10:00:02Z",
                )
            )
            self.assertEqual(evidence.phase, "waiting")
            self.assertEqual(evidence.source, "observer")
            self.assertIsNotNone(evidence.question_token)

    def test_newer_history_idle_beats_stale_claim_working(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "message", "id": "a1", "timestamp": "2026-09-13T10:05:00Z",
                        "message": {
                            "role": "assistant", "stopReason": "stop",
                            "content": [{"type": "text", "text": "做完了"}],
                        },
                    }
                ],
            )
            evidence = inspect_session(
                _session(
                    "pi",
                    path,
                    agent_phase="working",
                    agent_phase_event="agent_start",
                    agent_phase_at="2026-09-13T10:00:00Z",
                )
            )
            self.assertEqual(evidence.phase, "idle")
            self.assertEqual(evidence.source, "history")


class OpenCodeAttentionSignalTests(unittest.TestCase):
    def _database(self, path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(path)
        connection.execute(
            "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT, "
            "time_created INTEGER, time_updated INTEGER, parent_id TEXT, time_archived INTEGER)"
        )
        connection.execute(
            "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, "
            "time_updated INTEGER, data TEXT)"
        )
        connection.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, "
            "time_created INTEGER, time_updated INTEGER, data TEXT)"
        )
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
        return connection

    def _write_v2(self, connection: sqlite3.Connection, rows: list[tuple]) -> None:
        """rows: (id, type, seq, ts, data-dict)。"""
        for mid, mtype, seq, ts, data in rows:
            connection.execute(
                "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                (mid, "session-1", mtype, seq, ts, ts, json.dumps(data)),
            )

    def test_incomplete_assistant_and_pending_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                ("m1", "session-1", 1, 1, json.dumps({"role": "assistant", "time": {"created": 1}})),
            )
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "p1",
                    "m1",
                    "session-1",
                    2,
                    2,
                    json.dumps(
                        {
                            "type": "tool",
                            "tool": "question",
                            "callID": "ask-1",
                            "state": {"status": "running"},
                        }
                    ),
                ),
            )
            connection.commit()
            connection.close()
            evidence = inspect_session(_session("opencode", path))
            self.assertEqual(evidence.phase, "waiting")
            self.assertIsNotNone(evidence.question_token)

    def test_completed_question_and_completed_assistant_are_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                (
                    "m1",
                    "session-1",
                    1,
                    2,
                    json.dumps({"role": "assistant", "finish": "stop", "time": {"created": 1, "completed": 2}}),
                ),
            )
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "p1",
                    "m1",
                    "session-1",
                    2,
                    2,
                    json.dumps(
                        {
                            "type": "tool",
                            "tool": "question",
                            "callID": "ask-1",
                            "state": {"status": "completed"},
                        }
                    ),
                ),
            )
            connection.commit()
            connection.close()
            evidence = inspect_session(_session("opencode", path))
            self.assertEqual(evidence.phase, "idle")
            self.assertIsNone(evidence.question_token)
            self.assertIsNotNone(evidence.activity_token)

    def test_empty_incomplete_assistant_is_idle_even_while_live(self) -> None:
        """tool-calls 之后插入的空助手占位不能因为进程还在就亮绿。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                ("m1", "session-1", 1, 1, json.dumps({"role": "assistant", "time": {"created": 1}})),
            )
            connection.commit()
            connection.close()
            self.assertEqual(inspect_session(_session("opencode", path)).phase, "idle")
            self.assertEqual(inspect_session(_session("opencode", path, live=False)).phase, "idle")

    def test_streaming_assistant_parts_keep_live_turn_working(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                ("m1", "session-1", 1, 1, json.dumps({"role": "assistant", "time": {"created": 1}})),
            )
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                ("p1", "m1", "session-1", 2, 2, json.dumps({"type": "step-start"})),
            )
            connection.commit()
            connection.close()
            self.assertEqual(inspect_session(_session("opencode", path)).phase, "working")
            self.assertEqual(inspect_session(_session("opencode", path, live=False)).phase, "idle")

    def test_running_tool_keeps_live_turn_working(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                ("m1", "session-1", 1, 1, json.dumps({"role": "assistant", "time": {"created": 1}})),
            )
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "p1",
                    "m1",
                    "session-1",
                    2,
                    2,
                    json.dumps({"type": "tool", "tool": "bash", "callID": "c1", "state": {"status": "running"}}),
                ),
            )
            connection.commit()
            connection.close()
            self.assertEqual(inspect_session(_session("opencode", path)).phase, "working")

    def test_v2_tool_calls_tail_is_working_while_live(self) -> None:
        """v2 专属会话：tool-calls 尾部 live 亮绿，进程已死收 idle。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            self._write_v2(
                connection,
                [
                    ("m1", "user", 0, 1000, {"time": {"created": 1000}, "text": "hi"}),
                    (
                        "m2",
                        "assistant",
                        1,
                        2000,
                        {
                            "time": {"created": 2000},
                            "content": [
                                {
                                    "type": "tool",
                                    "id": "c1",
                                    "name": "bash",
                                    "state": {"status": "completed", "input": {}, "content": []},
                                }
                            ],
                            "finish": "tool-calls",
                        },
                    ),
                ],
            )
            connection.commit()
            connection.close()
            self.assertEqual(inspect_session(_session("opencode", path)).phase, "working")
            self.assertEqual(inspect_session(_session("opencode", path, live=False)).phase, "idle")

    def test_v2_stop_and_error_tails_are_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            self._write_v2(
                connection,
                [
                    ("m1", "user", 0, 1000, {"time": {"created": 1000}, "text": "hi"}),
                    (
                        "m2",
                        "assistant",
                        1,
                        2000,
                        {
                            "time": {"created": 2000, "completed": 2500},
                            "content": [{"type": "text", "text": "done"}],
                            "finish": "stop",
                        },
                    ),
                    ("m3", "idle", 2, 3000, {"time": {"created": 3000}, "outcome": "succeeded"}),
                ],
            )
            connection.commit()
            connection.close()
            self.assertEqual(inspect_session(_session("opencode", path)).phase, "idle")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            self._write_v2(
                connection,
                [
                    ("m1", "user", 0, 1000, {"time": {"created": 1000}, "text": "hi"}),
                    (
                        "m2",
                        "assistant",
                        1,
                        2000,
                        {
                            "time": {"created": 2000, "completed": 2500},
                            "content": [],
                            "finish": "error",
                            "error": {"type": "aborted", "message": "Aborted"},
                        },
                    ),
                    ("m3", "idle", 2, 3000, {"time": {"created": 3000}, "outcome": "failed"}),
                ],
            )
            connection.commit()
            connection.close()
            self.assertEqual(inspect_session(_session("opencode", path)).phase, "idle")

    def test_v2_unanswered_question_is_waiting_answered_is_not(self) -> None:
        """v2 question 只有 completed/error 终态：completed 无 answers 才算等待。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            self._write_v2(
                connection,
                [
                    ("m1", "user", 0, 1000, {"time": {"created": 1000}, "text": "hi"}),
                    (
                        "m2",
                        "assistant",
                        1,
                        2000,
                        {
                            "time": {"created": 2000},
                            "content": [
                                {
                                    "type": "tool",
                                    "id": "q1",
                                    "name": "question",
                                    "state": {"status": "completed", "input": {}, "content": [], "metadata": {}},
                                }
                            ],
                            "finish": "tool-calls",
                        },
                    ),
                ],
            )
            connection.commit()
            connection.close()
            evidence = inspect_session(_session("opencode", path))
            self.assertEqual(evidence.phase, "waiting")
            self.assertIsNotNone(evidence.question_token)
            self.assertEqual(inspect_session(_session("opencode", path, live=False)).phase, "idle")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            self._write_v2(
                connection,
                [
                    ("m1", "user", 0, 1000, {"time": {"created": 1000}, "text": "hi"}),
                    (
                        "m2",
                        "assistant",
                        1,
                        2000,
                        {
                            "time": {"created": 2000},
                            "content": [
                                {
                                    "type": "tool",
                                    "id": "q1",
                                    "name": "question",
                                    "state": {
                                        "status": "completed",
                                        "input": {},
                                        "content": [{"type": "text", "text": "User has answered"}],
                                        "metadata": {"answers": [["x"]]},
                                    },
                                }
                            ],
                            "finish": "tool-calls",
                        },
                    ),
                ],
            )
            connection.commit()
            connection.close()
            evidence = inspect_session(_session("opencode", path))
            self.assertEqual(evidence.phase, "working")
            self.assertIsNone(evidence.question_token)

    def test_v2_user_tail_and_midstream_streaming(self) -> None:
        """user 尾 live 仍 idle；无 finish 但有文本/工具的落盘中行 live 才 working。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            self._write_v2(
                connection,
                [
                    (
                        "m1",
                        "assistant",
                        0,
                        1000,
                        {
                            "time": {"created": 1000},
                            "content": [{"type": "text", "text": "done"}],
                            "finish": "stop",
                        },
                    ),
                    ("m2", "user", 1, 2000, {"time": {"created": 2000}, "text": "next?"}),
                ],
            )
            connection.commit()
            connection.close()
            self.assertEqual(inspect_session(_session("opencode", path)).phase, "idle")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            self._write_v2(
                connection,
                [
                    ("m1", "user", 0, 1000, {"time": {"created": 1000}, "text": "hi"}),
                    (
                        "m2",
                        "assistant",
                        1,
                        2000,
                        {"time": {"created": 2000}, "content": [{"type": "text", "text": "draft"}]},
                    ),
                ],
            )
            connection.commit()
            connection.close()
            self.assertEqual(inspect_session(_session("opencode", path)).phase, "working")
            self.assertEqual(inspect_session(_session("opencode", path, live=False)).phase, "idle")

    def test_v2_only_skip_tail_falls_back_to_v1(self) -> None:
        """v2 只有 compaction/idle 尾巴时回落 v1：v1 有 running 工具则 working。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            connection.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("other", "/r", "t", 1, 1, None, None),
            )
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                ("m1", "session-1", 1, 1, json.dumps({"role": "assistant", "time": {"created": 1}})),
            )
            connection.execute(
                "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "p1",
                    "m1",
                    "session-1",
                    2,
                    2,
                    json.dumps({"type": "tool", "tool": "bash", "callID": "c1", "state": {"status": "running"}}),
                ),
            )
            self._write_v2(
                connection,
                [("m9", "idle", 12, 3000, {"time": {"created": 3000}, "outcome": "succeeded"})],
            )
            connection.commit()
            connection.close()
            self.assertEqual(inspect_session(_session("opencode", path)).phase, "working")

    def test_dual_table_session_follows_v1(self) -> None:
        """双表并存的迁移行走 v1：v2 是 tool-calls 也必须看 v1 的 stop。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "opencode.db"
            connection = self._database(path)
            connection.execute(
                "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("session-1", "/r", "t", 1, 2, None, None),
            )
            connection.execute(
                "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                (
                    "m1",
                    "session-1",
                    1,
                    2,
                    json.dumps({"role": "assistant", "finish": "stop", "time": {"created": 1, "completed": 2}}),
                ),
            )
            self._write_v2(
                connection,
                [
                    ("m1", "user", 0, 1000, {"time": {"created": 1000}, "text": "hi"}),
                    (
                        "m2",
                        "assistant",
                        1,
                        2000,
                        {
                            "time": {"created": 2000},
                            "content": [
                                {
                                    "type": "tool",
                                    "id": "c1",
                                    "name": "bash",
                                    "state": {"status": "completed", "input": {}, "content": []},
                                }
                            ],
                            "finish": "tool-calls",
                        },
                    ),
                ],
            )
            connection.commit()
            connection.close()
            self.assertEqual(inspect_session(_session("opencode", path)).phase, "idle")


class CursorAttentionSignalTests(unittest.TestCase):
    def _database(self, path: Path, objects: list[dict | bytes]) -> None:
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        for index, obj in enumerate(objects):
            payload = (
                obj if isinstance(obj, (bytes, bytearray))
                else json.dumps(obj, ensure_ascii=False).encode()
            )
            connection.execute(
                "INSERT INTO blobs VALUES (?, ?)",
                (f"blob-{index:04d}", payload),
            )
        connection.commit()
        connection.close()

    def test_default_non_live_cursor_does_not_open_database(self) -> None:
        with mock.patch("corral.attention_signals.sqlite3.connect") as connect:
            evidence = inspect_session(
                {"source": "cursor", "path": "/无需存在/store.db", "live": False}
            )
        connect.assert_not_called()
        self.assertEqual(evidence.phase, "unknown")

    def test_probe_reads_activity_but_never_infers_working(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            self._database(
                path,
                [
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "新的结果"}],
                    }
                ],
            )
            evidence = inspect_session(_session("cursor", path, live=False, signal_probe=True))
            self.assertEqual(evidence.phase, "idle")
            self.assertNotEqual(evidence.phase, "working")
            self.assertIsNotNone(evidence.activity_token)

    def test_live_completed_assistant_text_is_idle_despite_resident_process(self) -> None:
        """Cursor 常驻进程还在、历史已有最终答复时，不得亮执行中绿点。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            self._database(
                path,
                [
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "对照 BrainDrop 的液态玻璃已经改完"}],
                    }
                ],
            )
            evidence = inspect_session(_session("cursor", path))
            self.assertEqual(evidence.phase, "idle")
            self.assertNotEqual(evidence.phase, "working")

    def test_live_open_tool_call_infers_working_from_history(self) -> None:
        """进程仍在且历史里最新是未完成工具调用时必须亮绿（Globbing / Shell 等）。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            self._database(
                path,
                [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool-call", "toolName": "Shell", "toolCallId": "call-shell-1"}
                        ],
                    }
                ],
            )
            self.assertEqual(inspect_session(_session("cursor", path)).phase, "working")
            self.assertEqual(
                inspect_session(_session("cursor", path, live=False, signal_probe=True)).phase,
                "unknown",
            )

    def test_live_tool_activity_after_assistant_text_restores_working(self) -> None:
        """中间正文之后若仍有工具活动，必须亮绿，不能停在 idle/unknown。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            self._database(
                path,
                [
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "先搜一下相关文件"}],
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool-call",
                                "toolName": "Glob",
                                "toolCallId": "call-glob-1",
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "content": [
                            {
                                "type": "tool-result",
                                "toolName": "Glob",
                                "toolCallId": "call-glob-1",
                            }
                        ],
                    },
                ],
            )
            self.assertEqual(inspect_session(_session("cursor", path)).phase, "working")

    def test_structured_terminal_marker_generates_activity_without_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            self._database(
                path,
                [{"role": "assistant", "content": [], "status": "aborted"}],
            )
            evidence = inspect_session(_session("cursor", path, live=False, signal_probe=True))
            self.assertEqual(evidence.phase, "idle")
            self.assertIsNotNone(evidence.activity_token)

    def test_unpaired_ask_question_is_waiting_only_while_live(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            question = {
                "role": "assistant",
                "content": [
                    {"type": "tool-call", "toolName": "AskQuestion", "toolCallId": "ask-1", "args": {}}
                ],
            }
            self._database(path, [question])
            self.assertEqual(inspect_session(_session("cursor", path)).phase, "waiting")
            not_live = inspect_session(_session("cursor", path, live=False, signal_probe=True))
            self.assertEqual(not_live.phase, "unknown")
            self.assertIsNone(not_live.question_token)

    def test_answer_clears_question_and_tail_query_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            answered_path = Path(directory) / "answered.db"
            self._database(
                answered_path,
                [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool-call", "toolName": "AskQuestion", "toolCallId": "ask-1"}
                        ],
                    },
                    {
                        "role": "tool",
                        "content": [
                            {"type": "tool-result", "toolName": "AskQuestion", "toolCallId": "ask-1"}
                        ],
                    },
                ],
            )
            self.assertEqual(inspect_session(_session("cursor", answered_path)).phase, "unknown")

            bounded_path = Path(directory) / "bounded.db"
            old_question = {
                "role": "assistant",
                "content": [
                    {"type": "tool-call", "toolName": "AskQuestion", "toolCallId": "too-old"}
                ],
            }
            fillers = [
                {"role": "assistant", "content": [{"type": "text", "text": f"结果 {index}"}]}
                for index in range(220)
            ]
            self._database(bounded_path, [old_question, *fillers])
            evidence = inspect_session(_session("cursor", bounded_path))
            self.assertEqual(evidence.phase, "idle")
            self.assertIsNone(evidence.question_token)

    def test_result_before_call_is_not_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            self._database(
                path,
                [
                    {
                        "role": "tool",
                        "content": [
                            {"type": "tool-result", "toolName": "AskQuestion", "toolCallId": "ask-1"}
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool-call", "toolName": "AskQuestion", "toolCallId": "ask-1"}
                        ],
                    },
                ],
            )
            self.assertEqual(inspect_session(_session("cursor", path)).phase, "unknown")

    def test_protobuf_ask_question_is_waiting_until_json_result(self) -> None:
        call_id = "call-pending\nfc_child_0"
        question_blob = _cursor_ask_question_blob(call_id)
        other_tool_blob = _cursor_field2_blob(8, b"grep-hits", "call-grep\nfc_child_1")
        with tempfile.TemporaryDirectory() as directory:
            pending_path = Path(directory) / "pending.db"
            self._database(pending_path, [other_tool_blob, question_blob])
            evidence = inspect_session(_session("cursor", pending_path))
            self.assertEqual(evidence.phase, "waiting")
            self.assertIsNotNone(evidence.question_token)

            answered_path = Path(directory) / "answered.db"
            self._database(
                answered_path,
                [
                    question_blob,
                    {
                        "role": "tool",
                        "content": [
                            {
                                "type": "tool-result",
                                "toolName": "AskQuestion",
                                "toolCallId": call_id,
                            }
                        ],
                    },
                ],
            )
            self.assertEqual(inspect_session(_session("cursor", answered_path)).phase, "unknown")

    def test_protobuf_ask_question_clears_after_later_tool_work(self) -> None:
        call_id = "call-stale\nfc_child_0"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            self._database(
                path,
                [
                    _cursor_ask_question_blob(call_id),
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool-call",
                                "toolName": "Shell",
                                "toolCallId": "call-shell-1",
                            }
                        ],
                    },
                ],
            )
            self.assertEqual(inspect_session(_session("cursor", path)).phase, "working")

    def test_protobuf_ask_question_clears_after_later_protobuf_tool(self) -> None:
        call_id = "call-stale\nfc_child_0"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            self._database(
                path,
                [
                    _cursor_ask_question_blob(call_id),
                    _cursor_field2_blob(8, b"grep-hits", "call-grep\nfc_child_1"),
                ],
            )
            self.assertEqual(inspect_session(_session("cursor", path)).phase, "working")

    def test_stale_protobuf_question_outside_json_window_is_not_waiting(self) -> None:
        call_id = "call-old\nfc_child_0"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            fillers = [
                {"role": "assistant", "content": [{"type": "text", "text": f"结果 {index}"}]}
                for index in range(_DB_TAIL_ROWS + 1)
            ]
            self._database(path, [_cursor_ask_question_blob(call_id), *fillers])
            evidence = inspect_session(_session("cursor", path))
            self.assertEqual(evidence.phase, "idle")
            self.assertIsNone(evidence.question_token)


def _pb_varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _pb_bytes(field: int, payload: bytes) -> bytes:
    return _pb_varint((field << 3) | 2) + _pb_varint(len(payload)) + payload


def _cursor_field2_blob(inner_field: int, inner_payload: bytes, call_id: str) -> bytes:
    inner = _pb_bytes(inner_field, inner_payload) + _pb_bytes(57, call_id.encode())
    return _pb_bytes(2, inner)


def _cursor_ask_question_blob(call_id: str) -> bytes:
    return _cursor_field2_blob(23, b"question-payload", call_id)


class AttentionSignalFallbackTests(unittest.TestCase):
    def test_unknown_missing_and_corrupt_inputs_degrade_safely(self) -> None:
        self.assertEqual(inspect_session({"source": "future", "live": True}).phase, "unknown")
        self.assertEqual(inspect_session({"source": "claude", "path": "/missing", "live": True}).phase, "unknown")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text("not-json\n{broken", encoding="utf-8")
            self.assertEqual(inspect_session(_session("codex", path)).phase, "unknown")

    def test_unchanged_history_has_stable_observed_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "uuid": "assistant-1",
                        "timestamp": "2026-07-01T10:00:05Z",
                        "message": {"content": [{"type": "text", "text": "结果"}]},
                    }
                ],
            )
            first = inspect_session(_session("claude", path))
            second = inspect_session(_session("claude", path))
            self.assertEqual(first, second)
            self.assertGreater(first.observed_at, 0)


if __name__ == "__main__":
    unittest.main()
