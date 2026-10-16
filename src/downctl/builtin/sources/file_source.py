"""The local-file source plugin (``file://``).

Expands ``file:///abs/path`` into download tasks: a directory is walked
recursively, a plain path is a single file. Downloads go through the ``file``
transport, so the same piece/resume machinery applies (useful for copying into
another directory via ``-d``).
"""

from __future__ import annotations

import fnmatch
import os
from typing import TYPE_CHECKING
from urllib.parse import quote

from downctl.builtin.transports.file_transport import local_path
from downctl.core.errors import DownctlError
from downctl.core.task import DownloadTask
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

SCHEME = "file://"


class FileSource:
    """Expand a local file or directory into download tasks."""

    name = "file"
    api_version = API_VERSION
    priority = 10

    def can_handle(self, target: str) -> bool:
        return target.startswith(SCHEME)

    async def resolve(self, target: str, ctx: RuntimeContext) -> list[DownloadTask]:
        path = local_path(target)
        if os.path.isdir(path):
            include = list(ctx.params.get("include") or [])
            exclude = list(ctx.params.get("exclude") or [])
            tasks: list[DownloadTask] = []
            for root, _dirs, files in os.walk(path):
                for name in sorted(files):
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, path).replace(os.sep, "/")
                    if include and not any(fnmatch.fnmatchcase(rel, p) for p in include):
                        continue
                    if any(fnmatch.fnmatchcase(rel, p) for p in exclude):
                        continue
                    tasks.append(self._task(full, rel))
            if not tasks:
                raise DownctlError(f"no files found under {path!r}")
            return tasks

        if not os.path.isfile(path):
            raise DownctlError(f"file not found: {path}")
        return [self._task(path, os.path.basename(path))]

    def _task(self, full: str, rel: str) -> DownloadTask:
        return DownloadTask(
            url="file://" + quote(os.path.abspath(full).replace(os.sep, "/"), safe="/:"),
            target=rel,
            size=os.path.getsize(full),
            source=self.name,
            transport=self.name,
        )
