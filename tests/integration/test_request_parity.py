"""A task's request contract must reach every request made for it.

``DownloadTask.headers`` is how a source plugin hands the transfer the
credentials it negotiated for one specific object. The transfer used them; the
probe did not. A credentialed target was therefore *measured* by a request with
no credential, answered ``401``, and was classified permanent before a single
byte was fetched — while the same URL answered ``200`` to the download.

These tests pin the contract at both ends: :func:`downctl.probe.probe_metadata`
sends the headers it is given on *both* of its requests, and the probe phase
passes the task's. They also pin who may receive a token: the host that issued
it, and a mirror only when the user says so explicitly.
"""

from __future__ import annotations

import hashlib
import re
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
from downctl.core.errors import DownctlError
from downctl.core.task import DownloadTask
from downctl.probe import probe_metadata

CONFIG_BYTES = b'{"hidden_size": 16}'
WEIGHTS = b"W" * 7500
WEIGHTS_SHA = hashlib.sha256(WEIGHTS).hexdigest()
TOKEN = "hf_private_token"
AUTHORIZATION = f"Bearer {TOKEN}"


@dataclass
class Hub:
    """A Hub that can demand the credential, and remembers what it was sent."""

    base_url: str
    #: ``(method, path, Authorization header or "")`` per request served.
    seen: list[tuple[str, str, str]] = field(default_factory=list)
    #: Reject the listing without the credential (a private repository).
    require_listing_auth: bool = True
    #: Reject a blob without the credential.
    require_blob_auth: bool = True

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def auth_for(self, path: str) -> list[str]:
        return [auth for _, seen_path, auth in self.seen if seen_path == path]

    def methods_for(self, path: str) -> list[str]:
        return [method for method, seen_path, _ in self.seen if seen_path == path]


def _record(hub: Hub, request: web.Request) -> None:
    hub.seen.append((request.method, request.path, request.headers.get("Authorization", "")))


def _authorised(hub: Hub, request: web.Request, required: bool) -> bool:
    return not required or request.headers.get("Authorization") == AUTHORIZATION


def _range_response(body: bytes, header: str) -> web.Response:
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", header.strip())
    if match is None:
        return web.Response(status=400, text="malformed Range header")
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else len(body) - 1
    end = min(end, len(body) - 1)
    if start >= len(body):
        return web.Response(status=416, headers={"Content-Range": f"bytes */{len(body)}"})
    return web.Response(
        status=206,
        body=body[start : end + 1],
        headers={
            "Content-Range": f"bytes {start}-{end}/{len(body)}",
            "Accept-Ranges": "bytes",
            "ETag": '"private-v1"',
        },
    )


@pytest_asyncio.fixture
async def hub() -> Hub:
    state = Hub(base_url="")

    async def listing(request: web.Request) -> web.Response:
        _record(state, request)
        if not _authorised(state, request, state.require_listing_auth):
            return web.Response(status=401, text="authentication required")
        return web.json_response(
            {
                "siblings": [
                    {"rfilename": "config.json", "size": len(CONFIG_BYTES)},
                    {
                        "rfilename": "weights.bin",
                        "lfs": {"sha256": WEIGHTS_SHA, "size": len(WEIGHTS)},
                    },
                ]
            }
        )

    async def blob(request: web.Request) -> web.StreamResponse:
        _record(state, request)
        rel = unquote(request.match_info["rel"])
        if not _authorised(state, request, state.require_blob_auth):
            return web.Response(status=401, text="authentication required")
        body = {"config.json": CONFIG_BYTES, "weights.bin": WEIGHTS}.get(rel)
        if body is None:
            raise web.HTTPNotFound
        header = request.headers.get("Range")
        if header:
            return _range_response(body, header)
        return web.Response(body=body, headers={"Accept-Ranges": "bytes", "ETag": '"private-v1"'})

    async def head_rejected(request: web.Request) -> web.StreamResponse:
        """A method-scoped rejection: HEAD is refused even when authorised.

        Modelled on a presigned URL, whose signature covers the HTTP method. The
        probe falls back to a ranged GET, and *that* request is the one the
        credential has to reach.
        """
        _record(state, request)
        if request.method == "HEAD":
            return web.Response(status=403, text="HEAD is not signed")
        if request.headers.get("Authorization") != AUTHORIZATION:
            return web.Response(status=401, text="authentication required")
        header = request.headers.get("Range")
        if not header:
            return web.Response(body=WEIGHTS, headers={"Accept-Ranges": "bytes"})
        return _range_response(WEIGHTS, header)

    application = web.Application()
    application.router.add_get("/api/{kind}/{repo:.+}", listing)
    application.router.add_get("/{repo:.+}/resolve/{rev}/{rel:.+}", blob)
    application.router.add_route("*", "/head-rejected", head_rejected)

    runner = web.AppRunner(application, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.base_url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        yield state
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def session() -> aiohttp.ClientSession:
    async with aiohttp.ClientSession() as client:
        yield client


def make_ctx(
    session: aiohttp.ClientSession, tmp_path: Path, options: dict | None = None
) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path))),
        options={"huggingface": options or {}},
        params={},
    )


# --------------------------------------------------------------------------- #
# the probe carries what the task carries
# --------------------------------------------------------------------------- #
async def test_probe_metadata_sends_the_task_headers_on_both_requests(
    hub: Hub, session: aiohttp.ClientSession
) -> None:
    """HEAD *and* the ranged-GET fallback carry the credential.

    A probe that authenticated only its HEAD would still fail on the objects
    whose HEAD is not usable, which is exactly the presigned-URL case the
    fallback exists for.
    """
    result = await probe_metadata(
        session, hub.url("/head-rejected"), headers={"Authorization": AUTHORIZATION}
    )

    assert result.ok, f"probe failed: {result.error}"
    assert result.size == len(WEIGHTS)
    assert hub.methods_for("/head-rejected") == ["HEAD", "GET"]
    assert hub.auth_for("/head-rejected") == [AUTHORIZATION, AUTHORIZATION]


