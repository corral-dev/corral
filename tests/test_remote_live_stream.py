"""Tests for Option A hybrid chat streaming (`live_stream.py` and service capability gating)."""

from __future__ import annotations

import json
import os
import queue
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from corral.remote import live_stream, protocol
from corral.remote.richmsg import RichMessage
from corral.remote.service import Connection, RemoteService


class _FakeHub:
    def __init__(self) -> None:
        self.watched: list[str] = []
        self.active_streams_payload: list[dict] = []

    def watch_conversation(self, key: str, *, limit: int = 80, after_seq=None, generation=None) -> dict:
        self.watched.append(key)
        page = {
            "messages": [],
            "oldest_seq": 0,
            "newest_seq": 0,
            "has_more": False,
            "generation": 1,
            "resume": "tail",
            "attention": "working",
            "live": True,
        }
        if self.active_streams_payload:
            page["active_streams"] = list(self.active_streams_payload)
        return page

    def conversation_watch_page(self, key: str, *, limit: int = 80, after_seq=None, generation=None) -> dict:
        return self.watch_conversation(key, limit=limit, after_seq=after_seq, generation=generation)

    def unwatch_conversation(self, key: str) -> None:
        pass

    def runtimes(self) -> list[dict]:
        return []


class LiveStreamBrokerTests(unittest.TestCase):
    def test_codex_stream_coalesces_completes_and_reconciles(self) -> None:
        events: queue.Queue[tuple[str, dict]] = queue.Queue()
        activity: list[str] = []
        broker = live_stream.LiveStreamBroker(
            emit_event=lambda key, payload: events.put((key, payload)),
            on_turn_activity=lambda key: activity.append(key),
            after_seq_provider=lambda _key: 7,
            enable_socket=False,
            enable_opencode_sse=False,
            flush_interval=0.04,
            complete_grace_seconds=0.3,
        )
        broker.start()
        self.addCleanup(broker.stop)

        tid = "01a0d806-55d8-76a3-bb24-3e3b74232588"
        broker.ingest("codex", tid, {
            "method": "turn/started",
            "params": {"threadId": tid, "turn": {"id": "turn_1"}},
        })
        broker.ingest("codex", tid, {
            "method": "item/started",
            "params": {"threadId": tid, "turnId": "turn_1", "item": {"id": "msg_1", "type": "agentMessage"}},
        })
        broker.ingest("codex", tid, {
            "method": "item/agentMessage/delta",
            "params": {"threadId": tid, "turnId": "turn_1", "itemId": "msg_1", "delta": "Hel"},
        })
        # First chunk emits immediately as `replace`.
        key, first = events.get(timeout=1.0)
        self.assertEqual(key, f"codex:{tid}")
        self.assertEqual(first["op"], "replace")
        self.assertEqual(first["text"], "Hel")
        self.assertEqual(first["after_seq"], 7)
        self.assertFalse(first["complete"])

        # Rapid bursts coalesce before flush_interval.
        broker.ingest("codex", tid, {
            "method": "item/agentMessage/delta",
            "params": {"threadId": tid, "turnId": "turn_1", "itemId": "msg_1", "delta": "lo, "},
        })
        broker.ingest("codex", tid, {
            "method": "item/agentMessage/delta",
            "params": {"threadId": tid, "turnId": "turn_1", "itemId": "msg_1", "delta": "world"},
        })
        _, coalesced = events.get(timeout=1.0)
        self.assertEqual(coalesced["op"], "delta")
        self.assertEqual(coalesced["delta"], "lo, world")
        self.assertFalse(coalesced["complete"])

        # Mid-turn active_streams snapshot stays accurate.
        snaps = broker.active_streams(f"codex:{tid}")
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["text"], "Hello, world")

        broker.ingest("codex", tid, {
            "method": "item/completed",
            "params": {
                "threadId": tid,
                "turnId": "turn_1",
                "item": {"id": "msg_1", "type": "agentMessage", "text": "Hello, world"},
            },
        })
        _, completed = events.get(timeout=1.0)
        self.assertTrue(completed["complete"])
        self.assertIn(f"codex:{tid}", activity)

        cleared = broker.reconcile_official(
            f"codex:{tid}",
            [RichMessage(seq=8, role="assistant", text="Hello, world")],
        )
        self.assertEqual(cleared, ["msg_1"])
        self.assertEqual(broker.active_streams(f"codex:{tid}"), [])

    def test_oversized_draft_clears_and_degrades(self) -> None:
        events: queue.Queue[tuple[str, dict]] = queue.Queue()
        broker = live_stream.LiveStreamBroker(
            emit_event=lambda key, payload: events.put((key, payload)),
            enable_socket=False,
            enable_opencode_sse=False,
            max_draft_bytes=1024,
            flush_interval=0.02,
        )
        broker.start()
        self.addCleanup(broker.stop)

        sid = "pi-overflow-1"
        broker.ingest("pi", sid, {"type": "message_start", "message": {"id": "m1", "role": "assistant"}})
        broker.ingest("pi", sid, {
            "type": "message_update",
            "message": {"id": "m1", "role": "assistant"},
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "a" * 512},
        })
        _, first = events.get(timeout=1.0)
        self.assertEqual(first["op"], "replace")

        broker.ingest("pi", sid, {
            "type": "message_update",
            "message": {"id": "m1", "role": "assistant"},
            "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "b" * 700},
        })
        deadline = time.monotonic() + 1.0
        ops: list[dict] = []
        while time.monotonic() < deadline:
            try:
                _, item = events.get(timeout=0.1)
                ops.append(item)
                if item.get("op") == "clear":
                    break
            except queue.Empty:
                continue
        self.assertTrue(any(item.get("op") == "clear" for item in ops))
        self.assertEqual(broker.active_streams(f"pi:{sid}"), [])

    def test_unix_socket_receives_pi_events_with_owner_only_permissions(self) -> None:
        events: queue.Queue[tuple[str, dict]] = queue.Queue()
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="clive-") as td:
            sock_path = Path(td) / "stream.sock"
            broker = live_stream.LiveStreamBroker(
                emit_event=lambda key, payload: events.put((key, payload)),
                socket_path=sock_path,
                enable_socket=True,
                enable_opencode_sse=False,
                flush_interval=0.03,
            )
            broker.start()
            self.addCleanup(broker.stop)

            self.assertEqual(broker.socket_path, sock_path)
            self.assertEqual(sock_path.stat().st_mode & 0o777, 0o600)

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(sock_path))
                lines = [
                    {"v": 1, "runtime": "pi", "session_id": "pi-s1", "event": {
                        "type": "message_start", "message": {"id": "m1", "role": "assistant"},
                    }},
                    {"v": 1, "runtime": "pi", "session_id": "pi-s1", "event": {
                        "type": "message_update",
                        "message": {"id": "m1", "role": "assistant"},
                        "assistantMessageEvent": {
                            "type": "text_delta",
                            "contentIndex": 0,
                            "delta": "Streamed via socket",
                        },
                    }},
                ]
                client.sendall(b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in lines))

            key, first = events.get(timeout=1.5)
            self.assertEqual(key, "pi:pi-s1")
            self.assertEqual(first["text"], "Streamed via socket")

    def test_opencode_v2_event_routing_and_service_capability_gating(self) -> None:
        events: queue.Queue[tuple[str, dict]] = queue.Queue()
        broker = live_stream.LiveStreamBroker(
            emit_event=lambda key, payload: events.put((key, payload)),
            enable_socket=False,
            enable_opencode_sse=False,
            flush_interval=0.03,
        )
        broker.start()
        self.addCleanup(broker.stop)

        broker.ingest("opencode", "ses_oc1", {
            "id": "evt_1",
            "type": "session.text.delta",
            "data": {"sessionID": "ses_oc1", "assistantMessageID": "m_oc", "ordinal": 0, "delta": "OpenCode chunk"},
        })
        key, first = events.get(timeout=1.0)
        self.assertEqual(key, "opencode:ses_oc1")
        self.assertEqual(first["text"], "OpenCode chunk")

        hub = _FakeHub()
        hub.active_streams_payload = [{"stream_id": "s1", "rev": 1, "text": "Hi", "after_seq": 0, "complete": False}]
        service = RemoteService(hub=hub)
        service.is_known_device = lambda _k: True
        service.device_access = lambda _k: "full"
        service.reconcile_devices = lambda: 0

        legacy_sent: list[dict] = []
        modern_sent: list[dict] = []
        legacy_conn = Connection("k-legacy", legacy_sent.append)
        modern_conn = Connection("k-modern", modern_sent.append)
        service.attach(legacy_conn)
        service.attach(modern_conn)
        legacy_conn.paired = True
        modern_conn.paired = True

        hello_legacy = service._invoke(legacy_conn, protocol.M_HELLO, {"name": "Old"})
        _ = service._invoke(modern_conn, protocol.M_HELLO, {"name": "New", "want_chat_streaming": True})
        self.assertTrue(hello_legacy["capabilities"][protocol.CAPABILITY_CHAT_STREAMING])
        self.assertFalse(legacy_conn.chat_streaming)
        self.assertTrue(modern_conn.chat_streaming)

        watch_legacy = service._invoke(legacy_conn, protocol.M_SESSION_WATCH, {"key": "opencode:ses_oc1"})
        watch_modern = service._invoke(modern_conn, protocol.M_SESSION_WATCH, {"key": "opencode:ses_oc1"})
        self.assertNotIn("active_streams", watch_legacy)
        self.assertEqual(watch_modern["active_streams"], hub.active_streams_payload)

        service._dispatch_event(
            "session:opencode:ses_oc1",
            {"kind": "stream", "op": "delta", "stream_id": "s1", "rev": 2, "delta": "!", "complete": False},
        )
        self.assertEqual(legacy_sent, [])
        self.assertEqual(len(modern_sent), 1)
        self.assertEqual(modern_sent[0]["d"]["kind"], "stream")

        # Also verify stopped OpenCode service discovery degrades quietly to None.
        with (
            mock.patch.dict(os.environ, {"CORRAL_OPENCODE_SERVER_URL": "", "OPENCODE_SERVER_URL": ""}),
            mock.patch("corral.remote.live_stream._opencode_state_dir", return_value=Path("/nonexistent")),
        ):
            self.assertIsNone(live_stream._resolve_opencode_service())


if __name__ == "__main__":
    unittest.main()
