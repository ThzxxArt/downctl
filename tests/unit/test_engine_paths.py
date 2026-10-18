"""Engine branches that only fire when something goes wrong.

``core/engine.py`` decides whether a file counts as finished, so its *failure*
branches are exactly the ones that must never quietly do the wrong thing: an
unreachable retry, an unverifiable skip, a consent to overwrite. These tests
drive each branch directly instead of hoping an integration path reaches it.
"""

from __future__ import annotations

import asyncio
import itertools
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from downctl.config import Config, CoreConfig
from downctl.core import engine
from downctl.core import pieces as pc
from downctl.core.context import RuntimeContext
from downctl.core.errors import (
    ChecksumError,
    ContentChangedError,
    DiskSpaceError,
    DownctlError,
    PermanentHTTPError,
    RangeNotSatisfiableError,
    TransientHTTPError,
    TransportError,
)
from downctl.core.task import DownloadTask
from downctl.core.transfer import TransferResult
from downctl.plugin.loader import discover
from downctl.probe import ProbeResult
from downctl.urls import url_identity


class RecordingReporter:
    """Reporter that remembers everything the engine told it."""

    def __init__(self) -> None:
        self.started: list[DownloadTask] = []
        self.finished: list[bool] = []
        self.resumed: list[int] = []
        self.progress = 0
        self.task_resumed_calls: list[int] = []

    def task_started(self, task: DownloadTask) -> None:
        self.started.append(task)

    def task_progress(self, task: DownloadTask, delta: int) -> None:
        self.progress += delta

    def task_finished(self, task: DownloadTask, ok: bool) -> None:
        self.finished.append(ok)

    def run_resumed(self, done_bytes: int) -> None:
        self.resumed.append(done_bytes)

    def task_resumed(self, task: DownloadTask, done: int) -> None:
        self.task_resumed_calls.append(done)


def make_ctx(tmp_path: Path, **core: object) -> RuntimeContext:
    return RuntimeContext(
        session=None,  # type: ignore[arg-type]
        config=Config(core=CoreConfig(dir=str(tmp_path), **core)),  # type: ignore[arg-type]
        registry=discover(),
    )


def patch_transfer(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: Any | Iterator[Any],
) -> list[dict[str, Any]]:
    """Replace ``transfer_file`` with a scripted sequence of results/raises.

    A single outcome is repeated (the retry loop may call it more than once);
    pass an explicit iterator to script a sequence.
    """
    calls: list[dict[str, Any]] = []
    iterator = outcomes if isinstance(outcomes, Iterator) else itertools.repeat(outcomes)

    async def fake(*args: Any, **kwargs: Any) -> TransferResult:
        calls.append(kwargs)
        outcome = next(iterator)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome

    monkeypatch.setattr(engine, "transfer_file", fake)
    return calls


def task_for(tmp_path: Path, name: str = "f.bin", **kwargs: Any) -> DownloadTask:
    return DownloadTask(url="http://h/f", target=name, **kwargs)


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_completion_proof_truthiness() -> None:
    assert bool(engine.CompletionProof(True, "none"))
    assert not engine.CompletionProof(False)
    assert engine.CompletionProof(False).witness == "incomplete"


def test_resolve_target_rejects_a_cross_drive_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> str:
        raise ValueError("different drives")

    monkeypatch.setattr(engine.os.path, "commonpath", boom)
    with pytest.raises(DownctlError, match="escapes the destination"):
        engine._resolve_target(make_ctx(tmp_path), task_for(tmp_path, "sub/f.bin"))


def test_get_verifier_returns_none_without_a_registry() -> None:
    ctx = RuntimeContext(session=None, config=Config())  # type: ignore[arg-type]
    assert engine._get_verifier(ctx, "sha256") is None
    assert engine._get_verifier(ctx, "") is None


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ([ContentChangedError("u")], "changed"),
        ([RangeNotSatisfiableError("u")], "range"),
        ([PermanentHTTPError(404, "u")], "permanent"),
        ([TransportError("x")], "transport"),
        ([OSError("x")], "os"),
        ([ValueError("x")], "other"),
    ],
)
def test_transfer_error_classification(row: list[BaseException], expected: str) -> None:
    from downctl.core.transfer import _classify

    assert _classify(row) == expected


