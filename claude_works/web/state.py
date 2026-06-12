import asyncio
import threading
import time
from collections import defaultdict
from typing import Any


class _SlidingWindow:
    """Thread-safe sliding window counter per key (typically client IP)."""

    def __init__(self, limit: int, window: int) -> None:
        self._limit = limit
        self._window = window
        self._log: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """Record a hit. Returns True if within limit, False if exceeded."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            log = self._log[key]
            pruned = [t for t in log if t > cutoff]
            if len(pruned) >= self._limit:
                self._log[key] = pruned
                return False
            pruned.append(now)
            self._log[key] = pruned
            return True


# 120 API requests / 60 s per IP (general DoS protection)
api_limiter = _SlidingWindow(limit=120, window=60)
# 10 failed auth attempts / 300 s per IP (brute-force lockout)
auth_fail_limiter = _SlidingWindow(limit=10, window=300)

daemon_ref: Any = None
setup_token: str | None = None
cli_auth_proc: asyncio.subprocess.Process | None = None
runtime_cli_auth_proc: asyncio.subprocess.Process | None = None


def set_daemon(daemon: Any) -> None:
    global daemon_ref
    daemon_ref = daemon


def set_setup_token(token: str) -> None:
    global setup_token
    setup_token = token
