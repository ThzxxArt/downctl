"""Run/task state behind the progress display."""

from __future__ import annotations

import pytest

from downctl.core.task import DownloadTask
from downctl.progress.model import Rate, RunView, failure_label

MB = 1 << 20


def _task(name: str, size: int) -> DownloadTask:
    return DownloadTask(url=f"https://example.test/{name}", target=name, size=size)


def test_rate_is_a_window_not_a_lifetime_average() -> None:
    rate = Rate(window=3.0)
    rate.add(MB, 0.0)
    rate.add(MB, 1.0)
    assert rate.rate(1.0) == pytest.approx(MB)
    # A stale window is unknown, not "still one megabyte a second".
    assert rate.rate(10.0) is None


def test_rate_needs_two_samples() -> None:
    rate = Rate()
    rate.add(MB, 0.0)
    assert rate.rate(0.0) is None
    assert rate.rate(1.0) is None


def test_run_seed_is_counted_once() -> None:
    view = RunView()
    view.start(1, 10 * MB, 1, now=0.0)
    task = _task("a.bin", 10 * MB)
    view.run_resumed(4 * MB, now=0.0)
    view.task_started(task, now=0.0)
    view.task_resumed(task, 4 * MB, now=0.0)
    view.task_progress(task, MB, now=1.0)
    assert view.done_bytes() == 5 * MB
    assert view.fraction() == pytest.approx(0.5)


def test_unknown_size_has_no_invented_percentage() -> None:
    view = RunView()
    view.start(1, 0, 1, unknown=1, now=0.0)
    task = _task("stream.bin", 0)
    view.task_started(task, 0.0)
    view.task_progress(task, 2 * MB, 1.0)
    assert view.fraction() is None
    assert view.eta(1.0) is None
    assert view.tasks[id(task)].fraction() is None
    assert view.tasks[id(task)].done() == 2 * MB


def test_restart_forgets_bytes_from_the_abandoned_attempt() -> None:
    view = RunView()
    view.start(1, 10 * MB, 1, now=0.0)
    task = _task("a.bin", 10 * MB)
    view.task_started(task, 0.0)
    view.task_progress(task, 6 * MB, 1.0)
    view.task_restarted(task, 2.0)
    view.task_progress(task, 2 * MB, 3.0)
    assert view.tasks[id(task)].done() == 2 * MB


def test_finished_without_bytes_is_skipped() -> None:
    view = RunView()
    view.start(1, 10 * MB, 1, now=0.0)
    task = _task("a.bin", 10 * MB)
    view.task_started(task, 0.0)
    view.task_resumed(task, 10 * MB, 0.0)
    view.task_finished(task, True, 1.0)
    assert view.tasks[id(task)].state == "skipped"
    done, failed, skipped, active, queued = view.counts()
    assert (done, failed, skipped, active, queued) == (0, 0, 1, 0, 0)


def test_zero_byte_download_is_done_not_skipped() -> None:
    view = RunView()
    view.start(1, 0, 1, now=0.0)
    task = _task("empty.bin", 0)
    view.task_started(task, 0.0)
    view.task_finished(task, True, 1.0)
    assert view.tasks[id(task)].state == "done"


def test_remaining_bytes_includes_queued_work_and_excludes_failures() -> None:
    view = RunView()
    view.start(3, 30 * MB, 3, now=0.0)
    first = _task("a.bin", 10 * MB)
    second = _task("b.bin", 10 * MB)
    view.task_started(first, 0.0)
    view.task_progress(first, 4 * MB, 1.0)
    view.task_started(second, 0.0)
    view.task_finished(second, False, 1.0)
    # 6 MB missing from first + 10 MB queued; the failed second file is not
    # work the run can still finish.
    assert view.remaining_bytes() == 16 * MB
    assert view.counts()[1] == 1
    assert view.counts()[4] == 1


def test_eta_uses_the_aggregate_rate() -> None:
    view = RunView()
    view.start(1, 10 * MB, 1, now=0.0)
    task = _task("a.bin", 10 * MB)
    view.task_started(task, 0.0)
    view.task_progress(task, 2 * MB, 0.0)
    view.task_progress(task, 2 * MB, 2.0)
    assert view.eta(2.0) == pytest.approx(6.0)


def test_retrying_state_has_a_countdown() -> None:
    view = RunView()
    view.start(1, 10 * MB, 1, now=0.0)
    task = _task("a.bin", 10 * MB)
    view.task_started(task, 0.0)
    view.task_retrying(task, 3, 5.0, "HTTP 502", 1.0)
    assert view.tasks[id(task)].state == "retrying"
    assert view.tasks[id(task)].retry_seconds(1.5) == 5
    view.task_progress(task, 1, 2.0)
    assert view.tasks[id(task)].state == "active"


def test_failure_label_is_human_readable() -> None:
    assert failure_label("permanent") == "failed"
    assert failure_label("transient-exhausted") == "gave up"
    assert failure_label("timeout") == "timed out"
    assert failure_label("io") == "I/O error"
    assert failure_label("weird") == "weird"


def test_restart_removes_this_files_resume_point_from_the_seed() -> None:
    view = RunView()
    view.start(1, 10 * MB, 1, now=0.0)
    task = _task("a.bin", 10 * MB)
    view.run_resumed(4 * MB, now=0.0)
    view.task_started(task, 0.0)
    view.task_resumed(task, 4 * MB, 0.0)
    view.task_progress(task, MB, 1.0)
    assert view.done_bytes() == 5 * MB
    view.task_restarted(task, 2.0)
    assert view.done_bytes() == 0
    view.task_progress(task, MB, 3.0)
    assert view.done_bytes() == MB
