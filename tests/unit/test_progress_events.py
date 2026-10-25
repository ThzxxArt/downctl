"""Engine -> display events that the live reporter needs to stay honest."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from downctl.config import Config, CoreConfig
from downctl.core import engine
from downctl.core import pieces as pc
from downctl.core.context import RuntimeContext
from downctl.core.errors import TransportError
from downctl.core.task import DownloadTask
from downctl.core.transfer import TransferResult
from downctl.plugin.loader import discover


class EventReporter:
    name = "events"
    api_version = "1"
    priority = 0

    def __init__(self) -> None:
        self.run_started_kwargs: dict[str, object] = {}
        self.retrying: list[tuple[int, float, str]] = []
        self.phases: list[tuple[str, str]] = []
        self.restarted = 0
        self.resumed: list[int] = []
        self.finished: list[bool] = []

    def run_started(self, total: int, size: int, jobs: int = 1, **kwargs: object) -> None:
        self.run_started_kwargs = {"total": total, "size": size, "jobs": jobs, **kwargs}

    def run_resumed(self, done: int) -> None:
        self.resumed.append(done)

    def task_started(self, task: DownloadTask) -> None:
        pass

    def task_progress(self, task: DownloadTask, delta: int) -> None:
        pass

    def task_finished(self, task: DownloadTask, ok: bool) -> None:
        self.finished.append(ok)

    def task_phase(self, task: DownloadTask, phase: str, detail: str = "") -> None:
        self.phases.append((phase, detail))

    def task_retrying(
        self, task: DownloadTask, attempt: int, delay: float, reason: str = ""
    ) -> None:
        self.retrying.append((attempt, delay, reason))

    def task_restarted(self, task: DownloadTask) -> None:
        self.restarted += 1


def make_ctx(tmp_path: Path, **core: object) -> RuntimeContext:
    return RuntimeContext(
        session=None,  # type: ignore[arg-type]
        config=Config(core=CoreConfig(dir=str(tmp_path), **core)),  # type: ignore[arg-type]
        registry=discover(),
    )


def _task(name: str = "f.bin", size: int = 10) -> DownloadTask:
    return DownloadTask(url="http://h/f", target=name, size=size)


@pytest.mark.asyncio
async def test_transient_failure_reports_a_wait_then_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    async def fake_transfer(*_args: object, **_kwargs: object) -> TransferResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TransportError("boom")
        return TransferResult(complete=True, fetched=True)

    monkeypatch.setattr(engine, "transfer_file", fake_transfer)
    reporter = EventReporter()
    task = _task()
    assert await engine.download_task(make_ctx(tmp_path), task, reporter=reporter, stall_limit=5)
    assert reporter.retrying and reporter.retrying[0][0] == 1
    assert "boom" in reporter.retrying[0][2]
    assert reporter.finished == [True]


@pytest.mark.asyncio
async def test_verification_is_a_visible_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_transfer(*_args: object, **_kwargs: object) -> TransferResult:
        return TransferResult(complete=True, fetched=True)

    async def fake_verify(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(engine, "transfer_file", fake_transfer)
    monkeypatch.setattr(engine, "_verify", fake_verify)
    reporter = EventReporter()
    task = _task()
    task.checksum_algo = "sha256"
    task.checksum_hex = "a" * 64
    assert await engine.download_task(make_ctx(tmp_path), task, reporter=reporter, stall_limit=5)
    assert ("verify", "sha256") in reporter.phases


@pytest.mark.asyncio
async def test_a_checksum_mismatch_reports_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_transfer(*_args: object, **_kwargs: object) -> TransferResult:
        return TransferResult(complete=True, fetched=True)

    answers: Iterator[bool] = iter([False, True])

    async def fake_verify(*_args: object, **_kwargs: object) -> bool:
        return next(answers)

    monkeypatch.setattr(engine, "transfer_file", fake_transfer)
    monkeypatch.setattr(engine, "_verify", fake_verify)
    reporter = EventReporter()
    task = _task()
    task.checksum_algo = "sha256"
    task.checksum_hex = "a" * 64
    assert await engine.download_task(make_ctx(tmp_path), task, reporter=reporter, stall_limit=5)
    assert reporter.restarted >= 1


@pytest.mark.asyncio
async def test_skipped_file_is_marked_for_the_reporter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"0123456789")
    task = _task()
    identity = engine._task_identity(task)
    assert pc.save_completion(str(target), identity, "", "", validator="")
    reporter = EventReporter()
    assert await engine.download_task(
        make_ctx(tmp_path),
        task,
        reporter=reporter,
        skip_existing=True,
        stall_limit=5,
    )
    assert task.meta.get("skipped") is True
    assert reporter.finished == [True]


@pytest.mark.asyncio
async def test_run_started_carries_unknown_count_and_connection_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_download(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(engine, "download_task", fake_download)
    reporter = EventReporter()
    tasks = [_task("a.bin", 10), _task("b.bin", 0)]
    ok, failed = await engine.download_all(
        make_ctx(tmp_path), tasks, jobs=2, reporter=reporter, connections=7
    )
    assert (ok, failed) == (2, 0)
    assert reporter.run_started_kwargs["unknown"] == 1
    assert reporter.run_started_kwargs["connections"] == 7


@pytest.mark.asyncio
async def test_resume_seed_obeys_require_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, object]] = []

    def fake_completed(*_args: object, **kwargs: object) -> int:
        seen.append(dict(kwargs))
        return 5

    async def fake_download(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(engine, "_completed_bytes", fake_completed)
    monkeypatch.setattr(engine, "download_task", fake_download)
    reporter = EventReporter()
    ctx = make_ctx(tmp_path, require_witness=True)
    await engine.download_all(ctx, [_task()], reporter=reporter)
    assert reporter.resumed == [5]
    assert seen and seen[0]["require_witness"] is True
