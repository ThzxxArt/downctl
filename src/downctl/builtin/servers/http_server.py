"""The built-in HTTP server plugin (``downctl serve``).

It exposes one local directory (or a single file) over HTTP so another machine
can fetch it *with the same resumable semantics downctl itself speaks*: byte
ranges, ``If-Range``, ``416`` and a strong ETag taken from a completed
download's ``.downctl-done`` marker when one exists.

Two properties are security boundaries rather than features:

- Every path is percent-decoded, resolved with :func:`os.path.realpath` and
  checked against the real root, so neither ``..`` nor a symlink can escape.
- Basic auth is checked before the path is resolved, so a wrong credential
  learns nothing about which files exist.

Request logging deliberately prints only the request target and status: the
query string may carry a token and the ``Authorization`` header always does.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import ipaddress
import os
import re
import secrets
import signal
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from email.utils import formatdate, parsedate_to_datetime
from typing import TYPE_CHECKING
from urllib.parse import quote_from_bytes, unquote

from aiohttp import web

from downctl import console
from downctl.core import pieces
from downctl.core.errors import DownctlError
from downctl.plugin.protocols import API_VERSION
from downctl.urls import MAX_VALIDATOR_LENGTH

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

CHUNK_SIZE = 64 * 1024

_BASIC_REALM = 'Basic realm="downctl"'
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
#: RFC 9110 ``etagc``: visible ASCII except DQUOTE, plus obs-text.
_ETAGC = re.compile(r"[\x21\x23-\x7e\x80-\xff]*\Z")
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)", re.IGNORECASE)

_ALLOWED_OPTIONS = frozenset({"bind", "port", "auth", "index", "quiet"})
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})


@dataclass(frozen=True)
class _Options:
    """Strictly validated server options."""

    bind: str = "127.0.0.1"
    port: int = 8080
    auth: tuple[str, str] | None = None
    index: bool = False
    quiet: bool = False


@dataclass(frozen=True)
class _Range:
    """One satisfiable byte range (inclusive bounds)."""

    start: int
    end: int


@dataclass(frozen=True)
class _RangeDecision:
    """The outcome of parsing a single-range header."""

    byte_range: _Range | None = None
    unsatisfiable: bool = False


def _parse_options(options: Mapping[str, object]) -> _Options:
    """Validate ``options`` strictly; unknown keys are configuration errors."""
    if not isinstance(options, Mapping):
        raise DownctlError("http server options must be a mapping")
    unknown = sorted(str(key) for key in options if key not in _ALLOWED_OPTIONS)
    if unknown:
        raise DownctlError(f"unknown http server option(s): {', '.join(unknown)}")

    bind = options.get("bind", "127.0.0.1")
    if not isinstance(bind, str) or not bind:
        raise DownctlError("http server option 'bind' must be a non-empty string")

    port = options.get("port", 8080)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise DownctlError("http server option 'port' must be an integer between 0 and 65535")

    raw_auth = options.get("auth")
    auth = _parse_auth_option(raw_auth) if raw_auth is not None else None

    index = options.get("index", False)
    if not isinstance(index, bool):
        raise DownctlError("http server option 'index' must be true or false")

    quiet = options.get("quiet", False)
    if not isinstance(quiet, bool):
        raise DownctlError("http server option 'quiet' must be true or false")

    return _Options(bind=bind, port=port, auth=auth, index=index, quiet=quiet)


def _parse_auth_option(value: object) -> tuple[str, str]:
    """Parse a ``USER:PASS`` option without ever echoing the secret."""
    if not isinstance(value, str):
        raise DownctlError("http server option 'auth' must be a 'USER:PASS' string or None")
    user, separator, password = value.partition(":")
    if not separator or not user or not password or _CONTROL_CHARS.search(value):
        raise DownctlError("http server option 'auth' must be a non-empty 'USER:PASS' string")
    return user, password


def _auth_material(auth: tuple[str, str] | None) -> bytes | None:
    """Validate ``create_app``'s auth pair and return the expected credentials."""
    if auth is None:
        return None
    if (
        not isinstance(auth, tuple)
        or len(auth) != 2
        or not all(isinstance(part, str) for part in auth)
    ):
        raise DownctlError("http server auth must be a (user, password) pair")
    user, password = auth
    if not user or not password or ":" in user or _CONTROL_CHARS.search(user + password):
        raise DownctlError("http server auth must contain a non-empty user and password")
    return f"{user}:{password}".encode()


