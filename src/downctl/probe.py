"""Remote resource probing: content length and HTTP status.

Kept independent of the engine so plugins (sources, routers) can call it
without importing the download loop.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import unquote, urlparse

import aiohttp


def content_length(headers: Mapping[str, str]) -> int:
    try:
        return int(headers.get("Content-Length", 0) or 0)
    except (TypeError, ValueError):
        return 0


def filename_from_url(url: str) -> str:
    """Best-effort filename from a URL path (final segment, URL-decoded)."""
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1])
    return name.replace("\\", "/").rsplit("/", 1)[-1]


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


async def probe_resource(session: aiohttp.ClientSession, url: str) -> dict[str, object]:
    """Probe a URL and return richer metadata for the ``probe`` command."""
    result: dict[str, object] = {
        "target": url,
        "status": None,
        "size": 0,
        "content_type": "",
        "final_url": url,
        "filename": "",
        "reachable": False,
    }
    try:
        async with session.head(url, allow_redirects=True) as resp:
            result["status"] = resp.status
            result["reachable"] = True
            result["content_type"] = resp.headers.get("Content-Type", "")
            result["final_url"] = str(resp.url)
            length = content_length(resp.headers)
            if length > 0:
                result["size"] = length
                result["filename"] = filename_from_url(str(resp.url))
                return result

        headers = {"Range": "bytes=0-0"}
        async with session.get(url, headers=headers, allow_redirects=True) as resp:
            result["status"] = resp.status
            result["reachable"] = True
            if not result["content_type"]:
                result["content_type"] = resp.headers.get("Content-Type", "")
            result["final_url"] = str(resp.url)
            content_range = resp.headers.get("Content-Range", "")
            match = re.search(r"/(\d+)\s*$", content_range)
            if match:
                result["size"] = int(match.group(1))
            else:
                result["size"] = content_length(resp.headers)
        result["filename"] = filename_from_url(str(result["final_url"]))
        return result
    except (aiohttp.ClientError, ValueError):
        result["status"] = None
        result["reachable"] = False
        return result
