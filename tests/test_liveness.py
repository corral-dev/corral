"""Unit tests for Antigravity process liveness resolution (`liveness.py`)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import liveness


class AntigravityLivenessTests(unittest.TestCase):
    def test_parse_antigravity_argv_classifies_interactive_vs_headless_and_subcommands(self) -> None:
        self.assertEqual(liveness._parse_antigravity_argv(["agy"]), (True, None))
        self.assertEqual(
            liveness._parse_antigravity_argv(["/Users/u/.local/bin/agy", "--dangerously-skip-permissions"]),
            (True, None),
        )
        self.assertEqual(
            liveness._parse_antigravity_argv(["agy", "--conversation", "cid-42"]),
            (True, "cid-42"),
        )
        self.assertEqual(
            liveness._parse_antigravity_argv(["agy", "--conversation=cid-99", "-i", "help"]),
            (True, "cid-99"),
        )
        for cmd in (
            ["agy", "-p", "hi"],
            ["agy", "--print", "hi"],
            ["agy", "--prompt", "hi"],
            ["agy", "--prompt=hi"],
            ["agy", "-h"],
            ["agy", "--version"],
            ["agy", "acp"],
            ["agy", "models"],
            ["agy", "mcp", "list"],
            ["agy", "plugin", "list"],
            ["agy", "update"],
            ["claude", "--resume", "abc"],
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(liveness._parse_antigravity_argv(cmd), (False, None))

    def test_parse_lsof_lock_fds_keeps_only_regular_lock_fds(self) -> None:
        sample = "\n".join([
            "p101",
            "fcwd",
            "tDIR",
            "n/tmp/root/presence",
            "f12",
            "au",
            "tREG",
            "n/tmp/root/presence/cid-1.lock",
            "f13",
            "ar",
            "tREG",
            "n/tmp/root/presence/not-a-lock.txt",
            "p202",
            "f4",
            "aw",
            "tREG",
            "n/tmp/root/presence/cid-2.lock",
        ])
        self.assertEqual(
            liveness._parse_lsof_lock_fds(sample),
            [
                (101, "u", "/tmp/root/presence/cid-1.lock"),
                (202, "w", "/tmp/root/presence/cid-2.lock"),
            ],
        )

    def test_same_cwd_bare_agy_processes_resolve_one_to_one_and_dead_locks_stay_idle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            presence = root / "presence"
            presence.mkdir()
            (presence / "cid-a.lock").write_text("", encoding="utf-8")
            (presence / "cid-b.lock").write_text("", encoding="utf-8")
            (presence / "cid-dead.lock").write_text("", encoding="utf-8")

            sessions = [
                {
                    "source": "antigravity",
                    "id": "cid-a",
                    "cwd": "/tmp/same-proj",
                    "path": str(root / "brain" / "cid-a" / ".system_generated" / "logs" / "transcript_full.jsonl"),
                    "live": True,
                    "pid": 999999,
                },
                {
                    "source": "antigravity",
                    "id": "cid-b",
                    "cwd": "/tmp/same-proj",
                    "path": str(root / "brain" / "cid-b" / ".system_generated" / "logs" / "transcript_full.jsonl"),
                },
                {
                    "source": "antigravity",
                    "id": "cid-dead",
                    "cwd": "/tmp/same-proj",
                    "path": str(root / "brain" / "cid-dead" / ".system_generated" / "logs" / "transcript_full.jsonl"),
                    "live": True,
                    "pid": 888888,
                },
            ]
            # Process 3333 is headless `agy -p` (excluded by _list_antigravity_processes),
            # 1111 and 2222 are two bare interactive `agy` in the same cwd.
            lsof_fds = [
                (1111, "u", str(presence / "cid-a.lock")),
                (2222, "u", str(presence / "cid-b.lock")),
                (3333, "u", str(presence / "cid-dead.lock")),
                (4444, "u", str(presence / "cid-dead.lock")),
            ]
            with (
                mock.patch.object(
                    liveness,
                    "_list_antigravity_processes",
                    return_value=({1111, 2222}, {}),
                ),
                mock.patch.object(liveness, "_list_lsof_lock_fds", return_value=lsof_fds),
                mock.patch.object(liveness, "list_managed_hosts", return_value=[]),
            ):
                liveness.annotate(sessions)

            by_id = {s["id"]: s for s in sessions}
            self.assertEqual((by_id["cid-a"]["live"], by_id["cid-a"]["pid"]), (True, 1111))
            self.assertEqual((by_id["cid-b"]["live"], by_id["cid-b"]["pid"]), (True, 2222))
            self.assertIs(by_id["cid-dead"]["live"], False)
            self.assertIsNone(by_id["cid-dead"]["pid"])

    def test_conversation_argv_fallback_when_lsof_has_no_lock_fd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "presence").mkdir()
            session = {
                "source": "antigravity",
                "id": "cid-argv",
                "path": str(root / "brain" / "cid-argv" / ".system_generated" / "logs" / "transcript_full.jsonl"),
            }
            with (
                mock.patch.object(
                    liveness,
                    "_list_antigravity_processes",
                    return_value=({5555}, {"cid-argv": 5555}),
                ),
                mock.patch.object(liveness, "_list_lsof_lock_fds", return_value=[]),
            ):
                liveness.annotate_antigravity_sessions([session], force_override=True)
            self.assertIs(session["live"], True)
            self.assertEqual(session["pid"], 5555)

    def test_failed_probe_keeps_existing_live_state(self) -> None:
        """A ps/lsof timeout is unknown, never "everything is dead"."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "presence").mkdir()
            (root / "presence" / "cid-live.lock").write_text("", encoding="utf-8")

            def _session() -> dict:
                return {
                    "source": "antigravity",
                    "id": "cid-live",
                    "path": str(root / "brain" / "cid-live" / ".system_generated" / "logs" / "transcript_full.jsonl"),
                    "live": True,
                    "pid": 7777,
                }

            ps_failed = _session()
            with mock.patch.object(liveness, "_list_antigravity_processes", return_value=None):
                liveness.annotate_antigravity_sessions([ps_failed], force_override=True)
            self.assertEqual((ps_failed["live"], ps_failed["pid"]), (True, 7777))

            lsof_failed = _session()
            with (
                mock.patch.object(liveness, "_list_antigravity_processes", return_value=({7777}, {})),
                mock.patch.object(liveness, "_list_lsof_lock_fds", return_value=None),
            ):
                liveness.annotate_antigravity_sessions([lsof_failed], force_override=True)
                self.assertEqual(liveness.resolve_antigravity_live_pids([lsof_failed]), {})
            self.assertEqual((lsof_failed["live"], lsof_failed["pid"]), (True, 7777))

    def test_lsof_timeout_reports_probe_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            presence = Path(td) / "presence"
            presence.mkdir()
            (presence / "cid.lock").write_text("", encoding="utf-8")
            with (
                mock.patch.object(liveness.shutil, "which", return_value="/usr/sbin/lsof"),
                mock.patch.object(
                    liveness.subprocess, "run",
                    side_effect=subprocess.TimeoutExpired(cmd="lsof", timeout=1.5),
                ),
            ):
                self.assertIsNone(liveness._list_lsof_lock_fds([presence]))

    def test_presence_dirs_never_leak_host_dirs_for_tmp_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture_path = root / "brain" / "cid-x" / ".system_generated" / "logs" / "transcript_full.jsonl"
            env = {k: v for k, v in os.environ.items() if k != "ANTIGRAVITY_APP_DATA_DIR"}
            with mock.patch.dict(os.environ, env, clear=True):
                dirs = liveness._antigravity_presence_dirs(
                    [{"source": "antigravity", "id": "cid-x", "path": str(fixture_path)}]
                )
            self.assertEqual([str(d) for d in dirs], [os.path.join(str(root), "presence")])


if __name__ == "__main__":
    unittest.main()
