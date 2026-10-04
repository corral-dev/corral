"""corral.remote.service：会话动作路由与未配对拒绝。"""

from __future__ import annotations

import os
import tempfile
import unittest

from corral.remote import protocol, ratelimit
from corral.remote.service import Connection, RemoteService


class FakeHub:
    """只记录被调了什么，不做任何真事。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.prompts_result: list[dict] = []

    def _record(self, name: str, *args) -> None:
        self.calls.append((name, args))

    def runtimes(self):
        return [{"id": "codex", "name": "Codex", "available": True}]

    def list_sessions(self, query: str = "", limit: int = 0):
        return [{"key": "codex:abc", "title": "示例"}]

    def watch_sessions(self):
        pass

    def unwatch_sessions(self):
        pass

    def watch_conversation(
        self, key: str, *, limit: int = 80, after_seq=None, generation=None
    ):
        return {
            "version": 1,
            "kind": "snapshot",
            "messages": [],
            "oldest_seq": 0,
            "newest_seq": 0,
            "has_more": False,
            "resume": "tail",
            "generation": 1,
        }

    def conversation_snapshot(self, key: str):
        return []

    def conversation_page(self, key: str, *, limit: int = 80, before_seq: int | None = None):
        return {
            "version": 1,
            "kind": "snapshot",
            "messages": [],
            "oldest_seq": 0,
            "newest_seq": 0,
            "has_more": False,
        }

    def message_page(self, key: str, *, limit: int = 80, before_seq: int | None = None):
        return self.conversation_page(key, limit=limit, before_seq=before_seq)

    def unwatch_conversation(self, key: str):
        pass

    def watch_screen(self, key: str):
        return None

    def resync_screen(self, key: str):
        self._record("resync_screen", key)
        return {"cols": 80, "rows": 24, "full": True, "status": "resync", "lines": []}

    def unwatch_screen(self, key: str):
        pass

    def send_text(self, key: str, text: str, submit: bool):
        self._record("send_text", key, text, submit)

    def send_keys(self, key: str, keys):
        self._record("send_keys", key, tuple(keys))

    def send_image(self, key: str, image_bytes: bytes) -> str:
        self._record("send_image", key, len(image_bytes))
        return "/tmp/paste.jpg"

    def projects(self):
        return [
            {
                "cwd": "/Codes/demo",
                "path": "/Codes/demo",
                "label": "demo",
                "name": "demo",
                "count": 2,
                "mtime": 9.0,
            }
        ]

    def mark_read(self, key: str) -> str:
        self._record("mark_read", key)
        return "none"

    def toggle_pin(self, key: str) -> bool:
        self._record("toggle_pin", key)
        return True

    def stop_session(self, key: str) -> None:
        self._record("stop_session", key)

    def delete_session(self, key: str) -> None:
        self._record("delete_session", key)

    def new_session(self, runtime_id: str, cwd: str | None, *, whitelist=None):
        self._record("new_session", runtime_id, cwd)
        return {"key": "codex:new", "title": "新会话"}

    def resume_session(self, key: str):
        self._record("resume_session", key)
        return {"key": key, "title": "已恢复"}

    def handoff_session(self, key: str, target_runtime_id: str):
        self._record("handoff_session", key, target_runtime_id)
        return {"key": "claude:new", "title": "接力"}

    def copy_session(self, key: str):
        self._record("copy_session", key)
        return {"key": key + ":copy", "title": "副本"}

    def prompts(self, key: str) -> list[dict]:
        self._record("prompts", key)
        return list(self.prompts_result)


class RemoteActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cache = os.environ.get("CORRAL_CACHE_DIR")
        os.environ["CORRAL_CACHE_DIR"] = self._tmp.name
        self.addCleanup(self._restore_cache)
        ratelimit.PAIR_ATTEMPTS.reset()
        ratelimit.PAIR_ATTEMPTS_HOURLY.reset()
        ratelimit.INPUT_ACTIONS.reset()
        ratelimit.TERMINAL_TYPING.reset()
        ratelimit.SESSION_CREATE.reset()
        ratelimit.PUSH_REGISTER.reset()

        self.hub = FakeHub()
        self.service = RemoteService(self.hub)  # type: ignore[arg-type]
        self.sent: list[dict] = []

    def _restore_cache(self) -> None:
        if self._old_cache is None:
            os.environ.pop("CORRAL_CACHE_DIR", None)
        else:
            os.environ["CORRAL_CACHE_DIR"] = self._old_cache

    def _connect(self, public_key: str = "aa" * 32) -> Connection:
        connection = Connection(public_key, self.sent.append)
        self.service.attach(connection)
        return connection

    def _pair(self, connection: Connection | None = None) -> Connection:
        code = self.service.begin_pairing()
        connection = connection or self._connect()
        self.service.handle(connection, protocol.request(1, protocol.M_PAIR, {"code": code}))
        return connection

    def _call(self, connection: Connection, method: str, params: dict | None = None) -> dict:
        self.sent.clear()
        self.service.handle(connection, protocol.request(2, method, params or {}))
        self.assertEqual(len(self.sent), 1)
        return self.sent[0]

    def test_unpaired_session_actions_are_rejected(self) -> None:
        connection = self._connect()
        for method in (
            protocol.M_SESSION_NEW,
            protocol.M_SESSION_RESUME,
            protocol.M_SESSION_HANDOFF,
            protocol.M_SESSION_COPY,
            protocol.M_SESSION_STOP,
            protocol.M_SESSION_DELETE,
            protocol.M_SESSION_PIN,
            protocol.M_SESSION_MARK_READ,
            protocol.M_SESSION_PROMPTS,
        ):
            with self.subTest(method=method):
                reply = self._call(connection, method, {"key": "codex:abc", "runtime": "claude"})
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["e"]["code"], protocol.E_UNAUTHORIZED)
        self.assertEqual(self.hub.calls, [])

    def test_new_session(self) -> None:
        connection = self._pair()
        # cwd 必须是已知项目路径（FakeHub.projects 里是 /Codes/demo）
        reply = self._call(
            connection, protocol.M_SESSION_NEW, {"runtime": "codex", "cwd": "/Codes/demo"}
        )
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["d"]["session"]["key"], "codex:new")
        self.assertEqual(self.hub.calls, [("new_session", ("codex", "/Codes/demo"))])

    def test_resume_session(self) -> None:
        connection = self._pair()
        reply = self._call(connection, protocol.M_SESSION_RESUME, {"key": "codex:abc"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["session"]["title"], "已恢复")
        self.assertIn(("resume_session", ("codex:abc",)), self.hub.calls)

    def test_handoff_session(self) -> None:
        connection = self._pair()
        reply = self._call(
            connection,
            protocol.M_SESSION_HANDOFF,
            {"key": "codex:abc", "runtime": "claude"},
        )
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["session"]["key"], "claude:new")
        self.assertIn(("handoff_session", ("codex:abc", "claude")), self.hub.calls)

    def test_copy_session(self) -> None:
        connection = self._pair()
        reply = self._call(connection, protocol.M_SESSION_COPY, {"key": "codex:abc"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["session"]["key"], "codex:abc:copy")
        self.assertIn(("copy_session", ("codex:abc",)), self.hub.calls)

    def test_stop_and_delete(self) -> None:
        connection = self._pair()
        stop = self._call(
            connection, protocol.M_SESSION_STOP, {"key": "codex:abc", "confirm": True}
        )
        delete = self._call(
            connection, protocol.M_SESSION_DELETE, {"key": "codex:abc", "confirm": True}
        )
        self.assertTrue(stop["ok"], stop)
        self.assertTrue(delete["ok"], delete)
        self.assertIn(("stop_session", ("codex:abc",)), self.hub.calls)
        self.assertIn(("delete_session", ("codex:abc",)), self.hub.calls)

    def test_pin_and_mark_read(self) -> None:
        connection = self._pair()
        pin = self._call(connection, protocol.M_SESSION_PIN, {"key": "codex:abc"})
        mark = self._call(connection, protocol.M_SESSION_MARK_READ, {"key": "codex:abc"})
        self.assertTrue(pin["ok"])
        self.assertTrue(mark["ok"])
        self.assertTrue(pin["d"]["pinned"])
        self.assertEqual(mark["d"]["attention"], "none")

    def test_session_prompts(self) -> None:
        self.hub.prompts_result = [
            {"id": "ask-1", "name": "AskUserQuestion", "summary": "继续吗？", "options": ["是", "否"]}
        ]
        connection = self._pair()
        reply = self._call(connection, protocol.M_SESSION_PROMPTS, {"key": "codex:abc"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["d"]["prompts"][0]["options"], ["是", "否"])
        self.assertIn(("prompts", ("codex:abc",)), self.hub.calls)

    def test_input_text_and_image_shapes(self) -> None:
        import base64

        connection = self._pair()
        text = self._call(
            connection,
            protocol.M_INPUT_TEXT,
            {"key": "codex:abc", "text": "你好", "submit": True},
        )
        self.assertTrue(text["ok"])
        self.assertEqual(text["d"], {"ok": True})
        image = self._call(
            connection,
            protocol.M_INPUT_IMAGE,
            {"key": "codex:abc", "data": base64.b64encode(b"\xff\xd8\xffdata").decode()},
        )
        self.assertTrue(image["ok"])
        self.assertEqual(image["d"]["path"], "/tmp/paste.jpg")
        self.assertIn(("send_text", ("codex:abc", "你好", True)), self.hub.calls)
        self.assertIn(("send_image", ("codex:abc", 7)), self.hub.calls)

    def test_projects_and_runtimes_list(self) -> None:
        connection = self._pair()
        projects = self._call(connection, protocol.M_PROJECTS_LIST)
        runtimes = self._call(connection, protocol.M_RUNTIMES_LIST)
        self.assertTrue(projects["ok"])
        self.assertEqual(projects["d"]["projects"][0]["path"], "/Codes/demo")
        self.assertEqual(projects["d"]["projects"][0]["name"], "demo")
        self.assertTrue(runtimes["ok"])
        self.assertEqual(runtimes["d"]["runtimes"][0]["id"], "codex")


if __name__ == "__main__":
    unittest.main()
