"""The HTTP directory-listing source plugin.

Expands a URL that names a *directory* (its path ends in ``/``) into one
download task per file the server lists. Two listing shapes are supported
because they are the two shapes servers actually expose:

- **HTML** (Apache ``mod_autoindex`` and friends): ``href`` attributes are
  collected with :class:`html.parser.HTMLParser`. Parent/query/fragment links,
  root-relative links, ``mailto:`` links, subdirectories and links that leave
  the listing's origin are ignored.
- **JSON**: ``{"files": [{"name": ..., "size"?: ..., "url"?: ...}]}`` or a bare
  array of names/objects. Detected from ``Content-Type: application/json`` or
  from a body that starts with ``{``.

The final URL after redirects is the base for every relative link, so
``http://host/dir -> 302 -> http://host/dir/`` lists the redirected directory.

``target`` is always a *basename*: names are URL-decoded and then reduced with
the same rule as :func:`downctl.builtin.sources.url_source.filename_from_url`,
so a listing cannot steer a download outside the destination directory.

``--include``/``--exclude`` are applied to that basename. When a listing has
files but the filters remove all of them, resolving fails closed (naming the
filters) instead of silently downloading nothing; an empty listing returns
``[]`` and lets the caller report "no tasks".
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote, urljoin, urlsplit

from downctl.builtin.sources.url_source import filename_from_url
from downctl.core.errors import DownctlError, PermanentHTTPError, TransientHTTPError
from downctl.core.task import DownloadTask
from downctl.plugin.matching import filters, matches
from downctl.plugin.protocols import API_VERSION
from downctl.session import policy_request
from downctl.urls import display_url, redact_text

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

#: Characters kept verbatim when a JSON manifest name becomes a URL path.
#: ``?`` and ``#`` are encoded so a name cannot smuggle a query/fragment into
#: the URL, and ``:`` is encoded so a relative name is never mistaken for a
#: scheme by :func:`urllib.parse.urljoin`.
_NAME_SAFE = "/%!$&'()*+,;=@-._~"
#: Prefixes that never name a file in this directory.
_SKIP_PREFIXES = ("#", "?", "/", "\\", "mailto:")


class HTTPIndexSource:
    """Expand an HTTP(S) directory listing into download tasks."""

    name = "http_index"
    api_version = API_VERSION
    #: Higher than the plain ``url`` source (0), so a directory URL is listed
    #: rather than fetched as if it were the file itself.
    priority = 5
    supports_filters = True

    def can_handle(self, target: str) -> bool:
        """Whether ``target`` is an HTTP(S) URL naming a directory."""
        try:
            parts = urlsplit(target)
        except ValueError:
            return False
        return (
            parts.scheme.lower() in ("http", "https")
            and bool(parts.netloc)
            and parts.path.endswith("/")
        )

    async def resolve(self, target: str, ctx: RuntimeContext) -> list[DownloadTask]:
        """Fetch ``target`` once and turn its listing into tasks."""
        try:
            parts = urlsplit(target)
        except ValueError as exc:
            raise DownctlError(f"invalid directory URL: {display_url(target)}") from exc
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            raise DownctlError(f"http_index only handles HTTP(S) URLs: {display_url(target)}")

        # One request for the whole listing. ``policy_request`` owns the proxy
        # and credential decisions per hop; the response's URL is the final
        # URL after redirects and therefore the base for relative links.
        async with policy_request(
            ctx.session,
            "GET",
            target,
            policy=ctx.proxy_policy,
            allow_redirects=True,
        ) as response:
            status = response.status
            final_url = str(response.url)
            if not 200 <= status < 300:
                _raise_status(status, final_url)
            body = await response.text()
            as_json = response.content_type == "application/json" or body.lstrip().startswith("{")

        entries = _json_entries(body, final_url) if as_json else _html_entries(body, final_url)
        include, exclude = filters(ctx)
        tasks = [task for task in entries if matches(task.target, include, exclude)]
        if not tasks and entries and (include or exclude):
            raise _no_match_error(final_url, include, exclude)
        return tasks


class _LinkParser(HTMLParser):
    """Collect the ``href`` of every ``<a>`` tag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.hrefs.append(value)


def _raise_status(status: int, url: str) -> None:
    """Map a non-2xx listing response to the matching error class."""
    shown = redact_text(url)
    if status == 429 or 500 <= status < 600:
        raise TransientHTTPError(status, shown)
    if 400 <= status < 500:
        raise PermanentHTTPError(status, shown)
    raise DownctlError(f"cannot list directory {shown} (HTTP {status})")


