"""Unit tests for the HTTP directory-listing source plugin.

Every test talks to a real ``aiohttp.web`` server on ``127.0.0.1``; nothing
about listing parsing or redirect handling is mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from downctl.builtin.sources.http_index_source import HTTPIndexSource
from downctl.config import Config, CoreConfig
from downctl.core.context import RuntimeContext
from downctl.core.errors import DownctlError, PermanentHTTPError, TransientHTTPError

HTML_LISTING = """<!doctype html>
<html><body>
<h1>Index of /html</h1>
<a href="alpha.bin">alpha.bin</a>
<a href="./beta.bin">beta.bin</a>
<a href="gamma%20delta.txt">gamma delta.txt</a>
</body></html>"""

#: Every link here names something that is not a downloadable file in the
#: listing directory.
NOISE_LISTING = """<!doctype html>
<html><body>
<a href="?C=N;O=D">Name</a>
<a href="/">Parent Directory</a>
<a href="/other/file.bin">Root relative</a>
<a href="../">Parent</a>
<a href="subdir/">subdir/</a>
<a href="https://example.invalid/evil.bin">external</a>
<a href="//example.invalid/evil2.bin">protocol relative external</a>
<a href="mailto:admin@example.invalid">mail</a>
<a href="#top">top</a>
<a href="">empty</a>
</body></html>"""

ENCODED_LISTING = """<!doctype html>
<html><body>
<a href="file%20name.bin">file name.bin</a>
<a href="nested%2F..%2Fescape.bin">escape</a>
</body></html>"""

JSON_MANIFEST = {
    "files": [
        {"name": "one.bin", "size": 10},
        {"name": "two.bin", "size": 20},
        {"name": "no-size.bin"},
    ]
}

JSON_ARRAY = ["a.bin", "sub/b.txt", {"name": "c.bin", "size": 3}]

FILTER_MANIFEST = {"files": [{"name": "keep.bin"}, {"name": "skip.txt"}]}


@dataclass
class Server:
    """The fixture server's base URL plus the paths it was asked for."""

    base_url: str
    requests: list[str] = field(default_factory=list)


@pytest_asyncio.fixture
async def index_server() -> AsyncIterator[Server]:
    requests: list[str] = []

    async def html(request: web.Request) -> web.Response:
        requests.append(request.path)
        return web.Response(text=HTML_LISTING, content_type="text/html")

    async def noise(request: web.Request) -> web.Response:
        return web.Response(text=NOISE_LISTING, content_type="text/html")

    async def encoded(request: web.Request) -> web.Response:
        return web.Response(text=ENCODED_LISTING, content_type="text/html")

    async def json_manifest(request: web.Request) -> web.Response:
        return web.json_response(JSON_MANIFEST)

    async def json_array(request: web.Request) -> web.Response:
        return web.json_response(JSON_ARRAY)

    async def filter_manifest(request: web.Request) -> web.Response:
        return web.json_response(FILTER_MANIFEST)

    async def url_field(request: web.Request) -> web.Response:
        base = f"{request.scheme}://{request.host}"
        return web.json_response(
            {
                "files": [
                    {
                        "name": "local.bin",
                        "size": 7,
                        "url": f"{base}/served/real.bin",
                    },
                    {
                        "name": "fallback.bin",
                        "url": "https://example.invalid/elsewhere.bin",
                    },
                ]
            }
        )

    async def redirect(request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound("/html/")

    async def missing(request: web.Request) -> web.Response:
        raise web.HTTPNotFound()

    async def broken(request: web.Request) -> web.Response:
        raise web.HTTPInternalServerError()

    async def empty(request: web.Request) -> web.Response:
        return web.Response(text="<html><body>nothing here</body></html>", content_type="text/html")

    async def sniffed(request: web.Request) -> web.Response:
        # No JSON content type: the body itself is the signal.
        return web.Response(
            text='\n  {"files": [{"name": "sniffed.bin", "size": 4}]}',
            content_type="text/plain",
        )

    async def broken_json(request: web.Request) -> web.Response:
        return web.Response(text="{this is not json", content_type="text/plain")

    async def object_without_files(request: web.Request) -> web.Response:
        return web.json_response({"error": "not a directory listing"})

    async def non_array_files(request: web.Request) -> web.Response:
        return web.json_response({"files": "not-a-list"})

    async def empty_json(request: web.Request) -> web.Response:
        return web.json_response([])

    async def surrogate_json(request: web.Request) -> web.Response:
        # A lone surrogate is valid JSON when escaped; json.loads produces the
        # unencodable str, which used to become UnicodeEncodeError -> HTTP 500.
        return web.Response(text='["\\ud800"]', content_type="application/json")

    async def mixed_surrogate_json(request: web.Request) -> web.Response:
        return web.Response(
            text='["ok.bin", "\\ud800"]',
            content_type="application/json",
        )

    application = web.Application()
    application.router.add_get("/html/", html)
    application.router.add_get("/noise/", noise)
    application.router.add_get("/encoded/", encoded)
    application.router.add_get("/json/", json_manifest)
    application.router.add_get("/array/", json_array)
    application.router.add_get("/filter/", filter_manifest)
    application.router.add_get("/urlfield/", url_field)
    application.router.add_get("/redirect/", redirect)
    application.router.add_get("/missing/", missing)
    application.router.add_get("/broken/", broken)
    application.router.add_get("/empty/", empty)
    application.router.add_get("/sniffed/", sniffed)
    application.router.add_get("/broken-json/", broken_json)
    application.router.add_get("/object-without-files/", object_without_files)
    application.router.add_get("/non-array-files/", non_array_files)
    application.router.add_get("/empty-json/", empty_json)
    application.router.add_get("/surrogate/", surrogate_json)
    application.router.add_get("/mixed-surrogate/", mixed_surrogate_json)

    runner = web.AppRunner(application, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    server = Server(base_url=f"http://127.0.0.1:{port}", requests=requests)
    try:
        yield server
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    # No trust_env: an ambient HTTP_PROXY must not decide whether a loopback
    # fixture is reachable.
    async with aiohttp.ClientSession(trust_env=False) as client:
        yield client


def make_ctx(
    session: aiohttp.ClientSession,
    tmp_path: Path,
    params: dict[str, object] | None = None,
) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path))),
        params=params or {},
    )


