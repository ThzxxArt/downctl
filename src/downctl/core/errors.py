"""Exception hierarchy for downctl."""

from __future__ import annotations


class DownctlError(Exception):
    """Base class for all downctl errors."""


class StallTimeout(DownctlError):
    """Raised by the watchdog when no progress is made for the stall limit.

    Must NOT be an ``asyncio.CancelledError``: on Python 3.13+ a task that
    cancels itself is silently swallowed by ``TaskGroup``.
    """


class ProbeError(DownctlError):
    """Raised when a remote resource cannot be probed.

    Distinguishes "the probe itself failed" from "the server answered with a
    status": the app raises it after a transient probe failure exhausted its
    retries (5xx/429/408/425 or a network drop), while a 4xx is reported as a
    :class:`PermanentHTTPError`. Raised since 0.1.4.
    """


class PermanentHTTPError(DownctlError):
    """A 4xx response that retrying will not fix (401/403/404)."""

    def __init__(self, status: int, url: str = "") -> None:
        self.status = status
        self.url = url
        super().__init__(f"HTTP {status} for {url}" if url else f"HTTP {status}")


class TransportError(DownctlError):
    """A transient transport failure (network drop, timeout, 5xx).

    Safe to retry: the piece bitmap preserves completed work.
    """


class TransientHTTPError(TransportError):
    """A 5xx/429 style response that is worth retrying."""

    def __init__(self, status: int, url: str = "", retry_after: float | None = None) -> None:
        self.status = status
        self.url = url
        #: Seconds the server asked us to wait (``Retry-After``), if any.
        self.retry_after = retry_after
        super().__init__(f"HTTP {status} for {url}" if url else f"HTTP {status}")


class RangeMismatchError(TransportError):
    """A 206 response whose ``Content-Range`` contradicts the request.

    Retryable: the bytes of that piece were not written, so the ledger is
    unchanged and another attempt may succeed against a well-behaved server.
    """


class RangeNotSatisfiableError(DownctlError):
    """The server rejected the requested range with 416.

    The remote object is smaller than the offset we asked for, i.e. our ledger
    or the probed size describes another revision. The engine re-probes and
    resets once instead of failing the file outright.
    """

    def __init__(self, url: str = "", requested: str = "") -> None:
        self.url = url
        self.requested = requested
        detail = f" for range {requested}" if requested else ""
        super().__init__(f"HTTP 416{detail}{f' for {url}' if url else ''}")


class ContentChangedError(DownctlError):
    """The remote object was replaced while we were downloading it.

    Not a ``TransportError``: retrying with the same resume ledger is exactly
    what would splice two revisions together. Carries the freshly observed
    witness so the caller can restart against the new revision.
    """

    def __init__(self, url: str = "", validator: str = "", if_range: str = "") -> None:
        self.url = url
        self.validator = validator
        self.if_range = if_range
        super().__init__(f"remote object changed during transfer{f': {url}' if url else ''}")


class ChecksumError(DownctlError):
    """Raised when a downloaded file fails verification."""


class DiskSpaceError(DownctlError):
    """Raised when the destination lacks free space for the download."""
