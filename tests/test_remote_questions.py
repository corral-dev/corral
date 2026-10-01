from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral.remote import questions, richmsg


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _claude_ask(path: Path, questions_input: list[dict]) -> dict:
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
                            "id": "toolu_1",
                            "input": {"questions": questions_input},
                        }
                    ],
                },
            }
        ],
    )
    return {"source": "claude", "path": str(path), "live": True}


COLOR = {
    "question": "Pick a color",
    "header": "Color",
    "multiSelect": False,
    "options": [{"label": "Red", "description": "warm"}, {"label": "Blue", "description": "cool"}],
}
PETS = {
    "question": "Pick pets",
    "header": "Pets",
    "multiSelect": True,
    "options": [{"label": "Cat"}, {"label": "Dog"}, {"label": "Fish"}],
}

LAYOUT = {
    "question": "Which layout?",
    "header": "Layout",
    "multiSelect": False,
    "options": [{"label": "Two columns", "preview": "A|B"}, {"label": "Single column", "preview": "A\nB"}],
}


class NativePromptTests(unittest.TestCase):
    def _prompts(self, questions_input: list[dict]) -> list[dict]:
        with tempfile.TemporaryDirectory() as directory:
            session = _claude_ask(Path(directory) / "c.jsonl", questions_input)
            return questions.pending_prompts(session, richmsg.RichReader(session).read_all())

    def test_single_question_uses_question_text_not_tool_name(self) -> None:
        [prompt] = self._prompts([COLOR])
        self.assertEqual(prompt["summary"], "Pick a color")
        self.assertEqual(prompt["prompt"], "Pick a color")
        self.assertEqual(prompt["header"], "Color")
        self.assertEqual(prompt["request_id"], "toolu_1")
        self.assertEqual(prompt["id"], "toolu_1")
        self.assertEqual(prompt["options"], ["Red", "Blue"])
        self.assertEqual(
            prompt["option_details"],
            [{"id": "0", "label": "Red", "description": "warm"}, {"id": "1", "label": "Blue", "description": "cool"}],
        )
        self.assertTrue(prompt["allow_custom"])
        self.assertFalse(prompt["multi_select"])

    def test_multi_question_request_shares_identity(self) -> None:
        prompts = self._prompts([COLOR, PETS])
        self.assertEqual([p["id"] for p in prompts], ["toolu_1:0", "toolu_1:1"])
        self.assertEqual({p["request_id"] for p in prompts}, {"toolu_1"})
        self.assertEqual([p["question_id"] for p in prompts], ["0", "1"])
        self.assertTrue(prompts[1]["multi_select"])

    def test_history_wire_shape_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = _claude_ask(Path(directory) / "c.jsonl", [COLOR])
            tool = richmsg.RichReader(session).read_all()[0].tools[0]
        wire = tool.to_summary_dict()
        self.assertNotIn("questions_meta", wire)
        self.assertEqual(wire["options"], ["Red", "Blue"])
        restored = richmsg.ToolCall.from_dict(tool.to_dict())
        self.assertEqual(restored.questions_meta, tool.questions_meta)

    def test_runtime_without_answer_path_disables_typed_answers(self) -> None:
        tool = richmsg.ToolCall(
            call_id="q",
            name="AskQuestion",
            kind="question",
            summary="AskQuestion",
            questions_meta=[{"id": "0", "prompt": "Go?", "options": [{"id": "0", "label": "Yes"}]}],
        )
        messages = [richmsg.RichMessage(seq=1, role="assistant", tools=[tool])]
        [prompt] = questions.pending_prompts({"source": "cursor"}, messages)
        self.assertFalse(prompt["allow_custom"])


def _entries(meta: list[dict], request_id: str = "r") -> list[dict]:
    return richmsg.prompt_entries(request_id=request_id, name="AskUserQuestion", questions=meta)


def _meta(*items: dict) -> list[dict]:
    return richmsg._question_meta("question", {"questions": list(items)})


