"""The shared ``aiohttp`` session factory.

Builds the client session used by the runtime context. The socket read
timeout is kept longer than the stall watchdog so a stalled transfer is torn
down gracefully (and resumable) instead of hitting a hard timeout.
"""

from __future__ import annotations

import base64

import aiohttp

from downctl import __version__, console

DEFAULT_STALL_LIMIT = 30


def make_session(
    proxy: str = "",
    token: str = "",
    *,
    stall_limit: int = DEFAULT_STALL_LIMIT,
    user_agent: str = "",
    headers: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
) -> aiohttp.ClientSession:
    """Create a client session with sane timeouts and downctl's User-Agent."""
    merged = {"User-Agent": user_agent or f"downctl/{__version__}"}
    if token and auth:
        console.warn("both a bearer token and --user are set; using --user credentials")
    if token:
        merged["Authorization"] = f"Bearer {token}"
    if auth:
        # aiohttp's ``encode_basic_auth`` helper is not available across all
        # supported aiohttp versions (and disappeared from the top-level
        # namespace in several releases). Match its UTF-8 default rather than
        # latin-1 so non-ASCII credentials keep working; keep its rejection of
        # a colon in the login (RFC 7617 splits on the first colon).
        if ":" in auth[0]:
            raise ValueError('a ":" is not allowed in login (RFC 7617#section-2)')
        credentials = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode("ascii")
        merged["Authorization"] = f"Basic {credentials}"
    for key, value in (headers or {}).items():
        if key.lower() == "authorization":
            for existing in [k for k in merged if k.lower() == "authorization"]:
                del merged[existing]
            if token or auth:
                console.warn(
                    "an explicit Authorization header overrides the configured credentials"
                )
        merged[key] = value
    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=15,
        sock_read=max(60, stall_limit * 3),
    )
    kwargs: dict[str, object] = {
        "headers": merged,
        "timeout": timeout,
        "trust_env": True,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return aiohttp.ClientSession(**kwargs)  # type: ignore[arg-type]