# --------------------------------------------------------------------------- #
# byte accounting
# --------------------------------------------------------------------------- #
def test_completed_bytes_counts_a_hole_free_file(tmp_path: Path) -> None:
    target = tmp_path / "full.bin"
    target.write_bytes(b"z" * 4096)
    task = task_for(tmp_path, "full.bin", size=4096)
    if os.name == "nt" or not hasattr(os, "SEEK_DATA"):
        # Documented distrust: where a hole cannot be told from data, an
        # unmarked full-size file does not count as progress.
        assert engine._completed_bytes(str(target), task, 4) == 0
    else:
        assert engine._completed_bytes(str(target), task, 4) == 4096


def test_remaining_bytes_treats_a_foreign_marker_as_nothing(tmp_path: Path) -> None:
    target = tmp_path / "marked.bin"
    target.write_bytes(b"z" * 100)
    assert pc.save_completion(str(target), url_identity("http://other/x"))
    task = task_for(tmp_path, "marked.bin", size=100)
    assert engine._remaining_bytes(str(target), task, 4) == 100
    assert engine._completed_bytes(str(target), task, 4) == 0


def test_prove_complete_refuses_a_holed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full-size file whose holes cannot be ruled out is not proof.

    Hole detection itself is filesystem-dependent (this development box's
    tmpfs reports a truncated file as hole-free), so the *engine* decision is
    tested here by pinning the predicate it asks.
    """
    target = tmp_path / "sparse.bin"
    target.write_bytes(b"x" * 4096)
    monkeypatch.setattr(engine.piece_store, "trusted_complete", lambda path, size: False)
    task = task_for(tmp_path, "sparse.bin", size=4096)
    proof = engine._prove_complete(str(target), task, url_identity(task.url))
    assert not proof
    assert proof.witness == "incomplete"


def test_prove_complete_without_a_size_needs_a_marker(tmp_path: Path) -> None:
    target = tmp_path / "unknown.bin"
    target.write_bytes(b"z" * 100)
    task = task_for(tmp_path, "unknown.bin")
    assert not engine._prove_complete(str(target), task, url_identity(task.url))
    assert pc.save_completion(str(target), url_identity(task.url), validator='"v1"')
    proof = engine._prove_complete(str(target), task, url_identity(task.url), validator='"v1"')
    assert proof and proof.witness == "validator"


def test_prove_complete_labels_a_complete_bitmap(tmp_path: Path) -> None:
    target = tmp_path / "complete.bin"
    target.write_bytes(b"z" * 100)
    identity = url_identity("http://h/f")
    assert pc.save(
        str(target),
        pc.Bitmap(100, 4, [1, 1, 1, 1], 100, 25, url_sha256=identity, validator='"v1"'),
    )
    task = task_for(tmp_path, "complete.bin", size=100)
    proof = engine._prove_complete(str(target), task, identity, validator='"v1"')
    assert proof and proof.witness == "validator"


# --------------------------------------------------------------------------- #
# pre-flight failures
# --------------------------------------------------------------------------- #
async def test_lock_conflict_fails_the_task(tmp_path: Path) -> None:
    target = tmp_path / "locked.bin"
    target.write_bytes(b"x")
    lock_dir = Path(str(target) + ".lock")
    lock_dir.mkdir()
    (lock_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")

    reporter = RecordingReporter()
    ctx = make_ctx(tmp_path)
    assert not await engine.download_task(
        ctx, task_for(tmp_path, "locked.bin", size=1), reporter=reporter
    )
    assert reporter.finished == [False]


async def test_checksum_without_an_algorithm_fails_before_fetching(tmp_path: Path) -> None:
    reporter = RecordingReporter()
    task = task_for(tmp_path, size=10, checksum_hex="0" * 64)
    assert not await engine.download_task(make_ctx(tmp_path), task, reporter=reporter)
    assert reporter.started and reporter.finished == [False]
    assert not (tmp_path / "f.bin").exists()


async def test_a_verifier_that_cannot_run_fails_the_skip(tmp_path: Path) -> None:
    target = tmp_path / "skip.bin"
    target.write_bytes(b"z" * 100)
    task = task_for(
        tmp_path,
        "skip.bin",
        size=100,
        checksum_algo="sha256",
        checksum_hex="a" * 64,
        meta={"user_checksum": True},
    )
    assert pc.save_completion(str(target), url_identity(task.url))

    async def broken(*_args: object, **_kwargs: object) -> bool:
        raise ChecksumError("cannot read the file")

    reporter = RecordingReporter()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(engine, "_verify", broken)
    try:
        assert not await engine.download_task(
            make_ctx(tmp_path), task, reporter=reporter, skip_existing=True
        )
    finally:
        monkeypatch.undo()
    assert reporter.finished == [False]


async def test_a_skip_whose_marker_cannot_be_written_still_succeeds(tmp_path: Path) -> None:
    target = tmp_path / "skip.bin"
    target.write_bytes(b"z" * 100)
    task = task_for(tmp_path, "skip.bin", size=100)
    assert pc.save_completion(str(target), url_identity(task.url))

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(engine.piece_store, "save_completion", lambda *a, **k: False)
    try:
        reporter = RecordingReporter()
        assert await engine.download_task(
            make_ctx(tmp_path), task, reporter=reporter, skip_existing=True
        )
    finally:
        monkeypatch.undo()
    assert reporter.finished == [True]


# --------------------------------------------------------------------------- #
# the retry loop
# --------------------------------------------------------------------------- #
async def test_a_failing_reporter_does_not_leak_the_lock(tmp_path: Path) -> None:
    """The target lock is released even when reporting explodes.

    The lock used to be released only on the skip path's own success and
    ``ChecksumError`` branches, so an exception from a reporter (or a hook, or a
    third-party verifier) left ``<target>.lock`` behind and failed the next run
    for that target.
    """
    target = tmp_path / "skip.bin"
    target.write_bytes(b"z" * 100)
    task = task_for(tmp_path, "skip.bin", size=100)
    assert pc.save_completion(str(target), url_identity(task.url))

    class Exploding(RecordingReporter):
        def task_finished(self, task: DownloadTask, ok: bool) -> None:
            raise RuntimeError("reporter exploded")

    with pytest.raises(RuntimeError):
        await engine.download_task(
            make_ctx(tmp_path), task, reporter=Exploding(), skip_existing=True
        )
    assert not Path(str(target) + ".lock").exists()

    # The same target is usable again in this process.
    assert await engine.download_task(
        make_ctx(tmp_path), task, reporter=RecordingReporter(), skip_existing=True
    )


async def test_a_failing_verifier_does_not_leak_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "verify.bin"
    target.write_bytes(b"z" * 100)
    task = task_for(
        tmp_path,
        "verify.bin",
        size=100,
        checksum_algo="sha256",
        checksum_hex="a" * 64,
        meta={"user_checksum": True},
    )
    assert pc.save_completion(str(target), url_identity(task.url))

    async def boom(*_args: object, **_kwargs: object) -> bool:
        raise ValueError("verifier misbehaved")

    monkeypatch.setattr(engine, "_verify", boom)
    with pytest.raises(ValueError):
        await engine.download_task(make_ctx(tmp_path), task, skip_existing=True)
    assert not Path(str(target) + ".lock").exists()


async def test_resume_notifies_a_resume_aware_reporter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"q" * 4096
    target = tmp_path / "resumed.bin"
    target.write_bytes(data[:2048])
    identity = url_identity("http://h/f")
    piece_size, pieces, _ = pc.layout(len(data), 1, 2048)
    assert pc.save(
        str(target),
        pc.Bitmap(
            len(data), pieces, [1, 0], 2048, piece_size, url_sha256=identity, validator='"v1"'
        ),
    )
    patch_transfer(monkeypatch, TransferResult(complete=False))
    task = task_for(tmp_path, "resumed.bin", size=len(data))
    task.meta["validator"] = '"v1"'
    reporter = RecordingReporter()
    await engine.download_task(
        make_ctx(tmp_path), task, reporter=reporter, retries=1, stall_limit=5
    )
    assert reporter.task_resumed_calls == [2048]


async def test_disk_full_before_an_attempt_fails_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "_has_space", lambda path, need: False)
    reporter = RecordingReporter()
    assert not await engine.download_task(
        make_ctx(tmp_path), task_for(tmp_path, size=10), reporter=reporter
    )
    assert reporter.finished == [False]


async def test_a_target_that_runs_out_of_time_gives_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transfer(monkeypatch, TransientHTTPError(503, "http://h/f"))
    monkeypatch.setattr(engine, "retry_delay", lambda attempt, retry_after=None: 5.0)
    reporter = RecordingReporter()
    ok = await engine.download_task(
        make_ctx(tmp_path, max_time=1),
        task_for(tmp_path, size=10),
        reporter=reporter,
        stall_limit=5,
    )
    assert not ok
    assert reporter.finished == [False]


async def test_a_timeout_without_a_deadline_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transfer(monkeypatch, TimeoutError())
    with pytest.raises(TimeoutError):
        await engine.download_task(make_ctx(tmp_path), task_for(tmp_path, size=10))


async def test_too_many_restarts_give_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transfer(monkeypatch, ContentChangedError("http://h/f", '"v2"', '"v2"'))

    async def settled(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(engine, "_witness_changed_again", settled)
    reporter = RecordingReporter()
    task = task_for(tmp_path, size=10)
    task.meta["validator"] = '"v1"'
    assert not await engine.download_task(
        make_ctx(tmp_path), task, reporter=reporter, stall_limit=5
    )
    assert reporter.finished == [False]


async def test_a_witness_that_never_settles_gives_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transfer(monkeypatch, ContentChangedError("http://h/f", '"v2"', '"v2"'))

    async def always_changing(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(engine, "_witness_changed_again", always_changing)
    reporter = RecordingReporter()
    assert not await engine.download_task(
        make_ctx(tmp_path), task_for(tmp_path, size=10), reporter=reporter, stall_limit=5
    )
    assert reporter.finished == [False]


async def test_a_content_change_reuses_the_new_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = patch_transfer(
        monkeypatch,
        iter([ContentChangedError("http://h/f", '"v2"', '"v2"'), TransferResult(complete=True)]),
    )

    async def settled(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(engine, "_witness_changed_again", settled)
    monkeypatch.setattr(engine, "_verify", lambda *a, **k: _true())
    task = task_for(tmp_path, size=10)
    task.meta["validator"] = '"v1"'
    assert await engine.download_task(
        make_ctx(tmp_path), task, reporter=RecordingReporter(), stall_limit=5
    )
    assert task.meta["if_range"] == '"v2"'
    assert calls[1]["validator"] == '"v2"'


async def _true() -> bool:
    return True


async def test_a_content_change_clears_a_dropped_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that stopped offering a validator must not keep receiving one.

    The restart used to update ``if_range`` only when the new value was
    non-empty, so a dropped witness kept sending the previous revision's header
    and caused one more pointless restart.
    """
    calls = patch_transfer(
        monkeypatch,
        iter([ContentChangedError("http://h/f", '"v2"', ""), TransferResult(complete=True)]),
    )

    async def settled(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(engine, "_witness_changed_again", settled)
    monkeypatch.setattr(engine, "_verify", lambda *a, **k: _true())
    task = task_for(tmp_path, size=10)
    task.meta["validator"] = '"v1"'
    task.meta["if_range"] = '"v1"'
    assert await engine.download_task(
        make_ctx(tmp_path), task, reporter=RecordingReporter(), stall_limit=5
    )
    assert task.meta["if_range"] == ""
    assert calls[1]["if_range"] == ""


async def test_416_reprobes_and_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = patch_transfer(
        monkeypatch,
        iter([RangeNotSatisfiableError("http://h/f", "bytes=9-9"), TransferResult(complete=True)]),
    )
    probes: list[str] = []

    async def fake_probe(session: object, url: str) -> ProbeResult:
        probes.append(url)
        return ProbeResult(size=8, status=200, validator='"v1"', if_range='"v1"', reached=True)

    monkeypatch.setattr(engine, "probe_metadata", fake_probe)
    monkeypatch.setattr(engine, "_verify", lambda *a, **k: _true())
    task = task_for(tmp_path, size=64)
    assert await engine.download_task(
        make_ctx(tmp_path), task, reporter=RecordingReporter(), stall_limit=5
    )
    assert probes == ["http://h/f"]
    assert task.size == 8
    assert len(calls) == 2


async def test_416_twice_gives_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transfer(monkeypatch, RangeNotSatisfiableError("http://h/f", "bytes=9-9"))

    async def fake_probe(session: object, url: str) -> ProbeResult:
        return ProbeResult(size=8, status=200, reached=True)

    monkeypatch.setattr(engine, "probe_metadata", fake_probe)
    reporter = RecordingReporter()
    assert not await engine.download_task(
        make_ctx(tmp_path), task_for(tmp_path, size=64), reporter=reporter, stall_limit=5
    )
    assert reporter.finished == [False]


async def test_a_permanent_http_error_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = patch_transfer(monkeypatch, PermanentHTTPError(403, "http://h/f"))
    reporter = RecordingReporter()
    assert not await engine.download_task(
        make_ctx(tmp_path), task_for(tmp_path, size=10), reporter=reporter, stall_limit=5
    )
    assert len(calls) == 1
    assert reporter.finished == [False]


async def test_a_checksum_error_from_the_verifier_fails_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transfer(monkeypatch, TransferResult(complete=True))

    async def broken(*_args: object, **_kwargs: object) -> bool:
        raise ChecksumError("no verifier for algorithm 'sha3'")

    monkeypatch.setattr(engine, "_verify", broken)
    monkeypatch.setattr(engine.piece_store, "discard", lambda target: None)
    reporter = RecordingReporter()
    assert not await engine.download_task(
        make_ctx(tmp_path), task_for(tmp_path, size=10), reporter=reporter, stall_limit=5
    )
    assert reporter.finished == [False]


async def test_a_disk_error_inside_an_attempt_fails_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transfer(monkeypatch, DiskSpaceError("no room"))
    reporter = RecordingReporter()
    assert not await engine.download_task(
        make_ctx(tmp_path), task_for(tmp_path, size=10), reporter=reporter, stall_limit=5
    )
    assert reporter.finished == [False]


async def test_an_io_error_fails_the_task_without_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = patch_transfer(monkeypatch, OSError("permission denied"))
    reporter = RecordingReporter()
    assert not await engine.download_task(
        make_ctx(tmp_path), task_for(tmp_path, size=10), reporter=reporter, stall_limit=5
    )
    assert len(calls) == 1
    assert reporter.finished == [False]


async def test_a_marker_that_cannot_be_written_does_not_fail_the_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transfer(monkeypatch, TransferResult(complete=True))
    monkeypatch.setattr(engine.piece_store, "save_completion", lambda *a, **k: False)
    reporter = RecordingReporter()
    assert await engine.download_task(
        make_ctx(tmp_path), task_for(tmp_path, size=10), reporter=reporter, stall_limit=5
    )
    assert reporter.finished == [True]


async def test_checksum_mismatch_twice_gives_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_transfer(monkeypatch, TransferResult(complete=True))

    async def never_matches(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(engine, "_verify", never_matches)
    monkeypatch.setattr(engine.piece_store, "discard", lambda target: None)
    reporter = RecordingReporter()
    task = task_for(tmp_path, size=10, checksum_algo="sha256", checksum_hex="a" * 64)
    assert not await engine.download_task(
        make_ctx(tmp_path), task, reporter=reporter, retries=1, stall_limit=5
    )
    assert reporter.finished == [False]


async def test_max_retries_gives_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transfer(monkeypatch, TransferResult(complete=False))
    reporter = RecordingReporter()
    assert not await engine.download_task(
        make_ctx(tmp_path), task_for(tmp_path, size=10), reporter=reporter, retries=1, stall_limit=5
    )
    assert reporter.finished == [False]


# --------------------------------------------------------------------------- #
# probe helpers used by the recovery paths
# --------------------------------------------------------------------------- #
async def test_witness_changed_again_is_conservative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(tmp_path)
    local = DownloadTask(url="file:///tmp/x", target="f.bin")
    assert not await engine._witness_changed_again(ctx, local, '"v1"')

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("no network")

    monkeypatch.setattr(engine, "probe_metadata", boom)
    assert not await engine._witness_changed_again(ctx, task_for(tmp_path), '"v1"')

    async def unreachable(*_args: object, **_kwargs: object) -> ProbeResult:
        return ProbeResult(status=None, retryable=True)

    monkeypatch.setattr(engine, "probe_metadata", unreachable)
    assert not await engine._witness_changed_again(ctx, task_for(tmp_path), '"v1"')

    async def moved(*_args: object, **_kwargs: object) -> ProbeResult:
        return ProbeResult(status=200, validator='"v2"', reached=True)

    monkeypatch.setattr(engine, "probe_metadata", moved)
    assert await engine._witness_changed_again(ctx, task_for(tmp_path), '"v1"')


async def test_reprobe_updates_size_and_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(tmp_path)
    local = DownloadTask(url="sftp://h/x", target="f.bin", size=5)
    await engine._reprobe(ctx, local)
    assert local.size == 5  # a non-HTTP task is left alone

    async def unreachable(*_args: object, **_kwargs: object) -> ProbeResult:
        return ProbeResult(status=None, retryable=True)

    monkeypatch.setattr(engine, "probe_metadata", unreachable)
    task = task_for(tmp_path, size=5)
    await engine._reprobe(ctx, task)
    assert task.size == 5 and "validator" not in task.meta

    async def fresh(*_args: object, **_kwargs: object) -> ProbeResult:
        return ProbeResult(
            size=7,
            status=200,
            validator='"v9"',
            if_range='"v9"',
            validator_kind="etag",
            reached=True,
        )

    monkeypatch.setattr(engine, "probe_metadata", fresh)
    await engine._reprobe(ctx, task)
    assert task.size == 7
    assert task.meta["validator"] == '"v9"'
    assert task.meta["if_range"] == '"v9"'
    assert task.meta["witness_kind"] == "etag"


async def test_verify_wraps_a_reader_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = make_ctx(tmp_path)
    target = tmp_path / "v.bin"
    target.write_bytes(b"x")
    verifier = ctx.registry.get("verifier", "sha256")  # type: ignore[union-attr]
    assert verifier is not None

    async def broken(path: str, expected: str) -> bool:
        raise OSError("input/output error")

    monkeypatch.setattr(verifier, "verify", broken)
    task = task_for(tmp_path, "v.bin", size=1, checksum_algo="sha256", checksum_hex="a" * 64)
    with pytest.raises(ChecksumError, match="cannot verify"):
        await engine._verify(ctx, task, str(target))


# --------------------------------------------------------------------------- #
# batch orchestration
# --------------------------------------------------------------------------- #
async def test_download_all_survives_a_task_that_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("plugin exploded")

    monkeypatch.setattr(engine, "download_task", boom)
    outcomes: list[tuple[str, bool]] = []
    ok, failed = await engine.download_all(
        make_ctx(tmp_path),
        [task_for(tmp_path, f"f{index}.bin", size=1) for index in range(3)],
        jobs=2,
        on_result=lambda task, success: outcomes.append((task.target, success)),
    )
    assert (ok, failed) == (0, 3)
    assert [success for _target, success in outcomes] == [False, False, False]


async def test_download_all_reports_a_cancelled_task_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def cancelled(*_args: object, **_kwargs: object) -> bool:
        raise asyncio.CancelledError

    monkeypatch.setattr(engine, "download_task", cancelled)
    outcomes: list[bool] = []
    ok, failed = await engine.download_all(
        make_ctx(tmp_path),
        [task_for(tmp_path, f"c{index}.bin", size=1) for index in range(2)],
        jobs=2,
        on_result=lambda task, success: outcomes.append(success),
    )
    assert (ok, failed) == (0, 2)
    assert outcomes == [False, False]


async def test_download_all_blocks_a_target_that_escapes_the_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target outside the destination is counted and still attempted.

    ``download_all`` cannot resolve the path for its disk budget, so it falls
    back to a zero-byte requirement and lets ``download_task`` report the real
    error (which is what the failed log records).
    """
    called: list[str] = []

    async def record(ctx: RuntimeContext, task: DownloadTask, **_kwargs: object) -> bool:
        called.append(task.target)
        return False

    monkeypatch.setattr(engine, "download_task", record)
    ok, failed = await engine.download_all(
        make_ctx(tmp_path), [DownloadTask(url="http://h/f", target="../escape.bin", size=1)]
    )
    assert (ok, failed) == (0, 1)
    assert called == ["../escape.bin"]


async def test_download_all_skips_an_unresolvable_target_when_counting_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reporter = RecordingReporter()

    async def record(ctx: RuntimeContext, task: DownloadTask, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(engine, "download_task", record)
    task = DownloadTask(url="http://h/f", target="../escape.bin", size=1)
    await engine.download_all(make_ctx(tmp_path), [task], reporter=reporter, jobs=2)
    assert reporter.resumed == []  # nothing to seed, and no crash
