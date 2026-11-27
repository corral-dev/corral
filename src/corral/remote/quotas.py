"""Read-only host quota hub backed by local `subswap` CLI."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from corral.remote import protocol

_POLL_INTERVAL_SECONDS = 15.0
_DEFAULT_TIMEOUT_SECONDS = 25.0
_CANDIDATE_BINS = (
    Path.home() / ".local" / "bin" / "subswap",
    Path("/opt/homebrew/bin/subswap"),
    Path("/usr/local/bin/subswap"),
    Path.home() / ".cargo" / "bin" / "subswap",
)


def find_subswap_binary() -> str | None:
    """Locate an executable `subswap` CLI binary without shell startup overhead."""
    resolved = shutil.which("subswap")
    if resolved and os.access(resolved, os.X_OK):
        return resolved
    for candidate in _CANDIDATE_BINS:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _is_unknown_refresh_flag(stderr: str) -> bool:
    lowered = stderr.lower()
    return "--refresh" in lowered and (
        "unexpected argument" in lowered
        or "unknown" in lowered
        or "unrecognized" in lowered
        or "invalid option" in lowered
    )


def _error_text(proc: subprocess.CompletedProcess[str]) -> str:
    detail = (proc.stderr or proc.stdout or "").strip()
    if detail:
        return detail.splitlines()[0][:240]
    return f"subswap exited with status {proc.returncode}"


def fetch_subswap_quotas(
    *,
    refresh: bool = False,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Execute `subswap status --json --no-metrics [--refresh]` cleanly.

    Never performs swap/write mutations. Older CLIs that reject `--refresh`
    fall back once to plain `status --json --no-metrics`.
    """
    binary = find_subswap_binary()
    if binary is None:
        return {
            "available": False,
            "reason": "subswap_not_installed",
            "error": "subswap CLI is not installed on this host.",
            "fetched_at": time.time(),
        }

    args = [binary, "status", "--json", "--no-metrics"]
    if refresh:
        args.append("--refresh")

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if refresh and proc.returncode != 0 and _is_unknown_refresh_flag(proc.stderr or ""):
            proc = subprocess.run(
                [binary, "status", "--json", "--no-metrics"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "reason": "error",
            "error": "subswap status timed out.",
            "fetched_at": time.time(),
        }
    except OSError as exc:
        return {
            "available": False,
            "reason": "error",
            "error": str(exc),
            "fetched_at": time.time(),
        }

    if proc.returncode != 0:
        return {
            "available": False,
            "reason": "error",
            "error": _error_text(proc),
            "fetched_at": time.time(),
        }

    try:
        snapshot = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {
            "available": False,
            "reason": "error",
            "error": f"Invalid subswap JSON output: {exc.msg}",
            "fetched_at": time.time(),
        }

    if not isinstance(snapshot, dict) or "accounts" not in snapshot:
        return {
            "available": False,
            "reason": "error",
            "error": "Unexpected subswap JSON structure.",
            "fetched_at": time.time(),
        }

    return {
        "available": True,
        "snapshot": snapshot,
        "fetched_at": time.time(),
    }


def _payload_signature(payload: dict) -> str:
    """Content signature ignoring volatile packaging timestamps so idle polls don't broadcast."""
    normalized: dict = {
        "available": bool(payload.get("available")),
        "reason": payload.get("reason"),
        "error": payload.get("error"),
    }
    snap = payload.get("snapshot")
    if isinstance(snap, dict):
        filtered = {k: v for k, v in snap.items() if k != "generated_at"}
        normalized["snapshot"] = filtered
    return json.dumps(normalized, sort_keys=True, ensure_ascii=False)


class _InflightCall:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: dict | None = None
        self.error: BaseException | None = None


class QuotaHub:
    """Thread-safe, read-only quota aggregator with in-flight coalescing and watch polling."""

    def __init__(
        self,
        *,
        on_event: Callable[[str, dict], None] | None = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        fetcher: Callable[[bool], dict] | None = None,
    ) -> None:
        self._on_event = on_event
        self._poll_interval = max(0.05, float(poll_interval))
        self._fetcher = fetcher or (lambda refresh: fetch_subswap_quotas(refresh=refresh))
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._watchers = 0
        self._stop = False
        self._poll_thread: threading.Thread | None = None
        self._last_payload: dict | None = None
        self._last_signature: str | None = None
        self._inflight: dict[bool, _InflightCall] = {}

    def snapshot(self, *, refresh: bool = False) -> dict:
        """Return the current quota payload, coalescing concurrent runs."""
        want_refresh = bool(refresh)
        with self._lock:
            call = self._inflight.get(want_refresh)
            if call is None and not want_refresh:
                call = self._inflight.get(True)
                if call is not None:
                    want_refresh = True
            if call is None:
                call = _InflightCall()
                self._inflight[want_refresh] = call
                owner = True
            else:
                owner = False

        if not owner:
            call.event.wait()
            if call.error is not None:
                raise call.error
            return copy.deepcopy(call.result or {})

        try:
            payload = self._fetcher(want_refresh)
            sig = _payload_signature(payload)
            with self._lock:
                self._last_payload = copy.deepcopy(payload)
                self._last_signature = sig
                call.result = payload
            return copy.deepcopy(payload)
        except BaseException as exc:
            call.error = exc
            raise
        finally:
            with self._lock:
                self._inflight.pop(want_refresh, None)
                call.event.set()

    def watch_subscribe(self) -> tuple[bool, dict | None]:
        """Increment watcher count synchronously and report whether a cold refresh is needed."""
        with self._lock:
            self._watchers += 1
            cached = copy.deepcopy(self._last_payload)
            needs_cold = cached is None or not bool(cached.get("available"))
            self._ensure_poll_thread_locked()
            return needs_cold, cached

    def watch(self) -> dict:
        """Subscribe a quota watcher and return current state (refreshing on cold start)."""
        needs_cold, cached = self.watch_subscribe()
        if needs_cold:
            return self.snapshot(refresh=True)
        return cached or {}

    def unwatch(self) -> None:
        """Release a quota watcher; stops polling once watcher count reaches zero."""
        with self._cond:
            self._watchers = max(0, self._watchers - 1)
            self._cond.notify_all()

    def stop(self) -> None:
        """Stop background polling cleanly on daemon shutdown."""
        with self._cond:
            self._stop = True
            self._watchers = 0
            thread = self._poll_thread
            self._cond.notify_all()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _ensure_poll_thread_locked(self) -> None:
        if self._stop:
            return
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._poll_thread = threading.Thread(
                target=self._poll_loop,
                daemon=True,
                name="corral-quota-watch",
            )
            self._poll_thread.start()

    def _poll_loop(self) -> None:
        while True:
            with self._cond:
                if self._stop or self._watchers <= 0:
                    return
                self._cond.wait(timeout=self._poll_interval)
                if self._stop or self._watchers <= 0:
                    return
                prev_sig = self._last_signature

            try:
                payload = self.snapshot(refresh=False)
            except Exception:
                continue

            sig = _payload_signature(payload)
            if sig != prev_sig and self._on_event is not None:
                self._on_event(protocol.CH_QUOTAS, payload)
