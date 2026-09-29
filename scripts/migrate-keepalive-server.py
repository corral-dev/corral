#!/usr/bin/env python3
"""One-time move of a clamped keepalive server to an app-class launchd job.

A keepalive tmux server started from a launchd job keeps launchd's CPU/I/O
clamp for its whole life, and so does every hosted agent (see
docs/MAINTAINER_GUIDE.md "Keepalive server scheduling class"). The clamp cannot
be lifted in place, so this script:

1. waits until no hosted agent is in the ``working`` phase (up to --max-wait);
2. ends the old server (hosted processes end; their history stays and native
   resume brings them back);
3. starts the server as the Interactive launchd job and probes a pane's
   scheduling priority.

Every step is appended to ~/.cache/corral/keepalive-migration.log. Run it
detached when the caller itself lives inside the server.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from corral import keepalive, observe, tmux_server
from corral.legacy_names import SOCKET_NAME, cache_dir

LOG = cache_dir() / "keepalive-migration.log"
PROBE = "migration-probe"


def log(event: str, **fields: object) -> None:
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    observe.event(f"keepalive_migration_{event}", **fields)


def tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-L", SOCKET_NAME, *args], capture_output=True, text=True,
        timeout=10, check=False, env=keepalive.tmux_env(),
    )


def hosted_names() -> list[str]:
    out = tmux("list-sessions", "-F", "#{session_name}")
    return [n for n in out.stdout.split() if n] if out.returncode == 0 else []


def working_hosted(names: list[str]) -> list[str]:
    pairs = keepalive._load_working_pairs()
    return [n for n in names if keepalive._is_working_keepalive(n, pairs)]


def wait_until_idle(max_wait: float, poll: float) -> bool:
    deadline = time.monotonic() + max_wait
    while True:
        busy = working_hosted(hosted_names())
        if not busy:
            return True
        if time.monotonic() >= deadline:
            log("gave_up_busy", busy=busy)
            return False
        log("waiting_busy", busy=busy)
        time.sleep(poll)


def probe_pane_priority() -> int | None:
    tmux("new-session", "-d", "-s", PROBE, "--", "sleep", "20")
    try:
        pid = tmux("display-message", "-p", "-t", PROBE, "#{pane_pid}").stdout.strip()
        return tmux_server.scheduling_priority(int(pid)) if pid.isdigit() else None
    finally:
        tmux("kill-session", "-t", f"={PROBE}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--delay", type=float, default=0.0, help="seconds to wait before starting")
    parser.add_argument("--max-wait", type=float, default=6 * 3600, help="give up if agents stay busy")
    parser.add_argument("--poll", type=float, default=30.0)
    parser.add_argument("--force", action="store_true", help="restart even if the server is not clamped")
    args = parser.parse_args()
    observe.init(debug=False)
    time.sleep(args.delay)
    env = keepalive.tmux_env()
    before = tmux_server.server_report(SOCKET_NAME, env)
    log("start", before=before)
    if before.get("running") and not before.get("clamped") and not args.force:
        log("skipped_not_clamped")
        return 0
    if before.get("running"):
        if not wait_until_idle(args.max_wait, args.poll):
            return 2
        ended = hosted_names()
        tmux("kill-server")
        for _ in range(100):
            if not tmux_server.server_running(SOCKET_NAME, env):
                break
            time.sleep(0.1)
        log("old_server_ended", sessions=ended)
    started = tmux_server.ensure_server(SOCKET_NAME, keepalive.ensure_config_file(), env)
    after = tmux_server.server_report(SOCKET_NAME, env)
    pane_priority = probe_pane_priority()
    ok = bool(after.get("interactive_job")) and not after.get("clamped") and (pane_priority or 0) >= 31
    log("done", started=started, after=after, pane_priority=pane_priority, ok=ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
