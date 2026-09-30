"""Command receipt durable store and input.* wire path (Slice A)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral.remote import protocol, ratelimit
from corral.remote.command_receipts import (
    STATUS_ACCEPTED,
    STATUS_DELIVERED,
    STATUS_DISPATCHING,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    STATUS_UNSEEN,
    CommandReceiptStore,
    Receipt,
    digest_for_text,
)
from corral.remote.service import Connection, RemoteService
from corral.remote.sessions import ActionError


class FakeHub:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_send = False

    def runtimes(self):
        return []

    def list_sessions(self, query: str = "", limit: int = 0):
        return []

    def send_text(self, key: str, text: str, submit: bool):
        if self.fail_send:
            raise ActionError(protocol.E_UNAVAILABLE, "pane gone")
        self.calls.append(("send_text", key, text, submit))

    def send_keys(self, key: str, keys):
        self.calls.append(("send_keys", key, tuple(keys)))

    def send_image(self, key: str, image_bytes: bytes) -> str:
        self.calls.append(("send_image", key, len(image_bytes)))
        return "/tmp/paste.jpg"


class CommandReceiptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_duplicate_same_digest_returns_existing_without_rewrite(self) -> None:
        store = CommandReceiptStore("run-a", root=self.root)
        first, is_new = store.begin(
            device_key="dev1",
            command_id="cmd-1",
            payload_digest=digest_for_text(key="codex:a", text="hi", submit=True),
            target_key="codex:a",
            method=protocol.M_INPUT_TEXT,
        )
        self.assertTrue(is_new)
        self.assertEqual(first.status, STATUS_ACCEPTED)
        store.mark_dispatching(first)
        store.mark_delivered(first)
        again, is_new2 = store.begin(
            device_key="dev1",
            command_id="cmd-1",
            payload_digest=digest_for_text(key="codex:a", text="hi", submit=True),
            target_key="codex:a",
            method=protocol.M_INPUT_TEXT,
        )
        self.assertFalse(is_new2)
        self.assertEqual(again.status, STATUS_DELIVERED)

    def test_digest_mismatch_rejects_without_clobbering_original(self) -> None:
        store = CommandReceiptStore("run-a", root=self.root)
        digest = digest_for_text(key="codex:a", text="one", submit=True)
        first, _ = store.begin(
            device_key="dev1",
            command_id="cmd-1",
            payload_digest=digest,
            target_key="codex:a",
            method=protocol.M_INPUT_TEXT,
        )
        store.mark_dispatching(first)
        store.mark_delivered(first)
        other = digest_for_text(key="codex:a", text="two", submit=True)
        rejected, is_new = store.begin(
            device_key="dev1",
            command_id="cmd-1",
            payload_digest=other,
            target_key="codex:a",
            method=protocol.M_INPUT_TEXT,
        )
        self.assertFalse(is_new)
        self.assertEqual(rejected.status, STATUS_REJECTED)
        self.assertEqual(rejected.reason, "digest_mismatch")
        kept = store.get("dev1", "cmd-1")
        assert kept is not None
        self.assertEqual(kept.status, STATUS_DELIVERED)
        self.assertEqual(kept.payload_digest, digest)

    def test_status_unseen(self) -> None:
        store = CommandReceiptStore("run-a", root=self.root)
        wire = store.status("dev1", "never-seen")
        self.assertEqual(wire["status"], STATUS_UNSEEN)
        self.assertTrue(wire["retryable"])
        self.assertEqual(wire["host_run_id"], "run-a")

    def test_restart_accepted_becomes_interrupted_rejected(self) -> None:
        store = CommandReceiptStore("run-a", root=self.root)
        receipt, _ = store.begin(
            device_key="dev1",
            command_id="cmd-acc",
            payload_digest=digest_for_text(key="codex:a", text="x", submit=True),
            target_key="codex:a",
            method=protocol.M_INPUT_TEXT,
        )
        self.assertEqual(receipt.status, STATUS_ACCEPTED)
        restarted = CommandReceiptStore("run-b", root=self.root)
        after = restarted.get("dev1", "cmd-acc")
        assert after is not None
        self.assertEqual(after.status, STATUS_REJECTED)
        self.assertEqual(after.reason, "interrupted")
        self.assertFalse(after.retryable)

    def test_restart_dispatching_becomes_unknown(self) -> None:
        store = CommandReceiptStore("run-a", root=self.root)
        receipt, _ = store.begin(
            device_key="dev1",
            command_id="cmd-disp",
            payload_digest=digest_for_text(key="codex:a", text="y", submit=True),
            target_key="codex:a",
            method=protocol.M_INPUT_TEXT,
        )
        store.mark_dispatching(receipt)
        restarted = CommandReceiptStore("run-b", root=self.root)
        after = restarted.get("dev1", "cmd-disp")
        assert after is not None
        self.assertEqual(after.status, STATUS_UNKNOWN)
        self.assertEqual(after.reason, "host_restart")
        # Never auto-convert unknown further.
        restarted.mark_rejected(after, reason="nope", retryable=True)
        still = restarted.get("dev1", "cmd-disp")
        assert still is not None
        self.assertEqual(still.status, STATUS_UNKNOWN)

    def test_lease_expired_before_dispatch(self) -> None:
        store = CommandReceiptStore("run-a", root=self.root)
        receipt, _ = store.begin(
            device_key="dev1",
            command_id="cmd-lease",
            payload_digest=digest_for_text(key="codex:a", text="z", submit=True),
            target_key="codex:a",
            method=protocol.M_INPUT_TEXT,
            lease_sec=1,
        )
        receipt.lease_expires_mono = 0.0
        store._write(receipt)
        after = store.mark_dispatching(receipt)
        self.assertEqual(after.status, STATUS_REJECTED)
        self.assertEqual(after.reason, "lease_expired")


class CommandReceiptWireTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cache = os.environ.get("CORRAL_CACHE_DIR")
        os.environ["CORRAL_CACHE_DIR"] = self._tmp.name
        self.addCleanup(self._restore_cache)
        ratelimit.INPUT_ACTIONS.reset()
        ratelimit.PAIR_ATTEMPTS.reset()
        ratelimit.PAIR_ATTEMPTS_HOURLY.reset()
        self.hub = FakeHub()
        self.service = RemoteService(self.hub)  # type: ignore[arg-type]
        self.sent: list[dict] = []
        # Fixture LAN stub (same boundary as RemoteServiceTests): receipt flows
        # call M_HELLO per pairing. Hint content is never asserted here;
        # LAN coverage is test_remote_lan.
        self._lan_hints = mock.patch(
            "corral.remote.lan.local_hints", return_value=[]
        )
        self._lan_hints.start()
        self.addCleanup(self._lan_hints.stop)

    def _restore_cache(self) -> None:
        if self._old_cache is None:
            os.environ.pop("CORRAL_CACHE_DIR", None)
        else:
            os.environ["CORRAL_CACHE_DIR"] = self._old_cache

    def _pair_with_receipts(self) -> Connection:
        code = self.service.begin_pairing()
        connection = Connection("aa" * 32, self.sent.append)
        self.service.attach(connection)
        self.service.handle(connection, protocol.request(1, protocol.M_PAIR, {"code": code, "name": "Phone"}))
        self.sent.clear()
        self.service.handle(
            connection,
            protocol.request(1, protocol.M_HELLO, {"name": "Phone", "want_command_receipts": True}),
        )
        self.assertTrue(connection.command_receipts)
        self.sent.clear()
        return connection

    def _call(self, connection: Connection, method: str, params: dict | None = None) -> dict:
        self.sent.clear()
        self.service.handle(connection, protocol.request(1, method, params or {}))
        self.assertEqual(len(self.sent), 1)
        return self.sent[0]

    def test_legacy_input_without_capability(self) -> None:
        code = self.service.begin_pairing()
        connection = Connection("aa" * 32, self.sent.append)
        self.service.attach(connection)
        self._call(connection, protocol.M_PAIR, {"code": code, "name": "Phone"})
        reply = self._call(
            connection,
            protocol.M_INPUT_TEXT,
            {"key": "codex:abc", "text": "你好", "submit": True},
        )
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"], {"ok": True})
        self.assertEqual(self.hub.calls, [("send_text", "codex:abc", "你好", True)])

    def test_hello_advertises_command_receipts(self) -> None:
        connection = Connection("aa" * 32, self.sent.append)
        self.service.attach(connection)
        reply = self._call(connection, protocol.M_HELLO, {"name": "Phone"})
        self.assertTrue(reply["d"]["capabilities"]["command_receipts"])
        self.assertIn("host_run_id", reply["d"])
        self.assertFalse(connection.command_receipts)

    def test_want_command_receipts_opt_in_end_to_end(self) -> None:
        """Advertise → opt-in → receipted send → status query; no opt-in stays legacy."""
        code = self.service.begin_pairing()
        legacy = Connection("aa" * 32, self.sent.append)
        self.service.attach(legacy)
        self._call(legacy, protocol.M_PAIR, {"code": code, "name": "Phone"})
        hello = self._call(legacy, protocol.M_HELLO, {"name": "Phone"})
        self.assertTrue(hello["d"]["capabilities"][protocol.CAPABILITY_COMMAND_RECEIPTS])
        self.assertFalse(legacy.command_receipts)
        legacy_send = self._call(
            legacy,
            protocol.M_INPUT_TEXT,
            {"key": "codex:abc", "text": "legacy", "submit": True, "command_id": "ignored"},
        )
        self.assertEqual(legacy_send["d"], {"ok": True})

        opted = Connection("bb" * 32, self.sent.append)
        self.service.attach(opted)
        # Already paired? No — second device needs its own pair window.
        code2 = self.service.begin_pairing()
        self._call(opted, protocol.M_PAIR, {"code": code2, "name": "Phone2"})
        opted_hello = self._call(
            opted,
            protocol.M_HELLO,
            {"name": "Phone2", "want_command_receipts": True},
        )
        self.assertTrue(opted_hello["d"]["capabilities"][protocol.CAPABILITY_COMMAND_RECEIPTS])
        self.assertEqual(opted_hello["d"]["host_run_id"], self.service.host_run_id)
        self.assertTrue(opted.command_receipts)
        sent = self._call(
            opted,
            protocol.M_INPUT_TEXT,
            {"key": "codex:abc", "text": "receipted", "submit": True, "command_id": "e2e-1"},
        )
        self.assertTrue(sent["ok"])
        self.assertEqual(sent["d"]["status"], STATUS_DELIVERED)
        self.assertEqual(sent["d"]["command_id"], "e2e-1")
        self.assertEqual(sent["d"]["host_run_id"], self.service.host_run_id)
        status = self._call(opted, protocol.M_COMMAND_STATUS, {"command_id": "e2e-1"})
        self.assertEqual(status["d"]["status"], STATUS_DELIVERED)
        unseen = self._call(opted, protocol.M_COMMAND_STATUS, {"command_id": "never"})
        self.assertEqual(unseen["d"]["status"], STATUS_UNSEEN)

    def test_receipted_send_and_duplicate(self) -> None:
        connection = self._pair_with_receipts()
        params = {
            "key": "codex:abc",
            "text": "ping",
            "submit": True,
            "command_id": "cmd-dup-1",
        }
        first = self._call(connection, protocol.M_INPUT_TEXT, params)
        self.assertTrue(first["ok"])
        self.assertEqual(first["d"]["status"], STATUS_DELIVERED)
        self.assertEqual(first["d"]["command_id"], "cmd-dup-1")
        self.assertEqual(first["d"]["host_run_id"], self.service.host_run_id)
        self.assertEqual(len(self.hub.calls), 1)
        second = self._call(connection, protocol.M_INPUT_TEXT, params)
        self.assertTrue(second["ok"])
        self.assertEqual(second["d"]["status"], STATUS_DELIVERED)
        self.assertEqual(len(self.hub.calls), 1, "duplicate must not re-dispatch")

    def test_receipted_digest_mismatch_on_wire(self) -> None:
        connection = self._pair_with_receipts()
        self._call(
            connection,
            protocol.M_INPUT_TEXT,
            {"key": "codex:abc", "text": "a", "submit": True, "command_id": "cmd-mm"},
        )
        reply = self._call(
            connection,
            protocol.M_INPUT_TEXT,
            {"key": "codex:abc", "text": "b", "submit": True, "command_id": "cmd-mm"},
        )
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["status"], STATUS_REJECTED)
        self.assertEqual(reply["d"]["reason"], "digest_mismatch")
        self.assertEqual(len(self.hub.calls), 1)

    def test_command_status_unseen_and_seen(self) -> None:
        connection = self._pair_with_receipts()
        unseen = self._call(connection, protocol.M_COMMAND_STATUS, {"command_id": "nope"})
        self.assertTrue(unseen["ok"])
        self.assertEqual(unseen["d"]["status"], STATUS_UNSEEN)
        self._call(
            connection,
            protocol.M_INPUT_TEXT,
            {"key": "codex:abc", "text": "z", "submit": True, "command_id": "cmd-seen"},
        )
        seen = self._call(connection, protocol.M_COMMAND_STATUS, {"command_id": "cmd-seen"})
        self.assertEqual(seen["d"]["status"], STATUS_DELIVERED)

    def test_receipt_required_command_id(self) -> None:
        connection = self._pair_with_receipts()
        reply = self._call(
            connection,
            protocol.M_INPUT_TEXT,
            {"key": "codex:abc", "text": "x", "submit": True},
        )
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["e"]["code"], protocol.E_USAGE)

    def test_side_effect_failure_marks_rejected(self) -> None:
        connection = self._pair_with_receipts()
        self.hub.fail_send = True
        reply = self._call(
            connection,
            protocol.M_INPUT_TEXT,
            {"key": "codex:abc", "text": "x", "submit": True, "command_id": "cmd-fail"},
        )
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["status"], STATUS_REJECTED)
        self.assertEqual(reply["d"]["reason"], protocol.E_UNAVAILABLE)
        self.assertTrue(reply["d"]["retryable"])

    def test_embed_oserror_receipt_not_delivered(self) -> None:
        """Failed tmux injection must not produce a durable delivered receipt."""
        from unittest import mock

        from corral.remote.sessions import SessionHub

        connection = self._pair_with_receipts()
        real_hub = SessionHub(on_event=lambda *_: None)
        self.service.hub = real_hub  # type: ignore[assignment]
        with (
            mock.patch.object(real_hub, "_keepalive_name", return_value="fake-pane"),
            mock.patch(
                "corral.embed.subprocess.run",
                side_effect=OSError("injection failed"),
            ),
        ):
            reply = self._call(
                connection,
                protocol.M_INPUT_TEXT,
                {
                    "key": "codex:abc",
                    "text": "hi",
                    "submit": True,
                    "command_id": "cmd-oserr",
                },
            )
        self.assertTrue(reply["ok"])
        self.assertNotEqual(reply["d"]["status"], STATUS_DELIVERED)
        self.assertIn(reply["d"]["status"], (STATUS_REJECTED, STATUS_UNKNOWN))
        status = self._call(connection, protocol.M_COMMAND_STATUS, {"command_id": "cmd-oserr"})
        self.assertNotEqual(status["d"]["status"], STATUS_DELIVERED)

    def test_partial_paste_enter_marks_unknown(self) -> None:
        """Paste ok + Enter fail is ambiguous — receipt must stay unknown."""
        from unittest import mock

        from corral.remote.sessions import SessionHub

        connection = self._pair_with_receipts()
        real_hub = SessionHub(on_event=lambda *_: None)
        self.service.hub = real_hub  # type: ignore[assignment]

        def _run(argv, **_kwargs):
            completed = mock.Mock()
            # set-buffer / paste-buffer succeed; send-keys (Enter) fails.
            if "send-keys" in argv:
                completed.returncode = 1
            else:
                completed.returncode = 0
            return completed

        with (
            mock.patch.object(real_hub, "_keepalive_name", return_value="fake-pane"),
            mock.patch("corral.embed.subprocess.run", side_effect=_run),
            mock.patch("corral.remote.sessions.time.sleep", return_value=None),
            mock.patch("corral.embed._active_channel", return_value=None),
        ):
            reply = self._call(
                connection,
                protocol.M_INPUT_TEXT,
                {
                    "key": "codex:abc",
                    "text": "partial",
                    "submit": True,
                    "command_id": "cmd-partial",
                },
            )
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["status"], STATUS_UNKNOWN)
        self.assertEqual(reply["d"]["reason"], "partial_injection")

    def test_restart_simulation_via_new_store(self) -> None:
        connection = self._pair_with_receipts()
        # Force a dispatching receipt on disk, then open a new store as a restart.
        receipt = Receipt(
            command_id="cmd-crash",
            device_key=connection.device_public_key,
            status=STATUS_DISPATCHING,
            host_run_id="old-run",
            payload_digest=digest_for_text(key="codex:abc", text="c", submit=True),
            target_key="codex:abc",
            method=protocol.M_INPUT_TEXT,
            accepted_host_run_id="old-run",
            created_at=1.0,
            updated_at=1.0,
        )
        self.service.receipts._write(receipt)
        new_store = CommandReceiptStore("fresh-run", root=self.service.receipts._base())
        after = new_store.get(connection.device_public_key, "cmd-crash")
        assert after is not None
        self.assertEqual(after.status, STATUS_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
