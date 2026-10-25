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


def test_no_color_is_honoured_by_presence_even_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "")
    console.set_color(None)
    assert console.color_enabled() is False


def test_force_color_enables_colour_without_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    console.set_color(None)
    assert console.color_enabled() is True


def test_set_color_overrides_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "0")
    console.set_color(True)
    assert console.color_enabled() is True


def test_messages_pause_an_attached_live_region(capsys: pytest.CaptureFixture[str]) -> None:
    import contextlib

    class Region:
        def __init__(self) -> None:
            self.active = 0
            self.writes = 0

        @contextlib.contextmanager
        def writing(self):  # type: ignore[no-untyped-def]
            self.active += 1
            self.writes += 1
            yield
            self.active -= 1

    region = Region()
    console.attach_live(region)
    try:
        console.info("hello")
        assert region.writes == 1
        assert region.active == 0
        assert "hello" in capsys.readouterr().out
    finally:
        console.detach_live(region)


def test_reset_live_closes_the_region() -> None:
    class Region:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    region = Region()
    console.attach_live(region)  # type: ignore[arg-type]
    console.reset_live()
    assert region.closed == 1
    # Nothing attached now; a second reset is harmless.
    console.reset_live()
    assert region.closed == 1
