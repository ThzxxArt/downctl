"""Integration tests for the file:// source and transport."""

from __future__ import annotations

from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio

from downctl import app
from downctl.builtin.sources.file_source import FileSource
from downctl.builtin.transports.file_transport import local_path
from downctl.config import Config, CoreConfig
from downctl.core.context import RuntimeContext

DATA_A = b"hello world\n" * 100
DATA_B = b"B" * 4096


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


def _ctx(session, tmp_path: Path) -> RuntimeContext:
    return RuntimeContext(session=session, config=Config(core=CoreConfig(dir=str(tmp_path))))


def _src_tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_bytes(DATA_A)
    (src / "sub" / "b.bin").write_bytes(DATA_B)
    return src


def test_local_path_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "x"
    assert Path(local_path(target.as_uri())) == target


async def test_resolve_single(session, tmp_path: Path) -> None:
    src = _src_tree(tmp_path)
    tasks = await FileSource().resolve((src / "a.txt").as_uri(), _ctx(session, tmp_path))
    assert len(tasks) == 1
    assert tasks[0].target == "a.txt"
    assert tasks[0].size == len(DATA_A)
    assert tasks[0].transport == "file"
    assert tasks[0].meta["identity"]


async def test_local_identity_changes_when_the_source_is_edited(session, tmp_path: Path) -> None:
    import os

    src = _src_tree(tmp_path)
    source = src / "a.txt"
    first = await FileSource().resolve(source.as_uri(), _ctx(session, tmp_path))
    source.write_bytes(b"x" * len(DATA_A))  # same size, different content
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    second = await FileSource().resolve(source.as_uri(), _ctx(session, tmp_path))
    assert first[0].meta["identity"] != second[0].meta["identity"]


async def test_local_identity_survives_an_mtime_restore(session, tmp_path: Path) -> None:
    import os

    if os.name == "nt":
        pytest.skip("Windows st_ctime is creation time, not metadata-change time")
    src = _src_tree(tmp_path)
    source = src / "a.txt"
    before = source.stat()
    first = await FileSource().resolve(source.as_uri(), _ctx(session, tmp_path))
    source.write_bytes(b"x" * len(DATA_A))  # same size, different content
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    second = await FileSource().resolve(source.as_uri(), _ctx(session, tmp_path))
    assert first[0].meta["identity"] != second[0].meta["identity"]


async def test_resolve_directory_recursive(session, tmp_path: Path) -> None:
    src = _src_tree(tmp_path)
    tasks = await FileSource().resolve(src.as_uri(), _ctx(session, tmp_path))
    assert {t.target for t in tasks} == {"a.txt", "sub/b.bin"}


async def test_resolve_include(session, tmp_path: Path) -> None:
    src = _src_tree(tmp_path)
    ctx = _ctx(session, tmp_path)
    ctx.params = {"include": ["*.bin"], "exclude": []}
    tasks = await FileSource().resolve(src.as_uri(), ctx)
    assert [t.target for t in tasks] == ["sub/b.bin"]


async def test_end_to_end_copy(tmp_path: Path, monkeypatch) -> None:
    src = _src_tree(tmp_path)
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "none.toml"))

    destination = tmp_path / "dst"
    args = app._default_get_args()
    args.targets = [src.as_uri()]
    args.dir = str(destination)
    args.reporter = "quiet"

    assert await app._get(args) == 0
    assert (destination / "a.txt").read_bytes() == DATA_A
    assert (destination / "sub" / "b.bin").read_bytes() == DATA_B