class KeyPlanTests(unittest.TestCase):
    def test_claude_multi_question_plan_matches_verified_picker(self) -> None:
        prompts = _entries(_meta(COLOR, PETS, {"question": "Size", "options": [{"label": "S"}, {"label": "M"}]}))
        answers = questions._validated_answers(
            prompts,
            [
                {"question_id": "0", "selected": ["1"], "text": ""},
                {"question_id": "1", "selected": ["0", "2"], "text": "Hamster"},
                {"question_id": "2", "selected": ["0"], "text": "a bit bigger"},
            ],
        )
        self.assertEqual(
            questions.key_plan("claude", prompts, answers),
            [
                ("key", "2"),
                ("key", "1"),
                ("key", "3"),
                ("key", "Down"),
                ("key", "Down"),
                ("key", "Down"),
                ("paste", "Hamster"),
                ("key", "Down"),
                ("key", "Enter"),
                ("key", "3"),
                ("paste", "S — a bit bigger"),
                ("key", "Enter"),
                ("key", "1"),
            ],
        )

    def test_claude_preview_question_uses_notes_and_enter(self) -> None:
        # Live 2.1.286: preview picker has no custom row; digit highlights, n opens notes.
        prompts = _entries(_meta(LAYOUT, COLOR))
        self.assertEqual([p["custom_needs_choice"] for p in prompts], [True, False])
        answers = questions._validated_answers(
            prompts,
            [
                {"question_id": "0", "selected": ["1"], "text": "my own idea"},
                {"question_id": "1", "selected": ["1"], "text": ""},
            ],
        )
        self.assertEqual(
            questions.key_plan("claude", prompts, answers),
            [
                ("key", "2"),
                ("key", "n"),
                ("paste", "my own idea"),
                ("key", "Enter"),
                ("key", "2"),
                ("key", "1"),
            ],
        )

    def test_claude_lone_preview_choice_commits_with_enter(self) -> None:
        prompts = _entries(_meta(LAYOUT))
        answers = questions._validated_answers(prompts, [{"question_id": "0", "selected": ["0"], "text": ""}])
        self.assertEqual(questions.key_plan("claude", prompts, answers), [("key", "1"), ("key", "Enter")])

    def test_preview_question_rejects_text_without_choice(self) -> None:
        prompts = _entries(_meta(LAYOUT))
        with self.assertRaises(questions.QuestionOutcome) as caught:
            questions._validated_answers(prompts, [{"question_id": "0", "selected": [], "text": "other"}])
        self.assertEqual(caught.exception.status, "unavailable")

    def test_claude_single_choice_submits_on_digit(self) -> None:
        prompts = _entries(_meta(COLOR))
        answers = questions._validated_answers(prompts, [{"question_id": "0", "selected": ["0"], "text": ""}])
        self.assertEqual(questions.key_plan("claude", prompts, answers), [("key", "1")])

    def test_codex_plan_uses_none_of_the_above_and_option_notes(self) -> None:
        prompts = _entries(_meta(COLOR, COLOR, COLOR))
        answers = questions._validated_answers(
            prompts,
            [
                {"question_id": "0", "selected": ["1"], "text": ""},
                {"question_id": "1", "selected": [], "text": "green"},
                {"question_id": "2", "selected": ["1"], "text": "dark"},
            ],
        )
        self.assertEqual(
            questions.key_plan("codex", prompts, answers),
            [
                ("key", "2"),
                ("key", "Down"),
                ("key", "Down"),
                ("key", "Enter"),
                ("paste", "green"),
                ("key", "Enter"),
                ("key", "Down"),
                ("key", "Tab"),
                ("paste", "dark"),
                ("key", "Enter"),
            ],
        )


