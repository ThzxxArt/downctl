"""Plugin registry: registration, validation and selection."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from downctl.plugin import protocols
from downctl.plugin.protocols import API_VERSION

PluginT = TypeVar("PluginT")

#: The six plugin categories, mapped to their Protocol.
CATEGORIES: dict[str, type] = {
    "source": protocols.Source,
    "transport": protocols.Transport,
    "verifier": protocols.Verifier,
    "reporter": protocols.Reporter,
    "router": protocols.Router,
    "hook": protocols.Hook,
}

#: Required callable members per category (structural protocols only check
#: attribute presence, so verify callability explicitly).
CATEGORY_METHODS: dict[str, tuple[str, ...]] = {
    "source": ("can_handle", "resolve"),
    "transport": ("supports_range", "request"),
    "verifier": ("verify",),
    "reporter": ("task_started", "task_progress", "task_finished"),
    "router": ("rewrite",),
    "hook": ("on_event",),
}


class PluginError(Exception):
    """Raised when a plugin is invalid or incompatible."""


class Registry:
    """Holds discovered plugins, keyed by category then name."""

    def __init__(self) -> None:
        self._plugins: dict[str, dict[str, object]] = {cat: {} for cat in CATEGORIES}

    def register(self, category: str, plugin: object, *, origin: str = "") -> None:
        """Validate and store ``plugin`` under ``category``."""
        if category not in CATEGORIES:
            raise PluginError(f"unknown plugin category: {category}")
        protocol = CATEGORIES[category]
        if not isinstance(plugin, protocol):
            raise PluginError(
                f"{origin or plugin!r} is not a valid {category} plugin "
                f"(missing required attributes/methods)"
            )
        for method in CATEGORY_METHODS[category]:
            if not callable(getattr(plugin, method, None)):
                raise PluginError(
                    f"{origin or plugin!r} is missing a callable {category}.{method}()"
                )
        version = getattr(plugin, "api_version", None)
        if version != API_VERSION:
            raise PluginError(
                f"{origin or plugin!r} has api_version={version!r}, expected {API_VERSION!r}"
            )
        name = getattr(plugin, "name", None)
        if not name:
            raise PluginError(f"{origin or plugin!r} is missing a 'name'")
        self._plugins[category][name] = plugin

    def get(self, category: str, name: str) -> object | None:
        return self._plugins.get(category, {}).get(name)

    def names(self, category: str) -> list[str]:
        return sorted(self._plugins.get(category, {}))

    def all(self, category: str) -> list[object]:
        return list(self._plugins.get(category, {}).values())

    def select_source(self, target: str) -> object | None:
        """Return the highest-priority source that can handle ``target``."""
        candidates = [s for s in self.all("source") if s.can_handle(target)]  # type: ignore[attr-defined]
        if not candidates:
            return None
        return max(candidates, key=lambda s: getattr(s, "priority", 0))

    def items(self) -> Iterable[tuple[str, dict[str, object]]]:
        return self._plugins.items()

    def __len__(self) -> int:
        return sum(len(v) for v in self._plugins.values())
