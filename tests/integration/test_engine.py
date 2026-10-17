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


async def test_download_task_rejects_an_unknown_verifier_without_fetching(
    file_server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)
    task = DownloadTask(
        url=f"{file_server.base_url}/file",
        target="bogus.bin",
        size=len(file_server.data),
        checksum_algo="sha3-512",
        checksum_hex="a" * 128,
    )
    ok = await download_task(ctx, task, reporter=QuietReporter(), connections=6, stall_limit=5)
    assert not ok
    assert file_server.ranges == []
    assert not (tmp_path / "bogus.bin").exists()


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
    from downctl.core import pieces as pc
    from downctl.urls import url_identity

    target = tmp_path / "skip.bin"
    target.write_bytes(file_server.data)
    # A file downloaded by downctl carries a completion marker; without one a
    # full-size file is only trusted on filesystems where holes are provable.
    url = f"{file_server.base_url}/file"
    pc.save_completion(str(target), url_identity(url))
    ctx = make_ctx(session, tmp_path)
    task = DownloadTask(url=url, target="skip.bin", size=len(file_server.data))
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
    from downctl.urls import url_identity

    data = file_server.data
    target = str(tmp_path / "r.bin")
    piece_size, pieces, _ = pc.layout(len(data), 6)
    with open(target, "wb") as fh:
        fh.write(data[:piece_size])
    pc.save(
        target,
        pc.Bitmap(
            len(data),
            pieces,
            [1] + [0] * (pieces - 1),
            piece_size,
            piece_size,
            url_sha256=url_identity(f"{file_server.base_url}/file"),
        ),
    )

    stream = io.StringIO()
    ctx = make_ctx(session, tmp_path)
    task = DownloadTask(url=f"{file_server.base_url}/file", target="r.bin", size=len(data))
    ok = await download_task(ctx, task, reporter=JSONReporter(stream), connections=6, stall_limit=5)
    assert ok
    events = [json.loads(line)["event"] for line in stream.getvalue().splitlines() if line]
    assert "resume" in events
    assert (tmp_path / "r.bin").read_bytes() == data


def test_completed_bytes_partial_without_bitmap_is_zero(tmp_path: Path) -> None:
    from downctl.core import engine

    target = tmp_path / "p.bin"
    target.write_bytes(b"x" * 50)
    task = DownloadTask(url="u", target="p.bin", size=100)
    # A partial file with no bitmap is re-downloaded from scratch -> 0 skipped.
    assert engine._completed_bytes(str(target), task, 4, 0) == 0


def test_completed_bytes_with_bitmap(tmp_path: Path) -> None:
    from downctl.core import engine
    from downctl.core import pieces as pc
    from downctl.urls import url_identity

    target = tmp_path / "b.bin"
    target.write_bytes(b"y" * 100)
    task = DownloadTask(url="u", target="b.bin", size=100)
    pc.save(
        str(target),
        pc.Bitmap(100, 4, [1, 1, 0, 0], 100, 25, url_sha256=url_identity("u")),
    )
    assert engine._completed_bytes(str(target), task, 4, 25) == 50


def test_completed_bytes_bitmap_for_other_url_is_zero(tmp_path: Path) -> None:
    from downctl.core import engine
    from downctl.core import pieces as pc
    from downctl.urls import url_identity

    target = tmp_path / "b.bin"
    target.write_bytes(b"y" * 100)
    task = DownloadTask(url="u", target="b.bin", size=100)
    pc.save(
        str(target),
        pc.Bitmap(100, 4, [1, 1, 0, 0], 100, 25, url_sha256=url_identity("other")),
    )
    assert engine._completed_bytes(str(target), task, 4, 25) == 0


