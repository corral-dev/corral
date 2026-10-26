"""On-demand history metadata, independent of the recent browsing window.

Native eligibility and record interpretation remain in the runtime scanners.
This catalog only owns search breadth, single-flight preparation and reuse.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from corral.cache import scan_period
from corral.models import session_key
from corral.runtime.registry import ACTIVE_RUNTIME_IDS
from corral.titles import resolve_initial_title

_REUSE_SECONDS = 30.0
_INITIAL_DEPTH = 128


class SearchCatalogError(RuntimeError):
    """Some runtimes could not be read; the retained snapshot is incomplete."""


class SearchCatalog:
    def __init__(self, registry) -> None:
        self._registry = registry
        self._lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._buckets: dict[str, tuple[object, int, list[dict]]] = {}
        self._sessions: dict[str, dict] = {}
        self.titles: dict[str, str] = {}
        self.ready = False
        self.failed = False
        self._prepared_at = float("-inf")

    @property
    def fresh(self) -> bool:
        return self.ready and not self.failed and time.monotonic() - self._prepared_at < _REUSE_SECONDS

    def sessions(self) -> list[dict]:
        return list(self._sessions.values())

    def find(self, key: str) -> dict | None:
        return self._sessions.get(key)

    def update_titles(self, title_cache: dict) -> None:
        self.titles = {key: resolve_initial_title(session, title_cache)[0]
                       for key, session in self._sessions.items()}

    def refresh(self, title_cache: dict, progress=None) -> None:
        """Prepare once for concurrent callers; never read conversation bodies.

        Each scanner returns at most the requested depth. Grow that depth until
        the scanner exhausts its eligible records, instead of choosing another
        arbitrary fixed search limit. Reuse unchanged runtime signatures on later
        reconciliations. Only complete successful reads replace a runtime bucket.
        """
        with self._lock:
            if self.fresh:
                return
            previous = self._buckets
            previous_sessions, previous_titles = self._sessions, self.titles
            runtimes = tuple(runtime for runtime in self._registry if runtime.id in ACTIVE_RUNTIME_IDS)

            def publish_partial(rows):
                if progress is None:
                    return
                partial = {session_key(row): dict(row) for row in rows}
                resolved = {key: resolve_initial_title(row, title_cache)[0]
                            for key, row in partial.items()}
                with self._publish_lock:
                    self.titles = self.titles | resolved
                    self._sessions = self._sessions | partial
                progress()

            if progress is not None and not self._sessions:
                # The shared scanner usually already has a wider recent window
                # than the TUI. Reuse it for immediate provisional matches, but
                # still exhaust native history before declaring completeness.
                from corral import scan_index

                meta = scan_index.published_meta()
                shared = scan_index.try_consume(meta[1]) if meta is not None else None
                if shared:
                    publish_partial([row for runtime in runtimes
                                     for row in shared.get(runtime.id, [])])

            def scan(runtime):
                old = previous.get(runtime.id)
                try:
                    # A first scan has nothing to validate or reuse. Some
                    # native signatures inspect a whole shared database; do not
                    # delay its first useful metadata batch with that work.
                    try:
                        signature = runtime.scan_signature() if old is not None else None
                    except Exception:
                        signature = None
                    if old is not None and signature is not None and signature == old[0]:
                        return runtime.id, old, None
                    depth = old[1] if old else _INITIAL_DEPTH
                    first = True
                    while True:
                        rows = runtime.scan_sessions(depth)
                        if first:
                            # Show useful early matches while the count still
                            # says preparation, never a false complete zero.
                            publish_partial(rows)
                            first = False
                        if len(rows) < depth:
                            return runtime.id, (signature, depth, rows), None
                        depth *= 2
                except Exception as exc:
                    return runtime.id, old, exc

            with scan_period(), ThreadPoolExecutor(max_workers=max(1, len(runtimes))) as pool:
                results = list(pool.map(scan, runtimes))
            failures = []
            buckets = dict(previous)
            for runtime_id, bucket, error in results:
                if bucket is not None:
                    buckets[runtime_id] = bucket
                if error is not None:
                    failures.append(runtime_id)
            sessions = {
                session_key(session): dict(session)
                for _signature, _depth, rows in buckets.values()
                for session in rows
            }
            self.titles = {
                key: resolve_initial_title(session, title_cache)[0]
                for key, session in sessions.items()
            }
            self.failed = bool(failures)
            if failures and self.ready:
                self._sessions, self.titles = previous_sessions, previous_titles
            else:
                self._buckets = buckets
                self._sessions = sessions
            if failures:
                raise SearchCatalogError("History search unavailable: " + ", ".join(failures))
            self.ready = True
            self._prepared_at = time.monotonic()