class AnswerValidationTests(unittest.TestCase):
    def _answer(self, runtime: str, answers: object, request_id: str = "r", **kwargs) -> dict:
        return questions.answer(
            {"source": runtime, "id": "s"},
            _entries(_meta(COLOR, PETS)),
            request_id,
            answers,
            pane_name=kwargs.get("pane_name", lambda: "pane"),
        )

    def test_unknown_request_is_stale(self) -> None:
        self.assertEqual(self._answer("claude", [], request_id="old")["status"], "stale")

    def test_every_question_needs_an_answer(self) -> None:
        result = self._answer("claude", [{"question_id": "0", "selected": ["0"], "text": ""}])
        self.assertEqual(result["status"], "unavailable")

    def test_unknown_option_is_stale(self) -> None:
        result = self._answer(
            "claude",
            [
                {"question_id": "0", "selected": ["9"], "text": ""},
                {"question_id": "1", "selected": ["0"], "text": ""},
            ],
        )
        self.assertEqual(result["status"], "stale")

    def test_two_choices_on_single_choice_rejected(self) -> None:
        result = self._answer(
            "claude",
            [
                {"question_id": "0", "selected": ["0", "1"], "text": ""},
                {"question_id": "1", "selected": ["0"], "text": ""},
            ],
        )
        self.assertEqual(result["status"], "unavailable")

    def test_unsupported_runtime_never_sends(self) -> None:
        with mock.patch.object(questions.embed, "send_key") as send_key, mock.patch.object(
            questions.embed, "paste"
        ) as paste:
            result = self._answer(
                "cursor",
                [
                    {"question_id": "0", "selected": ["0"], "text": ""},
                    {"question_id": "1", "selected": ["0"], "text": ""},
                ],
            )
        self.assertEqual(result["status"], "unavailable")
        send_key.assert_not_called()
        paste.assert_not_called()

    def test_picker_must_be_on_screen_before_any_key(self) -> None:
        with mock.patch.object(questions.embed, "capture", return_value="❯ plain composer"), mock.patch.object(
            questions.embed, "send_key"
        ) as send_key:
            result = self._answer(
                "claude",
                [
                    {"question_id": "0", "selected": ["0"], "text": ""},
                    {"question_id": "1", "selected": ["0"], "text": ""},
                ],
            )
        self.assertEqual(result["status"], "unavailable")
        send_key.assert_not_called()

    def test_delivered_once_picker_closes(self) -> None:
        screens = iter(
            ["Pick a\ncolor\n 1. Red\nEnter to select · Tab/Arrow keys to navigate"] + ["❯ done"] * 20
        )
        with mock.patch.object(questions.embed, "capture", side_effect=lambda *a, **k: next(screens)), \
                mock.patch.object(questions.embed, "send_key", return_value=True) as send_key, \
                mock.patch.object(questions.time, "sleep"):
            result = self._answer(
                "claude",
                [
                    {"question_id": "0", "selected": ["0"], "text": ""},
                    {"question_id": "1", "selected": ["1"], "text": ""},
                ],
            )
        self.assertEqual(result, {"status": "delivered"})
        self.assertEqual(
            [call.args[1] for call in send_key.call_args_list],
            ["1", "2", "Down", "Down", "Down", "Down", "Enter", "1"],
        )


FORM = {
    "id": "frm_1",
    "sessionID": "ses_1",
    "metadata": {"kind": "question"},
    "fields": [
        {
            "key": "q0",
            "title": "Color",
            "description": "Pick a color",
            "type": "string",
            "options": [{"value": "Red", "label": "Red", "description": "warm"}, {"value": "Blue", "label": "Blue"}],
            "custom": True,
        },
        {
            "key": "q1",
            "title": "Fruit",
            "description": "Pick fruits",
            "type": "multiselect",
            "options": [{"value": "Apple", "label": "Apple"}, {"value": "Pear", "label": "Pear"}],
        },
    ],
}


