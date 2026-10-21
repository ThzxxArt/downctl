"""The AuthProvider contract: can_handle, TTL, locking and host matching.

Each test pins a way credential resolution used to lie: a dead ``can_handle``,
a TTL counted from the wrong instant, one lock serializing every host, a host
spelling that did not find its own rule. Failures must never be cached as an
answer; the next call has to retry.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest

from downctl.builtin.auths import EnvAuth
from downctl.config import Config
from downctl.core import credentials as credentials_module
from downctl.core.context import RuntimeContext
from downctl.core.credentials import (
    AuthError,
    AuthMaterial,
    AuthResolver,
    build_rules,
    host_from_input,
)
from downctl.core.errors import DownctlError


class _Provider:
    name = "fake"
    api_version = "1"
    priority = 0

    def __init__(
        self,
        material: AuthMaterial | None = None,
        *,
        accepts: bool = True,
    ) -> None:
        self._material = (
            material if material is not None else AuthMaterial(headers={"X-Token": "value"})
        )
        self.accepts = accepts
        self.handle_calls: list[str] = []
        self.material_calls: list[str] = []

    def can_handle(self, host: str) -> bool:
        self.handle_calls.append(host)
        return self.accepts

    async def material(
        self,
        host: str,
        ctx: RuntimeContext,
        options: Mapping[str, object],
    ) -> AuthMaterial | None:
        self.material_calls.append(host)
        return self._material


class _Registry:
    def __init__(self, provider: object, name: str = "fake") -> None:
        self.provider = provider
        self.name = name

    def get(self, category: str, name: str) -> object | None:
        return self.provider if category == "auth" and name == self.name else None

    def names(self, category: str) -> list[str]:
        return [self.name] if category == "auth" else []


def _ctx() -> RuntimeContext:
    return RuntimeContext(session=object(), config=Config())  # type: ignore[arg-type]


def _resolver(
    provider: object,
    *,
    name: str = "fake",
    patterns: tuple[str, ...] = ("example.test",),
    **options: object,
) -> AuthResolver:
    rules = build_rules({pattern: {"provider": name, **options} for pattern in patterns})
    return AuthResolver(rules, _Registry(provider, name), _ctx())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# can_handle is enforced, not decorative
# --------------------------------------------------------------------------- #
async def test_a_provider_that_declines_a_host_is_not_called() -> None:
    provider = _Provider(accepts=False)
    resolver = _resolver(provider)

    assert await resolver.material_for("https://example.test/x") is None
    assert provider.handle_calls == ["example.test"]
    assert provider.material_calls == []

    # The decline is remembered for the run; a hot request loop must not ask
    # the same question again, and must still not call material.
    assert await resolver.material_for("https://example.test/y") is None
    assert provider.material_calls == []


async def test_a_builtin_provider_materializes_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOWNCTL_RESOLVER_TOKEN", "secret")
    resolver = _resolver(
        EnvAuth(),
        name="env",
        env="DOWNCTL_RESOLVER_TOKEN",
    )

    material = await resolver.material_for("https://example.test/x")
    assert material is not None
    assert material.headers == {"Authorization": "Bearer secret"}


async def test_a_provider_without_can_handle_is_treated_as_accepting() -> None:
    """A hand-built registry may hold a provider predating the check; absence
    means "no opinion", never "deny every configured credential"."""

    class _Legacy:
        name = "fake"
        api_version = "1"
        priority = 0

        async def material(
            self,
            host: str,
            ctx: RuntimeContext,
            options: Mapping[str, object],
        ) -> AuthMaterial | None:
            return AuthMaterial(headers={"X-Token": "legacy"})

    resolver = _resolver(_Legacy())
    material = await resolver.material_for("https://example.test/x")
    assert material is not None
    assert material.headers == {"X-Token": "legacy"}


# --------------------------------------------------------------------------- #
# freshness: TTL is measured from materialization
# --------------------------------------------------------------------------- #
async def test_ttl_starts_when_the_material_is_produced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(credentials_module.time, "monotonic", lambda: clock["now"])

    class _Slow(_Provider):
        async def material(
            self,
            host: str,
            ctx: RuntimeContext,
            options: Mapping[str, object],
        ) -> AuthMaterial | None:
            self.material_calls.append(host)
            clock["now"] += 30.0  # material appears 30s into a 10s TTL
            return self._material

    provider = _Slow()
    resolver = _resolver(provider, ttl=10)

    first = await resolver.material_for("https://example.test/a")
    assert first is not None
    clock["now"] += 1.0  # 1s after material was produced, 31s after the call began
    second = await resolver.material_for("https://example.test/b")

    assert second is first
    assert provider.material_calls == ["example.test"]


async def test_ttl_still_expires_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(credentials_module.time, "monotonic", lambda: clock["now"])
    provider = _Provider()
    resolver = _resolver(provider, ttl=10)

    assert await resolver.material_for("https://example.test/a") is not None
    clock["now"] += 11.0
    assert await resolver.material_for("https://example.test/a") is not None

    assert provider.material_calls == ["example.test", "example.test"]


# --------------------------------------------------------------------------- #
# one slow host must not serialize the others
# --------------------------------------------------------------------------- #
async def test_a_slow_host_does_not_block_another_host() -> None:
    class _Blocking(_Provider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def material(
            self,
            host: str,
            ctx: RuntimeContext,
            options: Mapping[str, object],
        ) -> AuthMaterial | None:
            self.material_calls.append(host)
            if host == "a.example":
                self.started.set()
                await self.release.wait()
            return AuthMaterial(headers={"X-Host": host})

    provider = _Blocking()
    resolver = _resolver(provider, patterns=("a.example", "b.example"))
    slow = asyncio.create_task(resolver.material_for("https://a.example/x"))
    try:
        await asyncio.wait_for(provider.started.wait(), 1)
        fast = await asyncio.wait_for(resolver.material_for("https://b.example/x"), 1)

        assert fast is not None
        assert fast.headers == {"X-Host": "b.example"}
        assert provider.material_calls == ["a.example", "b.example"]
    finally:
        provider.release.set()
        await asyncio.wait_for(slow, 1)


# --------------------------------------------------------------------------- #
# failures are retried, not cached
# --------------------------------------------------------------------------- #
async def test_a_failed_materialization_is_retried() -> None:
    class _Flaky(_Provider):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def material(
            self,
            host: str,
            ctx: RuntimeContext,
            options: Mapping[str, object],
        ) -> AuthMaterial | None:
            self.attempts += 1
            if self.attempts == 1:
                raise AuthError("temporary failure")
            return self._material

    provider = _Flaky()
    resolver = _resolver(provider, ttl=300)

    with pytest.raises(AuthError, match="temporary failure"):
        await resolver.material_for("https://example.test/a")

    material = await resolver.material_for("https://example.test/a")
    assert material is not None
    assert provider.attempts == 2, "a failure was cached instead of retried"


async def test_a_can_handle_failure_is_retried() -> None:
    class _FlakyCheck(_Provider):
        def __init__(self) -> None:
            super().__init__()
            self.checks = 0

        def can_handle(self, host: str) -> bool:
            self.checks += 1
            if self.checks == 1:
                raise RuntimeError("host logic exploded")
            return True

    provider = _FlakyCheck()
    resolver = _resolver(provider)

    with pytest.raises(AuthError, match="can_handle"):
        await resolver.material_for("https://example.test/a")

    material = await resolver.material_for("https://example.test/a")
    assert material is not None
    assert provider.checks == 2


# --------------------------------------------------------------------------- #
# host spellings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("huggingface.co", "huggingface.co"),
        ("huggingface.co:8443", "huggingface.co"),
        ("user:pass@huggingface.co:8443", "huggingface.co"),
        ("https://HuggingFace.co/a", "huggingface.co"),
        ("https://user:pass@huggingface.co:8443/a", "huggingface.co"),
        ("https://bücher.example/a", "xn--bcher-kva.example"),
        ("https://xn--bcher-kva.example/a", "xn--bcher-kva.example"),
        ("https://[::1]:8443/a", "::1"),
        ("", None),
        ("https:///nohost", None),
    ],
)
def test_host_from_input_accepts_urls_and_bare_hosts(value: str, expected: str | None) -> None:
    assert host_from_input(value) == expected


async def test_a_bare_host_finds_the_same_rule_as_its_url() -> None:
    provider = _Provider()
    resolver = _resolver(provider)

    material = await resolver.material_for("example.test")

    assert material is not None
    assert provider.handle_calls == ["example.test"]
    assert provider.material_calls == ["example.test"]


def test_rule_lookup_accepts_a_url_bare_host_and_port() -> None:
    resolver = _resolver(_Provider())

    assert resolver.rule_for("example.test") is not None
    assert resolver.rule_for("example.test:8443") is not None
    assert resolver.rule_for("https://user:pass@example.test:8443/x") is not None
    assert resolver.rule_for("other.test") is None


def test_rule_matching_accepts_the_same_spellings() -> None:
    rule = build_rules({"example.test": {"provider": "fake"}})[0]

    assert rule.matches("example.test")
    assert rule.matches("EXAMPLE.TEST")
    assert rule.matches("example.test:8443")
    assert rule.matches("https://user:pass@example.test:8443/x")
    assert not rule.matches("other.test")


async def test_a_port_and_userinfo_are_not_part_of_the_auth_host() -> None:
    provider = _Provider()
    resolver = _resolver(provider)

    assert await resolver.material_for("https://user:secret@example.test:8443/x") is not None
    assert provider.handle_calls == ["example.test"]
    assert provider.material_calls == ["example.test"]


async def test_unicode_and_punycode_spellings_share_one_rule() -> None:
    provider = _Provider()
    unicode_rule = AuthResolver(
        build_rules({"bücher.example": {"provider": "fake"}}),
        _Registry(provider),  # type: ignore[arg-type]
        _ctx(),
    )

    assert await unicode_rule.material_for("https://xn--bcher-kva.example/x") is not None
    assert provider.material_calls == ["xn--bcher-kva.example"]

    punycode_rule = AuthResolver(
        build_rules({"xn--bcher-kva.example": {"provider": "fake"}}),
        _Registry(provider),  # type: ignore[arg-type]
        _ctx(),
    )
    assert await punycode_rule.material_for("https://bücher.example/x") is not None
    assert provider.material_calls == ["xn--bcher-kva.example", "xn--bcher-kva.example"]


def test_two_spellings_of_one_host_are_a_duplicate_rule() -> None:
    with pytest.raises(DownctlError, match="more than once"):
        build_rules(
            {
                "bücher.example": {"provider": "fake"},
                "xn--bcher-kva.example": {"provider": "fake"},
            }
        )


async def test_auth_check_accepts_a_bare_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``downctl auth check huggingface.co`` must not turn the host into "no host"."""
    from downctl import app
    from downctl.cli import build_parser

    config = tmp_path / "config.toml"
    config.write_text(
        '[auth."example.test"]\n'
        'provider = "env"\n'
        'env = "DOWNCTL_BARE_HOST_TOKEN"\n'
        'header = "X-Api-Key"\n'
        'prefix = ""\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOWNCTL_CONFIG", str(config))
    monkeypatch.setenv("DOWNCTL_BARE_HOST_TOKEN", "SECRET-VALUE")
    args = build_parser().parse_args(["auth", "check", "example.test"])

    assert await asyncio.to_thread(app.cmd_auth, args) == 0

    out = capsys.readouterr().out
    assert "host      example.test" in out
    assert "X-Api-Key" in out
    assert "SECRET-VALUE" not in out
