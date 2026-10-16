"""Plugin discovery and registry tests."""

from __future__ import annotations

import pytest

from downctl.plugin.loader import discover
from downctl.plugin.registry import PluginError, Registry


def test_discover_loads_builtins() -> None:
    registry = discover()
    assert "url" in registry.names("source")
    assert "huggingface" in registry.names("source")
    assert "http" in registry.names("transport")
    assert "sha256" in registry.names("verifier")
    assert "filesystem" in registry.names("sink")
    assert {"tqdm", "json", "quiet"} <= set(registry.names("reporter"))
    assert "mirror" in registry.names("router")
    assert "logfile" in registry.names("hook")


def test_select_source_by_scheme() -> None:
    registry = discover()
    url_source = registry.select_source("https://example.com/a.bin")
    assert url_source is not None
    assert url_source.name == "url"

    hf_source = registry.select_source("hf://org/name")
    assert hf_source is not None
    assert hf_source.name == "huggingface"


def test_select_source_unknown_returns_none() -> None:
    registry = discover()
    assert registry.select_source("ftp://example.com/a.bin") is None


def test_register_rejects_wrong_api_version() -> None:
    registry = Registry()

    class Bad:
        name = "bad"
        api_version = "999"
        priority = 0

        def can_handle(self, target: str) -> bool:  # pragma: no cover - stub
            return False

        async def resolve(self, target: str, ctx: object) -> list:  # pragma: no cover - stub
            return []

    with pytest.raises(PluginError, match="api_version"):
        registry.register("source", Bad())


def test_register_rejects_unknown_category() -> None:
    registry = Registry()
    with pytest.raises(PluginError, match="unknown plugin category"):
        registry.register("nonsense", object())
