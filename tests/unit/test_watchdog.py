"""Watchdog tests."""

from __future__ import annotations

import asyncio

import pytest

from downctl.core.errors import StallTimeout
from downctl.core.watchdog import watch


async def test_watchdog_returns_when_done() -> None:
    done = asyncio.Event()
    done.set()
    started = asyncio.get_running_loop().time()
    assert (
        await asyncio.wait_for(
            watch(lambda: 0, 100, stall_limit=1, done=done),
            timeout=5,
        )
        is None
    )
    # An already-finished download must not wait out one stall interval.
    assert asyncio.get_running_loop().time() - started < 0.5


async def test_watchdog_returns_when_progress_reaches_expected() -> None:
    done = asyncio.Event()
    started = asyncio.get_running_loop().time()
    assert (
        await asyncio.wait_for(
            watch(lambda: 100, 100, stall_limit=1, done=done),
            timeout=5,
        )
        is None
    )
    assert asyncio.get_running_loop().time() - started < 0.5


async def test_watchdog_raises_on_stall() -> None:
    done = asyncio.Event()
    stalls: list[int] = []
    with pytest.raises(StallTimeout):
        await asyncio.wait_for(
            watch(lambda: 0, 100, stall_limit=1, done=done, on_stall=stalls.append),
            timeout=5,
        )
    assert stalls == [1]
