"""Tests for the HTTP session factory."""

from __future__ import annotations

import base64

import aiohttp
import pytest

from downctl.session import make_session


async def test_default_user_agent() -> None:
    async with make_session() as session:
        assert session.headers["User-Agent"].startswith("downctl/")


async def test_custom_user_agent_and_token() -> None:
    async with make_session(token="secret", user_agent="custom/1") as session:
        assert session.headers["User-Agent"] == "custom/1"
        assert session.headers["Authorization"] == "Bearer secret"


async def test_extra_headers_and_basic_auth() -> None:
    async with make_session(headers={"X-Trace": "abc"}, auth=("user", "pass")) as session:
        assert session.headers["X-Trace"] == "abc"
        expected = base64.b64encode(b"user:pass").decode("ascii")
        assert session.headers["Authorization"] == f"Basic {expected}"


async def test_basic_auth_does_not_need_a_version_specific_aiohttp_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # aiohttp only exposed ``encode_basic_auth`` at the top level in some
    # releases; downctl must not depend on that accident.
    monkeypatch.delattr(aiohttp, "encode_basic_auth", raising=False)
    async with make_session(auth=("user", "pass")) as session:
        expected = base64.b64encode(b"user:pass").decode("ascii")
        assert session.headers["Authorization"] == f"Basic {expected}"


async def test_basic_auth_uses_utf8_like_aiohttp() -> None:
    async with make_session(auth=("用户", "密码")) as session:
        expected = base64.b64encode("用户:密码".encode()).decode("ascii")
        assert session.headers["Authorization"] == f"Basic {expected}"


def test_basic_auth_rejects_a_colon_in_the_login() -> None:
    with pytest.raises(ValueError, match="not allowed in login"):
        make_session(auth=("user:name", "password"))


async def test_proxy_is_passed_to_the_session() -> None:
    # ``ClientSession(proxy=...)`` requires aiohttp >= 3.11 (the declared floor).
    async with make_session(proxy="http://127.0.0.1:1") as session:
        assert session._default_proxy == "http://127.0.0.1:1"  # noqa: SLF001
