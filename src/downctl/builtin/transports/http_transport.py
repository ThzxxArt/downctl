"""The HTTP(S) transport plugin.

Streams response bodies with ``aiohttp`` and maps aiohttp's exceptions onto
the downctl error hierarchy so the core never imports aiohttp directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import aiohttp

from downctl.core.errors import (
    PermanentHTTPError,
    RangeNotSatisfiableError,
    TransientHTTPError,
    TransportError,
)
from downctl.plugin.protocols import API_VERSION
from downctl.probe import retry_after
from downctl.session import policy_request

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
    #: Optional transport capability (feature-detected by the core): a ``206``
    #: from this transport must carry a ``Content-Range`` matching the request,
    #: otherwise the piece is refused before a byte is written.
    sends_content_range = True

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
        # Ask for an uncompressed body *and* switch off the client-side
        # decompressor. ``Accept-Encoding: identity`` is a request, not a
        # guarantee: an object stored with ``Content-Encoding: gzip`` is
        # answered that way whatever we ask for, and aiohttp then transparently
        # inflates the body while ``Content-Length`` keeps describing the stored
        # bytes. Every offset and size check downstream is about *transferred*
        # bytes, so the transport has to hand those over untouched — a range is
        # defined over the encoded representation, and decoding a slice of a
        # gzip stream is meaningless.
        request_headers.setdefault("Accept-Encoding", "identity")
        if byte_range is not None:
            request_headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        try:
            # ``policy_request`` follows redirects *per hop*: a proxy that was
            # guessed for remote targets must not carry a redirect aimed at this
            # machine's loopback, and a single ``proxy=`` (what aiohttp applies
            # to a whole chain) cannot express that.
            async with policy_request(
                ctx.session,
                "GET",
                url,
                policy=ctx.proxy_policy,
                headers=request_headers,
                allow_redirects=True,
                auto_decompress=False,
            ) as response:
                if response.status == 416:
                    # The object is smaller than the offset we asked for: a
                    # stale ledger/probe, which the engine recovers from by
                    # re-probing and restarting once.
                    raise RangeNotSatisfiableError(url, request_headers.get("Range", ""))
                if response.status in PERMANENT_STATUSES:
                    raise PermanentHTTPError(response.status, url)
                if response.status >= 500 or response.status in _TRANSIENT_STATUSES:
                    raise TransientHTTPError(response.status, url, retry_after(response.headers))
                if response.status >= 400:
                    raise PermanentHTTPError(response.status, url)
                yield HTTPResponse(response)
        except aiohttp.ClientError as exc:
            raise TransportError(str(exc)) from exc
