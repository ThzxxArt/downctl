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
- While a multi-connection transfer runs, the bitmap is also checkpointed
  every ``checkpoint`` seconds (fsync data first, then atomically write the
  bitmap). A hard kill (``SIGKILL``, power loss) therefore loses at most one
  checkpoint interval, instead of everything written since the retry loop
  started.
- The bitmap is bound to a URL identity supplied by the caller, so state from
  another revision/host is never resumed into this file.
- The bitmap is also bound to the object's *version witness* (``validator``).
  Pieces are requested with ``If-Range`` when a strong witness is known, every
  ``206`` must cover exactly the requested window (``Content-Range`` is
  asserted, not trusted), and **every response's own validator must agree with
  the witness this run observed** — ``If-Range`` is a request a server may
  ignore, so the response header is the only evidence that all the pieces come
  from one revision. A full body answering a conditional request is resolved
  with one probe: either the server cannot do ranges at all (fall back to a
  single stream) or the object was replaced (raise
  :class:`ContentChangedError` so the caller restarts from zero). Two
  revisions can never be spliced into one file.
- :attr:`TransferResult.witness_bound` says whether the bytes now on disk were
  obtained — or compared — under the observed witness. It is ``False`` when the
  completion was carried over from state this run never checked, so the caller
  knows not to certify it.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO

from downctl import console
from downctl.core import pieces as piece_store
from downctl.core.disk import ensure_space
from downctl.core.errors import (
    ContentChangedError,
    DownctlError,
    PermanentHTTPError,
    RangeMismatchError,
    RangeNotSatisfiableError,
    RangeSizeMismatchError,
    StallTimeout,
    TransportError,
)
from downctl.core.ratelimit import RateLimiter
from downctl.core.watchdog import DEFAULT_STALL_LIMIT, watch
from downctl.plugin.protocols import Response, Transport
from downctl.urls import header_value, normalize_validator, url_identity

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

LogFn = Callable[[str], None]

#: ``bytes <start>-<end>/<total|*>`` (RFC 9110 §14.4).
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


def _default_log(message: str) -> None:
    console.warn(message.strip())


@dataclass
class TransferResult:
    complete: bool
    stalled: bool = False
    bytes_written: int = 0
    #: Whether the bytes now on disk were obtained — or compared — under the
    #: version witness this run observed.
    #:
    #: ``complete=True`` does not imply this. :func:`~downctl.core.pieces.resolve`
    #: can hand back an all-ones grid from state that predates this run (a
    #: completion marker whose recorded witness was never compared, or a hole
    #: scan), and then no request is made at all. The caller must not stamp the
    #: freshly probed witness onto such a file: that certifies content this run
    #: never looked at, and every later run trusts the certificate.
    witness_bound: bool = True
    #: Whether this run actually requested any bytes. ``False`` means the whole
    #: grid was already complete, so the completion rests entirely on the state
    #: ``resolve`` accepted — the caller must not describe that as "fetched".
    fetched: bool = True


class _Progress:
    """Cumulative byte counter shared across concurrent pieces."""

    def __init__(self) -> None:
        self.n = 0
        self._lock = asyncio.Lock()

    async def add(self, delta: int) -> None:
        async with self._lock:
            self.n += delta


