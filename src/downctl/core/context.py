"""Runtime context injected into plugins.

Plugins must not build their own global state: everything they need (HTTP
session, config, event bus) is handed to them through a ``RuntimeContext``.
This keeps plugins testable and the engine in control of lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiohttp

    from downctl.config import Config
    from downctl.core.ratelimit import RateLimiter
    from downctl.plugin.hooks import EventBus
    from downctl.plugin.registry import Registry
    from downctl.session import ProxyPolicy


@dataclass
class RunStats:
    """Per-run counters used for an honest final summary.

    ``bytes_transferred`` counts bytes actually received this run, so resumed
    or skipped files do not inflate the reported throughput.
    """

    bytes_transferred: int = 0


@dataclass
class RuntimeContext:
    """Shared services available to every plugin call."""

    session: aiohttp.ClientSession
    config: Config
    registry: Registry | None = None
    events: EventBus | None = None
    #: Bytes-per-second limit shared by all tasks, or ``None`` for unlimited.
    rate_limit: float | None = None
    #: A single rate limiter shared across all tasks (global throttle).
    rate_limiter: RateLimiter | None = None
    #: Plugin-scoped options, i.e. ``config.plugins[<plugin-name>]``.
    options: dict[str, Any] = None  # type: ignore[assignment]
    #: Per-run parameters passed from the CLI (e.g. include/exclude/revision).
    params: dict[str, Any] = None  # type: ignore[assignment]
    #: Counters for the run summary.
    stats: RunStats = field(default_factory=RunStats)
    #: Whether a target may be fetched through the configured proxy. Empty by
    #: default, i.e. every request connects directly.
    proxy_policy: ProxyPolicy | None = None

    def __post_init__(self) -> None:
        if self.options is None:
            self.options = {}
        if self.params is None:
            self.params = {}

    def proxy_for(self, url: str) -> str | None:
        """The proxy this run may use for ``url``, or ``None`` for a direct connection.

        Every request made for this run has to ask this question with the URL it
        is about to fetch. A session-wide proxy would answer it once for all
        targets, which is what made a guessed host proxy intercept loopback.
        """
        return self.proxy_policy.proxy_for(url) if self.proxy_policy else None

    def plugin_options(self, name: str) -> dict[str, Any]:
        """Return the ``[plugins.<name>]`` options, or an empty mapping."""
        options = self.options.get(name, {})
        return options if isinstance(options, dict) else {}
