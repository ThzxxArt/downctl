"""Testing helpers for plugin authors.

Shipped as part of the public API (``downctl.plugin.testing``) so a third-party
plugin can be checked against the same contract the registry enforces, without
installing downctl's test suite or copying its internals. The registry remains
the only owner of "is this a valid plugin"; this module calls it instead of
re-deriving the rules.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from downctl.config import Config
from downctl.plugin.protocols import API_VERSION
from downctl.plugin.registry import (
    CATEGORIES,
    CATEGORY_METHODS,
    PluginError,
    Registry,
    integer_problem,
)


def check_plugin(plugin: object, category: str) -> list[str]:
    """Return every contract problem ``plugin`` has as ``category``.

    This is exactly what :meth:`Registry.register` validates (plus the optional
    capability hints), so ``plugin check`` passing means the registry will
    accept the plugin.
    """
    problems: list[str] = []
    if category not in CATEGORIES:
        return [f"unknown category {category!r}; known: {', '.join(sorted(CATEGORIES))}"]
    protocol = CATEGORIES[category]
    if not isinstance(plugin, protocol):
        missing = [member for member in _required_members(category) if not hasattr(plugin, member)]
        detail = f" (missing: {', '.join(missing)})" if missing else ""
        problems.append(f"does not satisfy the {category} protocol{detail}")
    for method in CATEGORY_METHODS[category]:
        if not callable(getattr(plugin, method, None)):
            problems.append(f"missing a callable {method}()")
    version = getattr(plugin, "api_version", None)
    if version != API_VERSION:
        problems.append(f"api_version is {version!r}, this build expects {API_VERSION!r}")
    name = getattr(plugin, "name", None)
    if not isinstance(name, str) or not name:
        problems.append("missing a non-empty 'name'")
    absent = object()
    priority = getattr(plugin, "priority", absent)
    if priority is not absent:
        problem = integer_problem(plugin, "priority")
        if problem is not None:
            problems.append(problem)
    return problems


#: Attributes a category needs beyond the registry's method checks.
_EXTRA_MEMBERS: dict[str, tuple[str, ...]] = {
    "verifier": ("algo",),
    "jobsource": ("formats",),
}


def _required_members(category: str) -> tuple[str, ...]:
    base = ("name", "api_version", "priority")
    return base + _EXTRA_MEMBERS.get(category, ()) + CATEGORY_METHODS.get(category, ())


def capabilities(plugin: object, category: str) -> list[str]:
    """Optional capabilities the plugin has switched on (for ``plugin info``).

    A ``runtime_checkable`` Protocol proves only that an attribute exists, not
    that its value is true. Every flag here is an opt-in promise, so only an
    explicit ``True`` is reported: a source declaring ``supports_filters =
    False`` (the plain-URL source) supports no filtering and must not be listed
    beside one that does. ``aclose`` has no flag; it is reported when callable.
    """
    del category  # kept for API compatibility: the flags describe themselves
    found: list[str] = []
    if callable(getattr(plugin, "aclose", None)):
        found.append("aclose")
    if getattr(plugin, "machine_readable", False) is True:
        found.append("machine_readable")
    if getattr(plugin, "sends_content_range", False) is True:
        found.append("sends_content_range")
    if getattr(plugin, "supports_filters", False) is True:
        found.append("supports_filters")
    return found


def option_names(plugin: object) -> list[str]:
    """The names a configurable plugin declares, if it declares any."""
    options = getattr(plugin, "OPTIONS", None)
    if not isinstance(options, dict):
        return []
    return sorted(str(key) for key in options)


def assert_registers(registry: Registry, category: str, plugin: object) -> None:
    """Register ``plugin``, raising :class:`PluginError` on a contract problem."""
    problems = check_plugin(plugin, category)
    if problems:
        raise PluginError(f"{category} plugin {getattr(plugin, 'name', plugin)!r}: {problems}")
    registry.register(category, plugin, origin="testing")


def fake_context(**overrides: Any) -> SimpleNamespace:
    """A context-shaped object for unit tests that never touch the network.

    Deliberately a ``SimpleNamespace`` and not a ``RuntimeContext``: the real
    one requires an ``aiohttp`` session, and a plugin that needs one in a unit
    test should be handed a real session rather than a fake with no behaviour.
    """
    base: dict[str, Any] = {
        "session": None,
        "config": Config(),
        "registry": None,
        "events": None,
        "rate_limit": None,
        "rate_limiter": None,
        "options": {},
        "params": {},
        "proxy_policy": None,
        "auth": None,
        "job": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)
