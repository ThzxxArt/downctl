"""Runtime context injected into plugins.

Plugins must not build their own global state: everything they need (HTTP
session, config, event bus) is handed to them through a ``RuntimeContext``.
This keeps plugins testable and the engine in control of lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiohttp

    from downctl.config import Config
    from downctl.core.ratelimit import RateLimiter
    from downctl.plugin.hooks import EventBus
    from downctl.plugin.registry import Registry


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

    def __post_init__(self) -> None:
        if self.options is None:
            self.options = {}
        if self.params is None:
            self.params = {}

    def plugin_options(self, name: str) -> dict[str, Any]:
        """Return the ``[plugins.<name>]`` options, or an empty mapping."""
        options = self.options.get(name, {})
        return options if isinstance(options, dict) else {}
