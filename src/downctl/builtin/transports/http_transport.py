"""The HTTP(S) transport plugin.

Streams response bodies with ``aiohttp`` and maps aiohttp's exceptions onto
the downctl error hierarchy so the core never imports aiohttp directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import aiohttp

from downctl.core.errors import PermanentHTTPError, TransientHTTPError, TransportError
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

CHUNK_SIZE = 64 * 1024
PERMANENT_STATUSES = frozenset({401, 403, 404})
#: 4xx worth retrying (request/connection timeout, too-early).
_TRANSIENT_STATUSES = frozenset({408, 425, 429})


class HTTPResponse:
    """Adapter exposing an ``aiohttp`` response as a downctl response."""

    def __init__(self, response: aiohttp.ClientResponse) -> None:
        self._response = response
        self.status = response.status
        self.headers: Mapping[str, str] = response.headers

    @property
    def content_length(self) -> int | None:
        return self._response.content_length

    def iter_chunks(self, size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
        return self._response.content.iter_chunked(size)


class HTTPTransport:
    """Fetch bytes over HTTP(S) using the runtime's shared session."""

    name = "http"
    api_version = API_VERSION
    priority = 0

    def supports_range(self) -> bool:
        return True

    @asynccontextmanager
    async def request(
        self,
        ctx: RuntimeContext,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> AsyncIterator[HTTPResponse]:
        request_headers = dict(headers or {})
        # Ask for an uncompressed body so Content-Length matches the bytes we
        # receive (aiohttp auto-decompresses, which would break size checks).
        request_headers.setdefault("Accept-Encoding", "identity")
        if byte_range is not None:
            request_headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        try:
            async with ctx.session.get(
                url, headers=request_headers, allow_redirects=True
            ) as response:
                if response.status in PERMANENT_STATUSES:
                    raise PermanentHTTPError(response.status, url)
                if response.status >= 500 or response.status in _TRANSIENT_STATUSES:
                    raise TransientHTTPError(response.status, url)
                if response.status >= 400:
                    raise PermanentHTTPError(response.status, url)
                yield HTTPResponse(response)
        except aiohttp.ClientError as exc:
            raise TransportError(str(exc)) from exc
