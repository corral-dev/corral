"""Agent-origin marking: session/message attribution projection (W1).

Covers corral.models.infer_session_origin and
corral.transcript.message_origin / annotate_conversation. All fixtures are
in-memory dicts; no history files, caches, or services are touched.
"""

from __future__ import annotations

import json
import unittest

from corral.models import SESSION_ORIGINS, infer_session_origin
from corral.transcript import MESSAGE_ORIGINS, annotate_conversation, message_origin

HANDOFF_RAW = (
    "Task: migrate the auth table\n"
    "\n"
    "You are picking up a session from codex:abc123.\n"
    "Original session history file: /tmp/auth.jsonl\n"
    "Original working directory: ~/Codes/Shop\n"
    "migrate the auth table now"
)

HANDOFF_PEELED_EXCERPT = "Task: migrate the auth table\n\nmigrate the auth table now"

TITLE_NOISE = "你将看到一批编程助手会话的摘录，请为下面会话生成标题……"


def _session(**overrides) -> dict:
    base = {"source": "codex", "id": "abc123", "first_user_msg": "", "last_user_msg": ""}
    base.update(overrides)
    return base


class SessionOriginTests(unittest.TestCase):
    def test_origins_vocabulary(self) -> None:
        self.assertEqual(tuple(SESSION_ORIGINS), ("human", "agent", "system", "unknown"))

    def test_codex_subagent_thread_source_is_agent(self) -> None:
        result = infer_session_origin(_session(first_user_msg="do the subtask"))
        self.assertEqual(result["initiated_by"], "human")
        result = infer_session_origin(_session(first_user_msg="do the subtask", thread_source="subagent"))
        self.assertEqual(result["initiated_by"], "agent")
        self.assertEqual(result["confidence"], "high")
        self.assertIn("codex.thread_source=subagent", result["evidence"])
        self.assertIsNone(result["agent_id"])

    def test_relations_subagent_dict_and_object(self) -> None:
        result = infer_session_origin(
            _session(source="cursor", first_user_msg="hello"),
            relations=[{"kind": "subagent", "target": "parent-1"}],
        )
        self.assertEqual(result["initiated_by"], "agent")
        self.assertIn("sesskit.relation=subagent:parent-1", result["evidence"])

        class RelationStub:
            kind = "subagent"
            target = ""

        result = infer_session_origin(_session(), relations=[RelationStub()])
        self.assertEqual(result["initiated_by"], "agent")

    def test_raw_handoff_message_is_agent_high(self) -> None:
        messages = [{"role": "user", "text": HANDOFF_RAW, "timestamp": 1.0}]
        result = infer_session_origin(_session(first_user_msg=HANDOFF_PEELED_EXCERPT), messages=messages)
        self.assertEqual(result["initiated_by"], "agent")
        self.assertEqual(result["confidence"], "high")
        self.assertIn("corral.handoff_wrapper", result["evidence"])

    def test_peeled_excerpt_handoff_is_agent_medium(self) -> None:
        result = infer_session_origin(_session(first_user_msg=HANDOFF_PEELED_EXCERPT))
        self.assertEqual(result["initiated_by"], "agent")
        self.assertEqual(result["confidence"], "medium")
        self.assertIn("corral.handoff_wrapper_peeled", result["evidence"])

    def test_title_noise_is_system(self) -> None:
        result = infer_session_origin(_session(first_user_msg=TITLE_NOISE))
        self.assertEqual(result["initiated_by"], "system")
        self.assertEqual(result["confidence"], "high")

    def test_plain_human_first_message(self) -> None:
        result = infer_session_origin(_session(first_user_msg="fix the login bug"))
        self.assertEqual(result["initiated_by"], "human")
        self.assertIsNone(result["agent_id"])

    def test_empty_session_is_unknown(self) -> None:
        result = infer_session_origin(_session())
        self.assertEqual(result["initiated_by"], "unknown")
        self.assertEqual(result["confidence"], "low")

    def test_w2_agent_header_session_is_agent_with_sender(self) -> None:
        from corral.control import build_agent_text

        text = build_agent_text("fix it", from_agent="opencode:ses_abc123")
        result = infer_session_origin(_session(first_user_msg=text))
        self.assertEqual(result["initiated_by"], "agent")
        self.assertEqual(result["agent_id"], "opencode:ses_abc123")
        self.assertIn("corral.agent_header", result["evidence"])

    def test_result_is_json_serializable_and_input_unmutated(self) -> None:
        session = _session(first_user_msg="hi", thread_source="subagent")
        snapshot = dict(session)
        result = infer_session_origin(session, messages=[{"role": "user", "text": "hi"}])
        json.dumps(result)
        self.assertEqual(session, snapshot)


