"""The SFTP source plugin.

Expands ``sftp://[user@]host[:port]/path`` into download tasks. A path ending
in ``/`` (or naming a directory) is listed; a plain path is a single file.
Credentials come from the URL or ``[plugins.sftp]``; passwords are stripped
from task URLs so they never reach the failed log.

Requires the optional ``asyncssh`` dependency (``downctl[sftp]``).
"""

from __future__ import annotations

import posixpath
import stat as stat_module
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from downctl.builtin.transports._sftp import SCHEME, connect, parse_url
from downctl.core.task import DownloadTask
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext


class SFTPSource:
    """Expand an SFTP file or directory into download tasks."""

    name = "sftp"
    api_version = API_VERSION
    priority = 10

    def can_handle(self, target: str) -> bool:
        return target.startswith(SCHEME)

    async def resolve(self, target: str, ctx: RuntimeContext) -> list[DownloadTask]:
        options = ctx.plugin_options(self.name)
        parsed = parse_url(target, options)
        # Never let an inline password leak into task URLs (and the failed log).
        if parsed.password and not options.get("password"):
            ctx.options.setdefault(self.name, {})["password"] = parsed.password

        connection = await connect(parsed, options)
        include = list(ctx.params.get("include") or [])
        exclude = list(ctx.params.get("exclude") or [])
        tasks: list[DownloadTask] = []
        try:
            async with connection.start_sftp_client() as sftp:
                if parsed.path.endswith("/") or await self._is_dir(sftp, parsed.path):
                    tasks = await self._list_dir(sftp, parsed, include, exclude)
                else:
                    tasks = [await self._single(sftp, parsed)]
        finally:
            connection.close()

        if not tasks:
            raise _no_files(target)
        return tasks

    async def _is_dir(self, sftp: Any, path: str) -> bool:
        try:
            attrs = await sftp.stat(path)
        except Exception:  # noqa: BLE001
            return False
        permissions = attrs.permissions
        return bool(permissions and stat_module.S_ISDIR(permissions))

    async def _list_dir(
        self,
        sftp: Any,
        parsed: Any,
        include: list[str],
        exclude: list[str],
    ) -> list[DownloadTask]:
        import fnmatch

        tasks: list[DownloadTask] = []
        for name in await sftp.listdir(parsed.path):
            full = posixpath.join(parsed.path, name)
            attrs = await sftp.stat(full)
            permissions = attrs.permissions
            if permissions and stat_module.S_ISDIR(permissions):
                continue
            if include and not any(fnmatch.fnmatchcase(name, pat) for pat in include):
                continue
            if any(fnmatch.fnmatchcase(name, pat) for pat in exclude):
                continue
            tasks.append(
                DownloadTask(
                    url=_task_url(parsed, full),
                    target=name,
                    size=int(attrs.size or 0),
                    source=self.name,
                    transport=self.name,
                )
            )
        return tasks

    async def _single(self, sftp: Any, parsed: Any) -> DownloadTask:
        attrs = await sftp.stat(parsed.path)
        return DownloadTask(
            url=_task_url(parsed, parsed.path),
            target=posixpath.basename(parsed.path.rstrip("/")),
            size=int(attrs.size or 0),
            source=self.name,
            transport=self.name,
        )


def _task_url(parsed: Any, path: str) -> str:
    authority = parsed.host if not parsed.user else f"{parsed.user}@{parsed.host}"
    if parsed.port and parsed.port != 22:
        authority = f"{authority}:{parsed.port}"
    return f"sftp://{authority}/{quote(path.lstrip('/'), safe='/')}"


def _no_files(target: str) -> Exception:
    from downctl.core.errors import DownctlError

    return DownctlError(f"no files found at {target!r}")
