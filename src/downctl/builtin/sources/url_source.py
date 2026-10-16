"""The plain-URL source plugin.

Handles ordinary ``http(s)://`` links. Full task construction (destination
resolution, probing) lands in P4; the shape of the plugin is final.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

from downctl.core.errors import DownctlError
from downctl.core.task import DownloadTask
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext


class URLSource:
    """Resolve plain HTTP(S) URLs into download tasks."""

    name = "url"
    api_version = API_VERSION
    priority = 0

    def can_handle(self, target: str) -> bool:
        return target.startswith(("http://", "https://"))

    async def resolve(self, target: str, ctx: RuntimeContext) -> list[DownloadTask]:
        name = filename_from_url(target)
        return [DownloadTask(url=target, target=name, source=self.name)]


def filename_from_url(url: str) -> str:
    """Best-effort filename from a URL path, URL-decoded.

    Only the final path segment is used; any directory separators or ``..``
    re-introduced by percent-decoding are stripped to their basename so a
    crafted URL cannot escape the destination directory.
    """
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1])
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in (".", ".."):
        raise DownctlError(f"cannot infer filename from URL: {url}")
    return name