class MessageOriginTests(unittest.TestCase):
    def test_origins_vocabulary(self) -> None:
        self.assertEqual(tuple(MESSAGE_ORIGINS), ("human", "agent", "system", "assistant", "unknown"))

    def test_assistant_role_is_assistant(self) -> None:
        result = message_origin("assistant", "done, fixed the bug")
        self.assertEqual(result["origin"], "assistant")

    def test_title_noise_user_message_is_system(self) -> None:
        result = message_origin("user", TITLE_NOISE)
        self.assertEqual(result["origin"], "system")

    def test_handoff_user_message_is_agent(self) -> None:
        result = message_origin("user", HANDOFF_RAW)
        self.assertEqual(result["origin"], "agent")
        self.assertEqual(result["confidence"], "high")

    def test_plain_user_message_defaults_human(self) -> None:
        result = message_origin("user", "fix the login bug")
        self.assertEqual(result["origin"], "human")

    def test_empty_user_text_is_unknown(self) -> None:
        self.assertEqual(message_origin("user", "   ")["origin"], "unknown")

    def test_activity_injected_is_system(self) -> None:
        result = message_origin("user", "some wrapper chrome", activity_origin="injected")
        self.assertEqual(result["origin"], "system")
        self.assertIn("sesskit.activity.origin=injected", result["evidence"])

    def test_run_mark_overrides_to_agent(self) -> None:
        result = message_origin("user", "fix the login bug", run_ref="run:42", agent_id="agent-7")
        self.assertEqual(result["origin"], "agent")
        self.assertEqual(result["agent_id"], "agent-7")
        self.assertIn("control.run=run:42", result["evidence"])

    def test_w2_agent_header_is_agent_with_sender(self) -> None:
        text = "[From agent opencode:ses_abc123 via Corral]\n\nfix the login bug"
        result = message_origin("user", text)
        self.assertEqual(result["origin"], "agent")
        self.assertEqual(result["agent_id"], "opencode:ses_abc123")
        self.assertEqual(result["confidence"], "high")
        self.assertIn("corral.agent_header", result["evidence"])

    def test_w2_agent_header_with_footer_still_agent(self) -> None:
        from corral.control import build_agent_text

        text = build_agent_text("fix it", from_agent="pi:xyz")
        result = message_origin("user", text)
        self.assertEqual(result["origin"], "agent")
        self.assertEqual(result["agent_id"], "pi:xyz")

    def test_agent_header_beats_run_mark_absence(self) -> None:
        # No run_marks passed (show-path): the header alone must attribute.
        annotated = annotate_conversation([
            {"role": "user", "text": "[From agent codex:sess_9 via Corral]\n\nhello", "timestamp": 1.0},
        ])
        self.assertEqual(annotated[0]["origin"], "agent")
        self.assertEqual(annotated[0]["agent_id"], "codex:sess_9")


    def test_unheaded_corral_run_marker_is_agent_medium(self) -> None:
        text = "check the build\n\n[Corral run=run_abc message=msg_1]"
        result = message_origin("user", text)
        self.assertEqual(result["origin"], "agent")
        self.assertIsNone(result["agent_id"])
        self.assertEqual(result["confidence"], "medium")
        self.assertIn("corral.run_marker=run_abc", result["evidence"])

    def test_unheaded_run_marker_session_is_agent(self) -> None:
        text = "check the build\n\n[Corral run=run_abc message=msg_1]"
        result = infer_session_origin(_session(first_user_msg=text))
        self.assertEqual(result["initiated_by"], "agent")
        self.assertEqual(result["confidence"], "medium")


class AnnotateConversationTests(unittest.TestCase):
    def test_show_shape_with_origin(self) -> None:
        messages = [
            {"role": "user", "text": "fix the login bug", "timestamp": 1000.0},
            {"role": "assistant", "text": "fixed", "timestamp": 1001.0},
            {"role": "user", "text": HANDOFF_RAW, "timestamp": None},
        ]
        annotated = annotate_conversation(messages)
        self.assertEqual([entry["origin"] for entry in annotated], ["human", "assistant", "agent"])
        for entry in annotated:
            for key in ("role", "text", "time", "mtime", "origin"):
                self.assertIn(key, entry)
        self.assertIsNone(annotated[2]["time"])
        self.assertIsNotNone(annotated[0]["time"])
        json.dumps(annotated)

    def test_conversation_message_objects_supported(self) -> None:
        from sesskit.models import ConversationMessage

        annotated = annotate_conversation([
            ConversationMessage("user", "hi", 2000.0),
            ConversationMessage("assistant", "hello", 2001.0),
        ])
        self.assertEqual([entry["origin"] for entry in annotated], ["human", "assistant"])
        self.assertEqual(annotated[0]["mtime"], 2000.0)

    def test_compact_mode_omits_evidence(self) -> None:
        annotated = annotate_conversation(
            [{"role": "user", "text": "hi", "timestamp": 1.0}], include_evidence=False
        )
        self.assertIn("origin", annotated[0])
        self.assertNotIn("evidence", annotated[0])
        self.assertNotIn("confidence", annotated[0])

    def test_run_marks_parallel_list(self) -> None:
        annotated = annotate_conversation(
            [
                {"role": "user", "text": "steer left", "timestamp": 1.0},
                {"role": "user", "text": "my own note", "timestamp": 2.0},
            ],
            run_marks=[{"agent_id": "agent-7", "run_ref": "run:42"}, None],
        )
        self.assertEqual(annotated[0]["origin"], "agent")
        self.assertEqual(annotated[0]["agent_id"], "agent-7")
        self.assertEqual(annotated[1]["origin"], "human")
        self.assertNotIn("agent_id", annotated[1])

    def test_length_mismatch_raises(self) -> None:
        messages = [{"role": "user", "text": "hi", "timestamp": 1.0}]
        with self.assertRaises(ValueError):
            annotate_conversation(messages, activity_origins=["human", "human"])
        with self.assertRaises(ValueError):
            annotate_conversation(messages, run_marks=[])


if __name__ == "__main__":
    unittest.main()
