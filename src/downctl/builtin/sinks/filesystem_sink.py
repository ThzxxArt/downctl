"""The filesystem sink plugin.

Writes downloaded bytes to a local file using positional writes so that
out-of-order piece completion is supported.

Note: the current core writes directly to the target file handle for ranged
writes; this sink is available to plugins and future backends but is not on
the engine's hot path.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, BinaryIO

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
        self._handle: BinaryIO | None = None

    async def open(self, task: DownloadTask, ctx: RuntimeContext) -> None:
        path = str(task.meta.get("path") or task.target)
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._handle = open(path, "r+b" if os.path.exists(path) else "w+b")  # noqa: SIM115

    async def write(self, offset: int, data: bytes) -> None:
        if self._handle is None:
            raise RuntimeError("sink not opened")
        self._handle.seek(offset)
        self._handle.write(data)

    async def close(self, *, success: bool) -> None:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
            self._handle = None
