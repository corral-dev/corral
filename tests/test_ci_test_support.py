"""Mock-only tests for tests/ci_test_support.py (no UI, no real tmux)."""

from __future__ import annotations

import os
import unittest
from unittest import mock

import ci_test_support
from ci_test_support import (
    ShardResources,
    isolated_test_resources,
    private_tmux,
    unique_socket_name,
)


class SocketNameTests(unittest.TestCase):
    def test_names_are_unique_and_scoped(self) -> None:
        names = {unique_socket_name() for _ in range(50)}
        self.assertEqual(len(names), 50)
        for name in names:
            self.assertTrue(name.startswith("corral-ci-"))
            self.assertIn(str(os.getpid()), name)
            self.assertNotIn("corral-keepalive", name)


class IsolationEnvTests(unittest.TestCase):
    def test_env_set_inside_and_restored_after(self) -> None:
        prev_cache = os.environ.get("CORRAL_CACHE_DIR")
        prev_iso = os.environ.get("CORRAL_ISOLATE_MANAGED_HOSTS")
        with mock.patch.object(
            ci_test_support.subprocess,
            "run",
            return_value=mock.Mock(returncode=0),
        ):
            with isolated_test_resources() as res:
                self.assertIsInstance(res, ShardResources)
                self.assertTrue(os.path.isdir(res.fixture_root))
                self.assertEqual(os.environ["CORRAL_CACHE_DIR"], res.fixture_root)
                self.assertEqual(os.environ["CORRAL_ISOLATE_MANAGED_HOSTS"], "1")
                self.assertEqual(os.environ["CORRAL_CI_SHARD"], "1")
        self.assertEqual(os.environ.get("CORRAL_CACHE_DIR"), prev_cache)
        self.assertEqual(os.environ.get("CORRAL_ISOLATE_MANAGED_HOSTS"), prev_iso)
        self.assertIsNone(os.environ.get("CORRAL_CI_SHARD"))
        self.assertFalse(os.path.exists(res.fixture_root))

    def test_exception_still_cleans_up(self) -> None:
        with mock.patch.object(
            ci_test_support.subprocess,
            "run",
            return_value=mock.Mock(returncode=0),
        ):
            with self.assertRaises(RuntimeError):
                with isolated_test_resources() as res:
                    raise RuntimeError("boom")
        self.assertIsNone(os.environ.get("CORRAL_CI_SHARD"))
        self.assertFalse(os.path.exists(res.fixture_root))


class RoutingPatchTests(unittest.TestCase):
    def test_tmux_argv_routed_inside_only(self) -> None:
        from corral import keepalive

        orig = keepalive.tmux_argv()
        self.assertIn("corral-keepalive", orig)
        with mock.patch.object(
            ci_test_support.subprocess,
            "run",
            return_value=mock.Mock(returncode=0),
        ):
            with isolated_test_resources() as res:
                routed = keepalive.tmux_argv()
                self.assertEqual(tuple(routed), res.tmux_base_argv)
                self.assertIn(res.tmux_socket, routed)
                # Named lookups route to the private socket as well.
                named = keepalive.tmux_argv("corral-claude-s0")
                self.assertIn(res.tmux_socket, named)
                # Pressure janitor is a no-op under isolation.
                self.assertEqual(keepalive.reap_pressure(), [])
        self.assertEqual(tuple(keepalive.tmux_argv()), tuple(orig))

    def test_teardown_kills_only_private_socket(self) -> None:
        calls: list[tuple] = []

        def fake_run(argv, **kwargs):
            calls.append(tuple(argv))
            return mock.Mock(returncode=0)

        with mock.patch.object(ci_test_support.subprocess, "run", side_effect=fake_run):
            with isolated_test_resources() as res:
                pass
        kill_calls = [c for c in calls if "kill-server" in c]
        self.assertTrue(kill_calls, "teardown must kill the private server")
        for call in kill_calls:
            self.assertIn(res.tmux_socket, call)
            joined = " ".join(call)
            self.assertNotIn("corral-keepalive", joined)
            self.assertNotIn("pickup-keepalive", joined)

    def test_private_tmux_alias_yields_same_shape(self) -> None:
        with mock.patch.object(
            ci_test_support.subprocess,
            "run",
            return_value=mock.Mock(returncode=0),
        ):
            with private_tmux(prefix="corral-ci-probe") as res:
                self.assertTrue(res.tmux_socket.startswith("corral-ci-probe-"))
                self.assertEqual(os.environ["CORRAL_ISOLATE_MANAGED_HOSTS"], "1")
        self.assertIsNone(os.environ.get("CORRAL_CI_SHARD"))


if __name__ == "__main__":
    unittest.main()
