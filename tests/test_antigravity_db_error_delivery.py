"""A persisted Antigravity turn error reaches an already-open remote chat."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from corral.attention_signals import inspect_session
from corral.cache import history_signature
from corral.remote.richmsg import RichReader

# Native protobuf framing: field 1 (step type), field 24.3.1 (visible message),
# field 24.3.7 (HTTP status). All content is synthetic.
_QUOTA_STEP = b"\x08\x11\xc2\x01\x1f\x1a\x1d\x0a\x18Individual quota reached\x38\xad\x03"


class AntigravityDatabaseErrorDeliveryTests(unittest.TestCase):
    def test_wal_only_error_reaches_details_and_ends_working(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            transcript = root / "brain/agy-session/.system_generated/logs/transcript_full.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps({
                "step_index": 0,
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "content": "Please continue",
                "status": "DONE",
            }) + "\n", encoding="utf-8")
            db = root / "conversations/agy-session.db"
            db.parent.mkdir()
            session = {"source": "antigravity", "id": "agy-session", "path": str(transcript), "live": True}
            with closing(sqlite3.connect(db)) as writer:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, step_type INT, step_payload BLOB)")
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                before = history_signature(str(transcript))
                reader = RichReader(session)
                self.assertEqual([message.text for message in reader.poll()], ["Please continue"])
                self.assertEqual(inspect_session(session).phase, "working")
                main_mtime = db.stat().st_mtime_ns

                writer.execute("INSERT INTO steps VALUES (1, 17, ?)", (_QUOTA_STEP,))
                writer.commit()
                self.assertEqual(db.stat().st_mtime_ns, main_mtime)
                after = history_signature(str(transcript))
                self.assertNotEqual(before, after)
                self.assertEqual(before[:3], after[:3])
                self.assertEqual(inspect_session(session).phase, "idle")
                messages = [message.to_wire_dict() for message in reader.poll()]
                self.assertEqual([message["text"] for message in messages], ["Individual quota reached"])
                self.assertEqual(reader.poll(), [])
