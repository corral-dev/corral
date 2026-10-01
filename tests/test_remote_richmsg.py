"""corral.remote.richmsg：各助手工具调用摘要与提问选项解析。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from corral.remote import richmsg


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
        encoding="utf-8",
    )


def _session(source: str, path: Path, **extra) -> dict:
    return {"source": source, "path": str(path), "id": "session-1", **extra}


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


class RichmsgUtilityTests(unittest.TestCase):
    def test_classify_maps_known_tools(self) -> None:
        self.assertEqual(richmsg.classify("Write"), "write")
        self.assertEqual(richmsg.classify("Bash"), "shell")
        self.assertEqual(richmsg.classify("AskUserQuestion"), "question")
        self.assertEqual(richmsg.classify("unknown_tool"), "other")

    def test_summarize_file_and_command(self) -> None:
        summary, _ = richmsg.summarize("Write", "write", {"file_path": "/tmp/foo/bar.py"})
        self.assertEqual(summary, "Write bar.py")
        summary, _ = richmsg.summarize(
            "bash",
            "shell",
            {"command": "export PATH=/usr/bin\npython3 -m pytest tests/"},
        )
        self.assertEqual(summary, "python3 -m pytest tests/")

    def test_extract_options_flat_and_nested(self) -> None:
        flat = richmsg._extract_options({"options": ["继续", "取消"]})
        self.assertEqual(flat, ["继续", "取消"])
        nested = richmsg._extract_options(
            {
                "questions": [
                    {
                        "question": "选方案",
                        "options": [{"label": "方案 A"}, {"label": "方案 B"}],
                    }
                ]
            }
        )
        self.assertEqual(nested, ["方案 A", "方案 B"])

    def test_two_questions_are_not_flattened(self) -> None:
        args = {
            "questions": [
                {
                    "question": "会补上来吗？",
                    "options": [
                        {"label": "会补上来，重进就都在了"},
                        {"label": "补上来一部分，还是缺最新的"},
                        {"label": "重进也还是没有"},
                        {"label": "没试过"},
                    ],
                },
                {
                    "question": "哪个助手？",
                    "options": [
                        {"label": "Cursor"},
                        {"label": "Claude Code"},
                        {"label": "Codex"},
                        {"label": "Pi"},
                    ],
                },
            ]
        }
        self.assertEqual(richmsg._extract_options(args), [])
        groups = richmsg._extract_question_groups(args)
        self.assertEqual([group["summary"] for group in groups], ["会补上来吗？", "哪个助手？"])
        self.assertEqual(len(groups[0]["options"]), 4)
        self.assertEqual(groups[1]["options"][-1], "Pi")

    def test_pending_prompts_split_questions_and_drop_when_later_activity(self) -> None:
        ask = richmsg.ToolCall(
            call_id="q1",
            name="AskQuestion",
            kind="question",
            summary="AskQuestion",
            status="running",
            question_groups=[
                {"summary": "会补上来吗？", "options": ["A", "B", "C", "D"]},
                {"summary": "哪个助手？", "options": ["Cursor", "Claude Code", "Codex", "Pi"]},
            ],
        )
        live = [richmsg.RichMessage(seq=1, role="assistant", tools=[ask])]
        prompts = richmsg.pending_prompts_from_messages(live)
        self.assertEqual([item["id"] for item in prompts], ["q1:0", "q1:1"])
        self.assertEqual(prompts[0]["options"], ["A", "B", "C", "D"])
        self.assertEqual(prompts[1]["summary"], "哪个助手？")

        later = richmsg.ToolCall(call_id="g1", name="grep", kind="search", summary="grep", status="ok")
        moved = live + [richmsg.RichMessage(seq=2, role="assistant", tools=[later])]
        self.assertEqual(richmsg.pending_prompts_from_messages(moved), [])

    def test_looks_failed_only_on_explicit_markers(self) -> None:
        self.assertFalse(richmsg._looks_failed("all good\nprinted error in body"))
        self.assertTrue(richmsg._looks_failed("exit code: 1\nsomething broke"))


class CodexRichmsgTests(unittest.TestCase):
    def test_function_call_edit_shell_and_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            entries = [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        # 真实形态 content 为列表；字符串形态在 2051 条本地历史里
                        # 零出现，SessKit 会话/活动两层统一丢弃（P1 已上报）。
                        "content": [{"type": "input_text", "text": "帮我改一下"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "apply_patch",
                        "call_id": "edit-1",
                        "arguments": json.dumps({"path": "/proj/main.py"}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "edit-1",
                        "output": "patched",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "shell",
                        "call_id": "shell-1",
                        "input": 'tools.exec_command({"cmd":"npm test"})',
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call_output",
                        "call_id": "shell-1",
                        "output": "exit code: 1\nfailed",
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "request_user_input",
                        "call_id": "ask-1",
                        "arguments": json.dumps({"question": "继续吗？", "options": ["是", "否"]}),
                    },
                },
            ]
            _write_jsonl(path, entries)
            messages = richmsg.RichReader(_session("codex", path)).read_all()

            user = [m for m in messages if m.role == "user"]
            self.assertEqual(len(user), 1)
            self.assertEqual(user[0].text, "帮我改一下")

            tools = [t for m in messages if m.role == "assistant" for t in m.tools]
            self.assertEqual(len(tools), 3)

            edit = next(t for t in tools if t.call_id == "edit-1")
            self.assertEqual(edit.kind, "edit")
            self.assertIn("main.py", edit.summary)
            self.assertEqual(edit.status, "ok")

            shell = next(t for t in tools if t.call_id == "shell-1")
            self.assertEqual(shell.kind, "shell")
            self.assertIn("npm test", shell.summary)
            self.assertEqual(shell.status, "error")

            ask = next(t for t in tools if t.call_id == "ask-1")
            self.assertEqual(ask.kind, "question")
            self.assertEqual(ask.options, ["是", "否"])
            self.assertEqual(ask.status, "running")

    def test_agent_message_text(self) -> None:
        # 旧 event_msg 信封的 agent_message 是 SessKit 解释的真实形态；
        # response_item 信封里套旧 agent_message payload 的过渡形态在 2051 条
        # 本地历史里零出现，SessKit 会话/活动两层统一丢弃（P1 已上报）。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": "已完成"},
                    }
                ],
            )
            messages = richmsg.RichReader(_session("codex", path)).read_all()
            self.assertEqual(messages[0].role, "assistant")
            self.assertEqual(messages[0].text, "已完成")

    def test_response_item_assistant_message_text(self) -> None:
        """Codex 当前 response_item 格式的助手正文也必须进入远程聊天流。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "当前格式也可见"}],
                        },
                    }
                ],
            )
            messages = richmsg.RichReader(_session("codex", path)).read_all()
            self.assertEqual([(m.role, m.text) for m in messages], [("assistant", "当前格式也可见")])

    def test_injected_agents_md_user_blob_is_dropped(self) -> None:
        """系统说明与真人问题常写成两条 user；手机不能把说明当成第一条人话。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "# AGENTS.md instructions\n<INSTRUCTIONS>秘密</INSTRUCTIONS>",
                                },
                                {"type": "input_text", "text": "<environment_context>cwd=/tmp</environment_context>"},
                            ],
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "给我看图标"}],
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "给我看图标"},
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": "好的"},
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "好的"}],
                        },
                    },
                ],
            )
            messages = richmsg.RichReader(_session("codex", path)).read_all()
            self.assertEqual(
                [(item.role, item.text) for item in messages],
                [("user", "给我看图标"), ("assistant", "好的")],
            )

    def test_turn_aborted_and_string_agents_blob_are_dropped(self) -> None:
        """中断标记和字符串形态的系统说明都不能当手机第一句。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "# AGENTS.md instructions\n秘密",
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "<turn_aborted> The user interrupted the previous turn",
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "继续"},
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": "好"},
                    },
                ],
            )
            messages = richmsg.RichReader(_session("codex", path)).read_all()
            self.assertEqual(
                [(item.role, item.text) for item in messages],
                [("user", "继续"), ("assistant", "好")],
            )

    def test_code_review_prompt_is_kept(self) -> None:
        """技能默认提问也是真人可见会话，手机不能滤成空白。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            prompt = "对本仓库当前的 git diff 做一次 code review，不要额外限制范围。"
            _write_jsonl(
                path,
                [{"type": "event_msg", "payload": {"type": "user_message", "message": prompt}}],
            )
            messages = richmsg.RichReader(_session("codex", path)).read_all()
            self.assertEqual([(item.role, item.text) for item in messages], [("user", prompt)])

    def test_plan_followup_prompt_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "Briefly inform the user about the task result.",
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "现在四角根本无法调整了"},
                    },
                ],
            )
            messages = richmsg.RichReader(_session("codex", path)).read_all()
            self.assertEqual(
                [(item.role, item.text) for item in messages],
                [("user", "现在四角根本无法调整了")],
            )


class ClaudeRichmsgTests(unittest.TestCase):
    def test_tool_use_with_string_input_and_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            entries = [
                {
                    "type": "user",
                    "uuid": "u1",
                    "origin": {"kind": "human"},
                    "message": {"role": "user", "content": "开始"},
                },
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "我来改文件"},
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "id": "w1",
                                "input": json.dumps({"file_path": "/tmp/demo.txt"}),
                            },
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "u2",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "w1", "content": "ok"}
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "uuid": "a2",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "id": "b1",
                                "input": {"command": "false"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "u3",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "b1",
                                "content": "error: command failed",
                                "is_error": True,
                            }
                        ],
                    },
                },
            ]
            _write_jsonl(path, entries)
            messages = richmsg.RichReader(_session("claude", path)).read_all()
            tools = [t for m in messages if m.role == "assistant" for t in m.tools]

            write = next(t for t in tools if t.call_id == "w1")
            self.assertEqual(write.kind, "write")
            self.assertEqual(write.status, "ok")

            bash = next(t for t in tools if t.call_id == "b1")
            self.assertEqual(bash.kind, "shell")
            self.assertEqual(bash.status, "error")

    def test_task_notification_and_interrupt_are_dropped(self) -> None:
        """Claude 的到点通知和中断标记不能占手机用户气泡。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "origin": {"kind": "task-notification"},
                        "message": {"role": "user", "content": "<task-notification>到点了"},
                    },
                    {
                        "type": "user",
                        "origin": {"kind": "human"},
                        "message": {"role": "user", "content": "[Request interrupted by user]"},
                    },
                    {
                        "type": "user",
                        "origin": {"kind": "human"},
                        "message": {"role": "user", "content": "<task-notification>伪装成人话"},
                    },
                    {
                        "type": "user",
                        "origin": {"kind": "human"},
                        "message": {"role": "user", "content": "现在侧边栏记忆做得怎么样？"},
                    },
                ],
            )
            messages = richmsg.RichReader(_session("claude", path)).read_all()
            self.assertEqual(
                [(item.role, item.text) for item in messages],
                [("user", "现在侧边栏记忆做得怎么样？")],
            )

    def test_ask_user_question_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "AskUserQuestion",
                                    "id": "ask-1",
                                    "input": {
                                        "questions": [
                                            {
                                                "question": "选哪个？",
                                                "options": [
                                                    {"label": "方案一"},
                                                    {"label": "方案二"},
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ],
                        },
                    }
                ],
            )
            messages = richmsg.RichReader(_session("claude", path)).read_all()
            tool = messages[0].tools[0]
            self.assertEqual(tool.kind, "question")
            self.assertEqual(tool.options, ["方案一", "方案二"])
            self.assertEqual(tool.status, "running")

            prompts = richmsg.pending_prompts(_session("claude", path))
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["options"], ["方案一", "方案二"])

    def test_pending_prompts_always_include_options_array(self) -> None:
        """无选项的提问也必须带 options:[]，否则手机端整份 prompts 解码失败。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "AskUserQuestion",
                                    "id": "ask-2",
                                    "input": {
                                        "questions": [{"question": "随便说点什么？"}]
                                    },
                                }
                            ],
                        },
                    }
                ],
            )
            prompts = richmsg.pending_prompts(_session("claude", path))
            self.assertEqual(len(prompts), 1)
            self.assertEqual(prompts[0]["options"], [])


class CursorRichmsgTests(unittest.TestCase):
    def test_tool_call_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _cursor_db(
                path,
                [
                    {"role": "user", "content": "查一下"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "正在搜索"},
                            {
                                "type": "tool-call",
                                "toolName": "grep",
                                "toolCallId": "g1",
                                "args": {"pattern": "TODO"},
                            },
                        ],
                    },
                    {
                        "role": "tool",
                        "content": [
                            {
                                "type": "tool-result",
                                "toolCallId": "g1",
                                "result": "src/main.py:42",
                            }
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool-call",
                                "toolName": "AskQuestion",
                                "toolCallId": "q1",
                                "args": {"question": "继续？", "choices": ["好", "停"]},
                            }
                        ],
                    },
                ],
            )
            messages = richmsg.RichReader(_session("cursor", path)).read_all()
            tools = [t for m in messages if m.role == "assistant" for t in m.tools]

            grep = next(t for t in tools if t.call_id == "g1")
            self.assertEqual(grep.kind, "search")
            self.assertEqual(grep.status, "ok")

            ask = next(t for t in tools if t.call_id == "q1")
            self.assertEqual(ask.kind, "question")
            self.assertEqual(ask.options, ["好", "停"])
            self.assertEqual(ask.status, "running")

    def test_user_query_is_extracted_and_injected_context_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            _cursor_db(
                path,
                [
                    {
                        "role": "user",
                        "content": (
                            "<user_info>\nName: Tester\n</user_info>\n"
                            "<rules>\nAlways be verbose.\n</rules>\n"
                            "<user_query>\n把登录改成验证码\n</user_query>"
                        ),
                    },
                    {"role": "user", "content": "<user_info>\n只剩上下文\n</user_info>"},
                ],
            )
            messages = richmsg.RichReader(_session("cursor", path)).read_all()
            user_texts = [item.text for item in messages if item.role == "user"]
            self.assertEqual(user_texts, ["把登录改成验证码"])



class RichmsgSerializationTests(unittest.TestCase):
    def test_to_dict_includes_options_in_tools(self) -> None:
        tool = richmsg.ToolCall(
            call_id="1",
            name="AskUserQuestion",
            kind="question",
            summary="选方案",
            options=["A", "B"],
        )
        message = richmsg.RichMessage(seq=1, role="assistant", tools=[tool])
        data = message.to_dict()
        self.assertIn("tools", data)
        self.assertEqual(data["tools"][0]["options"], ["A", "B"])
        restored = richmsg.RichMessage.from_dict(data)
        self.assertEqual(restored.seq, 1)
        self.assertEqual(restored.tools[0].options, ["A", "B"])
        self.assertEqual(restored.tools[0].call_id, "1")

    def test_to_dict_keeps_question_groups(self) -> None:
        tool = richmsg.ToolCall(
            call_id="q1",
            name="AskQuestion",
            kind="question",
            summary="AskQuestion",
            question_groups=[
                {"summary": "会补上来吗？", "options": ["A", "B"]},
                {"summary": "哪个助手？", "options": ["Cursor", "Pi"]},
            ],
        )
        data = tool.to_dict()
        self.assertNotIn("options", data)
        self.assertEqual(len(data["questions"]), 2)
        restored = richmsg.ToolCall.from_dict(data)
        self.assertEqual(restored.question_groups[1]["options"], ["Cursor", "Pi"])

    def test_to_wire_dict_omits_tool_bodies(self) -> None:
        tool = richmsg.ToolCall(
            call_id="t1",
            name="Bash",
            kind="bash",
            summary="ls",
            detail="ls -la /tmp",
            output="file1\nfile2\n" * 40,
            status="ok",
        )
        message = richmsg.RichMessage(seq=3, role="assistant", text="done", tools=[tool])
        wire = message.to_wire_dict()
        self.assertEqual(wire["text"], "done")
        summary = wire["tools"][0]
        self.assertEqual(summary["id"], "t1")
        self.assertTrue(summary["has_detail"])
        self.assertNotIn("status", summary)
        self.assertNotIn("output", summary)
        self.assertNotIn("detail", summary)
        detail_page = message.tool_detail_page(tool_id="t1")
        self.assertEqual(detail_page["total"], 1)
        self.assertIn("file1", detail_page["tools"][0]["output"])
        self.assertEqual(detail_page["tools"][0]["detail"], "ls -la /tmp")
        self.assertNotIn("status", detail_page["tools"][0])

    def test_question_summary_keeps_detail_for_prompts(self) -> None:
        tool = richmsg.ToolCall(
            call_id="q1",
            name="Ask",
            kind="question",
            summary="pick",
            detail="Which path?",
            options=["A", "B"],
            status="running",
        )
        summary = tool.to_summary_dict()
        self.assertEqual(summary["detail"], "Which path?")
        self.assertEqual(summary["options"], ["A", "B"])
        self.assertEqual(summary["status"], "running")
        self.assertTrue(summary["has_detail"])


class RichmsgIncrementalTests(unittest.TestCase):
    def test_codex_tool_result_is_reemitted_on_poll(self) -> None:
        """增量轮询时结果回填必须再推宿主消息，否则手机工具卡永远停在 running。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            path.write_text("", encoding="utf-8")
            reader = richmsg.RichReader(_session("codex", path))

            _write_jsonl(
                path,
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "Bash",
                            "call_id": "c1",
                            "arguments": json.dumps({"command": "echo hi"}),
                        },
                    }
                ],
            )
            first = reader.poll()
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0].tools[0].status, "running")
            seq = first[0].seq

            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": "c1",
                                "output": "hi\n",
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            second = reader.poll()
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0].seq, seq)
            self.assertEqual(second[0].tools[0].status, "ok")
            self.assertIn("hi", second[0].tools[0].output)

    def test_claude_visible_thinking_is_shown_as_reply_text(self) -> None:
        """Claude Code renders non-empty thinking inline; the phone must too."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "uuid": "u1",
                        "message": {"role": "user", "content": "good job"},
                    },
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "id": "msg_1",
                            "role": "assistant",
                            "content": [{"type": "thinking", "thinking": "", "signature": "x"}],
                        },
                    },
                    {
                        "type": "assistant",
                        "uuid": "a2",
                        "parentUuid": "a1",
                        "message": {
                            "id": "msg_1",
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": "Glad the picture showed up.", "signature": "y"}
                            ],
                        },
                    },
                    {
                        "type": "assistant",
                        "uuid": "a3",
                        "parentUuid": "a2",
                        "message": {
                            "id": "msg_2",
                            "role": "assistant",
                            "content": [{"type": "text", "text": "The diff touches no layout."}],
                        },
                    },
                ],
            )
            messages = richmsg.RichReader(_session("claude", path)).read_all()
        texts = [message.text for message in messages if message.role == "assistant"]
        joined = "\n".join(texts)
        self.assertIn("Glad the picture showed up.", joined)
        self.assertIn("The diff touches no layout.", joined)
        self.assertNotIn("", [text.strip() for text in texts])

    def test_claude_tool_result_is_reemitted_on_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            path.write_text("", encoding="utf-8")
            reader = richmsg.RichReader(_session("claude", path))
            _write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Bash",
                                    "id": "b1",
                                    "input": {"command": "true"},
                                }
                            ],
                        },
                    }
                ],
            )
            first = reader.poll()
            self.assertEqual(first[0].tools[0].status, "running")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "user",
                            "uuid": "u2",
                            "message": {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "b1",
                                        "content": "ok",
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            second = reader.poll()
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0].seq, first[0].seq)
            self.assertEqual(second[0].tools[0].status, "ok")


class PlainRuntimeRichmsgTests(unittest.TestCase):
    def test_every_default_runtime_has_a_remote_parser(self) -> None:
        from corral.runtime import default_registry

        missing = [runtime_id for runtime_id in default_registry().ids if runtime_id not in richmsg._PARSERS]
        self.assertEqual(missing, [], f"手机远程解析器漏登记：{missing}")

    def test_unknown_runtime_returns_no_messages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unknown.jsonl"
            _write_jsonl(path, [{"type": "message", "message": {"role": "user", "content": "hi"}}])
            messages = richmsg.RichReader(_session("unknown-runtime", path)).read_all()
        self.assertEqual(messages, [])

    def test_pi_user_and_assistant_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-08-26T00-00-00-000Z_sess-1.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "session",
                        "id": "sess-1",
                        "timestamp": "2026-08-26T00:00:00Z",
                        "cwd": directory,
                    },
                    {
                        "type": "message",
                        "id": "u1",
                        "parentId": None,
                        "timestamp": "2026-08-26T00:00:01Z",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "你好"}],
                        },
                    },
                    {
                        "type": "message",
                        "id": "a1",
                        "parentId": "u1",
                        "timestamp": "2026-08-26T00:00:02Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": "先想想"},
                                {"type": "text", "text": "收到"},
                            ],
                        },
                    },
                ],
            )
            messages = richmsg.RichReader(_session("pi", path)).read_all()
        self.assertEqual([(item.role, item.text) for item in messages], [("user", "你好"), ("assistant", "收到")])

    def test_pi_tool_call_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-08-26T00-00-00-000Z_sess-tools.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "session",
                        "id": "sess-tools",
                        "timestamp": "2026-08-26T00:00:00Z",
                        "cwd": directory,
                    },
                    {
                        "type": "message",
                        "id": "u1",
                        "parentId": None,
                        "timestamp": "2026-08-26T00:00:01Z",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "查版本"}],
                        },
                    },
                    {
                        "type": "message",
                        "id": "a1",
                        "parentId": "u1",
                        "timestamp": "2026-08-26T00:00:02Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "我先跑一下"},
                                {
                                    "type": "toolCall",
                                    "id": "call_bash_1",
                                    "name": "bash",
                                    "arguments": {"command": "corral --version"},
                                },
                            ],
                        },
                    },
                    {
                        "type": "message",
                        "id": "tr1",
                        "parentId": "a1",
                        "timestamp": "2026-08-26T00:00:03Z",
                        "message": {
                            "role": "toolResult",
                            "toolCallId": "call_bash_1",
                            "toolName": "bash",
                            "isError": False,
                            "content": [{"type": "text", "text": "corral 0.24.207"}],
                        },
                    },
                ],
            )
            reader = richmsg.RichReader(_session("pi", path))
            first = reader.read_all()
        self.assertEqual(richmsg.supports_tool_calls("pi"), True)
        self.assertEqual(len(first), 2)
        self.assertEqual(first[0].role, "user")
        self.assertEqual(first[1].role, "assistant")
        self.assertEqual(first[1].text, "我先跑一下")
        self.assertEqual(len(first[1].tools), 1)
        tool = first[1].tools[0]
        self.assertEqual(tool.name, "bash")
        self.assertEqual(tool.kind, "shell")
        self.assertEqual(tool.status, "ok")
        self.assertIn("corral 0.24.207", tool.output)
        self.assertTrue(tool.has_body())

    def test_pi_poll_reemits_when_tool_result_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "2026-08-26T00-00-00-000Z_sess-live.jsonl"
            _write_jsonl(
                path,
                [
                    {
                        "type": "session",
                        "id": "sess-live",
                        "timestamp": "2026-08-26T00:00:00Z",
                        "cwd": directory,
                    },
                    {
                        "type": "message",
                        "id": "u1",
                        "parentId": None,
                        "timestamp": "2026-08-26T00:00:01Z",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "读文件"}],
                        },
                    },
                    {
                        "type": "message",
                        "id": "a1",
                        "parentId": "u1",
                        "timestamp": "2026-08-26T00:00:02Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "toolCall",
                                    "id": "call_read_1",
                                    "name": "read",
                                    "arguments": {"path": "/tmp/a.py"},
                                },
                            ],
                        },
                    },
                ],
            )
            reader = richmsg.RichReader(_session("pi", path))
            first = reader.poll()
            self.assertEqual(len(first), 2)
            self.assertEqual(first[1].tools[0].status, "running")
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "message",
                            "id": "tr1",
                            "parentId": "a1",
                            "timestamp": "2026-08-26T00:00:03Z",
                            "message": {
                                "role": "toolResult",
                                "toolCallId": "call_read_1",
                                "toolName": "read",
                                "isError": False,
                                "content": [{"type": "text", "text": "print(1)"}],
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            second = reader.poll()
            self.assertEqual(len(second), 1)
            self.assertEqual(second[0].seq, first[1].seq)
            self.assertEqual(second[0].tools[0].status, "ok")
            self.assertIn("print(1)", second[0].tools[0].output)

    def test_jsonl_incomplete_last_line_is_not_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            complete = json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "先到"}],
                    },
                },
                ensure_ascii=False,
            )
            path.write_bytes(complete.encode("utf-8") + b"\n{\"type\":\"assistant\"")
            reader = richmsg.RichReader(_session("claude", path))
            first = reader.read_all()
            self.assertEqual([item.text for item in first], ["先到"])
            tail = json.dumps(
                {
                    "type": "assistant",
                    "uuid": "a2",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "后到"}],
                    },
                },
                ensure_ascii=False,
            )
            path.write_bytes(complete.encode("utf-8") + b"\n" + tail.encode("utf-8") + b"\n")
            second = reader.poll()
            self.assertEqual([item.text for item in second], ["后到"])


def _claude_assistant_line(index: int, *, pad: str = "") -> dict:
    return {
        "type": "assistant",
        "uuid": f"u{index}",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": f"尾部消息-{index}{pad}"}],
        },
    }


class RichmsgTailWindowTests(unittest.TestCase):
    def test_jsonl_read_all_parses_tail_not_whole_file(self) -> None:
        # SessKit-backed Claude/Codex 用整代物化 + 尾部窗口：SessKit 冷启动做
        # 一次完整解释（不再按字节切块），Corral 只返回尾部窗口并用稳定全局
        # 序号翻页；无变更 poll 只 stat 文件。这是 P1 迁移后的新契约。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            total = 4000
            _write_jsonl(path, [_claude_assistant_line(index) for index in range(total)])
            reader = richmsg.RichReader(_session("claude", path))
            messages = reader.read_all(limit=80)
            self.assertEqual(len(messages), 80)
            self.assertEqual(messages[-1].text, f"尾部消息-{total - 1}")
            self.assertEqual(messages[0].text, f"尾部消息-{total - 80}")
            self.assertTrue(reader.has_earlier())

            extra = _claude_assistant_line(total)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(extra, ensure_ascii=False) + "\n")
            added = reader.poll()
            self.assertEqual([item.text for item in added], [f"尾部消息-{total}"])

            older_than = messages[0].seq
            earlier = reader.read_earlier(80, before_seq=older_than)
            self.assertEqual(len(earlier), 80)
            self.assertLess(earlier[-1].seq, older_than)
            self.assertEqual(earlier[-1].text, f"尾部消息-{total - 81}")
            self.assertTrue(reader.has_earlier())

    def test_jsonl_read_all_io_failure_returns_empty(self) -> None:
        reader = richmsg.RichReader(_session("claude", Path("/no/such/claude.jsonl")))
        self.assertEqual(reader.read_all(), [])

    def test_cursor_read_all_takes_tail_rows_not_full_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.db"
            total = 800
            blobs = [
                {"role": "user", "content": f"<user_query>\n游标消息-{index}\n</user_query>"}
                for index in range(total)
            ]
            _cursor_db(path, blobs)
            reader = richmsg.RichReader(_session("cursor", path))
            messages = reader.read_all(limit=80)
            self.assertGreater(len(messages), 0)
            self.assertLess(reader.parsed_line_count, total)
            self.assertEqual(messages[-1].text, f"游标消息-{total - 1}")
            self.assertTrue(reader.has_earlier())
            older_than = messages[0].seq
            earlier = reader.read_earlier(80, before_seq=older_than)
            self.assertGreater(len(earlier), 0)
            self.assertLess(earlier[-1].seq, older_than)
            self.assertLess(reader.parsed_line_count, total)


if __name__ == "__main__":
    unittest.main()
