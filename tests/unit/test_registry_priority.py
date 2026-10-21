"""A bad priority must be refused while the plugin is still nameable.

It used to be stored as declared and cast only during source selection, so
``priority = "high"`` registered cleanly and every target then raised
``ValueError`` comparing ``str`` with ``int``. Registration is the only place
the plugin name and value are both available.
"""

from __future__ import annotations

from typing import Any

import pytest

from downctl.plugin.registry import PluginError, Registry
from downctl.plugin.testing import check_plugin


class _Source:
    api_version = "1"

    def __init__(self, name: str = "bad", priority: Any = 0) -> None:
        self.name = name
        self.priority = priority

    def can_handle(self, target: str) -> bool:
        return target.startswith("x://")

    async def resolve(self, target: str, ctx: Any) -> list[object]:
        return []


@pytest.mark.parametrize("priority", ["high", 1.5, True, False, None])
def test_a_non_integer_priority_is_refused_at_registration(priority: object) -> None:
    registry = Registry()

    with pytest.raises(PluginError, match="priority must be an integer") as caught:
        registry.register("source", _Source(priority=priority), origin="test:bad")

    message = str(caught.value)
    assert "plugin 'bad'" in message
    assert f"got {priority!r}" in message
    # A failed registration must not leave a half-registered plugin behind.
    assert registry.names("source") == []


def test_the_registry_is_still_usable_after_a_refused_priority() -> None:
    registry = Registry()
    with pytest.raises(PluginError):
        registry.register("source", _Source(priority="high"), origin="test:bad")

    good = _Source(name="good", priority=10)
    registry.register("source", good, origin="test:good")

    assert registry.names("source") == ["good"]
    assert registry.select_source("x://a") is good


def test_a_refused_replacement_keeps_the_existing_plugin() -> None:
    registry = Registry()
    good = _Source(name="same", priority=10)
    registry.register("source", good, origin="test:good")

    with pytest.raises(PluginError):
        registry.register("source", _Source(name="same", priority="high"), origin="test:bad")

    assert registry.get("source", "same") is good
    assert registry.origin_of("source", "same") == "test:good"


@pytest.mark.parametrize("priority", ["high", 1.5, True])
def test_check_plugin_applies_the_same_priority_rule(priority: object) -> None:
    """``plugin check`` and the registry must not disagree about an int."""
    problems = check_plugin(_Source(priority=priority), "source")

    assert any(
        "priority must be an integer" in problem and repr(priority) in problem
        for problem in problems
    ), problems
