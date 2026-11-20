"""A2A storm admission, target queues and the persistent kill-switch."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from corral import agent_api, control_cli, control_store, control_worker
from corral.control_store import ControlError, RunStore


class A2AControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "control"
        patcher = mock.patch.object(control_store, "control_home", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = RunStore()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, key: str, *, sender="pi:storm", target="claude:target", a2a=True, report_back=False,
             delivered=False, hop=None):
        spec = {
            "mode": "submit", "runtime": "claude", "session_ref": target,
            "prompt": key, "on_busy": "queue", "queue_timeout": 120,
        }
        initial = {
            "mode": "submit", "runtime": "claude", "session_ref": target,
            "delivery": "accepted", "execution": "queued", "created_at": time.time(),
        }
        if sender:
            spec["from_agent"] = initial["from_agent"] = sender
        if a2a:
            spec["a2a"] = initial["a2a"] = True
        if report_back:
            spec["report_back"] = True
        record, _ = self.store.create("test", key, spec, initial)
        if hop is not None:
            self.store.update(record["run_id"], {"hop": hop, "chain_id": key})
        if delivered:
            self.store.update(record["run_id"], {
                "delivery": "native_recorded", "execution": "completed",
                "result": {"started_at": time.time() + 5, "usage": {
                    "input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 100, "cache_write_tokens": 4,
                }},
            })
        return self.store.get(record["run_id"], private=True)

    def test_reply_consumes_one_grant_and_enforces_the_hop_budget(self):
        parent = self._run("parent", delivered=True)
        found = control_cli._find_reply_parent(self.store, "claude:target", "pi:storm")
        self.assertEqual(found["run_id"], parent["run_id"])
        spec = {"mode": "submit", "session_ref": "pi:storm", "from_agent": "claude:target",
                "parent_run_id": parent["run_id"], "a2a": True}
        initial = {"mode": "submit", "session_ref": "pi:storm"}
        control_cli._apply_lineage(self.store, "child", spec, initial)
        self.assertEqual(spec["chain_id"], parent["run_id"])
        self.assertEqual(spec["hop"], 2)
        self.assertTrue(self.store.get(parent["run_id"])["reply_consumed"])
        with self.assertRaises(ControlError) as caught:
            control_cli._apply_lineage(self.store, "child-2", dict(spec), dict(initial))
        self.assertEqual(caught.exception.code, "reply_already_consumed")

        exhausted = self._run("exhausted", delivered=True, hop=control_cli.MAX_A2A_HOPS)
        with self.assertRaises(ControlError) as caught:
            control_cli._apply_lineage(self.store, "child-3", {
                "mode": "submit", "session_ref": "pi:storm", "from_agent": "claude:target",
                "parent_run_id": exhausted["run_id"], "a2a": True,
            }, {})
        self.assertEqual(caught.exception.code, "chain_budget_exhausted")

    def test_chain_budget_suppresses_report_back_instead_of_echoing(self):
        run = self._run("report-budget", report_back=True)
        self.store.update(run["run_id"], {
            "execution": "completed", "delivery": "native_recorded",
            "result": {"final_response": "done"},
        })
        error = ControlError("chain_budget_exhausted", "budget", 5)
        with mock.patch.object(control_cli, "cmd_send", side_effect=error):
            outcome = control_worker.report_back(self.store, run["run_id"], registry=None)
        self.assertEqual(outcome, {"state": "suppressed", "reason": "chain_budget_exhausted"})

    def test_chatter_alert_counts_pair_messages_and_cache_tokens(self):
        for index in range(5):
            self._run(f"hot-{index}", delivered=True)
        alerts = control_cli.a2a_chatter_alerts(self.store)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["messages"], 5)
        self.assertEqual(alerts[0]["cache_read_tokens"], 500)

    def test_ledger_reuses_run_usage_and_queue_evidence(self):
        self._run("usage", delivered=True)
        data = control_cli.cmd_a2a_ledger(SimpleNamespace(hours=24), None)["data"]
        self.assertEqual(data["returned"], 1)
        row = data["pairs"][0]
        self.assertEqual(row["messages"], 1)
        self.assertEqual(row["turns"], 1)
        self.assertAlmostEqual(row["queue_seconds"], 5.0, delta=0.1)
        self.assertEqual(row["input_tokens"], 10)
        self.assertEqual(row["output_tokens"], 2)
        self.assertEqual(row["cache_read_tokens"], 100)
        self.assertEqual(row["cache_write_tokens"], 4)

    def test_send_defaults_reject_and_explicit_queue_is_bounded(self):
        args = agent_api.build_parser().parse_args(["send", "claude:target", "hello"])
        self.assertEqual(args.on_busy, "reject")
        self.assertEqual(args.queue_timeout, 120)
        submit_args = agent_api.build_parser().parse_args([
            "run", "submit", "claude:target", "--prompt", "owner task", "--idempotency-key", "owner-1",
        ])
        self.assertEqual(submit_args.on_busy, "queue")
        self.assertEqual(submit_args.queue_timeout, 3600)

    def test_queue_expiry_stays_on_the_run_without_waking_the_sender(self):
        run = self._run("expired", report_back=True)
        self.store.update(run["run_id"], {
            "execution": "failed", "delivery": "rejected",
            "error": {"code": "queue_expired", "message": "stale after 120s"},
        })
        with mock.patch.object(control_cli, "cmd_send") as send:
            outcome = control_worker.report_back(self.store, run["run_id"], registry=None)
        self.assertEqual(outcome, {"state": "suppressed", "reason": "queue_expired"})
        send.assert_not_called()

    def test_create_attribution_is_not_peer_mail(self):
        self.assertFalse(control_cli._is_a2a({"mode": "create", "from_agent": "pi:creator"}))
        self.assertTrue(control_cli._is_a2a({"mode": "submit", "from_agent": "pi:sender"}))
        with mock.patch.object(control_cli, "_supervisor_alive", return_value=True):
            # An attributed ordinary create still follows the machine limit,
            # never the A2A sender/target quotas.
            control_cli._guard_capacity(self.store, {"mode": "create", "from_agent": "pi:creator"})

    def test_sender_quota_preserves_an_unrelated_actor_and_names_occupancy(self):
        for index in range(control_cli.MAX_A2A_ACTOR_RUNS):
            self._run(f"storm-{index}", target=f"claude:t-{index}")
        with mock.patch.object(control_cli, "_supervisor_alive", return_value=True):
            with self.assertRaises(ControlError) as caught:
                control_cli._guard_capacity(self.store, self._run_spec("pi:storm", "claude:new"))
            self.assertEqual(caught.exception.code, "capacity_exceeded")
            self.assertEqual(caught.exception.details["quota"], "sender")
            self.assertIn("pi:storm", caught.exception.details["occupancy"][0]["senders"])
            # The same global state must not refuse another sender's work.
            control_cli._guard_capacity(self.store, self._run_spec("opencode:other", "claude:other"))

    def test_target_quota_counts_queued_runs_but_machine_counts_one_owner(self):
        for index in range(control_cli.MAX_A2A_TARGET_RUNS):
            self._run(f"same-{index}", sender=f"pi:s{index}")
        with mock.patch.object(control_cli, "_supervisor_alive", return_value=True):
            with self.assertRaises(ControlError) as caught:
                control_cli._guard_capacity(self.store, self._run_spec("pi:new", "claude:target"))
            self.assertEqual(caught.exception.details["quota"], "target")
            owners = {control_cli._owner_key(record) for record in self.store.unsettled()}
            self.assertEqual(owners, {"target:claude:target"})

    def test_target_queue_drains_in_order_with_one_lock_owner(self):
        first = self._run("first")
        second = self._run("second")
        calls = []

        def settle(run_id):
            calls.append(run_id)
            self.store.update(run_id, {"execution": "completed", "delivery": "native_recorded"})
            return 0

        with mock.patch.object(control_worker, "_run_and_report", side_effect=settle):
            self.assertEqual(control_worker.main(first["run_id"], target_queue=True), 0)
        self.assertEqual(calls, [first["run_id"], second["run_id"]])

        third = self._run("third")
        with control_worker.file_lock(self.store, "target-supervisor:claude:target"):
            with mock.patch.object(control_worker, "_run_and_report") as run:
                self.assertEqual(control_worker.main(third["run_id"], target_queue=True), 0)
                run.assert_not_called()
            self.assertTrue(control_cli._supervisor_alive(self.store, third["run_id"]))

    def test_pause_refuses_new_peer_work_defers_queue_and_audits_resume(self):
        paused = control_cli.cmd_a2a_pause(SimpleNamespace(), None)["data"]
        self.assertTrue(paused["paused"])
        self.assertTrue(self.store.a2a_paused())
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_submit(SimpleNamespace(
                target="claude:new", prompt="peer", prompt_file=None, idempotency_key="paused-send",
                if_version=None, on_busy="queue", queue_timeout=120, allow_concurrent=False,
                task_ref=None, dry_run=False, from_agent="pi:storm", no_reply_footer=False,
                report_back=True,
            ), None)
        self.assertEqual(caught.exception.code, "a2a_paused")

        run = self._run("queued")
        with mock.patch.object(control_worker, "_run_and_report") as run_work:
            self.assertEqual(control_worker.main(run["run_id"], target_queue=True), 0)
            run_work.assert_not_called()
        with mock.patch.object(control_cli, "_spawn_supervisor", return_value=True) as spawn:
            resumed = control_cli.cmd_a2a_resume(SimpleNamespace(), None)["data"]
        self.assertFalse(resumed["paused"])
        self.assertIn(run["run_id"], resumed["recovered"])
        spawn.assert_called_once()
        actions = [row["action"] for row in self.store.audit_trail("a2a")]
        self.assertEqual(actions, ["a2a.resume", "a2a.pause"])

    def test_pause_defers_report_back_without_persisting_failure(self):
        run = self._run("settled", report_back=True)
        self.store.update(run["run_id"], {"execution": "completed", "delivery": "native_recorded",
                                          "result": {"final_response": "done"}})
        self.store.set_a2a_paused(True, actor="test")
        with mock.patch.object(control_cli, "cmd_send") as send:
            outcome = control_worker.report_back(self.store, run["run_id"], registry=None)
        self.assertEqual(outcome, {"state": "deferred", "reason": "a2a_paused"})
        send.assert_not_called()
        self.assertNotIn("report_back", self.store.get(run["run_id"]))

    def test_agent_caller_cannot_bypass_steer_timeout_or_quota_with_from_agent_none(self):
        session = {"source": "claude", "id": "target", "cwd": self.tmp.name,
                   "path": str(Path(self.tmp.name) / "h.jsonl")}
        version = control_cli.target_payload(session)["version"]
        clean_env = dict(os.environ)
        for name in ("CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID", "PI_SESSION_ID",
                     "ANTIGRAVITY_CONVERSATION_ID", "CORRAL_RUNTIME", "CORRAL_SESSION_ID", "CORRAL_ACTOR"):
            clean_env.pop(name, None)
        clean_env["CODEX_THREAD_ID"] = "bypass-1"
        with mock.patch.dict(os.environ, clean_env, clear=True), mock.patch.object(
            control_cli, "resolve_target", return_value=session
        ):
            with self.assertRaises(ControlError) as steer_err:
                control_cli.cmd_submit(SimpleNamespace(
                    target="claude:target", prompt="steered", prompt_file=None, idempotency_key="b-steer",
                    if_version=version, on_busy="steer", queue_timeout=120, allow_concurrent=False,
                    task_ref=None, dry_run=False, from_agent="none", no_reply_footer=False, report_back=False,
                ), None)
            self.assertEqual(steer_err.exception.code, "steer_not_allowed")
            with self.assertRaises(ControlError) as zero_err:
                control_cli.cmd_submit(SimpleNamespace(
                    target="claude:target", prompt="forever", prompt_file=None, idempotency_key="b-zero",
                    if_version=version, on_busy="queue", queue_timeout=0, allow_concurrent=False,
                    task_ref=None, dry_run=False, from_agent="none", no_reply_footer=False, report_back=False,
                ), None)
            self.assertEqual(zero_err.exception.code, "usage_error")
            with self.assertRaises(ControlError) as range_err:
                control_cli.cmd_send(SimpleNamespace(
                    target="claude:target", message="too long", prompt_file=None, from_agent=None,
                    no_reply_footer=False, ask=False, no_report_back=True, on_busy="queue",
                    queue_timeout=121, idempotency_key="b-range", compact=False,
                ), None)
            self.assertEqual(range_err.exception.code, "usage_error")

        with (
            mock.patch.dict(os.environ, clean_env, clear=True),
            mock.patch.object(control_cli, "resolve_target", return_value=session),
            mock.patch.object(control_cli, "_spawn_supervisor", return_value=True),
            mock.patch.object(control_cli, "_supervisor_alive", return_value=True),
        ):
            data = control_cli.cmd_submit(SimpleNamespace(
                target="claude:target", prompt="bounded", prompt_file=None, idempotency_key="b-cap",
                if_version=version, on_busy="queue", queue_timeout=3600, allow_concurrent=True,
                task_ref=None, dry_run=False, from_agent="none", no_reply_footer=False, report_back=False,
            ), None)["data"]
            saved = self.store.get(data["run"]["run_id"], private=True)
            self.assertEqual(saved["spec"]["queue_timeout"], 120)
            self.assertEqual(saved["actor"], "codex:bypass-1")

            for idx in range(control_cli.MAX_A2A_ACTOR_RUNS - 1):
                other = {"source": "claude", "id": f"t-{idx}", "cwd": self.tmp.name,
                         "path": str(Path(self.tmp.name) / f"h-{idx}.jsonl")}
                with mock.patch.object(control_cli, "resolve_target", return_value=other):
                    control_cli.cmd_submit(SimpleNamespace(
                        target=f"claude:t-{idx}", prompt=f"msg-{idx}", prompt_file=None,
                        idempotency_key=f"b-cap-{idx}", if_version=control_cli.target_payload(other)["version"],
                        on_busy="queue", queue_timeout=120, allow_concurrent=True, task_ref=None,
                        dry_run=False, from_agent="none", no_reply_footer=False, report_back=False,
                    ), None)
            with self.assertRaises(ControlError) as quota_err:
                control_cli.cmd_submit(SimpleNamespace(
                    target="claude:target", prompt="over-quota", prompt_file=None, idempotency_key="b-over",
                    if_version=version, on_busy="queue", queue_timeout=120, allow_concurrent=True,
                    task_ref=None, dry_run=False, from_agent="none", no_reply_footer=False, report_back=False,
                ), None)
            self.assertEqual(quota_err.exception.code, "capacity_exceeded")
            self.assertEqual(quota_err.exception.details["quota"], "sender")

    def test_send_with_on_busy_reject_fails_synchronously_when_target_run_active(self):
        self._run("active", target="claude:target")
        session = {"source": "claude", "id": "target", "cwd": self.tmp.name,
                   "path": str(Path(self.tmp.name) / "h.jsonl")}
        with (
            mock.patch.object(control_cli, "resolve_target", return_value=session),
            mock.patch.object(control_cli, "_supervisor_alive", return_value=True),
        ):
            with self.assertRaises(ControlError) as caught:
                control_cli.cmd_send(SimpleNamespace(
                    target="claude:target", message="ping", prompt_file=None, from_agent="pi:other",
                    no_reply_footer=False, ask=True, no_report_back=False, on_busy="reject",
                    queue_timeout=120, idempotency_key="sync-reject", compact=False,
                ), None)
        self.assertEqual(caught.exception.code, "target_busy")

    def test_target_queue_expires_before_dispatch_after_blocker_clears(self):
        run = self._run("stale-queued", report_back=True)
        with self.store.connection(write=True) as conn:
            conn.execute(
                "UPDATE runs SET created_at = created_at - 300, "
                "data = json_set(data, '$.created_at', json_extract(data, '$.created_at') - 300) "
                "WHERE id = ?",
                (run["run_id"],),
            )
        with (
            mock.patch("corral.remote.sessions.SessionHub"),
            mock.patch.object(control_worker, "corral_hooks", return_value=(mock.MagicMock(), {})),
            mock.patch.object(control_worker, "run_supervisor") as shared_run,
            mock.patch.object(control_cli, "cmd_send") as send,
        ):
            rc = control_worker._run_and_report(run["run_id"])
        self.assertEqual(rc, 1)
        shared_run.assert_not_called()
        send.assert_not_called()
        settled = self.store.get(run["run_id"])
        self.assertEqual(settled["delivery"], "rejected")
        self.assertEqual(settled["execution"], "failed")
        self.assertEqual(settled["error"]["code"], "queue_expired")
        self.assertEqual(settled["report_back"], {"state": "suppressed", "reason": "queue_expired"})

    def test_on_busy_reject_fails_fast_behind_earlier_run(self):
        self._run("blocker", target="claude:target")
        spec = {"mode": "submit", "runtime": "claude", "session_ref": "claude:target", "cwd": self.tmp.name,
                "history_path": str(Path(self.tmp.name) / "h.jsonl"), "prompt": "hi", "on_busy": "reject",
                "queue_timeout": 120, "from_agent": "pi:other", "a2a": True, "caller": {"actor": "pi:other"}}
        initial = {"mode": "submit", "runtime": "claude", "session_ref": "claude:target", "cwd": self.tmp.name,
                   "delivery": "accepted", "execution": "queued", "from_agent": "pi:other", "a2a": True}
        run, _ = self.store.create("pi:other", "reject-queued", spec, initial)
        with (
            mock.patch("corral.remote.sessions.SessionHub"),
            mock.patch.object(control_worker, "corral_hooks", return_value=(mock.MagicMock(), {})),
            mock.patch.object(control_worker, "run_supervisor") as shared_run,
            mock.patch.object(control_worker.time, "sleep") as sleep,
        ):
            rc = control_worker._supervise(run["run_id"])
        self.assertEqual(rc, 1)
        shared_run.assert_not_called()
        sleep.assert_not_called()
        settled = self.store.get(run["run_id"])
        self.assertEqual(settled["delivery"], "rejected")
        self.assertEqual(settled["execution"], "failed")
        self.assertEqual(settled["error"]["code"], "target_busy")

    def test_peer_a2a_queue_timeout_clamping_warns_and_exposes_effective_timeout(self):
        session = {
            "source": "claude",
            "id": "target",
            "cwd": self.tmp.name,
            "path": str(Path(self.tmp.name) / "h.jsonl"),
        }
        version = control_cli.target_payload(session)["version"]
        with (
            mock.patch.object(control_cli, "resolve_target", return_value=session),
            mock.patch.object(control_cli, "_spawn_supervisor", return_value=True),
            mock.patch.object(control_cli, "_supervisor_alive", return_value=True),
        ):
            dry = control_cli.cmd_submit(
                SimpleNamespace(
                    target="claude:target",
                    prompt="brief",
                    prompt_file=None,
                    idempotency_key="clamp-dry",
                    if_version=None,
                    on_busy="queue",
                    queue_timeout=3600,
                    allow_concurrent=False,
                    task_ref=None,
                    dry_run=True,
                    from_agent="pi:ae93a9bf",
                    no_reply_footer=False,
                    report_back=False,
                ),
                None,
            )["data"]
            self.assertEqual(dry["run"]["queue_timeout"], 120)
            self.assertEqual(dry["warnings"][0]["code"], "queue_timeout_clamped")
            self.assertEqual(dry["warnings"][0]["requested"], 3600)
            self.assertEqual(dry["warnings"][0]["effective"], 120)

            live = control_cli.cmd_submit(
                SimpleNamespace(
                    target="claude:target",
                    prompt="brief",
                    prompt_file=None,
                    idempotency_key="clamp-live",
                    if_version=version,
                    on_busy="queue",
                    queue_timeout=3600,
                    allow_concurrent=False,
                    task_ref=None,
                    dry_run=False,
                    from_agent="pi:ae93a9bf",
                    no_reply_footer=False,
                    report_back=False,
                ),
                None,
            )["data"]
            self.assertEqual(live["warnings"][0]["code"], "queue_timeout_clamped")
            self.assertEqual(live["run"]["queue_timeout"], 120)
            self.assertGreater(live["run"]["queue_remaining_s"], 0)
            self.assertIn("--until delivered", live["delivery_note"])
            self.assertIn("NOT yet sent", live["run"]["delivery_meaning"])

    def test_run_status_projects_blocked_by_and_mixed_warning(self):
        blocker = self._run("blocker")
        self.store.update(
            blocker["run_id"],
            {"delivery": "native_recorded", "execution": "running", "attribution": "mixed"},
        )
        with self.store.connection(write=True) as conn:
            conn.execute(
                "UPDATE runs SET data = json_set(data, '$.updated_at', json_extract(data, '$.updated_at') - 400) "
                "WHERE id = ?",
                (blocker["run_id"],),
            )
        queued = self._run("queued-second")
        with mock.patch.object(control_cli, "_supervisor_alive", return_value=False):
            data = control_cli.cmd_status(
                SimpleNamespace(run_ids=[blocker["run_id"], queued["run_id"]], compact=False),
                None,
            )["data"]
        by_id = {r["run_id"]: r for r in data["runs"]}
        b_view = by_id[blocker["run_id"]]
        q_view = by_id[queued["run_id"]]
        self.assertIn("mixed_attribution_active", {w["code"] for w in b_view.get("warnings", [])})
        self.assertEqual(q_view["queue_reason"], "earlier_run_on_session")
        self.assertEqual(q_view["queued_behind"], blocker["run_id"])
        self.assertEqual(q_view["blocked_by"]["run_id"], blocker["run_id"])
        self.assertEqual(q_view["blocked_by"]["attribution"], "mixed")
        self.assertGreaterEqual(q_view["blocked_by"]["idle_s"], 395.0)
        self.assertFalse(q_view["blocked_by"]["supervisor_alive"])
        q_codes = {w["code"] for w in q_view.get("warnings", [])}
        self.assertTrue(
            {"blocked_by_mixed_attribution", "blocked_by_dead_supervisor", "blocked_by_stalled_run"} <= q_codes
        )

    def test_queued_run_interrupt_cancels_immediately_without_supervisor(self):
        queued = self._run("cancel-now")
        with mock.patch.object(control_cli, "_supervisor_alive", return_value=False):
            out = control_cli.cmd_interrupt(
                SimpleNamespace(
                    run_id=queued["run_id"],
                    if_version=queued["version"],
                    idempotency_key="int-now",
                    compact=False,
                ),
                None,
            )["data"]
        self.assertEqual(out["status"], "created")
        self.assertEqual(out["message"]["state"], "applied")
        self.assertEqual(out["run"]["delivery"], "rejected")
        self.assertEqual(out["run"]["execution"], "interrupted")
        self.assertEqual(out["run"]["error"]["code"], "cancelled")

    def test_run_withdraw_cancels_queued_control_message(self):
        run = self._run("withdrawable")
        self.store.update(run["run_id"], {"delivery": "native_recorded", "execution": "running"})
        ver = self.store.get(run["run_id"])["version"]
        with mock.patch.object(control_cli, "_supervisor_alive", return_value=True):
            sent = control_cli.cmd_message(
                SimpleNamespace(
                    run_id=run["run_id"],
                    prompt="extra direction",
                    prompt_file=None,
                    if_version=ver,
                    idempotency_key="msg-w1",
                    from_agent=None,
                    no_reply_footer=True,
                ),
                None,
            )["data"]
            msg_id = sent["message"]["message_id"]
            withdrawn = control_cli.cmd_withdraw(
                SimpleNamespace(
                    run_id=run["run_id"],
                    message_id=msg_id,
                    if_version=sent["run"]["version"],
                    compact=False,
                ),
                None,
            )["data"]
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertEqual(withdrawn["message"]["state"], "withdrawn")
        done = self.store.update(run["run_id"], {"execution": "completed"}, settled_messages={"initial"})
        self.assertEqual(done["execution"], "completed")

        run2 = self._run("submitted-msg")
        self.store.update(run2["run_id"], {"delivery": "native_recorded", "execution": "running"})
        msg2, _ = self.store.enqueue(
            run2["run_id"],
            "pi:storm",
            "msg-w2",
            {"action": "steer", "prompt": "hi"},
            self.store.get(run2["run_id"])["version"],
        )
        self.store.message_state(msg2["message_id"], "input_submitted")
        with self.assertRaises(ControlError) as caught:
            control_cli.cmd_withdraw(
                SimpleNamespace(
                    run_id=run2["run_id"],
                    message_id=msg2["message_id"],
                    if_version=self.store.get(run2["run_id"])["version"],
                    compact=False,
                ),
                None,
            )
        self.assertEqual(caught.exception.code, "message_not_queued")

    def test_run_wait_until_delivered_returns_on_native_recorded_and_rejected(self):
        run = self._run("wait-delivered")
        with mock.patch.object(control_cli, "_supervisor_alive", return_value=True):
            self.assertIsNone(control_cli._actionable(self.store, self.store.get(run["run_id"]), "delivered"))
            self.store.update(run["run_id"], {"delivery": "input_submitted", "execution": "running"})
            self.assertIsNone(control_cli._actionable(self.store, self.store.get(run["run_id"]), "delivered"))
            self.store.update(run["run_id"], {"delivery": "native_recorded", "execution": "running"})
            out = control_cli.cmd_wait(
                SimpleNamespace(run_ids=[run["run_id"]], timeout=1, until="delivered"),
                None,
            )["data"]
            self.assertEqual(out["ready"][run["run_id"]], "delivered")

        run2 = self._run("wait-rejected")
        self.store.update(
            run2["run_id"],
            {"delivery": "rejected", "execution": "failed", "error": {"code": "target_busy", "message": "busy"}},
        )
        out2 = control_cli.cmd_wait(
            SimpleNamespace(run_ids=[run2["run_id"]], timeout=1, until="delivered"),
            None,
        )["data"]
        self.assertEqual(out2["ready"][run2["run_id"]], "failed")

    @staticmethod
    def _run_spec(sender, target):
        return {"mode": "submit", "runtime": "claude", "session_ref": target,
                "from_agent": sender, "a2a": True}


if __name__ == "__main__":
    unittest.main()
