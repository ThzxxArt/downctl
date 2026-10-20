"""Tests for the HTTP session factory."""

from __future__ import annotations

import base64

import aiohttp
import pytest

from downctl.session import PROXY_ALL, PROXY_EXTERNAL, ProxyPolicy, make_session


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


async def test_the_session_carries_no_default_proxy() -> None:
    """A session-wide proxy cannot be exempted for one target, so there is none.

    aiohttp applies ``ClientSession(proxy=...)`` to every request; the policy
    answers per target instead, at the request site.
    """
    async with make_session() as session:
        assert session._default_proxy is None  # noqa: SLF001


def test_a_proxy_with_the_default_policy_applies_to_every_target() -> None:
    policy = ProxyPolicy("http://10.0.0.2:3128")
    assert policy.scope == PROXY_ALL
    assert policy.proxy_for("https://example.com/file.bin") == "http://10.0.0.2:3128"
    assert policy.proxy_for("http://127.0.0.1:8000/file.bin") == "http://10.0.0.2:3128"


def test_a_guessed_proxy_does_not_carry_a_request_for_this_machine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("no_proxy", raising=False)
    policy = ProxyPolicy("http://10.255.255.254:7897", PROXY_EXTERNAL)

    assert policy.proxy_for("http://127.0.0.1:8080/file.bin") is None
    assert policy.proxy_for("http://127.1.2.3:8080/file.bin") is None
    assert policy.proxy_for("http://localhost:8080/file.bin") is None
    assert policy.proxy_for("http://files.localhost/file.bin") is None
    assert policy.proxy_for("http://[::1]:8080/file.bin") is None
    assert policy.proxy_for("http://169.254.10.1/file.bin") is None
    # Anything off this machine still uses the guess.
    assert policy.proxy_for("https://huggingface.co/x") == "http://10.255.255.254:7897"


def test_a_guessed_proxy_honours_an_explicit_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("no_proxy", "internal.example,.corp.example")
    policy = ProxyPolicy("http://10.255.255.254:7897", PROXY_EXTERNAL)

    assert policy.proxy_for("https://internal.example/x") is None
    assert policy.proxy_for("https://build.corp.example/x") is None
    assert policy.proxy_for("https://huggingface.co/x") == "http://10.255.255.254:7897"


def test_no_proxy_does_not_override_an_explicit_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-p`` is the more specific instruction, so it wins over ``no_proxy``."""
    monkeypatch.setenv("no_proxy", "internal.example")
    policy = ProxyPolicy("http://10.0.0.2:3128", PROXY_ALL)

    assert policy.proxy_for("https://internal.example/x") == "http://10.0.0.2:3128"
