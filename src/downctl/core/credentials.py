"""Where a credential for a *host* comes from — the ``AuthProvider`` stage.

0.1.5 fixed *who may receive* a credential: ``policy_request`` binds the
headers configured for a run to the origin that issued them and drops them when
a redirect leaves it. What was still missing is the other half of the same
question: where the material comes from. Every source invented its own answer
(a ``[plugins.*] token`` key or an environment variable), so ``~/.netrc``, a
password-manager command and per-host configuration had nowhere to live.

The split is deliberate and is the one rule this module exists to keep:

* an :class:`AuthProvider` answers **"what material does this host get?"**;
* :func:`downctl.session.policy_request` answers **"which hop may carry it?"**.

Neither answer is derived in two places. A provider that also decided which
hosts receive its material would be a second owner of the 0.1.5 rule, and the
first redirect would show the two disagreeing.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from downctl.core.errors import DownctlError

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext
    from downctl.plugin.registry import Registry


class AuthError(DownctlError):
    """A configured credential source could not produce material.

    The message names the rule and the provider and never contains the
    material: an error is the most likely place for a secret to be printed by
    accident, so the type itself carries the rule that it must not be.
    """


@dataclass(frozen=True)
class AuthMaterial:
    """What a provider hands back: headers and/or HTTP Basic credentials."""

    headers: Mapping[str, str] = field(default_factory=dict)
    basic: tuple[str, str] | None = None
    #: Where the material came from (``env HF_TOKEN``, ``command``, ``netrc``),
    #: for ``auth check`` and ``doctor``. Never the value itself.
    source: str = ""

    def header_items(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.headers.items())

    def describe(self) -> str:
        """One line naming the header(s) and the source, never the value."""
        names = ", ".join(sorted(self.headers)) or ("basic" if self.basic else "none")
        return f"{names} (from {self.source or 'unknown'})"


@dataclass(frozen=True)
class AuthRule:
    """One ``[auth."<host>"]`` table, already validated."""

    #: The host pattern as written: ``example.com`` or ``*.example.com``.
    pattern: str
    provider: str
    options: Mapping[str, object] = field(default_factory=dict)

    @property
    def wildcard(self) -> bool:
        return self.pattern.startswith("*.")

    def matches(self, host: str) -> bool:
        host = host.lower().strip("[]")
        pattern = self.pattern.lower()
        if self.wildcard:
            suffix = pattern[2:]
            return bool(suffix) and host.endswith("." + suffix) and host != suffix
        return host == pattern

    def specificity(self) -> int:
        """Longer (more specific) patterns win; exact beats a wildcard of equal length."""
        return len(self.pattern) + (0 if self.wildcard else 1000)


def _rule_from_table(pattern: str, table: object) -> AuthRule:
    if not isinstance(table, dict):
        raise DownctlError(f'[auth."{pattern}"] must be a table')
    provider = table.get("provider")
    if not isinstance(provider, str) or not provider:
        raise DownctlError(f'[auth."{pattern}"] needs a non-empty "provider"')
    options = {str(key): value for key, value in table.items() if key != "provider"}
    return AuthRule(pattern=pattern.strip(), provider=provider, options=options)


def build_rules(section: Mapping[str, object]) -> list[AuthRule]:
    """Validate the ``[auth.*]`` section into rules, refusing duplicates.

    A duplicate pattern is an error rather than "last one wins": two tables
    cannot both be the answer for the same host, and silently choosing one
    makes ``auth check`` and the run disagree depending on load order.
    """
    rules: list[AuthRule] = []
    seen: set[str] = set()
    for pattern, table in section.items():
        key = str(pattern).strip().lower()
        if key in seen:
            raise DownctlError(f'[auth."{pattern}"] is defined more than once')
        seen.add(key)
        rules.append(_rule_from_table(str(pattern), table))
    # Most specific first, so the first match is the winner.
    rules.sort(key=lambda rule: rule.specificity(), reverse=True)
    return rules


class AuthResolver:
    """Turns a URL into the material its host is entitled to.

    Resolution is cached per ``(origin, rule)``. ``ttl`` (seconds, default
    ``0``) controls freshness: ``0`` means "resolve once for the run", which is
    what an environment variable or a file means; a command-backed provider can
    set ``ttl`` so a short-lived token is renewed instead of reused until it
    expires mid-download.
    """

    def __init__(
        self,
        rules: list[AuthRule],
        registry: Registry,
        ctx: RuntimeContext,
    ) -> None:
        self._rules = rules
        self._registry = registry
        self._ctx = ctx
        self._cache: dict[tuple[str, str, str], tuple[float | None, AuthMaterial | None]] = {}
        self._lock = asyncio.Lock()

    def __bool__(self) -> bool:
        return bool(self._rules)

    @property
    def rules(self) -> list[AuthRule]:
        return list(self._rules)

    def rule_for(self, host: str) -> AuthRule | None:
        for rule in self._rules:
            if rule.matches(host):
                return rule
        return None

    def provider_for(self, rule: AuthRule) -> object | None:
        return self._registry.get("auth", rule.provider)

    async def material_for(self, url: str) -> AuthMaterial | None:
        """Resolve the material for ``url``'s host, or ``None``."""
        from urllib.parse import urlsplit

        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
        except ValueError:
            return None
        if not host:
            return None
        rule = self.rule_for(host)
        if rule is None:
            return None
        provider = self.provider_for(rule)
        if provider is None:
            raise AuthError(
                f'[auth."{rule.pattern}"] names unknown auth provider {rule.provider!r}; '
                f"available: {', '.join(self._registry.names('auth')) or 'none'}"
            )
        origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
        cache_key = (origin, rule.pattern, rule.provider)
        ttl = _ttl(rule)
        # One provider call at a time. Probe and piece requests run
        # concurrently, and without this a ``command`` provider was executed
        # once per in-flight request before any of them filled the cache.
        async with self._lock:
            now = time.monotonic()
            cached = self._cache.get(cache_key)
            if cached is not None:
                expires, cached_material = cached
                if expires is None or expires > now:
                    return cached_material
            try:
                material = await provider.material(host, self._ctx, rule.options)  # type: ignore[attr-defined]
            except AuthError:
                raise
            except Exception as exc:  # noqa: BLE001 - text is redacted and re-raised
                from downctl.urls import redact_text

                raise AuthError(
                    f'[auth."{rule.pattern}"]: provider {rule.provider!r} failed: '
                    f"{redact_text(str(exc))}"
                ) from exc
            if material is not None and not isinstance(material, AuthMaterial):
                raise AuthError(
                    f'[auth."{rule.pattern}"]: provider {rule.provider!r} returned '
                    f"{type(material).__name__}, expected AuthMaterial"
                )
            expires_at = None if ttl <= 0 else now + ttl
            self._cache[cache_key] = (expires_at, material)
            return material

    def describe(self, host: str) -> tuple[AuthRule | None, object | None]:
        """The rule and provider ``host`` would use (for ``auth check``)."""
        rule = self.rule_for(host)
        return rule, (self.provider_for(rule) if rule else None)


def _ttl(rule: AuthRule) -> float:
    raw = rule.options.get("ttl", 0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise DownctlError(f'[auth."{rule.pattern}"]: ttl must be a number of seconds')
    if raw < 0:
        raise DownctlError(f'[auth."{rule.pattern}"]: ttl must be >= 0 (0 = reuse for the run)')
    return float(raw)
