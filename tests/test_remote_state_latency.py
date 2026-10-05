"""State freshness between full scans (owner budget: ~2 s to every client).

Dots, Working, Ended and newly hosted panes must not wait for the throttled
full scan, which backs off to a minute under memory pressure, and a new session
history file must not wait out the scan worker's churn backoff.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from corral import history_watch
from corral import store as store_module
from corral.attention import AttentionEvidence, AttentionState
from corral.history_watch import HistoryWatcher, is_session_arrival_path


def _dead_pid() -> int:
    """A pid that is guaranteed not to exist right now."""
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class ArrivalDetectionTests(unittest.TestCase):
    def test_session_history_paths_count_as_arrivals(self) -> None:
        self.assertTrue(is_session_arrival_path("/h/.claude/projects/p/abc.jsonl"))
        self.assertTrue(is_session_arrival_path("/h/.codex/sessions/2026/10/05/rollout-x.jsonl"))
        self.assertTrue(is_session_arrival_path("/h/.cursor/chats/a/b/store.db"))
        self.assertFalse(is_session_arrival_path("/h/.claude/projects/p/abc/subagents/agent-1.jsonl"))
        self.assertFalse(is_session_arrival_path("/h/.cursor/chats/a/b/store.db-wal"))
        self.assertFalse(is_session_arrival_path("/h/.pi/agent/claims/x.json"))
        self.assertFalse(is_session_arrival_path(""))

    def test_note_created_counts_each_new_history_once(self) -> None:
        watcher = HistoryWatcher(roots=[])
        self.assertEqual(watcher.arrival_seq, 0)
        watcher.note_created("/h/.claude/projects/p/a.jsonl")
        # FSEvents can repeat the created flag on later appends.
        watcher.note_created("/h/.claude/projects/p/a.jsonl")
        watcher.note_created("/h/.claude/projects/p/notes.txt")
        self.assertEqual(watcher.arrival_seq, 1)
        self.assertTrue(watcher.is_set())
        watcher.note_created("/h/.claude/projects/p/b.jsonl")
        self.assertEqual(watcher.arrival_seq, 2)

    def test_inotify_records_are_parsed_into_created_paths(self) -> None:
        import struct

        watcher = HistoryWatcher(roots=[])
        name = b"new.jsonl\0\0\0"
        modify = struct.pack("iIII", 1, 0x2, 0, len(name)) + name
        create = struct.pack("iIII", 1, 0x100, 0, len(name)) + name
        watcher._note_inotify_creations(modify + create, {1: "/h/.pi/agent/sessions/p"}, 0x100)
        self.assertEqual(watcher.arrival_seq, 1)

    @unittest.skipUnless(os.uname().sysname == "Darwin", "FSEvents backend")
    def test_fsevents_reports_a_real_new_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            watcher = HistoryWatcher(roots=[root], debounce=0.05)
            watcher.start()
            self.addCleanup(watcher.stop)
            deadline = time.monotonic() + 2.0
            while watcher.backend == "none" and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.3)
            (root / "s1.jsonl").write_text("{}\n", encoding="utf-8")
            deadline = time.monotonic() + 5.0
            while watcher.arrival_seq == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(watcher.arrival_seq, 1)


class FreshenFromDiskTests(unittest.TestCase):
    def test_grown_history_and_exited_pid_are_corrected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            path.write_text("x" * 100, encoding="utf-8")
            stale_mtime = path.stat().st_mtime - 30
            session = {
                "source": "claude", "id": "s", "path": str(path), "live": True,
                "pid": _dead_pid(), "size_bytes": 10, "file_mtime": stale_mtime,
                "mtime": stale_mtime,
            }
            self.assertTrue(store_module._freshen_from_disk([session]))
            self.assertEqual(session["size_bytes"], 100)
            self.assertGreater(session["file_mtime"], stale_mtime)
            self.assertFalse(session["live"])
            self.assertIsNone(session["pid"])
            # The display mtime is the scanner's activity time; leave it alone.
            self.assertEqual(session["mtime"], stale_mtime)
            self.assertFalse(store_module._freshen_from_disk([session]))

    def test_cold_sessions_and_shared_databases_are_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.db"
            path.write_bytes(b"x" * 50)
            opencode = {
                "source": "opencode", "id": "o", "path": str(path), "live": True,
                "pid": os.getpid(), "size_bytes": 7,
            }
            cold = {"source": "claude", "id": "c", "path": str(path), "live": False,
                    "size_bytes": 7}
            self.assertFalse(store_module._freshen_from_disk([opencode, cold]))
            self.assertEqual(opencode["size_bytes"], 7)
            self.assertTrue(opencode["live"])
            self.assertEqual(cold["size_bytes"], 7)


class _Runtime:
    id = "claude"
    display_name = "Claude"

    def __init__(self, sessions):
        self.sessions = sessions

    def is_available(self):
        return True

    def scan_sessions(self, limit, **_kwargs):
        return [dict(item) for item in self.sessions]


class StoreRefreshStateTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = mock.patch.dict(
            os.environ,
            {"CORRAL_CACHE_DIR": str(self.tmp / "cache"), "CORRAL_ISOLATE_MANAGED_HOSTS": "1"},
        )
        env.start()
        self.addCleanup(env.stop)

    def _store(self, sessions):
        import corral

        registry = corral.RuntimeRegistry((_Runtime(sessions),))
        with mock.patch.object(corral.titles, "load_cache", return_value={}):
            store = corral.SessionStore(limit=20, registry=registry)
            store.load()
        return store

    def test_finished_turn_reaches_the_dot_without_a_scan(self) -> None:
        path = self.tmp / "s.jsonl"
        path.write_text("a" * 10, encoding="utf-8")
        session = {
            "source": "claude", "id": "s1", "short_id": "s1", "path": str(path),
            "mtime": time.time(), "file_mtime": path.stat().st_mtime,
            "size_bytes": 10, "size_kb": 0.0, "native_title": None,
            "fallback_title": "t", "cwd": str(self.tmp), "live": True,
            "pid": os.getpid(), "first_user_msg": "hi",
        }
        phases = iter(["working", "idle"])

        def inspect(candidate):
            return AttentionEvidence(phase=next(phases), observed_at=time.time(), source="history")

        with mock.patch.object(store_module, "inspect_session", side_effect=inspect):
            store = self._store([session])
            listed = store.all_sessions()[0]
            self.assertEqual(listed.get("attention_kind"), "working")
            path.write_text("a" * 40, encoding="utf-8")  # the turn's final record
            with mock.patch.object(store.registry, "scan_all") as scan_all:
                self.assertTrue(store.refresh_state())
                scan_all.assert_not_called()
        listed = store.all_sessions()[0]
        self.assertNotEqual(listed.get("attention_kind"), "working")

    def test_exited_process_clears_live_without_a_scan(self) -> None:
        session = {
            "source": "claude", "id": "s2", "short_id": "s2", "path": "",
            "mtime": time.time(), "size_bytes": 1, "size_kb": 0.0,
            "native_title": None, "fallback_title": "t", "cwd": str(self.tmp),
            "live": True, "pid": os.getpid(), "first_user_msg": "hi",
        }
        with mock.patch.object(
            store_module, "inspect_session",
            return_value=AttentionEvidence(phase="idle", observed_at=time.time(), source="history"),
        ):
            store = self._store([session])
            store.sessions["claude"][0]["pid"] = _dead_pid()
            self.assertTrue(store.refresh_state())
        self.assertFalse(store.all_sessions()[0]["live"])

    def test_partial_injection_keeps_cold_session_attention(self) -> None:
        store = self._store([])
        cold = {"source": "claude", "id": "c", "attention_kind": "unread"}
        hot = {"source": "claude", "id": "h", "live": True}
        store.sessions["claude"] = [cold, hot]
        store.attention_states = {"claude:c": AttentionState(kind="unread")}
        store._inject_partial_attention_states({"claude:h": AttentionState(kind="working")})
        self.assertEqual(cold["attention_kind"], "unread")
        self.assertEqual(hot["attention_kind"], "working")
        self.assertIn("claude:c", store.attention_states)

    def test_vanished_host_drops_hosted_mark_only_when_listing_is_trustworthy(self) -> None:
        store = self._store([])
        gone = {"source": "claude", "id": "g", "live": True, "keepalive_name": "corral-claude-g"}
        kept = {"source": "claude", "id": "k", "live": True, "keepalive_name": "corral-claude-k"}
        with (
            mock.patch.object(store_module.liveness, "list_managed_hosts",
                              return_value=[{"name": "corral-claude-k"}]),
            mock.patch.object(store, "_adopt_foreign_hosted"),
        ):
            store._probe_hosts([gone, kept])
        self.assertNotIn("keepalive_name", gone)
        self.assertEqual(kept["keepalive_name"], "corral-claude-k")
        # An empty listing may be a tmux timeout: keep live sessions hosted.
        live = {"source": "claude", "id": "l", "live": True, "keepalive_name": "corral-claude-l"}
        with (
            mock.patch.object(store_module.liveness, "list_managed_hosts", return_value=[]),
            mock.patch.object(store, "_adopt_foreign_hosted"),
        ):
            store._probe_hosts([live])
        self.assertEqual(live["keepalive_name"], "corral-claude-l")


class RemoteLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": tmp.name})
        env.start()
        self.addCleanup(env.stop)
        from corral import split_layout

        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)

    def _hub(self, events):
        from corral.remote.sessions import SessionHub

        hub = SessionHub(on_event=lambda channel, payload: events.append((channel, payload)),
                         scan_limit=10)
        hub._sessions_watchers = 1
        self.addCleanup(hub.stop)
        return hub

    def test_state_change_is_pushed_within_a_tick_under_memory_pressure(self) -> None:
        from corral.remote import sessions as remote_sessions

        events: list = []
        hub = self._hub(events)
        watcher = HistoryWatcher(roots=[])
        hub._history_watcher = watcher
        probes = threading.Event()

        def refresh_state(**_kwargs):
            probes.set()
            return True

        with (
            mock.patch("corral.schedprio.demote_background"),
            mock.patch.object(remote_sessions, "_STATE_TICK", 0.02),
            mock.patch.object(history_watch, "memory_pressured", return_value=True),
            mock.patch.object(hub.store, "refresh") as full_scan,
            mock.patch.object(hub.store, "refresh_state", side_effect=refresh_state),
            mock.patch.object(hub.store, "poll_title_updates", return_value=set()),
            mock.patch.object(hub, "list_snapshot", return_value={"kind": "list"}),
        ):
            watcher._changed.set()  # history moved, but the pressured scan gap is 60 s
            worker = threading.Thread(target=hub._refresh_loop, daemon=True)
            worker.start()
            try:
                self.assertTrue(probes.wait(5.0))
                deadline = time.monotonic() + 5.0
                while not events and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                hub._stop.set()
                worker.join(5.0)
        full_scan.assert_not_called()
        self.assertIn(("sessions", {"kind": "list"}), events)

    def test_new_history_file_triggers_a_scan_once_the_index_moves(self) -> None:
        from corral.remote import sessions as remote_sessions

        events: list = []
        hub = self._hub(events)
        watcher = HistoryWatcher(roots=[])
        hub._history_watcher = watcher
        stamps = iter([("old",), ("old",), ("new",)] + [("new",)] * 1000)
        scanned = threading.Event()

        def full_scan():
            scanned.set()
            return True

        with (
            mock.patch("corral.schedprio.demote_background"),
            mock.patch.object(remote_sessions, "_STATE_TICK", 0.02),
            mock.patch.object(remote_sessions, "_ARRIVAL_MIN_GAP", 0.0),
            mock.patch.object(remote_sessions, "_scan_index_stamp", side_effect=lambda: next(stamps)),
            mock.patch.object(history_watch, "memory_pressured", return_value=True),
            mock.patch.object(hub.store, "refresh", side_effect=full_scan),
            mock.patch.object(hub.store, "refresh_state", return_value=False),
            mock.patch.object(hub.store, "poll_title_updates", return_value=set()),
            mock.patch.object(hub, "_reclaim_inactive_hosts"),
            mock.patch.object(hub, "list_snapshot", return_value={"kind": "list"}),
        ):
            worker = threading.Thread(target=hub._refresh_loop, daemon=True)
            worker.start()
            try:
                time.sleep(0.1)
                self.assertFalse(scanned.is_set())
                watcher.note_created("/h/.claude/projects/p/new.jsonl")
                watcher.clear()  # only the arrival, not a generic write, drives this
                self.assertTrue(scanned.wait(5.0), "arrival never triggered a scan")
            finally:
                hub._stop.set()
                worker.join(5.0)


class ScanWorkerArrivalTests(unittest.TestCase):
    def test_arrival_skips_pressured_churn_backoff(self) -> None:
        from test_scan_worker import _AlwaysParseRegistry, _FakeTime

        from corral import scan_index, scan_worker

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache = Path(tmp.name)
        fake = _FakeTime(start=time.time())
        patches = [
            mock.patch("corral.scan_index.cache_dir", return_value=cache),
            mock.patch("corral.scan_index._shared_index_enabled", return_value=True),
            mock.patch("corral.scan_worker._worker_enabled", return_value=True),
            mock.patch("corral.cache.cache_dir", return_value=cache),
            mock.patch("corral.scan_worker._remembered_keep_ids", return_value={}),
            mock.patch("corral.scan_worker._memory_pressured", return_value=True),
            mock.patch("corral.scan_worker.time", fake),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        scan_index._LAST_PUBLISH.update({"at": 0.0, "path": "", "limit": 0, "keys": None})
        self.addCleanup(
            scan_index._LAST_PUBLISH.update, {"at": 0.0, "path": "", "limit": 0, "keys": None},
        )
        t0 = fake.now
        arrive_at = t0 + 40.0

        class _Watcher:
            def wait(self, timeout=None):
                return True  # sustained churn

            def clear(self):
                pass

            @property
            def arrival_seq(self):
                return 1 if fake.now >= arrive_at else 0

        registry = _AlwaysParseRegistry(
            lambda: {"opencode": [{"source": "opencode", "id": "a", "mtime": 1, "cwd": "/tmp"}]}
        )
        registry._clock = fake.monotonic
        code = scan_worker.run_loop(
            limit=50, interval=2.0, parent_pid=os.getppid(), max_passes=40,
            registry=registry, watcher=_Watcher(),
        )
        self.assertEqual(code, 0)
        after = [at for at in registry.parse_at if at >= arrive_at]
        self.assertTrue(after, "no parse after the arrival")
        self.assertLessEqual(after[0] - arrive_at, 2.5)


if __name__ == "__main__":
    unittest.main()
