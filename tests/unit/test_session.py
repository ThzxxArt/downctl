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
