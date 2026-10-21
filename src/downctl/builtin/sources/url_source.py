"""The plain-URL source plugin.

Handles ordinary ``http(s)://`` links. Full task construction (destination
resolution, probing) lands in P4; the shape of the plugin is final.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, unquote, urlparse

from downctl.core.errors import DownctlError
from downctl.core.task import DownloadTask
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

#: Query parameters that make a URL *presigned*: the whole query is an
#: expiring credential rather than part of the object's identity. A resumed
#: download compares the sealed identity, so a rotating signature would look
#: like a different object on every run and every retry would start over.
_PRESIGNED_PARAMS = frozenset(
    {"x-amz-signature", "x-amz-credential", "x-goog-signature", "awsaccesskeyid"}
)


def is_presigned(url: str) -> bool:
    """Whether ``url``'s query carries its own expiring signature."""
    try:
        query = urlparse(url).query
    except ValueError:
        return False
    return any(key.lower() in _PRESIGNED_PARAMS for key, _ in parse_qsl(query))


class URLSource:
    """Resolve plain HTTP(S) URLs into download tasks."""

    name = "url"
    api_version = API_VERSION
    priority = 0
    #: A single file has no listing to filter; ``--include/--exclude`` are
    #: meaningless here, and the app warns instead of pretending otherwise.
    supports_filters = False

    def can_handle(self, target: str) -> bool:
        return target.startswith(("http://", "https://"))

    async def resolve(self, target: str, ctx: RuntimeContext) -> list[DownloadTask]:
        name = filename_from_url(target)
        task = DownloadTask(url=target, target=name, source=self.name)
        if is_presigned(target):
            # Identity hashes the URL without its query: two runs of the same
            # signed link resume, and a different object behind a new signature
            # is still a different path.
            task.meta["redact_url"] = True
        return [task]


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
    if any(ord(character) < 32 for character in name):
        # A NUL (or newline) cannot be a path component; it reached
        # ``os.lstat`` as ``ValueError: embedded null character in path``.
        raise DownctlError(f"cannot use a control character in the filename of: {url}")
    return name
