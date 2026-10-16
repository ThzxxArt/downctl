"""The SFTP transport plugin.

Fetches files over SSH/SFTP using ``asyncssh`` (optional: ``downctl[sftp]``).
Byte ranges are honoured by seeking, so multi-connection downloads and resume
work over SFTP too.

Options are read from ``[plugins.sftp]``: ``user``, ``password``, ``key``,
``key_passphrase``, ``port``, ``known_hosts``. Credentials may also be given in
the URL (``sftp://user:pass@host/path``).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from downctl.builtin.transports._sftp import SftpTarget, connect, parse_url
from downctl.core.errors import TransportError
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext


class _SFTPResponse:
    """Adapter presenting an asyncssh SFTP file as a downctl response."""

    def __init__(self, sftp_file: Any, length: int, status: int) -> None:
        self._file = sftp_file
        self._remaining = length
        self.status = status
        self.headers: dict[str, str] = {}
        self._length = length

    @property
    def content_length(self) -> int:
        return self._length

    def iter_chunks(self, size: int = 64 * 1024) -> AsyncIterator[bytes]:
        return self._iterate(size)

    async def _iterate(self, size: int) -> AsyncIterator[bytes]:
        remaining = self._remaining
        while remaining > 0:
            chunk = await self._file.read(min(size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


class SFTPTransport:
    """Fetch bytes over SFTP."""

    name = "sftp"
    api_version = API_VERSION
    priority = 0

    def __init__(self) -> None:
        self._connections: dict[tuple[str, int, str], Any] = {}

    def supports_range(self) -> bool:
        return True

    async def _connection(self, target: SftpTarget, options: dict[str, Any]) -> Any:
        key = (target.host, target.port, target.user)
        conn = self._connections.get(key)
        if conn is None:
            conn = await connect(target, options)
            self._connections[key] = conn
        return conn

    def close(self) -> None:
        for conn in self._connections.values():
            with contextlib.suppress(Exception):
                conn.close()
        self._connections.clear()

    @asynccontextmanager
    async def request(
        self,
        ctx: RuntimeContext,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> AsyncIterator[_SFTPResponse]:
        options = ctx.plugin_options(self.name)
        target = parse_url(url, options)
        connection = await self._connection(target, options)
        try:
            async with connection.start_sftp_client() as sftp:
                try:
                    attrs = await sftp.stat(target.path)
                    size = int(attrs.size or 0)
                except Exception as exc:  # noqa: BLE001
                    raise TransportError(f"SFTP stat failed for {target.path}: {exc}") from exc

                if byte_range is not None:
                    start, end = byte_range
                else:
                    start, end = 0, size - 1
                length = max(0, end - start + 1)
                status = 206 if byte_range is not None else 200

                sftp_file = await sftp.open(target.path, "rb")
                await sftp_file.seek(start)
                try:
                    yield _SFTPResponse(sftp_file, length, status)
                finally:
                    await sftp_file.close()
        except TransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransportError(f"SFTP request failed for {url}: {exc}") from exc
