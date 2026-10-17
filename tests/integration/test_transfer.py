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
    piece_size, pieces, _ = pc.layout(expected, 6)

    with open(target, "wb") as fh:
        fh.write(data[:piece_size])
    pc.save(target, pc.Bitmap(expected, pieces, [1] + [0] * (pieces - 1), piece_size))

    before = len(file_server.ranges)
    ctx = make_ctx(session, tmp_path)
    result = await transfer_file(
        ctx,
        HTTPTransport(),
        f"{file_server.base_url}/file",
        target,
        expected,
        connections=6,
        stall_limit=5,
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
    target = str(tmp_path / "out.bin")
    ctx = make_ctx(session, tmp_path)

    stalled = await transfer_file(
        ctx,
        HTTPTransport(),
        f"{file_server.base_url}/stall",
        target,
        len(file_server.data),
        connections=2,
        stall_limit=1,
    )
    assert not stalled.complete
    assert Path(pc.path_for(target)).exists()  # interrupted progress is persisted

    resumed = await transfer_file(
        ctx,
        HTTPTransport(),
        f"{file_server.base_url}/file",
        target,
        len(file_server.data),
        connections=2,
        stall_limit=5,
    )
    assert resumed.complete
    assert Path(target).read_bytes() == file_server.data
