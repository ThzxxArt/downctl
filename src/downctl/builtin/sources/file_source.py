"""The local-file source plugin (``file://``).

Expands ``file:///abs/path`` into download tasks: a directory is walked
recursively, a plain path is a single file. Downloads go through the ``file``
transport, so the same piece/resume machinery applies (useful for copying into
another directory via ``-d``).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING

from downctl.builtin.transports.file_transport import local_path
from downctl.core.errors import DownctlError
from downctl.core.task import DownloadTask
from downctl.plugin.matching import filters, matches
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
            include, exclude = filters(ctx)
            tasks: list[DownloadTask] = []
            for root, _dirs, files in os.walk(path):
                for name in sorted(files):
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, path).replace(os.sep, "/")
                    if not matches(rel, include, exclude):
                        continue
                    tasks.append(self._task(full, rel))
            if not tasks:
                raise DownctlError(f"no files found under {path!r}")
            return tasks

        if not os.path.isfile(path):
            raise DownctlError(f"file not found: {path}")
        return [self._task(path, os.path.basename(path))]

    def _task(self, full: str, rel: str) -> DownloadTask:
        stat = os.stat(full)
        uri = Path(os.path.abspath(full)).as_uri()
        # A local source can change content without changing its path. Bind the
        # identity to size + mtime + ctime + inode so an edited source (even
        # one whose mtime was restored by tar/cp -p) never resumes into a
        # mixture of old and new bytes.
        identity = hashlib.sha256(
            f"{uri}\0{stat.st_size}\0{stat.st_mtime_ns}\0{stat.st_ctime_ns}\0{stat.st_ino}".encode()
        ).hexdigest()
        return DownloadTask(
            url=uri,
            target=rel,
            size=stat.st_size,
            source=self.name,
            transport=self.name,
            # The resolved source path, so the engine can refuse a "copy" whose
            # destination is this very file. Naming the source is the source's
            # job; deciding whether that is legal belongs to whoever resolves
            # the target.
            meta={"identity": identity, "source_path": os.path.realpath(full)},
        )
