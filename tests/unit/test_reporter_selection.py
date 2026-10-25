"""Reporter selection: the place 0.1.6 had no tests and two wrong answers."""

from __future__ import annotations

import argparse

import pytest

from downctl import app, console
from downctl.builtin.reporters.json_reporter import JSONReporter
from downctl.builtin.reporters.live_reporter import LiveReporter, PlainReporter
from downctl.builtin.reporters.quiet_reporter import QuietReporter
from downctl.builtin.reporters.tqdm_reporter import TqdmReporter
from downctl.config import Config
from downctl.core.errors import DownctlError
from downctl.plugin.registry import Registry


@pytest.fixture(autouse=True)
def _reset_console(monkeypatch: pytest.MonkeyPatch) -> None:
    # The test runner's own TERM is irrelevant; each case states its terminal.
    monkeypatch.setenv("TERM", "xterm-256color")
    console.set_level(console.NORMAL)
    console.set_color(None)
    console.set_machine_readable(False)
    yield
    console.set_level(console.NORMAL)
    console.set_color(None)
    console.set_machine_readable(False)


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    for plugin in (
        LiveReporter(),
        PlainReporter(),
        TqdmReporter(),
        JSONReporter(),
        QuietReporter(),
    ):
        reg.register("reporter", plugin, origin="test")
    return reg


def _select(
    monkeypatch: pytest.MonkeyPatch,
    registry: Registry,
    *,
    tty: bool,
    reporter: str | None = None,
    progress: str | None = None,
    plugins: dict[str, dict[str, object]] | None = None,
) -> object:
    monkeypatch.setattr(app, "stderr_is_tty", lambda: tty)
    args = argparse.Namespace(reporter=reporter, progress=progress)
    cfg = Config()
    cfg.plugins = plugins or {}
    return app._select_reporter(args, cfg, registry)


def test_auto_on_a_terminal_selects_live(
    monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    reporter = _select(monkeypatch, registry, tty=True)
    assert getattr(reporter, "name", "") == "live"


def test_auto_without_a_terminal_keeps_the_quiet_contract(
    monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    reporter = _select(monkeypatch, registry, tty=False)
    assert getattr(reporter, "name", "") == "quiet"


def test_no_color_does_not_delete_the_progress_display(
    monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    reporter = _select(monkeypatch, registry, tty=True)
    assert getattr(reporter, "name", "") == "live"


def test_progress_off_and_plain_are_explicit(
    monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    assert getattr(_select(monkeypatch, registry, tty=True, progress="off"), "name", "") == "quiet"
    assert (
        getattr(_select(monkeypatch, registry, tty=True, progress="plain"), "name", "") == "plain"
    )


def test_live_on_a_pipe_falls_back_to_plain(
    monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    reporter = _select(monkeypatch, registry, tty=False, progress="live")
    assert getattr(reporter, "name", "") == "plain"


def test_quiet_wins_over_an_implicit_progress_mode(
    monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    console.set_level(console.QUIET)
    reporter = _select(monkeypatch, registry, tty=True, progress="live")
    assert getattr(reporter, "name", "") == "quiet"


def test_an_explicit_reporter_owns_stdout(
    monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    reporter = _select(monkeypatch, registry, tty=True, reporter="json")
    assert getattr(reporter, "name", "") == "json"
    assert console.is_machine_readable() is True


def test_unknown_reporter_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    with pytest.raises(DownctlError, match="unknown reporter"):
        _select(monkeypatch, registry, tty=True, reporter="nope")


def test_invalid_progress_mode_is_refused(
    monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    with pytest.raises(DownctlError, match="progress must be one of"):
        _select(monkeypatch, registry, tty=True, progress="pretty")


def test_legacy_tqdm_switch_off_still_means_silence(
    monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    reporter = _select(
        monkeypatch,
        registry,
        tty=True,
        plugins={"tqdm": {"enabled": False}},
    )
    assert getattr(reporter, "name", "") == "quiet"


def test_disabling_live_falls_back_to_tqdm(
    monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    reporter = _select(
        monkeypatch,
        registry,
        tty=True,
        plugins={"live": {"enabled": False}},
    )
    assert getattr(reporter, "name", "") == "tqdm"


def test_live_options_are_applied(monkeypatch: pytest.MonkeyPatch, registry: Registry) -> None:
    reporter = _select(
        monkeypatch,
        registry,
        tty=True,
        plugins={"live": {"fps": 4, "unicode": "never"}},
    )
    assert isinstance(reporter, LiveReporter)
    assert reporter.fps == 4
    assert reporter.unicode_mode == "never"


def test_dumb_terminal_is_not_drawn_on(monkeypatch: pytest.MonkeyPatch, registry: Registry) -> None:
    monkeypatch.setenv("TERM", "dumb")
    reporter = _select(monkeypatch, registry, tty=True)
    assert getattr(reporter, "name", "") == "quiet"
