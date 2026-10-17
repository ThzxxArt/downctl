"""Integration tests for probe helpers (HEAD, ranged fallback, errors)."""

from __future__ import annotations

import aiohttp
import pytest_asyncio

from downctl.probe import content_length, filename_from_url, probe_resource, probe_size


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


async def test_probe_size_via_head(file_server, session) -> None:
    size, status = await probe_size(session, f"{file_server.base_url}/file")
    assert size == len(file_server.data)
    assert status == 200


async def test_probe_resource_head(file_server, session) -> None:
    detail = await probe_resource(session, f"{file_server.base_url}/file")
    assert detail["reachable"] is True
    assert detail["size"] == len(file_server.data)
    assert detail["filename"] == "file"


def test_content_length_parsing() -> None:
    assert content_length({"Content-Length": "42"}) == 42
    assert content_length({"Content-Length": "not-a-number"}) == 0
    assert content_length({}) == 0


def test_filename_from_url() -> None:
    assert filename_from_url("https://h/a/b/model.bin?x=1") == "model.bin"
    assert filename_from_url("https://h/a/my%20file.bin") == "my file.bin"
    assert filename_from_url("https://h/") == ""
