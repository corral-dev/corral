from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import split_layout, titles
from corral.embed import InjectionResult
from corral.remote import sessions as remote_sessions
from corral.remote.richmsg import _pi_build_messages
from corral.remote.sessions import SessionHub
from corral.runtime import default_registry
from corral.skill_invocation import (
    format_skill_invocation,
    parse_skill_invocation,
    skill_invocation_display_text,
)


class PortableSkillInvocationTests(unittest.TestCase):
    def test_all_active_runtime_adapters_use_their_native_form(self) -> None:
        expected = {
            "claude": "/swarm-manager 修复标题",
            "codex": "$swarm-manager 修复标题",
            "opencode": "@swarm-manager 修复标题",
            "cursor": "/swarm-manager 修复标题",
            "pi": "/skill:swarm-manager 修复标题",
            "antigravity": "/swarm-manager 修复标题",
        }
        registry = default_registry()
        for runtime_id, native in expected.items():
            with self.subTest(runtime=runtime_id):
                runtime = registry.get(runtime_id)
                self.assertEqual(
                    runtime.prepare_user_prompt("/skill:swarm-manager 修复标题"),
                    native,
                )
                self.assertEqual(
                    runtime.user_prompt_display_text("/skill:swarm-manager 修复标题"),
                    "修复标题" if runtime_id in {"claude", "pi"} else native,
                )

    def test_argument_bytes_and_no_argument_form_are_preserved(self) -> None:
        source = "/skill:swarm-manager\n  第一行\n\t第二行"
        self.assertEqual(
            format_skill_invocation(source, "dollar"),
            "$swarm-manager\n  第一行\n\t第二行",
        )
        self.assertEqual(skill_invocation_display_text(source), "第一行\n\t第二行")
        self.assertEqual(format_skill_invocation("/skill:doc-compact   ", "slash"), "/doc-compact")
        self.assertEqual(skill_invocation_display_text("/skill:doc-compact"), "/skill:doc-compact")

    def test_unrelated_or_malformed_text_is_never_rewritten(self) -> None:
        cases = (
            "/compact",
            "$doc-update",
            "@src/main.py",
            " /skill:swarm-manager 修复",
            "/skill:",
            "/skill:Uppercase 修复",
            "/skill:-leading 修复",
            "/skill:double--hyphen 修复",
            "/skill:plugin:name 修复",
            "/skill:" + "a" * 65 + " 修复",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(parse_skill_invocation(text))
                self.assertEqual(format_skill_invocation(text, "slash"), text)
                self.assertEqual(skill_invocation_display_text(text), text)

    def test_title_input_uses_task_not_the_portable_command(self) -> None:
        session = {
            "source": "pi",
            "id": "skill-title",
            "first_user_msg": "/skill:swarm-manager 适配 skill 标题和消息",
            "last_user_msg": "/skill:swarm-manager 适配 skill 标题和消息",
            "fallback_title": "/skill:swarm-manager 适配 skill 标题和消息",
            "native_title": None,
        }
        self.assertEqual(titles._compact_title(session["first_user_msg"]), "适配 skill 标题和消息")
        self.assertEqual(titles._prompt_item(session)["user_request"], "适配 skill 标题和消息")
        title, needs = titles.resolve_initial_title(session, {})
        self.assertEqual(title, "适配 skill 标题和消息")
        self.assertTrue(needs)


    def test_cached_skill_wrappers_cannot_survive_as_titles(self) -> None:
        for runtime in ("claude", "codex", "opencode", "cursor", "pi", "antigravity"):
            for wrapper in ('<skill name="review"…', '<skill>', '<command-message>review'):
                with self.subTest(runtime=runtime, wrapper=wrapper):
                    session = {
                        "source": runtime, "id": "cached-skill",
                        "first_user_msg": "修复用户消息和会话标题",
                        "fallback_title": "修复用户消息和会话标题",
                    }
                    cache = {f"{runtime}:cached-skill": {"title": wrapper}}
                    self.assertEqual(
                        titles.resolve_initial_title(session, cache),
                        ("修复用户消息和会话标题", True),
                    )


class RemoteSkillInvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"CORRAL_CACHE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.hub = SessionHub(scan_limit=10)
        prepare = mock.patch.object(self.hub.store, "prepare_search")
        prepare.start()
        self.addCleanup(prepare.stop)
        self.hub.layout_db = split_layout.SidebarLayoutDB()
        self.addCleanup(self.hub.layout_db.close)

    def tearDown(self) -> None:
        self.hub.stop()

    def _session(self, runtime: str) -> dict:
        return {
            "source": runtime,
            "id": "a",
            "short_id": "a",
            "cwd": "/tmp/proj",
            "cwd_display": "/tmp/proj",
            "mtime": 1_700_000_000.0,
            "display_time": "01-01 12:00",
            "size_kb": 1.5,
            "status_tag": "ended",
            "live": True,
            "keepalive_name": "pane-a",
            "fallback_title": "示例会话",
            "attention_kind": "none",
            "last_user_msg": "你好",
            "last_agent_msg": "好的",
            "path": "/tmp/hist.jsonl",
        }

    def test_delivery_uses_native_command_but_echo_uses_task(self) -> None:
        events: list[tuple[str, dict]] = []
        self.hub._on_event = lambda channel, data: events.append((channel, data))
        self.hub.store.sessions = {"claude": [self._session("claude")]}
        with (
            mock.patch.object(
                remote_sessions.embed, "paste_detailed", return_value=InjectionResult(True)
            ) as paste,
            mock.patch.object(
                remote_sessions.embed, "send_key_detailed", return_value=InjectionResult(True)
            ),
            mock.patch.object(remote_sessions.time, "sleep"),
        ):
            self.hub.send_text("claude:a", "/skill:swarm-manager 修复标题")
        paste.assert_called_once_with("pane-a", "/swarm-manager 修复标题")
        echoes = [data for _channel, data in events if data.get("kind") == "echo"]
        self.assertEqual([item["text"] for item in echoes], ["修复标题"])

    def test_busy_opencode_api_receives_mention_and_echo_receives_task(self) -> None:
        events: list[tuple[str, dict]] = []
        self.hub._on_event = lambda channel, data: events.append((channel, data))
        self.hub.store.sessions = {"opencode": [self._session("opencode")]}
        with (
            mock.patch.object(remote_sessions, "_opencode_api_steer_applies", return_value=True),
            mock.patch.object(self.hub, "_steer_opencode_via_api") as steer,
            mock.patch.object(self.hub, "_emit_provisional_working"),
        ):
            self.hub.send_text("opencode:a", "/skill:swarm-manager 修复标题")
        steer.assert_called_once_with("opencode:a", mock.ANY, "@swarm-manager 修复标题")
        echoes = [data for _channel, data in events if data.get("kind") == "echo"]
        self.assertEqual([item["text"] for item in echoes], ["@swarm-manager 修复标题"])

    def test_unsubmitted_terminal_typing_is_not_translated_or_echoed(self) -> None:
        events: list[tuple[str, dict]] = []
        self.hub._on_event = lambda channel, data: events.append((channel, data))
        self.hub.store.sessions = {"codex": [self._session("codex")]}
        with mock.patch.object(
            remote_sessions.embed, "paste_detailed", return_value=InjectionResult(True)
        ) as paste:
            self.hub.send_text("codex:a", "/skill:swarm-manager 修复标题", submit=False)
        paste.assert_called_once_with("pane-a", "/skill:swarm-manager 修复标题")
        self.assertEqual(events, [])

    def test_legacy_pi_history_projection_delegates_to_sesskit_user_text(self) -> None:
        skill_text = (
            '<skill name="swarm-manager" location="/tmp/swarm-manager/SKILL.md">\n'
            "References are relative to /tmp/swarm-manager.\n\nInstructions\n"
            "</skill>\n\n修复标题和用户消息"
        )
        path = Path(self._tmp.name) / "pi-skill.jsonl"
        rows = [
            {"type": "session", "id": "pi-skill", "timestamp": "2026-11-16T00:00:00Z", "cwd": "/tmp"},
            {
                "type": "message", "id": "u1", "parentId": None,
                "timestamp": "2026-11-16T00:00:01Z",
                "message": {"role": "user", "content": [{"type": "text", "text": skill_text}]},
            },
            {
                "type": "message", "id": "a1", "parentId": "u1",
                "timestamp": "2026-11-16T00:00:02Z",
                "message": {
                    "role": "assistant", "stopReason": "stop",
                    "content": [{"type": "text", "text": "已处理"}],
                },
            },
        ]
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        messages = _pi_build_messages(str(path))
        self.assertEqual([message.text for message in messages], ["修复标题和用户消息", "已处理"])


if __name__ == "__main__":
    unittest.main()
