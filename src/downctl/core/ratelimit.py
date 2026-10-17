"""Token-bucket rate limiter shared across concurrent piece fetchers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class RateLimiter:
    """Approximate a target bytes/second across all callers.

    ``rate`` is bytes per second; ``None`` disables limiting entirely (a
    no-op fast path so the hot loop costs nothing when unthrottled).

    ``wait`` accepts an optional ``touch`` callback invoked on every polling
    step so a stall watchdog can tell a deliberate throttle wait from a real
    stall.
    """

    def __init__(self, rate: float | None) -> None:
        self.rate = rate
        self._bucket = 0.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def wait(self, size: float, touch: Callable[[], None] | None = None) -> None:
        if not self.rate:
            return
        async with self._lock:
            self._bucket += size
            while self._bucket >= self.rate:
                if touch is not None:
                    touch()
                elapsed = time.monotonic() - self._last
                if elapsed > 0:
                    self._bucket -= self.rate * elapsed
                    self._last = time.monotonic()
                if self._bucket < self.rate:
                    break
                await asyncio.sleep(min(0.1, self._bucket / self.rate))
