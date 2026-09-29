#!/usr/bin/env python3
"""One-time move of a clamped keepalive server to an app-class launchd job.

A keepalive tmux server started from a launchd job keeps launchd's CPU/I/O
clamp for its whole life, and so does every hosted agent (see
docs/MAINTAINER_GUIDE.md "Keepalive server scheduling class"). The clamp cannot
be lifted in place, so this script:

1. waits until no hosted agent is busy under the shared verdict (``working``
   attention phase or pending background work — see ``corral.busycheck``;
   refuses to proceed while any session is busy unless ``--force``);
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

from corral import busycheck, keepalive, observe, tmux_server
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


def busy_hosted(names: list[str]) -> dict[str, str]:
    """Sessions that must block a whole-server restart, with reasons.

    The shared background-work verdict (``busycheck``) plus the working
    attention phase. A finished reply is not proof the agent is done: on
    2026-09-30 this wait saw no ``working`` agent and ended a server whose
    Claude session was still waiting on its own background shell tasks.
    """
    pairs = keepalive._load_working_pairs()
    blocked = {n: "working" for n in names if keepalive._is_working_keepalive(n, pairs)}
    checker = busycheck.BusyChecker.from_probe()
    if checker is None:
        # The probe itself is unusable: block on everything rather than
        # declare the server idle.
        return {n: blocked.get(n, "background_unknown") for n in names}
    for name, verdict in checker.process_busy_names(names).items():
        blocked.setdefault(name, verdict.reason)
    return blocked


def wait_until_idle(max_wait: float, poll: float, *, force: bool = False) -> bool:
    if force:
        log("forced_busy_bypass")
        print("migrate-keepalive-server: --force bypasses the busy wait", flush=True)
        return True
    deadline = time.monotonic() + max_wait
    while True:
        busy = busy_hosted(hosted_names())
        if not busy:
            return True
        if time.monotonic() >= deadline:
            log("gave_up_busy", busy=busy)
            print(f"migrate-keepalive-server: still busy, giving up: {sorted(busy)}", flush=True)
            return False
        log("waiting_busy", busy=busy)
        print(f"migrate-keepalive-server: waiting, busy sessions: {sorted(busy)}", flush=True)
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
    parser.add_argument("--force", action="store_true",
                        help="restart even if the server is not clamped, and do not wait for busy sessions")
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
        if not wait_until_idle(args.max_wait, args.poll, force=args.force):
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
