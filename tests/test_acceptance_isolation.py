#!/usr/bin/env python3
"""Focused helper tests for the isolated acceptance entry.

Pure unit coverage only: envelope shape, failure categories, socket naming,
date-bucket stability. Never touches real sessions, the shared keepalive
socket, or the Textual UI (the real run lives in ``scripts/acceptance.py``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "acceptance.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("acceptance_entry", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["acceptance_entry"] = module
    spec.loader.exec_module(module)
    return module


ACC = _load_module()


class EnvelopeTests(unittest.TestCase):
    def test_success_envelope_has_all_keys(self) -> None:
        env = ACC.build_envelope(True, {"a": 1}, None, {"elapsed_s": 0.1})
        self.assertEqual(set(env), {"ok", "data", "error", "meta"})
        self.assertTrue(env["ok"])
        self.assertIsNone(env["error"])

    def test_failure_envelope_carries_stable_category(self) -> None:
        for category in ("setup", "product_assertion", "timeout"):
            env = ACC.fail_envelope(category, "boom")
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], category)

    def test_unknown_category_falls_back_to_setup(self) -> None:
        env = ACC.fail_envelope("mystery", "boom")
        self.assertEqual(env["error"]["code"], "setup")


class IsolationNamingTests(unittest.TestCase):
    def test_socket_names_unique_and_never_shared(self) -> None:
        names = {ACC.socket_name() for _ in range(20)}
        self.assertEqual(len(names), 20)
        for name in names:
            self.assertTrue(name.startswith("corral-accept-"))
            self.assertNotIn("keepalive", name)

    def test_backend_names_avoid_managed_prefixes(self) -> None:
        names = ACC.backend_names("deadbeef")
        self.assertEqual(len(names), ACC.GROUP_SIZE)
        for name in names:
            self.assertFalse(name.startswith("corral-"))
            self.assertFalse(name.startswith("pickup-"))
            self.assertFalse(name.startswith("sc-"))


class DateBucketTests(unittest.TestCase):
    def test_live_always_today_regardless_of_wall_clock(self) -> None:
        now = time.time()
        for mtime in (now, now - 86400 * 30, now - 86400 * 365, 0, -1):
            self.assertEqual(ACC.days_ago(mtime, now, live=True), 0)

    def test_invalid_mtime_never_steals_named_bucket(self) -> None:
        now = time.time()
        self.assertGreaterEqual(ACC.days_ago(0, now), 7)
        self.assertGreaterEqual(ACC.days_ago(-5, now), 7)

    def test_same_day_is_zero_next_day_is_one(self) -> None:
        noon = time.mktime(time.strptime("2026-03-04 12:00", "%Y-%m-%d %H:%M"))
        self.assertEqual(ACC.days_ago(noon - 3600, noon), 0)
        self.assertEqual(ACC.days_ago(noon - 86400, noon), 1)


class RouteHookTests(unittest.TestCase):
    def test_own_names_route_to_isolated_socket(self) -> None:
        argv = ACC.route_argv("sock-1", "acc-xyz-", "acc-xyz-0", lambda n: ("tmux", "-L", "other"))
        self.assertEqual(argv, ("tmux", "-L", "sock-1"))
        self.assertEqual(
            ACC.route_socket("sock-1", "acc-xyz-", "acc-xyz-0", lambda n: "other"), "sock-1")

    def test_foreign_names_fall_through_untouched(self) -> None:
        for name in ("corral-claude-s0", "pickup-claude-s0", "sc-claude-s0", None, ""):
            self.assertEqual(
                ACC.route_argv("sock-1", "acc-xyz-", name, lambda n: ("tmux", "-L", "other")),
                ("tmux", "-L", "other"))
            self.assertEqual(
                ACC.route_socket("sock-1", "acc-xyz-", name, lambda n: "other"), "other")


class DryRunTests(unittest.TestCase):
    def test_dry_run_json_exit_zero_and_no_side_effects(self) -> None:
        before = set(os.listdir(tempfile.gettempdir()))
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--dry-run", "--json"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "CORRAL_ISOLATE_MANAGED_HOSTS": "1"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        envelope = json.loads(proc.stdout)
        self.assertTrue(envelope["ok"])
        self.assertTrue(envelope["meta"].get("dry_run"))
        self.assertFalse(envelope["data"]["reads_real_sessions"])
        self.assertFalse(envelope["data"]["touches_shared_socket"])
        self.assertFalse(envelope["data"]["capture_mocked"])
        self.assertIn("real tmux capture/liveness", envelope["data"]["terminal_behavior"])
        after = set(os.listdir(tempfile.gettempdir()))
        new_dirs = {name for name in after - before if name.startswith("corral-accept-")}
        self.assertEqual(new_dirs, set())
        tmux_dir = Path(f"/tmp/tmux-{os.getuid()}")
        if tmux_dir.is_dir():
            strays = [p.name for p in tmux_dir.iterdir()
                      if p.name.startswith("corral-accept-")]
            self.assertEqual(strays, [])

    def test_unknown_flag_is_usage_error(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--no-such-flag"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
