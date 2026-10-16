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
    #: Extra request headers (auth, etc.).
    headers: dict[str, str] = field(default_factory=dict)
    #: Free-form metadata owned by plugins (e.g. repo relative path).
    meta: dict[str, Any] = field(default_factory=dict)
