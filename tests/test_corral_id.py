"""Tests for stateless Corral ID derivation, resolution, and TUI Copy Corral ID."""

from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from textual.widgets import ListView

import corral
from corral import agent_api, display, scan_index, scan_worker, titles
from corral.corral_id import (
    CROCKFORD_ALPHABET,
    RUNTIME_PREFIXES,
    corral_id_for_session,
    derive_corral_id,
    matches_corral_id,
    normalize_corral_id,
)
from corral.remote.sessions import SessionHub
from corral.runtime import RuntimeRegistry, default_registry
from corral.runtime.base import BaseRuntime
from corral.ui.app import CorralApp
from corral.ui.modals import COPY_CORRAL_ID_CHOICE, RuntimePickerModal


class _FakeRuntime(BaseRuntime):
    def __init__(self, runtime_id: str = "claude", display_name: str = "Claude") -> None:
        self.id = runtime_id
        self.display_name = display_name
        self.executable = runtime_id
        self.history_reading_hint = ""
        self._sessions: list[dict] = []

    def is_available(self) -> bool:
        return True

    def scan_sessions(self, limit: int, keep_ids: set[str] | None = None) -> list[dict]:
        return list(self._sessions)

    def load_conversation(self, session):
        return []

    def build_resume_plan(self, session):
        return corral.LaunchPlan((self.executable,), session.get("cwd"))

    def build_new_plan(self, handoff):
        return corral.LaunchPlan((self.executable,), handoff.original_cwd)

    def build_new_session_plan(self, cwd: str | None):
        return corral.LaunchPlan((self.executable,), cwd)


async def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not satisfied before timeout")


