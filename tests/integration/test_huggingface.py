"""Integration tests for the HuggingFace source plugin against a fake Hub."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from downctl import app
from downctl.builtin.sources.huggingface_source import HuggingFaceSource
from downctl.config import Config, CoreConfig
from downctl.core.context import RuntimeContext

CONFIG = b'{"hidden_size": 16}'
WEIGHTS = b"W" * 4096
WEIGHTS_SHA = hashlib.sha256(WEIGHTS).hexdigest()


@dataclass
class Hub:
    base_url: str
    files: dict[str, bytes] = field(default_factory=dict)
    requests: list[tuple[str, str]] = field(default_factory=list)


@pytest_asyncio.fixture
async def hub() -> Hub:
    files = {"config.json": CONFIG, "model.safetensors": WEIGHTS}
    requests: list[tuple[str, str]] = []

    async def listing(request: web.Request) -> web.Response:
        repo = unquote(request.match_info["repo"])
        revision = request.query.get("revision", "main")
        siblings: list[dict[str, object]] = [
            {"rfilename": "config.json", "size": len(CONFIG)},
            {
                "rfilename": "model.safetensors",
                "lfs": {"sha256": WEIGHTS_SHA, "size": len(WEIGHTS)},
            },
        ]
        requests.append((repo, revision))
        return web.json_response({"siblings": siblings})

    async def blob(request: web.Request) -> web.StreamResponse:
        rel = unquote(request.match_info["rel"])
        data = files.get(rel)
        if data is None:
            raise web.HTTPNotFound
        return web.Response(body=data, headers={"Accept-Ranges": "bytes"})

    application = web.Application()
    application.router.add_get("/api/{kind}/{repo:.+}", listing)
    application.router.add_get("/{repo:.+}/resolve/{rev}/{rel:.+}", blob)

    runner = web.AppRunner(application, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]

    server = Hub(base_url=f"http://127.0.0.1:{port}", files=files, requests=requests)
    try:
        yield server
    finally:
        await runner.cleanup()


def make_ctx(
    session: aiohttp.ClientSession, tmp_path: Path, params: dict | None = None
) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path))),
        options={"huggingface": {}},  # host defaults; overridden per test
        params=params or {},
    )


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


async def test_resolve_expands_repo(hub: Hub, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    ctx.options["huggingface"] = {"mirror": hub.base_url}

    tasks = await HuggingFaceSource().resolve("hf://org/repo", ctx)
    by_target = {t.target: t for t in tasks}
    assert set(by_target) == {"config.json", "model.safetensors"}
    assert by_target["config.json"].url == f"{hub.base_url}/org/repo/resolve/main/config.json"
    weights = by_target["model.safetensors"]
    assert weights.size == len(WEIGHTS)
    assert weights.checksum_algo == "sha256"
    assert weights.checksum_hex == WEIGHTS_SHA


async def test_resolve_include_filter(hub: Hub, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path, params={"include": ["*.safetensors"]})
    ctx.options["huggingface"] = {"mirror": hub.base_url}

    tasks = await HuggingFaceSource().resolve("org/repo", ctx)
    assert [t.target for t in tasks] == ["model.safetensors"]


async def test_resolve_revision(hub: Hub, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    ctx.options["huggingface"] = {"mirror": hub.base_url}

    tasks = await HuggingFaceSource().resolve("hf://org/repo@dev", ctx)
    assert all("/resolve/dev/" in t.url for t in tasks)
    assert ("org/repo", "dev") in hub.requests  # type: ignore[attr-defined]


async def test_resolve_attaches_token_header(hub: Hub, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    ctx.options["huggingface"] = {"mirror": hub.base_url, "token": "hf_secret"}

    tasks = await HuggingFaceSource().resolve("hf://org/repo", ctx)
    assert tasks
    assert all(t.headers.get("Authorization") == "Bearer hf_secret" for t in tasks)


async def test_end_to_end_download(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)

    config_file = tmp_path / "config.toml"
    config_file.write_text(f'[plugins.huggingface]\nmirror = "{hub.base_url}"\n', encoding="utf-8")
    monkeypatch.setenv("DOWNCTL_CONFIG", str(config_file))

    destination = tmp_path / "repo"
    args = app._default_get_args()
    args.targets = ["hf://org/repo"]
    args.dir = str(destination)
    args.reporter = "quiet"

    assert await app._get(args) == 0
    assert (destination / "config.json").read_bytes() == CONFIG
    assert (destination / "model.safetensors").read_bytes() == WEIGHTS
    assert json.loads((destination / "config.json").read_text(encoding="utf-8"))
