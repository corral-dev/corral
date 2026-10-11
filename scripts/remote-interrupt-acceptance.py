#!/usr/bin/env python3
"""session.interrupt acceptance through the REAL remote dispatch path.

RemoteService.handle (pair -> hello -> session.interrupt) with a real
SessionHub drives real assistants hosted in owned tmux panes on the keepalive
socket. Each runtime starts a long shell-command turn; the interrupt must end
it (the assistant prints its own interrupted marker). A session without a
hosted pane must be refused without starting anything.

Requires the chosen runtimes to be installed and signed in; each run spends
one short model turn per runtime. Never touches user sessions: panes are named
corral-intprobe-<runtime>, work in a fresh temp directory and are always killed.

Usage:  PYTHONPATH=src .venv/bin/python scripts/remote-interrupt-acceptance.py [claude opencode codex cursor pi]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")
os.environ["CORRAL_CACHE_DIR"] = tempfile.mkdtemp(prefix="corral-interrupt-accept-")

from corral import keepalive  # noqa: E402
from corral.remote import protocol, ratelimit  # noqa: E402
from corral.remote.service import Connection, RemoteService  # noqa: E402
from corral.remote.sessions import SessionHub  # noqa: E402

PROMPT = "Run the shell command `sleep 90 && echo done` and then tell me the result."
# argv, working marker shown during the turn, interrupted marker afterwards
RUNTIMES = {
    "claude": (["claude", "--dangerously-skip-permissions"], "$ sleep 90", "Interrupted"),
    "opencode": (["opencode"], "esc interrupt", "interrupted"),
    "codex": (["codex", "--dangerously-bypass-approvals-and-sandbox"], "esc to interrupt", "interrupted"),
    "cursor": (["cursor-agent", "--force"], "ctrl+c to stop", "stopped"),
    "pi": (["pi"], "Working", "Aborted"),
}
TRUST_PROMPTS = ("Yes, I trust this folder", "Trust and continue")


def _pane(runtime: str) -> str:
    return f"corral-intprobe-{runtime}"


def _tmux(name: str, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*keepalive.tmux_argv(name), *argv],
        capture_output=True, text=True, timeout=10, env=keepalive.tmux_env(),
    )


def _capture(name: str) -> str:
    return _tmux(name, "capture-pane", "-p", "-t", name).stdout


def _hosted_names() -> set[str]:
    listed = _tmux("corral-intprobe", "list-sessions", "-F", "#S")
    return set(listed.stdout.split())


def _wait_for(name: str, needle: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle.lower() in _capture(name).lower():
            return True
        time.sleep(0.5)
    return False


def _session(runtime: str, pane: str | None) -> dict:
    session = {
        "source": runtime, "id": f"intprobe-{runtime}", "short_id": "intprobe",
        "cwd": "/tmp", "cwd_display": "/tmp", "mtime": time.time(), "display_time": "",
        "size_kb": 0.0, "status_tag": "running", "live": True,
        "fallback_title": "interrupt probe", "attention_kind": "working",
        "last_user_msg": "", "last_agent_msg": "", "path": "/tmp/intprobe.jsonl",
    }
    if pane:
        session["keepalive_name"] = pane
    return session


def _start_turn(runtime: str, workdir: str) -> None:
    name = _pane(runtime)
    argv, working, _ = RUNTIMES[runtime]
    _tmux(name, "kill-session", "-t", name)
    created = _tmux(name, "new-session", "-d", "-s", name, "-x", "160", "-y", "45", "-c", workdir, *argv)
    assert created.returncode == 0, created.stderr
    time.sleep(6)
    screen = _capture(name)
    if TRUST_PROMPTS[0] in screen:
        _tmux(name, "send-keys", "-t", name, "Down")
        time.sleep(0.3)
    if any(prompt in screen for prompt in TRUST_PROMPTS):
        _tmux(name, "send-keys", "-t", name, "Enter")
        time.sleep(4)
    _tmux(name, "send-keys", "-t", name, "-l", "--", PROMPT)
    time.sleep(0.3)
    _tmux(name, "send-keys", "-t", name, "Enter")
    assert _wait_for(name, working, 30), f"{runtime} never started working:\n{_capture(name)[-600:]}"
    time.sleep(4)  # let the shell command start


def main(selected: list[str]) -> int:
    workdir = tempfile.mkdtemp(prefix="corral-intprobe-")
    subprocess.run(["git", "init", "-q", workdir], check=False)
    hub = SessionHub(scan_limit=10)
    evidence: list[str] = []
    try:
        hub.store.sessions = {r: [_session(r, _pane(r))] for r in selected}
        hub.store.sessions.setdefault("claude", []).append(_session("claude", None) | {"id": "intprobe-ended"})
        ratelimit.INPUT_ACTIONS.reset()
        service = RemoteService(hub)
        sent: list[dict] = []
        connection = Connection("ad" * 32, sent.append)
        service.attach(connection)

        def call(method: str, params: dict) -> dict:
            sent.clear()
            if method == protocol.M_PAIR:
                params = {**params, "code": service.begin_pairing()}
            service.handle(connection, protocol.request(1, method, params))
            assert len(sent) == 1, (method, sent)
            return sent[0]

        call(protocol.M_PAIR, {"name": "Acceptance"})
        hello = call(protocol.M_HELLO, {"name": "Acceptance"})
        assert hello["d"]["capabilities"][protocol.CAPABILITY_SESSION_INTERRUPT] is True, hello
        evidence.append("hello advertises session_interrupt")

        before = _hosted_names()
        ended = call(protocol.M_SESSION_INTERRUPT, {"key": "claude:intprobe-ended"})
        assert not ended["ok"] and ended["e"]["code"] == "unavailable", ended
        time.sleep(1)
        assert _hosted_names() == before, "interrupt started a hosted session"
        evidence.append(f"unhosted session refused, nothing started: {ended['e']['message']}")

        for runtime in selected:
            name = _pane(runtime)
            _start_turn(runtime, workdir)
            reply = call(protocol.M_SESSION_INTERRUPT, {"key": f"{runtime}:intprobe-{runtime}"})
            assert reply["ok"] and reply["d"] == {"ok": True}, reply
            marker = RUNTIMES[runtime][2]
            assert _wait_for(name, marker, 15), f"{runtime} not interrupted:\n{_capture(name)[-800:]}"
            line = next(ln.strip() for ln in _capture(name).splitlines() if marker.lower() in ln.lower())
            evidence.append(f"{runtime}: interrupted mid-turn -> {line!r}")
    finally:
        for runtime in selected:
            _tmux(_pane(runtime), "kill-session", "-t", _pane(runtime))
        hub.stop()
        shutil.rmtree(workdir, ignore_errors=True)
    print("\n".join(evidence))
    print("ACCEPTANCE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["claude", "opencode"]))
