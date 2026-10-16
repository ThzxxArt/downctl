"""Multi-connection resumable transfer of a single file.

The engine drives this module. It is the only place that knows about pieces,
the watchdog and the transport contract; plugins never call it directly.

Guarantees
----------
- Resume state comes from the piece bitmap, never from file size.
- A retry re-fetches only missing pieces.
- ``complete`` in :class:`TransferResult` is authoritative: callers must not
  infer success from file size.
- On any interruption the completed pieces are persisted (fsync data, then
  atomically write the bitmap) so work is never lost.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO

from downctl import console
from downctl.core import pieces as piece_store
from downctl.core.errors import PermanentHTTPError, StallTimeout, TransportError
from downctl.core.ratelimit import RateLimiter
from downctl.core.watchdog import DEFAULT_STALL_LIMIT, watch
from downctl.plugin.protocols import Transport

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

LogFn = Callable[[str], None]


def _default_log(message: str) -> None:
    console.warn(message.strip())


@dataclass
class TransferResult:
    complete: bool
    stalled: bool = False
    bytes_written: int = 0


class _Progress:
    """Cumulative byte counter shared across concurrent pieces."""

    def __init__(self) -> None:
        self.n = 0
        self._lock = asyncio.Lock()

    async def add(self, delta: int) -> None:
        async with self._lock:
            self.n += delta


class _PieceFetcher:
    """Download one byte range into the shared file handle."""

    def __init__(
        self,
        ctx: RuntimeContext,
        transport: Transport,
        url: str,
        headers: dict[str, str],
        fd: BinaryIO,
        lock: asyncio.Lock,
        start: int,
        end: int,
        rate_limiter: RateLimiter | None,
        progress: _Progress,
        on_progress: Callable[[int], None] | None,
    ) -> None:
        self.ctx = ctx
        self.transport = transport
        self.url = url
        self.headers = headers
        self.fd = fd
        self.lock = lock
        self.start = start
        self.end = end
        self.rate = rate_limiter
        self.progress = progress
        self.on_progress = on_progress
        self.supports_range = True
        self.completed = False

    async def run(self) -> None:
        async with self.transport.request(
            self.ctx, self.url, headers=self.headers, byte_range=(self.start, self.end)
        ) as response:
            if response.status == 200:
                # Server ignored the Range header and returned the whole body.
                self.supports_range = False
                return
            pos = self.start
            limit = self.end + 1
            async for chunk in response.iter_chunks():
                if pos >= limit:
                    break
                if pos + len(chunk) > limit:
                    chunk = chunk[: limit - pos]
                if self.rate is not None:
                    await self.rate.wait(len(chunk))
                async with self.lock:
                    self.fd.seek(pos)
                    self.fd.write(chunk)
                pos += len(chunk)
                await self.progress.add(len(chunk))
                if self.on_progress is not None:
                    self.on_progress(len(chunk))
            self.completed = pos == limit


async def _stream_watchdog(
    state: dict[str, float], stall_limit: int, log: LogFn, done: asyncio.Event
) -> None:
    """Abort a single-stream download that makes no progress for the limit."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            await asyncio.wait_for(done.wait(), timeout=1)
            return
        except TimeoutError:
            pass
        if loop.time() - state["last"] >= stall_limit:
            log(f"  stalled {stall_limit}s with no progress, reconnecting ...")
            raise StallTimeout(stall_limit)


async def _download_stream(
    ctx: RuntimeContext,
    transport: Transport,
    url: str,
    fd: BinaryIO,
    headers: dict[str, str],
    rate_limiter: RateLimiter | None,
    on_progress: Callable[[int], None] | None,
    log: LogFn,
    stall_limit: int,
) -> tuple[bool, bool]:
    """Single-stream fallback for unknown sizes.

    Returns ``(complete, stalled)``. A stall or a short read against a declared
    ``Content-Length`` yields ``(False, ...)`` so the caller retries.

    The read loop is never cancelled to poll for progress (cancelling
    ``__anext__`` closes the async generator and looks like a clean EOF,
    truncating the file): a watchdog task aborts the whole group instead.
    """
    loop = asyncio.get_running_loop()
    declared: dict[str, int | None] = {"value": None}
    state: dict[str, float] = {"written": 0.0, "last": loop.time()}
    done = asyncio.Event()

    fd.seek(0)
    fd.truncate()

    async def read() -> None:
        try:
            async with transport.request(ctx, url, headers=headers) as response:
                declared["value"] = response.content_length
                async for chunk in response.iter_chunks():
                    if rate_limiter is not None:
                        await rate_limiter.wait(len(chunk))
                    fd.write(chunk)
                    state["written"] += len(chunk)
                    state["last"] = loop.time()
                    if on_progress is not None:
                        on_progress(len(chunk))
            fd.flush()
            os.fsync(fd.fileno())
        finally:
            done.set()

    try:
        async with asyncio.TaskGroup() as group:
            group.create_task(read())
            group.create_task(_stream_watchdog(state, stall_limit, log, done))
    except asyncio.CancelledError:
        raise
    except BaseExceptionGroup as group_error:
        flat = _flatten(group_error)
        for exc in flat:
            if isinstance(exc, PermanentHTTPError):
                raise exc from None
        if any(isinstance(exc, (StallTimeout, asyncio.CancelledError)) for exc in flat):
            return False, True
        for exc in flat:
            if isinstance(exc, TransportError):
                raise exc from None
        raise

    length = declared["value"]
    if length is not None and state["written"] != length:
        return False, False
    return True, False


