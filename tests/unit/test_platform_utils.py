"""Tests for platform helpers."""

from __future__ import annotations

import pytest

from downctl.platform_utils import parse_rate, platform_kind, win_to_wsl_path


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("512", 512.0),
        ("1k", 1024.0),
        ("5M", 5 * 1048576.0),
        ("2g", 2 * 1073741824.0),
        (" 1.5m ", 1.5 * 1048576.0),
    ],
)
def test_parse_rate_ok(text: str, expected: float) -> None:
    assert parse_rate(text) == expected


def test_parse_rate_none() -> None:
    assert parse_rate("") is None


def test_parse_rate_invalid() -> None:
    with pytest.raises(ValueError, match="cannot parse rate"):
        parse_rate("fast")


def test_win_to_wsl_path() -> None:
    assert win_to_wsl_path(r"D:\models") == "/mnt/d/models"
    assert win_to_wsl_path("C:/data/x") == "/mnt/c/data/x"
    assert win_to_wsl_path("/already/unix") == "/already/unix"


def test_platform_kind_is_known() -> None:
    assert platform_kind() in {"WSL", "Linux", "Darwin", "Windows", "unknown"}
