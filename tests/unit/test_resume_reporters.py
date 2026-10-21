"""Resume-aware progress reporting and disk pre-flight helpers."""

from __future__ import annotations

import io
import re
import time
from pathlib import Path

import pytest

from downctl.builtin.reporters.json_reporter import JSONReporter
from downctl.builtin.reporters.tqdm_reporter import TqdmReporter
from downctl.core import disk
from downctl.core.errors import DiskSpaceError
from downctl.core.task import DownloadTask


def test_tqdm_reporter_run_resumed_bumps_overall(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reporter = TqdmReporter()
    reporter.run_started(1, 100)
    time.sleep(0.12)  # let tqdm's initial mininterval window pass
    reporter.run_resumed(40)
    assert "40.0/100" in capsys.readouterr().err
    reporter.run_finished(0, 0)


def test_json_reporter_survives_a_closed_pipe() -> None:
    class ClosedPipe(io.StringIO):
        write_attempts = 0

        def write(self, text: str) -> int:
            self.write_attempts += 1
            raise BrokenPipeError

    pipe = ClosedPipe()
    reporter = JSONReporter(pipe)
    reporter.run_started(1, 100)
    reporter.run_finished(1, 0)  # second write is silently skipped
    assert pipe.write_attempts == 1, "a broken pipe must be latched, not retried"


def test_tqdm_reporter_task_resumed_jumps_bar(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reporter = TqdmReporter()
    reporter.run_started(1, 100)
    task = DownloadTask(url="u", target="a.bin", size=100)
    reporter.task_started(task)
    capsys.readouterr()  # drop the initial 0 % rendering

    time.sleep(0.25)  # let the per-file bar's 0.2 s mininterval pass
    reporter.task_resumed(task, 40)
    reporter.task_progress(task, 0)  # force a render at the resumed position
    output = capsys.readouterr().err
    assert re.search(r"a\.bin:\s+40%", output), output
    assert "40.0/100" in output

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


def test_json_reporter_redacts_presigned_urls() -> None:
    stream = io.StringIO()
    reporter = JSONReporter(stream)
    task = DownloadTask(
        url="http://s3/b?X-Amz-Signature=SECRET",
        target="b",
        origin="s3://bucket/b",
        meta={"redact_url": True},
    )
    reporter.task_started(task)
    reporter.task_finished(task, False)
    text = stream.getvalue()
    assert "SECRET" not in text
    assert "http://s3/b" in text
    assert "s3://bucket/b" in text


def test_json_reporter_masks_origin_credentials() -> None:
    stream = io.StringIO()
    reporter = JSONReporter(stream)
    task = DownloadTask(
        url="https://h/f",
        target="f",
        origin="https://h/f?token=SUPERSECRET",
    )
    reporter.task_started(task)
    assert "SUPERSECRET" not in stream.getvalue()
    assert "token=***" in stream.getvalue()


def test_disk_helpers(tmp_path: Path) -> None:
    assert disk.has_space(str(tmp_path / "x"), 0) is True
    free = disk.free_space(str(tmp_path / "x"))
    assert free is None or free > 0
    # A tiny requirement fits unless the filesystem is genuinely full.
    assert disk.has_space(str(tmp_path / "x"), 1) is (free is None or free > disk.DISK_RESERVE)
    assert disk.batch_budget([]).fits == ()


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
