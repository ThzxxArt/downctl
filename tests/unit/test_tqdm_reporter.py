"""Tests for the aggregate progress reporter."""

from __future__ import annotations

import pytest

from downctl.builtin.reporters.tqdm_reporter import TqdmReporter
from downctl.core.task import DownloadTask


def test_reporter_lifecycle_with_overall(capsys: pytest.CaptureFixture[str]) -> None:
    reporter = TqdmReporter()
    reporter.run_started(2, 20)
    assert reporter._overall is not None  # noqa: SLF001

    task = DownloadTask(url="u", target="a.bin", size=10)
    reporter.task_started(task)
    reporter.task_progress(task, 5)
    reporter.task_finished(task, True)

    reporter.run_finished(1, 0)
    assert reporter._overall is None  # noqa: SLF001


def test_reporter_skips_overall_when_size_unknown(capsys: pytest.CaptureFixture[str]) -> None:
    reporter = TqdmReporter()
    reporter.run_started(1, 0)
    assert reporter._overall is None  # noqa: SLF001
    reporter.run_finished(0, 1)


def test_reporter_concurrent_has_no_per_task_bar(capsys: pytest.CaptureFixture[str]) -> None:
    reporter = TqdmReporter()
    reporter.run_started(3, 30, jobs=3)
    assert reporter._per_task is False  # noqa: SLF001
    task = DownloadTask(url="u", target="a.bin", size=10)
    reporter.task_started(task)
    assert reporter._bars == {}  # noqa: SLF001
    reporter.task_progress(task, 5)
    reporter.task_finished(task, True)
    reporter.run_finished(1, 0)
    assert reporter._overall is None  # noqa: SLF001