async def test_can_handle_only_http_directories(session, tmp_path: Path) -> None:
    source = HTTPIndexSource()

    assert source.can_handle("http://example.com/dir/")
    assert source.can_handle("https://example.com/dir/?token=x")
    assert source.can_handle("https://example.com/dir/#section")

    assert not source.can_handle("https://example.com/file.bin")
    assert not source.can_handle("https://example.com/dir")
    assert not source.can_handle("ftp://example.com/dir/")
    assert not source.can_handle("./dir/")
    assert not source.can_handle("https://example.com")
    assert not source.can_handle("http://[broken/dir/")

    assert source.name == "http_index"
    assert source.priority == 5
    assert source.supports_filters is True

    with pytest.raises(DownctlError, match="only handles HTTP"):
        await source.resolve("ftp://example.com/dir/", make_ctx(session, tmp_path))


async def test_html_listing_builds_tasks(index_server: Server, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)

    tasks = await HTTPIndexSource().resolve(f"{index_server.base_url}/html/", ctx)

    by_target = {task.target: task for task in tasks}
    assert set(by_target) == {"alpha.bin", "beta.bin", "gamma delta.txt"}
    assert by_target["alpha.bin"].url == f"{index_server.base_url}/html/alpha.bin"
    assert by_target["beta.bin"].url == f"{index_server.base_url}/html/beta.bin"
    assert by_target["gamma delta.txt"].url == f"{index_server.base_url}/html/gamma%20delta.txt"
    for task in tasks:
        assert task.source == "http_index"
        assert task.size == 0
    # One request for the whole listing, not one per link.
    assert index_server.requests == ["/html/"]


async def test_html_listing_skips_noise(index_server: Server, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)

    tasks = await HTTPIndexSource().resolve(f"{index_server.base_url}/noise/", ctx)

    assert tasks == []


async def test_json_manifest_with_sizes(index_server: Server, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)

    tasks = await HTTPIndexSource().resolve(f"{index_server.base_url}/json/", ctx)

    by_target = {task.target: task for task in tasks}
    assert set(by_target) == {"one.bin", "two.bin", "no-size.bin"}
    assert by_target["one.bin"].size == 10
    assert by_target["two.bin"].size == 20
    assert by_target["no-size.bin"].size == 0
    assert by_target["one.bin"].url == f"{index_server.base_url}/json/one.bin"


async def test_json_bare_array_of_strings(index_server: Server, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)

    tasks = await HTTPIndexSource().resolve(f"{index_server.base_url}/array/", ctx)

    by_target = {task.target: task for task in tasks}
    assert set(by_target) == {"a.bin", "b.txt", "c.bin"}
    assert by_target["a.bin"].url == f"{index_server.base_url}/array/a.bin"
    # A nested name keeps its URL path but cannot create a nested target.
    assert by_target["b.txt"].url == f"{index_server.base_url}/array/sub/b.txt"
    assert by_target["c.bin"].size == 3
    assert all("/" not in task.target for task in tasks)


async def test_json_include_and_exclude_filters_applied(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path, params={"include": ["*.bin"]})

    tasks = await HTTPIndexSource().resolve(f"{index_server.base_url}/filter/", ctx)

    assert [task.target for task in tasks] == ["keep.bin"]

    excluded = make_ctx(session, tmp_path, params={"exclude": ["*.bin"]})
    tasks = await HTTPIndexSource().resolve(f"{index_server.base_url}/filter/", excluded)
    assert [task.target for task in tasks] == ["skip.txt"]


