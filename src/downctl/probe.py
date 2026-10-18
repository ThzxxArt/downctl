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

import email.utils
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from urllib.parse import unquote, urlparse

import aiohttp

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


async def probe_metadata(session: aiohttp.ClientSession, url: str) -> ProbeResult:
    """Probe ``url`` and return size, status and version witness.

    Tries ``HEAD`` first; falls back to a ranged ``GET`` reading
    ``Content-Range`` when the server does not answer ``HEAD`` with a usable
    length, or rejects ``HEAD`` with 401/403/405/501/5xx (a method-scoped
    rejection says nothing about the object). Any other 4xx is returned as-is so
    the caller can report the real status instead of probing twice. A network
    failure yields a result with ``retryable=True`` and ``status=None``.
    """
    head_content_type = ""
    try:
        async with session.head(
            url, headers=dict(_IDENTITY_ENCODING), allow_redirects=True
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
            elif status not in _HEAD_FALLBACK_STATUSES and status < 500:
                # A definitive 4xx: report it rather than probing twice.
                return _failed(status, headers=headers, final_url=final_url)
            head_content_type = header_value(headers, "Content-Type")
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as exc:
        # A socket-level failure *is* a network failure: classify it as
        # retryable instead of letting it escape as an unhandled crash.
        return ProbeResult(retryable=True, error=str(exc))

    try:
        ranged = {"Range": "bytes=0-0", **_IDENTITY_ENCODING}
        async with session.get(url, headers=ranged, allow_redirects=True) as resp:
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


async def probe_size(session: aiohttp.ClientSession, url: str) -> tuple[int, int | None]:
    """Return ``(size, http_status)`` for a URL (see :func:`probe_metadata`)."""
    result = await probe_metadata(session, url)
    return result.size, result.status


async def probe_resource(session: aiohttp.ClientSession, url: str) -> dict[str, object]:
    """Probe a URL and return richer metadata for the ``probe`` command."""
    result = await probe_metadata(session, url)
    return {
        "target": url,
        "status": result.status,
        "size": result.size,
        "content_type": result.content_type,
        "final_url": result.final_url or url,
        "filename": filename_from_url(result.final_url) if result.reached else "",
        "reachable": result.reached,
        #: Version witness the server offered, and which header it came from.
        "validator": result.validator,
        "witness": result.validator_kind,
    }