async def test_the_probe_headers_cannot_override_the_identity_encoding(
    hub: Hub, session: aiohttp.ClientSession
) -> None:
    """A caller cannot make the probe measure a different representation.

    ``Accept-Encoding: identity`` is what keeps the reported length equal to the
    bytes the transfer will receive; letting a task header win would let the
    probe describe a compressed body the transport never asks for.
    """
    seen: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(request.headers.get("Accept-Encoding", ""))
        return web.Response(body=b"x" * 4, headers={"ETag": '"e"'})

    application = web.Application()
    application.router.add_route("*", "/enc", handler)
    runner = web.AppRunner(application, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        url = f"http://127.0.0.1:{runner.addresses[0][1]}/enc"
        result = await probe_metadata(session, url, headers={"Accept-Encoding": "gzip"})
    finally:
        await runner.cleanup()

    assert result.ok
    assert seen == ["identity"]


async def test_a_credentialed_target_is_probed_with_its_own_headers(
    hub: Hub, session: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """The probe phase passes ``task.headers`` to the probe.

    Removing ``headers=task.headers`` at this call site is what turned every
    private HuggingFace repository into a ``permanent`` failure in 0.1.4.
    """
    task = DownloadTask(
        url=hub.url("/acme/private/resolve/main/weights.bin"),
        target="weights.bin",
        source="huggingface",
        headers={"Authorization": AUTHORIZATION},
    )
    ctx = make_ctx(session, tmp_path)

    await app._probe_task(ctx, task, retries=0)

    assert task.size == len(WEIGHTS)
    assert hub.auth_for("/acme/private/resolve/main/weights.bin") == [AUTHORIZATION]


async def test_an_uncredentialed_probe_of_the_same_target_is_rejected(
    hub: Hub, session: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """The fixture really is enforcing the credential.

    Without this the test above would pass against a server that accepts
    anything, and would not notice the header being dropped.
    """
    task = DownloadTask(
        url=hub.url("/acme/private/resolve/main/weights.bin"),
        target="weights.bin",
        source="huggingface",
    )
    ctx = make_ctx(session, tmp_path)

    with pytest.raises(Exception) as caught:
        await app._probe_task(ctx, task, retries=0)

    assert "401" in str(caught.value)


# --------------------------------------------------------------------------- #
# a token belongs to the host that issued it
# --------------------------------------------------------------------------- #
async def test_the_hf_token_is_not_sent_to_a_configured_mirror(
    hub: Hub, session: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """Configuring a mirror is not consent to hand it the credential."""
    hub.require_listing_auth = False
    ctx = make_ctx(session, tmp_path, {"mirror": hub.base_url, "token": TOKEN})

    tasks = await HuggingFaceSource().resolve("hf://acme/private", ctx)

    assert tasks
    assert hub.auth_for("/api/models/acme/private") == [""]
    assert all("Authorization" not in task.headers for task in tasks)


async def test_the_hf_token_reaches_a_mirror_when_explicitly_allowed(
    hub: Hub, session: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """``token_to_mirror`` is the documented way to authenticate a mirror."""
    ctx = make_ctx(
        session,
        tmp_path,
        {"mirror": hub.base_url, "token": TOKEN, "token_to_mirror": True},
    )

    tasks = await HuggingFaceSource().resolve("hf://acme/private", ctx)

    assert tasks
    assert hub.auth_for("/api/models/acme/private") == [AUTHORIZATION]
    assert all(task.headers["Authorization"] == AUTHORIZATION for task in tasks)


async def test_a_withheld_token_names_the_switch_that_would_send_it(
    hub: Hub, session: aiohttp.ClientSession, tmp_path: Path
) -> None:
    """A 401 caused by *our own* default has to explain itself.

    The user configured a token and got "authentication required"; the message
    must name the setting that changes the outcome, or the default looks like a
    broken mirror.
    """
    ctx = make_ctx(session, tmp_path, {"mirror": hub.base_url, "token": TOKEN})

    with pytest.raises(DownctlError) as caught:
        await HuggingFaceSource().resolve("hf://acme/private", ctx)

    message = str(caught.value)
    assert "401" in message
    assert "token_to_mirror" in message
    assert hub.auth_for("/api/models/acme/private") == [""]


# --------------------------------------------------------------------------- #
# end to end
# --------------------------------------------------------------------------- #
async def test_a_private_repository_downloads_end_to_end(
    hub: Hub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole chain: listing, probe, transfer — each with the credential.

    This is the regression the 0.1.4 release shipped: every request except the
    probe was authenticated, so the run died before the transfer started.
    """
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[plugins.huggingface]\nmirror = "{hub.base_url}"\n'
        f'token = "{TOKEN}"\ntoken_to_mirror = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOWNCTL_CONFIG", str(config_file))

    destination = tmp_path / "out"
    args = app._default_get_args()
    args.targets = ["hf://acme/private"]
    args.dir = str(destination)
    args.reporter = "quiet"

    assert await app._get(args) == 0

    assert (destination / "weights.bin").read_bytes() == WEIGHTS
    assert (destination / "config.json").read_bytes() == CONFIG_BYTES
    blob_path = "/acme/private/resolve/main/weights.bin"
    # Listing, probe and transfer: every request for this object carried it.
    assert AUTHORIZATION in hub.auth_for("/api/models/acme/private")
    assert hub.auth_for(blob_path) == [AUTHORIZATION] * len(hub.auth_for(blob_path))