async def test_include_matching_nothing_fails_closed(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path, params={"include": ["*.nomatch"]})

    with pytest.raises(DownctlError, match=r"--include \*\.nomatch"):
        await HTTPIndexSource().resolve(f"{index_server.base_url}/filter/", ctx)


async def test_missing_listing_is_permanent(index_server: Server, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)

    with pytest.raises(PermanentHTTPError) as caught:
        await HTTPIndexSource().resolve(f"{index_server.base_url}/missing/", ctx)

    assert caught.value.status == 404
    assert caught.value.url == f"{index_server.base_url}/missing/"


async def test_server_error_is_transient(index_server: Server, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)

    with pytest.raises(TransientHTTPError) as caught:
        await HTTPIndexSource().resolve(f"{index_server.base_url}/broken/", ctx)

    assert caught.value.status == 500
    assert caught.value.retry_after is None


async def test_encoded_names_are_decoded_and_basename_safe(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)

    tasks = await HTTPIndexSource().resolve(f"{index_server.base_url}/encoded/", ctx)

    by_target = {task.target: task for task in tasks}
    assert set(by_target) == {"file name.bin", "escape.bin"}
    assert by_target["file name.bin"].url == (f"{index_server.base_url}/encoded/file%20name.bin")
    # ``nested%2F..%2Fescape.bin`` decodes to a traversal attempt; only the
    # final basename may survive.
    assert by_target["escape.bin"].url == (
        f"{index_server.base_url}/encoded/nested%2F..%2Fescape.bin"
    )
    assert all("/" not in task.target and "\\" not in task.target for task in tasks)


async def test_trailing_slash_redirect_is_followed(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)

    tasks = await HTTPIndexSource().resolve(f"{index_server.base_url}/redirect/", ctx)

    # Relative links were resolved against ``/html/``, the redirected URL.
    assert {task.target for task in tasks} == {"alpha.bin", "beta.bin", "gamma delta.txt"}
    assert all(task.url.startswith(f"{index_server.base_url}/html/") for task in tasks)


async def test_json_absolute_url_wins_only_when_same_origin(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)

    tasks = await HTTPIndexSource().resolve(f"{index_server.base_url}/urlfield/", ctx)

    by_target = {task.target: task for task in tasks}
    assert set(by_target) == {"local.bin", "fallback.bin"}
    # A same-origin absolute URL wins; a cross-origin one is ignored and the
    # name is resolved against the listing URL instead.
    assert by_target["local.bin"].url == f"{index_server.base_url}/served/real.bin"
    assert by_target["local.bin"].size == 7
    assert by_target["fallback.bin"].url == (f"{index_server.base_url}/urlfield/fallback.bin")


async def test_empty_html_listing_is_an_explicit_error(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)

    with pytest.raises(DownctlError, match="contains no links"):
        await HTTPIndexSource().resolve(f"{index_server.base_url}/empty/", ctx)


async def test_json_object_without_a_files_array_is_an_explicit_error(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)

    with pytest.raises(DownctlError, match="without a 'files' array"):
        await HTTPIndexSource().resolve(f"{index_server.base_url}/object-without-files/", ctx)


async def test_json_files_field_must_be_an_array(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)

    with pytest.raises(DownctlError, match="non-array 'files' field"):
        await HTTPIndexSource().resolve(f"{index_server.base_url}/non-array-files/", ctx)


async def test_empty_json_listing_is_an_explicit_error(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)

    with pytest.raises(DownctlError, match="is empty"):
        await HTTPIndexSource().resolve(f"{index_server.base_url}/empty-json/", ctx)


async def test_lone_surrogate_in_json_names_is_rejected_cleanly(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)

    with pytest.raises(DownctlError, match="no usable file entries") as caught:
        await HTTPIndexSource().resolve(f"{index_server.base_url}/surrogate/", ctx)
    # The error itself must stay printable: no lone surrogate may leak into it.
    str(caught.value).encode("utf-8")


async def test_lone_surrogate_entries_are_skipped_beside_valid_ones(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)

    tasks = await HTTPIndexSource().resolve(f"{index_server.base_url}/mixed-surrogate/", ctx)

    assert [task.target for task in tasks] == ["ok.bin"]
    assert tasks[0].url == f"{index_server.base_url}/mixed-surrogate/ok.bin"


async def test_json_body_is_sniffed_without_a_json_content_type(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)

    tasks = await HTTPIndexSource().resolve(f"{index_server.base_url}/sniffed/", ctx)

    assert [(task.target, task.size) for task in tasks] == [("sniffed.bin", 4)]


async def test_invalid_json_listing_is_rejected(
    index_server: Server, session, tmp_path: Path
) -> None:
    ctx = make_ctx(session, tmp_path)

    with pytest.raises(DownctlError, match="not valid JSON"):
        await HTTPIndexSource().resolve(f"{index_server.base_url}/broken-json/", ctx)