class OpenCodeFormTests(unittest.TestCase):
    session = {"source": "opencode", "id": "ses_1", "live": True}

    def test_pending_form_becomes_one_request(self) -> None:
        with mock.patch.object(questions, "_opencode_api", return_value=(0, json.dumps({"data": [FORM]}))):
            prompts = questions.pending_prompts(self.session, [])
        self.assertEqual({p["request_id"] for p in prompts}, {"frm_1"})
        self.assertEqual([p["question_id"] for p in prompts], ["q0", "q1"])
        self.assertEqual(prompts[0]["prompt"], "Pick a color")
        self.assertEqual(prompts[0]["header"], "Color")
        self.assertTrue(prompts[1]["multi_select"])
        self.assertTrue(prompts[1]["allow_custom"])

    def test_unsupported_field_type_hides_the_form(self) -> None:
        form = {**FORM, "fields": [*FORM["fields"], {"key": "n", "type": "number"}]}
        with mock.patch.object(questions, "_opencode_api", return_value=(0, json.dumps({"data": [form]}))):
            self.assertEqual(questions.pending_prompts(self.session, []), [])

    def test_reply_payload_maps_choices_and_typed_text(self) -> None:
        with mock.patch.object(questions, "_opencode_api", return_value=(0, json.dumps({"data": [FORM]}))):
            prompts = questions.pending_prompts(self.session, [])
        answers = questions._validated_answers(
            prompts,
            [
                {"question_id": "q0", "selected": [], "text": "Purple"},
                {"question_id": "q1", "selected": ["0"], "text": "Kiwi"},
            ],
        )
        self.assertEqual(
            questions.opencode_answer_payload(prompts, answers),
            {"answer": {"q0": "Purple", "q1": ["Apple", "Kiwi"]}},
        )

    def test_reply_posts_to_same_service_form(self) -> None:
        calls: list[tuple] = []

        def fake(*args: str):
            calls.append(args)
            return (0, json.dumps({"data": [FORM]})) if args[0] == "GET" else (0, "")

        with mock.patch.object(questions, "_opencode_api", side_effect=fake):
            prompts = questions.pending_prompts(self.session, [])
            result = questions.answer(
                self.session,
                prompts,
                "frm_1",
                [
                    {"question_id": "q0", "selected": ["1"], "text": ""},
                    {"question_id": "q1", "selected": ["1"], "text": ""},
                ],
                pane_name=lambda: "unused",
            )
        self.assertEqual(result, {"status": "delivered"})
        post = calls[-1]
        self.assertEqual(post[:2], ("POST", "/api/session/ses_1/form/frm_1/reply"))
        self.assertEqual(json.loads(post[3]), {"answer": {"q0": "Blue", "q1": ["Pear"]}})

    def test_settled_form_is_stale(self) -> None:
        with mock.patch.object(questions, "_opencode_api", return_value=(0, json.dumps({"data": []}))):
            result = questions.answer(
                self.session,
                [{"request_id": "frm_1", "question_id": "q0", "option_details": [], "allow_custom": True}],
                "frm_1",
                [{"question_id": "q0", "selected": [], "text": "x"}],
                pane_name=lambda: "unused",
            )
        self.assertEqual(result["status"], "stale")


ASYNC_META = [
    {
        "id": "0",
        "prompt": "测试单选：你现在使用什么网络？",
        "options": [
            {"id": "0", "label": "Wi-Fi"},
            {"id": "1", "label": "蜂窝网络"},
            {"id": "2", "label": "其他网络"},
        ],
    },
    {"id": "1", "prompt": "测试自由填写：请随便写一句话。", "options": []},
]


def _async_prompts(request_id: str = "call_async1") -> list[dict]:
    return richmsg.prompt_entries(
        request_id=request_id, name="request_user_input_async", questions=ASYNC_META
    )


