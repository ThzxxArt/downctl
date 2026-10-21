"""Credential placement: providers supply material, the request decides the hop.

0.1.5 established that a credential belongs to an origin. 0.1.6 added *where it
comes from* (``AuthProvider``) without adding a second owner: these tests pin
that the provider never sees a hop decision, and that every user header —
not just ``Authorization`` — is dropped when a redirect leaves the origin.
"""

from __future__ import annotations

from typing import Any

import pytest

from downctl.config import Config
from downctl.core.context import RuntimeContext
from downctl.core.credentials import AuthError, AuthMaterial, AuthResolver, build_rules
from downctl.session import AUTH_RESOLVER_ATTR, policy_request


class _Response:
    def __init__(self, status: int, location: str | None = None) -> None:
        self.status = status
        self.headers: dict[str, str] = {"Location": location} if location else {}


class _Manager:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _RecordingSession:
    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.seen: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, **kwargs: object) -> _Manager:
        headers = kwargs.get("headers") or {}
        assert isinstance(headers, dict)
        self.seen.append((url, {str(k): str(v) for k, v in headers.items()}))
        return _Manager(self.responses[url])

    def head(self, url: str, **kwargs: object) -> _Manager:
        return self.get(url, **kwargs)

    def request(self, _method: str, url: str, **kwargs: object) -> _Manager:
        return self.get(url, **kwargs)


class _Provider:
    name = "fake"
    api_version = "1"
    priority = 0

    def __init__(
        self, material: AuthMaterial | None = None, error: Exception | None = None
    ) -> None:
        self.calls = 0
        self._material = material
        self._error = error

    def can_handle(self, host: str) -> bool:
        return True

    async def material(
        self, host: str, ctx: RuntimeContext, options: dict[str, object]
    ) -> AuthMaterial | None:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._material


def _resolver(provider: object, pattern: str = "example.test", **options: object) -> AuthResolver:
    ctx = RuntimeContext(session=object(), config=Config())  # type: ignore[arg-type]
    registry = _RegistryWithAuth(provider)
    rules = build_rules({pattern: {"provider": "fake", **options}})
    return AuthResolver(rules, registry, ctx)  # type: ignore[arg-type]


class _RegistryWithAuth:
    def __init__(self, provider: object) -> None:
        self._provider = provider

    def get(self, category: str, name: str) -> object | None:
        return self._provider if category == "auth" and name == "fake" else None

    def names(self, category: str) -> list[str]:
        return ["fake"] if category == "auth" else []


async def test_every_user_header_is_dropped_when_a_redirect_leaves_the_origin() -> None:
    """``-H X-Api-Key`` used to follow a 302 to another host.

    Only ``Authorization``/``Cookie`` were origin-bound, so an API key sent as a
    custom header reached whatever host the server redirected to.
    """
    session = _RecordingSession(
        {
            "https://example.test/file": _Response(302, "https://cdn.test/file"),
            "https://cdn.test/file": _Response(200),
        }
    )
    from downctl.session import ORIGIN_HEADERS_ATTR

    setattr(session, ORIGIN_HEADERS_ATTR, {"X-Api-Key": "SECRET", "X-Trace": "t"})
    async with policy_request(session, "GET", "https://example.test/file"):
        pass

    first = session.seen[0][1]
    second = session.seen[1][1]
    assert first["X-Api-Key"] == "SECRET"
    assert second.get("X-Api-Key") is None
    assert second.get("X-Trace") is None


async def test_provider_material_is_attached_to_the_first_hop_only() -> None:
    provider = _Provider(
        AuthMaterial(headers={"Authorization": "Bearer FROM-PROVIDER"}, source="fake")
    )
    resolver = _resolver(provider)
    session = _RecordingSession(
        {
            "https://example.test/file": _Response(302, "https://cdn.test/file"),
            "https://cdn.test/file": _Response(200),
        }
    )
    setattr(session, AUTH_RESOLVER_ATTR, resolver)

    async with policy_request(session, "GET", "https://example.test/file"):
        pass

    assert session.seen[0][1]["Authorization"] == "Bearer FROM-PROVIDER"
    assert "Authorization" not in session.seen[1][1]


async def test_an_explicit_header_wins_over_the_provider() -> None:
    provider = _Provider(AuthMaterial(headers={"Authorization": "Bearer PROVIDER"}))
    resolver = _resolver(provider)
    session = _RecordingSession({"https://example.test/file": _Response(200)})
    from downctl.session import ORIGIN_HEADERS_ATTR

    setattr(session, ORIGIN_HEADERS_ATTR, {"Authorization": "Bearer EXPLICIT"})
    setattr(session, AUTH_RESOLVER_ATTR, resolver)

    async with policy_request(session, "GET", "https://example.test/file"):
        pass

    assert session.seen[0][1]["Authorization"] == "Bearer EXPLICIT"


async def test_the_resolver_caches_when_ttl_is_zero() -> None:
    provider = _Provider(AuthMaterial(headers={"X-Token": "v"}))
    resolver = _resolver(provider, ttl=0)

    first = await resolver.material_for("https://example.test/a")
    second = await resolver.material_for("https://example.test/b")
    assert first is not None and first.headers == {"X-Token": "v"}
    assert second is not None
    assert provider.calls == 1


async def test_a_provider_failure_names_the_rule_and_never_the_value() -> None:
    provider = _Provider(error=RuntimeError("boom at https://h/x?token=SECRET"))
    resolver = _resolver(provider)

    with pytest.raises(AuthError) as caught:
        await resolver.material_for("https://example.test/a")
    message = str(caught.value)
    assert '[auth."example.test"]' in message
    assert "SECRET" not in message
    assert "token=***" in message


def test_rules_match_exact_hosts_and_the_longest_suffix() -> None:
    rules = build_rules(
        {
            "*.corp.example": {"provider": "fake"},
            "a.corp.example": {"provider": "fake"},
            "example.test": {"provider": "fake"},
        }
    )
    assert rules[0].pattern == "a.corp.example"  # exact beats wildcard
    assert rules[-1].pattern == "*.corp.example"

    resolver = _resolver(_Provider())
    wildcard = AuthResolver(
        build_rules({"*.corp.example": {"provider": "fake"}}),
        _RegistryWithAuth(_Provider()),  # type: ignore[arg-type]
        RuntimeContext(session=object(), config=Config()),  # type: ignore[arg-type]
    )
    assert resolver.rule_for("example.test") is not None
    assert wildcard.rule_for("build.corp.example") is not None
    assert wildcard.rule_for("corp.example") is None  # the apex is not a subdomain
    assert wildcard.rule_for("elsewhere.test") is None


def test_a_duplicate_rule_is_refused() -> None:
    from downctl.core.errors import DownctlError

    with pytest.raises(DownctlError, match="more than once"):
        # TOML cannot express a duplicate key, but the merged mapping can after
        # an include/write cycle; refusing is what keeps "the" rule answerable.
        rules_section: dict[str, dict[str, Any]] = {
            "example.test": {"provider": "fake"},
            "EXAMPLE.TEST": {"provider": "fake"},
        }
        build_rules(rules_section)
