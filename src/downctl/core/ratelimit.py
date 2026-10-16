"""Token-bucket rate limiter shared across concurrent piece fetchers."""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Approximate a target bytes/second across all callers.

    ``rate`` is bytes per second; ``None`` disables limiting entirely (a
    no-op fast path so the hot loop costs nothing when unthrottled).
    """

    def __init__(self, rate: float | None) -> None:
        self.rate = rate
        self._bucket = 0.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def wait(self, size: float) -> None:
        if not self.rate:
            return
        async with self._lock:
            self._bucket += size
            while self._bucket >= self.rate:
                elapsed = time.monotonic() - self._last
                if elapsed > 0:
                    self._bucket -= self.rate * elapsed
                    self._last = time.monotonic()
                if self._bucket < self.rate:
                    break
                await asyncio.sleep(min(0.1, self._bucket / self.rate))
