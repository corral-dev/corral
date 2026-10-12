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
        self.assertEqual(self.store._order, ["claude:recent", "claude:empty"])
        self.store._order.reverse()  # old order can also arrive before load
        self.store._merge_scanned({"claude": [recent]})
        self.assertEqual(self.store._order, ["claude:recent", "claude:empty"])
        # Later activity updates existing cards in place.
        self.store._merge_scanned({"claude": [dict(recent, mtime=self.now - 13 * 3600)]})
        self.assertEqual(self.store._order, ["claude:recent", "claude:empty"])

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
