"""Tests for the console level helpers."""

from __future__ import annotations

import pytest

from downctl import console


@pytest.fixture(autouse=True)
def _reset_level() -> None:
    console.set_level(console.NORMAL)
    console.set_color(None)
    yield
    console.set_level(console.NORMAL)
    console.set_color(None)


def test_info_hidden_when_quiet(capsys: pytest.CaptureFixture[str]) -> None:
    console.set_level(console.QUIET)
    console.info("x")
    console.verbose("y")
    console.warn("z")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_info_shown_at_normal(capsys: pytest.CaptureFixture[str]) -> None:
    console.info("hello")
    assert "hello" in capsys.readouterr().out


def test_verbose_only_at_verbose(capsys: pytest.CaptureFixture[str]) -> None:
    console.info("a")
    console.verbose("b")
    out = capsys.readouterr().out
    assert "a" in out and "b" not in out

    console.set_level(console.VERBOSE)
    console.verbose("c")
    assert "c" in capsys.readouterr().out


def test_error_always_shown(capsys: pytest.CaptureFixture[str]) -> None:
    console.set_level(console.QUIET)
    console.error("boom")
    assert "boom" in capsys.readouterr().err


def test_is_quiet() -> None:
    assert not console.is_quiet()
    console.set_level(console.QUIET)
    assert console.is_quiet()


def test_color_forced_on(capsys: pytest.CaptureFixture[str]) -> None:
    console.set_color(True)
    console.warn("careful")
    console.error("bad")
    err = capsys.readouterr().err
    assert "\033[33m" in err
    assert "\033[31m" in err


def test_color_forced_off(capsys: pytest.CaptureFixture[str]) -> None:
    console.set_color(False)
    console.warn("plain")
    assert "\033[" not in capsys.readouterr().err