def _resolve_root(root: str) -> tuple[str, bool]:
    """Return ``(real_root, is_directory)`` or raise when ``root`` is unusable."""
    real_root = os.path.realpath(root)
    if not os.path.exists(real_root):
        raise DownctlError(f"cannot serve {root!r}: no such file or directory")
    if os.path.isdir(real_root):
        return real_root, True
    if os.path.isfile(real_root):
        return real_root, False
    raise DownctlError(f"cannot serve {root!r}: not a regular file or directory")


def create_app(
    root: str,
    *,
    auth: tuple[str, str] | None = None,
    index: bool = False,
    quiet: bool = False,
) -> web.Application:
    """Build the aiohttp application for ``root`` without binding a port.

    Exposed so tests (and embedders) can drive it through
    :class:`aiohttp.test_utils.TestClient`.
    """
    real_root, root_is_directory = _resolve_root(root)
    expected_auth = _auth_material(auth)

    async def handler(request: web.Request) -> web.StreamResponse:
        return await _handle_request(
            request,
            real_root=real_root,
            root_is_directory=root_is_directory,
            expected_auth=expected_auth,
            index=index,
        )

    app = web.Application()
    app.router.add_route("*", "/", handler)
    app.router.add_route("*", "/{tail:.*}", handler)
    if not quiet:
        app.on_response_prepare.append(_log_request)
    return app


async def _handle_request(
    request: web.Request,
    *,
    real_root: str,
    root_is_directory: bool,
    expected_auth: bytes | None,
    index: bool,
) -> web.StreamResponse:
    """Dispatch one request after authentication and path containment."""
    if expected_auth is not None and not _authorized(request, expected_auth):
        return _unauthorized()

    if request.method not in ("GET", "HEAD"):
        raise web.HTTPMethodNotAllowed(request.method, ["GET", "HEAD"])

    decoded = _decoded_path(request)
    if decoded is None:
        return _forbidden()
    if ".." in decoded.split("/"):
        # Parent segments are never followed, even when they would resolve back
        # inside the root: the listing never generates them, so any occurrence
        # is an attempt to reason about a path the server did not hand out.
        return _forbidden()

    if root_is_directory:
        resolved = os.path.realpath(os.path.join(real_root, decoded.lstrip("/")))
        if not _within(resolved, real_root):
            return _forbidden()
    else:
        # A single-file root is served at "/" (and at its own basename, which
        # makes ``downctl serve model.bin`` usable without guessing the URL).
        normalized = "/" + os.path.normpath("/" + decoded.lstrip("/")).lstrip("/")
        if normalized not in ("/", "/" + os.path.basename(real_root)):
            return _not_found()
        resolved = real_root

    if os.path.isdir(resolved):
        if not index:
            return _not_found()
        if not decoded.endswith("/"):
            # Relative links in the listing are resolved against the request
            # URL, so serving ``/sub`` directly would make every href point at
            # the parent directory. The slash form is the canonical one.
            return _redirect_to_slash(request)
        return await _directory_response(request, resolved, real_root)
    if not os.path.isfile(resolved):
        return _not_found()
    return await _file_response(request, resolved)


def _request_target(request: web.Request) -> str:
    """The raw request target without its query string or fragment."""
    return request.raw_path.partition("?")[0].partition("#")[0] or "/"


def _redirect_to_slash(request: web.Request) -> web.Response:
    """Permanently redirect a directory URL to its trailing-slash form."""
    location = _request_target(request) + "/"
    if request.query_string:
        location += "?" + request.query_string
    return web.Response(status=301, headers={"Location": location})


def _decoded_path(request: web.Request) -> str | None:
    """Percent-decode the raw request target before any filesystem check.

    ``request.path`` is already decoded once by yarl; using ``raw_path`` keeps
    this function the single, explicit decode point (and avoids decoding a
    literal ``%25`` a second time). ``None`` means the path is unusable.
    """
    raw = _request_target(request)
    try:
        decoded = unquote(raw, errors="strict")
    except UnicodeDecodeError:
        return None
    if "\x00" in decoded:
        return None
    if not decoded.startswith("/"):
        decoded = "/" + decoded
    return decoded


