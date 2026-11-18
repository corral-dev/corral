"""Hosted lifecycle journal and inventory (§7 server loss as a first-class case).

Pure record/inventory tests run on a redirected cache directory. The one
real-tmux class uses a throwaway socket name and its own socket only: it must
never list, start or kill the developer's live ``corral-keepalive`` server.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from corral import hosted_lifecycle, observe, tmux_server

EVENTS = (
    hosted_lifecycle.SERVER_STARTED,
    hosted_lifecycle.SERVER_EXIT,
    hosted_lifecycle.SERVER_LOST,
    hosted_lifecycle.SESSION_DIED,
)


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _by_event(path: Path, event: str) -> list[dict]:
    return [row for row in _records(path) if row.get("event") == event]


class CacheDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self.temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CORRAL_ISOLATE_MANAGED_HOSTS", None)
        hosted_lifecycle.reset_for_tests()
        self.addCleanup(hosted_lifecycle.reset_for_tests)

    @property
    def log(self) -> Path:
        return Path(self.temp.name) / hosted_lifecycle.LOG_NAME

    @property
    def inventory(self) -> Path:
        return Path(self.temp.name) / hosted_lifecycle.INVENTORY_NAME


class RecordTests(CacheDirTestCase):
    def test_record_writes_one_json_line_per_event(self) -> None:
        self.assertTrue(hosted_lifecycle.record(hosted_lifecycle.SERVER_STARTED, socket="s", pid=42))
        self.assertTrue(hosted_lifecycle.record(hosted_lifecycle.SERVER_EXIT, socket="s", reason="x"))
        rows = _records(self.log)
        self.assertEqual([row["event"] for row in rows], list(EVENTS[:2]))
        self.assertTrue(all(row["ts"] and row["corral_pid"] for row in rows))
        self.assertEqual(rows[0]["socket"], "s")
        self.assertIsNone(hosted_lifecycle.last_error())

    def test_failed_write_is_surfaced_not_swallowed(self) -> None:
        events_log = Path(self.temp.name) / "events.log"
        # A directory where the journal file belongs makes every append fail.
        (Path(self.temp.name) / hosted_lifecycle.LOG_NAME).mkdir()
        with mock.patch.object(observe, "EVENTS_LOG", str(events_log)), \
                mock.patch.object(observe, "EMBED_ERROR_LOG", str(Path(self.temp.name) / "embed-error.log")):
            self.assertFalse(hosted_lifecycle.record(hosted_lifecycle.SERVER_STARTED, socket="s"))
        surfaced = [row for row in _records(events_log) if row.get("name") == "error"]
        self.assertTrue(surfaced, "a failed lifecycle write must appear in events.log")
        self.assertIn("hosted-lifecycle:", str(surfaced[-1].get("where")))
        error = hosted_lifecycle.last_error()
        self.assertIsNotNone(error)
        self.assertEqual(error["event"], hosted_lifecycle.SERVER_STARTED)

    def test_record_never_raises_when_the_cache_root_is_unusable(self) -> None:
        blocker = Path(self.temp.name) / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        with mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": str(blocker)}), \
                mock.patch.object(observe, "log_exception", side_effect=AssertionError("must be quiet")):
            self.assertFalse(hosted_lifecycle.record(hosted_lifecycle.SESSION_DIED, name="n"))

    def test_journal_prunes_old_days_and_stays_bounded(self) -> None:
        old = '{"ts": "2020-01-01T00:00:00", "event": "hosted_session_died", "name": "%s"}'
        recent = '{"ts": "%s", "event": "hosted_session_died", "name": "%s"}'
        today = time.strftime("%Y-%m-%dT%H:%M:%S")
        lines = [old % f"ancient-{index}" for index in range(30_000)]
        lines.append(recent % (today, "kept"))
        self.log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertGreater(self.log.stat().st_size, hosted_lifecycle.MAX_LOG_BYTES)
        self.assertTrue(hosted_lifecycle.record(hosted_lifecycle.SERVER_EXIT, socket="s"))
        size = self.log.stat().st_size
        self.assertLess(size, hosted_lifecycle.MAX_LOG_BYTES)
        rows = _records(self.log)
        self.assertEqual([row["name"] for row in rows if "name" in row], ["kept"])
        self.assertEqual(rows[-1]["event"], hosted_lifecycle.SERVER_EXIT)

    def test_journal_keeps_a_single_day_under_the_cap(self) -> None:
        today = time.strftime("%Y-%m-%dT%H:%M:%S")
        line = '{"ts": "%s", "event": "hosted_session_died", "name": "n-%s", "pad": "%s"}'
        pad = "x" * 400
        self.log.write_text(
            "\n".join(line % (today, index, pad) for index in range(10_000)) + "\n", encoding="utf-8",
        )
        self.assertTrue(hosted_lifecycle.record(hosted_lifecycle.SERVER_EXIT, socket="s"))
        self.assertLessEqual(self.log.stat().st_size, hosted_lifecycle.MAX_LOG_BYTES)
        self.assertEqual(_records(self.log)[-1]["event"], hosted_lifecycle.SERVER_EXIT)


class ServerEventTests(CacheDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.env = {"PATH": "/usr/bin:/bin"}
        self.socket = "corral-test-lifecycle"
        self.pid = 981_234
        self._patchers = [
            mock.patch.object(tmux_server, "server_pid", side_effect=self._pid),
            mock.patch.object(tmux_server, "server_running", side_effect=lambda *a, **k: self.pid is not None),
            mock.patch.object(tmux_server, "server_report", return_value={
                "running": True, "pid": self.pid, "priority": 31, "interactive_job": True,
            }),
            mock.patch.object(hosted_lifecycle, "_uptime_seconds", return_value=120),
            mock.patch.object(hosted_lifecycle, "_tmux_version", return_value="tmux 3.5a"),
            mock.patch.object(hosted_lifecycle, "_probe_sessions", side_effect=self._probe),
            mock.patch.object(hosted_lifecycle, "_busy_names", return_value=set()),
            mock.patch.object(hosted_lifecycle, "_memory_facts", return_value={"swap_used_mb": 3072.0}),
            mock.patch.object(hosted_lifecycle, "_crash_report_facts", return_value={"count": 2}),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.live: dict[str, dict] = {}

    def _pid(self, *args, **kwargs):
        return self.pid

    def _probe(self, *args, **kwargs):
        return {name: dict(entry) for name, entry in self.live.items()}

    def _add_session(self, name: str = "corral-pi-aaaa1111") -> None:
        self.live[name] = {
            "socket": self.socket, "created_at": time.time() - 600,
            "session_activity": time.time() - 60, "window_activity": time.time() - 60,
            "cwd": "/tmp/work",
        }

    def test_started_record_carries_socket_version_pid_and_config(self) -> None:
        config = Path(self.temp.name) / "keepalive.tmux.conf"
        config.write_text("set -g status off\n", encoding="utf-8")
        hosted_lifecycle.observe_server(self.socket, config_path=str(config), env=self.env, started=True)
        rows = _by_event(self.log, hosted_lifecycle.SERVER_STARTED)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["socket"], self.socket)
        self.assertEqual(row["pid"], self.pid)
        self.assertEqual(row["tmux_version"], "tmux 3.5a")
        self.assertEqual(row["config_path"], str(config))
        self.assertEqual(len(row["config_digest"]), 12)
        self.assertTrue(row["started_by_ensure_server"])
        self.assertEqual(row["source"], "ensure_server")
        self.assertEqual(row["sessions_expected"], 0)
        self.assertEqual(row["launchd_label"], f"{tmux_server.LABEL_PREFIX}.{self.socket}")
        self.assertEqual(row["priority"], 31)
        inventory = json.loads(self.inventory.read_text(encoding="utf-8"))
        self.assertEqual(inventory["server_pid"], self.pid)
        self.assertEqual(inventory["socket"], self.socket)

    def test_same_pid_is_recorded_once(self) -> None:
        for expected in (1, 1):
            hosted_lifecycle.observe_server(self.socket, env=self.env, started=False)
            self.assertEqual(len(_by_event(self.log, hosted_lifecycle.SERVER_STARTED)), expected)
            self.assertFalse(_by_event(self.log, hosted_lifecycle.SERVER_STARTED)[0]["started_by_ensure_server"])

    def test_corral_stop_is_an_exit_not_a_loss(self) -> None:
        self._add_session()
        hosted_lifecycle.observe_server(self.socket, env=self.env, started=True)
        hosted_lifecycle.refresh_inventory(force=True, env=self.env, sockets=(self.socket,))
        snapshot = hosted_lifecycle.record_server_exit(
            self.socket, reason="corral:kill-server", initiated_by="test", env=self.env,
        )
        self.assertTrue(snapshot["socket_answers"])
        self.assertEqual(snapshot["sessions_before"], 1)
        self.pid = None  # the stop landed
        self.live.clear()
        self.assertTrue(hosted_lifecycle.confirm_server_exit(snapshot, env=self.env))
        self.pid = 999_999  # a fresh server takes over
        hosted_lifecycle.observe_server(self.socket, env=self.env, started=False)
        exits = _by_event(self.log, hosted_lifecycle.SERVER_EXIT)
        self.assertEqual([row["phase"] for row in exits], ["requested", "observed"])
        self.assertTrue(exits[0]["socket_answers"])
        self.assertFalse(exits[1]["socket_answers"])
        self.assertEqual(exits[0]["uptime_seconds"], 120)
        self.assertEqual(exits[1]["sessions_after"], 0)
        self.assertEqual(exits[0]["reason"], "corral:kill-server")
        self.assertEqual(exits[0]["initiated_by"], "test")
        self.assertFalse(_by_event(self.log, hosted_lifecycle.SERVER_LOST),
                         "a Corral-initiated stop must not be reported as a loss")
        died = _by_event(self.log, hosted_lifecycle.SESSION_DIED)
        self.assertEqual([row["name"] for row in died], ["corral-pi-aaaa1111"])
        self.assertTrue(died[0]["server_exit_recorded"])

    def test_unrecorded_loss_reports_who_was_on_the_server(self) -> None:
        self._add_session()
        hosted_lifecycle.observe_server(self.socket, env=self.env, started=True)
        hosted_lifecycle.refresh_inventory(force=True, env=self.env, sockets=(self.socket,))
        self.pid = 123_457  # replaced without any Corral-initiated stop
        hosted_lifecycle.observe_server(self.socket, env=self.env, started=False)
        lost = _by_event(self.log, hosted_lifecycle.SERVER_LOST)
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0]["last_pid"], 981_234)
        self.assertFalse(lost[0]["recorded_exit"])
        self.assertTrue(lost[0]["socket_answers"])
        self.assertEqual(lost[0]["sessions_before"], 1)
        self.assertTrue(lost[0]["last_heartbeat"])
        self.assertEqual(lost[0]["memory"], {"swap_used_mb": 3072.0})
        self.assertEqual(lost[0]["crash_reports"], {"count": 2})
        died = _by_event(self.log, hosted_lifecycle.SESSION_DIED)
        self.assertEqual(len(died), 1)
        self.assertEqual(died[0]["cause"], "server_lost")
        self.assertFalse(died[0]["server_exit_recorded"])


class InventoryTests(CacheDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.env = {"PATH": "/usr/bin:/bin"}
        self.socket = "corral-test-inventory"
        self.live: dict[str, dict] = {}
        self.busy: set = set()
        for patcher in (
            mock.patch.object(hosted_lifecycle, "_probe_sessions", side_effect=self._probe),
            mock.patch.object(hosted_lifecycle, "_busy_names", side_effect=lambda: set(self.busy)),
            mock.patch.object(tmux_server, "server_pid", return_value=4242),
            mock.patch.object(tmux_server, "server_running", return_value=True),
            mock.patch.object(tmux_server, "server_report", return_value={"priority": 31, "interactive_job": True}),
            mock.patch.object(hosted_lifecycle, "_uptime_seconds", return_value=60),
            mock.patch.object(hosted_lifecycle, "_tmux_version", return_value="tmux 3.5a"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _probe(self, *args, **kwargs):
        return {name: dict(entry) for name, entry in self.live.items()}

    def _session(self, name: str = "corral-pi-aaaa1111") -> dict:
        self.live[name] = {
            "socket": self.socket, "created_at": 1_700_000_000.0,
            "session_activity": 1_700_000_600.0, "window_activity": 1_700_000_600.0,
            "cwd": "/tmp/pane",
        }
        return {
            "keepalive_name": name, "id": "aaaa1111-full", "path": "/h/pi.jsonl",
            "cwd": "/tmp/history", "source": "pi",
        }

    def test_inventory_written_and_reloaded(self) -> None:
        sessions = [self._session()]
        self.busy = {("pi", "aaaa1111-full")}
        hosted_lifecycle.refresh_inventory(force=True, env=self.env, sockets=(self.socket,), sessions=sessions)
        written = json.loads(self.inventory.read_text(encoding="utf-8"))
        entry = written["sessions"]["corral-pi-aaaa1111"]
        self.assertEqual(entry["runtime"], "pi")
        self.assertEqual(entry["ident"], "aaaa1111")
        self.assertEqual(entry["session_id"], "aaaa1111-full")
        self.assertEqual(entry["history_path"], "/h/pi.jsonl")
        self.assertEqual(entry["cwd"], "/tmp/history")
        self.assertEqual(entry["state"], "working")
        self.assertEqual(entry["socket"], self.socket)
        self.assertEqual(entry["created_at"], hosted_lifecycle._iso(1_700_000_000.0))
        reloaded = hosted_lifecycle.read_inventory()
        self.assertEqual(reloaded, written)
        self.assertEqual(len(hosted_lifecycle.read_inventory()["sessions"]), 1)

    def test_stopped_session_is_removed_without_a_death_record(self) -> None:
        self._session()
        hosted_lifecycle.refresh_inventory(force=True, env=self.env, sockets=(self.socket,))
        self.assertTrue(hosted_lifecycle.forget_session("corral-pi-aaaa1111"))
        self.live.clear()
        hosted_lifecycle.refresh_inventory(force=True, env=self.env, sockets=(self.socket,))
        self.assertEqual(hosted_lifecycle.read_inventory()["sessions"], {})
        self.assertEqual(_by_event(self.log, hosted_lifecycle.SESSION_DIED), [])

    def test_vanished_pane_is_recorded_as_a_death(self) -> None:
        source = self._session()
        hosted_lifecycle.refresh_inventory(
            force=True, env=self.env, sockets=(self.socket,), sessions=[source],
        )
        self.live.clear()
        hosted_lifecycle.refresh_inventory(force=True, env=self.env, sockets=(self.socket,))
        died = _by_event(self.log, hosted_lifecycle.SESSION_DIED)
        self.assertEqual(len(died), 1)
        self.assertEqual(died[0]["cause"], "pane_died")
        self.assertEqual(died[0]["session_id"], "aaaa1111-full")

    def test_unreadable_socket_writes_nothing(self) -> None:
        self._session()
        hosted_lifecycle.refresh_inventory(force=True, env=self.env, sockets=(self.socket,))
        before = self.inventory.read_text(encoding="utf-8")
        with mock.patch.object(hosted_lifecycle, "_probe_sessions", return_value=None):
            hosted_lifecycle.refresh_inventory(force=True, env=self.env, sockets=(self.socket,))
        self.assertEqual(self.inventory.read_text(encoding="utf-8"), before)
        self.assertEqual(_by_event(self.log, hosted_lifecycle.SESSION_DIED), [])

    def test_refresh_is_throttled_to_one_write_per_minute(self) -> None:
        self._session()
        hosted_lifecycle.refresh_inventory(force=True, env=self.env, sockets=(self.socket,))
        first = self.inventory.stat().st_mtime_ns
        time.sleep(0.01)
        hosted_lifecycle.refresh_inventory(env=self.env, sockets=(self.socket,))
        self.assertEqual(self.inventory.stat().st_mtime_ns, first)
        hosted_lifecycle.refresh_inventory(force=True, env=self.env, sockets=(self.socket,))
        self.assertGreaterEqual(self.inventory.stat().st_mtime_ns, first)

    def test_isolated_test_resources_never_probe(self) -> None:
        with mock.patch.dict(os.environ, {"CORRAL_ISOLATE_MANAGED_HOSTS": "1"}):
            hosted_lifecycle.refresh_inventory(force=True, env=self.env, sockets=(self.socket,))
        self.assertFalse(self.inventory.exists())


class RealTmuxTests(unittest.TestCase):
    """One private socket end to end: a real server start, stop and loss."""

    def setUp(self) -> None:
        if shutil.which("tmux") is None:
            self.skipTest("tmux not installed")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self.temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CORRAL_ISOLATE_MANAGED_HOSTS", None)
        hosted_lifecycle.reset_for_tests()
        self.addCleanup(hosted_lifecycle.reset_for_tests)
        self.socket = f"corral-test-lifecycle-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        self.env = {**os.environ, "TMUX_TMPDIR": "/tmp"}
        self.addCleanup(self._kill_server)
        self.log = Path(self.temp.name) / hosted_lifecycle.LOG_NAME

    def _tmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", "-L", self.socket, *args], capture_output=True, text=True,
            env=self.env, timeout=10,
        )

    def _kill_server(self) -> None:
        self._tmux("kill-server")
        sock = Path(f"/tmp/tmux-{os.getuid()}/{self.socket}")
        if sock.exists():
            sock.unlink()

    def _start(self) -> int:
        self._tmux("-f", "/dev/null", "new-session", "-d", "-s", "corral-pi-deadbeef",
                   "-x", "80", "-y", "24", "--", "sleep", "600")
        return int(self._tmux("display-message", "-p", "#{pid}").stdout.strip())

    def test_start_stop_and_loss_are_recorded_without_touching_the_live_server(self) -> None:
        pid = self._start()
        hosted_lifecycle.observe_server(self.socket, env=self.env, started=False)
        started = _by_event(self.log, hosted_lifecycle.SERVER_STARTED)
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["pid"], pid)
        self.assertEqual(started[0]["socket"], self.socket)
        self.assertTrue(started[0]["tmux_version"].startswith("tmux "))
        self.assertEqual(started[0]["sessions_live"], 1)
        hosted_lifecycle.refresh_inventory(force=True, env=self.env, sockets=(self.socket,))
        inventory = hosted_lifecycle.read_inventory()
        self.assertEqual(inventory["server_pid"], pid)
        self.assertEqual(list(inventory["sessions"]), ["corral-pi-deadbeef"])
        self.assertTrue(inventory["sessions"]["corral-pi-deadbeef"]["cwd"].startswith("/"))

        # "We stopped it": the exit record is written before the kill and closed after.
        snapshot = hosted_lifecycle.record_server_exit(
            self.socket, reason="corral:kill-server", initiated_by="test", env=self.env,
        )
        self.assertEqual(snapshot["sessions_before"], 1)
        self.assertTrue(snapshot["socket_answers"])
        self.assertIsNotNone(snapshot["uptime_seconds"])
        self._tmux("kill-server")
        self.assertTrue(hosted_lifecycle.confirm_server_exit(snapshot, env=self.env))
        exit_rows = _by_event(self.log, hosted_lifecycle.SERVER_EXIT)
        self.assertEqual([row["phase"] for row in exit_rows], ["requested", "observed"])
        self.assertFalse(exit_rows[1]["socket_answers"])
        self.assertEqual(exit_rows[1]["sessions_after"], 0)

        # A new server with no exit record on file is the "it disappeared" case:
        # the inventory still names the sessions that were on the dead server.
        current = hosted_lifecycle.read_inventory()
        self.assertEqual(list(current["sessions"]), ["corral-pi-deadbeef"])
        self.assertEqual(current["last_exit"]["reason"], "corral:kill-server")
        current["last_exit"] = {}
        Path(self.temp.name, hosted_lifecycle.INVENTORY_NAME).write_text(
            json.dumps(current), encoding="utf-8",
        )
        new_pid = self._start()
        hosted_lifecycle.observe_server(self.socket, env=self.env, started=False)
        lost = _by_event(self.log, hosted_lifecycle.SERVER_LOST)
        self.assertEqual(len(lost), 1)
        self.assertEqual(lost[0]["last_pid"], pid)
        self.assertFalse(lost[0]["recorded_exit"])
        self.assertTrue(lost[0]["socket_answers"])
        self.assertEqual(lost[0]["new_pid"], new_pid)
        self.assertEqual(lost[0]["sessions_before"], 1)
        died = _by_event(self.log, hosted_lifecycle.SESSION_DIED)
        self.assertEqual(died[0]["cause"], "server_lost")
        self.assertEqual(died[0]["name"], "corral-pi-deadbeef")
        self.assertTrue(Path(self.temp.name, hosted_lifecycle.INVENTORY_NAME).exists())
        # Nothing above named the live product socket.
        self.assertNotIn("corral-keepalive", self.log.read_text(encoding="utf-8"))


class PassWiringTests(unittest.TestCase):
    """The background tick (``reclaim._run_pass``) writes the inventory."""

    def setUp(self) -> None:
        if shutil.which("tmux") is None:
            self.skipTest("tmux not installed")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env_patch = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self.temp.name})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        os.environ.pop("CORRAL_ISOLATE_MANAGED_HOSTS", None)
        hosted_lifecycle.reset_for_tests()
        self.addCleanup(hosted_lifecycle.reset_for_tests)
        self.socket = f"corral-test-pass-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        self.env = {**os.environ, "TMUX_TMPDIR": "/tmp"}
        self.addCleanup(self._kill_server)
        from corral import reclaim, split_layout

        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self._tmux("-f", "/dev/null", "new-session", "-d", "-s", "corral-pi-feed0001",
                   "-x", "80", "-y", "24", "--", "sleep", "600")
        self.reclaim = reclaim

    def _tmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", "-L", self.socket, *args], capture_output=True, text=True,
            env=self.env, timeout=10,
        )

    def _kill_server(self) -> None:
        self._tmux("kill-server")
        sock = Path(f"/tmp/tmux-{os.getuid()}/{self.socket}")
        if sock.exists():
            sock.unlink()

    def test_pass_records_resume_inputs_for_each_hosted_session(self) -> None:
        session = {
            "source": "pi", "id": "feed0001-full-id", "path": "/hist/feed0001.jsonl",
            "cwd": "/tmp/project", "keepalive_name": "corral-pi-feed0001",
            "status_tag": "pending",  # never retirable here: this test only reads the record
        }
        with mock.patch.object(self.reclaim, "_SOCKETS", (self.socket,)), \
                mock.patch.object(self.reclaim, "_STARTED_AT", time.monotonic() - 1000), \
                mock.patch.object(self.reclaim, "memory_pressure", return_value=False):
            self.reclaim.maybe_reclaim(lambda: [session], now=time.time())
        inventory = hosted_lifecycle.read_inventory()
        entry = inventory["sessions"]["corral-pi-feed0001"]
        self.assertEqual(entry["session_id"], "feed0001-full-id")
        self.assertEqual(entry["history_path"], "/hist/feed0001.jsonl")
        self.assertEqual(entry["cwd"], "/tmp/project")
        self.assertEqual(inventory["socket"], self.socket)

    def test_server_loss_and_reboot_record_events_without_auto_restoring(self) -> None:
        s_idle = {
            "source": "pi", "id": "idle-1111", "path": "/hist/idle.jsonl",
            "cwd": "/tmp/p1", "keepalive_name": "corral-pi-11111111", "status_tag": "done",
        }
        s_wait = {
            "source": "claude", "id": "wait-2222", "path": "/hist/wait.jsonl",
            "cwd": "/tmp/p2", "keepalive_name": "corral-claude-22222222", "status_tag": "pending",
        }
        s_work = {
            "source": "codex", "id": "work-3333", "path": "/hist/work.jsonl",
            "cwd": "/tmp/p3", "keepalive_name": "corral-codex-33333333", "status_tag": "pending",
        }
        for name in ("corral-pi-11111111", "corral-claude-22222222", "corral-codex-33333333"):
            self._tmux("new-session", "-d", "-s", name, "-x", "80", "-y", "24", "--", "sleep", "600")
        self._tmux("kill-session", "-t", "corral-pi-feed0001")
        all_sessions = [s_idle, s_wait, s_work]
        with (
            mock.patch.object(hosted_lifecycle, "_busy_names", return_value={("codex", "33333333")}),
            mock.patch.object(self.reclaim, "_SOCKETS", (self.socket,)),
            mock.patch.object(self.reclaim, "_STARTED_AT", time.monotonic() - 1000),
            mock.patch.object(self.reclaim, "memory_pressure", return_value=False),
        ):
            self.reclaim._run_pass(lambda: all_sessions, now=time.time())

        # Write a tail to keepalive-server.log, then simulate a reboot-induced loss (`kill-server` + newer boot_time).
        (Path(self.temp.name) / "keepalive-server.log").write_text("oops\nfatal test reason\n", encoding="utf-8")
        self._tmux("kill-server")
        with mock.patch.object(self.reclaim, "_SOCKETS", (self.socket,)), \
                mock.patch.object(hosted_lifecycle, "boot_time", return_value=time.time() + 10.0):
            self.reclaim._run_pass(lambda: all_sessions, now=time.time() + 120)

        lost = _by_event(hosted_lifecycle.log_path(), hosted_lifecycle.SERVER_LOST)
        self.assertEqual(len(lost), 1)
        self.assertFalse(lost[0]["recorded_exit"])
        self.assertTrue(lost[0]["machine_reboot"])
        self.assertIn("fatal test reason", lost[0].get("server_log_tail", ""))
        died = _by_event(hosted_lifecycle.log_path(), hosted_lifecycle.SESSION_DIED)
        self.assertTrue(all(item.get("cause") == "machine_reboot" for item in died))

        # 2026-11-18 ruling: pending_restores stays empty and _reconcile_hosted_lifecycle
        # never restarts tmux or auto-launches agent sessions (and drops legacy queues).
        self.assertEqual(hosted_lifecycle.pending_restores(), [])
        inv = hosted_lifecycle.read_inventory()
        inv["pending_restores"] = [{"name": "corral-pi-11111111", "session_id": "idle-1111"}]
        hosted_lifecycle._write_inventory(inv)
        self.assertEqual(hosted_lifecycle.pending_restores(), [])

        from corral import keepalive, retirement
        from corral.remote.sessions import SessionHub

        for sess in all_sessions:
            sess["keepalive_name"] = ""
        store = mock.Mock()
        store.loaded = True
        store.all_sessions.return_value = list(all_sessions)
        hub = SessionHub.__new__(SessionHub)
        hub.store = store
        with (
            mock.patch.object(keepalive, "ensure_server", side_effect=AssertionError("must not start server")),
            mock.patch.object(retirement, "restore_session", side_effect=AssertionError("must not auto-restore")),
            mock.patch.object(hosted_lifecycle, "refresh_inventory", wraps=hosted_lifecycle.refresh_inventory) as ref,
            mock.patch.dict(os.environ, {"CORRAL_ISOLATE_MANAGED_HOSTS": "0"}),
        ):
            hub._reconcile_hosted_lifecycle()

        ref.assert_called_once()
        self.assertNotIn("pending_restores", hosted_lifecycle.read_inventory())


if __name__ == "__main__":
    unittest.main()
