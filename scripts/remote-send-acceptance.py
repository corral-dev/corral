#!/usr/bin/env python3
"""Phone-input delivery acceptance through the REAL remote dispatch path.

Exercises RemoteService.handle (pair -> hello -> input.text -> command.status)
with a real SessionHub, real CommandReceiptStore and a real tmux pane running
plain `sh` — never a user session. Asserts delivered / rejected / unknown
receipts plus the actual received text+Enter in the pane.

Legs:
  delivered — paste+Enter land in the owned pane; duplicate command_id does not
    re-dispatch (exact-once by digest).
  rejected  — unknown session key is rejected (not_found, not retryable) and
    nothing is written into any pane.
  unknown   — the paste-buffer syscall times out (surgical fault: everything
    else stays real), so the receipt must stay unknown/partial_injection and
    must never auto-retry or resume. The text may or may not have landed;
    only the receipt state is asserted.

Isolation: CORRAL_CACHE_DIR points at a fresh temp dir (no real pairing state
touched); probe panes are named corral-sendprobe-svc* and always killed.

Usage:  PYTHONPATH=src .venv/bin/python scripts/remote-send-acceptance.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("TEXTUAL_DISABLE_KITTY_KEY", "1")
_tmp_cache = tempfile.mkdtemp(prefix="corral-send-accept-")
os.environ["CORRAL_CACHE_DIR"] = _tmp_cache

from corral import embed, keepalive  # noqa: E402
from corral.remote import protocol, ratelimit  # noqa: E402
from corral.remote.command_receipts import (  # noqa: E402
    STATUS_DELIVERED,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
)
from corral.remote.service import Connection, RemoteService  # noqa: E402
from corral.remote.sessions import SessionHub  # noqa: E402

PROBE = "corral-sendprobe-svc"
MARK_OK = "SVCaccept-61073"
MARK_DUP = "SVCaccept-dup-61074"
MARK_UNKNOWN = "echo SVCaccept-unc-61075"
REJECT_TEXT = "SVCaccept-rejected-61076-must-never-land"


def _tmux(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*keepalive.tmux_argv(PROBE), *argv],
        capture_output=True, text=True, timeout=10, env=keepalive.tmux_env(),
    )


def _capture(name: str = PROBE) -> str:
    return _tmux("capture-pane", "-p", "-t", name).stdout


def _hub_session(sid: str, pane: str) -> dict:
    return {
        "source": "codex", "id": sid, "short_id": sid[:8], "cwd": "/tmp",
        "cwd_display": "/tmp", "mtime": 1_700_000_000.0, "display_time": "",
        "size_kb": 0.0, "status_tag": "running", "live": True,
        "keepalive_name": pane, "fallback_title": "acceptance probe",
        "attention_kind": "none", "last_user_msg": "", "last_agent_msg": "",
        "path": "/tmp/accept-probe.jsonl",
    }


def main() -> int:
    evidence: list[str] = []
    hub = SessionHub(scan_limit=10)
    try:
        _tmux("kill-session", "-t", PROBE)
        created = _tmux("new-session", "-d", "-s", PROBE, "-x", "80", "-y", "24", "sh")
        assert created.returncode == 0, created.stderr
        hub.store.sessions = {"codex": [_hub_session("svcprobe1", PROBE)]}

        for limiter in (ratelimit.INPUT_ACTIONS, ratelimit.SESSION_CREATE):
            limiter.reset()
        service = RemoteService(hub)
        sent: list[dict] = []
        connection = Connection("ac" * 32, sent.append)
        service.attach(connection)

        def _call(method: str, params: dict) -> dict:
            sent.clear()
            code = service.begin_pairing() if method == protocol.M_PAIR else None
            if code is not None:
                params = {**params, "code": code}
            service.handle(connection, protocol.request(1, method, params))
            assert len(sent) == 1, (method, sent)
            return sent[0]

        _call(protocol.M_PAIR, {"name": "Acceptance"})
        _call(protocol.M_HELLO, {"name": "Acceptance", "want_command_receipts": True})
        assert connection.command_receipts

        # -- delivered ----------------------------------------------------
        text = f"echo {MARK_OK}"
        reply = _call(
            protocol.M_INPUT_TEXT,
            {"key": "codex:svcprobe1", "text": text, "submit": True,
             "command_id": "acc-delivered-1"},
        )
        assert reply["ok"] and reply["d"]["status"] == STATUS_DELIVERED, reply
        assert "reason" not in reply["d"] and "detail" not in reply["d"], reply
        time.sleep(0.8)
        pane = _capture()
        assert MARK_OK in pane, pane[-300:]
        status = _call(protocol.M_COMMAND_STATUS, {"command_id": "acc-delivered-1"})
        assert status["d"]["status"] == STATUS_DELIVERED, status
        evidence.append(f"delivered: receipt+status delivered, pane shows {MARK_OK}")

        # exact-once: same command_id must not re-dispatch into the pane.
        before = _capture().count(MARK_OK)
        dup = _call(
            protocol.M_INPUT_TEXT,
            {"key": "codex:svcprobe1", "text": text, "submit": True,
             "command_id": "acc-delivered-1"},
        )
        assert dup["d"]["status"] == STATUS_DELIVERED, dup
        assert _capture().count(MARK_OK) == before
        evidence.append("exact-once: redelivered command_id did not re-inject")

        # -- rejected -----------------------------------------------------
        bad = _call(
            protocol.M_INPUT_TEXT,
            {"key": "codex:does-not-exist", "text": REJECT_TEXT, "submit": True,
             "command_id": "acc-rejected-1"},
        )
        assert bad["ok"] and bad["d"]["status"] == STATUS_REJECTED, bad
        assert bad["d"]["reason"] == "not_found", bad
        assert bad["d"]["retryable"] is False, bad
        assert "detail" in bad["d"] and REJECT_TEXT not in bad["d"]["detail"], bad
        bad_status = _call(protocol.M_COMMAND_STATUS, {"command_id": "acc-rejected-1"})
        assert bad_status["d"]["status"] == STATUS_REJECTED, bad_status
        assert REJECT_TEXT not in _capture()
        evidence.append("rejected: not_found/not-retryable, nothing written to pane")

        # -- unknown ------------------------------------------------------
        real_run = subprocess.run

        def _flaky_paste(argv, **kwargs):
            if isinstance(argv, list) and "paste-buffer" in argv:
                raise subprocess.TimeoutExpired(argv, embed._CALL_TIMEOUT)
            return real_run(argv, **kwargs)

        import unittest.mock as mock

        with mock.patch.object(embed.subprocess, "run", side_effect=_flaky_paste):
            unk = _call(
                protocol.M_INPUT_TEXT,
                {"key": "codex:svcprobe1", "text": MARK_UNKNOWN, "submit": True,
                 "command_id": "acc-unknown-1"},
            )
        assert unk["ok"] and unk["d"]["status"] == STATUS_UNKNOWN, unk
        assert unk["d"]["reason"] == "partial_injection", unk
        assert MARK_UNKNOWN.split(" ", 1)[1] not in unk["d"].get("detail", ""), unk
        unk_status = _call(protocol.M_COMMAND_STATUS, {"command_id": "acc-unknown-1"})
        assert unk_status["d"]["status"] == STATUS_UNKNOWN, unk_status
        evidence.append("unknown: paste-buffer timeout -> partial_injection, no retry/resume")

        print("REMOTE-SEND-ACCEPTANCE PASS")
        for line in evidence:
            print(f"  - {line}")
        return 0
    except AssertionError as exc:
        print(f"REMOTE-SEND-ACCEPTANCE FAIL: {exc!r}")
        return 1
    finally:
        try:
            hub.stop()
        except Exception:
            pass
        _tmux("kill-session", "-t", PROBE)


if __name__ == "__main__":
    sys.exit(main())