def test_get_transport_unknown(tmp_path: Path) -> None:
    from downctl.core import engine
    from downctl.core.context import RuntimeContext
    from downctl.core.errors import DownctlError

    ctx = RuntimeContext(session=None, config=Config(), registry=discover())  # type: ignore[arg-type]
    task = DownloadTask(url="u", target="f", transport="nope")
    with pytest.raises(DownctlError, match="no transport plugin"):
        engine._get_transport(ctx, task)


async def test_download_gives_up_after_retries(
    file_server, session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOWNCTL_RETRY_BASE", "0")
    ctx = make_ctx(session, tmp_path)
    task = DownloadTask(url=f"{file_server.base_url}/flaky", target="x.bin", size=0)
    ok = await download_task(ctx, task, reporter=QuietReporter(), retries=1, stall_limit=5)
    assert not ok


async def test_download_all_force_skips_run_resumed(file_server, session, tmp_path: Path) -> None:
    class RecordingReporter(QuietReporter):
        def __init__(self) -> None:
            self.resumed = 0

        def run_resumed(self, done_bytes: int) -> None:
            self.resumed += done_bytes

    (tmp_path / "z.bin").write_bytes(file_server.data)
    reporter = RecordingReporter()
    task = DownloadTask(
        url=f"{file_server.base_url}/file", target="z.bin", size=len(file_server.data)
    )
    ok, failed = await download_all(
        make_ctx(session, tmp_path),
        [task],
        jobs=1,
        reporter=reporter,
        force=True,
        connections=4,
        stall_limit=5,
    )
    assert (ok, failed) == (1, 0)
    assert reporter.resumed == 0


async def test_download_all_reports_resume_bytes(file_server, session, tmp_path: Path) -> None:
    from downctl.core import pieces as pc
    from downctl.urls import url_identity

    class RecordingReporter(QuietReporter):
        def __init__(self) -> None:
            self.resumed = 0

        def run_resumed(self, done_bytes: int) -> None:
            self.resumed += done_bytes

    data = file_server.data
    target = str(tmp_path / "r.bin")
    piece_size, pieces, _ = pc.layout(len(data), 6)
    with open(target, "wb") as fh:
        fh.write(data[:piece_size])
    pc.save(
        target,
        pc.Bitmap(
            len(data),
            pieces,
            [1] + [0] * (pieces - 1),
            piece_size,
            piece_size,
            url_sha256=url_identity(f"{file_server.base_url}/file"),
        ),
    )

    reporter = RecordingReporter()
    task = DownloadTask(url=f"{file_server.base_url}/file", target="r.bin", size=len(data))
    ctx = make_ctx(session, tmp_path)
    ok, failed = await download_all(
        ctx,
        [task],
        jobs=1,
        reporter=reporter,
        connections=6,
        stall_limit=5,
    )
    assert (ok, failed) == (1, 0)
    assert reporter.resumed == piece_size
    assert ctx.stats.bytes_transferred == len(data) - piece_size


async def test_skip_existing_rejects_full_size_file_with_holes(
    file_server, session, tmp_path: Path
) -> None:
    """A right-size file from an interrupted run must never be blessed."""
    from downctl.core import pieces as pc
    from downctl.urls import url_identity

    data = file_server.data
    url = f"{file_server.base_url}/file"
    target = str(tmp_path / "holed.bin")
    piece_size = 1 << 20
    _, pieces, _ = pc.layout(len(data), 2, piece_size)
    with open(target, "wb") as fh:
        fh.write(data[:piece_size])
        fh.truncate(len(data))  # full size, missing pieces are zeros
    pc.save(
        target,
        pc.Bitmap(
            len(data),
            pieces,
            [1] + [0] * (pieces - 1),
            len(data),
            piece_size,
            url_sha256=url_identity(url),
        ),
    )

    task = DownloadTask(url=url, target="holed.bin", size=len(data))
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        piece_size=piece_size,
        skip_existing=True,
        stall_limit=5,
    )
    assert ok
    assert (tmp_path / "holed.bin").read_bytes() == data
    assert file_server.ranges  # the missing pieces were fetched


