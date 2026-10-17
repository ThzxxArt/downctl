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

from downctl.builtin.transports._sftp import SCHEME, connect, host_key, parse_url
from downctl.core.errors import DownctlError
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
        options = dict(ctx.plugin_options(self.name))
        parsed = parse_url(target, options)
        # Keep inline credentials out of the shared config and out of task URLs
        # (so they never reach the failed log); the transport reads them from
        # this per-run map, keyed by host.
        if parsed.password:
            credentials = ctx.params.setdefault("sftp_credentials", {})
            credentials[host_key(parsed)] = parsed.password
            options.setdefault("password", parsed.password)

        try:
            connection = await connect(parsed, options)
        except DownctlError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DownctlError(f"SFTP connection to {parsed.host} failed: {exc}") from exc

        include = list(ctx.params.get("include") or [])
        exclude = list(ctx.params.get("exclude") or [])
        tasks: list[DownloadTask] = []
        try:
            async with connection.start_sftp_client() as sftp:
                try:
                    is_dir = parsed.path.endswith("/") or await self._is_dir(sftp, parsed.path)
                    if is_dir:
                        tasks = await self._list_dir(sftp, parsed, include, exclude)
                    else:
                        tasks = [await self._single(sftp, parsed)]
                except Exception as exc:  # noqa: BLE001
                    raise DownctlError(f"SFTP error for {target}: {exc}") from exc
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
    # Always carry the port the source actually used so the transport does not
    # fall back to a differently-configured default.
    authority = f"{authority}:{parsed.port}"
    return f"sftp://{authority}/{quote(path.lstrip('/'), safe='/')}"


def _no_files(target: str) -> Exception:
    from downctl.core.errors import DownctlError

    return DownctlError(f"no files found at {target!r}")