class AsyncQuestionTests(unittest.TestCase):
    def test_question_id_matches_official_stringify(self) -> None:
        prompts = _async_prompts("call_l7S70bSuBBLtuQPbefDhoU70")
        self.assertEqual(
            [p["question_id"] for p in prompts],
            [
                '["request_user_input_async","call_l7S70bSuBBLtuQPbefDhoU70",0]',
                '["request_user_input_async","call_l7S70bSuBBLtuQPbefDhoU70",1]',
            ],
        )
        self.assertEqual({p["request_id"] for p in prompts}, {"call_l7S70bSuBBLtuQPbefDhoU70"})

    def test_envelope_uses_native_reply_shape(self) -> None:
        prompts = _async_prompts()
        answers = questions._validated_answers(
            prompts,
            [
                {"question_id": prompts[0]["question_id"], "selected": ["1"], "text": ""},
                {"question_id": prompts[1]["question_id"], "selected": [], "text": "你好"},
            ],
        )
        envelope = questions.async_envelope(prompts, answers)
        self.assertTrue(envelope.startswith("<send_user_message_question_reply>"))
        self.assertTrue(envelope.endswith("</send_user_message_question_reply>"))
        body = envelope[len("<send_user_message_question_reply>") : -len("</send_user_message_question_reply>")]
        replies = json.loads(body)
        self.assertEqual(
            replies,
            [
                {
                    "questionItemId": prompts[0]["question_id"],
                    "question": "测试单选：你现在使用什么网络？",
                    "answer": "蜂窝网络",
                },
                {
                    "questionItemId": prompts[1]["question_id"],
                    "question": "测试自由填写：请随便写一句话。",
                    "answer": "你好",
                },
            ],
        )

    def test_async_answer_sends_envelope_not_plain_chat(self) -> None:
        prompts = _async_prompts()
        with mock.patch.object(questions.embed, "paste", return_value=True) as paste, mock.patch.object(
            questions.embed, "send_key", return_value=True
        ) as send_key, mock.patch.object(questions.time, "sleep"):
            result = questions.answer(
                {"source": "codex", "id": "s"},
                prompts,
                "call_async1",
                [
                    {"question_id": prompts[0]["question_id"], "selected": ["0"], "text": ""},
                    {"question_id": prompts[1]["question_id"], "selected": [], "text": "hi"},
                ],
                pane_name=lambda: "pane-1",
            )
        self.assertEqual(result, {"status": "delivered"})
        paste.assert_called_once()
        sent_pane, sent_text = paste.call_args.args
        self.assertEqual(sent_pane, "pane-1")
        self.assertIn("<send_user_message_question_reply>", sent_text)
        self.assertNotEqual(sent_text.strip(), "Wi-Fi")
        send_key.assert_called_once_with("pane-1", "Enter")

    def test_async_acceptance_receipt_keeps_tool_pending(self) -> None:
        from types import SimpleNamespace

        reader = richmsg.RichReader({"source": "codex", "id": "s", "path": ""})
        host = richmsg.RichMessage(seq=1, role="assistant", text="")
        tool = richmsg.ToolCall(
            call_id="call_async1",
            name="request_user_input_async",
            kind="question",
            summary="q",
            questions_meta=richmsg._question_meta(
                "question", {"questions": [{"title": "Q?", "options": ["A", "B"]}]},
            ),
        )
        reader._register_tool(host, tool)
        host.tools.append(tool)
        receipt = SimpleNamespace(
            type="tool_result", call_id="call_async1", raw_output='{"accepted":true}', result=None
        )
        self.assertEqual(
            richmsg._feed_typed_event(
                reader, receipt, runtime="codex", groups={}, batch=richmsg._TypedBatch()
            ),
            (None, False),
        )
        self.assertIn("call_async1", reader._pending)
        self.assertEqual(
            [p["request_id"] for p in richmsg.pending_prompts_from_messages([host])],
            ["call_async1"],
        )
        real_result = SimpleNamespace(
            type="tool_result", call_id="call_async1", raw_output="done", result=None
        )
        richmsg._feed_typed_event(
            reader, real_result, runtime="codex", groups={}, batch=richmsg._TypedBatch()
        )
        self.assertNotIn("call_async1", reader._pending)


