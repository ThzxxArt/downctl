"""Tests for platform helpers."""

from __future__ import annotations

import pytest

from downctl.platform_utils import (
    human_age,
    human_size,
    parse_rate,
    platform_kind,
    stderr_is_tty,
    stdout_is_tty,
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


@pytest.mark.parametrize("path", [r"D:models", "D:", r"\\server\share"])
def test_win_to_wsl_path_rejects_unmappable_forms(path: str) -> None:
    with pytest.raises(ValueError, match="WSL"):
        win_to_wsl_path(path)


def test_platform_kind_maps_each_supported_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the mapping instead of accepting any of five answers.

    The old assertion listed every possible value, so it could not fail.
    """
    import downctl.platform_utils as helpers

    monkeypatch.setattr(helpers.platform, "system", lambda: "Darwin")
    assert platform_kind() == "Darwin"
    monkeypatch.setattr(helpers.platform, "system", lambda: "Windows")
    assert platform_kind() == "Windows"
    monkeypatch.setattr(helpers.platform, "system", lambda: "Plan9")
    assert platform_kind() == "unknown"

    class _Version:
        def __init__(self, text: str) -> None:
            self._text = text

        def __enter__(self) -> _Version:
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

        def read(self) -> str:
            return self._text

    monkeypatch.setattr(helpers.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        helpers, "open", lambda *a, **k: _Version("Linux version 6.6"), raising=False
    )
    assert platform_kind() == "Linux"
    monkeypatch.setattr(
        helpers, "open", lambda *a, **k: _Version("Linux version 5.15 microsoft-standard-WSL2")
    )
    assert platform_kind() == "WSL"

    def unreadable(*_args: object, **_kwargs: object) -> object:
        raise OSError("no /proc")

    monkeypatch.setattr(helpers, "open", unreadable)
    assert platform_kind() == "Linux"


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


def test_tty_helpers_survive_a_missing_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert stdout_is_tty() is False
    assert stderr_is_tty() is False


def test_stderr_is_tty_is_its_own_question(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    class Stream:
        def __init__(self, answer: bool) -> None:
            self.answer = answer

        def isatty(self) -> bool:
            return self.answer

    monkeypatch.setattr(sys, "stdout", Stream(False))
    monkeypatch.setattr(sys, "stderr", Stream(True))
    assert stdout_is_tty() is False
    assert stderr_is_tty() is True
