"""Shared helpers for the SFTP transport and source plugins.

``asyncssh`` is an optional dependency (``pip install downctl[sftp]``); it is
imported lazily so the plugin can be registered even when it is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from downctl.core.errors import DownctlError

SCHEME = "sftp://"


@dataclass
class SftpTarget:
    host: str
    port: int
    user: str
    password: str
    path: str


def parse_url(url: str, options: dict[str, Any]) -> SftpTarget:
    parsed = urlparse(url)
    if parsed.scheme != "sftp":
        raise DownctlError(f"not an sftp URL: {url!r}")
    host = parsed.hostname or ""
    if not host:
        raise DownctlError(f"missing host in sftp URL: {url!r}")
    user = parsed.username or str(options.get("user", "")) or ""
    password = parsed.password or str(options.get("password", "")) or ""
    return SftpTarget(
        host=host,
        port=parsed.port or int(options.get("port", 22)),
        user=user,
        password=password,
        path=unquote(parsed.path),
    )


async def connect(target: SftpTarget, options: dict[str, Any]) -> Any:
    """Open an SSH connection (caller must close it)."""
    try:
        import asyncssh
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise DownctlError("SFTP support requires asyncssh: pip install 'downctl[sftp]'") from exc

    kwargs: dict[str, Any] = {
        "host": target.host,
        "port": target.port,
        "known_hosts": options.get("known_hosts") or None,
    }
    if target.user:
        kwargs["username"] = target.user
    if target.password:
        kwargs["password"] = target.password
    key = options.get("key")
    if key:
        kwargs["client_keys"] = [str(key)]
        if options.get("key_passphrase"):
            kwargs["passphrase"] = str(options["key_passphrase"])

    try:
        return await asyncssh.connect(**kwargs)
    except Exception as exc:  # noqa: BLE001 - surface a clean error
        raise DownctlError(f"SFTP connection to {target.host} failed: {exc}") from exc
