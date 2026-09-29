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


if __name__ == "__main__":
    unittest.main()
