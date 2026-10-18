"""Fixtures for integration tests: a controllable local HTTP file server.

The server is deliberately opinionated about HTTP range semantics so the
content-truth guarantees can be tested end to end:

- ``/versioned`` advertises a strong ETag per revision, honours ``If-Range``
  (a stale validator makes it answer ``200`` with the whole representation),
  and answers an unsatisfiable range with ``416``.
- ``/weak`` sends a weak ETag only, ``/mtime`` a ``Last-Modified`` only and
  ``/no-etag`` no validator at all.
- ``/wrong-range`` answers a ranged request with a window that does not match
  the request, ``/shrink`` shrinks the object right after the first probe.

Every handler records the ``Range``/``If-Range`` headers it saw so a test can
assert *which bytes* were fetched, not merely that the file ended up complete.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

CHUNK = 64 * 1024
#: Fixed timestamp per revision for the Last-Modified-only endpoint.
_MTIME = "Mon, 01 Jan 2024 00:00:{:02d} GMT"


@dataclass
class ServerState:
    data: bytes
    ranges: list[str | None] = field(default_factory=list)
    #: ``If-Range`` seen on ranged GETs (``None`` = unconditional request).
    if_ranges: list[str | None] = field(default_factory=list)
    #: HEAD requests seen (the probe path).
    heads: int = 0
    #: 416 answers handed out.
    rejections: int = 0
    #: Current revision number.
    version: int = 1
    #: Replacement content per revision (missing = ``data``).
    replacements: dict[int, bytes] = field(default_factory=dict)
    #: Make the ETag change on every single response.
    unstable_etag: bool = False
    etag_calls: int = 0
    #: Switch revision once this many ranged GETs have been served (0 = before
    #: the first one). Models an object replaced *during* a transfer.
    switch_after_ranges: int | None = None
    #: Ranged GETs served so far by the versioned endpoints.
    ranges_served: int = 0
    #: Serve ``shrunk`` for GETs while the first HEAD still reports ``data``.
    shrunk: bytes | None = None
    #: ``/flaky-then-ok``: fail this many HEADs with 500 before succeeding.
    flaky_failures: int = 0
    flaky_seen: int = 0
    #: ``/throttle``: answer the first HEAD with 429 + Retry-After.
    throttled: bool = False
    throttle_seen: int = 0
    retry_after: float = 1.0
    #: ``/slow-stall``: stall the first ranged GET, then behave normally.
    stall_once: bool = False
    stalled: bool = False
    #: Per-chunk delay for ``/slow`` (seconds).
    chunk_delay: float = 0.03
    #: Delay before ``/slow-head`` answers (seconds), to prove probes overlap.
    head_delay: float = 0.0
    #: Requests per path, for "no retry happened" assertions.
    hits: dict[str, int] = field(default_factory=dict)

    @property
    def current(self) -> bytes:
        return self.replacements.get(self.version, self.data)

    @property
    def get_body(self) -> bytes:
        """What a ranged GET serves (``/shrink`` serves the shrunken object)."""
        return self.shrunk if self.shrunk is not None else self.current

    def etag(self) -> str:
        self.etag_calls += 1
        if self.unstable_etag:
            return f'"unstable-{self.etag_calls}"'
        return f'"v{self.version}"'

    def last_modified(self) -> str:
        return _MTIME.format(min(self.version, 59))

    def replace(self, payload: bytes, *, version: int | None = None) -> int:
        """Publish a new revision (same size by default) and return its number."""
        self.version = self.version + 1 if version is None else version
        self.replacements[self.version] = payload
        return self.version

    def count(self, name: str) -> int:
        return self.hits.get(name, 0)


def _note(state: ServerState, name: str) -> None:
    state.hits[name] = state.hits.get(name, 0) + 1


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Return an inclusive ``(start, end)`` or ``None`` for a malformed header."""
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", header.strip())
    if match is None:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else size - 1
    return start, min(end, size - 1)


