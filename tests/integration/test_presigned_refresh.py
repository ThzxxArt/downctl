"""A presigned URL can expire while the download is still running.

S3 signs each URL for a fixed lifetime (one hour by default). A download that
outlives it starts receiving ``403`` for the ranges that are still missing, and
``403`` is classified permanent — so the transfer gave up half way with a
"forbidden" that read like a permissions problem, on a URL that was merely
stale. The source can hand the engine a way to re-sign; these tests pin both the
renewal and its bound.
"""

from __future__ import annotations

import re
from pathlib import Path

import aiohttp
import pytest_asyncio
from aiohttp import web

from downctl.config import Config, CoreConfig
from downctl.core.context import RuntimeContext
from downctl.core.engine import REFETCH_MAX, download_task
from downctl.core.task import DownloadTask
from downctl.plugin.loader import discover

PAYLOAD = bytes(range(256)) * 512


@pytest_asyncio.fixture
async def presigned_server() -> str:
    """Serves the object only for ``token=fresh``; everything else is a 403."""
    state: dict[str, list[str]] = {"seen": []}

    async def handler(request: web.Request) -> web.StreamResponse:
        token = request.query.get("token", "")
        state["seen"].append(token)
        if token != "fresh":
            return web.Response(status=403, text="Request has expired")
        header = request.headers.get("Range")
        headers = {"Accept-Ranges": "bytes", "ETag": '"v1"'}
        if not header:
            return web.Response(body=PAYLOAD, headers=headers)
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", header)
        assert match is not None
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else len(PAYLOAD) - 1
        end = min(end, len(PAYLOAD) - 1)
        return web.Response(
            status=206,
            body=PAYLOAD[start : end + 1],
            headers={**headers, "Content-Range": f"bytes {start}-{end}/{len(PAYLOAD)}"},
        )

    async def missing(request: web.Request) -> web.StreamResponse:
        # A real 404: the object is gone, not the signature.
        return web.Response(status=404, text="no such key")

    application = web.Application()
    application.router.add_route("*", "/object", handler)
    application.router.add_route("*", "/missing", missing)
    runner = web.AppRunner(application, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}/object"
    try:
        yield base
    finally:
        await runner.cleanup()


def _ctx(tmp_path: Path, session: aiohttp.ClientSession) -> RuntimeContext:
    cfg = Config(core=CoreConfig(dir=str(tmp_path), retries=3, connections=2))
    return RuntimeContext(session=session, config=cfg, registry=discover())


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


async def test_an_expired_presigned_url_is_re_signed_and_the_download_finishes(
    presigned_server: str, session, tmp_path: Path
) -> None:
    task = DownloadTask(
        url=f"{presigned_server}?token=expired",
        target="payload.bin",
        size=len(PAYLOAD),
        source="s3",
        meta={"redact_url": True, "refetch": lambda: f"{presigned_server}?token=fresh"},
    )
    ctx = _ctx(tmp_path, session)

    assert await download_task(ctx, task, reporter=None, stall_limit=5) is True

    assert (tmp_path / "payload.bin").read_bytes() == PAYLOAD
    assert task.meta["refetched"] == 1


async def test_a_signer_that_keeps_expiring_is_given_up_on(
    presigned_server: str, session, tmp_path: Path
) -> None:
    """The renewal is bounded: a wrong signer must not loop forever."""
    counter = {"n": 0}

    def refetch() -> str:
        counter["n"] += 1
        return f"{presigned_server}?token=stale-{counter['n']}"

    task = DownloadTask(
        url=f"{presigned_server}?token=expired",
        target="payload.bin",
        size=len(PAYLOAD),
        source="s3",
        meta={"redact_url": True, "refetch": refetch},
    )
    ctx = _ctx(tmp_path, session)

    assert await download_task(ctx, task, reporter=None, stall_limit=5) is False

    assert task.meta["refetched"] == REFETCH_MAX
    # A failed transfer may leave the ledger and an empty target; what it must
    # not leave is a file that looks complete.
    target = tmp_path / "payload.bin"
    assert not target.exists() or target.stat().st_size < len(PAYLOAD)


async def test_a_403_without_a_refetch_hook_stays_permanent(
    presigned_server: str, session, tmp_path: Path
) -> None:
    """Only a source that says it can re-sign gets a second chance."""
    task = DownloadTask(
        url=f"{presigned_server}?token=expired",
        target="payload.bin",
        size=len(PAYLOAD),
    )
    ctx = _ctx(tmp_path, session)

    assert await download_task(ctx, task, reporter=None, stall_limit=5) is False
    assert "refetched" not in task.meta


async def test_a_404_is_not_treated_as_an_expiry(
    presigned_server: str, session, tmp_path: Path
) -> None:
    """A missing object is not a stale signature, so the hook must not fire.

    The URL used to be ``http://127.0.0.1:1/gone``, which never produced a 404
    at all: the connection was refused, ``calls`` stayed at zero for a reason
    that had nothing to do with the rule, and the test could not fail.
    """
    calls = {"n": 0}

    def refetch() -> str:
        calls["n"] += 1
        return f"{presigned_server}?token=fresh"

    task = DownloadTask(
        url=presigned_server.replace("/object", "/missing") + "?token=expired",
        target="gone.bin",
        size=len(PAYLOAD),
        meta={"refetch": refetch},
    )
    ctx = _ctx(tmp_path, session)

    assert await download_task(ctx, task, reporter=None, stall_limit=2) is False
    # The engine classifies the failure it saw, so this also proves a 404 was
    # actually returned rather than a connection error.
    assert task.meta["failure"] == "permanent"
    assert calls["n"] == 0


async def test_the_probe_re_signs_an_expired_url_too(
    presigned_server: str, session, tmp_path: Path
) -> None:
    """A link that expires before the first HEAD used to fail the target.

    Renewal lived only in the transfer loop, so the probe answered 403, called
    it permanent, and the run ended before the transfer — the only place that
    could have re-signed — ever ran.
    """
    from downctl import app

    calls = {"n": 0}

    def refetch() -> str:
        calls["n"] += 1
        return f"{presigned_server}?token=fresh"

    task = DownloadTask(
        url=f"{presigned_server}?token=expired",
        target="payload.bin",
        meta={"refetch": refetch},
    )
    ctx = _ctx(tmp_path, session)

    await app._probe_task(ctx, task, retries=1)

    assert calls["n"] == 1
    assert task.url.endswith("token=fresh")
    assert task.size == len(PAYLOAD)
