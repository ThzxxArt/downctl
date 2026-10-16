"""Remote resource probing: content length and HTTP status.

Kept independent of the engine so plugins (sources, routers) can call it
without importing the download loop.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

import aiohttp


def content_length(headers: Mapping[str, str]) -> int:
    try:
        return int(headers.get("Content-Length", 0) or 0)
    except (TypeError, ValueError):
        return 0


async def probe_size(session: aiohttp.ClientSession, url: str) -> tuple[int, int | None]:
    """Return ``(size, http_status)`` for a URL.

    Tries ``HEAD`` first; falls back to a ranged ``GET`` reading
    ``Content-Range`` when the server does not answer ``HEAD`` with a length.
    Returns ``(0, None)`` on network failure.
    """
    try:
        async with session.head(url, allow_redirects=True) as resp:
            status = resp.status
            length = content_length(resp.headers)
            if length > 0:
                return length, status

        headers = {"Range": "bytes=0-0"}
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            content_range = resp.headers.get("Content-Range", "")
            match = re.search(r"/(\d+)\s*$", content_range)
            if match:
                return int(match.group(1)), resp.status
            return content_length(resp.headers), resp.status
    except (aiohttp.ClientError, ValueError):
        return 0, None
