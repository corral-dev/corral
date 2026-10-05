"""远程服务限流：配对尝试、输入、新建会话、建通道。

用简单的滑动窗口计数，不引第三方。命中后返回 False，调用方回 `rate_limited`。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    """每个 key 在 window 秒内最多 allow 次。"""

    def __init__(self, allow: int, window: float) -> None:
        self.allow = max(1, int(allow))
        self.window = float(window)
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow_request(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._events[key]
            cutoff = now - self.window
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.allow:
                return False
            bucket.append(now)
            return True

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)


# 默认限额：宽松到正常使用无感，紧到暴力尝试与刷屏拉不起
PAIR_ATTEMPTS = SlidingWindowLimiter(allow=8, window=60.0)
PAIR_ATTEMPTS_HOURLY = SlidingWindowLimiter(allow=30, window=3600.0)
INPUT_ACTIONS = SlidingWindowLimiter(allow=120, window=60.0)
# Live terminal typing from a desktop client (input.keys, and input.text with
# submit=false): one request per keystroke, so ordinary typing needs far more
# than a chat send. Submitted messages stay on INPUT_ACTIONS.
TERMINAL_TYPING = SlidingWindowLimiter(allow=1200, window=60.0)
SESSION_CREATE = SlidingWindowLimiter(allow=20, window=60.0)
# Channel opens. One reconnect race opens a socket per local hint for both the
# control and data planes, so a single global bucket of 16/min was exhausted by
# one reconnect storm and then refused the recovery itself. LAN opens are
# counted per peer address (loopback is never limited); relay opens cannot be
# told apart by address, so that bucket is per host and roomier. Unknown
# devices are still rejected at the handshake and pairing has its own limits.
LOCAL_CHANNEL_OPENS = SlidingWindowLimiter(allow=60, window=60.0)
RELAY_CHANNEL_OPENS = SlidingWindowLimiter(allow=120, window=60.0)
PUSH_REGISTER = SlidingWindowLimiter(allow=10, window=60.0)
