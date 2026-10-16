"""Integration tests for the S3 source plugin against a fake S3 endpoint."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from downctl import app
from downctl.builtin.sources.s3_source import S3Source
from downctl.config import Config, CoreConfig
from downctl.core.context import RuntimeContext

OBJECTS = {
    "logs/a.txt": b"A" * 100,
    "logs/sub/b.bin": b"B" * 4096,
    "other/c.txt": b"C" * 50,
}


@dataclass
class FakeS3:
    base_url: str
    keys: list[str] = field(default_factory=list)


def _xml(prefix: str) -> str:
    rows = [
        f"<Contents><Key>{key}</Key><Size>{len(data)}</Size></Contents>"
        for key, data in OBJECTS.items()
        if key.startswith(prefix)
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        "<IsTruncated>false</IsTruncated>" + "".join(rows) + "</ListBucketResult>"
    )


@pytest_asyncio.fixture
async def fake_s3() -> FakeS3:
    keys: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        tail = request.path.lstrip("/")
        bucket, _, key = tail.partition("/")
        if "list-type" in request.query:
            keys.append(f"LIST:{bucket}:{request.query.get('prefix', '')}")
            return web.Response(
                text=_xml(request.query.get("prefix", "")),
                content_type="application/xml",
            )
        key = unquote(key)
        data = OBJECTS.get(key)
        if data is None:
            raise web.HTTPNotFound
        if request.method == "HEAD":
            return web.Response(
                headers={"Content-Length": str(len(data)), "Accept-Ranges": "bytes"}
            )
        return web.Response(body=data, headers={"Accept-Ranges": "bytes"})

    application = web.Application()
    application.router.add_route("GET", "/{tail:.*}", handler)
    application.router.add_route("HEAD", "/{tail:.*}", handler)

    runner = web.AppRunner(application, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None  # noqa: SLF001
    port = site._server.sockets[0].getsockname()[1]

    server = FakeS3(base_url=f"http://127.0.0.1:{port}", keys=keys)
    try:
        yield server
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


def s3_options(base_url: str) -> dict:
    return {
        "endpoint": base_url,
        "path_style": True,
        "access_key": "AKID",
        "secret_key": "SECRET",
        "region": "us-east-1",
    }


def make_ctx(session, tmp_path: Path, params: dict | None = None) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path))),
        params=params or {},
    )


async def test_single_object(fake_s3: FakeS3, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    ctx.options["s3"] = s3_options(fake_s3.base_url)

    tasks = await S3Source().resolve("s3://bucket/logs/a.txt", ctx)
    assert len(tasks) == 1
    assert tasks[0].target == "a.txt"
    assert tasks[0].size == len(OBJECTS["logs/a.txt"])
    assert "X-Amz-Signature=" in tasks[0].url


async def test_prefix_listing(fake_s3: FakeS3, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    ctx.options["s3"] = s3_options(fake_s3.base_url)

    tasks = await S3Source().resolve("s3://bucket/logs/", ctx)
    by_target = {t.target: t for t in tasks}
    assert set(by_target) == {"a.txt", "sub/b.bin"}
    assert by_target["sub/b.bin"].size == len(OBJECTS["logs/sub/b.bin"])
    assert "other" not in fake_s3.keys[0]


async def test_prefix_include(fake_s3: FakeS3, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path, params={"include": ["*.bin"]})
    ctx.options["s3"] = s3_options(fake_s3.base_url)

    tasks = await S3Source().resolve("s3://bucket/logs/", ctx)
    assert [t.target for t in tasks] == ["sub/b.bin"]


async def test_end_to_end_download(
    fake_s3: FakeS3, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[plugins.s3]\n"
        f'endpoint = "{fake_s3.base_url}"\n'
        'access_key = "AKID"\n'
        'secret_key = "SECRET"\n'
        "path_style = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOWNCTL_CONFIG", str(config_file))

    destination = tmp_path / "dl"
    args = app._default_get_args()
    args.targets = ["s3://bucket/logs/"]
    args.dir = str(destination)
    args.reporter = "quiet"

    assert await app._get(args) == 0
    assert (destination / "a.txt").read_bytes() == OBJECTS["logs/a.txt"]
    assert (destination / "sub" / "b.bin").read_bytes() == OBJECTS["logs/sub/b.bin"]
