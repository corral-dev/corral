"""Desktop layout over the remote protocol: the Mac shares the TUI's split groups."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest import mock

from corral import split_layout
from corral.remote import protocol
from corral.remote.sessions import ActionError, SessionHub


class LayoutHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self._tmp.name}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.events: list[tuple[str, dict]] = []
        self.hub = SessionHub(scan_limit=10, on_event=lambda ch, data: self.events.append((ch, data)))
        self.hub.layout_db = split_layout.SidebarLayoutDB()
        # Keys resolve to themselves; resolution against scanned history is not under test.
        mock.patch.object(self.hub, "resolve_session_key", side_effect=lambda key: key).start()
        self.addCleanup(mock.patch.stopall)

    def test_empty_snapshot_shape(self) -> None:
        snap = self.hub.layout_snapshot()
        self.assertEqual(snap["groups"], [])
        self.assertEqual(snap["pinned_sessions"], {})
        self.assertIn("revision", snap)

    def test_set_group_creates_named_group_and_emits_event(self) -> None:
        snap = self.hub.layout_set_group("/p", ["claude:a", "codex:b"], "codex:b")
        self.assertEqual(len(snap["groups"]), 1)
        group = snap["groups"][0]
        self.assertEqual(group["members"], ["claude:a", "codex:b"])
        self.assertEqual(group["focus"], "codex:b")
        self.assertTrue(group["name"])
        self.assertEqual(self.events[-1][0], "layout")
        # Same members plus one more keeps the group identity and name (adding a pane).
        grown = self.hub.layout_set_group("/p", ["claude:a", "codex:b", "pi:c"], None)
        self.assertEqual(grown["groups"][0]["id"], group["id"])
        self.assertEqual(grown["groups"][0]["name"], group["name"])

    def test_single_member_is_rejected(self) -> None:
        with self.assertRaises(ActionError):
            self.hub.layout_set_group("/p", ["claude:a"], None)

    def test_closing_pane_below_two_members_dissolves_group(self) -> None:
        self.hub.layout_set_group("/p", ["claude:a", "codex:b"], None)
        snap = self.hub.layout_remove_session("codex:b")
        self.assertEqual(snap["groups"], [])

    def test_pin_on_group_member_pins_whole_group_like_the_tui(self) -> None:
        self.hub.layout_set_group("/p", ["claude:a", "codex:b"], None)
        snap = self.hub.layout_toggle_pin("claude:a")
        self.assertTrue(snap["groups"][0]["pinned"])
        snap = self.hub.layout_toggle_pin("codex:b")
        self.assertFalse(snap["groups"][0]["pinned"])

    def test_pin_on_independent_session_pins_only_it(self) -> None:
        snap = self.hub.layout_toggle_pin("claude:solo")
        self.assertIn("claude:solo", snap["pinned_sessions"])

    def test_collapse_round_trip(self) -> None:
        gid = self.hub.layout_set_group("/p", ["claude:a", "codex:b"], None)["groups"][0]["id"]
        self.assertTrue(self.hub.layout_set_collapsed(gid, True)["groups"][0]["collapsed"])
        self.assertFalse(self.hub.layout_set_collapsed(gid, False)["groups"][0]["collapsed"])

    def test_rename_round_trip_and_empty_restores_automatic_name(self) -> None:
        group = self.hub.layout_set_group("/p", ["claude:a", "codex:b"], None)["groups"][0]
        self.assertFalse(group["named"], "a new split has no user-given name")
        renamed = self.hub.layout_rename_group(group["id"], "  Release   check ")["groups"][0]
        self.assertEqual(renamed["name"], "Release check")
        self.assertTrue(renamed["named"])
        self.assertEqual(self.events[-1][0], "layout")
        restored = self.hub.layout_rename_group(group["id"], "")["groups"][0]
        self.assertFalse(restored["named"])
        self.assertTrue(split_layout.is_auto_group_name(restored["name"]))

    def test_tui_written_change_is_emitted_once_per_revision(self) -> None:
        self.hub._emit_layout_if_changed(force=True)
        self.events.clear()
        # The TUI writes the shared store directly (another process in real life).
        split_layout.SidebarLayoutDB().set_group("/p", ["claude:a", "codex:b"])
        self.hub._emit_layout_if_changed()
        self.hub._emit_layout_if_changed()
        layout_events = [data for channel, data in self.events if channel == "layout"]
        self.assertEqual(len(layout_events), 1)
        self.assertEqual(layout_events[0]["groups"][0]["members"], ["claude:a", "codex:b"])

    def test_phone_list_payload_still_has_no_group_field(self) -> None:
        self.hub.layout_set_group("/p", ["claude:a", "codex:b"], None)
        from test_remote_sessions import _session

        session = _session(sid="a")
        payload = self.hub.session_payload(session, self.hub._layout())
        self.assertNotIn("group", payload)


class FulltextSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        isolated = tempfile.TemporaryDirectory()
        self.addCleanup(isolated.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": isolated.name, "CORRAL_CACHE": "1"})
        env.start()
        self.addCleanup(env.stop)

    def test_body_hits_come_back_with_lines_and_spans(self) -> None:
        from test_remote_sessions import _session

        hub = SessionHub(scan_limit=10)
        session = _session(sid="body")
        hub.store.sessions = {"claude": [session]}
        from types import SimpleNamespace

        conversation = [
            SimpleNamespace(role="user", text="please migrate the relay timeout", timestamp=1.0),
            SimpleNamespace(role="assistant", text="Relay timeout migrated.", timestamp=2.0),
        ]
        with (
            mock.patch.object(hub.store, "get_conversation", return_value=conversation, create=True),
            mock.patch.object(hub.store, "get_title", return_value="Relay work"),
        ):
            result = hub.fulltext_search("timeout")
        self.assertEqual(result["total"], 1)
        match = result["matches"][0]
        self.assertEqual(match["key"], "claude:body")
        self.assertTrue(match["lines"])
        self.assertIn("timeout", match["lines"][0]["text"].lower())
        self.assertTrue(match["lines"][0]["spans"])

    def test_fulltext_is_readonly(self) -> None:
        from corral.remote.service import _READONLY_METHODS

        self.assertIn(protocol.M_SEARCH_FULLTEXT, _READONLY_METHODS)

    def test_warm_search_returns_during_slow_incremental_parse_then_updates(self) -> None:
        from test_remote_sessions import _session

        from corral.models import ConversationMessage

        hub = SessionHub(scan_limit=10)
        session = _session(sid="incremental")
        hub.store.sessions = {"claude": [session]}
        parsing, release, published, returned = (threading.Event() for _ in range(4))
        index = hub._search_index()
        original_refresh = index.refresh

        def slow_read(_session):
            parsing.set()
            if not release.wait(5):
                raise TimeoutError("test did not release parsing")
            return [ConversationMessage("user", "new searchable message", 2)]

        def refresh(*args):
            result = original_refresh(*args)
            published.set()
            return result

        def cleanup():
            hub._stop.set()
            hub._search_refresh.set()
            release.set()
            for thread in hub._threads:
                thread.join(5)

        self.addCleanup(cleanup)
        with mock.patch.object(hub.store, "get_title", return_value="Title"):
            with mock.patch.object(hub.store, "get_conversation", return_value=[
                ConversationMessage("user", "old searchable message", 1),
            ]):
                self.assertEqual(hub.fulltext_search("old")["total"], 1)
            session["file_mtime"] = (session.get("file_mtime") or 0) + 1
            with (mock.patch.object(hub.store, "get_conversation", side_effect=slow_read),
                  mock.patch.object(index, "refresh", side_effect=refresh)):
                worker = threading.Thread(target=hub._search_loop, daemon=True)
                hub._threads = [worker]
                worker.start()
                self.assertTrue(parsing.wait(3))
                results = []

                def query():
                    results.append(hub.fulltext_search("old"))
                    returned.set()

                request = threading.Thread(target=query, daemon=True)
                request.start()
                try:
                    self.assertTrue(returned.wait(1), "search blocked behind history parsing")
                    self.assertEqual(results[0]["total"], 1)
                finally:
                    release.set()
                    request.join(5)
                self.assertTrue(published.wait(3))
                self.assertEqual(hub.fulltext_search("new")["total"], 1)
                self.assertEqual(hub.fulltext_search("old")["total"], 0)
                cleanup()


class TerminalTypingEchoTests(unittest.TestCase):
    def test_unsubmitted_text_is_not_echoed_as_a_user_message(self) -> None:
        from test_remote_sessions import _session

        from corral.embed import InjectionResult
        from corral.remote import sessions as remote_sessions

        events: list[tuple[str, dict]] = []
        hub = SessionHub(scan_limit=10, on_event=lambda ch, data: events.append((ch, data)))
        session = _session(sid="typing")
        session["keepalive_name"] = "pane-typing"
        hub.store.sessions = {"claude": [session]}
        with (
            mock.patch.object(remote_sessions.embed, "paste_detailed", return_value=InjectionResult(True)),
            mock.patch.object(remote_sessions.embed, "send_key_detailed", return_value=InjectionResult(True)),
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            hub.send_text("claude:typing", "half typed", False)
            self.assertFalse([data for _, data in events if data.get("kind") == "echo"])
            hub.send_text("claude:typing", "sent turn", True)
        echoes = [data for _, data in events if data.get("kind") == "echo"]
        self.assertEqual([echo["text"] for echo in echoes], ["sent turn"])


class TerminalTypingLimitTests(unittest.TestCase):
    def test_terminal_typing_has_its_own_budget(self) -> None:
        from corral.remote import ratelimit

        self.assertGreaterEqual(ratelimit.TERMINAL_TYPING.allow, 600)
        self.assertEqual(ratelimit.INPUT_ACTIONS.allow, 120)


class LayoutProtocolTests(unittest.TestCase):
    def test_desktop_layout_methods_and_channel_are_declared(self) -> None:
        self.assertEqual(protocol.CH_LAYOUT, "layout")
        self.assertEqual(protocol.CAPABILITY_DESKTOP_LAYOUT, "desktop_layout")
        for name in (
            protocol.M_LAYOUT_WATCH,
            protocol.M_LAYOUT_SET_GROUP,
            protocol.M_LAYOUT_REMOVE,
            protocol.M_LAYOUT_PIN,
            protocol.M_LAYOUT_PIN_GROUP,
            protocol.M_LAYOUT_COLLAPSE,
            protocol.M_LAYOUT_RENAME_GROUP,
        ):
            self.assertTrue(name.startswith("layout."))

    def test_mutations_are_not_readonly_methods(self) -> None:
        from corral.remote.service import _READONLY_METHODS

        self.assertIn(protocol.M_LAYOUT_WATCH, _READONLY_METHODS)
        self.assertNotIn(protocol.M_LAYOUT_SET_GROUP, _READONLY_METHODS)
        self.assertNotIn(protocol.M_LAYOUT_PIN, _READONLY_METHODS)
        self.assertNotIn(protocol.M_LAYOUT_RENAME_GROUP, _READONLY_METHODS)


if __name__ == "__main__":
    unittest.main()
