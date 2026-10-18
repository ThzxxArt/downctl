"""Resilience: probe retries, server pacing, batch disk budget, concurrency.

These are the 0.1.4 "scale and network manners" guarantees. Each test names the
concrete failure it prevents:

- a single 5xx during probing used to be reported as a permanent failure;
- a permanent 5xx used to be retried forever with no deadline;
- a batch used to start writing before anyone checked that it could ever fit;
- target expansion was serial, so N targets cost N round trips before the
  first byte.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio

from downctl import app, console
from downctl.builtin.reporters.quiet_reporter import QuietReporter
from downctl.config import Config, CoreConfig
from downctl.core import disk
from downctl.core import pieces as pc
from downctl.core.context import RuntimeContext
from downctl.core.engine import download_all, download_task
from downctl.core.errors import FAILURE_PERMANENT, FAILURE_TRANSIENT_EXHAUSTED
from downctl.core.task import DownloadTask
from downctl.plugin.loader import discover
from downctl.probe import probe_metadata


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "does-not-exist.toml"))
    console.set_machine_readable(False)


def make_ctx(session: aiohttp.ClientSession, tmp_path: Path, **core: object) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path), **core)),  # type: ignore[arg-type]
        registry=discover(),
    )


def make_args(**overrides: object) -> argparse.Namespace:
    namespace = app._default_get_args()
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


# --------------------------------------------------------------------------- #
# probe-stage failure classification (T10a)
# --------------------------------------------------------------------------- #
async def test_a_transient_probe_failure_is_retried(file_server, tmp_path: Path) -> None:
    """Two failing requests, then success: the target must still be downloaded."""
    file_server.state.flaky_failures = 2
    destination = tmp_path / "dl"
    args = make_args(
        targets=[file_server.url("flaky-then-ok")], dir=str(destination), reporter="quiet"
    )
    assert await app._get(args) == 0
    assert (destination / "flaky-then-ok").read_bytes() == file_server.data
    assert file_server.state.count("flaky-then-ok") >= 3
    assert not (destination / ".downctl.failed").exists()


async def test_a_permanent_probe_failure_is_not_retried(file_server, tmp_path: Path) -> None:
    destination = tmp_path / "dl"
    args = make_args(
        targets=[file_server.url("always-404")],
        dir=str(destination),
        reporter="quiet",
        continue_=True,
    )
    assert await app._get(args) == 1
    assert file_server.state.count("always-404") == 1, "a 404 must not be retried"
    assert "always-404" in (destination / ".downctl.failed").read_text(encoding="utf-8")


async def test_probe_retries_stay_bounded(file_server, tmp_path: Path) -> None:
    """``-r 0`` means "retry the download forever"; probing is always bounded."""
    file_server.state.flaky_failures = 99
    destination = tmp_path / "dl"
    args = make_args(
        targets=[file_server.url("flaky-then-ok")],
        dir=str(destination),
        reporter="quiet",
        retries=0,
        continue_=True,
    )
    assert await app._get(args) == 1
    # Each bounded attempt is a HEAD plus the ranged-GET fallback a 5xx triggers.
    assert file_server.state.count("flaky-then-ok") == app.PROBE_ATTEMPT_LIMIT * 2


# --------------------------------------------------------------------------- #
# server pacing and deadlines (T10b)
# --------------------------------------------------------------------------- #
async def test_retry_after_is_honoured(file_server, tmp_path: Path) -> None:
    file_server.state.throttled = True
    file_server.state.retry_after = 1.0
    destination = tmp_path / "dl"
    args = make_args(targets=[file_server.url("throttle")], dir=str(destination), reporter="quiet")

    started = time.monotonic()
    assert await app._get(args) == 0
    waited = time.monotonic() - started
    assert waited >= 0.9, f"Retry-After was ignored (waited {waited:.2f}s)"
    assert (destination / "throttle").read_bytes() == file_server.data


async def test_max_time_gives_up_and_records_the_target(file_server, tmp_path: Path) -> None:
    file_server.state.chunk_delay = 0.1
    destination = tmp_path / "dl"
    args = make_args(
        targets=[file_server.url("slow")],
        dir=str(destination),
        reporter="quiet",
        max_time=1,
        connections=2,
        piece_size="1M",
    )

    started = time.monotonic()
    assert await app._get(args) == 1
    elapsed = time.monotonic() - started
    assert 0.9 <= elapsed < 8, f"--max-time 1 took {elapsed:.2f}s"
    assert "slow" in (destination / ".downctl.failed").read_text(encoding="utf-8")
    # The partial work is still resumable: nothing was deleted.
    assert (destination / "slow.downctl-pieces").exists()


async def test_a_transient_transfer_failure_still_honours_retry_after(
    file_server, session, tmp_path: Path
) -> None:
    """Retry-After on the *transfer* path is honoured too."""
    import downctl.core.engine as engine

    calls: list[float | None] = []
    real_sleep = asyncio.sleep

    async def record_sleep(delay: float) -> None:
        calls.append(delay)
        await real_sleep(0)

    task = DownloadTask(url=file_server.url("throttle"), target="t.bin", size=0)
    task.meta["path"] = str(tmp_path / "t.bin")
    ctx = make_ctx(session, tmp_path, retries=1)

    async def flaky_transfer(*_args: object, **_kwargs: object) -> object:
        from downctl.core.errors import TransientHTTPError

        raise TransientHTTPError(429, "http://h/f", retry_after=3.0)

    engine_original = engine.transfer_file
    engine.transfer_file = flaky_transfer  # type: ignore[assignment]
    try:
        import downctl.core.engine as patched

        original_sleep = patched.asyncio.sleep
        patched.asyncio.sleep = record_sleep  # type: ignore[assignment]
        try:
            assert not await download_task(ctx, task, reporter=QuietReporter(), stall_limit=5)
        finally:
            patched.asyncio.sleep = original_sleep  # type: ignore[assignment]
    finally:
        engine.transfer_file = engine_original  # type: ignore[assignment]
    assert calls == [3.0], f"Retry-After was not used for pacing: {calls}"


# --------------------------------------------------------------------------- #
# batch disk budget (T9)
# --------------------------------------------------------------------------- #
async def test_a_batch_that_cannot_fit_never_starts_writing(
    file_server,
    session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(disk, "free_space", lambda path: 4096)
    ctx = make_ctx(session, tmp_path)
    tasks = [
        DownloadTask(
            url=file_server.url("file"), target=f"f{index}.bin", size=len(file_server.data)
        )
        for index in range(4)
    ]
    outcomes: list[tuple[str, bool]] = []
    ok, failed = await download_all(
        ctx,
        tasks,
        jobs=4,
        reporter=QuietReporter(),
        connections=2,
        stall_limit=5,
        on_result=lambda task, success: outcomes.append((task.target, success)),
    )

    assert (ok, failed) == (0, 4)
    assert all(not success for _target, success in outcomes)
    assert file_server.ranges == [], "no byte may be fetched for an infeasible batch"
    assert len(outcomes) == 4
    error = capsys.readouterr().err
    assert "not enough free space" in error
    assert "need" in error and "free" in error


async def test_a_batch_that_fits_runs_normally(file_server, session, tmp_path: Path) -> None:
    ctx = make_ctx(session, tmp_path)
    tasks = [
        DownloadTask(
            url=file_server.url("file"), target=f"ok{index}.bin", size=len(file_server.data)
        )
        for index in range(2)
    ]
    ok, failed = await download_all(
        ctx, tasks, jobs=2, reporter=QuietReporter(), connections=2, stall_limit=5
    )
    assert (ok, failed) == (2, 0)
    for index in range(2):
        assert (tmp_path / f"ok{index}.bin").read_bytes() == file_server.data


def test_batch_budget_bills_a_repeated_path_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two targets that resolve to one file write it once, so bill it once."""
    monkeypatch.setattr(disk, "free_space", lambda path: 100)
    same = str(tmp_path / "one.bin")
    assert disk.batch_budget([(same, 60)], reserve=0).fits == (True,)
    # A mirror and its origin (or --retry-failed next to the original) resolve to
    # the same path; that must not be reported as two files' worth of space.
    assert disk.batch_budget([(same, 60), (same, 60)], reserve=0).fits == (True, True)
    assert disk.batch_budget([(same, 30), (same, 60)], reserve=0).fits == (True, True)
    # Two *different* files that do not fit together are still refused.
    other = str(tmp_path / "two.bin")
    assert disk.batch_budget([(same, 60), (other, 60)], reserve=0).fits == (False, False)


