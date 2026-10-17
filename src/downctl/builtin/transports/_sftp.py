"""Shared helpers for the SFTP transport and source plugins.

``asyncssh`` is an optional dependency (``pip install downctl[sftp]``); it is
imported lazily so the plugin can be registered even when it is absent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from downctl import console
from downctl.core.errors import DownctlError

SCHEME = "sftp://"
_UNSET: Any = object()


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
    user = unquote(parsed.username) if parsed.username else str(options.get("user", "")) or ""
    password = (
        unquote(parsed.password) if parsed.password else str(options.get("password", "")) or ""
    )
    if parsed.port is not None:
        port = parsed.port
    else:
        try:
            port = int(options.get("port", 22))
        except (TypeError, ValueError) as exc:
            raise DownctlError(
                f"[plugins.sftp] port must be an integer, got {options.get('port')!r}"
            ) from exc
    return SftpTarget(
        host=host,
        port=port,
        user=user,
        password=password,
        path=unquote(parsed.path),
    )


def host_key(target: SftpTarget) -> str:
    """Identity of a connection endpoint (excludes the secret)."""
    return f"{target.user}@{target.host}:{target.port}"


async def connect(target: SftpTarget, options: dict[str, Any]) -> Any:
    """Open an SSH connection (caller must close it)."""
    try:
        import asyncssh
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise DownctlError("SFTP support requires asyncssh: pip install 'downctl[sftp]'") from exc

    kwargs: dict[str, Any] = {
        "host": target.host,
        "port": target.port,
    }
    known: Any = options.get("known_hosts", _UNSET)
    if known is not _UNSET and str(known).strip().lower() in ("", "none", "false"):
        kwargs["known_hosts"] = None  # explicitly disabled
    elif known is not _UNSET:
        kwargs["known_hosts"] = os.path.expanduser(str(known))
    elif os.path.isfile(os.path.expanduser("~/.ssh/known_hosts")):
        kwargs["known_hosts"] = os.path.expanduser("~/.ssh/known_hosts")
    else:
        console.warn(
            "SFTP host-key verification is disabled (no known_hosts); "
            "set [plugins.sftp] known_hosts to enable it"
        )
        kwargs["known_hosts"] = None
    if target.user:
        kwargs["username"] = target.user
    if target.password:
        kwargs["password"] = target.password
    key = options.get("key")
    if key:
        kwargs["client_keys"] = [os.path.expanduser(str(key))]
        if options.get("key_passphrase"):
            kwargs["passphrase"] = str(options["key_passphrase"])

    try:
        return await asyncssh.connect(**kwargs)
    except Exception as exc:  # noqa: BLE001 - surface a clean error
        raise DownctlError(f"SFTP connection to {target.host} failed: {exc}") from exc