class CorralIDUnitTests(unittest.TestCase):
    def test_all_registered_runtimes_have_unique_two_char_prefixes(self) -> None:
        registry = default_registry()
        prefixes: dict[str, str] = {}
        for runtime in registry:
            self.assertIn(runtime.id, RUNTIME_PREFIXES)
            prefix = RUNTIME_PREFIXES[runtime.id]
            self.assertEqual(len(prefix), 2)
            self.assertTrue(prefix.isalnum() and prefix.islower())
            self.assertNotIn(prefix, prefixes, f"duplicate prefix {prefix} for {runtime.id}")
            prefixes[prefix] = runtime.id

    def test_derive_corral_id_format_and_golden_vectors(self) -> None:
        # Cross-platform deterministic vectors verified identically in Swift tests:
        # SHA-256("<runtime>:<id>") top 30 bits -> 6-char Crockford Base32.
        for rt, sid in (
            ("claude", "631fad3d-fc68-43cf-9342-7b0f33af7a65"),
            ("codex", "01a1f6f7-9984-7d01-b4be-448733344368"),
            ("opencode", "ses_e1b32bb81ffeQnwYXQn4f81uS0"),
            ("cursor", "6a994179-fc25-4eb6-aa19-bf2ff2495e29"),
            ("pi", "7463b1e8"),
            ("antigravity", "631fad3d-fc68-43cf-9342-7b0f33af7a65"),
        ):
            cid = derive_corral_id(rt, sid)
            self.assertEqual(len(cid), 8)
            self.assertEqual(cid[:2], RUNTIME_PREFIXES[rt])
            self.assertTrue(all(ch in CROCKFORD_ALPHABET for ch in cid[2:]), cid)

        # Distinct runtimes with identical native UUID get distinct prefixes and hashes.
        self.assertNotEqual(
            derive_corral_id("claude", "631fad3d-fc68-43cf-9342-7b0f33af7a65"),
            derive_corral_id("antigravity", "631fad3d-fc68-43cf-9342-7b0f33af7a65"),
        )
        # Golden exact values (must match Swift CorralIDTests).
        self.assertEqual(
            derive_corral_id("claude", "631fad3d-fc68-43cf-9342-7b0f33af7a65"),
            "ccm7k4bh",
        )
        self.assertEqual(
            derive_corral_id("codex", "01a1f6f7-9984-7d01-b4be-448733344368"),
            "cxqfmcyc",
        )
        self.assertEqual(
            derive_corral_id("opencode", "ses_e1b32bb81ffeQnwYXQn4f81uS0"),
            "oc59w4me",
        )
        self.assertEqual(
            derive_corral_id("cursor", "6a994179-fc25-4eb6-aa19-bf2ff2495e29"),
            "cuckt8av",
        )
        self.assertEqual(derive_corral_id("pi", "7463b1e8"), "pix5t1pk")
        self.assertEqual(
            derive_corral_id("antigravity", "631fad3d-fc68-43cf-9342-7b0f33af7a65"),
            "agcyqkzn",
        )

    def test_empty_shell_and_provisional_sessions_return_empty(self) -> None:
        self.assertEqual(derive_corral_id("shell", "sh-1"), "")
        self.assertEqual(derive_corral_id("claude", ""), "")
        self.assertEqual(derive_corral_id("", "abc"), "")
        self.assertEqual(
            corral_id_for_session({"source": "claude", "id": "temp-1", "provisional": True}),
            "",
        )
        self.assertEqual(
            corral_id_for_session({"source": "shell", "id": "sh-1"}),
            "",
        )

    def test_normalization_and_matching(self) -> None:
        session = {"source": "claude", "id": "631fad3d-fc68-43cf-9342-7b0f33af7a65"}
        cid = corral_id_for_session(session)
        self.assertEqual(cid, "ccm7k4bh")
        self.assertTrue(matches_corral_id(session, cid.upper()))
        self.assertTrue(matches_corral_id(session, f"{cid[:2]}-{cid[2:].upper()}"))
        # Crockford alias normalization on suffix ('i'/'l' -> '1', 'o' -> '0')
        pi_session = {"source": "pi", "id": "7463b1e8"}
        pi_cid = corral_id_for_session(pi_session)
        self.assertEqual(pi_cid, "pix5t1pk")
        aliased = pi_cid[:2] + pi_cid[2:].replace("1", "l")
        self.assertEqual(normalize_corral_id(aliased), pi_cid)
        self.assertTrue(matches_corral_id(pi_session, aliased))

    def test_display_query_filter_matches_corral_id(self) -> None:
        s1 = {
            "source": "claude",
            "id": "631fad3d-fc68-43cf-9342-7b0f33af7a65",
            "cwd": "/tmp/a",
            "cwd_display": "/tmp/a",
            "fallback_title": "First",
        }
        s2 = {
            "source": "codex",
            "id": "01a1f6f7-9984-7d01-b4be-448733344368",
            "cwd": "/tmp/b",
            "cwd_display": "/tmp/b",
            "fallback_title": "Second",
        }
        cid = corral_id_for_session(s1)
        matched = display._filter_sessions_by_query([s1, s2], cid.upper())
        self.assertEqual([item["id"] for item in matched], [s1["id"]])


