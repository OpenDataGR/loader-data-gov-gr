"""A small client-side token bucket.

Enforced BEFORE sending, not just reacted to after a 429 -- protects
provider systems that may be modestly-provisioned internal services, not
internet-scale APIs built to absorb bursts.
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, max_per_minute: int, *, clock=time.monotonic, sleep=time.sleep):
        if max_per_minute <= 0:
            raise ValueError("max_per_minute must be positive")
        self._capacity = max_per_minute
        self._tokens = float(max_per_minute)
        self._refill_per_second = max_per_minute / 60.0
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)

    def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                deficit = 1 - self._tokens
                wait_s = deficit / self._refill_per_second
            self._sleep(wait_s)
