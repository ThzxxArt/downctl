"""Stall watchdog: abort a stalled transfer so the engine can resume.

The watchdog is a member of the download ``TaskGroup``. It raises
:class:`~downctl.core.errors.StallTimeout` (a plain exception, never
``CancelledError``) so it reliably tears the group down on every Python
version.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from downctl.core.errors import StallTimeout

DEFAULT_STALL_LIMIT = 30


async def watch(
    progress: Callable[[], int],
    expected: int,
    *,
    stall_limit: int = DEFAULT_STALL_LIMIT,
    done: asyncio.Event,
    heartbeat: Callable[[], float] | None = None,
    on_stall: Callable[[int], None] | None = None,
) -> None:
    """Wait until ``done`` is set or progress stalls for ``stall_limit`` seconds.

    ``progress`` returns the cumulative byte count. ``done`` is set once all
    piece tasks have finished. When ``heartbeat`` is given it returns a
    monotonic timestamp updated on *any* activity (including deliberate
    rate-limit waits), which is used for stall detection so a throttled
    download is not mistaken for a stalled one.
    """
    last = progress()
    last_ts = time.monotonic()
    while True:
        if expected > 0 and progress() >= expected:
            return
        try:
            await asyncio.wait_for(done.wait(), timeout=1)
            return
        except TimeoutError:
            pass
        now = time.monotonic()
        if heartbeat is not None:
            beat = heartbeat()
            if beat > last_ts:
                last_ts = beat
        else:
            current = progress()
            if current > last:
                last = current
                last_ts = now
        if now - last_ts >= stall_limit:
            if on_stall is not None:
                on_stall(stall_limit)
            raise StallTimeout(stall_limit)