def _flatten(exc: BaseException) -> list[BaseException]:
    flat: list[BaseException] = []
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        else:
            flat.append(current)
    return flat


async def transfer_file(
    ctx: RuntimeContext,
    transport: Transport,
    url: str,
    target: str,
    expected: int,
    *,
    connections: int = 16,
    headers: dict[str, str] | None = None,
    rate_limiter: RateLimiter | None = None,
    on_progress: Callable[[int], None] | None = None,
    stall_limit: int = DEFAULT_STALL_LIMIT,
    log: LogFn | None = None,
) -> TransferResult:
    """Download ``url`` into ``target``, resuming from the piece bitmap."""
    log = log or _default_log
    request_headers = dict(headers or {})

    if not os.path.exists(target):
        with open(target, "wb"):
            pass

    bits: list[int] | None = None
    pieces = 0
    fetchers: dict[int, _PieceFetcher] = {}
    fd = open(target, "r+b")  # noqa: SIM115 - kept open across the whole transfer

    try:
        # Unknown size, or a transport without range support -> single stream.
        if expected <= 0 or not transport.supports_range():
            complete, stalled = await _download_stream(
                ctx,
                transport,
                url,
                fd,
                request_headers,
                rate_limiter,
                on_progress,
                log,
                stall_limit,
            )
            return TransferResult(complete=complete, stalled=stalled)

        piece_size, pieces, conn_n = piece_store.layout(expected, connections)
        bits = piece_store.resolve(target, expected, pieces)
        missing = [i for i, bit in enumerate(bits) if not bit]

        if missing:
            lock = asyncio.Lock()
            progress = _Progress()
            semaphore = asyncio.Semaphore(conn_n)
            done = asyncio.Event()
            remaining = len(missing)

            async def run_piece(index: int) -> None:
                nonlocal remaining
                try:
                    async with semaphore:
                        start = index * piece_size
                        end = min(start + piece_size - 1, expected - 1)
                        fetcher = _PieceFetcher(
                            ctx,
                            transport,
                            url,
                            request_headers,
                            fd,
                            lock,
                            start,
                            end,
                            rate_limiter,
                            progress,
                            on_progress,
                        )
                        fetchers[index] = fetcher
                        await fetcher.run()
                finally:
                    remaining -= 1
                    if remaining <= 0:
                        done.set()

            watchdog_task: asyncio.Task[None] | None = None
            try:
                async with asyncio.TaskGroup() as group:
                    for index in missing:
                        group.create_task(run_piece(index))
                    watchdog_task = group.create_task(
                        watch(
                            lambda: progress.n,
                            expected,
                            stall_limit=stall_limit,
                            done=done,
                            on_stall=lambda limit: log(
                                f"  stalled {limit}s with no progress, reconnecting ..."
                            ),
                        )
                    )
            except asyncio.CancelledError:
                raise
            except BaseExceptionGroup as group_error:
                flat = _flatten(group_error)
                for exc in flat:
                    if isinstance(exc, PermanentHTTPError):
                        raise exc from None
                if any(isinstance(exc, (StallTimeout, asyncio.CancelledError)) for exc in flat):
                    # Watchdog stall or a sibling cancellation: keep the pieces
                    # and let the caller retry. A genuine external cancellation
                    # is raised by the TaskGroup as CancelledError and handled by
                    # the clause above, so it never reaches here.
                    return TransferResult(
                        complete=False,
                        stalled=True,
                        bytes_written=progress.n,
                    )
                for exc in flat:
                    if isinstance(exc, TransportError):
                        raise exc from None
                raise
            finally:
                if watchdog_task is not None:
                    watchdog_task.cancel()

            for index, fetcher in fetchers.items():
                if fetcher.completed:
                    bits[index] = 1

            if any(not fetcher.supports_range for fetcher in fetchers.values()):
                # Server cannot do ranges: abandon pieces, stream the whole file.
                piece_store.remove(target)
                bits = None
                complete, stalled = await _download_stream(
                    ctx,
                    transport,
                    url,
                    fd,
                    request_headers,
                    rate_limiter,
                    on_progress,
                    log,
                    stall_limit,
                )
                return TransferResult(complete=complete, stalled=stalled)

            if not all(bits):
                # Interrupted: bitmap is persisted by the finally block.
                return TransferResult(complete=False)

        fd.flush()
        fd.truncate(expected)
        os.fsync(fd.fileno())
        piece_store.remove(target)
        return TransferResult(complete=True, bytes_written=expected)
    finally:
        if bits is not None and pieces > 0 and not all(bits):
            for index, fetcher in fetchers.items():
                if fetcher.completed:
                    bits[index] = 1
            try:
                fd.flush()
                os.fsync(fd.fileno())
            except OSError:
                pass
            try:
                current = os.path.getsize(target)
            except OSError:
                current = 0
            piece_store.save(target, piece_store.Bitmap(expected, pieces, bits, current))
        fd.close()