async def test_skip_existing_unknown_size_uses_the_completion_marker(
    file_server, session, tmp_path: Path
) -> None:
    from downctl.core import pieces as pc
    from downctl.urls import url_identity

    url = f"{file_server.base_url}/unknown"
    target = tmp_path / "unknown.bin"
    target.write_bytes(file_server.data)
    pc.save_completion(str(target), url_identity(url))

    task = DownloadTask(url=url, target="unknown.bin", size=0)
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        skip_existing=True,
        stall_limit=5,
    )
    assert ok
    assert file_server.ranges == []


async def test_skip_existing_trusts_the_marker_over_a_source_checksum(
    file_server, session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from downctl.core import engine as engine_module
    from downctl.core import pieces as pc
    from downctl.urls import url_identity

    url = f"{file_server.base_url}/file"
    target = tmp_path / "fast.bin"
    target.write_bytes(file_server.data)
    digest = hashlib.sha256(file_server.data).hexdigest()
    pc.save_completion(str(target), url_identity(url), "sha256", digest)

    async def must_not_run(ctx: object, task: object, path: str) -> bool:
        raise AssertionError("a matching marker digest must not be re-hashed on skip")

    monkeypatch.setattr(engine_module, "_verify", must_not_run)
    task = DownloadTask(
        url=url,
        target="fast.bin",
        size=len(file_server.data),
        checksum_algo="sha256",
        checksum_hex=digest,
    )
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        skip_existing=True,
        stall_limit=5,
    )
    assert ok
    assert file_server.ranges == []


async def test_skip_existing_rehashes_when_the_marker_digest_changed(
    file_server, session, tmp_path: Path
) -> None:
    from downctl.core import pieces as pc
    from downctl.urls import url_identity

    url = f"{file_server.base_url}/file"
    target = tmp_path / "changed-digest.bin"
    target.write_bytes(file_server.data)
    digest = hashlib.sha256(file_server.data).hexdigest()
    # The marker remembers an older source digest; the source now advertises
    # the real one. The file is re-hashed and the marker updated.
    pc.save_completion(str(target), url_identity(url), "sha256", "0" * 64)

    task = DownloadTask(
        url=url,
        target="changed-digest.bin",
        size=len(file_server.data),
        checksum_algo="sha256",
        checksum_hex=digest,
    )
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        skip_existing=True,
        stall_limit=5,
    )
    assert ok
    assert file_server.ranges == []
    marker = pc.load_completion(str(target))
    assert marker is not None
    assert marker["checksum_hex"] == digest


async def test_skip_existing_rechecks_a_supplied_checksum(
    file_server, session, tmp_path: Path
) -> None:
    digest = hashlib.sha256(file_server.data).hexdigest()
    target = tmp_path / "wrong.bin"
    target.write_bytes(b"z" * len(file_server.data))
    task = DownloadTask(
        url=f"{file_server.base_url}/file",
        target="wrong.bin",
        size=len(file_server.data),
        checksum_algo="sha256",
        checksum_hex=digest,
        meta={"user_checksum": True},
    )
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        skip_existing=True,
        stall_limit=5,
    )
    assert ok
    assert target.read_bytes() == file_server.data


async def test_checkpoint_persists_bitmap_while_transferring(
    file_server, session, tmp_path: Path
) -> None:
    import asyncio
    import contextlib
    import time

    from downctl.core import pieces as pc
    from downctl.core.transfer import transfer_file
    from downctl.urls import url_identity

    data = file_server.data
    url = f"{file_server.base_url}/slow"
    target = str(tmp_path / "checkpoint.bin")
    ctx = make_ctx(session, tmp_path)
    transport = ctx.registry.get("transport", "http")  # type: ignore[union-attr]
    job = asyncio.create_task(
        transfer_file(
            ctx,
            transport,  # type: ignore[arg-type]
            url,
            target,
            len(data),
            connections=2,
            piece_size=1 << 20,
            identity=url_identity(url),
            checkpoint=1,
            stall_limit=10,
        )
    )
    bitmap = None
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        bitmap = pc.load(target)
        if bitmap is not None and bitmap.done_pieces > 0:
            break
        await asyncio.sleep(0.1)
    job.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await job
    assert bitmap is not None
    assert bitmap.done_pieces > 0
    assert bitmap.url_sha256 == url_identity(url)


