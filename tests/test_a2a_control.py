"""A2A storm admission, target queues and the persistent kill-switch."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from corral import control_cli, control_store, control_worker
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

    def _run(self, key: str, *, sender="pi:storm", target="claude:target", a2a=True, report_back=False):
        spec = {
            "mode": "submit", "runtime": "claude", "session_ref": target,
            "prompt": key, "on_busy": "queue", "queue_timeout": 120,
        }
        initial = {
            "mode": "submit", "runtime": "claude", "session_ref": target,
            "delivery": "accepted", "execution": "queued", "created_at": 100.0,
        }
        if sender:
            spec["from_agent"] = initial["from_agent"] = sender
        if a2a:
            spec["a2a"] = initial["a2a"] = True
        if report_back:
            spec["report_back"] = True
        record, _ = self.store.create("test", key, spec, initial)
        return self.store.get(record["run_id"], private=True)

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
