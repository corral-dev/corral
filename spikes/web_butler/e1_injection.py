"""E1 — can Corral drive a worker (Y) reliably? One run per assistant runtime.

Checks, per runtime, through Corral's own session layer (no re-implementation):
  1. create a hosted session in a disposable git project;
  2. submit a first instruction that keeps the agent busy (~40 s);
  3. mid-turn, inject a second instruction (the "steer");
  4. confirm delivery from the session transcript (both instructions recorded as
     user messages), not from the paste command's exit status;
  5. confirm the agent acted on both (files exist), and observe completion signals.

Usage: python spikes/web_butler/e1_injection.py claude [codex ...] [--keep]
Writes a JSON line per runtime to spikes/web_butler/results/e1.jsonl.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from corral import embed, keepalive  # noqa: E402
from corral.remote import sessions as _sessions  # noqa: E402
from corral.remote.sessions import SessionHub, _plain_pane_text  # noqa: E402

_product_ready = _sessions._pane_accepts_input


def _pane_accepts_input(plain: str) -> bool:
    """Finding F2: product readiness only knows the arrow prompt; Claude Code 2.1 shows '❯'."""
    if _product_ready(plain):
        return True
    if "Trust" in plain and "folder" in plain:
        return False
    return any(line.strip() in ("❯", "›") or line.startswith(("❯ ", "› "))
               for line in plain.splitlines())


_sessions._pane_accepts_input = _pane_accepts_input  # experiment-only patch

FIRST = (
    "Run the shell command `sleep 40` and wait for it to finish. Then create a file "
    "named first.txt containing the word ONE. Reply with the single word FIRST-DONE."
)
STEER = (
    "Additional instruction for the current task: also create a file named steer.txt "
    "containing the word TWO. When both files exist reply with the single word ALL-DONE."
)
STEER_DELAY = 12.0
TIMEOUT = 300.0


def _project(runtime_id: str) -> Path:
    root = Path("/tmp/butler-e1") / f"{runtime_id}-{int(time.time())}"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "README.md").write_text("disposable project for Corral web butler experiment E1\n")
    return root


# Startup dialogs seen in practice: (needle in pane text, keys that accept "trust").
TRUST_GATES = (
    ("Yes, I trust this folder", ("Down", "Enter")),  # Claude Code: default is "No, exit"
    ("1. Trust and continue", ("Enter",)),  # Codex: default is trust
    ("Trust this workspace", ("Enter",)),  # Cursor (assumed wording; verify)
)


def _pass_startup_gates(name: str, timeout: float = 90.0) -> list[str]:
    """Wait until the pane accepts input, accepting known trust dialogs on the way."""
    seen: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        plain = _plain_pane_text(embed.capture(name, 0, 0))
        for needle, keys in TRUST_GATES:
            if needle in plain and needle not in seen:
                seen.append(needle)
                embed.send_key(name, *keys)
                time.sleep(2)
                break
        else:
            if _pane_accepts_input(plain):
                return seen
        time.sleep(1)
    seen.append("<timeout; last pane>\n" + _plain_pane_text(embed.capture(name, 0, 0))[-800:])
    return seen


def _hosted(hub: SessionHub, name: str) -> dict | None:
    for session in hub.store.all_sessions():
        if session.get("keepalive_name") == name:
            return session
    return None


def _user_texts(hub: SessionHub, session: dict) -> list[str]:
    try:
        return [m.text for m in hub.store.get_conversation(session) if m.role == "user"]
    except Exception as exc:  # experiment: record, do not crash
        return [f"<conversation error: {exc}>"]


def run(runtime_id: str, keep: bool) -> dict:
    hub = SessionHub()
    hub.store.load()
    hub.store.wait_loaded(60)
    cwd = _project(runtime_id)
    result: dict = {"runtime": runtime_id, "cwd": str(cwd), "t0": time.time()}
    payload = hub.new_session(runtime_id, str(cwd), whitelist=[str(cwd)])
    key = payload["key"]
    name = hub.store.hosted_name_for(key) or ""
    result["keepalive_name"] = name
    t_start = time.monotonic()
    try:
        result["startup_gates"] = _pass_startup_gates(name)
        result["ready_s"] = round(time.monotonic() - t_start, 1)
        hub.send_turn(key, FIRST, ready_timeout=90)
        result["first_sent_s"] = round(time.monotonic() - t_start, 1)
        time.sleep(STEER_DELAY)
        try:
            hub.send_turn(key, STEER, ready_timeout=30)
            result["steer_send"] = "ok"
        except Exception as exc:
            result["steer_send"] = f"{type(exc).__name__}: {exc}"
        result["steer_sent_s"] = round(time.monotonic() - t_start, 1)

        seen: dict[str, float] = {}
        while time.monotonic() - t_start < TIMEOUT:
            hub.store.refresh()
            session = _hosted(hub, name)
            elapsed = round(time.monotonic() - t_start, 1)
            if session:
                texts = _user_texts(hub, session)
                if "first_in_transcript" not in seen and any("first.txt" in t for t in texts):
                    seen["first_in_transcript"] = elapsed
                if "steer_in_transcript" not in seen and any("steer.txt" in t for t in texts):
                    seen["steer_in_transcript"] = elapsed
                status = str(session.get("status_tag") or "")
                if status and f"status:{status}" not in seen:
                    seen[f"status:{status}"] = elapsed
                comp = str(session.get("completion_id") or "")
                if comp and f"completion:{comp}" not in seen:
                    seen[f"completion:{comp}"] = elapsed
                result["session_id"] = session.get("id")
                hist = str(session.get("path") or "")
                result["history_path"] = hist
                if hist and "steer_in_raw_history" not in seen:
                    try:
                        if "steer.txt" in Path(hist).read_text(errors="replace"):
                            seen["steer_in_raw_history"] = elapsed
                    except OSError:
                        pass
                result["attention"] = session.get("attention_kind")
            for fname in ("first.txt", "steer.txt"):
                if f"file:{fname}" not in seen and (cwd / fname).exists():
                    seen[f"file:{fname}"] = elapsed
            if "file:first.txt" in seen and "file:steer.txt" in seen and \
                    "steer_in_transcript" in seen and any(k.startswith("completion:") for k in seen):
                break
            time.sleep(3)
        result["events"] = seen
        result["user_messages"] = [t[:120] for t in _user_texts(hub, _hosted(hub, name) or {})] \
            if _hosted(hub, name) else []
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["pane_tail"] = _plain_pane_text(embed.capture(name, 0, 0))[-1500:] if name else ""
        if not keep and name:
            keepalive.kill(name)
    result["ok"] = all(k in result.get("events", {}) for k in
                       ("file:first.txt", "file:steer.txt", "steer_in_transcript"))
    return result


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep = "--keep" in sys.argv
    out = HERE / "results" / "e1.jsonl"
    out.parent.mkdir(exist_ok=True)
    for runtime_id in args:
        try:
            res = run(runtime_id, keep)
        except Exception as exc:
            res = {"runtime": runtime_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        with out.open("a") as fh:
            fh.write(json.dumps(res, ensure_ascii=False) + "\n")
        summary = {k: res.get(k) for k in ("runtime", "ok", "error", "steer_send", "events")}
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("CORRAL_NO_UPDATE_CHECK", "1")
    raise SystemExit(main())
