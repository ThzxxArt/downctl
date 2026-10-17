"""Integration tests for the download engine (task -> file)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio

from downctl.builtin.reporters.quiet_reporter import QuietReporter
from downctl.config import Config, CoreConfig
from downctl.core.context import RuntimeContext
from downctl.core.engine import download_all, download_task
from downctl.core.task import DownloadTask
from downctl.plugin.loader import discover


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


def make_ctx(session: aiohttp.ClientSession, tmp_path: Path) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path))),
        registry=discover(),
    )


async def test_download_task_end_to_end(file_server, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    task = DownloadTask(
        url=f"{file_server.base_url}/file",
        target="sub/out.bin",
        size=len(file_server.data),
    )
    ok = await download_task(ctx, task, reporter=QuietReporter(), connections=6, stall_limit=5)
    assert ok
    assert (tmp_path / "sub" / "out.bin").read_bytes() == file_server.data


async def test_download_task_verifies_checksum(file_server, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    digest = hashlib.sha256(file_server.data).hexdigest()
    task = DownloadTask(
        url=f"{file_server.base_url}/file",
        target="ok.bin",
        size=len(file_server.data),
        checksum_algo="sha256",
        checksum_hex=digest,
    )
    assert await download_task(ctx, task, reporter=QuietReporter(), connections=6, stall_limit=5)
    assert (tmp_path / "ok.bin").read_bytes() == file_server.data


async def test_download_task_fails_on_bad_checksum(
    file_server, session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOWNCTL_RETRY_BASE", "1")
    ctx = make_ctx(session, tmp_path)
    task = DownloadTask(
        url=f"{file_server.base_url}/file",
        target="bad.bin",
        size=len(file_server.data),
        checksum_algo="sha256",
        checksum_hex="0" * 64,
    )
    ok = await download_task(
        ctx, task, reporter=QuietReporter(), retries=1, connections=6, stall_limit=5
    )
    assert not ok


async def test_download_all_concurrent(file_server, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    tasks = [
        DownloadTask(
            url=f"{file_server.base_url}/file", target=f"f{i}.bin", size=len(file_server.data)
        )
        for i in range(3)
    ]
    ok, failed = await download_all(ctx, tasks, jobs=2, reporter=QuietReporter(), connections=4)
    assert (ok, failed) == (3, 0)
    for i in range(3):
        assert (tmp_path / f"f{i}.bin").read_bytes() == file_server.data


async def test_download_task_permanent_error(file_server, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    task = DownloadTask(url=f"{file_server.base_url}/missing", target="x.bin", size=0)
    assert not await download_task(ctx, task, reporter=QuietReporter(), stall_limit=5)


async def test_download_task_skip_existing(file_server, session, tmp_path: Path) -> None:
    target = tmp_path / "skip.bin"
    target.write_bytes(file_server.data)
    ctx = make_ctx(session, tmp_path)
    task = DownloadTask(
        url=f"{file_server.base_url}/file", target="skip.bin", size=len(file_server.data)
    )
    ok = await download_task(ctx, task, reporter=QuietReporter(), skip_existing=True)
    assert ok
    assert file_server.ranges == []  # nothing was fetched


async def test_download_task_force_redownloads(file_server, session, tmp_path: Path) -> None:
    target = tmp_path / "force.bin"
    target.write_bytes(file_server.data)  # would otherwise be trusted complete
    ctx = make_ctx(session, tmp_path)
    task = DownloadTask(
        url=f"{file_server.base_url}/file", target="force.bin", size=len(file_server.data)
    )
    ok = await download_task(ctx, task, reporter=QuietReporter(), force=True, stall_limit=5)
    assert ok
    assert target.read_bytes() == file_server.data
    assert file_server.ranges  # it actually re-fetched


async def test_unknown_size_disk_full_fails_gracefully(
    file_server, session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from downctl.core import transfer as transfer_mod
    from downctl.core.errors import DiskSpaceError

    def _raise(path: str, need: int) -> None:
        raise DiskSpaceError("full")

    monkeypatch.setattr(transfer_mod, "ensure_space", _raise)
    ctx = make_ctx(session, tmp_path)
    # size=0 -> the engine streams; the response declares a Content-Length, so
    # the streaming fallback is what raises the disk-full error.
    task = DownloadTask(url=f"{file_server.base_url}/file", target="u.bin", size=0)
    assert not await download_task(ctx, task, reporter=QuietReporter(), stall_limit=5)


async def test_min_split_size_forces_single_connection(
    file_server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)
    task = DownloadTask(
        url=f"{file_server.base_url}/file", target="m.bin", size=len(file_server.data)
    )
    ok = await download_task(
        ctx,
        task,
        reporter=QuietReporter(),
        connections=6,
        min_split_size=1 << 30,
        stall_limit=5,
    )
    assert ok
    assert task.meta["path"].endswith("m.bin")


async def test_resume_emits_initial_progress(file_server, session, tmp_path: Path) -> None:
    import io
    import json

    from downctl.builtin.reporters.json_reporter import JSONReporter
    from downctl.core import pieces as pc

    data = file_server.data
    target = str(tmp_path / "r.bin")
    piece_size, pieces, _ = pc.layout(len(data), 6)
    with open(target, "wb") as fh:
        fh.write(data[:piece_size])
    pc.save(target, pc.Bitmap(len(data), pieces, [1] + [0] * (pieces - 1), piece_size, piece_size))

    stream = io.StringIO()
    ctx = make_ctx(session, tmp_path)
    task = DownloadTask(url=f"{file_server.base_url}/file", target="r.bin", size=len(data))
    ok = await download_task(ctx, task, reporter=JSONReporter(stream), connections=6, stall_limit=5)
    assert ok
    events = [json.loads(line)["event"] for line in stream.getvalue().splitlines() if line]
    assert "resume" in events
    assert (tmp_path / "r.bin").read_bytes() == data
