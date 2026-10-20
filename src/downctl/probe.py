"""Remote resource probing: content length, HTTP status and version witness.

Kept independent of the engine so plugins (sources, routers) can call it
without importing the download loop. :func:`probe_metadata` is the single
implementation of "what is behind this URL"; :func:`probe_size` and
:func:`probe_resource` are thin adapters that keep their historical shapes.

A probe reports the raw HTTP status instead of deciding whether it is fatal:
the caller owns the retry policy, and it needs to know whether a failure is
worth retrying (5xx/429/408/425, network drop) or permanent (401/403/404).
"""

from __future__ import annotations

import asyncio
import email.utils
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from urllib.parse import unquote, urlparse

import aiohttp

from downctl.session import PROXY_ALL, ProxyPolicy, policy_request
from downctl.urls import header_value, normalize_validator

#: Statuses meaning "this HEAD is not usable, try a ranged GET".
#: 401/403 are included because a *method-scoped* rejection is not a verdict on
#: the object: an S3 presigned URL signs the HTTP method, so a GET-signed link
#: answers HEAD with 403 while the ranged GET that the transfer will actually
#: use succeeds. Without this the probe would report a permanent failure for a
#: perfectly downloadable object.
_HEAD_FALLBACK_STATUSES = frozenset({401, 403, 405, 501})
#: 4xx statuses that are worth retrying rather than a permanent answer.
_TRANSIENT_STATUSES = frozenset({408, 425, 429})
_RANGE_TOTAL = re.compile(r"/(\d+)\s*$")
#: Ask for an uncompressed body so the length the probe reports is the length
#: the transfer will receive: aiohttp sends ``Accept-Encoding: gzip, deflate``
#: by default, while the transport pins ``identity``. A mismatch here used to
#: make every piece fail the ``Content-Range`` total check.
_IDENTITY_ENCODING = {"Accept-Encoding": "identity"}
#: Headers that change *what a response describes* rather than *which object* it
#: is about. ``Range: bytes=0-99`` makes the answer a 206 whose ``Content-Length``
#: is 100 bytes of a larger object, and the probe used to report exactly that as
#: the object's size — a user-supplied ``-H "Range: ..."`` then made every piece
#: fail the total check, permanently, with no hint about why. The transfer keeps
#: these headers (a caller may want a conditional or partial fetch); a *probe*
#: must describe the object the transfer will receive.
_PARTIAL_OR_CONDITIONAL = frozenset(
    {"range", "if-range", "if-match", "if-none-match", "if-modified-since", "if-unmodified-since"}
)
#: Upper bound on one probe. A probe is a single HEAD plus, at most, a single
#: ranged GET; a server that cannot answer that within this window is treated as
#: a network failure (retryable) rather than being allowed to park the whole
#: probe phase on one socket.
PROBE_TIMEOUT = 30.0


def content_length(headers: Mapping[str, str]) -> int:
    try:
        return int(headers.get("Content-Length", 0) or 0)
    except (TypeError, ValueError):
        return 0


def filename_from_url(url: str) -> str:
    """Best-effort filename from a URL path (final segment, URL-decoded)."""
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1])
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def retry_after(headers: Mapping[str, str]) -> float | None:
    """Seconds to wait before retrying, from a ``Retry-After`` header.

    Accepts both delta-seconds and an HTTP-date (RFC 9110 §10.2.3). A date in
    the past yields ``0``; an unparsable value yields ``None`` so the caller
    falls back to its normal backoff instead of sleeping for a garbage value.
    """
    raw = header_value(headers, "Retry-After").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


@dataclass(frozen=True)
class ProbeResult:
    """Everything one probe learned about a URL.

    ``retryable`` is set only when the failure was *not* a definitive answer
    from the server (network drop, 5xx, 429/408/425), so a caller can retry
    without re-classifying status codes itself.
    """

    size: int = 0
    status: int | None = None
    validator: str = ""
    if_range: str = ""
    validator_kind: str = "none"
    final_url: str = ""
    content_type: str = ""
    #: True when the server produced an HTTP response (any status).
    reached: bool = False
    retryable: bool = False
    retry_after: float | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


