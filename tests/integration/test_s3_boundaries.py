"""The S3 source's edge cases: endpoint prefix, HEAD rejection, pagination.

Each of these used to fail quietly in a different way: a gateway mounted under a
path signed a URI the server never saw, a rejected HEAD looked like "size
unknown", and a listing that kept answering ``IsTruncated`` with the same token
looped forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from downctl.builtin.sources import s3_source
from downctl.builtin.sources.s3_source import S3Source
from downctl.config import Config, CoreConfig
from downctl.core.context import RuntimeContext
from downctl.core.errors import DownctlError
from downctl.plugin.loader import discover

PAYLOAD = b"P" * 1024


@dataclass
class PrefixedS3:
    base_url: str
    paths: list[str] = field(default_factory=list)
    head_status: int = 200
    #: "same" replays one token forever, "fresh" invents a new one each time.
    token_mode: str = "same"
    #: "page" names a different key per page, "same" replays one key.
    key_mode: str = "page"
    #: Stop the listing after this many pages (0 = never end).
    stop_after: int = 0


@pytest_asyncio.fixture
async def prefixed_s3() -> PrefixedS3:
    state = PrefixedS3(base_url="")

    async def handler(request: web.Request) -> web.Response:
        state.paths.append(request.path)
        # Only the mounted prefix is served: a client that signed ``/bucket/...``
        # lands outside it and gets a 404, which is what a real gateway does.
        tail = request.path.removeprefix("/s3/")
        if tail == request.path:  # pragma: no cover - guards the fixture itself
            raise web.HTTPNotFound
        bucket, _, key = tail.partition("/")
        if "list-type" in request.query:
            page = request.query.get("continuation-token", "0") or "0"
            token = "stuck" if state.token_mode == "same" else str(int(page) + 1)
            key = "logs/same.bin" if state.key_mode == "same" else f"logs/{page}.bin"
            done = bool(state.stop_after) and int(page) >= state.stop_after
            if done:
                body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    "<IsTruncated>false</IsTruncated>"
                    "</ListBucketResult>"
                )
                return web.Response(text=body, content_type="application/xml")
            body = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<IsTruncated>true</IsTruncated><NextContinuationToken>{token}</NextContinuationToken>"
                f"<Contents><Key>{key}</Key><Size>1</Size></Contents>"
                "</ListBucketResult>"
            )
            return web.Response(text=body, content_type="application/xml")
        if request.method == "HEAD":
            if state.head_status != 200:
                return web.Response(status=state.head_status, text="denied")
            return web.Response(
                headers={"Content-Length": str(len(PAYLOAD)), "ETag": '"s3v1"'},
            )
        if unquote(key) != "logs/a.txt":
            raise web.HTTPNotFound
        return web.Response(body=PAYLOAD, headers={"Accept-Ranges": "bytes", "ETag": '"s3v1"'})

    application = web.Application()
    application.router.add_route("GET", "/s3/{tail:.*}", handler)
    application.router.add_route("HEAD", "/s3/{tail:.*}", handler)

    runner = web.AppRunner(application, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.base_url = f"http://127.0.0.1:{runner.addresses[0][1]}/s3"
    try:
        yield state
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


def make_ctx(session, tmp_path: Path) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path))),
        registry=discover(),
    )


def _options(base_url: str) -> dict[str, object]:
    return {
        "endpoint": base_url,
        "path_style": True,
        "access_key": "AKID",
        "secret_key": "SECRET",
        "region": "us-east-1",
    }


async def test_an_endpoint_path_prefix_is_kept_in_the_presigned_url(
    prefixed_s3: PrefixedS3, session, tmp_path: Path
) -> None:
    """A gateway mounted at ``/s3`` signs and serves from that prefix."""
    ctx = make_ctx(session, tmp_path)
    ctx.options["s3"] = _options(prefixed_s3.base_url)

    tasks = await S3Source().resolve("s3://bucket/logs/a.txt", ctx)

    assert tasks[0].url.startswith(f"{prefixed_s3.base_url}/bucket/logs/a.txt?")
    assert tasks[0].size == len(PAYLOAD)
    # The server only answers under ``/s3``; a lost prefix would be a 404 here.
    assert all(path.startswith("/s3/bucket/") for path in prefixed_s3.paths)


async def test_a_rejected_head_is_reported_as_a_credential_problem(
    prefixed_s3: PrefixedS3, session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``403`` on HEAD is not "size unknown"; it names what to fix."""
    prefixed_s3.head_status = 403
    ctx = make_ctx(session, tmp_path)
    ctx.options["s3"] = _options(prefixed_s3.base_url)

    tasks = await S3Source().resolve("s3://bucket/logs/a.txt", ctx)

    assert tasks[0].size == 0
    err = capsys.readouterr().err
    assert "was rejected (HTTP 403)" in err
    assert "[plugins.s3]" in err


async def test_a_repeated_continuation_token_is_refused(
    prefixed_s3: PrefixedS3, session, tmp_path: Path
) -> None:
    """A server that replays a token would otherwise list forever."""
    ctx = make_ctx(session, tmp_path)
    ctx.options["s3"] = _options(prefixed_s3.base_url)

    with pytest.raises(DownctlError, match="repeated a continuation token"):
        await S3Source().resolve("s3://bucket/logs/", ctx)


async def test_a_listing_that_never_advances_is_bounded(
    prefixed_s3: PrefixedS3, session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh token on every page still has to stop somewhere."""
    monkeypatch.setattr(s3_source, "LIST_PAGE_MAX", 3)
    prefixed_s3.token_mode = "fresh"
    ctx = make_ctx(session, tmp_path)
    ctx.options["s3"] = _options(prefixed_s3.base_url)

    with pytest.raises(DownctlError, match="did not finish after 3 pages"):
        await S3Source().resolve("s3://bucket/logs/", ctx)


async def test_a_listing_that_repeats_a_key_is_deduplicated(
    prefixed_s3: PrefixedS3, session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh token per page with the same key slipped past the token check.

    Reproduced before the fix: five pages produced five entries for three keys,
    i.e. the same object queued more than once.
    """
    monkeypatch.setattr(s3_source, "LIST_PAGE_MAX", 5)
    prefixed_s3.token_mode = "fresh"
    prefixed_s3.key_mode = "same"
    prefixed_s3.stop_after = 3
    ctx = make_ctx(session, tmp_path)
    ctx.options["s3"] = _options(prefixed_s3.base_url)

    tasks = await S3Source().resolve("s3://bucket/logs/", ctx)
    assert [task.target for task in tasks] == ["same.bin"]
