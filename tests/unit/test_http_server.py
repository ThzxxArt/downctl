"""Tests for the built-in HTTP server plugin."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from downctl import console
from downctl.builtin.servers.http_server import HTTPFileServer, create_app
from downctl.core.errors import DownctlError
from downctl.plugin.protocols import API_VERSION, Server

DATA = b"0123456789abcdefghijklmnopqrstuvwxyz"
AUTH = ("alice", "s3cret")
TEN_BYTES = b"0123456789"


@asynccontextmanager
async def _client(root: Path, **kwargs: Any) -> AsyncIterator[TestClient[Any, Any]]:
    """Start ``create_app`` on an ephemeral port without a real bind."""
    app = create_app(str(root), **kwargs)
    async with TestClient(TestServer(app)) as client:
        yield client


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "root"
    directory.mkdir()
    (directory / "data.bin").write_bytes(DATA)
    return directory


def _basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


async def test_get_full_file(root: Path) -> None:
    async with _client(root) as client:
        response = await client.get("/data.bin")
        assert response.status == 200
        assert await response.read() == DATA
        assert response.headers["Accept-Ranges"] == "bytes"
        assert response.headers["Content-Length"] == str(len(DATA))
        assert response.headers["ETag"]


async def test_head_has_identical_headers_and_no_body(root: Path) -> None:
    async with _client(root) as client:
        get_response = await client.get("/data.bin")
        await get_response.read()
        head_response = await client.head("/data.bin")
        assert head_response.status == 200
        for name in ("ETag", "Last-Modified", "Content-Length", "Content-Type", "Accept-Ranges"):
            assert head_response.headers[name] == get_response.headers[name]
        assert await head_response.read() == b""


async def test_range_start_end(root: Path) -> None:
    (root / "ten.bin").write_bytes(TEN_BYTES)
    async with _client(root) as client:
        response = await client.get("/ten.bin", headers={"Range": "bytes=2-5"})
        assert response.status == 206
        assert await response.read() == b"2345"
        assert response.headers["Content-Range"] == "bytes 2-5/10"
        assert response.headers["Content-Length"] == "4"


async def test_range_open_ended(root: Path) -> None:
    (root / "ten.bin").write_bytes(TEN_BYTES)
    async with _client(root) as client:
        response = await client.get("/ten.bin", headers={"Range": "bytes=6-"})
        assert response.status == 206
        assert await response.read() == b"6789"
        assert response.headers["Content-Range"] == "bytes 6-9/10"


async def test_range_suffix(root: Path) -> None:
    (root / "ten.bin").write_bytes(TEN_BYTES)
    async with _client(root) as client:
        response = await client.get("/ten.bin", headers={"Range": "bytes=-3"})
        assert response.status == 206
        assert await response.read() == b"789"
        assert response.headers["Content-Range"] == "bytes 7-9/10"


async def test_multiple_ranges_send_the_whole_file(root: Path) -> None:
    (root / "ten.bin").write_bytes(TEN_BYTES)
    async with _client(root) as client:
        response = await client.get("/ten.bin", headers={"Range": "bytes=0-1,4-5"})
        assert response.status == 200
        assert await response.read() == TEN_BYTES
        assert "Content-Range" not in response.headers


async def test_unsatisfiable_range_is_416(root: Path) -> None:
    (root / "ten.bin").write_bytes(TEN_BYTES)
    async with _client(root) as client:
        response = await client.get("/ten.bin", headers={"Range": "bytes=10-"})
        assert response.status == 416
        assert response.headers["Content-Range"] == "bytes */10"
        await response.read()


async def test_if_range_matches_etag_or_last_modified(root: Path) -> None:
    (root / "ten.bin").write_bytes(TEN_BYTES)
    async with _client(root) as client:
        initial = await client.get("/ten.bin")
        await initial.read()
        etag = initial.headers["ETag"]
        last_modified = initial.headers["Last-Modified"]

        by_etag = await client.get("/ten.bin", headers={"Range": "bytes=0-1", "If-Range": etag})
        assert by_etag.status == 206
        assert await by_etag.read() == b"01"
        assert by_etag.headers["Content-Range"] == "bytes 0-1/10"

        by_date = await client.get(
            "/ten.bin", headers={"Range": "bytes=0-1", "If-Range": last_modified}
        )
        assert by_date.status == 206
        assert await by_date.read() == b"01"

        stale = await client.get(
            "/ten.bin", headers={"Range": "bytes=0-1", "If-Range": '"stale-etag"'}
        )
        assert stale.status == 200
        assert await stale.read() == TEN_BYTES
        assert "Content-Range" not in stale.headers


async def test_etag_comes_from_completion_marker(root: Path) -> None:
    target = root / "marked.bin"
    target.write_bytes(b"marked")
    stat = target.stat()
    marker = {
        "version": 2,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "url_sha256": "identity",
        "checksum_algo": "",
        "checksum_hex": "",
        "validator": "witness-1",
    }
    (root / "marked.bin.downctl-done").write_text(json.dumps(marker), encoding="utf-8")

    async with _client(root) as client:
        response = await client.get("/marked.bin")
        assert response.status == 200
        assert response.headers["ETag"] == '"witness-1"'
        await response.read()

        ranged = await client.get(
            "/marked.bin",
            headers={"Range": "bytes=0-2", "If-Range": '"witness-1"'},
        )
        assert ranged.status == 206
        assert await ranged.read() == b"mar"
        assert ranged.headers["Content-Range"] == "bytes 0-2/6"


async def test_stale_completion_marker_is_not_reused_for_a_replaced_file(root: Path) -> None:
    target = root / "replace.bin"
    target.write_bytes(b"AAAA")
    stat = target.stat()
    marker = {
        "version": 2,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "url_sha256": "identity",
        "checksum_algo": "",
        "checksum_hex": "",
        "validator": "v1",
    }
    (root / "replace.bin.downctl-done").write_text(json.dumps(marker), encoding="utf-8")

    async with _client(root) as client:
        before = await client.head("/replace.bin")
        assert before.headers["ETag"] == '"v1"'

        # Same size, different bytes, different mtime: the marker describes the
        # old revision and must not be used as the new one's ETag.
        target.write_bytes(b"BBBB")
        os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

        current = await client.head("/replace.bin")
        assert current.headers["ETag"] != '"v1"'
        assert current.headers["ETag"] == f'W/"{len(b"BBBB")}-{target.stat().st_mtime_ns}"'

        # This used to answer 206 with B's tail, letting a client splice A+B.
        stale = await client.get(
            "/replace.bin",
            headers={"Range": "bytes=2-3", "If-Range": '"v1"'},
        )
        assert stale.status == 200
        assert await stale.read() == b"BBBB"
        assert "Content-Range" not in stale.headers


async def test_conditional_get_honours_if_none_match_and_if_modified_since(root: Path) -> None:
    (root / "ten.bin").write_bytes(TEN_BYTES)
    async with _client(root) as client:
        initial = await client.get("/ten.bin")
        await initial.read()
        etag = initial.headers["ETag"]
        last_modified = initial.headers["Last-Modified"]

        weak_match = await client.get("/ten.bin", headers={"If-None-Match": f'W/"other", {etag}'})
        assert weak_match.status == 304
        assert await weak_match.read() == b""
        assert weak_match.headers["ETag"] == etag
        assert weak_match.headers["Last-Modified"] == last_modified

        star = await client.get("/ten.bin", headers={"If-None-Match": "*"})
        assert star.status == 304
        await star.read()

        by_date = await client.get("/ten.bin", headers={"If-Modified-Since": last_modified})
        assert by_date.status == 304
        assert by_date.headers["ETag"] == etag
        await by_date.read()

        # If-None-Match wins over If-Modified-Since even when it does not match.
        ignored_date = await client.get(
            "/ten.bin",
            headers={"If-None-Match": '"not-the-etag"', "If-Modified-Since": last_modified},
        )
        assert ignored_date.status == 200
        assert await ignored_date.read() == TEN_BYTES

        ranged = await client.get("/ten.bin", headers={"Range": "bytes=0-1", "If-None-Match": etag})
        assert ranged.status == 304
        await ranged.read()


async def test_conditional_get_honours_if_match_and_if_unmodified_since(root: Path) -> None:
    (root / "ten.bin").write_bytes(TEN_BYTES)
    async with _client(root) as client:
        initial = await client.get("/ten.bin")
        await initial.read()
        etag = initial.headers["ETag"]
        last_modified = initial.headers["Last-Modified"]

        mismatch = await client.get("/ten.bin", headers={"If-Match": '"other"'})
        assert mismatch.status == 412
        assert mismatch.headers["ETag"] == etag
        assert mismatch.headers["Last-Modified"] == last_modified
        assert await mismatch.read() == b"precondition failed"

        star = await client.get("/ten.bin", headers={"If-Match": "*"})
        assert star.status == 200
        assert await star.read() == TEN_BYTES

        # A weak-only validator cannot satisfy If-Match's strong comparison.
        weak = await client.get("/ten.bin", headers={"If-Match": etag})
        if etag.startswith("W/"):
            assert weak.status == 412
            await weak.read()
        else:
            assert weak.status == 200
            await weak.read()

        stale_date = await client.get("/ten.bin", headers={"If-Unmodified-Since": last_modified})
        assert stale_date.status == 200
        assert await stale_date.read() == TEN_BYTES

        old_date = await client.get(
            "/ten.bin", headers={"If-Unmodified-Since": "Mon, 01 Jan 1990 00:00:00 GMT"}
        )
        assert old_date.status == 412
        assert old_date.headers["ETag"] == etag
        assert old_date.headers["Last-Modified"] == last_modified
        await old_date.read()


async def test_if_match_accepts_a_strong_marker_etag(root: Path) -> None:
    target = root / "strong.bin"
    target.write_bytes(b"strong")
    stat = target.stat()
    marker = {
        "version": 2,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "url_sha256": "identity",
        "checksum_algo": "",
        "checksum_hex": "",
        "validator": "strong-etag",
    }
    (root / "strong.bin.downctl-done").write_text(json.dumps(marker), encoding="utf-8")

    async with _client(root) as client:
        matched = await client.get("/strong.bin", headers={"If-Match": '"strong-etag"'})
        assert matched.status == 200
        assert await matched.read() == b"strong"

        mismatch = await client.get("/strong.bin", headers={"If-Match": '"other"'})
        assert mismatch.status == 412
        await mismatch.read()


async def test_directory_without_trailing_slash_redirects_and_links_resolve(root: Path) -> None:
    sub = root / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_text("inside", encoding="utf-8")

    async with _client(root, index=True) as client:
        redirect = await client.get("/sub", allow_redirects=False)
        assert redirect.status == 301
        assert redirect.headers["Location"] == "/sub/"
        await redirect.read()

        with_query = await client.get("/sub?token=x", allow_redirects=False)
        assert with_query.status == 301
        assert with_query.headers["Location"] == "/sub/?token=x"
        await with_query.read()

        listing = await client.get("/sub")
        assert listing.status == 200
        assert str(listing.url).endswith("/sub/")
        body = await listing.text()
        assert 'href="inner.txt"' in body

        linked = await client.get("/sub/inner.txt")
        assert linked.status == 200
        assert await linked.read() == b"inside"


async def test_directory_without_index_is_404(root: Path) -> None:
    async with _client(root) as client:
        response = await client.get("/")
        assert response.status == 404
        await response.read()


async def test_directory_index_escapes_and_quotes_names(root: Path) -> None:
    (root / "<script>.txt").write_text("x", encoding="utf-8")
    (root / "a b.txt").write_text("x", encoding="utf-8")
    (root / "sub").mkdir()

    async with _client(root, index=True) as client:
        response = await client.get("/")
        assert response.status == 200
        body = await response.text()
        assert "&lt;script&gt;.txt" in body
        assert "<script>.txt" not in body
        assert 'href="%3Cscript%3E.txt"' in body
        assert 'href="a%20b.txt"' in body
        assert 'href="sub/"' in body


async def test_auth_missing_is_401_and_reveals_nothing(root: Path) -> None:
    async with _client(root, auth=AUTH) as client:
        for path in ("/data.bin", "/does-not-exist"):
            response = await client.get(path)
            assert response.status == 401
            assert response.headers["WWW-Authenticate"] == 'Basic realm="downctl"'
            await response.read()


async def test_auth_wrong_then_right(root: Path) -> None:
    async with _client(root, auth=AUTH) as client:
        wrong = await client.get("/data.bin", headers=_basic("alice", "wrong"))
        assert wrong.status == 401
        await wrong.read()

        right = await client.get("/data.bin", headers=_basic(*AUTH))
        assert right.status == 200
        assert await right.read() == DATA


async def test_parent_traversal_is_refused(tmp_path: Path, root: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    async with _client(root) as client:
        response = await client.get("/../outside.txt")
        assert response.status in (403, 404)
        assert await response.read() != b"secret"


async def test_url_encoded_traversal_is_refused(tmp_path: Path, root: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    async with _client(root) as client:
        response = await client.get("/..%2foutside.txt")
        assert response.status in (403, 404)
        assert await response.read() != b"secret"


async def test_symlink_outside_root_is_refused_and_not_listed(tmp_path: Path, root: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = root / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform")

    async with _client(root, index=True) as client:
        direct = await client.get("/link.txt")
        assert direct.status in (403, 404)
        assert await direct.read() != b"secret"

        listing = await client.get("/")
        body = await listing.text()
        assert "link.txt" not in body


async def test_request_log_omits_query_and_authorization(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async with _client(root) as client:
        response = await client.get(
            "/data.bin?token=topsecret",
            headers={"Authorization": "Bearer topsecret"},
        )
        assert response.status == 200
        await response.read()

    logged = capsys.readouterr().err
    assert "GET /data.bin" in logged
    assert "token" not in logged
    assert "topsecret" not in logged
    assert "Authorization" not in logged


async def test_single_file_root_is_served_at_slash(tmp_path: Path) -> None:
    target = tmp_path / "model.bin"
    target.write_bytes(DATA)
    async with _client(target) as client:
        response = await client.get("/")
        assert response.status == 200
        assert await response.read() == DATA

        named = await client.get("/model.bin")
        assert named.status == 200
        assert await named.read() == DATA

        missing = await client.get("/other")
        assert missing.status == 404
        await missing.read()


async def test_quiet_suppresses_request_log(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    async with _client(root, quiet=True) as client:
        response = await client.get("/data.bin")
        assert response.status == 200
        await response.read()

    assert "GET /data.bin" not in capsys.readouterr().err


async def test_post_is_method_not_allowed(root: Path) -> None:
    async with _client(root) as client:
        response = await client.post("/data.bin")
        assert response.status == 405
        assert "GET" in response.headers["Allow"]
        await response.read()


async def test_serve_binds_a_real_port_and_cleans_up(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    started = asyncio.Event()
    announcement = ""
    original_result = console.result

    def record(text: str) -> None:
        nonlocal announcement
        announcement = text
        original_result(text)
        started.set()

    # The bound URL is the answer to ``serve``: it must survive ``-q``.
    monkeypatch.setattr(console, "result", record)
    console.set_level(console.QUIET)
    task = asyncio.create_task(
        HTTPFileServer().serve(str(root), None, {"port": 0, "quiet": True})  # type: ignore[arg-type]
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert announcement.startswith("serving ")
        match = re.search(r"http://127\.0\.0\.1:(\d+)/", announcement)
        assert match is not None
        assert announcement in capfd.readouterr().out

        async with aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{match.group(1)}/data.bin"
            async with session.get(url) as response:
                assert response.status == 200
                assert await response.read() == DATA
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        console.set_level(console.NORMAL)


async def test_unknown_option_is_rejected(root: Path) -> None:
    with pytest.raises(DownctlError, match="unknown http server option"):
        await HTTPFileServer().serve(str(root), None, {"bogus": True})  # type: ignore[arg-type]


def test_plugin_contract() -> None:
    server = HTTPFileServer()
    assert server.name == "http"
    assert server.api_version == API_VERSION
    assert server.priority == 0
    assert isinstance(server, Server)
