"""Failure injection for creation identity, startup and native input confirmation."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from corral import liveness
from corral.remote import ratelimit
from corral.remote.command_receipts import CommandReceiptStore
from corral.remote.richmsg import RichMessage
from corral.remote.service import Connection, RemoteService
from corral.remote.sessions import ActionError, PartialInjectionError, SessionHub
from corral.store import SessionStore


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        ratelimit.SESSION_CREATE.reset()

    def test_creation_lost_reply_duplicate_and_restart_return_same_result(self):
        hub = mock.Mock(spec=SessionHub)
        hub.new_session.return_value = {"key": "codex:created", "id": "created"}
        service = RemoteService(hub)
        connection = Connection("fixture-device", lambda _: None)
        params = {"runtime": "codex", "cwd": "/fixture", "command_id": "creation-one"}
        first = service._session_new(connection, params)
        again = service._session_new(connection, params)
        self.assertEqual(first, again)
        self.assertEqual(hub.new_session.call_count, 1)
        reopened = CommandReceiptStore("after-restart")
        self.assertEqual(reopened.status("fixture-device", "creation-one")["result"]["session"], first["session"])
        mismatch = service._session_new(connection, {**params, "runtime": "claude"})
        self.assertEqual(mismatch["reason"], "digest_mismatch")
        self.assertEqual(hub.new_session.call_count, 1)

    def test_async_receipt_delivers_on_live_injection_without_native_history(self):
        """Option B (2026-10-15): injection into a live pane is the proof.

        The receipt must NOT wait for the native history record: a busy agent
        (Pi steering mid-turn) persists the user message only when its run
        ends. A passed legacy ``confirm`` is ignored; only a proven-dead pane
        after injection stays ``unknown`` and never replays.
        """
        hub = mock.Mock(spec=SessionHub)
        hub.resolve_session_key.side_effect = lambda key: key
        hub.delivery_binding.return_value = "same-pane"
        hub.verify_text_injected.return_value = None
        service = RemoteService(hub)
        connection = Connection("fixture-device", lambda _: None)
        service.refresh_state = lambda: SimpleNamespace(
            devices=[SimpleNamespace(public_key="fixture-device", access="full")]
        )
        calls = []

        def prepare(deadline):
            self.assertGreater(deadline, time.monotonic())
            return "baseline"

        def confirm(baseline, deadline):  # legacy native gate: must be ignored
            self.fail("native-history confirm must not gate delivery")

        from corral.remote.command_receipts import digest_for_text
        kwargs = dict(
            method="input.text", target_key="codex:a",
            digest=digest_for_text(key="codex:a", text="hello", submit=True),
            side_effect=lambda: calls.append("injected"), prepare=prepare, confirm=confirm,
        )
        first = service._run_receipted_input(connection, {"command_id": "one"}, **kwargs)
        self.assertEqual(first["status"], "accepted")
        deadline = time.monotonic() + 2
        while service.receipts.status("fixture-device", "one")["status"] != "delivered":
            self.assertLess(time.monotonic(), deadline)
            threading.Event().wait(.01)
        self.assertEqual(calls, ["injected"])
        # Same command identity never re-dispatches.
        duplicate = service._run_receipted_input(connection, {"command_id": "one"}, **kwargs)
        self.assertEqual(duplicate["status"], "delivered")
        self.assertEqual(calls, ["injected"])
        # Target admission releases asynchronously after the worker settles.
        deadline = time.monotonic() + 2
        while service._input_targets:
            self.assertLess(time.monotonic(), deadline)
            threading.Event().wait(.01)
        self.assertFalse(service._input_targets)

    def test_proven_dead_pane_after_injection_stays_unknown_without_replay(self):
        hub = mock.Mock(spec=SessionHub)
        hub.resolve_session_key.side_effect = lambda key: key
        hub.delivery_binding.return_value = "same-pane"
        hub.verify_text_injected.side_effect = PartialInjectionError("pane exited")
        service = RemoteService(hub)
        connection = Connection("fixture-device", lambda _: None)
        service.refresh_state = lambda: SimpleNamespace(
            devices=[SimpleNamespace(public_key="fixture-device", access="full")]
        )
        calls = []
        from corral.remote.command_receipts import digest_for_text
        kwargs = dict(
            method="input.text", target_key="codex:a",
            digest=digest_for_text(key="codex:a", text="hello", submit=True),
            side_effect=lambda: calls.append("injected"),
            prepare=lambda deadline: "baseline",
        )
        first = service._run_receipted_input(connection, {"command_id": "dead"}, **kwargs)
        self.assertEqual(first["status"], "accepted")
        deadline = time.monotonic() + 2
        while service.receipts.status("fixture-device", "dead")["status"] in (
            "accepted", "dispatching",
        ):
            self.assertLess(time.monotonic(), deadline)
            threading.Event().wait(.01)
        final = service.receipts.status("fixture-device", "dead")
        self.assertEqual(final["status"], "unknown")
        # Unknown never replays: same identity returns the receipt, no new inject.
        service._run_receipted_input(connection, {"command_id": "dead"}, **kwargs)
        self.assertEqual(calls, ["injected"])
        deadline = time.monotonic() + 2
        while service._input_targets:
            self.assertLess(time.monotonic(), deadline)
            threading.Event().wait(.01)
        self.assertFalse(service._input_targets)

    def test_waiting_input_expires_without_injection_when_target_stays_busy(self):
        hub = mock.Mock(spec=SessionHub)
        hub.resolve_session_key.side_effect = lambda key: key
        hub.delivery_binding.return_value = "pane"
        service = RemoteService(hub)
        connection = Connection("fixture-device", lambda _: None)
        service._input_targets.add("pane")
        from corral.remote.command_receipts import digest_for_text
        receipt = service._run_receipted_input(
            connection, {"command_id": "late", "lease_sec": 1}, method="input.text", target_key="codex:a",
            digest=digest_for_text(key="codex:a", text="hello", submit=True),
            side_effect=lambda: self.fail("must not inject"), prepare=lambda _: self.fail("must not prepare"),
            confirm=lambda *_: None,
        )
        self.assertEqual(receipt["status"], "accepted")
        deadline = time.monotonic() + 3
        while service.receipts.status("fixture-device", "late")["status"] == "accepted":
            self.assertLess(time.monotonic(), deadline)
            threading.Event().wait(.02)
        final = service.receipts.status("fixture-device", "late")
        self.assertEqual(final["status"], "rejected")
        self.assertNotIn("previous input", final["detail"].lower())
        self.assertTrue(service._input_slots.acquire(False))
        service._input_slots.release()

    def test_worker_start_failure_rejects_and_releases_admission(self):
        hub = mock.Mock(spec=SessionHub)
        hub.resolve_session_key.side_effect = lambda key: key
        hub.delivery_binding.return_value = "pane"
        service = RemoteService(hub)
        connection = Connection("fixture-device", lambda _: None)
        from corral.remote.command_receipts import digest_for_text
        with mock.patch("corral.remote.service.threading.Thread.start", side_effect=RuntimeError("no threads")):
            receipt = service._run_receipted_input(
                connection, {"command_id": "no-worker"}, method="input.text", target_key="codex:a",
                digest=digest_for_text(key="codex:a", text="hello", submit=True),
                side_effect=lambda: self.fail("must not inject"), prepare=lambda _: None,
                confirm=lambda *_: None,
            )
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(receipt["reason"], "worker_unavailable")
        self.assertFalse(service._input_targets)
        self.assertTrue(service._input_slots.acquire(False))
        service._input_slots.release()

    def test_normalized_confirmation_requires_new_user_record(self):
        hub = SessionHub()
        self.addCleanup(hub.stop)
        transcript = SimpleNamespace(path="history", generation=7, messages=[RichMessage(1, "user", "hello")])
        with (
            mock.patch.object(hub.store, "find_session", return_value={"path": "history"}),
            mock.patch.object(hub, "resolve_session_key", side_effect=lambda key: key),
            mock.patch.object(hub, "_ensure_transcript", return_value=transcript),
        ):
            with self.assertRaises(PartialInjectionError):
                hub.confirm_text_delivery("codex:a", "hello", ("history", 7, 1, ""), time.monotonic())
            transcript.messages.append(RichMessage(2, "user", "hello"))
            hub.confirm_text_delivery("codex:a", "hello", ("history", 7, 1, ""), time.monotonic())
            transcript.generation = 8
            with self.assertRaises(PartialInjectionError):
                hub.confirm_text_delivery("codex:a", "hello", ("history", 7, 1, ""), time.monotonic())

    def test_resumed_formal_history_waits_before_capturing_delivery_baseline(self):
        hub = SessionHub()
        self.addCleanup(hub.stop)
        order = []
        session = {"source": "pi", "id": "old", "path": "history", "keepalive_name": "resumed-pane"}
        transcript = SimpleNamespace(path="history", generation=7, messages=[RichMessage(1, "user", "old")])

        def resume(key, *, resume_if_needed, resumed):
            self.assertTrue(resume_if_needed)
            resumed.append(True)
            order.append("resumed")
            return "resumed-pane"

        def baseline(_):
            self.assertEqual(order, ["resumed", "ready"])
            return transcript

        with (
            mock.patch.object(hub, "_keepalive_name", side_effect=resume),
            mock.patch.object(hub, "_pane_proven_dead", return_value=False),
            mock.patch.object(hub, "require_session", return_value=session),
            mock.patch.object(hub, "_wait_pane_settled", side_effect=lambda *a, **kw: order.append("ready")),
            mock.patch.object(hub, "_ensure_transcript", side_effect=baseline),
        ):
            result = hub.prepare_text_delivery("pi:old", time.monotonic() + 30)
        self.assertEqual(result, ("history", 7, 1, "resumed-pane"))

    def test_startup_exit_is_rejected_before_input(self):
        hub = SessionHub()
        self.addCleanup(hub.stop)
        with (
            mock.patch.object(hub, "_keepalive_name", return_value="fixture-pane"),
            mock.patch.object(hub, "require_session", return_value={"provisional": True, "source": "codex"}),
            mock.patch("corral.embed.pane_liveness", return_value="dead"),
            mock.patch("corral.embed.take_exit_report", return_value=None),
        ):
            with self.assertRaises(ActionError):
                hub.prepare_text_delivery("codex:a", time.monotonic() + 2)

    def test_death_drops_empty_card_but_uncertainty_preserves_it(self):
        store = SessionStore(limit=20)
        card = store.register_hosted_session(
            runtime_id="codex", keepalive_name="fixture-pane", title="empty", cwd=None, ident="a"
        )
        with store.lock, mock.patch("corral.embed.pane_liveness", return_value="unknown"):
            store._reconcile_provisional_sessions({"codex": []})
        self.assertIs(store.find_session("codex:a"), card)
        with store.lock, mock.patch("corral.embed.pane_liveness", return_value="dead"):
            store._reconcile_provisional_sessions({"codex": []})
        self.assertIsNone(store.find_session("codex:a"))
        self.assertNotIn("codex:a", store.hosted)

    def test_restart_pane_of_existing_session_is_not_adopted_as_provisional(self):
        """Codex restart reuses the id prefix; the claim lags, so no ghost card (2026-10-20)."""
        store = SessionStore(limit=20)
        formal = {"source": "codex", "id": "019a7c2e-1111-2222-3333-444455556666",
                  "title": "Existing", "cwd": "/tmp/proj"}
        restarted = {"name": "corral-codex-019a7c2e", "runtime_id": "codex", "ident": "019a7c2e",
                     "cwd": "/tmp/proj", "created_at": time.time()}
        fresh = {"name": "corral-codex-5f3d9a1b", "runtime_id": "codex", "ident": "5f3d9a1b",
                 "cwd": "/tmp/proj", "created_at": time.time()}
        with mock.patch.object(liveness, "list_managed_hosts", return_value=[restarted, fresh]):
            store._adopt_foreign_hosted([formal])
        self.assertIsNone(store.find_session("codex:019a7c2e"))
        self.assertIsNotNone(store.find_session("codex:5f3d9a1b"))
        self.assertNotIn("keepalive_name", formal)

    def test_adoption_and_probe_preserve_real_creation_timestamp(self):
        store = SessionStore(limit=20)
        host = {"name": "corral-codex-a", "runtime_id": "codex", "ident": "a", "cwd": None,
                "created_at": time.time() - 86400}
        with mock.patch.object(liveness, "list_managed_hosts", return_value=[host]):
            store._adopt_foreign_hosted([])
            card = store.find_session("codex:a")
            self.assertEqual(card["mtime"], host["created_at"])
            card["mtime"] = time.time()  # repair an old cached discovery timestamp
            store._probe_hosts([card])
            self.assertEqual(card["mtime"], host["created_at"])
            store._adopt_foreign_hosted([])
            self.assertEqual(len(store.sessions["codex"]), 1)