def _codex_user(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def _codex_assistant(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _codex_async_call(call_id: str, asked: list[dict]) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "request_user_input_async",
            "call_id": call_id,
            "arguments": json.dumps({"questions": asked}, ensure_ascii=False),
        },
    }


def _codex_async_item(call_id: str, asked: list[dict], body: str) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "item": {
                "type": "AgentMessage",
                "id": call_id,
                "content": [{"type": "Text", "text": body}],
                "phase": "final_answer",
                "delivery": "async",
                "questions": asked,
            },
        },
    }


def _codex_output(call_id: str, output: str) -> dict:
    return {
        "type": "response_item",
        "payload": {"type": "function_call_output", "call_id": call_id, "output": output},
    }


def _codex_abort(message: str) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "last_agent_message": None,
            "error": {"message": message, "codex_error_info": "turn_aborted"},
        },
    }


def _codex_complete(text: str) -> dict:
    return {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "last_agent_message": text,
        },
    }


_ASYNC_ASKED = [
    {"title": "测试单选：你现在使用什么网络？", "options": ["Wi-Fi", "蜂窝网络", "其他网络"]},
    {"title": "测试自由填写：请随便写一句话。", "options": None},
    {"title": "测试多题提交：操作是否顺畅？", "options": ["顺畅", "遇到问题"]},
]
_ASYNC_BODY = "测试单选：你现在使用什么网络？\n- Wi-Fi\n- 蜂窝网络\n- 其他网络"


def _async_history(*extra: dict, call_id: str = "call_async1") -> list[dict]:
    """Real rollout shape: prompt, preface, async call, async panel, accepted receipt."""
    return [
        _codex_user("问我几个问题"),
        _codex_assistant("我会发三个测试问题。"),
        _codex_async_call(call_id, _ASYNC_ASKED),
        _codex_async_item(call_id, _ASYNC_ASKED, _ASYNC_BODY),
        _codex_output(call_id, '{"accepted":true}'),
        *extra,
    ]


def _codex_pending(rows: list[dict]) -> tuple[list, list[dict]]:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "codex.jsonl"
        _write_jsonl(path, rows)
        session = {"source": "codex", "path": str(path), "id": "async", "cwd": directory}
        messages = richmsg.RichReader(session).read_all()
        return messages, questions.pending_prompts(session, messages)


