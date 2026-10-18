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


# --------------------------------------------------------------------------- #
# failure classification
# --------------------------------------------------------------------------- #
#: Why a target failed, recorded in ``task.meta["failure"]``.
#:
#: The failed log and the JSON stream carry this so a machine can decide
#: whether re-running is worth anything, instead of having to parse an error
#: string. A ``500`` that exhausted its retries and a ``404`` are both "the
#: target failed"; only one of them is worth retrying, and before 0.1.4 the two
#: records were indistinguishable.
#:
#: * ``permanent`` - retrying cannot help: a 4xx, a checksum mismatch, no disk
#:   space, no verifier for the digest, a lock held by another process.
#: * ``transient-exhausted`` - the cause was retryable (5xx/429/408/425, a
#:   network drop) but the retry budget ran out. Running again may succeed.
#: * ``timeout`` - the target hit ``--max-time``. The cause is unknown, so it
#:   is neither of the above.
FAILURE_PERMANENT = "permanent"
FAILURE_TRANSIENT_EXHAUSTED = "transient-exhausted"
FAILURE_TIMEOUT = "timeout"

#: A failed-log record written before 0.1.4 has no failure class. Reported as
#: this, never guessed: a machine must be able to tell "this run proved it is
#: permanent" from "nobody recorded why".
FAILURE_UNKNOWN = "unknown"