def _no_match_error(base: str, include: list[str], exclude: list[str]) -> DownctlError:
    """The fail-closed error used when filters removed every candidate."""
    parts: list[str] = []
    if include:
        parts.append(f"--include {' '.join(include)}")
    if exclude:
        parts.append(f"--exclude {' '.join(exclude)}")
    return DownctlError(
        f"no files in {display_url(base)} match {' and '.join(parts)}; "
        "check --include/--exclude or the directory URL"
    )


def _origin(url: str) -> tuple[str, str, int] | None:
    """Return a comparable ``(scheme, host, port)`` tuple, or ``None``."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    if port is None:
        port = 80 if scheme == "http" else 443
    return scheme, parts.hostname.lower(), port


def _safe_name(raw: str) -> str:
    """URL-decode ``raw`` and reduce it to a safe basename.

    Percent-encoded separators are decoded *before* the basename is taken, so
    ``a%2F..%2Fescape.bin`` cannot become a path; an empty, ``.`` or ``..``
    name yields ``""`` and the caller skips that entry.
    """
    name = unquote(raw.strip()).replace("\\", "/").rsplit("/", 1)[-1]
    if not name or name in (".", ".."):
        return ""
    return name


def _html_entries(body: str, base: str) -> list[DownloadTask]:
    """Parse an HTML index page into tasks rooted at ``base``."""
    parser = _LinkParser()
    parser.feed(body)
    parser.close()
    base_origin = _origin(base)
    if base_origin is None:
        raise DownctlError(f"invalid directory URL: {display_url(base)}")
    base_path = urlsplit(base).path

    tasks: list[DownloadTask] = []
    for raw in parser.hrefs:
        href = raw.strip()
        if not href:
            continue
        lowered = href.lower()
        if lowered.startswith(_SKIP_PREFIXES) or href.startswith(".."):
            continue
        try:
            absolute = urljoin(base, href)
        except ValueError:
            continue
        if _origin(absolute) != base_origin:
            continue
        path = urlsplit(absolute).path
        # Directory links (including the listing itself) are not files.
        if path.endswith("/") or path == base_path:
            continue
        try:
            name = filename_from_url(absolute)
        except DownctlError:
            continue
        tasks.append(DownloadTask(url=absolute, target=name, source=HTTPIndexSource.name, size=0))
    return tasks


def _json_entries(body: str, base: str) -> list[DownloadTask]:
    """Parse a JSON manifest into tasks rooted at ``base``."""
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise DownctlError(f"directory listing at {display_url(base)} is not valid JSON") from exc
    if isinstance(data, dict):
        items: object = data.get("files", [])
    elif isinstance(data, list):
        items = data
    else:
        raise DownctlError(
            f"directory listing at {display_url(base)} must be a JSON object or array"
        )
    if not isinstance(items, list):
        raise DownctlError(
            f"directory listing at {display_url(base)} has a non-array 'files' field"
        )
    base_origin = _origin(base)
    if base_origin is None:
        raise DownctlError(f"invalid directory URL: {display_url(base)}")

    tasks: list[DownloadTask] = []
    for item in items:
        name = ""
        size = 0
        link = ""
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            raw_name = item.get("name")
            if not isinstance(raw_name, str):
                continue
            name = raw_name
            size = _size(item.get("size"))
            raw_link = item.get("url")
            if isinstance(raw_link, str):
                link = raw_link.strip()
        else:
            continue
        task = _json_task(name, link, size, base, base_origin)
        if task is not None:
            tasks.append(task)
    return tasks


def _json_task(
    raw_name: str,
    link: str,
    size: int,
    base: str,
    base_origin: tuple[str, str, int],
) -> DownloadTask | None:
    """Build one task from a manifest entry, or ``None`` if it is unusable."""
    name = _safe_name(raw_name)
    if not name:
        return None

    absolute = ""
    if link and _origin(link) == base_origin:
        # An absolute, same-origin ``url`` wins over the name: it is the only
        # field that can point at a rewritten download endpoint.
        absolute = link
    if not absolute:
        absolute = urljoin(base, quote(raw_name.strip(), safe=_NAME_SAFE))
    if _origin(absolute) != base_origin:
        return None
    return DownloadTask(
        url=absolute,
        target=name,
        size=size,
        source=HTTPIndexSource.name,
    )


def _size(value: object) -> int:
    """A manifest size, or 0 for anything unknown/invalid."""
    # ``bool`` is an ``int`` subclass; a JSON ``true`` is not a byte count.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value