class AsyncSettlementTests(unittest.TestCase):
    """Async panels stay up while the turn continues; only real settlement clears them.

    Every case runs the real JSONL → SessKit typed-event → RichMessage path,
    mirroring /tmp/corral-accept-20261001/{accepted,continued}.jsonl.
    """

    def test_commentary_after_accept_keeps_pending(self) -> None:
        _, prompts = _codex_pending(
            _async_history(_codex_assistant("Continuing the work while you answer."))
        )
        self.assertEqual(len(prompts), 3)
        self.assertEqual({p["request_id"] for p in prompts}, {"call_async1"})

    def test_unrelated_tool_activity_keeps_pending(self) -> None:
        rows = _async_history(
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "exec_1",
                    "arguments": json.dumps({"cmd": "ls"}),
                },
            },
            _codex_output("exec_1", "ok"),
            _codex_assistant("Still working on it."),
        )
        _, prompts = _codex_pending(rows)
        self.assertEqual(len(prompts), 3)

    def test_native_envelope_reply_settles(self) -> None:
        question_id = '["request_user_input_async","call_async1",0]'
        envelope = (
            "<send_user_message_question_reply>\n"
            + json.dumps(
                [{"questionItemId": question_id, "question": "q", "answer": "Wi-Fi"}]
            )
            + "\n</send_user_message_question_reply>"
        )
        _, prompts = _codex_pending(_async_history(_codex_user(envelope)))
        self.assertEqual(prompts, [])

    def test_ordinary_steering_settles(self) -> None:
        # Official TUI submits a prompt through clear_pending_questions
        # (chatwidget/input_submission.rs); steering is settlement, not an answer.
        _, prompts = _codex_pending(_async_history(_codex_user("先别问了，继续吧")))
        self.assertEqual(prompts, [])

    def test_turn_abort_settles(self) -> None:
        messages, prompts = _codex_pending(_async_history(_codex_abort("acceptance interrupted")))
        self.assertEqual(prompts, [])
        statuses = [
            tool.status
            for message in messages
            for tool in message.tools
            if tool.name == "request_user_input_async"
        ]
        self.assertEqual(statuses, ["error"])

    def test_replacement_request_shows_only_latest(self) -> None:
        second = [{"title": "Second?", "options": ["Yes", "No"]}]
        rows = _async_history(
            _codex_assistant("One more thing while you answer."),
            _codex_async_call("call_async2", second),
            _codex_async_item("call_async2", second, "Second?\n- Yes\n- No"),
            _codex_output("call_async2", '{"accepted":true}'),
        )
        _, prompts = _codex_pending(rows)
        self.assertEqual([p["request_id"] for p in prompts], ["call_async2"])

    def test_sync_question_still_cleared_by_commentary(self) -> None:
        rows = [
            _codex_user("Deploy?"),
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "request_user_input",
                    "call_id": "ask-1",
                    "arguments": json.dumps(
                        {
                            "questions": [
                                {
                                    "id": "q1",
                                    "question": "Proceed?",
                                    "header": "Deploy",
                                    "options": [{"label": "Yes"}, {"label": "No"}],
                                }
                            ]
                        }
                    ),
                },
            },
            _codex_assistant("Never mind, continuing."),
        ]
        _, prompts = _codex_pending(rows)
        self.assertEqual(prompts, [])

    def test_turn_error_event_settles_pending_tool(self) -> None:
        from types import SimpleNamespace

        reader = richmsg.RichReader({"source": "codex", "id": "s", "path": ""})
        host = richmsg.RichMessage(seq=1, role="assistant", text="")
        tool = richmsg.ToolCall(
            call_id="call_async1",
            name="request_user_input_async",
            kind="question",
            summary="q",
            questions_meta=richmsg._question_meta(
                "question", {"questions": [{"title": "Q?", "options": ["A", "B"]}]},
            ),
        )
        reader._register_tool(host, tool)
        host.tools.append(tool)
        error_card = SimpleNamespace(
            type="assistant_message",
            text="acceptance interrupted",
            error=SimpleNamespace(scope="turn"),
            ts=None,
            evidence=None,
        )
        richmsg._feed_typed_event(
            reader, error_card, runtime="codex", groups={}, batch=richmsg._TypedBatch()
        )
        self.assertNotIn("call_async1", reader._pending)
        self.assertEqual(tool.status, "error")
        self.assertEqual(richmsg.pending_prompts_from_messages([host]), [])

    def _register_live_async(self, reader, call_id="call_async1"):
        host = richmsg.RichMessage(seq=1, role="assistant", text="")
        tool = richmsg.ToolCall(
            call_id=call_id,
            name="request_user_input_async",
            kind="question",
            summary="q",
            questions_meta=richmsg._question_meta(
                "question", {"questions": [{"title": "Q?", "options": ["A", "B"]}]},
            ),
        )
        reader._register_tool(host, tool)
        host.tools.append(tool)
        return host, tool

    def test_turn_end_lifecycle_settles_clean_completion(self) -> None:
        from types import SimpleNamespace

        reader = richmsg.RichReader({"source": "codex", "id": "s", "path": ""})
        host, tool = self._register_live_async(reader)
        end = SimpleNamespace(type="lifecycle", text="task_complete",
                              stop_reason="task_complete", error=None,
                              ts=None, evidence=None)
        host_out, is_new = richmsg._feed_typed_event(
            reader, end, runtime="codex", groups={}, batch=richmsg._TypedBatch()
        )
        self.assertEqual((host_out, is_new), (None, False))
        self.assertNotIn("call_async1", reader._pending)
        self.assertEqual(tool.status, "ok")
        self.assertEqual(richmsg.pending_prompts_from_messages([host]), [])

    def test_turn_end_lifecycle_with_error_settles_as_error(self) -> None:
        from types import SimpleNamespace

        reader = richmsg.RichReader({"source": "codex", "id": "s", "path": ""})
        host, tool = self._register_live_async(reader)
        end = SimpleNamespace(type="lifecycle", text="task_complete",
                              stop_reason="task_complete",
                              error=SimpleNamespace(scope="turn"),
                              ts=None, evidence=None)
        richmsg._feed_typed_event(
            reader, end, runtime="codex", groups={}, batch=richmsg._TypedBatch()
        )
        self.assertEqual(tool.status, "error")
        self.assertEqual(richmsg.pending_prompts_from_messages([host]), [])

    def test_native_turn_abort_lifecycle_settles(self) -> None:
        from types import SimpleNamespace

        reader = richmsg.RichReader({"source": "codex", "id": "s", "path": ""})
        host, tool = self._register_live_async(reader)
        # SessKit turn_aborted lifecycles carry a turn-scoped error.
        end = SimpleNamespace(type="lifecycle", text="turn_aborted: interrupt",
                              stop_reason="interrupt",
                              error=SimpleNamespace(scope="turn"),
                              ts=None, evidence=None)
        richmsg._feed_typed_event(
            reader, end, runtime="codex", groups={}, batch=richmsg._TypedBatch()
        )
        self.assertEqual(tool.status, "error")
        self.assertEqual(richmsg.pending_prompts_from_messages([host]), [])

    def test_unrelated_lifecycle_leaves_panel_up(self) -> None:
        from types import SimpleNamespace

        reader = richmsg.RichReader({"source": "codex", "id": "s", "path": ""})
        host, tool = self._register_live_async(reader)
        other = SimpleNamespace(type="lifecycle", text="compaction", stop_reason=None,
                                error=None, ts=None, evidence=None)
        richmsg._feed_typed_event(
            reader, other, runtime="codex", groups={}, batch=richmsg._TypedBatch()
        )
        self.assertIn("call_async1", reader._pending)
        self.assertEqual(
            [p["request_id"] for p in richmsg.pending_prompts_from_messages([host])],
            ["call_async1"],
        )

    def test_normal_completion_jsonl_settles(self) -> None:
        messages, prompts = _codex_pending(
            _async_history(_codex_assistant("All done."), _codex_complete("All done."))
        )
        self.assertEqual(prompts, [])
        statuses = [
            tool.status
            for message in messages
            for tool in message.tools
            if tool.name == "request_user_input_async"
        ]
        self.assertEqual(statuses, ["ok"])

    def test_completed_turn_stays_settled_when_later_turn_begins(self) -> None:
        messages, prompts = _codex_pending(
            _async_history(
                _codex_assistant("All done."),
                _codex_complete("All done."),
                _codex_user("Thanks, next question"),
                _codex_assistant("On it."),
            )
        )
        self.assertEqual(prompts, [])
        # Same answer through a fresh read (replay determinism).
        session_messages = list(messages)
        self.assertEqual(
            richmsg.pending_prompts_from_messages(session_messages), []
        )

    def test_answer_on_settled_request_is_stale_without_paste(self) -> None:
        from unittest import mock

        with mock.patch.object(questions.embed, "paste") as paste, mock.patch.object(
            questions.embed, "send_key"
        ) as send_key:
            result = questions.answer(
                {"source": "codex", "id": "s"},
                [],
                "call_async1",
                [{"question_id": "gone", "selected": ["0"], "text": ""}],
                pane_name=lambda: "pane-1",
            )
        self.assertEqual(result["status"], "stale")
        paste.assert_not_called()
        send_key.assert_not_called()


if __name__ == "__main__":
    unittest.main()