def test_batch_budget_groups_by_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(disk, "free_space", lambda path: 100)
    monkeypatch.setattr(disk, "_device_key", lambda path: path.split("/")[1])
    budget = disk.batch_budget(
        [("/a/one.bin", 60), ("/a/two.bin", 60), ("/b/three.bin", 40)], reserve=0
    )
    # Device "a" needs 120 of its 100 free bytes; device "b" needs 40.
    assert budget.fits == (False, False, True)
    assert len(budget.shortfalls) == 1
    assert budget.shortfalls[0].needed == 120
    assert "120 B" in budget.message and "100 B" in budget.message


def test_batch_budget_is_a_no_op_without_needs() -> None:
    budget = disk.batch_budget([])
    assert budget.fits == ()
    assert budget.message == ""
    assert disk.batch_budget([("x", 0), ("y", -5)]).fits == (True, True)


def test_batch_budget_gives_up_when_the_free_space_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(disk, "free_space", lambda path: None)
    assert disk.batch_budget([("/x/a.bin", 10**12)]).fits == (True,)


# --------------------------------------------------------------------------- #
# bounded concurrency and deterministic reporting (T8)
# --------------------------------------------------------------------------- #
async def test_bounded_map_limits_concurrency_and_keeps_order() -> None:
    running = 0
    peak = 0

    async def action(item: int) -> int:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        if item == 2:
            raise RuntimeError("boom")
        return item * 10

    results = await app._bounded_map(list(range(6)), 2, action)
    assert peak <= 2
    assert [result for result in results if not isinstance(result, BaseException)] == [
        0,
        10,
        30,
        40,
        50,
    ]
    assert isinstance(results[2], RuntimeError)


