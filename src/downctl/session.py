"""The shared ``aiohttp`` session factory.

Builds the client session used by the runtime context. The socket read
timeout is kept longer than the stall watchdog so a stalled transfer is torn
down gracefully (and resumable) instead of hitting a hard timeout.
"""

from __future__ import annotations

import aiohttp

from downctl import __version__

DEFAULT_STALL_LIMIT = 30


def make_session(
    proxy: str = "",
    token: str = "",
    *,
    stall_limit: int = DEFAULT_STALL_LIMIT,
    user_agent: str = "",
) -> aiohttp.ClientSession:
    """Create a client session with sane timeouts and downctl's User-Agent."""
    headers = {"User-Agent": user_agent or f"downctl/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=15,
        sock_read=max(60, stall_limit * 3),
    )
    if proxy:
        return aiohttp.ClientSession(headers=headers, timeout=timeout, trust_env=True, proxy=proxy)
    return aiohttp.ClientSession(headers=headers, timeout=timeout, trust_env=True)