def _range_response(state: ServerState, header: str, body: bytes) -> web.Response:
    parsed = _parse_range(header, len(body))
    if parsed is None:
        # A malformed Range is the client's fault: say so with 400 instead of
        # letting an assertion inside the handler surface as a 500.
        return web.Response(status=400, text="malformed Range header")
    start, end = parsed
    if start >= len(body):
        state.rejections += 1
        return web.Response(status=416, headers={"Content-Range": f"bytes */{len(body)}"})
    return web.Response(
        status=206,
        body=body[start : end + 1],
        headers={
            "Content-Range": f"bytes {start}-{end}/{len(body)}",
            "Accept-Ranges": "bytes",
        },
    )


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every test talk to the fixture server directly.

    ``make_session`` uses ``trust_env=True`` and the app falls back to
    :func:`downctl.platform_utils.detect_wsl_proxy`, so a developer's global
    proxy (or the WSL host auto-detection) would otherwise decide whether a
    loopback request works. ``WSL_HOST_IP=127.0.0.1`` is the documented way to
    tell that helper there is no host proxy to fill in.
    """
    for name in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WSL_HOST_IP", "127.0.0.1")


@pytest_asyncio.fixture
async def file_server() -> ServerRunner:
    # 5 MiB + 12345 B: 6 pieces of ~876 KiB each. The 8 MiB piece ceiling is
    # covered by the ``layout`` unit tests, not by this fixture's size.
    size = 5 * (1 << 20) + 12345
    state = ServerState(data=os.urandom(size))

    async def handle_file(request: web.Request) -> web.StreamResponse:
        header = request.headers.get("Range")
        state.ranges.append(header)
        if header:
            return _range_response(state, header, state.data)
        return web.Response(body=state.data, headers={"Accept-Ranges": "bytes"})

    async def handle_no_range(request: web.Request) -> web.StreamResponse:
        state.ranges.append(request.headers.get("Range"))
        return web.Response(body=state.data)  # ignores Range entirely

    async def handle_unknown(request: web.Request) -> web.StreamResponse:
        state.ranges.append(request.headers.get("Range"))
        response = web.StreamResponse(status=200)
        await response.prepare(request)
        for offset in range(0, len(state.data), CHUNK):
            await response.write(state.data[offset : offset + CHUNK])
        await response.write_eof()
        return response

    async def handle_stall(request: web.Request) -> web.StreamResponse:
        header = request.headers.get("Range")
        state.ranges.append(header)
        if not header:
            return web.Response(body=state.data)
        parsed = _parse_range(header, len(state.data))
        if parsed is None:
            return web.Response(status=400, text="malformed Range header")
        start, end = parsed
        body = state.data[start : end + 1]
        response = web.StreamResponse(
            status=206,
            headers={
                "Content-Range": f"bytes {start}-{end}/{len(state.data)}",
                "Accept-Ranges": "bytes",
            },
        )
        response.content_length = len(body)
        await response.prepare(request)
        await response.write(body[: max(1, len(body) // 2)])  # then hang
        await asyncio.sleep(3600)
        return response

    async def handle_flaky(request: web.Request) -> web.StreamResponse:
        return web.Response(status=500, text="boom")

    async def handle_slow(request: web.Request) -> web.StreamResponse:
        """Range-capable but deliberately slow, for checkpoint tests."""
        if request.method == "HEAD":
            state.heads += 1
            return web.Response(
                body=state.data, headers={"Accept-Ranges": "bytes", "ETag": state.etag()}
            )
        header = request.headers.get("Range")
        state.ranges.append(header)
        status = 200
        body = state.data
        headers = {"Accept-Ranges": "bytes"}
        if header:
            parsed = _parse_range(header, len(state.data))
            if parsed is None:
                return web.Response(status=400, text="malformed Range header")
            start, end = parsed
            body = state.data[start : end + 1]
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{len(state.data)}"
        response = web.StreamResponse(status=status, headers=headers)
        response.content_length = len(body)
        await response.prepare(request)
        for offset in range(0, len(body), CHUNK):
            await response.write(body[offset : offset + CHUNK])
            await asyncio.sleep(state.chunk_delay)
        await response.write_eof()
        return response

    async def handle_slow_stall(request: web.Request) -> web.StreamResponse:
        """Like ``/slow``, but the first ranged GET stalls half-way through."""
        if state.stall_once and not state.stalled and request.headers.get("Range"):
            state.stalled = True
            return await handle_stall(request)
        return await handle_slow(request)

    async def handle_no_head(request: web.Request) -> web.StreamResponse:
        """A server that rejects HEAD with an error page that has a body."""
        if request.method == "HEAD":
            state.ranges.append(request.headers.get("Range"))
            return web.Response(status=501, text="HEAD not implemented")
        return await handle_file(request)

    # --- content-truth endpoints ------------------------------------------ #
    def _witness_headers(state: ServerState, kind: str) -> dict[str, str]:
        headers = {"Accept-Ranges": "bytes"}
        if kind == "etag":
            headers["ETag"] = state.etag()
        elif kind == "weak":
            headers["ETag"] = f'W/"{state.etag().strip(chr(34))}"'
        elif kind == "mtime":
            headers["Last-Modified"] = state.last_modified()
        return headers

    def kind_header(kind: str) -> str:
        return {"etag": "ETag", "weak": "ETag", "mtime": "Last-Modified", "none": "ETag"}[kind]

    def _make_versioned(name: str, kind: str) -> object:
        async def handler(request: web.Request) -> web.StreamResponse:
            _note(state, name)
            if request.method == "HEAD":
                state.heads += 1
                return web.Response(body=state.current, headers=_witness_headers(state, kind))
            body = state.get_body
            headers = _witness_headers(state, kind)
            header = request.headers.get("Range")
            state.ranges.append(header)
            state.if_ranges.append(request.headers.get("If-Range"))
            if (
                header
                and state.switch_after_ranges is not None
                and state.ranges_served >= state.switch_after_ranges
            ):
                state.version += 1
                state.switch_after_ranges = None
                body = state.get_body
                headers = _witness_headers(state, kind)
            if header:
                state.ranges_served += 1
                conditional = request.headers.get("If-Range")
                if conditional and conditional != headers.get(kind_header(kind), ""):
                    # RFC 9110: a stale validator makes the server ignore the
                    # Range header and send the whole representation.
                    return web.Response(status=200, body=body, headers=headers)
                parsed = _parse_range(header, len(body))
                if parsed is None:
                    return web.Response(status=400, text="malformed Range header")
                start, end = parsed
                if start >= len(body):
                    state.rejections += 1
                    return web.Response(
                        status=416, headers={**headers, "Content-Range": f"bytes */{len(body)}"}
                    )
                return web.Response(
                    status=206,
                    body=body[start : end + 1],
                    headers={**headers, "Content-Range": f"bytes {start}-{end}/{len(body)}"},
                )
            return web.Response(status=200, body=body, headers=headers)

        return handler

    async def handle_wrong_range(request: web.Request) -> web.StreamResponse:
        """Answer a ranged request with a window one byte off."""
        _note(state, "wrong-range")
        if request.method == "HEAD":
            state.heads += 1
            return web.Response(body=state.data, headers={"ETag": state.etag()})
        header = request.headers.get("Range")
        state.ranges.append(header)
        body = state.data
        if not header:
            return web.Response(body=body, headers={"ETag": state.etag()})
        parsed = _parse_range(header, len(body))
        if parsed is None:
            return web.Response(status=400, text="malformed Range header")
        start, end = parsed
        shifted = min(start + 1, len(body) - 1)
        return web.Response(
            status=206,
            body=body[shifted : end + 1],
            headers={
                "Content-Range": f"bytes {shifted}-{end}/{len(body)}",
                "Accept-Ranges": "bytes",
                "ETag": state.etag(),
            },
        )

    async def handle_shrink(request: web.Request) -> web.StreamResponse:
        """The object shrinks right after the first probe of its size."""
        _note(state, "shrink")
        if request.method == "HEAD":
            state.heads += 1
            if state.shrunk is not None and state.heads == 1:
                # First probe still reports the old (larger) size.
                response = web.StreamResponse(
                    status=200, headers={"Accept-Ranges": "bytes", "ETag": state.etag()}
                )
                response.content_length = len(state.data)
                await response.prepare(request)
                await response.write_eof()
                return response
            return web.Response(
                body=state.get_body, headers={"Accept-Ranges": "bytes", "ETag": state.etag()}
            )
        header = request.headers.get("Range")
        state.ranges.append(header)
        body = state.get_body
        headers = {"Accept-Ranges": "bytes", "ETag": state.etag()}
        if not header:
            return web.Response(body=body, headers=headers)
        parsed = _parse_range(header, len(body))
        if parsed is None:
            return web.Response(status=400, text="malformed Range header")
        start, end = parsed
        if start >= len(body):
            state.rejections += 1
            return web.Response(
                status=416, headers={**headers, "Content-Range": f"bytes */{len(body)}"}
            )
        return web.Response(
            status=206,
            body=body[start : end + 1],
            headers={**headers, "Content-Range": f"bytes {start}-{end}/{len(body)}"},
        )

    async def handle_flaky_then_ok(request: web.Request) -> web.StreamResponse:
        """Fail the first ``flaky_failures`` requests, then behave.

        Both the HEAD and the ranged-GET fallback count: a server that is
        having a bad minute is having it for every request.
        """
        state.flaky_seen += 1
        _note(state, "flaky-then-ok")
        if state.flaky_seen <= state.flaky_failures:
            return web.Response(status=500, text="boom")
        if request.method == "HEAD":
            state.heads += 1
            return web.Response(body=state.data, headers={"ETag": state.etag()})
        return await handle_file(request)

    async def handle_always_404(request: web.Request) -> web.StreamResponse:
        _note(state, "always-404")
        return web.Response(status=404, text="gone")

    async def handle_redirect(request: web.Request) -> web.StreamResponse:
        """A 302 in the middle of the download chain."""
        _note(state, "redirect")
        raise web.HTTPFound("/file")

    async def handle_slow_head(request: web.Request) -> web.StreamResponse:
        """A deliberately slow HEAD (and a normal GET), for concurrency tests."""
        _note(state, "slow-head")
        if request.method == "HEAD":
            state.heads += 1
            if state.head_delay:
                await asyncio.sleep(state.head_delay)
            return web.Response(body=state.data, headers={"ETag": state.etag()})
        return await handle_file(request)

    async def handle_head_forbidden(request: web.Request) -> web.StreamResponse:
        """A method-scoped rejection: HEAD is refused, the ranged GET is not.

        This is what an S3 presigned URL looks like (the signature covers the
        HTTP method), and the probe must not report it as a dead object. The
        ranged GET carries the ETag, exactly as S3 does.
        """
        _note(state, "head-forbidden")
        if request.method == "HEAD":
            return web.Response(status=403, text="HEAD is not signed")
        header = request.headers.get("Range")
        state.ranges.append(header)
        headers = {"Accept-Ranges": "bytes", "ETag": state.etag()}
        if not header:
            return web.Response(body=state.data, headers=headers)
        parsed = _parse_range(header, len(state.data))
        if parsed is None:
            return web.Response(status=400, text="malformed Range header")
        start, end = parsed
        return web.Response(
            status=206,
            body=state.data[start : end + 1],
            headers={**headers, "Content-Range": f"bytes {start}-{end}/{len(state.data)}"},
        )

    async def handle_throttle(request: web.Request) -> web.StreamResponse:
        if request.method == "HEAD":
            state.heads += 1
            if state.throttled:
                state.throttled = False
                return web.Response(
                    status=429,
                    text="slow down",
                    headers={"Retry-After": f"{state.retry_after:g}"},
                )
            return web.Response(body=state.data, headers={"ETag": state.etag()})
        return await handle_file(request)

    app = web.Application()
    app.router.add_get("/file", handle_file)
    app.router.add_get("/no-range", handle_no_range)
    app.router.add_get("/unknown", handle_unknown)
    app.router.add_get("/stall", handle_stall)
    app.router.add_get("/flaky", handle_flaky)
    app.router.add_get("/slow", handle_slow)
    app.router.add_get("/slow-stall", handle_slow_stall)
    app.router.add_route("*", "/no-head", handle_no_head)
    app.router.add_route("*", "/versioned", _make_versioned("versioned", "etag"))
    app.router.add_route("*", "/weak", _make_versioned("weak", "weak"))
    app.router.add_route("*", "/mtime", _make_versioned("mtime", "mtime"))
    app.router.add_route("*", "/no-etag", _make_versioned("no-etag", "none"))
    app.router.add_route("*", "/wrong-range", handle_wrong_range)
    app.router.add_route("*", "/shrink", handle_shrink)
    app.router.add_route("*", "/flaky-then-ok", handle_flaky_then_ok)
    app.router.add_route("*", "/throttle", handle_throttle)
    app.router.add_route("*", "/always-404", handle_always_404)
    app.router.add_route("*", "/redirect", handle_redirect)
    app.router.add_route("*", "/slow-head", handle_slow_head)
    app.router.add_route("*", "/head-forbidden", handle_head_forbidden)

    runner = web.AppRunner(app, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    server = ServerRunner(base_url=f"http://127.0.0.1:{port}", state=state)
    try:
        yield server
    finally:
        await runner.cleanup()


@dataclass
class ServerRunner:
    base_url: str
    state: ServerState

    @property
    def data(self) -> bytes:
        return self.state.data

    @property
    def ranges(self) -> list[str | None]:
        return self.state.ranges

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def child_env(self, tmp_path: Path) -> dict[str, str]:
        """Environment for a real ``python -m downctl`` child process.

        Isolates HOME/XDG so no user config or plugin is ever consulted, and
        keeps retry backoff instant so a failure test cannot stall the suite.
        """
        root = str(tmp_path)
        return {
            "HOME": os.path.join(root, "home"),
            "XDG_CONFIG_HOME": os.path.join(root, "xdg"),
            "DOWNCTL_CONFIG": os.path.join(root, "config.toml"),
            "DOWNCTL_RETRY_BASE": "0",
            "NO_COLOR": "1",
            "PYTHONPATH": _SRC,
            "PATH": os.environ.get("PATH", ""),
            # Same reason as the autouse fixture: never let the WSL host-proxy
            # auto-detection redirect a loopback request in a child process.
            "WSL_HOST_IP": "127.0.0.1",
        }


_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
