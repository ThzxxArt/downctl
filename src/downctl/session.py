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

For the same reason the session is built with ``trust_env=False``. aiohttp
consults ``http_proxy``/``https_proxy`` itself whenever ``proxy`` is ``None``,
which silently undoes a policy answer of "connect directly": with
``HTTP_PROXY=http://127.0.0.1:7890`` exported, a loopback request that the
policy had exempted went to the proxy anyway. The environment is not the
decision — :func:`downctl.app._build_config` already folds it into
:class:`ProxyPolicy`, so reading it twice not only duplicates the owner, it
overrides the answer.

Credentials are origin-bound for the same reason, and are therefore not session
defaults either: every header configured for this run (``-H``, ``-u``, a
token) describes the *first* URL's origin, and a redirect to another origin must
not carry it (aiohttp, like every other client, applies session defaults to
every hop). They travel in :func:`origin_headers` and are attached by
:func:`policy_request` to the hop that owns them; ``Authorization`` and
``Cookie`` carried in a per-request header are dropped the same way.
"""

from __future__ import annotations

import base64
import functools
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
#: Headers that are credentials wherever they appear, and must not be replayed
#: to another origin even when they arrived in a per-request dict.
_ORIGIN_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
#: Where :func:`make_session` leaves the headers configured for the run. Not
#: session defaults: a default is applied to *every* hop and every host, while a
#: header the user passed for this run describes the request's own origin. The
#: name is historical (0.1.5 only stored credentials here); 0.1.6 stores every
#: user-supplied header, because ``-H X-Api-Key`` followed a redirect to another
#: host exactly like ``Authorization`` did.
ORIGIN_HEADERS_ATTR = "downctl_origin_headers"
#: Where the runtime attaches :class:`~downctl.core.credentials.AuthResolver`.
#: Same pattern as ``ORIGIN_HEADERS_ATTR``: the request site only has the
#: session, and every request goes through one function.
AUTH_RESOLVER_ATTR = "downctl_auth_resolver"
_DEFAULT_PORTS = {"http": 80, "https": 443}


def origin_headers(session: aiohttp.ClientSession) -> dict[str, str]:
    """The headers configured for this run, bound to each request's origin."""
    stored = getattr(session, ORIGIN_HEADERS_ATTR, None)
    if not isinstance(stored, dict):
        return {}
    return {str(key): str(value) for key, value in stored.items()}


def auth_resolver(session: aiohttp.ClientSession) -> object | None:
    """The credential resolver attached to ``session``, if any."""
    return getattr(session, AUTH_RESOLVER_ATTR, None)


