"""The shared ``aiohttp`` session factory.

Builds the client session used by the runtime context. The socket read
timeout is kept longer than the stall watchdog so a stalled transfer is torn
down gracefully (and resumable) instead of hitting a hard timeout.
"""

from __future__ import annotations

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
        merged["Authorization"] = aiohttp.encode_basic_auth(auth[0], auth[1])
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
