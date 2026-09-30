"""CI shard resource isolation for UI/Pilot tests (test-only helper).

No product code is touched. The runner (scripts/ci-test.py, owned by task A)
enters :func:`isolated_test_resources` in each worker process BEFORE importing
any test module (tests/test_ui.py reads CORRAL_CACHE_DIR at import time), runs
that shard's classes sequentially with plain unittest, then exits the context.

What one worker gets:

- a fresh ``fixture_root`` with ``CORRAL_CACHE_DIR`` pointed at it, so neither
  the developer's real ``~/.cache/corral`` state nor another worker's fixtures
  are read or modified;
- ``CORRAL_ISOLATE_MANAGED_HOSTS=1``, the same switch ci-test.py already sets,
  so managed-host discovery never enumerates live user sessions;
- a unique private tmux socket (``corral-ci-<pid>-<uuid8>``). Every tmux
  operation in the worker is routed to it, including the session-creation path
  (``embed.host_session`` → ``keepalive.tmux_argv()`` with no name,
  ``keepalive.ensure_server()``), which the acceptance.py hook points alone do
  not cover. Teardown kills ONLY this socket's server, never the product
  ``corral-keepalive`` socket or any user session.

Provenance: socket routing mirrors scripts/acceptance.py (prefix-routed
``socket_for_session``/``tmux_argv_for_session`` family); the private-server
lifecycle mirrors ``RealTerminalBindingTests`` in
tests/test_tui_embed_review.py (``tmux -L <sock> -f /dev/null`` + finally
``kill-server``).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from unittest import mock

#: Informational marker so tests can assert they run inside a shard worker.
WORKER_MARK_ENV = "CORRAL_CI_SHARD"

_SUBPROCESS_TIMEOUT = 10


@dataclass(frozen=True)
class ShardResources:
    """Handles published to the worker while the context is active."""

    fixture_root: str
    tmux_socket: str
    tmux_base_argv: tuple[str, ...]


def unique_socket_name(prefix: str = "corral-ci-shard") -> str:
    """A socket name no other worker or user session can collide with."""
    return (
        f"corral-ci-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        if prefix == "corral-ci-shard"
        else f"{prefix}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )


def _ensure_private_server(base_argv: tuple[str, ...]) -> None:
    """Best-effort start of the private server; tmux also auto-starts on demand."""
    if shutil.which("tmux") is None:
        return
    try:
        subprocess.run(
            (*base_argv, "start-server"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _socket_path(socket: str) -> str:
    """Filesystem path of a ``tmux -L <socket>`` server socket (same lookup tmux uses)."""
    base = os.environ.get("TMUX_TMPDIR", "/tmp")
    return os.path.join(base, f"tmux-{os.getuid()}", socket)


def _remove_stale_socket_file(socket: str) -> None:
    """Unlink the socket file iff no server answers on it. Scoped to our own name."""
    if shutil.which("tmux") is None:
        return
    try:
        probe = subprocess.run(
            ("tmux", "-L", socket, "list-sessions"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return
    if probe.returncode != 0:
        with contextlib.suppress(OSError):
            os.unlink(_socket_path(socket))


def _kill_private_server(base_argv: tuple[str, ...]) -> None:
    """Kill ONLY the private socket's server. Never touches another socket."""
    if shutil.which("tmux") is None:
        return
    try:
        subprocess.run(
            (*base_argv, "kill-server"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_SUBPROCESS_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


@contextlib.contextmanager
def isolated_test_resources(
    *,
    prefix: str = "corral-ci-shard",
) -> Iterator[ShardResources]:
    """Provide one worker process with private cache state and tmux namespace.

    Enters routing patches for the whole ``with`` body and undoes everything on
    exit (success, failure, or exception): private server killed, fixture root
    removed, environment restored, patches reverted. Pre-existing control
    channels for other names are left alone; only channels opened inside the
    body are closed.
    """
    from corral import embed as embed_mod
    from corral import keepalive, legacy_names, liveness

    prev_cache_dir = os.environ.get("CORRAL_CACHE_DIR")
    prev_isolated = os.environ.get("CORRAL_ISOLATE_MANAGED_HOSTS")
    prev_mark = os.environ.get(WORKER_MARK_ENV)
    fixture_root = tempfile.mkdtemp(prefix="corral-ci-shard-")
    socket = unique_socket_name(prefix)
    base_argv: tuple[str, ...] = ("tmux", "-L", socket, "-f", "/dev/null")
    resources = ShardResources(
        fixture_root=fixture_root,
        tmux_socket=socket,
        tmux_base_argv=base_argv,
    )

    os.environ["CORRAL_CACHE_DIR"] = fixture_root
    os.environ["CORRAL_ISOLATE_MANAGED_HOSTS"] = "1"
    os.environ[WORKER_MARK_ENV] = "1"

    def _private_argv(name: str | None = None) -> tuple[str, ...]:
        return base_argv

    def _private_socket(name: str | None = None) -> str:
        return socket

    def _ensure_private() -> None:
        _ensure_private_server(base_argv)

    def _no_reap(*args, **kwargs) -> list:
        # Pressure-reclaim is a product janitor for the shared socket. Under
        # isolation every session in this worker is a fixture and teardown
        # kills the whole private server, so there is nothing to reclaim.
        return []

    channels_before: set[str] = set(getattr(embed_mod, "_channels", {}))
    patches = (
        mock.patch.object(keepalive, "tmux_argv", new=_private_argv),
        mock.patch.object(keepalive, "tmux_argv_for_session", new=_private_argv),
        mock.patch.object(keepalive, "ensure_server", new=_ensure_private),
        mock.patch.object(keepalive, "reap_pressure", new=_no_reap),
        mock.patch.object(legacy_names, "tmux_argv_for_session", new=_private_argv),
        mock.patch.object(legacy_names, "socket_for_session", new=_private_socket),
        mock.patch.object(liveness, "tmux_argv_for_session", new=_private_argv),
        mock.patch.object(embed_mod, "socket_for_session", new=_private_socket),
    )
    for entered in patches:
        entered.start()
    try:
        _ensure_private_server(base_argv)
        yield resources
    finally:
        for entered in reversed(patches):
            entered.stop()
        try:
            for name in list(getattr(embed_mod, "_channels", {})):
                if name not in channels_before:
                    with contextlib.suppress(Exception):
                        embed_mod.close_channel(name)
        finally:
            _kill_private_server(base_argv)
            _remove_stale_socket_file(socket)
            shutil.rmtree(fixture_root, ignore_errors=True)
            if prev_cache_dir is None:
                os.environ.pop("CORRAL_CACHE_DIR", None)
            else:
                os.environ["CORRAL_CACHE_DIR"] = prev_cache_dir
            if prev_isolated is None:
                os.environ.pop("CORRAL_ISOLATE_MANAGED_HOSTS", None)
            else:
                os.environ["CORRAL_ISOLATE_MANAGED_HOSTS"] = prev_isolated
            if prev_mark is None:
                os.environ.pop(WORKER_MARK_ENV, None)
            else:
                os.environ[WORKER_MARK_ENV] = prev_mark


@contextlib.contextmanager
def private_tmux(*, prefix: str = "corral-ci-shard") -> Iterator[ShardResources]:
    """Self-isolation for a single real-tmux test without the runner.

    Same routing and teardown as :func:`isolated_test_resources`, but scoped to
    one ``with`` body so the two real-hosting test classes in tests/test_ui.py
    never touch the product socket even under today's serial runner.
    """
    with isolated_test_resources(prefix=prefix) as resources:
        yield resources


#: Classes in tests/test_ui.py PROVEN isolated by focused runs on a private
#: socket / pure mocks, safe for the runner to shard to parallel workers.
#: Absence from this set means serial remainder. Filled by task B only.
#:
#: Proven 2026-09-30 (each class run green in isolation with
#: CORRAL_ISOLATE_MANAGED_HOSTS=1 and a private CORRAL_CACHE_DIR; the two
#: real-tmux classes additionally ran on private sockets with byte-identical
#: product `corral-keepalive` session lists before/after and zero private
#: socket files left). Explicitly NOT included (stay serial):
#: OscProbeFlushTests (raw-thread timing probes around the 0.12s tmux settle
#: window) and MainScreenWorkerLifecycleTests (8.0s wall-budget asserts).
UI_SAFE_CLASSES: frozenset[str] = frozenset(
    {
        "test_ui.KittyKeyboardProtocolTests",
        "test_ui.InterruptTerminalRestoreTests",
        "test_ui.RuntimeThemeParserTests",
        "test_ui.PointerShapeSequenceTests",
        "test_ui.PointerShapeUiTests",
        "test_ui.AppThemeTests",
        "test_ui.SessionStoreFailureTests",
        "test_ui.SessionStoreRemoveSessionTests",
        "test_ui.SessionCardVisualTests",
        "test_ui.SidebarVisualLayoutTests",
        "test_ui.SidebarSplitHighlightTests",
        "test_ui.SidebarStripeTests",
        "test_ui.SessionGroupSidebarTests",
        "test_ui.MainScreenNavigationTests",
        "test_ui.MainScreenHostWorkerTests",
        "test_ui.PaneCellHeaderSyncTests",
        "test_ui.FooterActionGatingTests",
        "test_ui.FooterVersionTests",
        "test_ui.SidebarToggleTests",
        "test_ui.InputMaskFilterTests",
        "test_ui.MainScreenEmbedFlowTests",
        "test_ui.EmbedPaneWheelTests",
        "test_ui.EmbedPaneSelectionSpanTests",
        "test_ui.EmbedPaneSelectionStyleTests",
        "test_ui.EmbedPaneResizeTests",
        "test_ui.DirectLaunchHostingTests",
        "test_ui.RestartEndedSessionTests",
        "test_ui.RightPanePreviewTests",
        "test_ui.ModalTests",
        "test_ui.ModalOutsideClickTests",
        "test_ui.KillKeepaliveFlowTests",
        "test_ui.DeleteSessionFlowTests",
        "test_ui.DeleteSessionGroupFlowTests",
        "test_ui.ExternalRunningSessionTests",
        "test_ui.FullTextSearchModalTests",
        "test_ui.SessionHudSummaryTests",
        "test_ui.SessionHudRenderTests",
        "test_ui.SessionHudPlacementTests",
        "test_ui.SessionHudGatingTests",
        "test_ui.PreviewSustainWarmTests",
        "test_ui.ShellPaneTests",
        "test_ui.SidebarSnapshotTests",
    }
)

#: Small proven-safe classes the runner may co-locate SEQUENTIALLY in one
#: isolated worker (one isolation entry per worker, unchanged). Filled only
#: with classes proven green as a group in a single process; absent classes
#: stay one-class-per-worker. Proof: cloud-speed-isolation-proof.json
#: (permitted_groups, 21 classes / 96 cases green in 9.3s on current tree).
UI_GROUPABLE_CLASSES: frozenset[str] = frozenset(
    {
        "test_ui.KittyKeyboardProtocolTests",
        "test_ui.InterruptTerminalRestoreTests",
        "test_ui.RuntimeThemeParserTests",
        "test_ui.PointerShapeSequenceTests",
        "test_ui.PointerShapeUiTests",
        "test_ui.SessionStoreFailureTests",
        "test_ui.SessionStoreRemoveSessionTests",
        "test_ui.SessionCardVisualTests",
        "test_ui.PaneCellHeaderSyncTests",
        "test_ui.FooterActionGatingTests",
        "test_ui.SidebarToggleTests",
        "test_ui.InputMaskFilterTests",
        "test_ui.EmbedPaneWheelTests",
        "test_ui.EmbedPaneSelectionSpanTests",
        "test_ui.EmbedPaneSelectionStyleTests",
        "test_ui.SessionHudSummaryTests",
        "test_ui.SessionHudRenderTests",
        "test_ui.SessionHudGatingTests",
        "test_ui.PreviewSustainWarmTests",
        "test_ui.SidebarSnapshotTests",
        "test_ui.FooterVersionTests",
    }
)

#: Heavyweight proven classes the runner may split into N contiguous method-ID
#: batches (sorted IDs, deterministic; each batch runs sequentially with fresh
#: module import and private resources, running the class's real async
#: setUp/tearDown and asserts). Filled only after per-class semantic/resource
#: proof; absent classes are never method-split. Proof:
#: cloud-speed-isolation-proof.json (permitted_chunks; Navigation 16+16+15+15
#: and AppTheme 9+9+8+8 verified green as separate processes with disjoint ID
#: sets covering the full class). Timing-sensitive classes are never listed here.
UI_SPLITTABLE_CLASSES: dict[str, int] = {
    "test_ui.MainScreenNavigationTests": 4,
    "test_ui.AppThemeTests": 4,
}
