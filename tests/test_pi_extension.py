"""Run Pi ownership event handlers against the actual bundled TS extension."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

from corral import pi_identity


class PiExtensionOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Optional Pi runtime requires Node.js 22.19+ for TS handler tests")
        probe = subprocess.run(
            [cls.node, "-p", "process.features.typescript ?? ''"],
            capture_output=True, text=True, timeout=10,
        )
        if probe.returncode or not probe.stdout.strip():
            raise unittest.SkipTest(f"Node.js TS API unavailable: {probe.stderr}")

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="pi-owner-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.current = self.root / "current.jsonl"
        self.target = self.root / "history_target.jsonl"
        self.current.write_text("fixture current\n")
        self.target.write_text("fixture target\n")
        self.owner = {
            "protocolVersion": 1, "pid": os.getpid(), "instanceId": "old-1.1-owner",
            "instanceNonce": "old-nonce", "ownershipToken": "original-token",
            "canonicalSessionFile": str(self.target.resolve()),
        }
        self.target_lock = self.write_owner(self.target, self.owner)

    def write_owner(self, path: Path, owner: dict) -> Path:
        digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        lock = self.root / "corral-session-identity" / "owners" / "v1" / f"{digest}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps(owner))
        return lock

    def run_extension(self, **overrides) -> dict:
        spec = {
            "agentDir": str(self.root), "sessionId": "current",
            "sessionFile": str(self.current), "target": str(self.target),
            "parents": f"{os.getpid()} 1\n",
            **overrides,
        }
        config = self.root / "config.json"
        config.write_text(json.dumps(spec))
        result = subprocess.run(
            [self.node, str(Path(__file__).parent / "fixtures" / "pi_identity_harness.mjs"),
             str(pi_identity.asset_path()), str(config)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # Keep Node diagnostics visible instead of hiding them in capture_output.
        if result.stderr:
            print(result.stderr, end="")
        return json.loads(result.stdout)

    def test_cancel_identifies_target_and_preserves_both_owners_and_current_claim(self) -> None:
        result = self.run_extension()
        self.assertEqual(result["result"], {"cancel": True})
        self.assertFalse(result["shutdown"])
        self.assertEqual(result["input"], {"action": "continue"})
        message = result["notifications"][0]["message"]
        self.assertIn(self.target.name, message)
        self.assertIn(f"Owner PID: {os.getpid()}", message)
        self.assertIn("exit Pi in the original window", message)
        self.assertNotIn("attach-session", message)
        self.assertEqual(json.loads(self.target_lock.read_text()), self.owner)
        claims = list((self.root / "corral-session-identity" / "claims" / "v1").glob("*.json"))
        claim = json.loads(claims[0].read_text())
        self.assertEqual((claim["sessionId"], claim["state"], claim["sequence"]), ("current", "active", 1))
        self.assertEqual(len(list(self.target_lock.parent.glob("*.lock"))), 2)
        self.assertEqual(self.target.read_text(), "fixture target\n")

    def test_legacy_owner_located_by_pid_in_existing_socket(self) -> None:
        directory = self.root / f"tmux-{os.getuid()}"
        directory.mkdir()
        path = directory / "legacy"
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(path))
            result = self.run_extension(panes={str(path): f"{os.getpid()}\t$7:0.2\told-pi:0.2\n"})
        message = result["notifications"][0]["message"]
        self.assertIn("Tmux: old-pi:0.2", message)
        command = next(line for line in message.splitlines() if line.startswith("tmux -S"))
        self.assertEqual(shlex.split(command), ["tmux", "-S", str(path), "attach-session", "-t", "$7:0.2"])

    def test_names_shared_between_servers_do_not_override_pid_evidence(self) -> None:
        right = str(self.root / "right")
        wrong = str(self.root / "wrong")
        self.owner["tmuxSocket"] = right
        self.write_owner(self.target, self.owner)
        result = self.run_extension(
            tmux=f"{wrong},123,0",
            panes={wrong: "1\t$0:0.0\tcorral-pi-target:0.0\n",
                   right: f"{os.getpid()}\t$3:0.0\tcorral-pi-target:0.0\n"},
        )
        message = result["notifications"][0]["message"]
        self.assertIn(right, message)
        self.assertNotIn(wrong, message)

    def test_owner_child_matches_pane_parent_and_socket_quotes_are_preserved(self) -> None:
        child = subprocess.Popen([self.node, "-e", "setInterval(()=>{},10000)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.terminate)
        self.owner.update(pid=child.pid, tmuxSocket=str(self.root / "comma,'socket"))
        self.write_owner(self.target, self.owner)
        result = self.run_extension(
            parents=f"{child.pid} {os.getpid()}\n{os.getpid()} 1\n",
            panes={self.owner["tmuxSocket"]: f"{os.getpid()}\t$0:1.2\tparent:1.2\n"},
        )
        command = next(
            line for line in result["notifications"][0]["message"].splitlines()
            if line.startswith("tmux -S")
        )
        self.assertEqual(shlex.split(command)[2], self.owner["tmuxSocket"])

    def test_missing_tmux_or_timeout_still_explains_safe_recovery(self) -> None:
        result = self.run_extension(tmux=f"{self.root}/unavailable,123,0", slowTmux=True)
        message = result["notifications"][0]["message"]
        self.assertIn("could not be located", message)
        self.assertIn("retry /resume", message)
        self.assertEqual(result["result"], {"cancel": True})

    def test_startup_conflict_stops_and_blocks_input_without_replacing_owner(self) -> None:
        result = self.run_extension(sessionFile=str(self.target), sessionId="target", target=None)
        self.assertTrue(result["shutdown"])
        self.assertEqual(result["input"], {"action": "handled"})
        self.assertEqual(json.loads(self.target_lock.read_text()), self.owner)
        self.assertFalse((self.root / "corral-session-identity" / "claims").exists())

    def test_resume_self_is_allowed(self) -> None:
        result = self.run_extension(target=str(self.current))
        self.assertNotIn("result", result)
        self.assertEqual(result["notifications"], [])
        self.assertFalse(result["shutdown"])

    def test_dead_owner_is_reclaimed_without_reporting_live_conflict(self) -> None:
        child = subprocess.Popen([self.node, "-e", ""])
        child.wait(timeout=10)
        self.owner["pid"] = child.pid
        self.write_owner(self.target, self.owner)
        result = self.run_extension(sessionFile=str(self.target), sessionId="target", target=None)
        self.assertFalse(result["shutdown"])
        self.assertEqual(result["notifications"], [])
        self.assertEqual(json.loads(self.target_lock.read_text())["pid"], result["pid"])

    def test_symlink_target_does_not_bypass_live_owner(self) -> None:
        link = self.root / "alias.jsonl"
        link.symlink_to(self.target)
        self.assertEqual(self.run_extension(target=str(link))["result"], {"cancel": True})


if __name__ == "__main__":
    unittest.main()
