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
import shutil
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from downctl import console
from downctl.core import pieces as piece_store
from downctl.core.errors import (
    DiskSpaceError,
    DownctlError,
    PermanentHTTPError,
    TransportError,
)
from downctl.core.locks import FileLock, LockError
from downctl.core.ratelimit import RateLimiter
from downctl.core.transfer import TransferResult, transfer_file
from downctl.core.watchdog import DEFAULT_STALL_LIMIT

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext
    from downctl.core.task import DownloadTask
    from downctl.plugin.protocols import Reporter, Transport, Verifier

DISK_RESERVE = 200 * (1 << 20)  # keep 200 MiB headroom
RETRY_BASE = 5
RETRY_BACKOFF_MAX = 60
CHECKSUM_FAIL_MAX = 2
DEFAULT_CONNECTIONS = 16


def _resolve_target(ctx: RuntimeContext, task: DownloadTask) -> str:
    if os.path.isabs(task.target):
        path = task.target
    else:
        base = ctx.config.core.dir or os.getcwd()
        path = os.path.join(os.path.expanduser(base), task.target)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return path


def _has_space(path: str, need: int) -> bool:
    if need <= 0:
        return True
    try:
        free = shutil.disk_usage(os.path.dirname(os.path.abspath(path))).free
    except OSError:
        return True
    return need + DISK_RESERVE <= free


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


async def download_task(
    ctx: RuntimeContext,
    task: DownloadTask,
    *,
    reporter: Reporter | None = None,
    retries: int | None = None,
    connections: int = DEFAULT_CONNECTIONS,
    stall_limit: int = DEFAULT_STALL_LIMIT,
) -> bool:
    """Download one task. Returns ``True`` on success.

    Raises nothing on ordinary failure; returns ``False`` instead. Permanent
    HTTP errors (401/403/404) also return ``False`` without retrying.
    """
    target = _resolve_target(ctx, task)
    task.meta["path"] = target
    max_retries = ctx.config.core.retries if retries is None else retries
    transport = _get_transport(ctx, task)
    rate_limiter = ctx.rate_limiter if ctx.rate_limiter is not None else RateLimiter(ctx.rate_limit)
    if reporter is not None:
        reporter.task_started(task)
    _emit(ctx, "on_task_start", {"task": task})

    lock = FileLock(target)
    try:
        lock.acquire()
    except LockError as exc:
        console.warn(str(exc))
        if reporter is not None:
            reporter.task_finished(task, False)
        _emit(ctx, "on_error", {"task": task, "error": str(exc)})
        return False

    attempt = 0
    checksum_failures = 0
    try:
        while True:
            current = os.path.getsize(target) if os.path.isfile(target) else 0
            if not _has_space(target, max(0, task.size - current)):
                error = DiskSpaceError(f"not enough free space for {target}")
                console.error(str(error))
                if reporter is not None:
                    reporter.task_finished(task, False)
                _emit(ctx, "on_error", {"task": task, "error": str(error)})
                return False

            def _progress(delta: int, _task: DownloadTask = task) -> None:
                if reporter is not None:
                    reporter.task_progress(_task, delta)

            try:
                result = await transfer_file(
                    ctx,
                    transport,
                    task.url,
                    target,
                    task.size,
                    connections=connections,
                    headers=task.headers,
                    rate_limiter=rate_limiter,
                    on_progress=_progress,
                    stall_limit=stall_limit,
                )
            except PermanentHTTPError as exc:
                console.error(f"{exc} (not retryable)")
                if reporter is not None:
                    reporter.task_finished(task, False)
                _emit(ctx, "on_error", {"task": task, "error": str(exc)})
                return False
            except (TransportError, OSError) as exc:
                console.warn(f"transient error: {exc}; retrying")
                result = TransferResult(complete=False)

            if result.complete and await _verify(ctx, task, target):
                if reporter is not None:
                    reporter.task_finished(task, True)
                _emit(ctx, "on_task_finish", {"task": task, "ok": True})
                return True

            if result.complete and task.checksum_hex:
                checksum_failures += 1
                if checksum_failures >= CHECKSUM_FAIL_MAX:
                    console.error(f"checksum failed {checksum_failures} times; giving up")
                    piece_store.remove(target)
                    if reporter is not None:
                        reporter.task_finished(task, False)
                    _emit(ctx, "on_error", {"task": task, "error": "checksum"})
                    return False
                console.warn(f"checksum mismatch; re-downloading ({checksum_failures})")
                _discard(target)
                result = TransferResult(complete=False)

            attempt += 1
            if max_retries > 0 and attempt >= max_retries:
                console.warn(f"giving up after {attempt} attempts")
                if reporter is not None:
                    reporter.task_finished(task, False)
                _emit(ctx, "on_error", {"task": task, "error": "max retries"})
                return False
            backoff = min(RETRY_BASE * (1 << (attempt - 1)), RETRY_BACKOFF_MAX)
            await asyncio.sleep(backoff + random.randint(0, backoff // 2 + 1))
    finally:
        lock.release()


async def _verify(ctx: RuntimeContext, task: DownloadTask, target: str) -> bool:
    if not task.checksum_hex or not task.checksum_algo:
        return True
    verifier = _get_verifier(ctx, task.checksum_algo)
    if verifier is None:
        console.verbose(f"no verifier for algorithm {task.checksum_algo!r}; skipping check")
        return True
    return await verifier.verify(target, task.checksum_hex)


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
    on_result: Callable[[DownloadTask, bool], None] | None = None,
) -> tuple[int, int]:
    """Download many tasks with a concurrency limit. Returns ``(ok, failed)``."""
    _emit(ctx, "on_start", {"targets": [task.url for task in tasks]})
    ok = failed = 0

    run_started = getattr(reporter, "run_started", None)
    if callable(run_started):
        run_started(len(tasks), sum(max(0, task.size) for task in tasks))

    if jobs <= 1:
        for task in tasks:
            success = await download_task(
                ctx, task, reporter=reporter, connections=connections, stall_limit=stall_limit
            )
            ok += success
            failed += not success
            if on_result is not None:
                on_result(task, success)
    else:
        semaphore = asyncio.Semaphore(jobs)

        async def worker(task: DownloadTask) -> bool:
            async with semaphore:
                return await download_task(
                    ctx, task, reporter=reporter, connections=connections, stall_limit=stall_limit
                )

        results = await asyncio.gather(*(worker(task) for task in tasks), return_exceptions=False)
        for task, success in zip(tasks, results, strict=False):
            ok += success
            failed += not success
            if on_result is not None:
                on_result(task, success)

    _emit(ctx, "on_finish", {"succeeded": ok, "failed": failed})
    run_finished = getattr(reporter, "run_finished", None)
    if callable(run_finished):
        run_finished(ok, failed)
    return ok, failed