def _from_headers(
    *,
    status: int,
    headers: Mapping[str, str],
    final_url: str,
    size: int,
    retryable: bool = False,
) -> ProbeResult:
    witness = normalize_validator(headers)
    return ProbeResult(
        size=size,
        status=status,
        validator=witness.value,
        if_range=witness.if_range,
        validator_kind=witness.kind,
        final_url=final_url,
        content_type=header_value(headers, "Content-Type"),
        reached=True,
        retryable=retryable,
        retry_after=retry_after(headers) if retryable else None,
    )


def _failed(status: int, *, headers: Mapping[str, str], final_url: str) -> ProbeResult:
    """Classify a non-2xx answer without losing the server's own status."""
    transient = status >= 500 or status in _TRANSIENT_STATUSES
    result = _from_headers(
        status=status, headers=headers, final_url=final_url, size=0, retryable=transient
    )
    return replace(result, error=f"HTTP {status}")


def _request_headers(
    headers: Mapping[str, str] | None,
    extra: Mapping[str, str],
    *,
    drop: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Overlay ``extra`` on ``headers``, dropping keys ``extra`` redefines.

    The probe's own headers win. ``Accept-Encoding: identity`` is what makes the
    reported length the length the transfer will receive (the transport pins
    identity too), and ``Range`` is how the fallback GET asks for a window. A
    caller-supplied value for either would make the probe describe an object it
    did not measure.

    ``drop`` removes headers that would make the answer describe a *part* of the
    object or a *conditional* variant of it (see :data:`_PARTIAL_OR_CONDITIONAL`).
    """
    overridden = {key.lower() for key in extra} | drop
    merged = {key: value for key, value in (headers or {}).items() if key.lower() not in overridden}
    merged.update(extra)
    return merged


async def probe_metadata(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    proxy: str | None = None,
    policy: ProxyPolicy | None = None,
) -> ProbeResult:
    """Probe ``url`` and return size, status and version witness.

    Tries ``HEAD`` first; falls back to a ranged ``GET`` reading
    ``Content-Range`` when the server does not answer ``HEAD`` with a usable
    length, or rejects ``HEAD`` with 401/403/405/501/5xx (a method-scoped
    rejection says nothing about the object). Any other 4xx is returned as-is so
    the caller can report the real status instead of probing twice. A network
    failure yields a result with ``retryable=True`` and ``status=None``.

    ``headers`` are the request headers the *task* carries — authorization and
    whatever else a source plugin negotiated for this specific object — and they
    are sent on both requests. Probing is part of fetching, so it carries the
    same contract as the transfer: a probe that asked without them measured a
    different resource than the one about to be downloaded, and a credentialed
    target then answered ``401`` to the probe while answering ``200`` to the
    download itself. Headers that would make the answer describe only *part* of
    the object (``Range``) or a *conditional* variant of it are the exception:
    they are dropped, because the number this function reports has to describe
    the whole object.

    ``proxy`` is the proxy this target is allowed to use, already resolved by
    the caller's policy. Asking the *target* here matters: the probe is often
    the first request made for a URL, so a session-wide proxy would decide the
    route before anyone looked at where the URL points.

    ``policy`` supersedes ``proxy`` when given: the probe follows redirects, and
    a proxy that was only allowed for the original target must not carry a
    redirect aimed at this machine. Passing the policy lets each hop decide.

    The whole probe is bounded by :data:`PROBE_TIMEOUT`, so a hung socket becomes
    a retryable result instead of stalling the probe phase. A caller with a
    tighter budget (``--max-time``) wraps this call in its own timeout; the
    shorter of the two wins.
    """
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            return await _probe_url(session, url, task_headers=headers, proxy=proxy, policy=policy)
    except TimeoutError:
        return ProbeResult(retryable=True, error=f"probe timed out after {PROBE_TIMEOUT:g}s")


@asynccontextmanager
async def _probe_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    proxy: str | None,
    policy: ProxyPolicy | None,
) -> AsyncIterator[aiohttp.ClientResponse]:
    """One probe request: HEAD, then the ranged GET fallback.

    The length this probe reports has to be the length the transfer will
    receive, so ``auto_decompress`` is off on both requests.

    Always issued through :func:`policy_request`, even when the caller only
    resolved a proxy itself: that function is also what attaches the run's
    origin-bound credentials and refuses to carry them across an origin change.
    A probe that called ``session.head`` directly skipped both, so with a
    ``--user`` credential a redirect could take it somewhere it was never
    configured for.
    """
    effective = policy if policy is not None else ProxyPolicy(proxy or "", PROXY_ALL)
    async with policy_request(
        session,
        method,
        url,
        policy=effective,
        headers=headers,
        allow_redirects=True,
        auto_decompress=False,
    ) as response:
        yield response


async def _probe_url(
    session: aiohttp.ClientSession,
    url: str,
    *,
    task_headers: Mapping[str, str] | None,
    proxy: str | None,
    policy: ProxyPolicy | None = None,
) -> ProbeResult:
    """One HEAD, then at most one ranged GET (see :func:`probe_metadata`).

    ``task_headers`` is named for what it is on the wire. The response headers
    below are ``headers``; keeping the two apart matters, because assigning the
    response over the request contract is exactly how the credential stopped
    reaching the second request.
    """
    head_content_type = ""
    try:
        async with _probe_request(
            session,
            "HEAD",
            url,
            headers=_request_headers(
                task_headers, _IDENTITY_ENCODING, drop=_PARTIAL_OR_CONDITIONAL
            ),
            proxy=proxy,
            policy=policy,
        ) as resp:
            status = resp.status
            headers = resp.headers
            final_url = str(resp.url)
            if 200 <= status < 300:
                length = content_length(headers)
                if length > 0:
                    return _from_headers(
                        status=status, headers=headers, final_url=final_url, size=length
                    )
                if header_value(headers, "Content-Length").strip() == "0":
                    # A genuinely empty object. Falling through to the ranged
                    # GET asked for ``bytes=0-0``, which is unsatisfiable for a
                    # zero-length body: the server answered 416 and the probe
                    # reported a dead target. A 2xx HEAD with an explicit
                    # ``Content-Length: 0`` is the answer, not a missing one.
                    return _from_headers(
                        status=status, headers=headers, final_url=final_url, size=0
                    )
            elif status not in _HEAD_FALLBACK_STATUSES and status < 500:
                # A definitive 4xx: report it rather than probing twice.
                return _failed(status, headers=headers, final_url=final_url)
            head_content_type = header_value(headers, "Content-Type")
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
        # A socket-level failure *is* a network failure: classify it as
        # retryable instead of letting it escape as an unhandled crash.
        return ProbeResult(retryable=True, error=str(exc))

    try:
        ranged = _request_headers(
            task_headers,
            {**_IDENTITY_ENCODING, "Range": "bytes=0-0"},
            drop=_PARTIAL_OR_CONDITIONAL,
        )
        async with _probe_request(
            session, "GET", url, headers=ranged, proxy=proxy, policy=policy
        ) as resp:
            status = resp.status
            headers = resp.headers
            final_url = str(resp.url)
            if 200 <= status < 300:
                match = _RANGE_TOTAL.search(header_value(headers, "Content-Range"))
                size = int(match.group(1)) if match else content_length(headers)
                result = _from_headers(
                    status=status, headers=headers, final_url=final_url, size=size
                )
                # A HEAD that answered with metadata (but no length) is still
                # the better source for the content type.
                if not result.content_type and head_content_type:
                    result = replace(result, content_type=head_content_type)
                return result
            return _failed(status, headers=headers, final_url=final_url)
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
        return ProbeResult(retryable=True, error=str(exc))


async def probe_size(
    session: aiohttp.ClientSession, url: str, *, proxy: str | None = None
) -> tuple[int, int | None]:
    """Return ``(size, http_status)`` for a URL (see :func:`probe_metadata`)."""
    result = await probe_metadata(session, url, proxy=proxy)
    return result.size, result.status


async def probe_resource(
    session: aiohttp.ClientSession,
    url: str,
    *,
    proxy: str | None = None,
    policy: ProxyPolicy | None = None,
) -> dict[str, object]:
    """Probe a URL and return richer metadata for the ``probe`` command."""
    result = await probe_metadata(session, url, proxy=proxy, policy=policy)
    return {
        "target": url,
        "status": result.status,
        "size": result.size,
        "content_type": result.content_type,
        "final_url": result.final_url or url,
        "filename": filename_from_url(result.final_url) if result.reached else "",
        "reachable": result.reached,
        #: The version value the server offered, and **which header** carried it
        #: (``etag`` / ``last-modified`` / ``none``). This is deliberately not
        #: called ``witness``: that word means "what a completion claim rests
        #: on" (``validator`` / ``checksum`` / ``none``) elsewhere in the output,
        #: and one name for two vocabularies is how readers came to compare them.
        "validator": result.validator,
        "validator_kind": result.validator_kind,
    }