def check_response_witness(response: Response, expected: str, url: str) -> bool:
    """Whether a response's own validator agrees with ``expected``.

    Returns ``True`` when the bytes may be treated as that version and ``False``
    when the response named no version at all. Raises
    :class:`ContentChangedError` when it names a *different* one.

    ``If-Range`` is a request, not a guarantee: RFC 9110 lets a server ignore
    it, and one that honours ``Range`` while ignoring ``If-Range`` answers every
    piece with a ``206`` drawn from whatever revision is current. Neither the
    status line nor ``Content-Range`` reveals that, so the response's own
    validator is the only evidence that all the pieces come from one revision.
    Reproduced before this check existed: a V1 prefix and a V2 suffix in one
    file, the correct total size, exit code 0, completion marker written.

    An empty ``expected`` means this run has no witness to enforce, and a
    response that names none leaves the bytes *unbound* rather than wrong —
    the caller records that instead of claiming a version it never checked.
    """
    if not expected:
        return True
    named = normalize_validator(response.headers)
    if not named.value:
        return False
    if named.value != expected:
        raise ContentChangedError(url, named.value, named.if_range)
    return True


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
        heartbeat: Callable[[], None],
        *,
        expected: int = 0,
        if_range: str = "",
        expected_validator: str = "",
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
        self.heartbeat = heartbeat
        self.expected = expected
        self.if_range = if_range
        #: The witness every response must agree with (``""`` = none observed).
        self.expected_validator = expected_validator
        #: Set when this piece's response named no version at all: the bytes are
        #: real but nothing ties them to a revision.
        self.unbound = False
        #: Only transports that speak HTTP range semantics carry a meaningful
        #: ``Content-Range``; a local/SFTP transport answers a range with a
        #: synthetic ``206`` and no headers (see ``Transport`` docs).
        self.validate_ranges = bool(getattr(transport, "sends_content_range", False))
        self.supports_range = True
        self.completed = False

    def request_headers(self) -> dict[str, str]:
        """Headers for this piece, made conditional when a witness is known."""
        if not self.if_range:
            return self.headers
        merged = dict(self.headers)
        # Never let a user-supplied -H override the conditional request: a
        # mismatched If-Range would silently disable splice detection.
        merged["If-Range"] = self.if_range
        return merged

    def check_content_range(self, raw: str) -> None:
        """Assert a ``206`` covers exactly the window we asked for.

        A server answering with a different window would make us write other
        bytes at this offset. Size checks downstream cannot see that (the file
        still ends up the right length), so the response is rejected before a
        single byte is written.
        """
        match = _CONTENT_RANGE.match(raw.strip())
        if match is None:
            raise RangeMismatchError(f"206 response without a usable Content-Range: {raw!r}")
        start, end, total = int(match.group(1)), int(match.group(2)), match.group(3)
        if start != self.start or end != self.end:
            raise RangeMismatchError(
                f"server returned bytes {start}-{end} for a request of {self.start}-{self.end}"
            )
        if total != "*" and self.expected > 0 and int(total) != self.expected:
            # The object is a different length than the probe said, so *this*
            # number cannot be retried away: the engine re-probes and restarts,
            # exactly as it does for a 416.
            raise RangeSizeMismatchError(self.url, int(total), self.expected)

    async def run(self) -> None:
        async with self.transport.request(
            self.ctx,
            self.url,
            headers=self.request_headers(),
            byte_range=(self.start, self.end),
        ) as response:
            if response.status == 200:
                # Server sent the whole body instead of our window: either it
                # cannot do ranges, or our witness is stale. The caller decides.
                self.supports_range = False
                return
            if response.status != 206:
                raise TransportError(
                    f"unexpected HTTP {response.status} for a range request "
                    f"of {self.start}-{self.end}"
                )
            if self.validate_ranges:
                self.check_content_range(header_value(response.headers, "Content-Range"))
            # A 206 that names another revision means the server ignored
            # ``If-Range``; writing these bytes at this offset would splice two
            # objects into one file that no size check can detect.
            if not check_response_witness(response, self.expected_validator, self.url):
                self.unbound = True
            pos = self.start
            limit = self.end + 1
            async for chunk in response.iter_chunks():
                if pos >= limit:
                    break
                if pos + len(chunk) > limit:
                    chunk = chunk[: limit - pos]
                if self.rate is not None:
                    await self.rate.wait(len(chunk), self.heartbeat)
                async with self.lock:
                    self.fd.seek(pos)
                    self.fd.write(chunk)
                pos += len(chunk)
                self.heartbeat()
                await self.progress.add(len(chunk))
                if self.on_progress is not None:
                    self.on_progress(len(chunk))
            self.completed = pos == limit


