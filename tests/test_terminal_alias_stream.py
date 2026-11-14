"""Hosted aliases share ordered bytes without colliding size/lifecycle ownership."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from corral import embed
from corral.remote.sessions import SessionHub
from corral.remote.terminal_stream import TerminalStream


class TerminalAliasStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": temporary.name,
                                          "CORRAL_ISOLATE_MANAGED_HOSTS": "1"})
        env.start()
        self.addCleanup(env.stop)
        with embed._VIEWER_LOCK:
            embed._viewer_last_write.clear()
        self.hub = SessionHub(scan_limit=1)
        self.addCleanup(self.hub.stop)
        self.events = []
        self.hub._on_event = lambda channel, payload: self.events.append((channel, payload))
        for target, replacement in (
            ("_keepalive_name", lambda key, **kw: "fixture-alias-pane"),
            ("_hosted_name", lambda key: "fixture-alias-pane"),
        ):
            patch = mock.patch.object(self.hub, target, new=replacement)
            patch.start()
            self.addCleanup(patch.stop)
        for target in ("start", "stop", "_apply_size"):
            patch = mock.patch.object(TerminalStream, target)
            result = patch.start()
            setattr(self, target, result)
            self.addCleanup(patch.stop)

    def _attach_aliases(self, runtime: str = "antigravity"):
        old, new = f"{runtime}:temporary", f"{runtime}:canonical"
        self.hub.terminal_attach(old, "remote:mac", 85, 61, vote=True, active=False)
        self.hub.terminal_attach(new, "remote:mac", 77, 61, vote=True, active=True)
        return old, new, self.hub._terminals[new][0]

    def test_aliases_share_one_worker_and_stale_renewals_cannot_overwrite_active_grid(self):
        for runtime in ("antigravity", "cursor", "pi", "codex", "claude", "opencode"):
            with self.subTest(runtime=runtime):
                old, new, stream = self._attach_aliases(runtime)
                self.assertIs(self.hub._terminals[old][0], stream)
                for _ in range(4):
                    self.hub.terminal_resize(old, "remote:mac", 85, 61, vote=True, active=False)
                    self.assertEqual(stream._effective_size(stream._vote_snapshot()), (77, 61))
                self.hub.terminal_detach(old, "remote:mac")
                self.hub.terminal_detach(new, "remote:mac")
        self.assertEqual(self.start.call_count, 6)

    def test_ordered_events_fan_out_and_detach_keeps_the_other_alias_alive(self):
        old, new, stream = self._attach_aliases()
        payload = {"kind": "output", "seq": 3, "data": "eA=="}
        stream._publish(payload)
        self.assertEqual(self.events, [(f"term:{old}", payload), (f"term:{new}", payload)])
        self.hub.terminal_detach(old, "remote:mac")
        self.assertNotIn(old, self.hub._terminals)
        self.stop.assert_not_called()
        self.events.clear()
        stream._publish(payload)
        self.assertEqual(self.events, [(f"term:{new}", payload)])
        self.assertEqual(len(stream._vote_snapshot()), 1)
        self.hub.terminal_detach(new, "remote:mac")
        self.stop.assert_called_once()

    def test_readonly_alias_does_not_vote_or_stop_the_full_access_viewer(self):
        old, new, stream = self._attach_aliases()
        with mock.patch.object(embed, "pane_size", return_value=(77, 61)):
            self.hub.terminal_attach("antigravity:readonly", "probe", 80, 24, vote=False)
        self.assertEqual(len(stream._vote_snapshot()), 2)
        self.hub.terminal_detach("antigravity:readonly", "probe")
        self.stop.assert_not_called()
        self.assertEqual(stream._effective_size(stream._vote_snapshot()), (77, 61))

    def test_input_and_paste_activate_only_the_requested_attachment(self):
        old, new, stream = self._attach_aliases()
        self.hub.terminal_used(old, "remote:mac")
        self.assertEqual(stream._effective_size(stream._vote_snapshot()), (85, 61))
        with mock.patch.object(embed, "send_bytes", return_value=True):
            self.hub.terminal_input(new, b"x", "remote:mac")
        self.assertEqual(stream._effective_size(stream._vote_snapshot()), (77, 61))
        with mock.patch.object(embed, "send_bytes", return_value=True):
            self.hub.terminal_input(old, b"\x1b[I", "remote:mac", passive=True)
        self.assertEqual(stream._effective_size(stream._vote_snapshot()), (77, 61))
