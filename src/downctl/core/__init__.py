"""downctl core: the stable, non-pluggable engine.

These modules form the download state machine (scheduling, resumable piece
bitmaps, stall watchdog, rate limiting, locking, disk checks). Everything here
is exposed to plugins as *stable API*; it is deliberately not itself a plugin.
"""

from __future__ import annotations

from downctl.core.context import RuntimeContext
from downctl.core.errors import (
    ChecksumError,
    DiskSpaceError,
    DownctlError,
    PermanentHTTPError,
    ProbeError,
    StallTimeout,
)
from downctl.core.task import DownloadTask

__all__ = [
    "RuntimeContext",
    "DownloadTask",
    "DownctlError",
    "StallTimeout",
    "ProbeError",
    "PermanentHTTPError",
    "ChecksumError",
    "DiskSpaceError",
]