class CorralIDApiAndRemoteTests(unittest.TestCase):
    def test_agent_api_exposes_and_resolves_corral_id(self) -> None:
        rt = _FakeRuntime("codex", "Codex")
        sid = "01a1f6f7-9984-7d01-b4be-448733344368"
        rt._sessions = [{
            "source": "codex",
            "id": sid,
            "short_id": sid[:8],
            "cwd": "/tmp/proj",
            "cwd_display": "/tmp/proj",
            "mtime": 100.0,
            "display_time": "just now",
            "size_kb": 1.0,
            "status_tag": "✅ 已完成",
            "live": False,
            "fallback_title": "Codex test",
        }]
        registry = RuntimeRegistry((rt,))
        expected_cid = derive_corral_id("codex", sid)

        list_res = agent_api.cmd_list(
            SimpleNamespace(
                since=None, until=None, runtime=None, limit=50, top=None,
                format="json", compact=True, status=None, cwd=None,
                live=False, keepalive=False, fields=None, fresh=True,
            ),
            registry,
        )
        self.assertEqual(list_res["data"]["sessions"][0]["corral_id"], expected_cid)

        resolved = agent_api.resolve_ref(registry, expected_cid.upper(), 50)
        self.assertEqual(resolved["id"], sid)

        search_res = agent_api.cmd_search(
            SimpleNamespace(
                keywords=[expected_cid], any=False, since=None, until=None,
                deep=False, scope="conversation", runtime=None, project=None,
                limit=50, top=None, format="json", compact=True,
                live=False, keepalive=False, fields=None, fresh=True,
            ),
            registry,
        )
        self.assertEqual(search_res["data"]["count"], 1)
        self.assertEqual(search_res["data"]["sessions"][0]["corral_id"], expected_cid)

    def test_remote_session_payload_and_search_include_corral_id(self) -> None:
        rt = _FakeRuntime("claude", "Claude")
        sid = "631fad3d-fc68-43cf-9342-7b0f33af7a65"
        session = {
            "source": "claude",
            "id": sid,
            "short_id": sid[:8],
            "cwd": "/tmp/proj",
            "cwd_display": "/tmp/proj",
            "mtime": 100.0,
            "display_time": "just now",
            "size_kb": 1.0,
            "status_tag": "✅ 已完成",
            "live": False,
            "fallback_title": "Remote test",
        }
        rt._sessions = [session]

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": tmp}, clear=False),
                mock.patch.object(scan_index, "try_consume", return_value=None),
                mock.patch.object(scan_index, "published_meta", return_value=None),
                mock.patch.object(scan_worker, "is_active", return_value=False),
            ):
                hub = SessionHub(scan_limit=10)
                try:
                    payload = hub.session_payload(session)
                    self.assertEqual(payload["corral_id"], "ccm7k4bh")
                    self.assertTrue(hub._session_matches(session, None, "ccm7k4bh"))
                    self.assertEqual(
                        hub.session_payload({**session, "provisional": True})["corral_id"],
                        "",
                    )
                finally:
                    hub.stop()


class CorralIDTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_ctrl_t_copy_corral_id_copies_to_clipboard(self) -> None:
        rt = _FakeRuntime("claude", "Claude")
        sid = "631fad3d-fc68-43cf-9342-7b0f33af7a65"
        rt._sessions = [{
            "source": "claude",
            "id": sid,
            "short_id": sid[:8],
            "cwd": "/tmp/proj",
            "cwd_display": "/tmp/proj",
            "mtime": 100.0,
            "display_time": "just now",
            "size_kb": 1.0,
            "status_tag": "✅ 已完成",
            "live": False,
            "fallback_title": "TUI Copy ID",
        }]
        store = corral.SessionStore(limit=20, registry=RuntimeRegistry((rt,)))
        with (
            mock.patch.object(titles, "load_cache", return_value={}),
            mock.patch.object(scan_index, "try_consume", return_value=None),
            mock.patch.object(scan_index, "published_meta", return_value=None),
            mock.patch.object(scan_worker, "is_active", return_value=False),
        ):
            store.load()
        app = CorralApp(store, embed_ok=True)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+t")
            await pilot.pause()
            self.assertIsInstance(app.screen, RuntimePickerModal)
            choices = app.screen._choices
            idx = next(i for i, c in enumerate(choices) if c.id == COPY_CORRAL_ID_CHOICE)
            self.assertTrue(choices[idx].available)
            self.assertEqual(choices[idx].action_text, "ccm7k4bh")
            app.screen.query_one(ListView).index = app.screen.row_for_choice(idx)
            await pilot.press("enter")
            await _wait_until(lambda: bool(app._clipboard))
            self.assertEqual(app._clipboard, "ccm7k4bh")
            self.assertEqual(type(app.screen).__name__, "MainScreen")
