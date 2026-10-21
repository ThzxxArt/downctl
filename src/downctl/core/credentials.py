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
from urllib.parse import urlsplit

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


def _normalize_host(host: str) -> str:
    """Canonicalize a host for rule matching.

    DNS is case-insensitive, and the same name has two spellings once it leaves
    ASCII: a config written as ``bücher.example`` and a URL already encoded as
    ``xn--bcher-kva.example`` name one host. Lower-casing and IDNA-encoding both
    sides is what makes them match; comparing the spellings made the rule look
    absent. IPv6 brackets are not part of the address either.
    """
    normalized = host.strip().lower().strip("[]")
    if not normalized:
        return ""
    try:
        return normalized.encode("idna").decode("ascii")
    except UnicodeError:
        # An underscore or an over-long label is not IDNA-encodable. Matching
        # it literally is more useful than turning a config typo into "no rule".
        return normalized


def _parse_target(value: str) -> tuple[str, str] | None:
    """``(canonical host, cache origin)`` for a URL or a bare ``host[:port]``."""
    raw = value.strip()
    if not raw:
        return None
    # ``urlsplit("huggingface.co")`` is a path with no host; an authority marker
    # is what makes it parse ``host:port`` and ``user@host`` correctly.
    candidate = raw if "://" in raw or raw.startswith("//") else f"//{raw}"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    host = _normalize_host(parts.hostname or "")
    if not host:
        return None
    # A cache key is an origin (scheme, host, port), never the userinfo: it may
    # itself be a credential, and the provider is only told the host.
    authority = parts.netloc.rsplit("@", 1)[-1].lower()
    origin = f"{parts.scheme.lower()}://{authority}"
    return host, origin


def host_from_input(value: str) -> str | None:
    """The canonical host named by a URL or a bare ``host[:port]``.

    ``downctl auth check`` is often given a host (``huggingface.co``) rather
    than a URL, but a credential rule is a statement about a host, so both
    spellings must find the same rule. Ports and userinfo are discarded here:
    :class:`AuthRule` never matches on them.
    """
    target = _parse_target(value)
    return target[0] if target is not None else None


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
        # Accept the same spellings ``AuthResolver.material_for`` does (URL,
        # bare ``host:port``, userinfo); a direct caller should not have to
        # strip them first. IPv6 and malformed inputs fall back to literal
        # normalization.
        canonical = host_from_input(host)
        host = canonical if canonical is not None else _normalize_host(host)
        pattern = self.pattern.lower()
        if self.wildcard:
            suffix = _normalize_host(pattern[2:])
            return bool(suffix) and host.endswith("." + suffix) and host != suffix
        return host == _normalize_host(pattern)

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
        # Two spellings of one DNS name (Unicode vs punycode) are one host, so
        # they are one rule; otherwise "which table wins" depends on load order.
        key = _normalize_host(str(pattern).strip())
        if key in seen:
            raise DownctlError(f'[auth."{pattern}"] is defined more than once')
        seen.add(key)
        rules.append(_rule_from_table(str(pattern), table))
    # Most specific first, so the first match is the winner.
    rules.sort(key=lambda rule: rule.specificity(), reverse=True)
    return rules


