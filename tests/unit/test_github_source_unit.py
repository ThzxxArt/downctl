"""Unit tests for the GitHub source (handling and parsing)."""

from __future__ import annotations

import pytest

from downctl.builtin.sources.github_source import GitHubSource, _parse_target
from downctl.core.errors import DownctlError


@pytest.mark.parametrize("target", ["gh://owner/repo", "gh://owner/repo@v1.2.3"])
def test_can_handle_true(target: str) -> None:
    assert GitHubSource().can_handle(target)


@pytest.mark.parametrize("target", ["https://github.com/a/b", "hf://a/b", "owner/repo"])
def test_can_handle_false(target: str) -> None:
    assert not GitHubSource().can_handle(target)


def test_parse_target() -> None:
    assert _parse_target("gh://owner/repo") == ("owner", "repo", "")
    assert _parse_target("gh://owner/repo@v2") == ("owner", "repo", "v2")


@pytest.mark.parametrize("target", ["gh://onlyowner", "gh://a/b/c", "gh://"])
def test_parse_target_invalid(target: str) -> None:
    with pytest.raises(DownctlError, match="invalid GitHub repo"):
        _parse_target(target)
