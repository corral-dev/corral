#!/usr/bin/env python3
"""Isolated behavioral acceptance for the sidebar group-close path.

Runs a repeatable focused probe without touching the developer's real data:

- unique tmux socket + session names on an isolated server,
- disposable ``CORRAL_CACHE_DIR`` and managed-host isolation,
- synthetic fixtures only (``live=False``, relative mtimes, never ages out),
- real tmux capture/liveness through routed socket hooks, with no capture mocks,
- real ``_PaneClose`` clicks for focused/unfocused 3->2 and 2->1 closes,
- maintained renderer screenshots (``App.save_screenshot``) before/after,
- cleanup of only the owned socket/temp dirs on success, failure, interrupt.

Usage (from ``cli/``)::

    python3 scripts/acceptance.py --json --artifacts-dir <dir>
    python3 scripts/acceptance.py --dry-run --json

Exit codes: 0 success, 1 setup/product-assertion failure, 2 usage, 6 timeout.
With ``--json`` stdout is a uniform ``{ok, data, error, meta}`` envelope.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 6

FIXTURE_COUNT = 50
GROUP_SIZE = 3
TMUX_TIMEOUT = 10.0
CAPTURE_MARKER_WAIT = 15.0
DEFAULT_TIMEOUT = 180.0

CATEGORIES = ("setup", "product_assertion", "timeout")


def socket_name() -> str:
    """Unique tmux socket name for this run (never the shared keepalive one)."""
    return f"corral-accept-{os.getpid()}-{secrets.token_hex(3)}"


def backend_names(run_id: str) -> list[str]:
    """Session names owned by this run; no managed ``corral-`` prefix."""
    return [f"acc-{run_id}-{i}" for i in range(GROUP_SIZE)]


def days_ago(mtime: float, now: float, live: bool = False) -> int:
    """Local calendar days between activity and ``now`` (mirror of sidebar rule).

    Live sessions always count as today (0); invalid timestamps fall into the
    unlabeled older bucket.
    """
    if live:
        return 0
    if mtime <= 0:
        return 7
    now_date = datetime.fromtimestamp(now).date()
    then_date = datetime.fromtimestamp(mtime).date()
    return max(0, (now_date - then_date).days)


def build_envelope(ok: bool, data=None, error=None, meta=None) -> dict:
    """Uniform agent-facing result envelope (same shape for dry-run and real)."""
    return {
        "ok": ok,
        "data": data,
        "error": error,
        "meta": meta or {},
    }


def fail_envelope(category: str, message: str, hint: str = "") -> dict:
    """Failure envelope with a stable machine-readable category."""
    if category not in CATEGORIES:
        category = "setup"
    return build_envelope(
        False, None,
        {"code": category, "message": message, "hint": hint},
        {},
    )


def tmux(socket: str, *args: str, timeout: float = TMUX_TIMEOUT) -> subprocess.CompletedProcess:
    """Run tmux against only our isolated socket (never the shared one)."""
    return subprocess.run(
        ["tmux", "-L", socket, *args],
        capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "TERM": "xterm-256color"},
    )


def tmux_alive(socket: str, name: str) -> bool:
    """True when our backend session still exists on our socket."""
    try:
        return tmux(socket, "has-session", "-t", name).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def tmux_capture(socket: str, name: str) -> str | None:
    """Raw screen capture of our synthetic backend (None when unavailable)."""
    try:
        proc = tmux(socket, "capture-pane", "-p", "-e", "-t", name)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def tmux_socket_candidates(socket: str) -> list[Path]:
    """Possible filesystem paths of our isolated socket (tmux resolution first).

    tmux resolves the socket dir from ``TMUX_TMPDIR`` when set, else the
    per-uid dir under ``/tmp``. Check every candidate; only exact-name matches
    under our run prefix are ever removed.
    """
    candidates = []
    override = os.environ.get("TMUX_TMPDIR")
    if override:
        candidates.append(Path(override) / f"tmux-{os.getuid()}" / socket)
        candidates.append(Path(override) / socket)
    candidates.append(Path(f"/tmp/tmux-{os.getuid()}") / socket)
    return candidates


def tmux_server_alive(socket: str) -> bool:
    """True when any server still answers on our socket."""
    try:
        return tmux(socket, "list-sessions").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cleanup_socket(socket: str, log=None) -> None:
    """Kill our server, then remove its leftover socket file (tmux keeps it).

    Only ever touches the exact socket name this run created; anything else
    (shared keepalive, other agents' sockets) is left alone.
    """
    if not socket.startswith("corral-accept-"):
        return
    try:
        tmux(socket, "kill-server", timeout=5.0)
    except Exception:  # noqa: BLE001 — best effort only
        pass
    removed: list[str] = []
    for _ in range(6):
        # A server that is still exiting may briefly reappear; re-unlink until
        # it stays gone, but never touch anything outside our run prefix.
        for candidate in tmux_socket_candidates(socket):
            try:
                if candidate.exists() or candidate.is_socket():
                    candidate.unlink(missing_ok=True)
                    removed.append(str(candidate))
            except OSError:
                pass
        leftovers = [str(p) for p in tmux_socket_candidates(socket) if p.exists()]
        if not leftovers and not tmux_server_alive(socket):
            break
        time.sleep(0.5)
    if log is not None:
        leftovers = [str(p) for p in tmux_socket_candidates(socket) if p.exists()]
        log.write(f"socket_cleaned {socket} removed={removed} leftovers={leftovers}\n")
        log.flush()


def route_socket(socket: str, prefix: str, name: str | None, fallback) -> str:
    """Routing hook: our run-prefixed sessions resolve to the isolated socket.

    Everything else falls through to the maintained resolver untouched, so the
    probe can never redirect foreign sessions.
    """
    if name and name.startswith(prefix):
        return socket
    return fallback(name)


def route_argv(socket: str, prefix: str, name: str | None, fallback_argv) -> tuple[str, ...]:
    """Same routing for full tmux argv (capture, channels, resize, liveness)."""
    if name and name.startswith(prefix):
        return ("tmux", "-L", socket)
    return fallback_argv(name)


class AcceptanceError(Exception):
    """Failure with a stable category for the JSON envelope."""

    def __init__(self, category: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.category = category if category in CATEGORIES else "setup"
        self.hint = hint


def check_available() -> None:
    """Precondition: required tools exist before doing anything else."""
    if shutil.which("tmux") is None:
        raise AcceptanceError("setup", "tmux not found on PATH",
                              "Install tmux to run the real-terminal scenarios.")
    try:
        import textual  # noqa: F401
    except ImportError as exc:
        raise AcceptanceError("setup", f"textual not importable: {exc}",
                              "Prepare the documented dev environment first.") from exc
    try:
        import cairosvg  # noqa: F401
    except ImportError as exc:
        raise AcceptanceError("setup", f"cairosvg not importable: {exc}",
                              "PNG evidence needs cairosvg.") from exc


async def _wait_until(predicate, *, deadline: float, interval: float = 0.05) -> None:
    while True:
        try:
            if predicate():
                return
        except Exception:  # noqa: BLE001 — keep polling until the deadline
            pass
        if time.monotonic() > deadline:
            raise AcceptanceError("timeout", "Timed out waiting for UI condition.")
        await asyncio.sleep(interval)


def _fixture_sessions(marker: str, member_names: list[str], count: int = FIXTURE_COUNT) -> list[dict]:
    """Synthetic store fixtures (mock data, real terminal behavior elsewhere).

    Every fixture uses ``live=False`` with mtimes relative to now so date
    buckets are controlled and cannot age out. Group members carry the REAL
    isolated backend tmux names; filler sessions carry no backend at all.
    """
    now = time.time()
    sessions = []
    for i in range(count):
        sessions.append({
            "source": "claude", "id": f"accs{i}", "short_id": f"accs{i}",
            "mtime": now - i * 60, "size_bytes": 1, "size_kb": 1,
            "native_title": None, "fallback_title": f"Accept fixture {i} {marker}",
            "cwd": "/tmp", "live": False,
        })
    for session, backend in zip(sessions[25:25 + GROUP_SIZE], member_names, strict=True):
        session["keepalive_name"] = backend
    return sessions


async def _run_ui_probe(
    marker: str, run_id: str, socket: str, backend_names: list[str],
    artifacts: Path, deadline: float, log,
) -> dict:
    """Headless UI probe using real isolated tmux capture and liveness.

    Raw tmux preconditions verify each synthetic backend's live process and
    screen marker. Maintained routing hooks redirect capture, control channels,
    resize, and liveness for this run's names to its unique socket; all other
    names retain their original resolver and are never redirected.
    """
    # ruff: E402 — isolation env must be set before corral imports resolve state.
    from unittest import mock  # noqa: E402

    import corral  # noqa: E402
    from corral import embed as embed_mod  # noqa: E402
    from corral import (  # noqa: E402
        i18n,
        keepalive,
        legacy_names,
        liveness,
    )
    from corral import split_layout as split_layout_mod  # noqa: E402
    from corral.ui.app import CorralApp  # noqa: E402
    from corral.ui.session_list import SessionListView  # noqa: E402
    from corral.ui.split_pane_area import SplitPaneArea, _PaneClose  # noqa: E402

    def note(text: str) -> None:
        log.write(text + "\n")
        log.flush()

    prefix = f"acc-{run_id}-"
    _orig_argv = legacy_names.tmux_argv_for_session
    _orig_sock = legacy_names.socket_for_session

    def _routed_argv(name=None):
        return route_argv(socket, prefix, name, _orig_argv)

    def _routed_sock(name=None):
        return route_socket(socket, prefix, name, _orig_sock)

    i18n.set_lang("en")
    sessions = _fixture_sessions(marker, backend_names)
    expected_count = len(sessions)

    claude = mock.Mock()
    claude.id = "claude"
    claude.display_name = "Claude"
    claude.is_available.return_value = True
    claude.scan_sessions.return_value = sessions
    claude.load_conversation.return_value = [
        corral.ConversationMessage("user", f"Accept probe question {marker}"),
        corral.ConversationMessage("assistant", f"Accept probe reply {marker}"),
    ]
    registry = corral.RuntimeRegistry((claude,))
    with mock.patch.object(corral.titles, "load_cache", return_value={}):
        store = corral.SessionStore(limit=100, registry=registry)
        store.load()
    actual_count = len(store.all_sessions())
    if actual_count != expected_count:
        raise AcceptanceError(
            "setup",
            f"Fixture count mismatch: expected {expected_count}, got {actual_count}.",
        )
    note(f"fixture_count={actual_count}")

    member_sessions = sessions[25:25 + GROUP_SIZE]
    member_keys = [corral.session_key(session) for session in member_sessions]
    store.limit = 100
    # Record the hosted identity the way real hosting does (plain fixture data,
    # no behavior mocks): activeness below is then proven by real tmux.
    for key, backend in zip(member_keys, backend_names, strict=True):
        store.hosted[key] = backend
    note(f"hosted={dict(store.hosted)}")

    results: dict = {
        "fixture_count": actual_count,
        "routed_prefix": prefix,
        "routed_socket": socket,
        "scenarios": [],
        "scroll_samples": {},
        "screenshots": {},
        "screenshot_bytes": {},
    }

    def save_frame(tag: str, app) -> str:
        svg_name = f"accept-{tag}.svg"
        saved = app.save_screenshot(svg_name, path=str(artifacts))
        svg_path = artifacts / Path(str(saved)).name
        if not svg_path.is_file() or svg_path.stat().st_size == 0:
            raise AcceptanceError("setup", f"Screenshot renderer produced no SVG for {tag}.")
        png_path = svg_path.with_suffix(".png")
        # Same conversion call the maintained docs/screenshots helper uses
        # (its _svg_to_png is cairosvg.svg2png); README cosmetics from that
        # script (chrome stripping, emoji embedding) are deliberately NOT
        # applied so the evidence keeps actual rendering fidelity.
        from cairosvg import svg2png
        try:
            svg2png(url=str(svg_path), write_to=str(png_path))
        except Exception as exc:
            raise AcceptanceError("setup", f"PNG conversion failed for {tag}: {exc}.") from exc
        if not png_path.is_file() or png_path.stat().st_size == 0:
            raise AcceptanceError("setup", f"PNG conversion produced no output for {tag}.")
        results["screenshot_bytes"][tag] = png_path.stat().st_size
        return str(png_path)

    def grids_show_marker(area) -> list[bool]:
        """Which mounted panes currently render the real synthetic marker."""
        shown = []
        for cell in area.cells():
            pane = cell.embed_pane()
            try:
                text = pane.render().plain if pane is not None else ""
            except Exception:  # noqa: BLE001 — treat render failure as not shown
                text = ""
            shown.append(marker in text)
        return shown

    async def wait_marker(area, what: str) -> None:
        """Wait until every mounted pane grid shows the real backend marker."""
        while True:
            cells = area.cells()
            shown = grids_show_marker(area)
            if cells and len(shown) == len(cells) and all(shown):
                note(f"grid_markers_{what}={shown}")
                return
            if time.monotonic() > deadline:
                raise AcceptanceError(
                    "timeout",
                    f"UI pane grids never showed marker for {what}: {shown}.")
            await asyncio.sleep(0.1)

    async def open_group(pilot, app, focus_idx: int):
        area = app.screen.query_one(SplitPaneArea)
        app.screen._apply_layout_change(  # noqa: SLF001
            lambda s: s.set_group("/tmp", member_keys, focus_key=member_keys[focus_idx])
        )
        area.show_hosted_group(
            "/tmp",
            [(session, session.get("keepalive_name"), lambda: "")
             for session in member_sessions],
            focus_key=member_keys[focus_idx],
        )
        await _wait_until(
            lambda: len(area.cells()) == GROUP_SIZE
            and all(cell.embed_pane() is not None for cell in area.cells()),
            deadline=deadline,
        )
        await pilot.pause()
        await wait_marker(area, f"open-focus-{focus_idx}")
        return area

    async def run_close_scenario(
        pilot, app, view, scroll, *, name: str, focus_idx: int,
        close_idx: int | None, close_picker, preset_close_idx: int | None,
        expected_remaining: int,
    ) -> None:
        """One close scenario: shots immediately around the click, then asserts."""
        area = await open_group(pilot, app, focus_idx)
        if preset_close_idx is not None:
            preset_btn = area.cells()[preset_close_idx].query_one(_PaneClose)
            await pilot.click(preset_btn)
            await _wait_until(lambda: len(area.cells()) == expected_remaining + 1,
                              deadline=deadline)
            await pilot.pause(delay=0.2)
        app.screen._cancel_follow_selection()  # noqa: SLF001
        app.screen._suppress_selection_follow += 1  # noqa: SLF001
        await view.rebuild(select_key=area.focus_key)
        await pilot.pause()
        app.screen._suppress_selection_follow -= 1  # noqa: SLF001
        if scroll.max_scroll_y <= 0:
            raise AcceptanceError("setup", "Sidebar is not scrollable; cannot sample scroll.")
        scroll.scroll_to(y=45, animate=False, immediate=True)
        await pilot.pause()
        before = scroll.scroll_y
        if before <= 0:
            raise AcceptanceError("setup", "Sidebar did not reach a scrolled offset.")
        shown = grids_show_marker(area)
        if not shown or not all(shown):
            raise AcceptanceError(
                "product_assertion",
                f"Panes do not all render the real marker before close: {shown}.")
        shot_before = save_frame(f"{name}-before", app)
        idx = close_idx if close_idx is not None else close_picker(area)
        pre_keys = list(area.ordered_session_keys())
        closed_key = pre_keys[idx]
        button = area.cells()[idx].query_one(_PaneClose)
        await pilot.click(button)
        await _wait_until(lambda: len(area.cells()) == expected_remaining,
                          deadline=deadline)
        await pilot.pause(delay=0.2)
        shot_after = save_frame(f"{name}-after", app)
        expected_scroll = min(before, scroll.max_scroll_y)
        samples = []
        for _ in range(3):
            await pilot.pause(delay=0.2)
            samples.append(scroll.scroll_y)
        results["scroll_samples"][f"{name}_post"] = samples
        selected = view.selected_session()
        selected_key = corral.session_key(selected) if selected else None
        remaining = list(area.ordered_session_keys())
        preserved = all(sample == expected_scroll for sample in samples)
        results["scenarios"].append({
            "name": name,
            "click_accepted": True,
            "closed_key": closed_key,
            "remaining_keys": remaining,
            "focus_key": area.focus_key,
            "selected_key": selected_key,
            "grid_markers_before_close": shown,
            "scroll_before": before,
            "scroll_samples_after": samples,
            "scroll_expected": expected_scroll,
            "scroll_preserved": preserved,
            "shot_before": shot_before,
            "shot_after": shot_after,
        })
        results["screenshots"][f"{name}-before"] = shot_before
        results["screenshots"][f"{name}-after"] = shot_after
        if remaining != [key for key in pre_keys if key != closed_key]:
            raise AcceptanceError(
                "product_assertion",
                f"{name}: remaining {remaining} is not pre-close minus {closed_key}.")
        if selected_key != area.focus_key:
            raise AcceptanceError(
                "product_assertion", f"{name}: surviving selection does not match focus.")
        if not preserved:
            raise AcceptanceError(
                "product_assertion",
                f"{name}: sidebar scroll moved after close: "
                f"before={before} samples={samples} expected={expected_scroll}.")

    def unfocused_picker(area) -> int:
        keys = area.ordered_session_keys()
        return 1 if area.focus_key == keys[0] else 0

    note(f"routed_prefix={prefix} socket={socket}")
    with (
        mock.patch.object(legacy_names, "tmux_argv_for_session", new=_routed_argv),
        mock.patch.object(legacy_names, "socket_for_session", new=_routed_sock),
        mock.patch.object(keepalive, "tmux_argv_for_session", new=_routed_argv),
        mock.patch.object(liveness, "tmux_argv_for_session", new=_routed_argv),
        mock.patch.object(embed_mod, "socket_for_session", new=_routed_sock),
    ):
        app = CorralApp(store, embed_ok=True)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause(delay=0.5)
            view = app.screen.query_one(SessionListView)
            scroll = view.query_one("#sidebar-scroll")
            if not scroll.is_mounted:
                raise AcceptanceError("setup", "Sidebar scroll viewport not mounted.")
            results["scroll_samples"]["viewport_mounted"] = True
            results["scroll_samples"]["max_scroll_y"] = scroll.max_scroll_y

            await run_close_scenario(
                pilot, app, view, scroll,
                name="s1-unfocused-3-to-2", focus_idx=0,
                close_idx=2, close_picker=None, preset_close_idx=None,
                expected_remaining=2,
            )
            await run_close_scenario(
                pilot, app, view, scroll,
                name="s2-focused-3-to-2", focus_idx=0,
                close_idx=0, close_picker=None, preset_close_idx=None,
                expected_remaining=2,
            )
            await run_close_scenario(
                pilot, app, view, scroll,
                name="s3-unfocused-2-to-1", focus_idx=0,
                close_idx=None, close_picker=unfocused_picker,
                preset_close_idx=0, expected_remaining=1,
            )

            # Scrolling still works after closes (sampled functional check).
            app.screen._suppress_selection_follow += 1  # noqa: SLF001
            view.index = view._sticky_count()  # noqa: SLF001
            await pilot.pause()
            top_y = scroll.scroll_y
            view.index = len(view.list_children) - 1
            await pilot.pause()
            end_y = scroll.scroll_y
            app.screen._suppress_selection_follow -= 1  # noqa: SLF001
            results["scroll_samples"]["top_y"] = top_y
            results["scroll_samples"]["end_y"] = end_y
            if top_y != 0 or end_y <= 0:
                raise AcceptanceError(
                    "product_assertion",
                    f"Scrolling broken after closes: top={top_y} end={end_y}.")

    split_layout_mod.reset_default_layout_db()
    return results


def start_backends(socket: str, names: list[str], marker: str, log) -> None:
    """Start one synthetic backend per group member on our socket only."""
    for name in names:
        cmd = f"printf '{marker} {name}\\n{marker} {name}\\n{marker} {name}\\n'; sleep 240"
        proc = tmux(socket, "-f", "/dev/null", "new-session", "-d",
                    "-s", name, "-x", "120", "-y", "30", "--", "sh", "-c", cmd)
        if proc.returncode != 0:
            raise AcceptanceError("setup", f"Cannot start backend {name}: {proc.stderr.strip()}")
        log.write(f"backend_started {name}\n")
    log.flush()
    deadline = time.monotonic() + CAPTURE_MARKER_WAIT
    for name in names:
        while True:
            text = tmux_capture(socket, name)
            if text is not None and marker in text and tmux_alive(socket, name):
                log.write(f"backend_verified {name}\n")
                log.flush()
                break
            if time.monotonic() > deadline:
                raise AcceptanceError(
                    "timeout", f"Backend {name} never showed marker {marker!r}.")
            time.sleep(0.2)


def verify_backends_alive(socket: str, names: list[str]) -> list[str]:
    """Return the subset of our backends that are still alive."""
    return [name for name in names if tmux_alive(socket, name)]


def run_acceptance(artifacts: Path, timeout: float, log) -> dict:
    """Full isolated run: real backends first, then the headless UI probe."""
    started = time.monotonic()
    deadline = started + timeout
    run_id = secrets.token_hex(4)
    marker = f"ACCEPT-{run_id}"
    socket = socket_name()
    names = backend_names(run_id)

    def cleanup() -> None:
        cleanup_socket(socket, log)

    cleanup_done = False
    try:
        check_available()
        artifacts.mkdir(parents=True, exist_ok=True)
        start_backends(socket, names, marker, log)
        ui = asyncio.run(_run_ui_probe(
            marker, run_id, socket, names, artifacts, deadline, log))
        alive = verify_backends_alive(socket, names)
        ui["backends_alive"] = alive
        ui["backends_expected"] = names
        if alive != names:
            raise AcceptanceError(
                "product_assertion",
                f"Backends missing after UI closes: alive={alive} expected={names}.",
                "Closing a pane must not stop the hosted backend.",
            )
        cleanup()
        proof = {
            "server_stopped": not tmux_server_alive(socket),
            "socket_files": [
                str(path) for path in tmux_socket_candidates(socket) if path.exists()
            ],
        }
        ui["cleanup_proof"] = proof
        if not proof["server_stopped"] or proof["socket_files"]:
            raise AcceptanceError("setup", f"Cleanup proof failed: {proof}.")
        ui["elapsed_s"] = round(time.monotonic() - started, 1)
        return ui
    finally:
        if not cleanup_done:
            cleanup()


def parse_args(argv: list[str]) -> argparse.Namespace:
    """CLI surface: JSON envelope, dry-run, artifacts dir, timeout."""
    parser = argparse.ArgumentParser(
        description="Isolated sidebar group-close acceptance (synthetic fixtures only).")
    parser.add_argument("--json", action="store_true",
                        help="Emit the uniform result envelope on stdout.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan without creating tmux state or running the UI.")
    parser.add_argument("--artifacts-dir", default="",
                        help="Directory for logs/screenshots (default: temp dir).")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="Overall deadline in seconds.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point honoring the exit-code contract (0/1/2/6)."""
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code == 2 else int(exc.code or 0)

    if args.dry_run:
        plan = {
            "dry_run": True,
            "socket_pattern": "corral-accept-<pid>-<rand>",
            "backends": GROUP_SIZE,
            "fixture_count": FIXTURE_COUNT,
            "scenarios": ["s1-unfocused-3-to-2", "s2-focused-3-to-2", "s3-unfocused-2-to-1"],
            "terminal_behavior": "real tmux capture/liveness via routed socket hooks",
            "capture_mocked": False,
            "reads_real_sessions": False,
            "touches_shared_socket": False,
        }
        envelope = build_envelope(True, plan, None, {"dry_run": True})
        print(json.dumps(envelope, ensure_ascii=False))
        return EXIT_OK

    artifacts = Path(args.artifacts_dir) if args.artifacts_dir else Path(
        tempfile.mkdtemp(prefix="corral-accept-"))
    artifacts.mkdir(parents=True, exist_ok=True)
    log_path = artifacts / "run.log"
    started = time.monotonic()
    try:
        # Isolation first: disposable cache/home plus managed-host isolation so
        # the probe can never see or modify the developer's real sessions.
        cache_dir = tempfile.mkdtemp(prefix="corral-accept-cache-")
        os.environ["CORRAL_CACHE_DIR"] = cache_dir
        os.environ["CORRAL_ISOLATE_MANAGED_HOSTS"] = "1"
        os.environ.pop("NO_COLOR", None)
        os.environ.setdefault("COLORTERM", "truecolor")
        os.environ.setdefault("CORRAL_LANG", "en")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        with open(log_path, "w", encoding="utf-8") as log:
            log.write(f"artifacts={artifacts}\n")
            data = run_acceptance(artifacts, args.timeout, log)
        elapsed = round(time.monotonic() - started, 1)
        envelope = build_envelope(
            True, data, None,
            {"elapsed_s": elapsed, "log": str(log_path),
             "artifacts": str(artifacts)},
        )
        print(json.dumps(envelope, ensure_ascii=False))
        return EXIT_OK
    except AcceptanceError as exc:
        elapsed = round(time.monotonic() - started, 1)
        envelope = build_envelope(
            False, None,
            {"code": exc.category, "message": str(exc), "hint": exc.hint},
            {"elapsed_s": elapsed, "log": str(log_path),
             "artifacts": str(artifacts)},
        )
        print(json.dumps(envelope, ensure_ascii=False))
        return EXIT_TIMEOUT if exc.category == "timeout" else EXIT_FAIL
    except KeyboardInterrupt:
        envelope = fail_envelope("timeout", "Interrupted by user.")
        print(json.dumps(envelope, ensure_ascii=False))
        return EXIT_TIMEOUT
    finally:
        try:
            cache = os.environ.get("CORRAL_CACHE_DIR", "")
            if cache.startswith(tempfile.gettempdir()) and "corral-accept-cache-" in cache:
                shutil.rmtree(cache, ignore_errors=True)
        except Exception:  # noqa: BLE001 — best effort only
            pass


if __name__ == "__main__":
    def _handle_signal(signum, _frame) -> None:  # noqa: ANN001, ANN202
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (OSError, ValueError):
        pass

    def _cleanup_shared_state() -> None:
        # Only our temp cache dir (guarded by prefix above); sockets die with run.
        pass

    atexit.register(_cleanup_shared_state)
    sys.exit(main())
