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
