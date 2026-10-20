"""Tests for the HTTP session factory."""

from __future__ import annotations

import base64

import aiohttp
import pytest

from downctl.session import (
    PROXY_ALL,
    PROXY_EXTERNAL,
    ProxyPolicy,
    make_session,
    policy_request,
)


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


# --------------------------------------------------------------------------- #
# a redirect is a chain of targets, and the policy is about the target
# --------------------------------------------------------------------------- #
class _Response:
    def __init__(self, status: int, location: str | None = None) -> None:
        self.status = status
        self.headers: dict[str, str] = {"Location": location} if location else {}


class _ContextManager:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _RecordingSession:
    """A session that records the proxy each hop was asked to use."""

    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str | None, bool | None]] = []

    def get(self, url: str, **kwargs: object) -> _ContextManager:
        self.calls.append(
            (url, kwargs.get("proxy"), kwargs.get("allow_redirects"))  # type: ignore[arg-type]
        )
        return _ContextManager(self.responses[url])


async def test_a_redirect_is_decided_per_hop_not_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """aiohttp applies one ``proxy=`` to a whole chain; the policy is per target.

    A guessed WSL host proxy must not carry an external host's redirect to
    ``127.0.0.1``: no request may reach the proxy asking it to connect to its
    own loopback.
    """
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    policy = ProxyPolicy("http://10.255.255.254:7897", PROXY_EXTERNAL)
    session = _RecordingSession(
        {
            "https://example.test/file": _Response(302, "http://127.0.0.1:9000/secret"),
            "http://127.0.0.1:9000/secret": _Response(200),
        }
    )

    async with policy_request(  # type: ignore[arg-type]
        session, "GET", "https://example.test/file", policy=policy
    ) as response:
        assert response.status == 200

    assert session.calls == [
        ("https://example.test/file", "http://10.255.255.254:7897", False),
        ("http://127.0.0.1:9000/secret", None, False),
    ]


async def test_a_cross_origin_redirect_drops_the_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    policy = ProxyPolicy("http://10.255.255.254:7897", PROXY_EXTERNAL)
    session = _RecordingSession(
        {
            "https://example.test/file": _Response(302, "https://cdn.test/file"),
            "https://cdn.test/file": _Response(200),
        }
    )
    seen: dict[str, str] = {}

    original = session.get

    def recording(url: str, **kwargs: object) -> _ContextManager:
        seen[url] = str((kwargs.get("headers") or {}).get("Authorization", ""))
        return original(url, **kwargs)

    session.get = recording  # type: ignore[method-assign]
    async with policy_request(  # type: ignore[arg-type]
        session,
        "GET",
        "https://example.test/file",
        policy=policy,
        headers={"Authorization": "Bearer secret"},
    ) as response:
        assert response.status == 200

    assert seen["https://example.test/file"] == "Bearer secret"
    assert seen["https://cdn.test/file"] == ""


async def test_without_a_policy_the_single_hop_behaviour_is_unchanged() -> None:
    session = _RecordingSession({"https://example.test/f": _Response(200)})

    async with policy_request(  # type: ignore[arg-type]
        session, "GET", "https://example.test/f", policy=None
    ) as response:
        assert response.status == 200

    assert session.calls == [("https://example.test/f", None, True)]
