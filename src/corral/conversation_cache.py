"""Byte-weighted retention for plain conversation previews.

``SessionStore.conversations`` is an acceleration cache, not a second copy of
user history. Before this module it was an unbounded dict: previewing many
sessions, or letting one-shot search preparation load every conversation,
retained each full message list for the life of the process. A read-only audit
of the current catalog measured 996 plain conversations at 14.9 MiB of traced
retention, duplicated by the TUI and the remote service; this module's
deliberately conservative estimator reports 16.6 MiB for the same rows.

The cache keeps the same mapping shape used by existing callers (``(version,
messages)`` tuples, with ``in``/``get``/``pop``/``clear``) but bounds retention
by a deterministic byte estimate and evicts least-recently-used entries. The
budget is internal acceleration policy, not a user setting: 8 MiB stays below
the measured full-catalog duplicate while retaining many hundreds of typical
previews. A conversation heavier than the whole budget is still returned
normally by the store, but is never retained as a permanent cache entry.
"""

from __future__ import annotations

import sys
import threading
from collections import OrderedDict
from collections.abc import Iterator, MutableMapping
from typing import Any

from corral.models import ConversationMessage

# Conservative estimator over that 996-row catalog: total 16.61 MiB, median
# 10.0 KiB, p99 136.7 KiB, maximum 263.1 KiB; unbounded traced retention was
# 14.91 MiB. 8 MiB caps roughly half the full duplicate while leaving ample
# room for interactive previews.
DEFAULT_CONVERSATION_CACHE_BYTES = 8 * 1024 * 1024

# Conservative per-message object cost: dataclass instance, its attribute
# storage, the timestamp and one list slot. Text/role byte sizes are added by
# ``_message_bytes`` from the live strings, so Unicode width is accounted for
# without serializing history. The audit measured about 176 B/message for
# short ASCII rows; this fixed part intentionally overestimates.
_MESSAGE_OBJECT_BYTES = 192


def _message_bytes(message: Any) -> int:
    """Approximate one message's retained bytes, including Unicode width."""
    size = _MESSAGE_OBJECT_BYTES
    size += sys.getsizeof(getattr(message, "role", ""))
    size += sys.getsizeof(getattr(message, "text", ""))
    return size


def estimate_conversation_bytes(messages: Any) -> int:
    """Approximate retained bytes for a conversation list.

    Uses ``sys.getsizeof`` on the message strings, so CJK/emoji text costs its
    real compact/wide representation rather than ``len()``. No JSON or other
    serialization is performed.
    """
    return sys.getsizeof(messages) + sum(_message_bytes(message) for message in messages)


class ConversationCache(MutableMapping[str, tuple[object, list[ConversationMessage]]]):
    """Thread-safe byte-budgeted LRU mapping used by ``SessionStore``.

    Reads promote an entry to most-recently-used; writes admit only when the
    estimated entry size fits the whole budget. Eviction is deterministic:
    least-recently-used entries go first. Oversized entries bypass retention
    without evicting healthy entries for a value that could never fit.
    """

    def __init__(self, max_bytes: int = DEFAULT_CONVERSATION_CACHE_BYTES) -> None:
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, tuple[object, list[ConversationMessage]]] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._total_bytes = 0
        self._max_bytes = max(0, int(max_bytes))

    @property
    def max_bytes(self) -> int:
        with self._lock:
            return self._max_bytes

    @max_bytes.setter
    def max_bytes(self, value: int) -> None:
        with self._lock:
            self._max_bytes = max(0, int(value))
            self._evict_to_budget()

    @property
    def total_bytes(self) -> int:
        """Estimated retained bytes; always <= ``max_bytes``."""
        with self._lock:
            return self._total_bytes

    def __getitem__(self, key: str) -> tuple[object, list[ConversationMessage]]:
        with self._lock:
            value = self._entries[key]
            self._entries.move_to_end(key)
            return value

    def __setitem__(self, key: str, value: tuple[object, list[ConversationMessage]]) -> None:
        version, messages = value
        with self._lock:
            # Copy the list so a caller mutating its own list cannot silently
            # change cached history; message objects are immutable dataclasses.
            retained = list(messages)
            size = estimate_conversation_bytes(retained)
            self._drop(key)
            if size > self._max_bytes:
                # Oversized values stay readable through the caller's return
                # value, but must not become permanent cache entries.
                return
            self._entries[key] = (version, retained)
            self._sizes[key] = size
            self._total_bytes += size
            self._evict_to_budget()

    def __delitem__(self, key: str) -> None:
        with self._lock:
            if key not in self._entries:
                raise KeyError(key)
            self._drop(key)

    def __contains__(self, key: object) -> bool:
        # Membership checks must not change LRU order.
        with self._lock:
            return key in self._entries

    def peek(self, key: str) -> tuple[object, list[ConversationMessage]] | None:
        """Return an entry without promoting it; for one-shot index readers."""
        with self._lock:
            return self._entries.get(key)

    def __iter__(self) -> Iterator[str]:
        # Snapshot keys: callers may write while iterating outside the lock.
        with self._lock:
            return iter(tuple(self._entries))

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def pop(self, key: str, *default: object) -> Any:
        if len(default) > 1:
            raise TypeError(f"pop expected at most 2 arguments, got {len(default) + 1}")
        with self._lock:
            if key not in self._entries:
                if default:
                    return default[0]
                raise KeyError(key)
            value = self._entries[key]
            self._drop(key)
            return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._sizes.clear()
            self._total_bytes = 0

    def _drop(self, key: str) -> None:
        """Remove one key and its accounting; caller holds the lock."""
        if self._entries.pop(key, None) is not None:
            self._total_bytes -= self._sizes.pop(key, 0)

    def _evict_to_budget(self) -> None:
        """Evict least-recently-used entries until within budget."""
        while self._total_bytes > self._max_bytes and self._entries:
            key, _value = self._entries.popitem(last=False)
            self._total_bytes -= self._sizes.pop(key, 0)
