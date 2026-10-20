"""The shared ``aiohttp`` session factory.

Builds the client session used by the runtime context. The socket read
timeout is kept longer than the stall watchdog so a stalled transfer is torn
down gracefully (and resumable) instead of hitting a hard timeout.

The session deliberately holds **no** proxy. aiohttp applies a session-wide
``proxy=`` to every request and offers no way to exempt one target, but a proxy
obtained by guessing (the WSL host detection) describes how this machine reaches
the network — it says nothing about where a particular URL lives. Carrying a
request for ``127.0.0.1`` through it asks the proxy to reach its own loopback,
which is another machine's service or nothing at all. So the decision travels
with each request: :class:`ProxyPolicy` answers "may this target use it?", and
the call site passes that answer to ``session.get(..., proxy=...)``.
"""

from __future__ import annotations

import base64
import ipaddress
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp

from downctl import __version__, console

DEFAULT_STALL_LIMIT = 30

#: Use the proxy for every target. This is what an explicit ``-p``, a proxy in
#: the config file, or one read from the environment means: the user asked for
#: *this* route.
PROXY_ALL = "all"
#: Use the proxy only for targets that are not on this machine. This is what a
#: guessed WSL host proxy means: a statement about reaching the network.
PROXY_EXTERNAL = "external"


@dataclass(frozen=True)
class ProxyPolicy:
    """Which requests may use ``url`` as a proxy.

    A value object, so the decision is described once (in :mod:`downctl.app`,
    which knows where the proxy came from) and carried out once (at the request
    site that knows the target), instead of each layer re-deriving it.
    """

    url: str = ""
    scope: str = PROXY_ALL

    def proxy_for(self, target: str) -> str | None:
        """The proxy to use for ``target``, or ``None`` to connect directly."""
        if not self.url:
            return None
        if self.scope != PROXY_EXTERNAL:
            return self.url
        parts = urlsplit(target)
        host = parts.hostname or ""
        if not host:
            return self.url
        if _is_local_host(host):
            return None
        # ``no_proxy`` is a per-target instruction, so it has to be honoured
        # where the target is known. A proxy that was *guessed* may not override
        # an explicit exclusion.
        if _no_proxy_matches(host, parts.port):
            return None
        return self.url


def _is_local_host(host: str) -> bool:
    """Whether ``host`` names this machine (or a link-local neighbour)."""
    name = host.strip("[]").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_link_local or address.is_unspecified


def _no_proxy_matches(host: str, port: int | None) -> bool:
    """Whether ``host:port`` is excluded by ``no_proxy``.

    ``no_proxy`` is a comma-separated list of host suffixes, each optionally
    carrying a leading dot and a port; ``*`` exempts everything. Matched here
    rather than through ``urllib``'s helper because that one consults the
    Windows registry on one of the three platforms we support, so the same
    configuration would mean different things per platform.
    """
    raw = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    if not raw.strip():
        return False
    name = host.lower()
    for entry in raw.split(","):
        rule = entry.strip().lower()
        if not rule:
            continue
        if rule == "*":
            return True
        rule_host, _, rule_port = rule.partition(":")
        if rule_port and rule_port != str(port or ""):
            continue
        rule_host = rule_host.lstrip(".")
        if name == rule_host or name.endswith("." + rule_host):
            return True
    return False


#: Statuses every client follows for a ``GET``.
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: How many hops a redirected request may take before it is refused.
MAX_REDIRECTS = 10
#: Headers that belong to one origin and must not be replayed to another.
_ORIGIN_HEADERS = frozenset({"authorization", "cookie"})


def policy_can_vary(policy: ProxyPolicy | None) -> bool:
    """Whether the proxy decision depends on the *hop* rather than the request.

    Only a proxy that must be skipped for some targets can be wrong for a
    redirect target: with no proxy, or with one the user routed everything
    through, every hop gets the same answer.
    """
    return bool(policy and policy.url and policy.scope == PROXY_EXTERNAL)


def _opener(session: aiohttp.ClientSession, method: str) -> Callable[..., Any]:
    """The ``session.get``/``head`` shorthand for ``method``, else ``request``.

    The shorthands are what the transports and their test doubles implement;
    going through ``request`` would require every fake session to grow a method
    the real code never needs.
    """
    upper = method.upper()
    if upper == "GET":
        return session.get
    if upper == "HEAD":
        return session.head
    return session.request


@asynccontextmanager
async def policy_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    policy: ProxyPolicy | None = None,
    headers: Mapping[str, str] | None = None,
    allow_redirects: bool = True,
    **kwargs: object,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """Issue one request, following redirects so each hop gets its own proxy.

    aiohttp applies a per-request ``proxy=`` to the whole redirect chain, so a
    proxy that may not touch a local target still carried a redirect from an
    external host to ``127.0.0.1``. Deciding per *hop* is the only place the
    answer can be right, and it costs nothing when the answer cannot differ.
    """
    if not allow_redirects or not policy_can_vary(policy):
        async with _opener(session, method)(
            url,
            headers=headers,
            allow_redirects=allow_redirects,
            proxy=policy.proxy_for(url) if policy else None,
            **kwargs,
        ) as response:
            yield response
        return

    stack = AsyncExitStack()
    try:
        current = url
        current_headers = dict(headers or {})
        for hop in range(MAX_REDIRECTS + 1):
            response = await stack.enter_async_context(
                _opener(session, method)(
                    current,
                    headers=current_headers,
                    allow_redirects=False,
                    proxy=policy.proxy_for(current) if policy else None,
                    **kwargs,
                )
            )
            status = response.status
            location = response.headers.get("Location") if status in REDIRECT_STATUSES else None
            if location is None:
                yield response
                return
            if hop >= MAX_REDIRECTS:
                raise aiohttp.ClientError(f"too many redirects starting at {url}")
            target = urljoin(current, location)
            if urlsplit(target).netloc != urlsplit(current).netloc:
                # Same rule aiohttp applies: credentials and cookies belong to
                # the origin they were configured for.
                current_headers = {
                    key: value
                    for key, value in current_headers.items()
                    if key.lower() not in _ORIGIN_HEADERS
                }
            if status == 303:
                method = "GET"
            # Release the redirect response before opening the next hop, or the
            # connection is held until the whole chain is done.
            await stack.aclose()
            current = target
    finally:
        await stack.aclose()


def make_session(
    token: str = "",
    *,
    stall_limit: int = DEFAULT_STALL_LIMIT,
    user_agent: str = "",
    headers: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
) -> aiohttp.ClientSession:
    """Create a client session with sane timeouts and downctl's User-Agent.

    The session carries no proxy: callers pick one per request from the run's
    :class:`ProxyPolicy`. Leaving it out here is what makes a loopback target
    reachable while a guessed host proxy is in play.
    """
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
    return aiohttp.ClientSession(
        headers=merged,
        timeout=timeout,
        trust_env=True,
    )
