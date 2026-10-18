"""Integration tests for the multi-connection transfer engine."""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest_asyncio

from downctl.builtin.transports.http_transport import HTTPTransport
from downctl.config import Config, CoreConfig
from downctl.core import pieces as pc
from downctl.core.context import RuntimeContext
from downctl.core.transfer import transfer_file
from downctl.urls import url_identity


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


def make_ctx(session: aiohttp.ClientSession, tmp_path: Path) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path))),
    )


async def test_transfer_full_download(file_server, session, tmp_path: Path) -> None:
    target = str(tmp_path / "out.bin")
    ctx = make_ctx(session, tmp_path)
    result = await transfer_file(
        ctx,
        HTTPTransport(),
        f"{file_server.base_url}/file",
        target,
        len(file_server.data),
        connections=6,
        stall_limit=5,
    )
    assert result.complete
    assert Path(target).read_bytes() == file_server.data
    assert not Path(pc.path_for(target)).exists()


async def test_transfer_resumes_only_missing_pieces(file_server, session, tmp_path: Path) -> None:
    data = file_server.data
    expected = len(data)
    target = str(tmp_path / "out.bin")
    url = f"{file_server.base_url}/file"
    identity = url_identity(url)
    piece_size, pieces, _ = pc.layout(expected, 6)

    with open(target, "wb") as fh:
        fh.write(data[:piece_size])
    pc.save(
        target,
        pc.Bitmap(
            expected,
            pieces,
            [1] + [0] * (pieces - 1),
            piece_size,
            piece_size,
            url_sha256=identity,
        ),
    )

    before = len(file_server.ranges)
    ctx = make_ctx(session, tmp_path)
    result = await transfer_file(
        ctx,
        HTTPTransport(),
        url,
        target,
        expected,
        connections=6,
        stall_limit=5,
        identity=identity,
    )

    assert result.complete
    assert Path(target).read_bytes() == data
    new_ranges = file_server.ranges[before:]
    assert new_ranges
    assert all(rng is not None and not rng.startswith("bytes=0-") for rng in new_ranges)


async def test_transfer_falls_back_when_range_ignored(file_server, session, tmp_path: Path) -> None:
    target = str(tmp_path / "out.bin")
    ctx = make_ctx(session, tmp_path)
    result = await transfer_file(
        ctx,
        HTTPTransport(),
        f"{file_server.base_url}/no-range",
        target,
        len(file_server.data),
        connections=6,
        stall_limit=5,
    )
    assert result.complete
    assert Path(target).read_bytes() == file_server.data
    assert not Path(pc.path_for(target)).exists()


async def test_transfer_unknown_size_streams(file_server, session, tmp_path: Path) -> None:
    target = str(tmp_path / "out.bin")
    ctx = make_ctx(session, tmp_path)
    result = await transfer_file(
        ctx,
        HTTPTransport(),
        f"{file_server.base_url}/unknown",
        target,
        0,
    )
    assert result.complete
    assert Path(target).read_bytes() == file_server.data


async def test_transfer_stream_short_body_is_incomplete(
    file_server, session, tmp_path: Path
) -> None:
    target = str(tmp_path / "out.bin")
    ctx = make_ctx(session, tmp_path)
    result = await transfer_file(
        ctx,
        HTTPTransport(),
        f"{file_server.base_url}/no-range",
        target,
        len(file_server.data) + 4096,  # expect more bytes than the server has
        connections=2,
        stall_limit=5,
    )
    assert not result.complete


async def test_transfer_stall_saves_bitmap_then_resumes(
    file_server, session, tmp_path: Path
) -> None:
    """The resume must happen against the *same* URL.

    The earlier version of this test resumed from a different endpoint, which
    meant the identity never matched and the file was re-downloaded in full —
    the test name promised something it never checked.
    """
    data = file_server.data
    url = file_server.url("slow-stall")
    target = str(tmp_path / "out.bin")
    ctx = make_ctx(session, tmp_path)
    piece_size = 1 << 20
    file_server.state.stall_once = True

    stalled = await transfer_file(
        ctx,
        HTTPTransport(),
        url,
        target,
        len(data),
        connections=2,
        piece_size=piece_size,
        stall_limit=1,
    )
    assert not stalled.complete
    assert Path(pc.path_for(target)).exists()  # interrupted progress is persisted

    bitmap = pc.load(target)
    assert bitmap is not None
    assert bitmap.url_sha256 == url_identity(url)

    def piece_range(index: int) -> str:
        start = index * piece_size
        return f"bytes={start}-{min(start + piece_size, len(data)) - 1}"

    expected = {piece_range(index) for index, bit in enumerate(bitmap.bits) if not bit}
    before = len(file_server.ranges)
    resumed = await transfer_file(
        ctx,
        HTTPTransport(),
        url,
        target,
        len(data),
        connections=2,
        piece_size=piece_size,
        stall_limit=5,
    )

    assert resumed.complete
    assert Path(target).read_bytes() == data
    refetched = {item for item in file_server.ranges[before:] if item}
    assert refetched, "the resume fetched nothing"
    assert refetched <= expected, f"re-fetched completed pieces: {refetched - expected}"


async def test_authorization_is_dropped_on_cross_origin_redirect() -> None:
    """Credentials stay on the host they were configured for (aiohttp contract)."""
    from aiohttp import web

    from downctl.session import make_session

    seen: dict[str, str | None] = {}

    async def target_handler(request: web.Request) -> web.Response:
        seen["authorization"] = request.headers.get("Authorization")
        return web.Response(body=b"ok")

    target_app = web.Application()
    target_app.router.add_get("/file", target_handler)
    target_runner = web.AppRunner(target_app, shutdown_timeout=0.5)
    await target_runner.setup()
    target_site = web.TCPSite(target_runner, "127.0.0.1", 0)
    await target_site.start()
    target_port = target_runner.addresses[0][1]

    async def redirect_handler(request: web.Request) -> web.Response:
        raise web.HTTPFound(f"http://127.0.0.1:{target_port}/file")

    redirect_app = web.Application()
    redirect_app.router.add_get("/go", redirect_handler)
    redirect_runner = web.AppRunner(redirect_app, shutdown_timeout=0.5)
    await redirect_runner.setup()
    redirect_site = web.TCPSite(redirect_runner, "127.0.0.1", 0)
    await redirect_site.start()
    redirect_port = redirect_runner.addresses[0][1]

    try:
        async with (
            make_session(auth=("user", "password")) as session,
            session.get(f"http://127.0.0.1:{redirect_port}/go") as response,
        ):
            assert await response.read() == b"ok"
    finally:
        await redirect_runner.cleanup()
        await target_runner.cleanup()

    assert seen["authorization"] is None