async def test_skip_existing_rejects_truncated_file_with_complete_bitmap(
    file_server, session, tmp_path: Path
) -> None:
    """A complete ledger is not proof when the data file has been truncated."""
    from downctl.core import pieces as pc
    from downctl.urls import url_identity

    data = file_server.data
    url = f"{file_server.base_url}/file"
    target = tmp_path / "truncated.bin"
    target.write_bytes(b"")
    piece_size = 1 << 20
    _, pieces, _ = pc.layout(len(data), 2, piece_size)
    pc.save(
        str(target),
        pc.Bitmap(
            len(data),
            pieces,
            [1] * pieces,
            len(data),
            piece_size,
            url_sha256=url_identity(url),
        ),
    )

    task = DownloadTask(url=url, target="truncated.bin", size=len(data))
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        piece_size=piece_size,
        skip_existing=True,
        stall_limit=5,
    )
    assert ok
    assert target.read_bytes() == data
    assert file_server.ranges  # the missing bytes were fetched


async def test_skip_existing_rehashes_a_complete_bitmap_with_a_source_checksum(
    file_server, session, tmp_path: Path
) -> None:
    """A complete bitmap proves bytes, not that verification ever ran."""
    from downctl.core import pieces as pc
    from downctl.urls import url_identity

    data = file_server.data
    url = f"{file_server.base_url}/file"
    target = tmp_path / "unverified.bin"
    target.write_bytes(b"x" * len(data))  # full size, wrong bytes
    piece_size = 1 << 20
    _, pieces, _ = pc.layout(len(data), 2, piece_size)
    pc.save(
        str(target),
        pc.Bitmap(
            len(data),
            pieces,
            [1] * pieces,
            len(data),
            piece_size,
            url_sha256=url_identity(url),
        ),
    )

    task = DownloadTask(
        url=url,
        target="unverified.bin",
        size=len(data),
        checksum_algo="sha256",
        checksum_hex=hashlib.sha256(data).hexdigest(),
    )
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        piece_size=piece_size,
        skip_existing=True,
        stall_limit=5,
    )
    assert ok
    assert target.read_bytes() == data
    assert file_server.ranges


async def test_skip_existing_rejects_an_oversized_target(
    file_server, session, tmp_path: Path
) -> None:
    """A file larger than the object is not a completed copy of it."""
    data = file_server.data
    target = tmp_path / "oversized.bin"
    target.write_bytes(b"x" * (len(data) + 4096))

    task = DownloadTask(url=f"{file_server.base_url}/file", target="oversized.bin", size=len(data))
    ok = await download_task(
        make_ctx(session, tmp_path),
        task,
        reporter=QuietReporter(),
        connections=2,
        skip_existing=True,
        stall_limit=5,
    )
    assert ok
    assert target.stat().st_size == len(data)
    assert target.read_bytes() == data


def test_remaining_bytes_counts_a_full_retry_when_resume_is_impossible(
    tmp_path: Path,
) -> None:
    from downctl.core import engine

    target = tmp_path / "partial.bin"
    target.write_bytes(b"x" * 50)
    task = DownloadTask(url="u", target="partial.bin", size=100)
    # No bitmap/marker: ``pieces.resolve`` truncates this file, so a disk
    # pre-flight must budget the whole object, not the missing tail.
    assert engine._remaining_bytes(str(target), task, 4) == 100
    assert engine._completed_bytes(str(target), task, 4) == 0
