"""Old foreign empty windows must not be mistaken for newly created sessions."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from corral import i18n, titles
from corral.runtime.registry import RuntimeRegistry
from corral.store import SessionStore


class ProvisionalOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patch in (
            mock.patch.dict("os.environ", {"CORRAL_CACHE_DIR": self.tmp.name}),
            mock.patch.object(titles, "load_cache", return_value={}),
            mock.patch("corral.liveness.annotate"),
            mock.patch("corral.liveness.list_managed_hosts", return_value=[]),
            mock.patch("corral.embed.pane_liveness", return_value="alive"),
            mock.patch.object(SessionStore, "request_title_generation"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        i18n.set_lang("en")
        self.now = time.time()
        runtime = mock.Mock(id="claude", display_name="Claude")
        self.store = SessionStore(50, registry=RuntimeRegistry((runtime,)))

    def session(self, sid, age):
        return {"source": "claude", "id": sid, "mtime": self.now - age,
                "fallback_title": "Fix the sidebar ordering", "live": False}

    def adopt(self, sid, age):
        return self.store.register_hosted_session(
            runtime_id="claude", keepalive_name=f"corral-claude-{sid}",
            title="New Claude session", cwd="/tmp", ident=sid,
            created_at=self.now - age,
        )

    def test_foreign_empty_window_uses_creation_time_and_stays_usable(self):
        self.store._merge_scanned({"claude": [self.session("recent", 60)]})
        empty = self.adopt("empty", 12 * 3600)
        self.store._merge_scanned({"claude": [self.session("recent", 60)]})
        self.assertEqual(self.store._order, ["claude:recent", "claude:empty"])
        self.assertTrue(empty["live"])
        self.assertEqual(self.store.get_title(empty), "New Claude session")
        self.assertNotIn("claude:empty", self.store.generating)
        self.adopt("new", 0)
        self.assertEqual(self.store._order[0], "claude:new")

    def test_merge_scanned_probes_liveness_outside_store_lock(self):
        # 2026-10-15 现场栈：刷新线程持 SessionStore.lock 逐会话 fork tmux
        # has-session（单次可达 1.5s），0.5s 标题轮询撞同一把锁 → TUI 卡 13~16s。
        # 锁内只许查快照，子进程探测必须在锁外。
        lock_held_during_probe = []

        def pane_probe(name):
            lock_held_during_probe.append(self.store.lock.locked())
            # One alive (short-circuit) and one unknown (falls through to
            # is_alive) so both probe types are exercised outside the lock.
            return "unknown" if name.endswith("-other") else "alive"

        def alive_probe(name):
            lock_held_during_probe.append(self.store.lock.locked())
            return True

        self.adopt("empty", 10)
        self.adopt("other", 10)
        with mock.patch("corral.embed.pane_liveness", side_effect=pane_probe), \
                mock.patch("corral.liveness.is_alive", side_effect=alive_probe):
            self.store._merge_scanned({"claude": [self.session("recent", 60)]})
        self.assertTrue(lock_held_during_probe, "expected liveness probes to run")
        self.assertFalse(
            any(lock_held_during_probe),
            "liveness probes must not run while holding the store lock",
        )
        self.assertIn("claude:empty", self.store._provisional)
        self.assertIn("claude:other", self.store._provisional)

    def test_discovered_twelve_hour_history_does_not_prepend_on_refresh(self):
        self.store._merge_scanned({"claude": [self.session("recent", 60)]})
        self.store._merge_scanned({"claude": [
            self.session("recent", 60), self.session("old", 12 * 3600),
        ]})
        self.assertEqual(self.store._order, ["claude:recent", "claude:old"])

    def test_snapshot_and_first_scan_repair_old_adoption_order(self):
        self.adopt("empty", 12 * 3600)
        recent = self.session("recent", 60)
        empty = self.store.find_session("claude:empty")
        payload = {"version": 1, "order": ["claude:empty", "claude:recent"],
                   "sessions": {"claude": [empty, recent]}}
        Path(self.store._snapshot_path()).write_text(json.dumps(payload))
        self.store.sessions = {"claude": []}
        self.store._order = []
        self.assertTrue(self.store.hydrate_from_snapshot())
        self.assertEqual(self.store._order, ["claude:recent"])
        self.store._order.reverse()  # old order can also arrive before load
        self.store._merge_scanned({"claude": [recent]})
        self.assertEqual(self.store._order, ["claude:recent", "claude:empty"])
        # Later activity updates existing cards in place.
        self.store._merge_scanned({"claude": [dict(recent, mtime=self.now - 13 * 3600)]})
        self.assertEqual(self.store._order, ["claude:recent", "claude:empty"])

    def test_snapshot_does_not_suppress_first_live_adoption(self):
        self.adopt("empty", 12 * 3600)
        self.store._save_sidebar_snapshot()
        payload = json.loads(Path(self.store._snapshot_path()).read_text())
        self.assertEqual(payload["order"], [])
        self.assertEqual(payload["sessions"]["claude"], [])
        # Compatibility with an old snapshot containing an unowned live card.
        payload["sessions"]["claude"] = [self.store.find_session("claude:empty")]
        payload["order"] = ["claude:empty"]
        Path(self.store._snapshot_path()).write_text(json.dumps(payload))
        fresh = SessionStore(50, registry=self.store.registry)
        self.assertTrue(fresh.hydrate_from_snapshot())
        self.assertEqual(fresh.all_sessions(), [])
        host = {"name": "corral-claude-empty", "runtime_id": "claude",
                "ident": "empty", "cwd": "/tmp",
                "created_at": self.now - 12 * 3600}
        with mock.patch("corral.liveness.list_managed_hosts", return_value=[host]), \
                mock.patch("corral.store.is_ephemeral_agent_cwd", return_value=False):
            fresh._merge_scanned({"claude": [self.session("recent", 60)]})
        self.assertEqual(fresh._order, ["claude:recent", "claude:empty"])
        self.assertIn("claude:empty", fresh._provisional)

    def test_provisional_cache_collision_cannot_invent_a_task(self):
        for runtime in ("claude", "codex", "opencode", "cursor", "pi"):
            with self.subTest(runtime=runtime):
                session = {"source": runtime, "id": "empty", "provisional": True,
                           "fallback_title": f"New {runtime} session"}
                cache = {f"{runtime}:empty": {"title": "Unrelated cached task"}}
                self.assertEqual(titles.resolve_initial_title(session, cache),
                                 (f"New {runtime} session", False))


if __name__ == "__main__":
    unittest.main()
