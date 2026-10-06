#!/usr/bin/env python3
"""Real CLI lifecycle tests using isolated Git checkouts and subprocesses."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ENTRY = Path(__file__).with_name("agent_workflow.py")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="corral-workflow-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.processes = []
        self.addCleanup(self.clean_processes)
        for args in [
            ("init", "-q"),
            ("config", "user.name", "Fixture"),
            ("config", "user.email", "fixture@example.invalid"),
        ]:
            self.git(*args)
        (self.repo / "source.txt").write_text("original\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def clean_processes(self):
        for pid in self.processes:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout

    def cli(self, *args, code=0):
        result = subprocess.run(
            [sys.executable, str(ENTRY), "--state-dir", str(self.state), *map(str, args)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(set(value), {"ok", "data", "error", "meta"})
        self.assertEqual(result.stderr, "")
        return value

    def run_command(self, text, *options, code=0):
        return self.cli("run", "--repo", self.repo, *options, "--", sys.executable, "-c", text, code=code)

    def status(self, run_id, code=0):
        return self.cli("status", "--repo", self.repo, "--run", run_id, code=code)

    def wait_status(self, run_id, terminal=True):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ENTRY),
                    "--state-dir",
                    str(self.state),
                    "status",
                    "--repo",
                    str(self.repo),
                    "--run",
                    run_id,
                ],
                capture_output=True,
                text=True,
            )
            value = json.loads(result.stdout)
            data = value.get("data") or value["error"]
            if terminal and data.get("status") == "succeeded":
                return data
            if not terminal and data.get("status") == "running":
                return data
            time.sleep(0.04)
        self.fail("No expected operation state: " + result.stdout)

    def make_plan(self, steps):
        path = self.root / "plan.json"
        path.write_text(json.dumps({"steps": steps}))
        return path

    def test_read_only_and_dry_run_create_nothing(self):
        before = self.git("status", "--porcelain")
        self.cli("doctor", "--repo", self.repo)
        self.cli("resources", "--resource", "fixture")
        self.run_command("raise SystemExit(77)", "--dry-run")
        self.cli(
            "checkpoint",
            "--repo",
            self.repo,
            "--task",
            "fixture",
            "--goal",
            "Reduce token overhead",
            "--next",
            "Read evidence",
            "--dry-run",
        )
        self.assertFalse(self.state.exists())
        self.assertEqual(before, self.git("status", "--porcelain"))

    def test_errors_are_structured_and_names_cannot_escape(self):
        self.cli("doctor", code=2)
        self.cli("resources", "--resource", "../../escape", code=2)
        self.cli("status", "--repo", self.repo, "--run", "missing", code=3)
        bad = self.make_plan([{"name": "one", "argv": ["echo", "--token=fixture-secret"]}])
        value = self.cli("run", "--repo", self.repo, "--plan", bad, code=2)
        self.assertNotIn("fixture-secret", json.dumps(value))
        self.cli("run", "--repo", self.repo, "--plan", bad, "--resource", "ignored", code=2)
        self.assertFalse(self.state.exists())

    def test_explicit_json_usage_error_is_structured_on_a_terminal(self):
        master, slave = os.openpty()
        try:
            result = subprocess.run(
                [sys.executable, str(ENTRY), "--json", "doctor"],
                stdout=slave,
                stderr=subprocess.PIPE,
                timeout=15,
            )
            value = json.loads(os.read(master, 8192).decode())
            self.assertEqual(result.returncode, 2)
            self.assertEqual(value["error"]["code"], "usage_error")
        finally:
            os.close(slave)
            os.close(master)

    def test_argv_never_uses_shell_and_output_stays_in_log(self):
        sentinel = self.root / "injected"
        text = f"$(touch {sentinel})"
        value = self.cli("run", "--repo", self.repo, "--", sys.executable, "-c", "import sys;print(sys.argv[1])", text)[
            "data"
        ]
        self.assertFalse(sentinel.exists())
        self.assertEqual(Path(value["results"][0]["log_path"]).read_text().strip(), text)

    def test_background_survives_submitter_and_receipt_is_terminal(self):
        started = self.run_command("import time;time.sleep(.25);print('DONE')", "--background")["data"]
        final = self.wait_status(started["run_id"])
        self.assertEqual(final["results"][0]["verification"], "not_requested")
        self.assertEqual(Path(final["results"][0]["log_path"]).read_text().strip(), "DONE")

    def test_resource_busy_reports_owner_and_releases_after_child_exits(self):
        started = self.run_command(
            "import time;print('LOCKED',flush=True);time.sleep(1.5)", "--background", "--resource", "fixture"
        )["data"]
        final = self.wait_status(started["run_id"], terminal=False)
        self.processes.append(final["worker_pid"])
        deadline = time.monotonic() + 3
        while not self.cli("resources", "--resource", "fixture")["data"]["busy"]:
            self.assertLess(time.monotonic(), deadline)
        blocked = self.run_command("print('SHOULD_NOT_RUN')", "--resource", "fixture", code=5)
        self.assertEqual(blocked["error"]["code"], "resource_busy")
        self.assertEqual(blocked["error"]["resource"]["owner"]["run_id"], started["run_id"])
        self.wait_status(started["run_id"])
        self.assertFalse(self.cli("resources", "--resource", "fixture")["data"]["busy"])
        self.run_command("print('NEXT')", "--resource", "fixture")

    def test_resume_skips_success_and_retries_only_declared_safe_failure(self):
        count = self.root / "count"
        ready = self.root / "ready"
        steps = [
            {
                "name": "first",
                "argv": [sys.executable, "-c", f"from pathlib import Path;p=Path({str(count)!r});p.write_text('once')"],
            },
            {
                "name": "second",
                "retry_safe": True,
                "argv": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path;raise SystemExit(0 if Path({str(ready)!r}).exists() else 1)",
                ],
            },
        ]
        result = self.cli("run", "--repo", self.repo, "--plan", self.make_plan(steps), code=1)
        run_id = result["error"]["run_id"]
        ready.touch()
        resumed = self.cli("run", "--repo", self.repo, "--resume", run_id)["data"]
        self.assertEqual(resumed["status"], "succeeded")
        self.assertEqual(count.read_text(), "once")
        first_time = resumed["results"][0]["finished"]
        repeated = self.cli("run", "--repo", self.repo, "--resume", run_id)["data"]
        self.assertEqual(repeated["results"][0]["finished"], first_time)

    def test_unsafe_failure_and_failed_verifier_never_replay_execution(self):
        result = self.run_command("raise SystemExit(1)", code=1)
        self.cli("run", "--repo", self.repo, "--resume", result["error"]["run_id"], code=5)
        counter = self.root / "counter"
        steps = [
            {
                "name": "write",
                "retry_safe": True,
                "argv": [sys.executable, "-c", f"from pathlib import Path;Path({str(counter)!r}).write_text('once')"],
                "verify_argv": [sys.executable, "-c", "raise SystemExit(1)"],
            }
        ]
        result = self.cli("run", "--repo", self.repo, "--plan", self.make_plan(steps), code=1)
        self.assertEqual(result["error"]["code"], "verification_failed")
        self.cli("run", "--repo", self.repo, "--resume", result["error"]["run_id"], code=5)
        self.assertEqual(counter.read_text(), "once")

    def test_successful_real_verification_and_source_drift(self):
        output = self.root / "artifact"
        steps = [
            {
                "name": "artifact",
                "argv": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path;Path({str(output)!r}).write_text('verified')",
                ],
                "verify_argv": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path;assert Path({str(output)!r}).read_text()=='verified'",
                ],
            }
        ]
        data = self.cli("run", "--repo", self.repo, "--plan", self.make_plan(steps))["data"]
        self.assertEqual(data["results"][0]["verification"], "passed")
        (self.repo / "source.txt").write_text("changed\n")
        result = self.cli("run", "--repo", self.repo, "--resume", data["run_id"], code=5)
        self.assertEqual(result["error"]["code"], "source_drift")
        self.assertEqual((self.repo / "source.txt").read_text(), "changed\n")

    def test_delivery_requires_committed_whole_workspace(self):
        (self.repo / "other-task.txt").write_text("keep this\n")
        self.run_command("print('no')", "--require-clean", code=5)
        self.assertEqual((self.repo / "other-task.txt").read_text(), "keep this\n")

    def test_checkpoint_detects_changed_workspace(self):
        self.cli(
            "checkpoint",
            "--repo",
            self.repo,
            "--task",
            "fixture",
            "--goal",
            "Finish delivery",
            "--next",
            "Verify installed version",
            "--evidence",
            "run:example",
        )
        data = self.cli("checkpoint", "--repo", self.repo, "--task", "fixture")["data"]
        self.assertFalse(data["source_changed"])
        repeated = self.cli(
            "checkpoint",
            "--repo",
            self.repo,
            "--task",
            "fixture",
            "--goal",
            "Finish delivery",
            "--next",
            "Verify installed version",
            "--evidence",
            "run:example",
        )["data"]
        self.assertFalse(repeated["changed"])
        (self.repo / "source.txt").write_text("changed\n")
        data = self.cli("checkpoint", "--repo", self.repo, "--task", "fixture")["data"]
        self.assertTrue(data["source_changed"])
        self.assertEqual(data["next"], "Verify installed version")

    def test_timeout_retains_uncertain_receipt_and_releases_resource(self):
        value = self.run_command(
            "import time;time.sleep(20)", "--resource", "timeout-fixture", "--timeout", "0.1", code=1
        )
        self.assertEqual(value["error"]["status"], "unknown")
        self.assertTrue(Path(value["error"]["receipt_path"]).is_file())
        self.assertFalse(self.cli("resources", "--resource", "timeout-fixture")["data"]["busy"])
        self.cli("run", "--repo", self.repo, "--resume", value["error"]["run_id"], code=5)

    def test_parallel_resume_does_not_duplicate_a_step(self):
        counter = self.root / "count"
        ready = self.root / "ready"
        code = (
            "from pathlib import Path;import time;"
            f"p=Path({str(counter)!r});r=Path({str(ready)!r});"
            "assert r.exists();p.write_text(p.read_text()+'x' if p.exists() else 'x');time.sleep(.5)"
        )
        steps = [{"name": "retry", "retry_safe": True, "argv": [sys.executable, "-c", code]}]
        result = self.cli("run", "--repo", self.repo, "--plan", self.make_plan(steps), code=1)
        record = json.loads(Path(result["error"]["receipt_path"]).read_text())
        ready.touch()
        argv = [
            sys.executable,
            str(ENTRY),
            "--state-dir",
            str(self.state),
            "run",
            "--repo",
            str(self.repo),
            "--resume",
            record["run_id"],
            "--background",
        ]
        a = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        b = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        outputs = [a.communicate(timeout=10)[0], b.communicate(timeout=10)[0]]
        self.assertEqual(sorted([a.returncode, b.returncode]), [0, 5], outputs)
        self.wait_status(record["run_id"])
        self.assertEqual(counter.read_text(), "x")

    def test_saved_plan_mutation_is_rejected(self):
        result = self.run_command("print('once')")["data"]
        path = Path(result["receipt_path"])
        record = json.loads(path.read_text())
        record["steps"][0]["argv"] = [sys.executable, "-c", "raise SystemExit(77)"]
        path.write_text(json.dumps(record))
        rejected = self.cli("run", "--repo", self.repo, "--resume", result["run_id"], code=5)
        self.assertEqual(rejected["error"]["code"], "plan_changed")

    def test_terminated_worker_records_unknown_and_cleans_child(self):
        started = self.run_command(
            "import time;print('RUNNING',flush=True);time.sleep(20)", "--background", "--resource", "interrupted"
        )["data"]
        data = self.wait_status(started["run_id"], terminal=False)
        self.processes.append(data["worker_pid"])
        deadline = time.monotonic() + 3
        log = Path(data["receipt_path"]).with_name("command.log")
        while not log.exists() or "RUNNING" not in log.read_text():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.04)
        os.kill(data["worker_pid"], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while self.cli("resources", "--resource", "interrupted")["data"]["busy"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.04)
        value = self.status(started["run_id"], code=1)
        self.assertEqual(value["error"]["status"], "unknown")
        self.cli("run", "--repo", self.repo, "--resume", started["run_id"], code=5)


if __name__ == "__main__":
    unittest.main()
