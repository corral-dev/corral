"""Claude `continued-in` follow-up: resume the latest id, merge list cards.

Incident 2026-09-30: an old session file carried
``{"type":"continued-in",...,"continuedInSessionId":<new>}`` while the
conversation continued in a new file; resuming the scanned old id missed
~10 minutes of work. Every resume path funnels through
``ClaudeRuntime.build_resume_plan`` (TUI host controller, restart,
agent_api, remote sessions, cli resume command), so resolving there plus
merging the superseded list card covers all of them.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from corral.models import LaunchRequest
from corral.runtime.claude import (
    ClaudeRuntime,
    _merge_continued_sessions,
    _resolve_continuation_id,
)
from corral.runtime.registry import default_registry
from corral.scan import claude as scan_claude

OLD = "aaaaaaaa-0000-4000-8000-000000000001"
NEW = "bbbbbbbb-0000-4000-8000-000000000002"


def _user(sid: str, text: str, cwd: str) -> str:
    return json.dumps({
        "type": "user", "cwd": cwd, "sessionId": sid,
        "timestamp": "2026-09-30T00:00:01.000Z",
        "message": {"role": "user", "content": text},
    })


def _assistant(sid: str, text: str, cwd: str) -> str:
    return json.dumps({
        "type": "assistant", "cwd": cwd, "sessionId": sid,
        "timestamp": "2026-09-30T00:00:02.000Z",
        "message": {"role": "assistant",
                    "content": [{"type": "text", "text": text}]},
    })


def _continued(old: str, new: str) -> str:
    return json.dumps({
        "type": "continued-in", "sessionId": old,
        "continuedInSessionId": new,
        "timestamp": "2026-09-30T00:00:03.000Z",
    })


class ContinuationFixture:
    def __init__(self, root: Path, *, target_missing: bool = False):
        self.cache_dir = str(root / "cache")
        self.cwd = root / "work"
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.folder = root / "projects" / "-work"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.old_path = self.folder / f"{OLD}.jsonl"
        self.old_path.write_text("\n".join([
            _user(OLD, "first task", str(self.cwd)),
            _assistant(OLD, "working on it", str(self.cwd)),
            _continued(OLD, NEW),
        ]) + "\n")
        self.new_path = self.folder / f"{NEW}.jsonl"
        if not target_missing:
            self.new_path.write_text("\n".join([
                _user(NEW, "continued task", str(self.cwd)),
                _assistant(NEW, "continued work", str(self.cwd)),
            ]) + "\n")

    def patched(self):
        return (
            mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self.cache_dir}),
            mock.patch.object(scan_claude, "PROJECTS_DIR", str(self.folder.parent)),
            mock.patch.object(scan_claude, "SESSIONS_DIR",
                              str(self.folder.parent / "nosuch")),
            mock.patch.object(scan_claude, "_live_session_ids", return_value={}),
        )

    def scan(self, limit: int = 10, keep_ids=None) -> list[dict]:
        patches = self.patched()
        for patch in patches:
            patch.start()
        try:
            return ClaudeRuntime().scan_sessions(limit, keep_ids=keep_ids)
        finally:
            for patch in reversed(patches):
                patch.stop()


class MergeTests(unittest.TestCase):
    def test_superseded_card_merges_onto_latest(self):
        with TemporaryDirectory() as td:
            sessions = ContinuationFixture(Path(td)).scan()
        ids = [s["id"] for s in sessions]
        self.assertIn(NEW, ids)
        self.assertNotIn(OLD, ids)

    def test_missing_target_keeps_old_card(self):
        with TemporaryDirectory() as td:
            sessions = ContinuationFixture(Path(td), target_missing=True).scan()
        ids = [s["id"] for s in sessions]
        self.assertIn(OLD, ids)

    def test_merge_path_tolerates_keep_ids(self):
        with TemporaryDirectory() as td:
            sessions = ContinuationFixture(Path(td)).scan(keep_ids={OLD})
        ids = [s["id"] for s in sessions]
        self.assertIn(NEW, ids)
        self.assertNotIn(OLD, ids)

    def test_merge_unit_transfers_host_binding(self):
        old = {"source": "claude", "id": OLD, "superseded_by": NEW,
               "keepalive_name": "corral-claude-x1"}
        new = {"source": "claude", "id": NEW}
        merged = _merge_continued_sessions([old, new])
        self.assertEqual([s["id"] for s in merged], [NEW])
        self.assertEqual(merged[0].get("keepalive_name"), "corral-claude-x1")

    def test_merge_unit_keeps_flagged_old_when_target_absent(self):
        old = {"source": "claude", "id": OLD, "superseded_by": "missing-id"}
        merged = _merge_continued_sessions([old])
        self.assertEqual(merged, [old])


class ResumeResolutionTests(unittest.TestCase):
    def test_resume_plan_uses_latest_id(self):
        with TemporaryDirectory() as td:
            fix = ContinuationFixture(Path(td))
            fix.scan()
            old_session = {"source": "claude", "id": OLD,
                           "path": str(fix.old_path),
                           "cwd": str(fix.cwd)}
            plan = ClaudeRuntime().build_resume_plan(old_session)
        self.assertIn(NEW, plan.argv)
        self.assertNotIn(OLD, plan.argv)
        self.assertIn("--resume", plan.argv)

    def test_continue_and_fork_plans_resolve(self):
        with TemporaryDirectory() as td:
            fix = ContinuationFixture(Path(td))
            fix.scan()
            old_session = {"source": "claude", "id": OLD,
                           "path": str(fix.old_path),
                           "cwd": str(fix.cwd)}
            runtime = ClaudeRuntime()
            cont = runtime.build_continue_plan(old_session, "go on")
            fork = runtime.build_fork_plan(old_session)
        self.assertIn(NEW, cont.argv)
        self.assertNotIn(OLD, cont.argv)
        self.assertIn(NEW, fork.argv)
        self.assertNotIn(OLD, fork.argv)

    def test_registry_launch_plan_resolves(self):
        """TUI open / restart / agent_api / remote all funnel through here."""
        with TemporaryDirectory() as td:
            fix = ContinuationFixture(Path(td))
            fix.scan()
            old_session = {"source": "claude", "id": OLD,
                           "path": str(fix.old_path),
                           "cwd": str(fix.cwd)}
            plan = default_registry().build_launch_plan(
                LaunchRequest(old_session, "claude", "title"))
        self.assertIn(NEW, plan.argv)
        self.assertNotIn(OLD, plan.argv)

    def test_resolution_falls_back_when_history_gone(self):
        session = {"source": "claude", "id": OLD,
                   "path": "/nonexistent-dir/x.jsonl", "cwd": "/tmp"}
        self.assertEqual(_resolve_continuation_id(session), OLD)
        plan = ClaudeRuntime().build_resume_plan(session)
        self.assertIn(OLD, plan.argv)


if __name__ == "__main__":
    unittest.main()
