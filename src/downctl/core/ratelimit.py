"""Strict, credit-free rate limiting shared across concurrent piece fetchers.

Every chunk is paced by its own duration (``size / rate``) before it is
released to the caller. There is deliberately **no token bucket and no idle
credit**: a limiter that saves unused budget while nothing is downloading will
burst at full speed after a retry backoff or an idle period, which is exactly
what ``--limit-rate`` must not do.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

#: Longest single sleep slice, so a stall watchdog still sees heartbeat calls.
SLICE = 0.1


class RateLimiter:
    """Approximate a target bytes/second across all callers.

    ``rate`` is bytes per second; ``None`` disables limiting entirely (a
    no-op fast path so the hot loop costs nothing when unthrottled).

    ``wait`` accepts an optional ``touch`` callback invoked on every polling
    step so a stall watchdog can tell a deliberate throttle wait from a real
    stall.
    """

    def __init__(self, rate: float | None) -> None:
        # Treat zero/negative as unlimited to avoid a busy spin.
        self.rate = rate if rate and rate > 0 else None
        self._lock = asyncio.Lock()

    async def wait(self, size: float, touch: Callable[[], None] | None = None) -> None:
        if not self.rate or size <= 0:
            return
        async with self._lock:
            remaining = size / self.rate
            while remaining > 0:
                if touch is not None:
                    touch()
                slice_time = min(SLICE, remaining)
                await asyncio.sleep(slice_time)
                remaining -= slice_time
