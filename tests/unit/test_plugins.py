"""Plugin discovery and registry tests."""

from __future__ import annotations

import pytest

from downctl.plugin.loader import discover
from downctl.plugin.registry import PluginError, Registry


def test_a_retired_entry_point_group_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A package declaring ``downctl.sinks`` must be told, never silently ignored."""
    from downctl.plugin import loader

    class _EntryPoint:
        name = "filesystem"

    monkeypatch.setattr(
        loader.metadata,
        "entry_points",
        lambda group=None: [_EntryPoint()] if group == "downctl.sinks" else [],
    )
    loader.warn_retired_groups()
    err = capsys.readouterr().err
    assert "downctl.sinks" in err
    assert "filesystem" in err
    assert "0.1.4" in err


def test_the_retired_group_warning_stays_quiet_without_declarations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from downctl.plugin import loader

    monkeypatch.setattr(loader.metadata, "entry_points", lambda group=None: [])
    loader.warn_retired_groups()
    assert capsys.readouterr().err == ""


def test_a_broken_entry_point_query_never_breaks_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from downctl.plugin import loader

    def boom(group: str | None = None) -> list[object]:
        raise RuntimeError("metadata is unreadable")

    monkeypatch.setattr(loader.metadata, "entry_points", boom)
    loader.warn_retired_groups()  # must not raise


def test_discover_loads_builtins() -> None:
    registry = discover()
    assert "url" in registry.names("source")
    assert "huggingface" in registry.names("source")
    assert "http" in registry.names("transport")
    assert "sha256" in registry.names("verifier")
    assert {"tqdm", "json", "quiet"} <= set(registry.names("reporter"))
    assert "mirror" in registry.names("router")
    assert "logfile" in registry.names("hook")


def test_sink_category_is_gone() -> None:
    """The retired category must not survive as a registered-but-unused one."""
    from downctl.plugin.registry import CATEGORIES

    assert "sink" not in CATEGORIES
    registry = discover()
    assert dict(registry.items()).keys() == set(CATEGORIES)
    with pytest.raises(PluginError, match="unknown plugin category"):
        Registry().register("sink", object())


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
