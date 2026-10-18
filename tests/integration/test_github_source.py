"""Integration tests for the GitHub Releases source plugin."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from downctl import app
from downctl.builtin.sources.github_source import GitHubSource
from downctl.config import Config, CoreConfig
from downctl.core.context import RuntimeContext

ASSETS = {
    "app-linux.tar.gz": b"L" * 2048,
    "app-win.zip": b"W" * 1024,
}


@dataclass
class Hub:
    base_url: str
    requests: list[str] = field(default_factory=list)


@pytest_asyncio.fixture
async def gh() -> Hub:
    requests: list[str] = []

    def payload(base: str) -> dict:
        return {
            "tag_name": "v1.0.0",
            "assets": [
                {
                    "name": name,
                    "browser_download_url": f"{base}/download/{name}",
                    "url": f"{base}/api/assets/{name}",
                    "size": len(data),
                    "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                }
                for name, data in ASSETS.items()
            ],
        }

    async def listing(request: web.Request) -> web.Response:
        base = f"{request.scheme}://{request.host}"
        tag = request.match_info.get("tag", "latest")
        requests.append(tag)
        if tag not in ("latest", "v1.0.0"):
            raise web.HTTPNotFound
        return web.json_response(payload(base))

    async def blob(request: web.Request) -> web.StreamResponse:
        data = ASSETS.get(unquote(request.match_info["name"]))
        if data is None:
            raise web.HTTPNotFound
        return web.Response(body=data, headers={"Accept-Ranges": "bytes"})

    application = web.Application()
    application.router.add_get("/repos/{owner}/{repo}/releases/latest", listing)
    application.router.add_get("/repos/{owner}/{repo}/releases/tags/{tag}", listing)
    application.router.add_get("/download/{name}", blob)

    runner = web.AppRunner(application, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    server = Hub(base_url=f"http://127.0.0.1:{port}", requests=requests)
    try:
        yield server
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


def make_ctx(session, tmp_path: Path, params: dict | None = None) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path))),
        params=params or {},
    )


async def test_resolve_release_assets(gh: Hub, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    ctx.options["github"] = {"api": gh.base_url}

    tasks = await GitHubSource().resolve("gh://me/app", ctx)
    by_target = {t.target: t for t in tasks}
    assert set(by_target) == set(ASSETS)
    linux = by_target["app-linux.tar.gz"]
    assert linux.url == f"{gh.base_url}/download/app-linux.tar.gz"
    assert linux.size == len(ASSETS["app-linux.tar.gz"])
    assert linux.checksum_algo == "sha256"
    assert linux.checksum_hex == hashlib.sha256(ASSETS["app-linux.tar.gz"]).hexdigest()


async def test_resolve_tag_and_include(gh: Hub, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path, params={"include": ["*.zip"]})
    ctx.options["github"] = {"api": gh.base_url}

    tasks = await GitHubSource().resolve("gh://me/app@v1.0.0", ctx)
    assert [t.target for t in tasks] == ["app-win.zip"]
    assert gh.requests[-1] == "v1.0.0"


async def test_resolve_unknown_tag(gh: Hub, session, tmp_path: Path) -> None:
    from downctl.core.errors import DownctlError

    ctx = make_ctx(session, tmp_path)
    ctx.options["github"] = {"api": gh.base_url}
    with pytest.raises(DownctlError, match="cannot list release"):
        await GitHubSource().resolve("gh://me/app@nope", ctx)


async def test_token_uses_api_asset_url(gh: Hub, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    ctx.options["github"] = {"api": gh.base_url, "token": "secret"}

    tasks = await GitHubSource().resolve("gh://me/app", ctx)
    linux = {t.target: t for t in tasks}["app-linux.tar.gz"]
    assert linux.url == f"{gh.base_url}/api/assets/app-linux.tar.gz"
    assert linux.headers["Authorization"] == "Bearer secret"
    assert linux.headers["Accept"] == "application/octet-stream"


async def test_public_uses_browser_url(gh: Hub, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    ctx.options["github"] = {"api": gh.base_url}

    tasks = await GitHubSource().resolve("gh://me/app", ctx)
    linux = {t.target: t for t in tasks}["app-linux.tar.gz"]
    assert linux.url == f"{gh.base_url}/download/app-linux.tar.gz"
    assert linux.headers == {}


async def test_end_to_end_download(
    gh: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)

    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[plugins.github]\napi = "{gh.base_url}"\n', encoding="utf-8")
    monkeypatch.setenv("DOWNCTL_CONFIG", str(config_file))

    destination = tmp_path / "dl"
    args = app._default_get_args()
    args.targets = ["gh://me/app"]
    args.dir = str(destination)
    args.reporter = "quiet"

    assert await app._get(args) == 0
    assert (destination / "app-linux.tar.gz").read_bytes() == ASSETS["app-linux.tar.gz"]
    assert (destination / "app-win.zip").read_bytes() == ASSETS["app-win.zip"]
