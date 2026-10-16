"""End-to-end smoke tests for the P1 skeleton."""

from __future__ import annotations

from downctl import API_VERSION, __version__
from downctl.builtin.sources.url_source import filename_from_url
from downctl.plugin.loader import discover


def test_version_and_api_version() -> None:
    assert __version__
    assert API_VERSION == "1"


def test_full_registry_and_source_resolution() -> None:
    registry = discover()
    assert len(registry) >= 9

    for name in ("url", "huggingface"):
        plugin = registry.get("source", name)
        assert plugin is not None
        assert plugin.api_version == API_VERSION


def test_filename_from_url() -> None:
    assert filename_from_url("https://example.com/a/b/model.safetensors") == "model.safetensors"
    assert filename_from_url("https://example.com/a/my%20file.bin?v=1") == "my file.bin"