class AuthResolver:
    """Turns a URL into the material its host is entitled to.

    Resolution is cached per ``(origin, rule, provider)``. ``ttl`` (seconds,
    default ``0``) controls freshness: ``0`` means "resolve once for the run",
    which is what an environment variable or a file means; a command-backed
    provider can set ``ttl`` so a short-lived token is renewed instead of reused
    until it expires mid-download.

    Materialization is serialized per host/rule, not across the resolver: a slow
    ``command`` provider for one host must not delay another host's first
    request.
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
        #: Serialize materialization per host/rule. Keyed by host rather than
        #: origin, so two schemes or ports of one host cannot run the provider
        #: twice at once; the cache stays per origin because that is what a
        #: request hop owns.
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    def __bool__(self) -> bool:
        return bool(self._rules)

    @property
    def rules(self) -> list[AuthRule]:
        return list(self._rules)

    def rule_for(self, host: str) -> AuthRule | None:
        # ``rule.matches`` accepts a URL, a bare host and ``host:port``; callers
        # (``describe``, ``material_for``) therefore do not need one spelling.
        for rule in self._rules:
            if rule.matches(host):
                return rule
        return None

    def provider_for(self, rule: AuthRule) -> object | None:
        return self._registry.get("auth", rule.provider)

    def _lock_for(self, key: tuple[str, str, str]) -> asyncio.Lock:
        """The lock for one ``(host, rule, provider)`` key.

        Creating it is synchronous, so two coroutines cannot race between the
        lookup and the insert on a single event loop.
        """
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _can_handle(self, provider: object, host: str, rule: AuthRule) -> bool:
        """Whether ``provider`` accepts ``host`` (the ``can_handle`` contract).

        The registry requires the method, so the missing branch only covers a
        hand-built registry in an embedding. Absence means "no opinion" (serve
        the host), not "deny every host": silently dropping a configured
        credential would be the worse default.
        """
        check = getattr(provider, "can_handle", None)
        if not callable(check):
            return True
        try:
            return bool(check(host))
        except Exception as exc:  # noqa: BLE001 - text is redacted and re-raised
            from downctl.urls import redact_text

            raise AuthError(
                f'[auth."{rule.pattern}"]: provider {rule.provider!r} can_handle() failed: '
                f"{redact_text(str(exc))}"
            ) from exc

    async def material_for(self, url: str) -> AuthMaterial | None:
        """Resolve the material for ``url``'s host, or ``None``.

        ``url`` may be a full URL or a bare host (``huggingface.co``): a rule is
        a statement about a host, and both spellings name the same one.
        """
        target = _parse_target(url)
        if target is None:
            return None
        host, origin = target
        rule = self.rule_for(host)
        if rule is None:
            return None
        provider = self.provider_for(rule)
        if provider is None:
            raise AuthError(
                f'[auth."{rule.pattern}"] names unknown auth provider {rule.provider!r}; '
                f"available: {', '.join(self._registry.names('auth')) or 'none'}"
            )
        cache_key = (origin, rule.pattern, rule.provider)
        lock_key = (host, rule.pattern, rule.provider)
        ttl = _ttl(rule)
        # One provider call at a time per host/rule. Probe and piece requests
        # run concurrently, and without this a ``command`` provider was executed
        # once per in-flight request before any of them filled the cache.
        async with self._lock_for(lock_key):
            now = time.monotonic()
            cached = self._cache.get(cache_key)
            if cached is not None:
                expires, cached_material = cached
                if expires is None or expires > now:
                    return cached_material
            if not self._can_handle(provider, host, rule):
                # A decline is an answer, not a failure: ``material`` is not
                # called and no material is resolved. Caching the answer keeps
                # a request loop from re-asking; the next TTL window retries.
                declined_until = None if ttl <= 0 else time.monotonic() + ttl
                self._cache[cache_key] = (declined_until, None)
                return None
            try:
                material = await provider.material(  # type: ignore[attr-defined]
                    host, self._ctx, rule.options
                )
            except AuthError as exc:
                # The provider's message ("env HF_TOKEN is unset") is the
                # useful half; the rule names *which* configured identity
                # failed, so a host with two rules is diagnosable. Failures are
                # not cached: the next call must retry, not inherit an error.
                raise AuthError(f'[auth."{rule.pattern}"]: {exc}') from exc
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
            # TTL starts when the material exists, not when this call started:
            # a provider that took longer than ``ttl`` to produce a token must
            # not hand back material that is already expired.
            expires_at = None if ttl <= 0 else time.monotonic() + ttl
            self._cache[cache_key] = (expires_at, material)
            return material

    def describe(self, host: str) -> tuple[AuthRule | None, object | None]:
        """The rule and provider ``host`` would use (for ``auth check``).

        ``host`` may be a URL or a bare host; :meth:`rule_for` canonicalizes it.
        """
        rule = self.rule_for(host)
        return rule, (self.provider_for(rule) if rule else None)


def _ttl(rule: AuthRule) -> float:
    raw = rule.options.get("ttl", 0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise DownctlError(f'[auth."{rule.pattern}"]: ttl must be a number of seconds')
    if raw < 0:
        raise DownctlError(f'[auth."{rule.pattern}"]: ttl must be >= 0 (0 = reuse for the run)')
    return float(raw)
