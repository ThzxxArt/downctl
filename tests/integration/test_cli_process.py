"""Real-process tests: the entry point, signals and restart-resume.

Every other integration test drives the code in-process. That leaves the
project's headline promise untested: "``kill -9`` and re-run, and it resumes
where it stopped". These tests start an actual ``python -m downctl`` child
against the fixture server, interrupt it for real, and check the bytes on disk.

They are slower than the rest of the suite on purpose: a simulated
interruption is exactly what let the 0.1.3 splicing defect through.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from downctl import __version__
from downctl.core import pieces as pc

#: One piece per MiB keeps the grid small enough that a second run can be
#: checked piece by piece.
PIECE_ARGS = ["--piece-size", "1M", "--connections", "2", "--checkpoint", "1"]


def child_environment(server, tmp_path: Path) -> dict[str, str]:
    """Environment for the child: isolated HOME/XDG and no ambient proxy.

    The parent process may legitimately run behind a proxy; a loopback request
    to the fixture server must not.
    """
    env = dict(os.environ)
    for name in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
    ):
        env.pop(name, None)
    env.update(server.child_env(tmp_path))
    return env


def start_download(server, tmp_path: Path, *extra: str, name: str = "slow") -> subprocess.Popen:
    """Start a real ``downctl get`` child process against the fixture server."""
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "downctl",
            "get",
            server.url(name),
            "-d",
            str(tmp_path / "dl"),
            "--reporter",
            "quiet",
            *PIECE_ARGS,
            *extra,
        ],
        env=child_environment(server, tmp_path),
        cwd=str(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def wait_for(predicate: Callable[[], bool], timeout: float = 20.0) -> bool:
    """Blocking poll used only *between* child processes.

    Never call this while the fixture server has to keep serving: it runs in
    the same event loop, so use :func:`await_until` there instead.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


async def await_until(predicate: Callable[[], bool], timeout: float = 20.0) -> bool:
    """Poll without starving the in-process fixture server."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def finish(process: subprocess.Popen, timeout: float = 60.0) -> tuple[int, str, str]:
    """Wait for a child process, then collect its output."""
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    if process.poll() is None:  # pragma: no cover - defensive
        process.kill()
    out, err = process.communicate()
    return int(process.returncode or 0), out, err


def bitmap_with_progress(target: str) -> pc.Bitmap | None:
    bitmap = pc.load(target)
    if bitmap is not None and bitmap.done_pieces > 0:
        return bitmap
    return None


def missing_ranges(server, bitmap: pc.Bitmap) -> set[str]:
    """The byte ranges a resuming run still has to fetch."""
    piece_size = bitmap.piece_size
    return {
        f"bytes={index * piece_size}-{min((index + 1) * piece_size, bitmap.size) - 1}"
        for index, bit in enumerate(bitmap.bits)
        if not bit
    }


def stop(process: subprocess.Popen, sig: int) -> tuple[int, str, str]:
    if process.poll() is None:
        process.send_signal(sig)
    return process.returncode or 0, "", ""


def test_module_entry_point_runs() -> None:
    """``python -m downctl`` is a documented entry point (and had 0% coverage)."""
    result = subprocess.run(
        [sys.executable, "-m", "downctl", "version"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="no catchable SIGTERM for console processes")
async def test_sigterm_exits_130_and_keeps_resumable_progress(file_server, tmp_path: Path) -> None:
    """``kill <pid>`` must not throw away what has already been written."""
    file_server.state.chunk_delay = 0.06  # leave room for a checkpoint
    process = start_download(file_server, tmp_path)
    target = tmp_path / "dl" / "slow"
    checkpointed = await await_until(lambda: bitmap_with_progress(str(target)) is not None)
    stop(process, signal.SIGTERM)
    code, out, err = await finish(process)

    assert checkpointed, f"no checkpoint before the transfer finished: {out!r} {err!r}"
    assert code == 130, f"SIGTERM must map to exit 130, got {code}: {err[-400:]}"
    bitmap = pc.load(str(target))
    assert bitmap is not None and bitmap.done_pieces > 0
    assert not (tmp_path / "dl" / ".downctl.failed").exists()


async def test_hard_kill_then_restart_resumes_only_missing_pieces(
    file_server, tmp_path: Path
) -> None:
    """The headline feature: SIGKILL, then re-run, and only fetch the gaps."""
    file_server.state.chunk_delay = 0.06
    process = start_download(file_server, tmp_path)
    target = tmp_path / "dl" / "slow"
    checkpointed = await await_until(lambda: bitmap_with_progress(str(target)) is not None)
    stop(process, signal.SIGKILL)
    code, out, err = await finish(process)

    assert checkpointed, f"no checkpoint to resume from: {out!r} {err!r}"
    assert code != 0, err

    bitmap = pc.load(str(target))
    assert bitmap is not None and bitmap.done_pieces > 0
    assert bitmap.url_sha256, "the ledger must stay bound to its URL"
    expected = missing_ranges(file_server, bitmap)

    before = len(file_server.ranges)
    second = start_download(file_server, tmp_path)
    code, out, err = await finish(second)
    assert code == 0, f"{out}\n{err}"

    assert target.read_bytes() == file_server.data
    refetched = {item for item in file_server.ranges[before:] if item is not None}
    assert refetched, "the second run fetched nothing at all"
    assert refetched <= expected, f"re-fetched completed pieces: {refetched - expected}"
    assert not (tmp_path / "dl" / ".downctl.failed").exists()
