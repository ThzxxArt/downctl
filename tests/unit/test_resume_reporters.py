"""Resume-aware progress reporting and disk pre-flight helpers."""

from __future__ import annotations

import io

import pytest

from downctl.builtin.reporters.json_reporter import JSONReporter
from downctl.builtin.reporters.tqdm_reporter import TqdmReporter
from downctl.core import disk
from downctl.core.errors import DiskSpaceError
from downctl.core.task import DownloadTask


def test_tqdm_reporter_run_resumed_bumps_overall() -> None:
    reporter = TqdmReporter()
    reporter.run_started(1, 100)
    reporter.run_resumed(40)
    assert reporter._overall is not None  # noqa: SLF001
    assert reporter._overall.n == 40  # type: ignore[attr-defined]
    reporter.run_finished(0, 0)


def test_tqdm_reporter_task_resumed_jumps_bar() -> None:
    reporter = TqdmReporter()
    reporter.run_started(1, 100)
    task = DownloadTask(url="u", target="a.bin", size=100)
    reporter.task_started(task)
    reporter.task_resumed(task, 40)
    assert reporter._bars[id(task)].n == 40  # type: ignore[attr-defined]  # noqa: SLF001
    reporter.run_resumed(40)  # no-op path for coverage of guard
    reporter.task_finished(task, True)
    reporter.run_finished(1, 0)


def test_json_reporter_resume_records() -> None:
    stream = io.StringIO()
    reporter = JSONReporter(stream)
    task = DownloadTask(url="u", target="a.bin", size=100)
    reporter.task_started(task)
    reporter.run_resumed(40)
    reporter.task_resumed(task, 40)
    reporter.task_progress(task, 60)
    reporter.task_finished(task, True)
    out = stream.getvalue()
    assert '"event": "run_resume"' in out
    assert '"downloaded": 40' in out
    assert '"event": "resume"' in out
    assert '"event": "finish"' in out


def test_json_reporter_resumed_unknown_size() -> None:
    stream = io.StringIO()
    reporter = JSONReporter(stream)
    task = DownloadTask(url="u", target="a.bin", size=0)
    reporter.task_started(task)
    reporter.task_resumed(task, 1 << 20)
    reporter.task_finished(task, True)
    assert '"event": "resume"' in stream.getvalue()


def test_disk_helpers(tmp_path) -> None:
    assert disk.has_space(str(tmp_path / "x"), 0) is True
    assert disk.free_space(str(tmp_path / "x")) is None or disk.free_space(str(tmp_path / "x")) >= 0
    monkeypatch_free = disk.free_space
    assert monkeypatch_free is not None


def test_disk_ensure_space_raises(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(disk, "has_space", lambda path, need: False)
    with pytest.raises(DiskSpaceError):
        disk.ensure_space(str(tmp_path / "x"), 10)


def test_disk_free_space_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import shutil

    def boom(_path: str) -> object:
        raise OSError("nope")

    monkeypatch.setattr(shutil, "disk_usage", boom)
    assert disk.free_space(str(tmp_path / "x")) is None
    assert disk.has_space(str(tmp_path / "x"), 1 << 30) is True
