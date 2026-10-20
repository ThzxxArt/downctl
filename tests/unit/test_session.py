"""Tests for the HTTP session factory."""

from __future__ import annotations

import base64

import aiohttp
import pytest

from downctl.session import (
    ORIGIN_HEADERS_ATTR,
    PROXY_ALL,
    PROXY_EXTERNAL,
    ProxyPolicy,
    make_session,
    origin_headers,
    policy_request,
)


async def test_default_user_agent() -> None:
    async with make_session() as session:
        assert session.headers["User-Agent"].startswith("downctl/")


async def test_custom_user_agent_and_token() -> None:
    async with make_session(token="secret", user_agent="custom/1") as session:
        assert session.headers["User-Agent"] == "custom/1"
        # A credential is bound to the origin that issued it, so it is not a
        # session default: those are applied to every hop of a redirect chain.
        assert "Authorization" not in session.headers
        assert origin_headers(session) == {"Authorization": "Bearer secret"}


async def test_extra_headers_and_basic_auth() -> None:
    async with make_session(headers={"X-Trace": "abc"}, auth=("user", "pass")) as session:
        assert session.headers["X-Trace"] == "abc"
        expected = base64.b64encode(b"user:pass").decode("ascii")
        assert origin_headers(session)["Authorization"] == f"Basic {expected}"


async def test_basic_auth_does_not_need_a_version_specific_aiohttp_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # aiohttp only exposed ``encode_basic_auth`` at the top level in some
    # releases; downctl must not depend on that accident.
    monkeypatch.delattr(aiohttp, "encode_basic_auth", raising=False)
    async with make_session(auth=("user", "pass")) as session:
        expected = base64.b64encode(b"user:pass").decode("ascii")
        assert origin_headers(session)["Authorization"] == f"Basic {expected}"


async def test_basic_auth_uses_utf8_like_aiohttp() -> None:
    async with make_session(auth=("用户", "密码")) as session:
        expected = base64.b64encode("用户:密码".encode()).decode("ascii")
        assert origin_headers(session)["Authorization"] == f"Basic {expected}"


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


# --------------------------------------------------------------------------- #
# credentials are bound to an origin, and a redirect can leave it
# --------------------------------------------------------------------------- #
def _session_with_credentials(
    responses: dict[str, _Response], credentials: dict[str, str]
) -> _RecordingSession:
    session = _RecordingSession(responses)
    setattr(session, ORIGIN_HEADERS_ATTR, dict(credentials))
    return session


def _record_authorization(session: _RecordingSession) -> dict[str, str]:
    seen: dict[str, str] = {}
    original = session.get

    def recording(url: str, **kwargs: object) -> _ContextManager:
        seen[url] = str((kwargs.get("headers") or {}).get("Authorization", ""))
        return original(url, **kwargs)

    session.get = recording  # type: ignore[method-assign]
    return seen


async def test_the_session_does_not_read_the_proxy_environment() -> None:
    """aiohttp re-reads ``http_proxy`` whenever ``proxy`` is ``None``.

    That silently undid a policy answer of "connect directly": with
    ``HTTP_PROXY`` exported, a loopback request the policy had exempted went to
    the proxy anyway. The environment is not the decision — ``_build_config``
    already folded it into the policy, so reading it twice overrode the answer.
    """
    async with make_session() as session:
        assert session.trust_env is False


async def test_configured_credentials_reach_the_origin_they_belong_to() -> None:
    policy = ProxyPolicy("http://10.255.255.254:7897", PROXY_EXTERNAL)
    session = _session_with_credentials(
        {"https://example.test/file": _Response(200)},
        {"Authorization": "Bearer configured"},
    )
    seen = _record_authorization(session)

    async with policy_request(  # type: ignore[arg-type]
        session, "GET", "https://example.test/file", policy=policy
    ):
        pass

    assert seen["https://example.test/file"] == "Bearer configured"


async def test_a_same_origin_redirect_keeps_the_configured_credentials() -> None:
    policy = ProxyPolicy("http://10.255.255.254:7897", PROXY_EXTERNAL)
    session = _session_with_credentials(
        {
            "https://example.test/file": _Response(302, "https://example.test/other"),
            "https://example.test/other": _Response(200),
        },
        {"Authorization": "Bearer configured"},
    )
    seen = _record_authorization(session)

    async with policy_request(  # type: ignore[arg-type]
        session, "GET", "https://example.test/file", policy=policy
    ):
        pass

    assert seen["https://example.test/other"] == "Bearer configured"


async def test_a_cross_origin_redirect_drops_the_configured_credentials() -> None:
    """The session-level credential is the one aiohttp would replay by default.

    Session headers apply to every request, including the one a redirect turns
    into, so a ``--user`` credential configured for one host used to follow the
    chain to another.
    """
    policy = ProxyPolicy("http://10.255.255.254:7897", PROXY_EXTERNAL)
    session = _session_with_credentials(
        {
            "https://example.test/file": _Response(302, "https://cdn.test/file"),
            "https://cdn.test/file": _Response(200),
        },
        {"Authorization": "Bearer configured", "Cookie": "sid=1"},
    )
    seen = _record_authorization(session)

    async with policy_request(  # type: ignore[arg-type]
        session, "GET", "https://example.test/file", policy=policy
    ):
        pass

    assert seen["https://example.test/file"] == "Bearer configured"
    assert seen["https://cdn.test/file"] == ""


async def test_a_scheme_change_is_an_origin_change() -> None:
    """Same host and port is not the same origin when the scheme differs."""
    policy = ProxyPolicy("http://10.255.255.254:7897", PROXY_EXTERNAL)
    session = _session_with_credentials(
        {
            "https://example.test/file": _Response(302, "http://example.test/file"),
            "http://example.test/file": _Response(200),
        },
        {"Authorization": "Bearer configured"},
    )
    seen = _record_authorization(session)

    async with policy_request(  # type: ignore[arg-type]
        session, "GET", "https://example.test/file", policy=policy
    ):
        pass

    assert seen["http://example.test/file"] == ""


async def test_a_non_get_request_is_bound_to_its_method() -> None:
    """``session.request`` is an unbound function; calling it broke the POST path."""

    class _PostSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def request(self, method: str, url: str, **_kwargs: object) -> _ContextManager:
            self.calls.append((method, url))
            return _ContextManager(_Response(200))

    session = _PostSession()
    async with policy_request(session, "post", "https://example.test/x") as response:  # type: ignore[arg-type]
        assert response.status == 200

    assert session.calls == [("POST", "https://example.test/x")]


async def test_credentials_are_watched_even_without_a_proxy() -> None:
    """The chain is walked whenever a credential could leave its origin.

    aiohttp's own redirect handling compares *hosts*; a credential belongs to a
    full origin, so ``https://h`` → ``http://h`` has to drop it too. Letting
    aiohttp follow the chain would keep it.
    """
    session = _session_with_credentials(
        {
            "https://example.test/file": _Response(302, "http://example.test/file"),
            "http://example.test/file": _Response(200),
        },
        {"Authorization": "Bearer configured"},
    )
    seen = _record_authorization(session)

    async with policy_request(session, "GET", "https://example.test/file"):  # type: ignore[arg-type]
        pass

    assert seen["https://example.test/file"] == "Bearer configured"
    assert seen["http://example.test/file"] == ""