async def _probe_range_support(
    ctx: RuntimeContext,
    transport: Transport,
    url: str,
    headers: dict[str, str],
) -> None:
    """Explain a full body that answered a *conditional* range request.

    Returns normally when the server has no range support at all (the caller
    may fall back to a single stream). Raises :class:`ContentChangedError` when
    the unconditional probe proves ranges *are* supported: then the earlier
    ``200`` meant ``If-Range`` did not match, i.e. the object was replaced.

    A server that ignores ``If-Range`` while supporting ranges is
    indistinguishable from a replaced object and is treated as the safer case
    (restart the download); that costs bandwidth, never correctness.
    """
    async with transport.request(ctx, url, headers=headers, byte_range=(0, 0)) as response:
        if response.status == 200:
            return
        witness = normalize_validator(response.headers)
        raise ContentChangedError(url, witness.value, witness.if_range)


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
    expected: int = 0,
    expected_validator: str = "",
) -> TransferResult:
    """Single-stream fallback for unknown sizes or rangeless servers.

    A stall, a short read against a declared ``Content-Length``, or fewer bytes
    than the known ``expected`` size yields ``complete=False`` so the caller
    retries.

    The read loop is never cancelled to poll for progress (cancelling
    ``__anext__`` closes the async generator and looks like a clean EOF,
    truncating the file): a watchdog task aborts the whole group instead.

    Nothing is destroyed until the response is open. A stream restarts at
    offset 0, so the bytes and the sidecars that describe them have to go — but
    only once a replacement is actually in hand, or a request that fails would
    cost the user a complete file it never replaced.
    """
    loop = asyncio.get_running_loop()
    declared: dict[str, int | None] = {"value": None}
    state: dict[str, float] = {"written": 0.0, "last": loop.time()}
    bound = {"value": True}
    done = asyncio.Event()

    def touch() -> None:
        state["last"] = loop.time()

    async def read() -> None:
        try:
            async with transport.request(ctx, url, headers=headers) as response:
                bound["value"] = check_response_witness(response, expected_validator, url)
                declared["value"] = response.content_length
                length = response.content_length
                if length is not None and length > 0:
                    # Unknown-size targets skip the engine's pre-flight, so
                    # check here once the length is known (before truncating).
                    ensure_space(fd.name, length)
                # Committed to writing from here: drop the old bytes *and* the
                # marker that describes them, so a half-written file can never
                # be read as either revision.
                piece_store.remove(fd.name)
                fd.seek(0)
                fd.truncate()
                async for chunk in response.iter_chunks():
                    if rate_limiter is not None:
                        await rate_limiter.wait(len(chunk), touch)
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
        if _classify(flat) == "stall":
            return TransferResult(
                complete=False,
                stalled=True,
                bytes_written=int(state["written"]),
                witness_bound=bound["value"],
            )
        _raise_leaf(flat)

    length = declared["value"]
    if length is not None and state["written"] != length:
        return TransferResult(
            complete=False, bytes_written=int(state["written"]), witness_bound=bound["value"]
        )
    if expected > 0 and state["written"] != expected:
        return TransferResult(
            complete=False, bytes_written=int(state["written"]), witness_bound=bound["value"]
        )
    return TransferResult(
        complete=True, bytes_written=int(state["written"]), witness_bound=bound["value"]
    )


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


def _classify(flat: list[BaseException]) -> str:
    """Pick a representative category from a flattened exception group."""
    for exc in flat:
        if isinstance(exc, ContentChangedError):
            return "changed"
    for exc in flat:
        if isinstance(exc, RangeNotSatisfiableError):
            return "range"
    for exc in flat:
        if isinstance(exc, PermanentHTTPError):
            return "permanent"
    if any(isinstance(exc, (StallTimeout, asyncio.CancelledError)) for exc in flat):
        return "stall"
    for exc in flat:
        if isinstance(exc, TransportError):
            return "transport"
    for exc in flat:
        if isinstance(exc, (OSError, DownctlError)):
            return "os"
    return "other"


