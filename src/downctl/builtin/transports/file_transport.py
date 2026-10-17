"""The local-file transport plugin (``file://``).

Serves local files, honouring byte ranges so the standard piece/resume
machinery works for local sources too. Reads run in a thread to avoid blocking
the event loop.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from downctl.core.errors import TransportError
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

CHUNK_SIZE = 64 * 1024


def local_path(url: str) -> str:
    """Translate a ``file://`` URL to a filesystem path (cross-platform)."""
    parsed = urlparse(url)
    if parsed.scheme != "file":
        raise TransportError(f"not a file URL: {url!r}")
    path = unquote(parsed.path)
    if os.name != "nt":
        return path
    netloc = parsed.netloc
    if netloc and netloc.lower() != "localhost":
        if len(netloc) == 2 and netloc[1] == ":":  # drive encoded as authority
            return f"{netloc}{path}"
        return f"//{netloc}{path}"  # UNC path
    if path.startswith("/") and len(path) > 2 and path[2] == ":":
        path = path[1:]
    return path


class _FileResponse:
    """Adapter presenting a local file object as a downctl response."""

    def __init__(self, handle: object, length: int, status: int) -> None:
        self._handle = handle
        self._remaining = length
        self._length = length
        self.status = status
        self.headers: dict[str, str] = {}

    @property
    def content_length(self) -> int:
        return self._length

    def iter_chunks(self, size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
        return self._iterate(size)

    async def _iterate(self, size: int) -> AsyncIterator[bytes]:
        remaining = self._remaining
        while remaining > 0:
            chunk = await asyncio.to_thread(self._handle.read, min(size, remaining))  # type: ignore[attr-defined]
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


class FileTransport:
    """Read bytes from the local filesystem."""

    name = "file"
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
    ) -> AsyncIterator[_FileResponse]:
        path = local_path(url)
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise TransportError(f"cannot read {path}: {exc}") from exc

        if byte_range is not None:
            start, end = byte_range
        else:
            start, end = 0, size - 1
        length = max(0, end - start + 1)
        status = 206 if byte_range is not None else 200

        handle = open(path, "rb")  # noqa: SIM115 - closed in finally
        handle.seek(start)
        try:
            yield _FileResponse(handle, length, status)
        finally:
            handle.close()
