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
    on_stall: Callable[[int], None] | None = None,
) -> None:
    """Wait until ``done`` is set or progress stalls for ``stall_limit`` seconds.

    ``progress`` returns the cumulative byte count. ``done`` is set once all
    piece tasks have finished (success, failure or unsupported Range) so the
    watchdog retires promptly instead of waiting out the stall limit.
    """
    last = progress()
    last_ts = time.monotonic()
    while True:
        await asyncio.sleep(1)
        if done.is_set():
            return
        if expected > 0 and progress() >= expected:
            return
        current = progress()
        now = time.monotonic()
        if current > last:
            last = current
            last_ts = now
        elif now - last_ts >= stall_limit:
            if on_stall is not None:
                on_stall(stall_limit)
            raise StallTimeout(stall_limit)
