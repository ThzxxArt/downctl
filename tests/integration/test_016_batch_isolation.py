"""Batch jobs own their credentials and their hooks.

Two jobs in one batch used to share one session (the first job's ``-H`` applied
to every later job) and one event bus (a job's ``hook`` ran for every later
job). Both are only observable end to end: the session is built inside
``_run_plan`` and the hooks are registered per job.
"""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

from downctl import app, cli, console


@pytest_asyncio.fixture
async def guarded_server() -> AsyncIterator[tuple[str, bytes]]:
    """A server that refuses any request without ``X-Job: secret``."""
    data = b"job-scoped payload" * 4096

    async def handle(request: web.Request) -> web.Response:
        if request.headers.get("X-Job") != "secret":
            return web.Response(status=403, text="missing X-Job")
        return web.Response(body=data, headers={"Content-Length": str(len(data))})

    application = web.Application()
    application.router.add_get("/{name}", handle)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", data
    finally:
        await runner.cleanup()


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "config.toml"))
    console.set_level(console.NORMAL)
    console.set_machine_readable(False)


def _jobs(tmp_path: Path, base: str, second_extra: str = "") -> Path:
    path = tmp_path / "jobs.toml"
    path.write_text(
        f"""
version = 1

[[job]]
name = "with-header"
targets = ["{base}/a.bin"]
dir = "{tmp_path / "a"}"
header = ["X-Job: secret"]
hook = ["logfile"]

[[job]]
name = "without-header"
targets = ["{base}/b.bin"]
dir = "{tmp_path / "b"}"
{second_extra}
""",
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_a_jobs_header_does_not_leak_into_the_next_job(
    guarded_server: tuple[str, bytes], tmp_path: Path
) -> None:
    base, data = guarded_server
    jobs = _jobs(tmp_path, base)
    args: argparse.Namespace = cli.build_parser().parse_args(["batch", str(jobs)])
    args.reporter = "quiet"
    assert await app._batch(args) == 1  # the second job is refused with 403
    assert (tmp_path / "a" / "a.bin").read_bytes() == data
    assert not (tmp_path / "b" / "b.bin").exists()


@pytest.mark.asyncio
async def test_a_jobs_hook_does_not_run_for_the_next_job(
    guarded_server: tuple[str, bytes], tmp_path: Path
) -> None:
    base, _data = guarded_server
    log = tmp_path / "hook.log"
    (tmp_path / "config.toml").write_text(
        f"""
[core]
hooks = []

[plugins.logfile]
path = "{log}"
""",
        encoding="utf-8",
    )
    jobs = _jobs(tmp_path, base)
    args: argparse.Namespace = cli.build_parser().parse_args(["batch", str(jobs)])
    args.reporter = "quiet"
    await app._batch(args)
    text = log.read_text(encoding="utf-8")
    assert "a.bin" in text  # the first job asked for the hook
    assert "b.bin" not in text  # the second job did not


@pytest.mark.asyncio
async def test_an_invalid_later_job_is_refused_before_any_download(
    guarded_server: tuple[str, bytes], tmp_path: Path
) -> None:
    base, _data = guarded_server
    jobs = _jobs(tmp_path, base, second_extra='header = ["not-a-header"]')
    args: argparse.Namespace = cli.build_parser().parse_args(["batch", str(jobs)])
    args.reporter = "quiet"
    with pytest.raises(app.DownctlError, match="header"):
        await app._batch(args)
    # Fail-fast: the valid first job did not run either.
    assert not (tmp_path / "a" / "a.bin").exists()
