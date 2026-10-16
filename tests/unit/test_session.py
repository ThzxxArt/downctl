"""Tests for the HTTP session factory."""

from __future__ import annotations

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
        assert session.headers["Authorization"].startswith("Basic ")
