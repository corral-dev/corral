"""Durable recent-combination preferences, without reading user state."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import recent_starts


class RecentStartsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "combos.json"
        patch = mock.patch.dict(os.environ, CORRAL_QUICK_START_PATH=str(self.path))
        patch.start()
        self.addCleanup(patch.stop)

    def test_record_is_durable_bounded_and_moves_reused_pair_to_front(self):
        for i in range(recent_starts.MAX_COMBOS + 2):
            recent_starts.record("claude", f"{self.temp.name}/project{i}")
        self.assertEqual([c.project_label for c in recent_starts.load()],
                         [f"project{i}" for i in range(recent_starts.MAX_COMBOS + 1, 1, -1)])
        recent_starts.record("claude", f"{self.temp.name}/project3")
        combos = recent_starts.load()
        self.assertEqual(len(combos), recent_starts.MAX_COMBOS)
        self.assertEqual(combos[0].project_label, "project3")
        proc = subprocess.run(
            [sys.executable, "-c", "from corral import recent_starts; print(recent_starts.load()[0].project_label)"],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual(proc.stdout.strip(), "project3")

    def test_runtime_and_full_path_define_identity_and_removal(self):
        first, second = f"{self.temp.name}/one/app", f"{self.temp.name}/two/app"
        for runtime, cwd in (("claude", first), ("codex", first), ("claude", second)):
            recent_starts.record(runtime, cwd)
        combos = recent_starts.load()
        self.assertEqual(len(combos), 3)
        recent_starts.remove(combos[1])
        self.assertEqual(recent_starts.load(), [combos[0], combos[2]])

    def test_corruption_and_dormant_runtime_do_not_break_loading(self):
        self.path.write_text("not json")
        self.assertEqual(recent_starts.load(), [])
        self.path.write_text(json.dumps([
            {"runtime_id": [], "cwd": "/tmp", "project_label": "bad"},
            {"runtime_id": "kimi", "cwd": "/tmp", "project_label": "hidden"},
            {"runtime_id": "claude", "cwd": "relative", "project_label": "bad"},
            {"runtime_id": "codex", "cwd": "/tmp", "project_label": "valid"},
        ]))
        self.assertEqual(recent_starts.load(), [recent_starts.RecentStart("codex", "/tmp", "valid")])

    def test_history_fills_past_five_without_reviving_dismissed_pairs(self):
        root = Path(self.temp.name).resolve()
        candidates = [recent_starts.RecentStart("codex", str(root / f"project{i}"), f"Project{i}")
                      for i in range(20)]
        recent_starts.record("codex", candidates[3].cwd)
        self.assertEqual(len(recent_starts.load(candidates)), 20)
        self.assertEqual(recent_starts.load(candidates)[0].cwd, candidates[3].cwd)
        recent_starts.remove(candidates[8])
        self.assertNotIn(candidates[8], recent_starts.load(candidates))
        recent_starts.record("codex", candidates[8].cwd)
        self.assertEqual(recent_starts.load(candidates)[0].cwd, candidates[8].cwd)
        self.assertEqual(len(recent_starts.load(candidates + candidates)), 20)

    def test_write_failure_does_not_interrupt_launch(self):
        with mock.patch.object(recent_starts.os, "replace", side_effect=OSError("read only")):
            recent_starts.record("claude", self.temp.name)
        self.assertEqual(recent_starts.load(), [])
        self.assertEqual(list(Path(self.temp.name).glob("tmp*")), [])

    def test_concurrent_tuis_preserve_each_other_combinations(self):
        code = "from corral import recent_starts; import sys; recent_starts.record('codex', sys.argv[1])"
        procs = [subprocess.Popen([sys.executable, "-c", code, f"{self.temp.name}/p{i}"])
                 for i in range(5)]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=10), 0)
        self.assertEqual({c.project_label for c in recent_starts.load()}, {f"p{i}" for i in range(5)})
