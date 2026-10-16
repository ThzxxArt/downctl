"""The HTTP(S) transport plugin.

Streams response bodies with ``aiohttp``. The streaming loop (chunking,
stall handling, Range fallback) is finalized in P2; the interface is stable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

CHUNK_SIZE = 64 * 1024


class HTTPTransport:
    """Fetch bytes over HTTP(S) using the runtime's shared session."""

    name = "http"
    api_version = API_VERSION
    priority = 0

    def supports_range(self) -> bool:
        return True

    async def fetch(
        self,
        ctx: RuntimeContext,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> AsyncIterator[bytes]:
        request_headers = dict(headers or {})
        if byte_range is not None:
            request_headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        async with ctx.session.get(url, headers=request_headers, allow_redirects=True) as resp:
            resp.raise_for_status()
            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                yield chunk
