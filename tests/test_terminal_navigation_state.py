"""Native navigation ownership must not corrupt another client's input."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from corral import embed
from corral import terminal_navigation_state as state


class NavigationOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patch = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": temporary.name})
        patch.start()
        self.addCleanup(patch.stop)

    def test_another_process_cancels_before_ordinary_input_acquires_lock(self):
        code = """
import time
from corral import embed
with embed.navigation_transaction('isolated-navigation-test') as current:
 print('ready', flush=True)
 deadline=time.monotonic()+3
 while current() and time.monotonic()<deadline:
  time.sleep(.01)
 print('cancelled' if not current() else 'timed-out', flush=True)
"""
        process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        self.assertEqual(process.stdout.readline().strip(), "ready")
        self.assertTrue(state.frame_state("isolated-navigation-test").active)
        started = time.monotonic()
        embed.cancel_navigation("isolated-navigation-test")
        with embed.input_transaction("isolated-navigation-test"):
            self.assertFalse(state.frame_state("isolated-navigation-test").active)
        output, errors = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, errors)
        self.assertEqual(output.strip(), "cancelled")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_dead_or_expired_owner_never_leaves_a_frozen_frame(self):
        code = "from corral.terminal_navigation_state import publish; publish('dead-owner', active=True)"
        subprocess.run([sys.executable, "-c", code], check=True)
        frame = state.frame_state("dead-owner")
        self.assertTrue(frame.revision)
        self.assertFalse(frame.active)
        state.publish("expired-owner", active=True)
        path = state._path("expired-owner", ".json")
        data = json.loads(path.read_text())
        data["started"] = time.time() - 11
        path.write_text(json.dumps(data))
        self.assertFalse(state.frame_state("expired-owner").active)

    def test_unwritable_optional_metadata_does_not_block_ordinary_input(self):
        @embed._serialized_input
        def ordinary_input(_name, data):
            return data

        with mock.patch.object(Path, "mkdir", side_effect=PermissionError("read-only metadata")):
            self.assertEqual(ordinary_input("unwritable-metadata", b"draft\r"), b"draft\r")
            with self.assertRaises(PermissionError):
                with embed.navigation_transaction("unwritable-native-metadata"):
                    self.fail("Native navigation needs process ownership")

    def test_metadata_never_contains_session_prompt_content(self):
        state.publish("metadata-only", active=True)
        payload = json.loads(state._path("metadata-only", ".json").read_text())
        self.assertEqual(set(payload), {"revision", "active", "pid", "started"})
        state.publish("metadata-only", active=False)
        self.assertFalse(state.frame_state("metadata-only").active)
