#!/usr/bin/env python3
"""Actual Corral card clicks against a disposable native Codex transcript.

Run with the prepared checkout environment. No HOME override, credentials,
model calls, user histories or shared tmux server. --rows exercises both short
and tall fullscreen layouts. SVGs are the actual Textual renderer output.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fixture(directory: Path, session_id: str) -> Path:
    native = directory / "codex"
    work = directory / "work"
    work.mkdir(parents=True)
    native.mkdir()
    (native / "config.toml").write_text(
        'model="fixture"\nmodel_provider="fixture"\ncheck_for_update_on_startup=false\n'
        '[model_providers.fixture]\nname="fixture"\nbase_url="http://127.0.0.1:1/v1"\n'
        'wire_api="responses"\nrequires_openai_auth=false\n'
        f'[projects.{json.dumps(str(work))}]\ntrust_level="trusted"\n'
    )
    stamp = datetime.now(timezone.utc).isoformat()
    rows = [{"timestamp": stamp, "type": "session_meta", "payload": {
        "id": session_id, "timestamp": stamp, "cwd": str(work),
        "originator": "codex_cli_rs", "cli_version": "0.163.0", "source": "cli",
        "model_provider": "fixture",
    }}]
    for number in range(50):
        rows.append({"timestamp": stamp, "type": "event_msg", "payload": {
            "type": "task_started", "turn_id": str(uuid.uuid4()), "model_context_window": 200000,
        }})
        prompt = "继续" if number in (7, 17, 27) else f"CODEX_PROMPT_{number:03d} synthetic question"
        answer = f"CODEX_ANSWER_{number:03d}\n" + "\n".join(f"Synthetic line {i}" for i in range(5))
        for role, content in (("user", prompt), ("assistant", answer)):
            rows.append({"timestamp": stamp, "type": "event_msg", "payload": {
                "type": "user_message" if role == "user" else "agent_message", "message": content,
            }})
            rows.append({"timestamp": stamp, "type": "response_item", "payload": {
                "type": "message", "role": role, "content": [{
                    "type": "input_text" if role == "user" else "output_text", "text": content,
                }],
            }})
    history = native / "sessions/2026/10/16" / f"rollout-2026-10-16T12-00-00-{session_id}.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    return history


async def exercise(directory, session_id, history, tmux, rows, columns):
    # Route only host resources to the disposable socket/history. Scanner,
    # SessKit reader, UI handler, navigation, capture and normal input are real.
    import corral
    from corral import embed, keepalive, legacy_names, liveness
    from corral.runtime.codex import CodexRuntime
    from corral.scan import codex
    from corral.ui import terminal_prompt_navigation as navigation
    from corral.ui.app import CorralApp
    from corral.ui.session_hud import SessionHud
    from corral.ui.split_pane_area import SplitPaneArea

    def argv(name=None):
        return tuple(tmux)

    def socket(name=None):
        return tmux[2]
    legacy_names.tmux_argv_for_session = keepalive.tmux_argv_for_session = argv
    liveness.tmux_argv_for_session = navigation.tmux_argv_for_session = argv
    legacy_names.socket_for_session = embed.socket_for_session = socket
    codex.SESSIONS_DIR = str(directory / "codex/sessions")
    codex.SESSION_INDEX = str(directory / "codex/session_index.jsonl")
    store = corral.SessionStore(limit=100, registry=corral.RuntimeRegistry((CodexRuntime(),)))
    store.load()
    summary = next(s for s in store.all_sessions() if s["id"] == session_id)
    summary.update(keepalive_name="probe", live=False)
    key = corral.session_key(summary)
    store.hosted[key] = "probe"
    assert len([m for m in store.get_conversation(summary) if m.role == "user"]) == 50
    app = CorralApp(store, embed_ok=True)
    reports = []
    async with app.run_test(size=(columns + 60, rows + 7)) as pilot:
        area = app.screen.query_one(SplitPaneArea)
        area.show_hosted_group(str(directory / "work"), [(summary, "probe", lambda: "")], focus_key=key)
        app.screen._hud_expanded = True
        app.screen._sync_hud()
        for _ in range(200):
            await pilot.pause(.025)
            cells = area.cells()
            pane = cells[0].embed_pane() if cells else None
            if pane and "CODEX_ANSWER_049" in pane.render().plain:
                break
        assert pane and "CODEX_ANSWER_049" in pane.render().plain, "Initial native transcript missing"
        pane.focus()  # Initial setup only. Never repair focus after a card click.
        await pilot.pause(.05)
        hud = cells[0].query_one(SessionHud)
        for number in (17, 7, 27):
            hud._scroll_by(number - hud._scroll)
            hud.refresh()
            await pilot.pause(.05)
            before = pane.render().plain
            app.save_screenshot(path=str(directory), filename=f"{number}-before.svg")
            started = time.monotonic()
            await pilot.click(hud, offset=(3, 1))
            frames = []
            for _ in range(500):
                await pilot.pause(.01)
                frames.append(pane.render().plain)
                if f"CODEX_ANSWER_{number:03d}" in frames[-1]:
                    break
            elapsed = round((time.monotonic() - started) * 1000)
            final = pane.render().plain
            assert f"CODEX_ANSWER_{number:03d}" in final, final
            assert all(frame in (before, final) for frame in frames), "Intermediate native frame leaked"
            assert all("Find:" not in frame and "Find ·" not in frame for frame in frames)
            assert app.screen._hud_expanded, "Card unexpectedly collapsed"
            assert pane.has_focus, "Card stole ordinary keyboard focus"
            assert "UNSENT_DRAFT_CODEX" in embed.capture("probe", 0, rows + 10)
            app.save_screenshot(path=str(directory), filename=f"{number}-after.svg")
            report = {"selectedTurn": number, "elapsedMs": elapsed, "actualCardClick": True,
                      "onlyInitialAndFinal": True, "draftAndFocusPreserved": True}
            reports.append(report)
            print(json.dumps(report), flush=True)
        marker = "ORDINARY_AFTER_JUMP_" + uuid.uuid4().hex[:8]
        await pilot.press(*list(marker), "enter")
        for _ in range(200):
            await pilot.pause(.025)
            data = [json.loads(line) for line in history.read_text().splitlines()]
            users = [v["payload"]["message"] for v in data if v.get("type") == "event_msg"
                     and v.get("payload", {}).get("type") == "user_message"]
            if len(users) > 50:
                break
        assert len(users) == 51, "Ordinary submission lost or duplicated"
        assert users[-1] == "UNSENT_DRAFT_CODEX" + marker, users[-1]
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--rows", type=int, default=28)
    parser.add_argument("--columns", type=int, default=100)
    parser.add_argument("--artifacts-dir", type=Path)
    args = parser.parse_args()
    if not args.codex or not shutil.which("tmux"):
        parser.error("Installed Codex and tmux are required")
    if not 20 <= args.rows <= 100 or not 80 <= args.columns <= 180:
        parser.error("Use rows 20..100 and columns 80..180")
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    directory = args.artifacts_dir or base / "corral/development/prompt-navigation" / uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    os.environ.update(CORRAL_CACHE_DIR=str(directory / "cache"), CORRAL_STATE_DIR=str(directory / "state"),
                      XDG_CONFIG_HOME=str(directory / "config"), CORRAL_ISOLATE_MANAGED_HOSTS="1")
    os.environ.pop("NO_COLOR", None)
    os.environ["COLORTERM"] = "truecolor"
    session_id = str(uuid.uuid4())
    history = fixture(directory, session_id)
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ}
    env.update(TERM="xterm-256color", COLORTERM="truecolor", CODEX_HOME=str(directory / "codex"),
               CODEX_DISABLE_UPDATE_CHECK="1")
    binary = str(Path(args.codex).resolve())
    argv = [binary, "--no-daemon", "--sandbox", "read-only", "--ask-for-approval", "never", "resume", session_id]
    runner = directory / "native.py"
    runner.write_text("import os\nos.environ.clear()\nos.environ.update(" + repr(env) + ")\nos.chdir("
                      + repr(str(directory / "work")) + ")\nos.execv(" + repr(binary) + "," + repr(argv) + ")\n")
    tmux = ["tmux", "-L", "corral-prompt-accept-" + uuid.uuid4().hex[:12], "-f", "/dev/null"]
    def command(*args, check=True):
        return subprocess.run([*tmux, *map(str, args)], capture_output=True, text=True, check=check, timeout=10).stdout
    try:
        command("new-session", "-d", "-s", "probe", "-x", args.columns, "-y", args.rows,
                shlex.join((sys.executable, str(runner))))
        for _ in range(200):
            if "CODEX_PROMPT_049" in command("capture-pane", "-p", "-t", "probe"):
                break
            time.sleep(.025)
        assert "CODEX_PROMPT_049" in command("capture-pane", "-p", "-t", "probe")
        assert command("display-message", "-p", "-t", "probe", "#{alternate_on}:#{history_size}").strip() == "1:0"
        command("send-keys", "-t", "probe", "-l", "UNSENT_DRAFT_CODEX")
        reports = asyncio.run(exercise(directory, session_id, history, tmux, args.rows, args.columns))
        report = {"ok": True, "nativeVersion": subprocess.check_output([binary, "--version"], text=True).strip(),
                  "cases": reports, "nextNativeUserMessages": 1, "artifacts": str(directory)}
        (directory / "result.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    finally:
        command("kill-server", check=False)
        assert subprocess.run([*tmux, "list-sessions"], capture_output=True).returncode != 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    main()
