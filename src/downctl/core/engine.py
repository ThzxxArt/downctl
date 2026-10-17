"""Download orchestration: turn tasks into completed files.

The engine owns the retry loop, disk checks, locking, transport selection and
checksum verification. It drives :mod:`downctl.core.transfer` and reports
through a reporter plugin.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from downctl import console
from downctl.core import pieces as piece_store
from downctl.core.disk import DISK_RESERVE, has_space
from downctl.core.errors import (
    ChecksumError,
    DiskSpaceError,
    DownctlError,
    PermanentHTTPError,
    TransportError,
)
from downctl.core.locks import FileLock, LockError
from downctl.core.ratelimit import RateLimiter
from downctl.core.transfer import TransferResult, transfer_file
from downctl.core.watchdog import DEFAULT_STALL_LIMIT
from downctl.urls import display_url, redact_text, url_identity

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext
    from downctl.core.task import DownloadTask
    from downctl.plugin.protocols import Reporter, Transport, Verifier

RETRY_BASE = 5
RETRY_BACKOFF_MAX = 60
CHECKSUM_FAIL_MAX = 2
DEFAULT_CONNECTIONS = 16

__all__ = ["DISK_RESERVE", "download_all", "download_task"]


def _resolve_target(ctx: RuntimeContext, task: DownloadTask) -> str:
    base = os.path.realpath(os.path.expanduser(ctx.config.core.dir or os.getcwd()))
    if task.allow_absolute:
        # Explicit user request (e.g. -o/--output).
        path = os.path.abspath(task.target)
    else:
        path = os.path.abspath(os.path.join(base, task.target))
        # Confine source-provided targets to the destination so remote metadata
        # cannot escape via "..", symlinks or a different drive.
        resolved = os.path.realpath(path)
        try:
            inside = os.path.commonpath([resolved, base]) == base
        except ValueError:  # different drive (Windows)
            inside = False
        if not inside:
            raise DownctlError(f"unsafe target path escapes the destination: {task.target!r}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _has_space(path: str, need: int) -> bool:
    return has_space(path, need)


def _task_identity(task: DownloadTask) -> str:
    """Stable identity of the remote object a task downloads.

    Computed from the URL *before* mirror rewriting (the app stores it in
    ``task.meta['identity']``); presigned URLs hash only their path, because
    their query rotates on every run.
    """
    stored = task.meta.get("identity") or task.meta.get("source_identity")
    if isinstance(stored, str) and stored:
        return stored
    return url_identity(task.url, redact=bool(task.meta.get("redact_url")))


def _safe_error(task: DownloadTask, exc: BaseException) -> str:
    """Format an error without leaking a presigned query or a token."""
    text = str(exc)
    if task.url and task.url in text:
        text = text.replace(task.url, display_url(task.url, bool(task.meta.get("redact_url"))))
    # The message may mention a *different* URL (a redirect target, a mirror,
    # a signed link from a plugin); redact every URL, not just this task's.
    return redact_text(text)


def _remaining_bytes(
    target: str,
    task: DownloadTask,
    connections: int,
    piece_size: int = 0,
    identity: str | None = None,
) -> int:
    """Bytes still needed, based on the piece bitmap rather than file size.

    Out-of-order pieces can make the on-disk size look almost complete while
    many pieces are still missing (written at absolute offsets), so the file
    size would understate the remaining work. When the existing bytes cannot
    be resumed (no bitmap/marker, stale identity, truncated file), the
    transfer resets the target and fetches the whole object, so the full size
    is reported instead of the misleading ``size - current``.
    """
    if task.size <= 0:
        return 0
    identity = _task_identity(task) if identity is None else identity
    bitmap = piece_store.load(target)
    if bitmap is not None and bitmap.size == task.size:
        current = os.path.getsize(target) if os.path.isfile(target) else 0
        if not piece_store.bitmap_trusted(bitmap, current, task.size, identity):
            # Foreign/stale ledger or a data file that no longer covers it.
            return task.size
        return max(0, task.size - bitmap.done_bytes())
    if piece_store.load_completion(target) is not None:
        return 0 if piece_store.completion_valid(target, task.size, identity) else task.size
    if piece_store.has_state(target):
        # A malformed/stale bitmap that could not even be loaded must not let
        # the weaker hole heuristic bless the file.
        return task.size
    current = os.path.getsize(target) if os.path.isfile(target) else 0
    if current == task.size and piece_store.trusted_complete(target, current):
        return 0
    # Anything else is discarded by ``pieces.resolve`` and fetched from zero.
    return task.size


def _completed_bytes(
    target: str,
    task: DownloadTask,
    connections: int,
    piece_size: int = 0,
    identity: str | None = None,
) -> int:
    """Bytes already complete, for resume-aware progress reporting.

    Only counts bytes the transfer will actually *skip*: a valid completion
    marker, what the URL-bound piece bitmap proves, or a full-size file with no
    holes. A partial/untrusted file is re-downloaded from scratch, so it must
    report ``0`` (otherwise the resume seed and the fresh bytes would be
    double-counted).
    """
    if task.size <= 0:
        return 0
    identity = _task_identity(task) if identity is None else identity
    bitmap = piece_store.load(target)
    if bitmap is not None and bitmap.size == task.size:
        current = os.path.getsize(target) if os.path.isfile(target) else 0
        if not piece_store.bitmap_trusted(bitmap, current, task.size, identity):
            # Foreign URL, truncated below the ledger's high-water mark, or a
            # malformed grid: the transfer will reset, so nothing is skipped.
            return 0
        return min(bitmap.done_bytes(), task.size)
    if piece_store.load_completion(target) is not None:
        return task.size if piece_store.completion_valid(target, task.size, identity) else 0
    if piece_store.has_state(target):
        return 0
    current = os.path.getsize(target) if os.path.isfile(target) else 0
    if current == task.size and piece_store.trusted_complete(target, current):
        return task.size
    return 0


def _get_transport(ctx: RuntimeContext, task: DownloadTask) -> Transport:
    registry = ctx.registry
    name = task.transport or "http"
    transport = registry.get("transport", name) if registry is not None else None
    if transport is None:
        raise DownctlError(f"no transport plugin named {name!r}")
    return transport  # type: ignore[return-value]


def _get_verifier(ctx: RuntimeContext, algo: str) -> Verifier | None:
    if not algo or ctx.registry is None:
        return None
    return ctx.registry.get("verifier", algo)  # type: ignore[return-value]


def _emit(ctx: RuntimeContext, event: str, payload: dict[str, object]) -> None:
    if ctx.events is not None:
        ctx.events.emit(event, payload)


def _notify_resumed(reporter: Reporter, task: DownloadTask, done: int) -> None:
    """Tell a resume-aware reporter how much of ``task`` is already complete."""
    resumed = getattr(reporter, "task_resumed", None)
    if callable(resumed):
        resumed(task, done)


def _proven_complete(target: str, task: DownloadTask, identity: str) -> bool:
    """Whether an existing file is *provably* the finished object.

    Size equality alone is never proof: an interrupted multi-connection
    download can leave a full-size file with holes. Only a completion marker,
    a matching all-complete bitmap, or a provably hole-free file count.
    """
    if task.size > 0:
        return _completed_bytes(target, task, 0, 0, identity) >= task.size
    return piece_store.completion_valid(target, 0, identity)


async def download_task(
    ctx: RuntimeContext,
    task: DownloadTask,
    *,
    reporter: Reporter | None = None,
    retries: int | None = None,
    connections: int = DEFAULT_CONNECTIONS,
    stall_limit: int = DEFAULT_STALL_LIMIT,
    piece_size: int = 0,
    min_split_size: int = 0,
    checkpoint: int = 0,
    force: bool = False,
    skip_existing: bool = False,
) -> bool:
    """Download one task. Returns ``True`` on success.

    Raises nothing on ordinary failure; returns ``False`` instead. Permanent
    HTTP errors (401/403/404) also return ``False`` without retrying.
    """
    target = _resolve_target(ctx, task)
    task.meta["path"] = target
    identity = _task_identity(task)
    max_retries = ctx.config.core.retries if retries is None else retries
    transport = _get_transport(ctx, task)
    rate_limiter = ctx.rate_limiter if ctx.rate_limiter is not None else RateLimiter(ctx.rate_limit)
    if reporter is not None:
        reporter.task_started(task)
    _emit(ctx, "on_task_start", {"task": task})

    checksum_error = ""
    if task.checksum_hex and not task.checksum_algo:
        checksum_error = "checksum digest provided without an algorithm"
    elif task.checksum_hex and _get_verifier(ctx, task.checksum_algo) is None:
        checksum_error = f"no verifier for algorithm {task.checksum_algo!r}"
    if checksum_error:
        # Do not fetch a byte that cannot be verified: a missing verifier is a
        # configuration error, not a reason to re-download the file twice.
        console.error(checksum_error)
        if reporter is not None:
            reporter.task_finished(task, False)
        _emit(ctx, "on_error", {"task": task, "error": checksum_error})
        return False

    lock = FileLock(target)
    try:
        lock.acquire()
    except LockError as exc:
        console.warn(str(exc))
        if reporter is not None:
            reporter.task_finished(task, False)
        _emit(ctx, "on_error", {"task": task, "error": str(exc)})
        return False

    if force:
        console.verbose(f"forcing re-download of {target}")
        _discard(target)

    if skip_existing and os.path.isfile(target):
        skip = _proven_complete(target, task, identity)
        # User-supplied checksums are always re-checked. A source checksum
        # (HF LFS, GitHub digest) is trusted only when a completion marker
        # proves this exact download was verified; a complete bitmap or a
        # hole-scan proof can survive a kill between the last checkpoint and
        # verification, so it must be hashed before being blessed.
        marker = piece_store.load_completion(target)
        marker_ok = piece_store.completion_valid(
            target, task.size if task.size > 0 else 0, identity
        )
        marker_digest_ok = not task.checksum_hex or (
            marker is not None
            and str(marker.get("checksum_algo", "")) == (task.checksum_algo or "").lower()
            and str(marker.get("checksum_hex", "")) == (task.checksum_hex or "").lower()
        )
        must_verify = bool(task.checksum_hex) and (
            bool(task.meta.get("user_checksum")) or not marker_ok or not marker_digest_ok
        )
        if skip and must_verify:
            try:
                skip = await _verify(ctx, task, target)
            except ChecksumError as exc:
                console.error(str(exc))
                if reporter is not None:
                    reporter.task_finished(task, False)
                _emit(ctx, "on_error", {"task": task, "error": str(exc)})
                lock.release()
                return False
            if not skip:
                console.warn(
                    f"existing {target} does not match the expected checksum; re-downloading"
                )
                _discard(target)
        if skip:
            console.verbose(f"skipping existing {target} (completion proven)")
            if not piece_store.save_completion(
                target, identity, task.checksum_algo, task.checksum_hex
            ):
                console.verbose(f"could not write completion marker for {target}")
            if reporter is not None:
                reporter.task_finished(task, True)
            _emit(ctx, "on_task_finish", {"task": task, "ok": True})
            lock.release()
            return True

    effective_connections = connections
    if min_split_size > 0 and 0 < task.size < min_split_size:
        effective_connections = 1

    retries_done = 0
    checksum_failures = 0
    try:
        done = _completed_bytes(target, task, effective_connections, piece_size, identity)
        if done > 0:
            if reporter is not None:
                _notify_resumed(reporter, task, done)
            console.verbose(
                f"resuming {target}: {done} byte(s) already complete ({task.size} total)"
            )
        while True:
            if not _has_space(
                target,
                _remaining_bytes(target, task, effective_connections, piece_size, identity),
            ):
                error = DiskSpaceError(f"not enough free space for {target}")
                console.error(str(error))
                if reporter is not None:
                    reporter.task_finished(task, False)
                _emit(ctx, "on_error", {"task": task, "error": str(error)})
                return False

            def _progress(delta: int, _task: DownloadTask = task) -> None:
                ctx.stats.bytes_transferred += delta
                if reporter is not None:
                    reporter.task_progress(_task, delta)

            try:
                result = await transfer_file(
                    ctx,
                    transport,
                    task.url,
                    target,
                    task.size,
                    connections=effective_connections,
                    headers=task.headers,
                    rate_limiter=rate_limiter,
                    on_progress=_progress,
                    stall_limit=stall_limit,
                    piece_size=piece_size,
                    identity=identity,
                    checkpoint=checkpoint,
                )
                verified = await _verify(ctx, task, target)
            except PermanentHTTPError as exc:
                message = _safe_error(task, exc)
                console.error(f"{message} (not retryable)")
                if reporter is not None:
                    reporter.task_finished(task, False)
                _emit(ctx, "on_error", {"task": task, "error": message})
                return False
            except ChecksumError as exc:
                console.error(str(exc))
                if reporter is not None:
                    reporter.task_finished(task, False)
                _emit(ctx, "on_error", {"task": task, "error": str(exc)})
                return False
            except DiskSpaceError as exc:
                console.error(str(exc))
                if reporter is not None:
                    reporter.task_finished(task, False)
                _emit(ctx, "on_error", {"task": task, "error": str(exc)})
                return False
            except TransportError as exc:
                console.warn(f"transient error: {_safe_error(task, exc)}; retrying")
                result = TransferResult(complete=False)
                verified = True
            except OSError as exc:
                # Permission denied, target is a directory, ENOSPC, ... are not
                # worth retrying forever; fail the file instead.
                console.error(f"cannot write {target}: {exc}")
                if reporter is not None:
                    reporter.task_finished(task, False)
                _emit(ctx, "on_error", {"task": task, "error": str(exc)})
                return False

            if result.complete and verified:
                if not piece_store.save_completion(
                    target, identity, task.checksum_algo, task.checksum_hex
                ):
                    console.verbose(f"could not write completion marker for {target}")
                if reporter is not None:
                    reporter.task_finished(task, True)
                _emit(ctx, "on_task_finish", {"task": task, "ok": True})
                return True

            if result.complete and not verified:
                checksum_failures += 1
                if checksum_failures >= CHECKSUM_FAIL_MAX:
                    console.error(f"checksum failed {checksum_failures} times; giving up")
                    _discard(target)
                    if reporter is not None:
                        reporter.task_finished(task, False)
                    _emit(ctx, "on_error", {"task": task, "error": "checksum"})
                    return False
                console.warn(f"checksum mismatch; re-downloading ({checksum_failures})")
                _discard(target)
                result = TransferResult(complete=False)

            retries_done += 1
            if max_retries > 0 and retries_done > max_retries:
                console.warn(f"giving up after {retries_done} retries")
                if reporter is not None:
                    reporter.task_finished(task, False)
                _emit(ctx, "on_error", {"task": task, "error": "max retries"})
                return False
            backoff_base = RETRY_BASE
            with contextlib.suppress(ValueError):
                backoff_base = int(os.environ.get("DOWNCTL_RETRY_BASE", RETRY_BASE))
            backoff = min(backoff_base * (1 << (retries_done - 1)), RETRY_BACKOFF_MAX)
            jitter = random.randint(0, backoff // 2 + 1)
            await asyncio.sleep(backoff + jitter)
    finally:
        lock.release()


async def _verify(ctx: RuntimeContext, task: DownloadTask, target: str) -> bool:
    if not task.checksum_hex or not task.checksum_algo:
        return True
    verifier = _get_verifier(ctx, task.checksum_algo)
    if verifier is None:
        # Retrying cannot conjure a verifier; fail immediately instead of
        # re-downloading the file twice before giving up.
        raise ChecksumError(f"no verifier for algorithm {task.checksum_algo!r}")
    try:
        return await verifier.verify(target, task.checksum_hex)
    except (OSError, ValueError) as exc:
        raise ChecksumError(f"cannot verify {target}: {exc}") from exc


def _discard(target: str) -> None:
    piece_store.remove(target)
    with contextlib.suppress(OSError):
        os.remove(target)


async def download_all(
    ctx: RuntimeContext,
    tasks: Sequence[DownloadTask],
    *,
    jobs: int = 1,
    reporter: Reporter | None = None,
    connections: int = DEFAULT_CONNECTIONS,
    stall_limit: int = DEFAULT_STALL_LIMIT,
    piece_size: int = 0,
    min_split_size: int = 0,
    checkpoint: int = 0,
    force: bool = False,
    skip_existing: bool = False,
    on_result: Callable[[DownloadTask, bool], None] | None = None,
    extra_failed: int = 0,
) -> tuple[int, int]:
    """Download many tasks with a concurrency limit. Returns ``(ok, failed)``.

    ``extra_failed`` counts targets that already failed before the engine ran
    (resolve/probe), so the reporter's ``run_finish`` reflects the whole run.
    """
    _emit(
        ctx,
        "on_start",
        {"targets": [display_url(task.url, bool(task.meta.get("redact_url"))) for task in tasks]},
    )
    ok = failed = 0

    run_started = getattr(reporter, "run_started", None)
    if callable(run_started):
        run_started(len(tasks), sum(max(0, task.size) for task in tasks), jobs)

    # Report already-complete bytes up front so an overall bar reflects resume
    # progress instead of assuming all bytes are still to fetch.
    if reporter is not None and not force:
        resumed = getattr(reporter, "run_resumed", None)
        if callable(resumed):
            done_total = 0
            for task in tasks:
                target = task.meta.get("path")
                if not target:
                    try:
                        target = _resolve_target(ctx, task)
                    except DownctlError:
                        continue
                done_total += _completed_bytes(
                    str(target), task, connections, piece_size, _task_identity(task)
                )
            if done_total > 0:
                resumed(done_total)

    def _kwargs() -> dict[str, object]:
        return {
            "reporter": reporter,
            "connections": connections,
            "stall_limit": stall_limit,
            "piece_size": piece_size,
            "min_split_size": min_split_size,
            "checkpoint": checkpoint,
            "force": force,
            "skip_existing": skip_existing,
        }

    if jobs <= 1:
        for task in tasks:
            try:
                success = await download_task(ctx, task, **_kwargs())  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 - one file must not abort the batch
                console.error(f"task failed: {redact_text(str(exc))}")
                success = False
            ok += success
            failed += not success
            if on_result is not None:
                on_result(task, success)
    else:
        semaphore = asyncio.Semaphore(jobs)

        async def worker(task: DownloadTask) -> bool:
            async with semaphore:
                try:
                    return await download_task(ctx, task, **_kwargs())  # type: ignore[arg-type]
                except Exception as exc:  # noqa: BLE001
                    console.error(f"task failed: {redact_text(str(exc))}")
                    return False

        results = await asyncio.gather(*(worker(task) for task in tasks), return_exceptions=True)
        for task, outcome in zip(tasks, results, strict=False):
            if isinstance(outcome, BaseException):
                console.error(f"task failed: {redact_text(str(outcome))}")
                success = False
            else:
                success = outcome
            ok += success
            failed += not success
            if on_result is not None:
                on_result(task, bool(success))

    _emit(ctx, "on_finish", {"succeeded": ok, "failed": failed + extra_failed})
    run_finished = getattr(reporter, "run_finished", None)
    if callable(run_finished):
        run_finished(ok, failed + extra_failed)
    return ok, failed + extra_failed
