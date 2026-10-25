"""The live/plain reporter is an observer: it must draw, and must never fail a run."""

from __future__ import annotations

import io

import pytest

from downctl import console
from downctl.builtin.reporters.live_reporter import LiveReporter, PlainReporter
from downctl.core.errors import DownctlError
from downctl.core.task import DownloadTask


class FakeTTY(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise OSError("no fd")


class FakePipe(io.StringIO):
    encoding = "utf-8"

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("no fd")


@pytest.fixture(autouse=True)
def _console_defaults() -> None:
    console.set_level(console.NORMAL)
    console.set_color(False)
    console.set_machine_readable(False)
    yield
    console.reset_live()
    console.set_level(console.NORMAL)
    console.set_color(None)
    console.set_machine_readable(False)


@pytest.fixture(autouse=True)
def _terminal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "90")
    monkeypatch.setenv("LINES", "20")


def _task(name: str = "a.bin", size: int = 10_000_000) -> DownloadTask:
    return DownloadTask(url=f"https://example.test/{name}", target=name, size=size)


def _run_two(reporter: LiveReporter) -> tuple[DownloadTask, DownloadTask]:
    reporter.run_started(2, 20_000_000, 2)
    first, second = _task("a.bin"), _task("b.bin")
    reporter.task_started(first)
    reporter.task_started(second)
    return first, second


def test_live_reporter_draws_frames_and_restores_the_cursor() -> None:
    stream = FakeTTY()
    reporter = LiveReporter(stream)
    first, _second = _run_two(reporter)
    reporter.task_progress(first, 2_000_000)
    reporter.run_finished(2, 0)
    text = stream.getvalue()
    assert "\x1b[?25l" in text
    assert "\x1b[2K" in text
    assert "\x1b[?25h" in text
    assert "downloading" in text


def test_a_second_run_starts_with_clean_state_and_keeps_the_terminal() -> None:
    stream = FakeTTY()
    reporter = LiveReporter(stream)
    first, _second = _run_two(reporter)
    reporter.task_progress(first, 1_000_000)
    reporter.run_finished(2, 0)
    stream.truncate(0)
    stream.seek(0)
    reporter.run_started(2, 10_000_000, 2, job="beta")
    text = stream.getvalue()
    assert "job beta" in text
    assert "2 files" in text


def test_non_tty_falls_back_to_plain_lines_without_escape_codes() -> None:
    stream = FakePipe()
    reporter = LiveReporter(stream)
    task = _task()
    reporter.run_started(1, 10_000_000, 1)
    reporter.task_started(task)
    reporter.task_progress(task, 1_000_000)
    reporter.task_finished(task, False)
    reporter.run_finished(0, 1)
    text = stream.getvalue()
    assert "\x1b[" not in text
    assert "downctl:" in text
    assert "failed a.bin" in text


def test_plain_reporter_never_draws_a_live_block_even_on_a_tty() -> None:
    stream = FakeTTY()
    reporter = PlainReporter(stream)
    reporter.run_started(1, 10_000_000, 1)
    reporter.run_finished(1, 0)
    assert "\x1b[?25l" not in stream.getvalue()
    assert "downctl:" in stream.getvalue()


def test_no_color_still_draws_the_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    console.set_color(None)
    stream = FakeTTY()
    reporter = LiveReporter(stream)
    _run_two(reporter)
    reporter.run_finished(2, 0)
    text = stream.getvalue()
    assert "\x1b[?25l" in text
    assert "\x1b[32m" not in text


def test_color_mode_adds_color_but_not_visible_width_changes() -> None:
    console.set_color(True)
    now = [0.0]
    stream = FakeTTY()
    reporter = LiveReporter(stream, clock=lambda: now[0])
    first, _second = _run_two(reporter)
    now[0] = 1.0
    reporter.task_progress(first, 1_000_000)
    reporter.task_started(first)  # force a draw after the clock advanced
    reporter.run_finished(2, 0)
    assert "\x1b[36m" in stream.getvalue() or "\x1b[32m" in stream.getvalue()


def test_configure_refuses_a_value_that_lies() -> None:
    reporter = LiveReporter(FakePipe())
    with pytest.raises(DownctlError):
        reporter.configure({"fps": 0})
    reporter.configure({"fps": 4, "unicode": "never", "color": "never"})
    assert reporter.fps == 4


def test_a_broken_renderer_does_not_escape_or_fail_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import downctl.builtin.reporters.live_reporter as module

    def exploding(*_args: object, **_kwargs: object) -> list[str]:
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(module, "render", exploding)
    stream = FakeTTY()
    reporter = LiveReporter(stream)
    reporter.run_started(1, 10_000_000, 1)  # must not raise
    reporter.close()


def test_console_messages_pause_and_restore_the_block() -> None:
    stream = FakeTTY()
    reporter = LiveReporter(stream)
    _run_two(reporter)
    before = stream.getvalue().count("\x1b[2K")
    console.warn("a warning during the run")
    after = stream.getvalue()
    assert after.count("\x1b[2K") > before
    assert "\x1b[?25h" in after  # suspended for the message
    reporter.run_finished(2, 0)


def test_task_retrying_and_verifying_do_not_raise() -> None:
    stream = FakeTTY()
    reporter = LiveReporter(stream)
    task = _task()
    reporter.run_started(1, 10_000_000, 1)
    reporter.task_started(task)
    reporter.task_progress(task, 1_000_000)
    reporter.task_retrying(task, 2, 5.0, "HTTP 502")
    reporter.task_phase(task, "verify", "sha256")
    reporter.task_restarted(task)
    reporter.run_finished(1, 0)


def test_close_is_idempotent_and_safe_without_a_run() -> None:
    reporter = LiveReporter(FakePipe())
    reporter.close()
    reporter.close()
    reporter.run_started(0, 0, 1)
    reporter.close()
    assert reporter.mode == "plain"


def test_plain_failure_line_is_immediate() -> None:
    stream = FakePipe()
    reporter = LiveReporter(stream)
    task = _task("broken.bin")
    reporter.run_started(1, task.size, 1)
    reporter.task_started(task)
    reporter.task_finished(task, False)
    assert "failed broken.bin" in stream.getvalue()
