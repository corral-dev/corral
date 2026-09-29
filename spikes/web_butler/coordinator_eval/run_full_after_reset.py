"""One-shot: wait for the claude-haiku session limit to reset, then run the full eval.

The claude-haiku CLI returns 429 "You've hit your session limit · resets 1:20am
(Asia/Shanghai)". This script sleeps until a few minutes after that, then runs the
full eval once and saves the result under a t6-specific name so nothing is lost.

Launch it detached (it survives the agent session):
    python3 run_full_after_reset.py
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Reset is 01:20 Asia/Shanghai (= CST). Give a small buffer and retry a few times in
# case the limit is a rolling window that does not clear exactly on time.
RESET_HOUR, RESET_MINUTE = 1, 20
BUFFER_SECONDS = 150
MAX_ATTEMPTS = 4
ATTEMPT_GAP_SECONDS = 15 * 60


def cst_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))


def seconds_until_reset() -> float:
    now = cst_now()
    target = now.replace(hour=RESET_HOUR, minute=RESET_MINUTE, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


def main() -> int:
    log = HERE / "results" / "t6-full-run.log"
    with log.open("a") as fh:
        def emit(msg: str) -> None:
            line = f"{cst_now().isoformat(timespec='seconds')} {msg}"
            print(line, flush=True)
            fh.write(line + "\n")
            fh.flush()

        emit(f"start; reset target {RESET_HOUR:02d}:{RESET_MINUTE:02d} CST")
        wait = seconds_until_reset() + BUFFER_SECONDS
        emit(f"sleeping {wait:.0f}s")
        time.sleep(wait)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            emit(f"attempt {attempt}/{MAX_ATTEMPTS}: full claude-haiku eval")
            proc = subprocess.run(
                [sys.executable, "run_eval.py", "claude-haiku"],
                cwd=HERE, capture_output=True, text=True,
            )
            out = proc.stdout + proc.stderr
            emit(out[-2000:])
            if "session limit" in out and "api_error_status\":429" in out:
                emit("still rate-limited; will retry")
                time.sleep(ATTEMPT_GAP_SECONDS)
                continue
            # Copy the fresh full result under a t6 name.
            src = HERE / "results" / "e3-claude-haiku-full.json"
            dst = HERE / "results" / "e3-claude-haiku-full-t6.json"
            if src.exists():
                dst.write_bytes(src.read_bytes())
                emit(f"saved {dst.name}")
            emit("done")
            return 0
        emit("exhausted retries; still rate-limited")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