async def test_bounded_map_runs_sequentially_when_limited_to_one() -> None:
    seen: list[int] = []

    async def action(item: int) -> int:
        seen.append(item)
        return item

    assert await app._bounded_map([1, 2, 3], 1, action) == [1, 2, 3]
    assert seen == [1, 2, 3]


async def test_probe_phase_is_concurrent(file_server, tmp_path: Path) -> None:
    """N targets must not cost N *sequential* round trips.

    The server delays every HEAD, so the wall clock separates a concurrent phase
    from a serial one: six 0.3 s probes cost ~1.8 s sequentially and ~0.3 s with
    ``--probe-jobs 6``. The earlier version of this test used an instant endpoint
    and could not fail.
    """
    file_server.state.head_delay = 0.3
    targets = [file_server.url(f"slow-head?i={index}") for index in range(6)]

    parallel = make_args(
        targets=targets,
        dir=str(tmp_path / "dl"),
        reporter="quiet",
        dry_run=True,
        probe_jobs=6,
    )
    started = time.monotonic()
    assert await app._get(parallel) == 0
    overlapped = time.monotonic() - started
    assert overlapped < 1.0, f"probes did not overlap ({overlapped:.2f}s for 6x0.3s)"

    serial = make_args(
        targets=targets,
        dir=str(tmp_path / "dl2"),
        reporter="quiet",
        dry_run=True,
        probe_jobs=1,
    )
    started = time.monotonic()
    assert await app._get(serial) == 0
    one_at_a_time = time.monotonic() - started
    assert one_at_a_time > 1.4, f"probe_jobs=1 did not serialize ({one_at_a_time:.2f}s)"


async def test_a_head_rejected_with_403_is_probed_with_a_ranged_get(
    file_server, session, tmp_path: Path
) -> None:
    """A method-scoped 403 says nothing about the object.

    An S3 presigned URL signs the HTTP method, so a GET-signed link answers HEAD
    with 403 while the ranged GET the transfer uses succeeds. Reporting that as a
    dead target would break every presigned download that needs a witness.
    """
    url = file_server.url("head-forbidden")
    result = await probe_metadata(session, url)
    assert result.ok
    assert result.size == len(file_server.data)
    assert result.validator == '"v1"'

    destination = tmp_path / "dl"
    args = make_args(targets=[url], dir=str(destination), reporter="quiet")
    assert await app._get(args) == 0
    assert (destination / "head-forbidden").read_bytes() == file_server.data
    marker = pc.load_completion(str(destination / "head-forbidden"))
    assert marker is not None and marker["validator"] == '"v1"'


