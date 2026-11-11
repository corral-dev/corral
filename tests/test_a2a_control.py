"""A2A storm admission, target queues and the persistent kill-switch."""

from __future__ import annotations

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

    @staticmethod
    def _run_spec(sender, target):
        return {"mode": "submit", "runtime": "claude", "session_ref": target,
                "from_agent": sender, "a2a": True}


if __name__ == "__main__":
    unittest.main()
