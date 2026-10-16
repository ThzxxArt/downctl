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
    """Raised when a remote resource cannot be probed."""


class PermanentHTTPError(DownctlError):
    """A 4xx response that retrying will not fix (401/403/404)."""

    def __init__(self, status: int, url: str = "") -> None:
        self.status = status
        self.url = url
        super().__init__(f"HTTP {status} for {url}" if url else f"HTTP {status}")


class ChecksumError(DownctlError):
    """Raised when a downloaded file fails verification."""


class DiskSpaceError(DownctlError):
    """Raised when the destination lacks free space for the download."""
