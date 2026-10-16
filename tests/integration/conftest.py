"""Fixtures for integration tests: a controllable local HTTP file server."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field

import pytest_asyncio
from aiohttp import web

CHUNK = 64 * 1024


@dataclass
class ServerState:
    data: bytes
    ranges: list[str | None] = field(default_factory=list)


def _range_response(state: ServerState, header: str) -> web.Response:
    match = re.match(r"bytes=(\d+)-(\d*)", header)
    assert match is not None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else len(state.data) - 1
    end = min(end, len(state.data) - 1)
    body = state.data[start : end + 1]
    return web.Response(
        status=206,
        body=body,
        headers={
            "Content-Range": f"bytes {start}-{end}/{len(state.data)}",
            "Accept-Ranges": "bytes",
        },
    )


@pytest_asyncio.fixture
async def file_server() -> ServerRunner:
    size = 5 * (1 << 20) + 12345  # a few pieces at the 8 MiB cap
    state = ServerState(data=os.urandom(size))

    async def handle_file(request: web.Request) -> web.StreamResponse:
        header = request.headers.get("Range")
        state.ranges.append(header)
        if header:
            return _range_response(state, header)
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
        match = re.match(r"bytes=(\d+)-(\d*)", header)
        assert match is not None
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else len(state.data) - 1
        end = min(end, len(state.data) - 1)
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

    app = web.Application()
    app.router.add_get("/file", handle_file)
    app.router.add_get("/no-range", handle_no_range)
    app.router.add_get("/unknown", handle_unknown)
    app.router.add_get("/stall", handle_stall)

    runner = web.AppRunner(app, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None  # noqa: SLF001 - test helper
    port = site._server.sockets[0].getsockname()[1]
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