def _origin(url: str) -> tuple[str, str, int]:
    """``(scheme, host, port)`` — the unit a credential is bound to."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port or _DEFAULT_PORTS.get(scheme, 0)
    except ValueError:  # malformed port in a Location header
        port = 0
    return scheme, (parts.hostname or "").lower(), port


def _same_origin(left: str, right: str) -> bool:
    return _origin(left) == _origin(right)


def _hop_headers(
    base: Mapping[str, str] | None,
    configured: Mapping[str, str],
    material: Mapping[str, str],
    *,
    owns_origin: bool,
) -> dict[str, str]:
    """Headers for one hop: request headers win, then the origin's own.

    ``configured`` are the headers the user gave for the run and ``material``
    is what the configured :class:`AuthProvider` produced. Both belong to the
    *first* origin: once a redirect leaves it they are dropped, together with
    the credential headers a request may carry in ``base`` (handled by the
    caller), because none of them describes the new host.
    """
    merged = {str(key): str(value) for key, value in (base or {}).items()}
    if owns_origin:
        for key, value in configured.items():
            merged.setdefault(key, value)
        for key, value in material.items():
            merged.setdefault(key, value)
    return merged


def policy_can_vary(policy: ProxyPolicy | None) -> bool:
    """Whether the proxy decision depends on the *hop* rather than the request.

    Only a proxy that must be skipped for some targets can be wrong for a
    redirect target: with no proxy, or with one the user routed everything
    through, every hop gets the same answer.
    """
    return bool(policy and policy.url and policy.scope == PROXY_EXTERNAL)


def _needs_hop_control(
    policy: ProxyPolicy | None,
    configured: Mapping[str, str],
    resolver: object | None,
    request_headers: Mapping[str, str] | None,
) -> bool:
    """Whether this request has to follow the chain itself.

    Two things must be decided per hop, and aiohttp can only do one of them:
    the proxy (it applies one value to the whole chain) and the credentials
    (its own rule compares hosts, while a credential belongs to a full origin —
    a rewrite from ``https`` to ``http`` on the same host is a different
    origin). Whenever either can differ, the chain is walked here.

    A per-request ``Authorization``/``Cookie`` counts too: a source puts its
    token there, and letting aiohttp follow the chain would replay it to
    whatever host the server names.
    """
    if policy_can_vary(policy) or configured or resolver is not None:
        return True
    return any(str(key).lower() in _ORIGIN_HEADERS for key in (request_headers or ()))


def _opener(session: aiohttp.ClientSession, method: str) -> Callable[..., Any]:
    """The ``session.get``/``head`` shorthand for ``method``, else ``request``.

    The shorthands are what the transports and their test doubles implement;
    going through ``request`` would require every fake session to grow a method
    the real code never needs. ``request`` has to be bound to its method: the
    bare attribute is an unbound function, so calling it with a URL as the first
    argument applied ``self=url`` and failed before a socket was opened.
    """
    upper = method.upper()
    if upper == "GET":
        return session.get
    if upper == "HEAD":
        return session.head
    return functools.partial(session.request, upper)


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

    The same argument applies to credentials: the headers the user configured
    for the run (``-H``, ``-u``) and the material an
    :class:`~downctl.core.credentials.AuthProvider` produces belong to the
    origin the request started at, so they are attached to the hops that still
    are that origin and dropped as soon as the chain leaves it. Every
    user-supplied header is treated this way — ``X-Api-Key`` followed a 302
    exactly like ``Authorization`` did.
    """
    configured = origin_headers(session)
    resolver = auth_resolver(session)
    #: The provider material for the *first* origin, resolved at most once per
    #: request. The resolver caches per origin, so a download loop pays a dict
    #: lookup per range.
    material: dict[str, str] = {}
    material_resolved = False

    async def _material() -> dict[str, str]:
        nonlocal material, material_resolved
        if material_resolved or resolver is None:
            return material
        material_resolved = True
        found = await resolver.material_for(url)  # type: ignore[attr-defined]
        if found is None:
            return material
        headers_out = {str(key): str(value) for key, value in found.headers.items()}
        if found.basic is not None and "authorization" not in {key.lower() for key in headers_out}:
            login, password = found.basic
            encoded = base64.b64encode(f"{login}:{password}".encode()).decode("ascii")
            headers_out["Authorization"] = f"Basic {encoded}"
        material = headers_out
        return material

    if not allow_redirects or not _needs_hop_control(policy, configured, resolver, headers):
        async with _opener(session, method)(
            url,
            headers=_hop_headers(headers, configured, await _material(), owns_origin=True),
            allow_redirects=allow_redirects,
            proxy=policy.proxy_for(url) if policy else None,
            **kwargs,
        ) as response:
            yield response
        return

    stack = AsyncExitStack()
    try:
        # Resolve the provider material once, for the first origin. It is used
        # on the hops that still are that origin and dropped with the rest.
        await _material()
        current = url
        current_headers = dict(headers or {})
        #: Whether this hop still speaks for the origin the credentials belong
        #: to. Once the chain leaves it, it never comes back: re-attaching on a
        #: later hop would hand them to an origin that merely redirected *to*
        #: the original one.
        owns_origin = True
        for hop in range(MAX_REDIRECTS + 1):
            response = await stack.enter_async_context(
                _opener(session, method)(
                    current,
                    headers=_hop_headers(
                        current_headers,
                        configured,
                        material if owns_origin else {},
                        owns_origin=owns_origin,
                    ),
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
            if not _same_origin(target, current):
                # Same rule aiohttp applies: credentials and cookies belong to
                # the origin they were configured for.
                current_headers = {
                    key: value
                    for key, value in current_headers.items()
                    if key.lower() not in _ORIGIN_HEADERS
                }
                owns_origin = False
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

    The session carries no proxy and no credentials. Both decisions belong to
    the request, because both are per-target: a proxy obtained by guessing must
    not carry a request for this machine, and a credential belongs to the origin
    that issued it. ``trust_env`` is off for the same reason — aiohttp would
    otherwise fall back to ``http_proxy`` for exactly the requests the policy
    had exempted.
    """
    #: Every header the user configured for this run is bound to the origin of
    #: the request it is used on — not applied to the session. ``-H X-Api-Key``
    #: used to be a session default, so a redirect to another host carried it.
    merged = {"User-Agent": user_agent or f"downctl/{__version__}"}
    configured: dict[str, str] = {}
    if token and auth:
        console.warn("both a bearer token and --user are set; using --user credentials")
    if token:
        configured["Authorization"] = f"Bearer {token}"
    if auth:
        # aiohttp's ``encode_basic_auth`` helper is not available across all
        # supported aiohttp versions (and disappeared from the top-level
        # namespace in several releases). Match its UTF-8 default rather than
        # latin-1 so non-ASCII credentials keep working; keep its rejection of
        # a colon in the login (RFC 7617 splits on the first colon).
        if ":" in auth[0]:
            raise ValueError('a ":" is not allowed in login (RFC 7617#section-2)')
        credentials = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode("ascii")
        configured["Authorization"] = f"Basic {credentials}"
    for key, value in (headers or {}).items():
        # The caller's own header wins over the configured credential, which is
        # what the previous merge order did — but only for the origin it was
        # written for, so it moves to the per-origin set with it.
        if key.lower() == "authorization" and (token or auth):
            console.warn("an explicit Authorization header overrides the configured credentials")
        for existing in [k for k in configured if k.lower() == key.lower()]:
            del configured[existing]
        configured[key] = value
    timeout = aiohttp.ClientTimeout(
        total=None,
        sock_connect=15,
        sock_read=max(60, stall_limit * 3),
    )
    session = aiohttp.ClientSession(
        headers=merged,
        timeout=timeout,
        trust_env=False,
    )
    setattr(session, ORIGIN_HEADERS_ATTR, configured)
    return session
