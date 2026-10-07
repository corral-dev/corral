"""Rebuilt sequence slots replace stale tails instead of merging into them."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from corral.remote import richmsg, sessions


class HistoryResetTests(unittest.TestCase):
    def test_rebuild_reassigning_a_user_slot_requires_replacement(self):
        reader = richmsg.RichReader({"source": "codex"})
        old = [richmsg.RichMessage(1, "user", "First"),
               richmsg.RichMessage(2, "assistant", "Old reply"),
               richmsg.RichMessage(3, "assistant", "Stale tail")]
        fresh = [richmsg.RichMessage(1, "user", "First"),
                 richmsg.RichMessage(2, "user", "New prompt")]
        richmsg._note_replacement(reader, {m.seq: richmsg._pi_fingerprint(m) for m in old}, fresh)
        self.assertEqual(reader.take_replacement(), fresh)
        self.assertIsNone(reader.take_replacement())

    def test_codex_tools_across_polls_reset_to_chronological_user_turns(self):
        from test_remote_richmsg_codex_baseline import (
            _assistant_item,
            _function_call,
            _session,
            _user_item,
            _write,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.jsonl"
            entries = [_user_item("First"), _assistant_item("Starting")]
            _write(path, entries)
            reader = richmsg.RichReader(_session(path))
            reader.read_all(100)
            for appended in [
                [_function_call("exec_command", "one", {"cmd": "true"})],
                [_function_call("exec_command", "two", {"cmd": "true"})],
                [_user_item("New prompt"), _assistant_item("Reply")],
            ]:
                entries += appended
                _write(path, entries)
                reader.poll()
            self.assertEqual(len(reader._cc_full), 6)
            reader._cc_reader = None
            reader._cc_full = None
            reader.poll()
            replacement = reader.take_replacement()
            self.assertIsNotNone(replacement)
            self.assertEqual([message.text for message in replacement],
                             ["First", "Starting", "New prompt", "Reply"])
            self.assertEqual([message.seq for message in replacement], [1, 2, 3, 4])

    def test_append_and_tool_result_updates_do_not_replace_history(self):
        reader = richmsg.RichReader({"source": "codex"})
        pending = richmsg.RichMessage(1, "assistant", "Checking", tools=[
            richmsg.ToolCall("check", "bash", "shell", "Check", status="running")])
        complete = richmsg.RichMessage.from_dict(pending.to_dict())
        complete.tools[0].status = "ok"
        richmsg._note_replacement(reader, {1: richmsg._pi_fingerprint(pending)}, [
            complete, richmsg.RichMessage(2, "user", "Next")])
        self.assertIsNone(reader.take_replacement())

    def hub(self, replacement):
        hub = object.__new__(sessions.SessionHub)
        hub._lock = threading.Lock()
        hub._persist_transcript = mock.Mock()
        hub._on_event = mock.Mock()
        reader = richmsg.RichReader({"source": "codex"})
        reader._replacement_messages = replacement
        transcript = sessions._Transcript("codex:k", "", None, 7, reader, [
            richmsg.RichMessage(99, "assistant", "Wrong old tail")])
        hub._transcripts = {"codex:k": transcript}
        watch = sessions._ConversationWatch("alias", reader, watchers=1, generation=7, canonical_key="codex:k")
        watch.deltas.append(transcript.messages)
        hub._conversations = {"alias": watch}
        return hub, reader, transcript, watch

    def test_reset_removes_old_tail_advances_generation_and_notifies_alias(self):
        fresh = [richmsg.RichMessage(1, "user", "Latest prompt")]
        hub, reader, transcript, watch = self.hub(fresh)
        hub._publish_reader_update("codex:k", reader, fresh)
        self.assertEqual(transcript.messages, fresh)
        self.assertEqual(transcript.generation, 8)
        self.assertEqual(watch.generation, 8)
        channel, event = hub._on_event.call_args.args
        self.assertEqual(channel, "session:alias")
        self.assertEqual(event["kind"], "history_reset")
        self.assertEqual(event["generation"], 8)
        self.assertEqual([m["seq"] for m in event["messages"]], [1])
        self.assertEqual(list(watch.deltas._items), [])
        hub._persist_transcript.assert_called_once_with(transcript)

    def test_empty_replacement_is_not_mistaken_for_no_delta(self):
        hub, reader, transcript, _ = self.hub([])
        hub._publish_reader_update("codex:k", reader, [])
        self.assertEqual(transcript.messages, [])
        self.assertEqual(hub._on_event.call_args.args[1]["messages"], [])


if __name__ == "__main__":
    unittest.main()
