"""Tests for the aggregate progress reporter.

The renderer's state was previously asserted through ``_overall``/``_per_task``
/``_bars``. What a user (and a test) can actually observe is the output stream
and what the public hooks do afterwards, so the assertions below read the
rendered bars instead of the private fields that produce them.
"""

from __future__ import annotations

import time

import pytest

from downctl.builtin.reporters.tqdm_reporter import TqdmReporter
from downctl.core.task import DownloadTask


def _stderr(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().err


def test_reporter_lifecycle_with_overall(capsys: pytest.CaptureFixture[str]) -> None:
    reporter = TqdmReporter()
    reporter.run_started(2, 20)
    assert "total (2 files)" in _stderr(capsys)

    task = DownloadTask(url="u", target="a.bin", size=10)
    reporter.task_started(task)
    assert "a.bin" in _stderr(capsys)

    reporter.task_progress(task, 5)
    reporter.task_finished(task, True)
    reporter.run_finished(1, 0)

    output = _stderr(capsys)
    assert "total (1/2 files)" in output
    # ``run_resumed`` only touches the overall bar, so silence here is the
    # observable proof that ``run_finished`` released it.
    reporter.run_resumed(5)
    assert _stderr(capsys) == ""


def test_reporter_skips_overall_when_size_unknown(capsys: pytest.CaptureFixture[str]) -> None:
    reporter = TqdmReporter()
    reporter.run_started(1, 0)
    assert "total (" not in _stderr(capsys)

    # With no overall bar there is nothing for the resume seed to advance.
    reporter.run_resumed(40)
    assert _stderr(capsys) == ""
    reporter.run_finished(0, 1)


def test_reporter_concurrent_has_no_per_task_bar(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reporter = TqdmReporter()
    reporter.run_started(3, 30, jobs=3)
    assert "total (3 files)" in _stderr(capsys)

    task = DownloadTask(url="u", target="a.bin", size=10)
    reporter.task_started(task)
    # A concurrent run shows one aggregate bar; the per-file description must
    # not appear at all.
    assert "a.bin" not in _stderr(capsys)

    reporter.task_progress(task, 5)
    reporter.task_finished(task, True)
    reporter.run_finished(1, 0)
    assert "total (1/3 files)" in _stderr(capsys)

    reporter.run_resumed(5)
    assert _stderr(capsys) == ""


def test_task_started_is_observable_without_reading_private_bars(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The bar description is the public evidence that a task bar exists."""
    reporter = TqdmReporter()
    reporter.run_started(1, 100)
    task = DownloadTask(url="u", target="visible.bin", size=100)
    reporter.task_started(task)

    assert "visible.bin" in _stderr(capsys)
    reporter.task_finished(task, True)
    reporter.run_finished(1, 0)


def test_progress_is_visible_in_the_rendered_bar(capsys: pytest.CaptureFixture[str]) -> None:
    reporter = TqdmReporter()
    reporter.run_started(1, 100)
    task = DownloadTask(url="u", target="a.bin", size=100)
    reporter.task_started(task)
    _stderr(capsys)

    time.sleep(0.25)  # let tqdm's mininterval (0.2 s) pass
    reporter.task_progress(task, 40)
    assert "40%" in _stderr(capsys)

    reporter.task_finished(task, True)
    reporter.run_finished(1, 0)


def test_a_second_run_does_not_keep_the_first_runs_bars(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``batch`` reuses one reporter; the old bars must not leak into job two."""
    reporter = TqdmReporter()
    first = DownloadTask(url="u", target="first.bin", size=10)
    reporter.run_started(1, 10)
    reporter.task_started(first)
    reporter.task_progress(first, 5)
    reporter.task_finished(first, True)
    reporter.run_finished(1, 0)
    _stderr(capsys)

    second = DownloadTask(url="u", target="second.bin", size=10)
    reporter.run_started(1, 10)
    reporter.task_started(second)
    output = _stderr(capsys)
    assert "second.bin" in output
    assert "first.bin" not in output
    reporter.task_finished(second, True)
    reporter.run_finished(1, 0)