def _raise_leaf(flat: list[BaseException]) -> None:
    """Re-raise the most specific leaf so callers can catch it directly."""
    for kind in (
        ContentChangedError,
        RangeNotSatisfiableError,
        RangeSizeMismatchError,
        PermanentHTTPError,
        TransportError,
        OSError,
        DownctlError,
    ):
        for exc in flat:
            if isinstance(exc, kind):
                raise exc from None
    raise flat[0] if flat else RuntimeError("unknown transfer failure")


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
    piece_size: int = 0,
    identity: str = "",
    checkpoint: int = 0,
    log: LogFn | None = None,
    validator: str = "",
    if_range: str = "",
    require_witness: bool = False,
) -> TransferResult:
    """Download ``url`` into ``target``, resuming from the piece bitmap.

    ``identity`` binds the bitmap to the remote object. ``checkpoint`` is the
    interval in seconds between durable bitmap snapshots while transferring
    (``0`` disables periodic snapshots; the final snapshot still happens).

    ``validator`` is the version witness this run observed for the object
    (``""`` means "the server offers none"); a ledger recorded for a different
    witness is discarded instead of resumed. ``if_range`` is the value to send
    in ``If-Range`` — empty for a weak or absent witness, which only costs the
    extra in-flight detection, never correctness.
    """
    log = log or _default_log
    request_headers = dict(headers or {})
    # Never persist an unbound ledger. Direct callers (tests, future core
    # users) may omit the identity; binding to the URL keeps v3/empty-identity
    # bitmaps from ever being trusted again.
    identity = identity or url_identity(url)

    if not os.path.exists(target):
        with open(target, "wb"):
            pass

    bits: list[int] = []
    tracked = False
    pieces = 0
    fetchers: dict[int, _PieceFetcher] = {}
    fd = open(target, "r+b")  # noqa: SIM115 - kept open across the whole transfer

    try:
        # Unknown size, or a transport without range support -> single stream.
        if expected <= 0 or not transport.supports_range():
            # A stream restarts at offset 0, so stale resume state must not
            # survive and be preferred by a later known-size run. The bytes and
            # the marker are dropped by ``_download_stream`` itself, once a
            # replacement response is actually open.
            return await _download_stream(
                ctx,
                transport,
                url,
                fd,
                request_headers,
                rate_limiter,
                on_progress,
                log,
                stall_limit,
                expected,
                expected_validator=validator,
            )

        try:
            piece_size, pieces, conn_n = piece_store.layout(expected, connections, piece_size)
        except ValueError as exc:
            raise DownctlError(str(exc)) from exc
        resume = piece_store.resolve(
            target,
            expected,
            pieces,
            piece_size,
            identity,
            validator=validator,
            require_witness=require_witness,
        )
        bits = resume.bits
        tracked = True
        if resume.dropped_claim:
            log(
                f"  {target}: the finished copy is of another revision of this URL "
                "(the server now reports a different version); fetching it again"
            )
        missing = [i for i, bit in enumerate(bits) if not bit]

        if missing:
            lock = asyncio.Lock()
            progress = _Progress()
            semaphore = asyncio.Semaphore(conn_n)
            done = asyncio.Event()
            remaining = len(missing)
            heartbeat = [asyncio.get_running_loop().time()]

            def beat() -> None:
                heartbeat[0] = asyncio.get_running_loop().time()

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
                            beat,
                            expected=expected,
                            if_range=if_range,
                            expected_validator=validator,
                        )
                        fetchers[index] = fetcher
                        await fetcher.run()
                finally:
                    remaining -= 1
                    if remaining <= 0:
                        done.set()

            watchdog_task: asyncio.Task[None] | None = None

            async def checkpoint_once() -> bool:
                """Durably snapshot completed pieces (data first, then ledger).

                The atomic ledger write is deliberately synchronous: if a
                checkpoint task is cancelled while saving, a worker thread
                would keep running and could overwrite the final ledger with a
                stale snapshot after the transfer's ``finally`` saved newer
                bits. Keeping snapshot + save on the loop thread makes the
                final save strictly ordered.
                """
                async with lock:
                    try:
                        fd.flush()
                        await asyncio.to_thread(os.fsync, fd.fileno())
                    except (OSError, ValueError):
                        # Never persist bits whose data did not reach disk: the
                        # previous durable bitmap remains authoritative.
                        return False
                    snapshot = list(bits)
                    for index, fetcher in list(fetchers.items()):
                        if fetcher.completed:
                            snapshot[index] = 1
                    try:
                        current = os.path.getsize(target)
                    except OSError:
                        current = 0
                return piece_store.save(
                    target,
                    piece_store.Bitmap(
                        size=expected,
                        pieces=pieces,
                        bits=snapshot,
                        min_size=current,
                        piece_size=piece_size,
                        url_sha256=identity,
                        validator=validator,
                    ),
                )

            async def checkpoint_loop() -> None:
                warned = False
                while True:
                    try:
                        await asyncio.wait_for(done.wait(), timeout=float(checkpoint))
                        return
                    except TimeoutError:
                        pass
                    try:
                        saved = await checkpoint_once()
                    except Exception as exc:  # noqa: BLE001 - never abort a download
                        if not warned:
                            log(f"  could not checkpoint resume state: {exc}")
                            warned = True
                        continue
                    if not saved and not warned:
                        log("  could not checkpoint resume state (write failed)")
                        warned = True

            try:
                async with asyncio.TaskGroup() as group:
                    for index in missing:
                        group.create_task(run_piece(index))
                    if checkpoint > 0:
                        group.create_task(checkpoint_loop())
                    watchdog_task = group.create_task(
                        watch(
                            lambda: progress.n,
                            expected,
                            stall_limit=stall_limit,
                            done=done,
                            heartbeat=lambda: heartbeat[0],
                            on_stall=lambda limit: log(
                                f"  stalled {limit}s with no progress, reconnecting ..."
                            ),
                        )
                    )
            except asyncio.CancelledError:
                raise
            except BaseExceptionGroup as group_error:
                flat = _flatten(group_error)
                # Watchdog stall or a sibling cancellation: keep the pieces and
                # let the caller retry. Permanent HTTP errors and transport/IO
                # errors are re-raised (as leaves) for the caller to handle.
                if _classify(flat) == "stall":
                    return TransferResult(
                        complete=False,
                        stalled=True,
                        bytes_written=progress.n,
                    )
                _raise_leaf(flat)
            finally:
                if watchdog_task is not None:
                    watchdog_task.cancel()

            for index, fetcher in fetchers.items():
                if fetcher.completed:
                    bits[index] = 1

            if any(not fetcher.supports_range for fetcher in fetchers.values()):
                # A full body answered a range request. When the request was
                # conditional, one probe tells "no range support" apart from
                # "the object was replaced under us".
                if if_range:
                    try:
                        await _probe_range_support(ctx, transport, url, request_headers)
                    except ContentChangedError:
                        # Completed bits belong to the old revision, the others
                        # to the new one: nothing on disk is reusable, and the
                        # ledger must not be re-saved. Pinned to zero rather than
                        # truncated — the replacement has not been fetched yet.
                        piece_store.abandon(
                            target, expected, pieces, piece_size, identity, validator
                        )
                        tracked = False
                        raise
                # Server cannot do ranges: abandon pieces, stream the whole file.
                tracked = False
                return await _download_stream(
                    ctx,
                    transport,
                    url,
                    fd,
                    request_headers,
                    rate_limiter,
                    on_progress,
                    log,
                    stall_limit,
                    expected,
                    expected_validator=validator,
                )

            if not all(bits):
                # Interrupted: bitmap is persisted by the finally block.
                return TransferResult(complete=False)

        fd.flush()
        fd.truncate(expected)
        os.fsync(fd.fileno())
        # The ledger is finished; the completion marker is not. It carries the
        # witness and digest this file earned, and the engine re-reads it to
        # decide what the *next* marker may keep.
        piece_store.remove_ledger(target)
        # Two independent halves decide whether this completion may claim the
        # observed version: what the ledger allowed us to skip (only a ``fresh``
        # or ``ledger`` decision rests on that witness) and what the responses
        # themselves named (a 206 that carried no validator is evidence of
        # nothing, however many bytes it delivered).
        return TransferResult(
            complete=True,
            bytes_written=expected if missing else 0,
            witness_bound=(
                resume.proof in (piece_store.PROOF_FRESH, piece_store.PROOF_LEDGER)
                and not any(fetcher.unbound for fetcher in fetchers.values())
            ),
            fetched=bool(missing),
        )
    finally:
        if tracked and pieces > 0 and not all(bits):
            for index, fetcher in fetchers.items():
                if fetcher.completed:
                    bits[index] = 1
            durable = True
            try:
                fd.flush()
                os.fsync(fd.fileno())
            except OSError:
                durable = False
            if not durable:
                log("  could not flush data to disk; keeping the last durable resume state")
            else:
                try:
                    current = os.path.getsize(target)
                except OSError:
                    current = 0
                saved = piece_store.save(
                    target,
                    piece_store.Bitmap(
                        size=expected,
                        pieces=pieces,
                        bits=bits,
                        min_size=current,
                        piece_size=piece_size,
                        url_sha256=identity,
                        validator=validator,
                    ),
                )
                if not saved:
                    log("  could not save resume state (write failed)")
        fd.close()
