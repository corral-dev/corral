"""Shared busy/idle verdict for hosted sessions waiting on background work.

Process-tree and transcript tests use fake tables and temp history files. The
cost probe at the bottom runs against a private tmux socket so it can never
see or touch a developer's live ``corral-keepalive`` sessions.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sesskit.titles import STATUS_DONE

from corral import busycheck, keepalive, reclaim
from corral.busycheck import BusyChecker, Proc, claude_pending_background, process_verdict
from corral.reclaim import BackgroundState, Context, History, Hosted

NOW = 10_000_000.0
HOUR = 3600.0
MIN = 60.0


def table(*rows: tuple[int, int, str]) -> list[Proc]:
    return [Proc(pid, ppid, command) for pid, ppid, command in rows]


def host(name: str = "corral-claude-aaaa1111", **overrides) -> Hosted:
    fields = dict(
        name=name, runtime_id="claude", ident=name.rsplit("-", 1)[-1], socket="corral-keepalive",
        created_at=NOW - 4 * HOUR, session_activity=NOW - 3 * HOUR, window_activity=NOW - 3 * HOUR,
    )
    fields.update(overrides)
    return Hosted(**fields)


def done(**overrides) -> History:
    fields = dict(
        status_tag=STATUS_DONE, active_at=NOW - 3 * HOUR, runtime_id="claude", session_id="aaaa1111-full-id",
    )
    fields.update(overrides)
    return History(**fields)


def ctx(**overrides) -> Context:
    return Context(now=NOW, **overrides)


def idle_checker(
    pane_pids: dict[str, int] | None = None,
    ps: list[Proc] | None = None,
    history_paths: dict[str, str] | None = None,
) -> BusyChecker:
    panes = dict(pane_pids) if pane_pids is not None else {"corral-claude-aaaa1111": 100}
    return BusyChecker(
        ps_table=list(ps) if ps is not None else [Proc(100, 1, "/opt/claude/claude")],
        pane_pids=panes,
        history_paths=history_paths or {},
    )


class ProcessVerdictTests(unittest.TestCase):
    def test_agent_with_only_helpers_is_idle(self) -> None:
        ps = table(
            (100, 1, "/opt/claude/claude"),
            (101, 100, "/opt/claude/claude"),
            (200, 101, "node /usr/lib/node_modules/mcp-server-memory/dist/index.js"),
            (201, 101, "node /home/u/.local/share/mcp/codebase-memory-mcp/server.js"),
            (202, 101, "typescript-language-server --stdio"),
        )
        verdict = process_verdict(100, ps)
        self.assertFalse(verdict.busy)
        self.assertEqual(verdict.reason, "idle")

    def test_background_build_tool_is_busy(self) -> None:
        ps = table(
            (100, 1, "/opt/claude/claude"),
            (300, 100, "npx vite preview --port 5287 --strictPort"),
        )
        verdict = process_verdict(100, ps)
        self.assertTrue(verdict.busy)
        self.assertEqual(verdict.reason, "background_process")
        self.assertIn("vite", verdict.evidence["background_commands"][0])
        self.assertEqual(verdict.evidence["stranger_count"], 1)

    def test_unknown_command_counts_as_busy(self) -> None:
        ps = table(
            (100, 1, "/opt/codex/codex"),
            (400, 100, " janitor-thing --deep-clean"),
        )
        verdict = process_verdict(100, ps)
        self.assertTrue(verdict.busy)
        self.assertEqual(verdict.reason, "background_process")

    def test_bare_shell_leftover_counts_as_busy(self) -> None:
        ps = table(
            (100, 1, "/opt/claude/claude"),
            (500, 100, "/bin/sh -c sleep 600"),
        )
        self.assertTrue(process_verdict(100, ps).busy)

    def test_grandchildren_are_seen_through_wrappers(self) -> None:
        ps = table(
            (100, 1, "/opt/claude/claude"),
            (500, 100, "/bin/bash -c run-tests.sh"),
            (501, 500, "python3 run-tests.py"),
        )
        verdict = process_verdict(100, ps)
        self.assertTrue(verdict.busy)
        self.assertEqual(verdict.evidence["stranger_count"], 2)

    def test_reparented_orphans_escape_the_tree(self) -> None:
        # A background task reparented to init is no longer a descendant:
        # documented miss, covered by the transcript signal instead.
        ps = table(
            (100, 1, "/opt/claude/claude"),
            (600, 1, "npx vite preview --port 5287"),
        )
        self.assertFalse(process_verdict(100, ps).busy)

    def test_missing_pane_pid_or_ps_is_busy_unknown(self) -> None:
        ps = table((100, 1, "claude"))
        self.assertEqual(process_verdict(None, ps).reason, "background_unknown")
        self.assertEqual(process_verdict(999, ps).reason, "background_unknown")
        self.assertEqual(process_verdict(100, None).reason, "background_unknown")

    def test_codex_proxy_wrapper_and_app_server_are_agent_side(self) -> None:
        ps = table(
            (100, 1, "python3 -m corral.codex_proxy -- codex resume abc"),
            (101, 100, "node /opt/codex/app-server.js"),
        )
        self.assertFalse(process_verdict(100, ps).busy)


class TranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def _write(self, name: str, lines: list[dict]) -> str:
        path = str(Path(self.temp.name) / name)
        with open(path, "w", encoding="utf-8") as handle:
            for entry in lines:
                handle.write(json.dumps(entry) + "\n")
        return path

    def _enqueue(self, task_id: str) -> dict:
        return {
            "type": "queue-operation", "operation": "enqueue",
            "content": f"<task-notification>\n<task-id>{task_id}</task-id>\n<status>failed</status>",
        }

    def _remove(self, task_id: str) -> dict:
        return {
            "type": "queue-operation", "operation": "remove",
            "content": f"<task-notification>\n<task-id>{task_id}</task-id>\n<status>failed</status>",
        }

    def test_enqueue_without_remove_is_pending(self) -> None:
        path = self._write("pending.jsonl", [
            {"type": "assistant", "message": {"content": "done"}},
            self._enqueue("bvp1wyceb"),
        ])
        pending, evidence = claude_pending_background(path)
        self.assertTrue(pending)
        self.assertEqual(evidence["pending_task_ids"], ["bvp1wyceb"])
        self.assertEqual(evidence["window"], "full")

    def test_enqueue_then_remove_is_settled(self) -> None:
        path = self._write("settled.jsonl", [self._enqueue("abc123"), self._remove("abc123")])
        pending, _evidence = claude_pending_background(path)
        self.assertFalse(pending)

    def test_one_pending_among_settled_still_blocks(self) -> None:
        path = self._write("mixed.jsonl", [
            self._enqueue("done1"), self._remove("done1"), self._enqueue("open2"),
        ])
        pending, evidence = claude_pending_background(path)
        self.assertTrue(pending)
        self.assertEqual(evidence["pending_task_ids"], ["open2"])

    def test_moved_to_background_without_notification_is_pending(self) -> None:
        path = self._write("moved.jsonl", [
            {"type": "user", "message": {"content": [{
                "type": "tool_result",
                "content": (
                    "Command did not complete within its 120s timeout "
                    "and was moved to the background (ID: bvp1wyceb)."
                ),
            }]}},
        ])
        pending, evidence = claude_pending_background(path)
        self.assertTrue(pending)
        self.assertEqual(evidence["background_moved_ids"], ["bvp1wyceb"])

    def test_moved_then_notified_then_removed_is_settled(self) -> None:
        path = self._write("moved-done.jsonl", [
            {"type": "user", "message": {"content": "moved to the background (ID: bvp1wyceb)."}},
            self._enqueue("bvp1wyceb"),
            self._remove("bvp1wyceb"),
        ])
        pending, _evidence = claude_pending_background(path)
        self.assertFalse(pending)

    def test_missing_or_non_jsonl_path_is_unchecked(self) -> None:
        pending, evidence = claude_pending_background(None)
        self.assertFalse(pending)
        self.assertFalse(evidence["checked"])
        pending, evidence = claude_pending_background(str(Path(self.temp.name) / "absent.jsonl"))
        self.assertFalse(pending)
        self.assertEqual(evidence["why"], "history_unreadable")
        db = self._write("opencode.db", [{"type": "x"}])
        _pending, evidence = claude_pending_background(db)
        self.assertEqual(evidence["why"], "not_claude_jsonl")

    def test_real_world_enqueue_remove_shape(self) -> None:
        # Shape copied from a live 2026-09-28 Claude history (ids shortened).
        content = (
            "<task-notification>\n<task-id>bj01tdhc4</task-id>\n"
            "<tool-use-id>toolu_015qQhdZeaFhNQpaaGbrszRR</tool-use-id>\n"
            "<output-file>/private/tmp/claude-501/tasks/bj01tdhc4.output</output-file>\n<status>failed</status>"
        )
        path = self._write("real.jsonl", [
            {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-09-28T02:36:41Z",
             "sessionId": "s", "content": content},
            {"type": "queue-operation", "operation": "remove", "timestamp": "2026-09-28T02:36:42Z",
             "sessionId": "s", "content": content},
        ])
        self.assertFalse(claude_pending_background(path)[0])


class CheckerTests(unittest.TestCase):
    def test_snapshot_is_fetched_once_per_checker(self) -> None:
        calls = {"ps": 0, "panes": 0}

        def fetch_ps():
            calls["ps"] += 1
            return [Proc(100, 1, "claude --resume")]

        def fetch_panes():
            calls["panes"] += 1
            return {"a": 100, "b": 100}

        checker = BusyChecker(ps_fetcher=fetch_ps, pane_fetcher=fetch_panes)
        checker.verdict("a")
        checker.verdict("b")
        checker.process_busy_names(["a", "b"])
        self.assertEqual(calls, {"ps": 1, "panes": 1})

    def test_tree_busy_short_circuits_before_any_history_read(self) -> None:
        checker = idle_checker(
            pane_pids={"corral-claude-aaaa1111": 100},
            ps=table((100, 1, "claude"), (300, 100, "npx vite --port 1")),
            history_paths={"corral-claude-aaaa1111": "/nonexistent/x.jsonl"},
        )
        verdict = checker.verdict("corral-claude-aaaa1111", runtime_id="claude")
        self.assertEqual(verdict.reason, "background_process")

    def test_quiet_tree_with_pending_transcript_is_busy(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = str(Path(temp.name) / "h.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "queue-operation", "operation": "enqueue",
                "content": "<task-notification>\n<task-id>zzz</task-id>",
            }) + "\n")
        checker = idle_checker(history_paths={"corral-claude-aaaa1111": path})
        verdict = checker.verdict("corral-claude-aaaa1111", runtime_id="claude")
        self.assertTrue(verdict.busy)
        self.assertEqual(verdict.reason, "background_transcript")
        self.assertEqual(verdict.evidence["transcript"]["pending_task_ids"], ["zzz"])

    def test_quiet_tree_with_clean_transcript_is_idle_with_evidence(self) -> None:
        checker = idle_checker(history_paths={"corral-claude-aaaa1111": "/nonexistent/x.jsonl"})
        verdict = checker.verdict("corral-claude-aaaa1111", runtime_id="claude")
        self.assertFalse(verdict.busy)
        self.assertIn("transcript", verdict.evidence)

    def test_non_transcript_runtime_skips_the_history_read(self) -> None:
        checker = idle_checker(
            pane_pids={"corral-pi-cccc3333": 100},
            history_paths={"corral-pi-cccc3333": "/nonexistent/x.jsonl"},
        )
        verdict = checker.verdict(
            "corral-pi-cccc3333", runtime_id="pi",
        )
        self.assertFalse(verdict.busy)
        self.assertNotIn("transcript", verdict.evidence)

    def test_unavailable_pane_map_reads_as_busy_unknown(self) -> None:
        checker = BusyChecker(ps_table=[], pane_fetcher=lambda: None)
        verdict = checker.verdict("corral-claude-aaaa1111", runtime_id="claude")
        self.assertTrue(verdict.busy)
        self.assertEqual(verdict.reason, "background_unknown")

    def test_from_probe_returns_none_without_tmux(self) -> None:
        with mock.patch.object(busycheck.shutil, "which", return_value=None):
            self.assertIsNone(BusyChecker.from_probe())


class ReclaimWiringTests(unittest.TestCase):
    def test_process_tree_busy_holds_with_evidence(self) -> None:
        state = BackgroundState(
            busy_names=frozenset({"corral-claude-aaaa1111"}),
            evidence={"corral-claude-aaaa1111": {"background_commands": ["npx vite --port 1"]}},
        )
        verdict = reclaim.evaluate(host(), [done()], ctx(background=state))
        self.assertFalse(verdict.reclaim)
        self.assertEqual(verdict.reason, "background_busy")
        self.assertEqual(
            verdict.evidence["background"], {"background_commands": ["npx vite --port 1"]},
        )

    def test_pending_transcript_holds_an_otherwise_idle_session(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = str(Path(temp.name) / "h.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "queue-operation", "operation": "enqueue",
                "content": "<task-notification>\n<task-id>zzz</task-id>",
            }) + "\n")
        checker = idle_checker(history_paths={"corral-claude-aaaa1111": path})
        verdict = reclaim.evaluate(host(), [done()], ctx(background=BackgroundState(checker=checker)))
        self.assertFalse(verdict.reclaim)
        self.assertEqual(verdict.reason, "background_busy")
        self.assertEqual(verdict.evidence["background"]["transcript"]["pending_task_ids"], ["zzz"])

    def test_clean_transcript_keeps_the_session_reclaimable_with_evidence(self) -> None:
        checker = idle_checker(history_paths={"corral-claude-aaaa1111": "/nonexistent/x.jsonl"})
        verdict = reclaim.evaluate(host(), [done()], ctx(background=BackgroundState(checker=checker)))
        self.assertTrue(verdict.reclaim)
        self.assertEqual(verdict.reason, "inactive")
        self.assertIn("background", verdict.evidence)

    def test_no_background_state_keeps_the_old_verdict(self) -> None:
        self.assertTrue(reclaim.evaluate(host(), [done()], ctx()).reclaim)

    def test_history_path_flows_from_the_session_dict(self) -> None:
        entry = reclaim.history_from_session({
            "source": "claude", "id": "abc", "status_tag": STATUS_DONE,
            "event_time": 1.0, "path": "/tmp/hist.jsonl",
        })
        self.assertEqual(entry.history_path, "/tmp/hist.jsonl")
        self.assertEqual(reclaim.history_from_session({}).history_path, "")


class BuildContextTests(unittest.TestCase):
    def _sources(self, probe):
        from corral.attention import AttentionStore

        return (
            mock.patch.object(AttentionStore, "busy_pairs", return_value=[]),
            mock.patch("corral.embed.live_host_viewer_names", return_value=set()),
            mock.patch("corral.split_layout.pinned_keys_effective", return_value=set()),
            mock.patch.object(busycheck.BusyChecker, "from_probe", probe),
        )

    def test_unreadable_background_probe_blocks_the_whole_pass(self) -> None:
        with contextlib.ExitStack() as stack:
            for patcher in self._sources(lambda **kwargs: None):
                stack.enter_context(patcher)
            self.assertIsNone(reclaim._build_context(NOW, [host()], {}))

    def test_background_state_names_process_busy_sessions(self) -> None:
        checker = idle_checker(
            pane_pids={"corral-claude-aaaa1111": 100},
            ps=table((100, 1, "claude"), (300, 100, "npx vite --port 1")),
        )
        with contextlib.ExitStack() as stack:
            for patcher in self._sources(lambda **kwargs: checker):
                stack.enter_context(patcher)
            state_ctx = reclaim._build_context(NOW, [host()], {})
        assert state_ctx is not None
        self.assertEqual(set(state_ctx.background.busy_names), {"corral-claude-aaaa1111"})


class MigrationScriptTests(unittest.TestCase):
    @staticmethod
    def _load_script():
        path = Path(keepalive.__file__).resolve().parent.parent.parent / "scripts" / "migrate-keepalive-server.py"
        spec = importlib.util.spec_from_file_location("migrate_keepalive_server", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # The real `log` appends to ~/.cache/corral/keepalive-migration.log and
        # events.log; tests must not leave fake migration records there.
        module.log = lambda event, **fields: None
        return module

    def test_busy_covers_working_and_background_process(self) -> None:
        script = self._load_script()
        checker = BusyChecker(
            ps_table=table((100, 1, "claude"), (101, 1, "claude"), (300, 100, "sleep 999")),
            pane_pids={"corral-claude-busy": 100, "corral-claude-quiet": 101},
        )
        with mock.patch.object(
            keepalive, "_load_working_pairs", return_value=[("claude", "working-full-id")],
        ), mock.patch.object(
            busycheck.BusyChecker, "from_probe", return_value=checker,
        ):
            busy = script.busy_hosted(["corral-claude-working", "corral-claude-busy", "corral-claude-quiet"])
        self.assertEqual(busy["corral-claude-working"], "working")
        self.assertEqual(busy["corral-claude-busy"], "background_process")
        self.assertNotIn("corral-claude-quiet", busy)

    def test_unusable_probe_blocks_everything(self) -> None:
        script = self._load_script()
        with mock.patch.object(keepalive, "_load_working_pairs", return_value=[]), mock.patch.object(
            busycheck.BusyChecker, "from_probe", return_value=None,
        ):
            busy = script.busy_hosted(["corral-claude-a"])
        self.assertEqual(busy, {"corral-claude-a": "background_unknown"})

    def test_wait_refuses_while_busy_and_force_bypasses(self) -> None:
        script = self._load_script()
        with mock.patch.object(script, "busy_hosted", return_value={"s": "working"}), \
                mock.patch.object(script.time, "sleep") as asleep:
            self.assertFalse(script.wait_until_idle(0, 0.01))
            # time.sleep is patched process-wide; background threads from other
            # tests in the same shard may nap too. Only the poll wait matters.
            self.assertNotIn(mock.call(0.01), asleep.call_args_list)
        with mock.patch.object(script, "busy_hosted", side_effect=AssertionError("must not probe")):
            self.assertTrue(script.wait_until_idle(60, 0.01, force=True))

    def test_wait_logs_the_blocking_sessions(self) -> None:
        script = self._load_script()
        calls = [{"s": "background_process"}, {}]
        with mock.patch.object(script, "busy_hosted", side_effect=calls), \
                mock.patch.object(script, "log") as logged, \
                mock.patch.object(script.time, "sleep"), \
                mock.patch("builtins.print") as printed:
            self.assertTrue(script.wait_until_idle(60, 0.01))
        reasons = [call.kwargs.get("busy") for call in logged.call_args_list]
        self.assertIn({"s": "background_process"}, reasons)
        printed.assert_called()


class CostTests(unittest.TestCase):
    """The check runs periodically: one ps + one list-panes per tick, on a
    private socket so real sessions are never touched."""

    socket = ""
    env: dict = {}

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("tmux") is None:
            raise unittest.SkipTest("tmux not installed")
        cls.socket = f"bc-test-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        cls.env = keepalive.tmux_env()
        for index in range(3):
            subprocess.run(
                ["tmux", "-L", cls.socket, "-f", "/dev/null", "new-session",
                 "-d", "-s", f"corral-claude-ffff{index:04d}",
                 "-x", "80", "-y", "24", "--", "sleep", "600"],
                env=cls.env, timeout=10, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.socket:
            subprocess.run(
                ["tmux", "-L", cls.socket, "kill-server"],
                env=cls.env, timeout=10, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            with contextlib.suppress(OSError):
                os.unlink(f"/tmp/tmux-{os.getuid()}/{cls.socket}")

    def test_tick_cost_is_milliseconds(self) -> None:
        costs = busycheck.measure_cost(sockets=[self.socket], repeats=3)
        print(f"\nbusycheck tick cost on 3 hosted sessions: {costs}")
        self.assertLess(costs["max_ms"], 5000.0)


if __name__ == "__main__":
    unittest.main()
