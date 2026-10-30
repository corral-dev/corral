"""Shared supervision test doubles (no real terminals, sessions or runtimes).

_FakeHub is SessionHub-shaped: it records terminal effects and serves
preregistered sessions. Observation always goes through REAL SessKit reads of
fixture histories the fake "persists" when it delivers, so the shared runner,
correlation and settlement paths under test are genuine.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace


class _StubRuntime:
    """Adapter-shaped stub: real call shapes, no runtime behavior."""

    id = "claude"
    display_name = "Claude"
    interrupt_keys = ("Escape",)
    auto_approve_args = ()
    model_flag = None
    session_id_flag = None

    def __init__(self, sessions=()):
        self._sessions = list(sessions)

    def composer_region(self, screen):
        return None

    def scan_sessions(self, limit, keep_ids=None):
        return list(self._sessions)

    def build_new_session_plan(self, cwd):
        from corral.models import LaunchPlan

        return LaunchPlan(("test-agent", "--fresh"), cwd)


class _StubCodexRuntime(_StubRuntime):
    id = "codex"
    display_name = "Codex"


class _FakeHubStore:
    def __init__(self):
        self.sessions = {}
        self.hosted = {}
        self.by_key = {}
        self.lock = threading.Lock()

    def refresh(self):
        pass

    def find_session(self, key):
        return self.by_key.get(key)

    def resolve_session_key(self, key):
        return key

    def seed(self, session):
        from corral.models import session_key

        bucket = [s for s in self.sessions.get(session["source"], []) if s.get("id") != session.get("id")]
        self.sessions[session["source"]] = [session, *bucket]
        key = session_key(session)
        self.by_key[key] = session
        if session.get("keepalive_name"):
            self.hosted[key] = session["keepalive_name"]


class _FakeHub:
    """SessionHub-shaped stub: records terminal effects, persists nothing itself."""

    def __init__(self, session=None, runtime=None, planned_key=None):
        self.runtime = runtime or _StubRuntime([session] if session else [])
        self.registry = SimpleNamespace(get=lambda rid: self.runtime)
        self.store = _FakeHubStore()
        if session:
            self.store.seed(session)
        self.sent = []
        self.interrupted = []
        self.append_on_send = None
        self.planned_key = planned_key
        self.prepared = []
        self.required = []
        self.answered = []

    def prepare_text_delivery(self, key, deadline):
        self.prepared.append(key)
        return ("tok", 0, 0, "")

    def require_session(self, key):
        self.required.append(key)
        found = self.store.find_session(key)
        if found is None:
            raise KeyError(key)
        return found

    def resolve_session_key(self, key):
        return key

    def send_text(self, key, text):
        self.sent.append((key, text))
        if self.append_on_send is not None:
            self.append_on_send(text)

    def verify_text_injected(self, key, baseline):
        pass

    def interrupt_session(self, key):
        self.interrupted.append(key)

    def answer_question(self, key, request_id, answers):
        self.answered.append((key, request_id))
        return {"status": "delivered", "detail": "ok"}

    def prompts(self, key):
        return []

    def host_planned_session(self, plan, runtime_id, title, cwd, ident=None):
        key = self.planned_key or f"{runtime_id}:fix-{ident}"
        if key not in self.store.by_key:
            self.store.by_key[key] = {
                "source": runtime_id,
                "id": f"fix-{ident}",
                "cwd": cwd,
                "path": None,
                "provisional": True,
                "live": True,
                "keepalive_name": f"corral-{runtime_id}-{ident}",
            }
        return {"key": key}


class _ClaudeComposerStub(_StubRuntime):
    """Stubbed scan plus the real Claude composer rule for the draft guard."""

    def composer_region(self, screen):
        from corral.runtime.claude import ClaudeRuntime

        return ClaudeRuntime().composer_region(screen)


def _write_fixture_rows(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _append_fixture_row(path, row):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
