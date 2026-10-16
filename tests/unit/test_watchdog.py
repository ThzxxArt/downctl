"""Watchdog tests."""

from __future__ import annotations

import asyncio

import pytest

from downctl.core.errors import StallTimeout
from downctl.core.watchdog import watch


async def test_watchdog_returns_when_done() -> None:
    done = asyncio.Event()
    done.set()
    await asyncio.wait_for(
        watch(lambda: 0, 100, stall_limit=1, done=done),
        timeout=5,
    )


async def test_watchdog_returns_when_progress_reaches_expected() -> None:
    done = asyncio.Event()
    await asyncio.wait_for(
        watch(lambda: 100, 100, stall_limit=1, done=done),
        timeout=5,
    )


async def test_watchdog_raises_on_stall() -> None:
    done = asyncio.Event()
    stalls: list[int] = []
    with pytest.raises(StallTimeout):
        await asyncio.wait_for(
            watch(lambda: 0, 100, stall_limit=1, done=done, on_stall=stalls.append),
            timeout=5,
        )
    assert stalls == [1]
