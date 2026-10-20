"""The central data model passed through the download pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DownloadTask:
    """A single file to download.

    A ``Source`` plugin turns user input (a URL, a repo id, ...) into one or
    more of these. The engine consumes them and never looks back at the
    original input.
    """

    url: str
    target: str
    size: int = 0
    checksum_algo: str = ""
    checksum_hex: str = ""
    #: Where the task came from (source plugin name), for reporting.
    source: str = ""
    #: The user-supplied target this task was resolved from (e.g.
    #: ``s3://bucket/key`` or ``hf://org/repo@rev``). Used by ``--retry-failed``
    #: to re-resolve fresh URLs and credentials instead of replaying an
    #: expired presigned URL.
    origin: str = ""
    #: Transport plugin to use (defaults to ``http`` when empty).
    transport: str = ""
    #: User explicitly requested an absolute target (e.g. -o); skips confinement.
    allow_absolute: bool = False
    #: Extra request headers (auth, etc.).
    headers: dict[str, str] = field(default_factory=dict)
    #: Free-form metadata owned by plugins (e.g. repo relative path).
    #:
    #: One key is read by the engine: ``refetch``, a zero-argument callable that
    #: returns a freshly signed URL for the same object. A source that hands out
    #: URLs with their own expiry (S3 presigned links) sets it so a download that
    #: outlives the signature can resume instead of failing with "forbidden".
    meta: dict[str, Any] = field(default_factory=dict)