async def test_failure_order_follows_the_command_line(file_server, tmp_path: Path) -> None:
    """Concurrency must not shuffle which target is reported failing first."""
    targets = [
        file_server.url("always-404"),
        file_server.url("file"),
        file_server.url("always-404?second=1"),
    ]
    args = make_args(
        targets=targets,
        dir=str(tmp_path / "dl"),
        reporter="quiet",
        continue_=True,
        probe_jobs=3,
    )
    assert await app._get(args) == 1
    lines = [
        line
        for line in (tmp_path / "dl" / ".downctl.failed").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(lines) == 2
    assert "always-404" in lines[0]
    assert "second=1" in lines[1]


# --------------------------------------------------------------------------- #
# why a target failed, and what a success rests on (T10a / T6)
# --------------------------------------------------------------------------- #
async def test_the_failed_log_says_whether_a_retry_is_worth_it(file_server, tmp_path: Path) -> None:
    """A 404 and a dead 5xx are both failures; only one deserves a retry."""
    destination = tmp_path / "dl"
    args = make_args(
        targets=[file_server.url("always-404")],
        dir=str(destination),
        reporter="quiet",
        continue_=True,
    )
    assert await app._get(args) == 1
    line = (destination / ".downctl.failed").read_text(encoding="utf-8").strip()
    assert line.split("\t")[8] == FAILURE_PERMANENT


async def test_an_exhausted_probe_is_recorded_as_transient_exhausted(
    file_server, tmp_path: Path
) -> None:
    """The retryable class is the whole point of the ninth column."""
    file_server.state.flaky_failures = 99
    destination = tmp_path / "dl"
    args = make_args(
        targets=[file_server.url("flaky-then-ok")],
        dir=str(destination),
        reporter="quiet",
        retries=0,
        continue_=True,
    )
    assert await app._get(args) == 1
    line = (destination / ".downctl.failed").read_text(encoding="utf-8").strip()
    assert line.split("\t")[8] == FAILURE_TRANSIENT_EXHAUSTED


async def test_json_says_why_a_target_failed(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A CI job must not have to parse ``error`` to know whether to retry."""
    destination = tmp_path / "dl"
    args = make_args(
        targets=[file_server.url("always-404")],
        dir=str(destination),
        reporter="json",
        continue_=True,
    )
    assert await app._get(args) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    failure = [record for record in records if record.get("event") == "failure"]
    assert failure and failure[0]["reason"] == FAILURE_PERMANENT


async def test_a_json_finish_record_says_what_the_success_rests_on(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``ok: true`` with no witness is an unchecked claim, not a proof."""
    destination = tmp_path / "dl"
    args = make_args(targets=[file_server.url("versioned")], dir=str(destination), reporter="json")
    assert await app._get(args) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    finish = [record for record in records if record.get("event") == "finish"]
    assert finish and finish[0]["ok"] is True
    assert finish[0]["witness"] == "validator"


async def test_an_unversioned_finish_reports_the_none_witness(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The record must not dress an unverifiable download up as proven."""
    destination = tmp_path / "dl"
    args = make_args(targets=[file_server.url("no-etag")], dir=str(destination), reporter="json")
    assert await app._get(args) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    finish = [record for record in records if record.get("event") == "finish"]
    assert finish and finish[0]["witness"] == "none"


async def test_a_json_skip_reports_the_witness_it_was_skipped_with(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--skip-existing`` is where ``ok: true`` is least trustworthy."""
    destination = tmp_path / "dl"
    first = make_args(
        targets=[file_server.url("versioned")], dir=str(destination), reporter="quiet"
    )
    assert await app._get(first) == 0
    capsys.readouterr()

    second = make_args(
        targets=[file_server.url("versioned")],
        dir=str(destination),
        reporter="json",
        skip_existing=True,
    )
    assert await app._get(second) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    finish = [record for record in records if record.get("event") == "finish"]
    assert finish and finish[0]["ok"] is True
    assert finish[0]["witness"] == "validator"


async def test_max_time_covers_the_probe_stage_too(file_server, tmp_path: Path) -> None:
    """A probe against a server that never answers is capped as well.

    Before this fix the probe ignored ``--max-time`` entirely (measured:
    ``--max-time 3`` exited after 11.2s), so one silent host could hold a batch
    for as long as its socket lived.
    """
    file_server.state.head_delay = 5.0
    destination = tmp_path / "dl"
    args = make_args(
        targets=[file_server.url("slow-head")],
        dir=str(destination),
        reporter="quiet",
        max_time=1,
        continue_=True,
    )
    started = time.monotonic()
    assert await app._get(args) == 1
    elapsed = time.monotonic() - started
    assert elapsed < 3.5, f"--max-time 1 took {elapsed:.2f}s to give up"
    assert "slow-head" in (destination / ".downctl.failed").read_text(encoding="utf-8")


async def test_the_transfer_inherits_what_the_probe_did_not_spend(
    file_server, tmp_path: Path
) -> None:
    """``--max-time`` caps the *target*, so the probe's share comes out of it.

    The fixture is sized so the two policies disagree: the probe takes ~0.7s and
    the body needs ~0.5s of a 1s budget. A shared budget runs out (exit 1); a
    fresh budget per phase would fit and exit 0.
    """
    file_server.state.head_delay = 0.7
    file_server.state.chunk_delay = 0.125
    destination = tmp_path / "dl"
    args = make_args(
        targets=[file_server.url("budget")],
        dir=str(destination),
        reporter="quiet",
        max_time=1,
        connections=1,
        piece_size="1M",
    )
    started = time.monotonic()
    assert await app._get(args) == 1, "the transfer was handed a fresh budget"
    elapsed = time.monotonic() - started
    assert elapsed < 2.5, f"--max-time 1 took {elapsed:.2f}s to give up"
    assert "budget" in (destination / ".downctl.failed").read_text(encoding="utf-8")
