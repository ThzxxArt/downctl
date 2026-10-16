"""The filesystem sink plugin.

Writes downloaded bytes to a local file using positional writes so that
out-of-order piece completion is supported. Full open/write/close semantics
land in P2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext
    from downctl.core.task import DownloadTask


class FilesystemSink:
    """Persist a task's bytes to ``task.target`` on the local filesystem."""

    name = "filesystem"
    api_version = API_VERSION
    priority = 0

    def __init__(self) -> None:
        self._fh = None

    async def open(self, task: DownloadTask, ctx: RuntimeContext) -> None:
        raise NotImplementedError("filesystem sink lands in P2")

    async def write(self, offset: int, data: bytes) -> None:
        raise NotImplementedError("filesystem sink lands in P2")

    async def close(self, *, success: bool) -> None:
        raise NotImplementedError("filesystem sink lands in P2")