def _within(path: str, root: str) -> bool:
    """Whether ``path`` is ``root`` or lies below it."""
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _authorized(request: web.Request, expected: bytes) -> bool:
    """Check ``Authorization: Basic`` with a constant-time comparison."""
    header = request.headers.get("Authorization", "")
    scheme, separator, parameter = header.partition(" ")
    if not separator or scheme.lower() != "basic":
        return False
    try:
        supplied = base64.b64decode(parameter.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    return secrets.compare_digest(supplied, expected)


def _not_found() -> web.Response:
    return web.Response(status=404, text="not found")


def _forbidden() -> web.Response:
    return web.Response(status=403, text="forbidden")


def _unauthorized() -> web.Response:
    return web.Response(
        status=401,
        text="authentication required",
        headers={"WWW-Authenticate": _BASIC_REALM},
    )


def _entity_headers(path: str, stat: os.stat_result) -> dict[str, str]:
    """Headers shared by a full response, a range response and ``416``."""
    return {
        "ETag": _etag_for(path, stat),
        "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
        "Accept-Ranges": "bytes",
        "Content-Type": "application/octet-stream",
    }


def _etag_for(path: str, stat: os.stat_result) -> str:
    """Prefer the witness a finished download recorded in ``.downctl-done``.

    The marker only describes the exact file it was written for: ``size`` and
    ``mtime_ns`` are its binding. Using its validator for a *different* file
    that happens to live at the same path is what let a same-size replacement
    answer a stale ``If-Range`` with ``206`` (and splice the two revisions).
    """
    record = pieces.load_completion(path)
    if (
        record is not None
        and pieces.witness_of(record) == "validator"
        and record.get("size") == stat.st_size
        and record.get("mtime_ns") == stat.st_mtime_ns
    ):
        marker_etag = _quoted_etag(str(record.get("validator", "")))
        if marker_etag is not None:
            return marker_etag
    return f'W/"{stat.st_size}-{stat.st_mtime_ns}"'


def _quoted_etag(value: str) -> str | None:
    """Turn a recorded witness into a syntactically valid ETag.

    A real marker stores the server's ETag verbatim (normally including its
    quotes); a hand-written one may store just the opaque value. Both are
    accepted, while control characters, an over-long value or an unquotable
    DQUOTE fall back to the weak size/mtime ETag.
    """
    value = value.strip()
    if not value or len(value) > MAX_VALIDATOR_LENGTH or _CONTROL_CHARS.search(value):
        return None
    if value[:2].upper() == "W/":
        opaque = value[2:]
        return f"W/{opaque}" if _valid_quoted(opaque) else None
    if _valid_quoted(value):
        return value
    if '"' in value:
        return None
    return f'"{value}"'


def _valid_quoted(value: str) -> bool:
    return (
        len(value) >= 2
        and value[0] == '"'
        and value[-1] == '"'
        and '"' not in value[1:-1]
        and bool(_ETAGC.fullmatch(value[1:-1]))
    )


def _split_etag_list(value: str) -> list[str]:
    """Split an ``If-Match``/``If-None-Match`` list on commas outside quotes.

    A comma is legal inside an opaque tag (``"a,b"``), so splitting the header
    blindly would turn one valid validator into two invalid ones.
    """
    tags: list[str] = []
    current: list[str] = []
    quoted = False
    for char in value:
        if char == '"':
            quoted = not quoted
        if char == "," and not quoted:
            tags.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tags.append("".join(current).strip())
    return [tag for tag in tags if tag]


def _etag_opaque(tag: str) -> str | None:
    """The quoted opaque value of an entity-tag, ignoring its weakness."""
    tag = tag.strip()
    if tag[:2].upper() == "W/":
        tag = tag[2:].strip()
    return tag if _valid_quoted(tag) else None


def _strong_etag(tag: str) -> str | None:
    """The tag when it can take part in a strong comparison, else ``None``."""
    tag = tag.strip()
    if tag[:2].upper() == "W/":
        return None
    return tag if _valid_quoted(tag) else None


def _etag_matches(header: str, etag: str, *, weak: bool) -> bool:
    """Whether an ``If-Match``/``If-None-Match`` list names ``etag``.

    ``If-None-Match`` uses weak comparison (RFC 7232 section 3.2); ``If-Match``
    uses strong comparison (section 3.1), so a weak validator never satisfies
    it. ``*`` always matches because the representation exists.
    """
    candidates = _split_etag_list(header)
    if any(candidate == "*" for candidate in candidates):
        return True
    if weak:
        opaque = _etag_opaque(etag)
        return opaque is not None and any(
            _etag_opaque(candidate) == opaque for candidate in candidates
        )
    strong = _strong_etag(etag)
    return strong is not None and any(_strong_etag(candidate) == strong for candidate in candidates)


def _http_date(value: str | None) -> float | None:
    """Parse an HTTP-date into a POSIX timestamp, or ``None`` when unparseable."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError):
        return None


def _not_modified(headers: Mapping[str, str]) -> web.Response:
    """A ``304`` that still names the selected representation."""
    return web.Response(status=304, headers=dict(headers))


def _precondition_failed(headers: Mapping[str, str]) -> web.Response:
    """A ``412`` that still carries the entity headers."""
    return web.Response(status=412, text="precondition failed", headers=dict(headers))


def _precondition_response(
    request: web.Request, headers: Mapping[str, str], stat: os.stat_result
) -> web.Response | None:
    """Evaluate RFC 7232 preconditions in their required order.

    ``If-Match`` wins over ``If-Unmodified-Since``, and the presence of
    ``If-None-Match`` makes ``If-Modified-Since`` irrelevant even when it does
    not match (RFC 7232 section 6). Dates have one-second resolution, matching
    ``Last-Modified``, so the comparison truncates ``st_mtime``.
    """
    etag = headers["ETag"]
    if_match = request.headers.get("If-Match")
    if if_match is not None:
        if not _etag_matches(if_match, etag, weak=False):
            return _precondition_failed(headers)
    else:
        unmodified_since = _http_date(request.headers.get("If-Unmodified-Since"))
        if unmodified_since is not None and int(stat.st_mtime) > unmodified_since:
            return _precondition_failed(headers)

    if_none_match = request.headers.get("If-None-Match")
    if if_none_match is not None:
        if _etag_matches(if_none_match, etag, weak=True):
            return _not_modified(headers)
    else:
        modified_since = _http_date(request.headers.get("If-Modified-Since"))
        if modified_since is not None and int(stat.st_mtime) <= modified_since:
            return _not_modified(headers)
    return None


async def _file_response(request: web.Request, path: str) -> web.StreamResponse:
    """Serve a file, honouring conditional headers and a single byte range."""
    try:
        stat = os.stat(path)
    except OSError:
        return _not_found()

    size = stat.st_size
    headers = _entity_headers(path, stat)
    precondition = _precondition_response(request, headers, stat)
    if precondition is not None:
        return precondition

    range_header = request.headers.get("Range")
    if range_header is not None and "," not in range_header:
        decision = _parse_range(range_header, size)
        if_range = request.headers.get("If-Range", "")
        if decision is not None and (not if_range or _if_range_matches(if_range, headers)):
            if decision.unsatisfiable or decision.byte_range is None:
                headers["Content-Range"] = f"bytes */{size}"
                return web.Response(status=416, text="range not satisfiable", headers=headers)
            byte_range = decision.byte_range
            headers["Content-Range"] = f"bytes {byte_range.start}-{byte_range.end}/{size}"
            return await _stream_file(
                request,
                path,
                headers,
                status=206,
                start=byte_range.start,
                length=byte_range.end - byte_range.start + 1,
            )

    return await _stream_file(request, path, headers, status=200, start=0, length=size)


async def _stream_file(
    request: web.Request,
    path: str,
    headers: dict[str, str],
    *,
    status: int,
    start: int,
    length: int,
) -> web.StreamResponse:
    """Stream ``length`` bytes from ``start`` (or just announce them for HEAD)."""
    response = web.StreamResponse(status=status, headers=headers)
    response.content_length = length

    try:
        handle = open(path, "rb")  # noqa: SIM115 - closed in finally
        handle.seek(start)
    except OSError:
        return _not_found()

    try:
        await response.prepare(request)
        if request.method == "HEAD":
            await response.write_eof()
            return response

        remaining = length
        while remaining > 0:
            chunk = await asyncio.to_thread(handle.read, min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            await response.write(chunk)
            remaining -= len(chunk)
        await response.write_eof()
        return response
    finally:
        handle.close()


def _parse_range(header: str, size: int) -> _RangeDecision | None:
    """Parse a single ``bytes`` range.

    ``None`` means "not a range we understand" (including multiple ranges),
    which makes the caller send the full representation. A malformed single
    range is also ignored rather than answered with an error, as RFC 9110
    permits.
    """
    match = _RANGE_RE.fullmatch(header.strip())
    if match is None:
        return None
    first, last = match.groups()
    if not first and not last:
        return None

    if not first:
        suffix = int(last)
        if suffix <= 0 or size <= 0:
            return _RangeDecision(unsatisfiable=True)
        return _RangeDecision(byte_range=_Range(max(0, size - suffix), size - 1))

    start = int(first)
    if start >= size:
        return _RangeDecision(unsatisfiable=True)
    end = min(int(last), size - 1) if last else size - 1
    if end < start:
        return _RangeDecision(unsatisfiable=True)
    return _RangeDecision(byte_range=_Range(start, end))


def _if_range_matches(header: str, headers: Mapping[str, str]) -> bool:
    """Whether ``If-Range`` names the current ETag or Last-Modified value."""
    value = header.strip()
    if not value:
        return False
    return value == headers.get("ETag") or value == headers.get("Last-Modified")


async def _directory_response(request: web.Request, directory: str, real_root: str) -> web.Response:
    """Render a deterministic HTML listing, escaping names and skipping escapes."""
    entries: list[str] = []
    try:
        with os.scandir(directory) as iterator:
            for entry in sorted(iterator, key=lambda item: item.name):
                resolved = os.path.realpath(entry.path)
                if not _within(resolved, real_root):
                    # A symlink pointing outside the served tree is not shown,
                    # even though requesting it directly would be a 403.
                    continue
                try:
                    is_directory = entry.is_dir(follow_symlinks=True)
                except OSError:
                    is_directory = False
                try:
                    href = quote_from_bytes(os.fsencode(entry.name), safe="")
                except (UnicodeEncodeError, OSError):
                    # A name that is not valid UTF-8 (Python decodes it with
                    # ``surrogateescape``) cannot be encoded into the response.
                    # One such entry must not kill the whole listing.
                    continue
                # The label is HTML text, not a URL: replace what UTF-8 cannot
                # carry so the response body stays encodable.
                label = entry.name.encode("utf-8", "replace").decode("utf-8")
                if is_directory:
                    href += "/"
                    label += "/"
                entries.append(
                    f'<li><a href="{html.escape(href, quote=True)}">{html.escape(label)}</a></li>'
                )
    except OSError:
        return _forbidden()

    listing = "\n".join(entries) if entries else "<li>(empty)</li>"
    shown = html.escape(_decoded_path(request) or "/")
    page = (
        "<!doctype html>\n"
        '<html><head><meta charset="utf-8">'
        f"<title>Index of {shown}</title></head>\n"
        f"<body><h1>Index of {shown}</h1><ul>\n{listing}\n</ul></body></html>\n"
    )
    return web.Response(
        text=page,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


async def _log_request(request: web.Request, response: web.StreamResponse) -> None:
    """Log one request to stderr without its query string or credentials."""
    remote = request.remote or "-"
    version = f"{request.version.major}.{request.version.minor}"
    target = _request_target(request)
    print(f'{remote} "{request.method} {target} HTTP/{version}" {response.status}', file=sys.stderr)


def _is_loopback(bind: str) -> bool:
    """Whether binding ``bind`` stays on this machine."""
    host = bind.strip().strip("[]").lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # An unresolved hostname is not provably loopback, so warn instead of
        # silently assuming it is safe.
        return False


def _serve_url(bind: str, port: int) -> str:
    host = bind
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}/"


def _install_stop_handlers(
    loop: asyncio.AbstractEventLoop, stop: asyncio.Event
) -> list[signal.Signals]:
    """Install SIGINT/SIGTERM handlers, or return ``[]`` for the fallback path.

    ``add_signal_handler`` is unavailable on Windows and in non-main threads;
    in that case Ctrl-C still raises :class:`KeyboardInterrupt`, which
    :meth:`HTTPFileServer.serve` turns into a normal cleanup.
    """
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            for added in installed:
                loop.remove_signal_handler(added)
            return []
        installed.append(signum)
    return installed


class HTTPFileServer:
    """Serve a local file or directory over HTTP until interrupted."""

    name = "http"
    api_version = API_VERSION
    priority = 0

    async def serve(
        self,
        root: str,
        ctx: RuntimeContext,
        options: Mapping[str, object],
    ) -> None:
        """Run the HTTP server on ``bind``/``port`` until SIGINT or SIGTERM."""
        parsed = _parse_options(options)
        app = create_app(root, auth=parsed.auth, index=parsed.index, quiet=parsed.quiet)

        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, parsed.bind, parsed.port)
            await site.start()
        except OSError as exc:
            await runner.cleanup()
            raise DownctlError(f"cannot serve on {parsed.bind}:{parsed.port}: {exc}") from exc
        except BaseException:
            await runner.cleanup()
            raise

        addresses = runner.addresses
        port = int(addresses[0][1]) if addresses else parsed.port
        # ``console.result``, not ``console.info``: the bound URL (including the
        # real port when ``--port 0`` was asked for) is the answer to the
        # command, and ``-q`` must not throw it away.
        console.result(f"serving {root} on {_serve_url(parsed.bind, port)}")
        if not _is_loopback(parsed.bind):
            console.warn(
                f"serving on {parsed.bind}:{port}, which is not a loopback address; "
                "this exposes the directory to the network",
                always=True,
            )

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        installed = _install_stop_handlers(loop, stop)
        try:
            await stop.wait()
        except KeyboardInterrupt:
            # No signal handler could be installed (Windows / non-main thread):
            # Ctrl-C is the documented way to stop the server there.
            pass
        finally:
            for signum in installed:
                loop.remove_signal_handler(signum)
            await runner.cleanup()
