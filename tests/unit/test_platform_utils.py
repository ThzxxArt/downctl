"""Tests for platform helpers."""

from __future__ import annotations

import pytest

from downctl.platform_utils import (
    fmt_num,
    human_age,
    human_size,
    parse_rate,
    platform_kind,
    win_to_wsl_path,
)


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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (2048, "2.0 KB"),
        (5 * 1048576, "5.0 MB"),
        (3 * 1073741824, "3.00 GB"),
    ],
)
def test_human_size(value: int, expected: str) -> None:
    assert human_size(value) == expected


def test_fmt_num() -> None:
    assert fmt_num(1234567) == "1,234,567"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (5, "just now"),
        (60, "1m ago"),
        (3600, "1h ago"),
        (2 * 86400, "2d ago"),
    ],
)
def test_human_age(seconds: int, expected: str) -> None:
    assert human_age(seconds) == expected
