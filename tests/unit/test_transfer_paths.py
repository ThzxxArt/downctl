"""Failure paths of the resumable transfer loop.

The happy path lives in ``tests/integration/test_transfer.py`` (a real HTTP
server is the only honest way to test it). What a unit test pins far more
cheaply is the *distrust* machinery, which decides whether a corrupt file can
be reported as complete: a 206 that does not cover the requested window, a
body shorter than its declared length, a full body answering a conditional
range request, a checkpoint whose fsync fails, and the ledger saves that must
degrade to a warning instead of losing an otherwise fine download. Every test
here uses a scripted transport, so no socket, timing or server behaviour leaks
into the result.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import BinaryIO

import pytest

from downctl.config import Config
from downctl.core import pieces as piece_store
from downctl.core import transfer as transfer_mod
from downctl.core.context import RuntimeContext
from downctl.core.errors import (
    ContentChangedError,
    DownctlError,
    PermanentHTTPError,
    RangeMismatchError,
    RangeNotSatisfiableError,
    RangeSizeMismatchError,
    TransportError,
)
from downctl.core.ratelimit import RateLimiter
from downctl.core.transfer import (
    _classify,
    _download_stream,
    _flatten,
    _PieceFetcher,
    _probe_range_support,
    _Progress,
    _raise_leaf,
    _stream_watchdog,
    transfer_file,
)
from downctl.plugin.protocols import Response
from downctl.urls import url_identity

URL = "https://example.test/file.bin"
IDENTITY = url_identity(URL)
MIB = 1 << 20


class FakeResponse(Response, AbstractAsyncContextManager["FakeResponse"]):
    """A scripted transport response (``Response`` protocol shape)."""

    def __init__(
        self,
        status: int,
        *,
        headers: Mapping[str, str] | None = None,
        chunks: tuple[bytes, ...] = (),
        content_length: int | None = None,
        delay: float = 0.0,
        hang: bool = False,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self._content_length = content_length
        self._chunks = chunks
        self._delay = delay
        self._hang = hang

    @property
    def content_length(self) -> int | None:
        """Declared body length, as the ``Response`` protocol exposes it."""
        return self._content_length

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def iter_chunks(self, size: int = 65536) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield chunk
        if self._hang:
            # Never yields and never returns: the stall watchdog must intervene.
            await asyncio.Event().wait()


class FakeTransport:
    """A range-capable transport whose responses are scripted per request."""

    name = "fake"
    api_version = "1"
    priority = 0
    sends_content_range = True

    def __init__(
        self,
        handler: Callable[[tuple[int, int] | None], FakeResponse],
        *,
        supports_ranges: bool = True,
    ) -> None:
        self._handler = handler
        self.ranges: list[tuple[int, int] | None] = []
        self._supports_ranges = supports_ranges

    def supports_range(self) -> bool:
        return self._supports_ranges

    def request(
        self,
        ctx: RuntimeContext,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> FakeResponse:
        self.ranges.append(byte_range)
        return self._handler(byte_range)


def make_ctx() -> RuntimeContext:
    return RuntimeContext(session=object(), config=Config())  # type: ignore[arg-type]


def make_fetcher(
    transport: FakeTransport,
    fd: BinaryIO,
    start: int,
    end: int,
    *,
    expected: int = 0,
    if_range: str = "",
    headers: dict[str, str] | None = None,
    rate: RateLimiter | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> _PieceFetcher:
    return _PieceFetcher(
        make_ctx(),
        transport,
        URL,
        dict(headers or {}),
        fd,
        asyncio.Lock(),
        start,
        end,
        rate,
        _Progress(),
        on_progress,
        lambda: None,
        expected=expected,
        if_range=if_range,
    )


def range_response(
    data: bytes, byte_range: tuple[int, int] | None, **kwargs: object
) -> FakeResponse:
    """A 206 whose ``Content-Range`` covers exactly ``byte_range``."""
    assert byte_range is not None
    start, end = byte_range
    return FakeResponse(
        206,
        headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"},
        content_length=end - start + 1,
        **kwargs,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# _PieceFetcher: the response is asserted, never trusted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        pytest.param(FakeResponse(206), RangeMismatchError, id="missing-header"),
        pytest.param(
            FakeResponse(206, headers={"Content-Range": "bytes 4-9/16"}),
            RangeMismatchError,
            id="wrong-window",
        ),
        # A wrong *total* is not "try the same window again": the size the probe
        # reported is stale, and only a fresh probe can make the retry valid.
        pytest.param(
            FakeResponse(206, headers={"Content-Range": "bytes 0-5/99"}),
            RangeSizeMismatchError,
            id="wrong-total",
        ),
    ],
)
async def test_piece_fetcher_rejects_a_206_that_does_not_cover_its_window(
    tmp_path: Path, response: FakeResponse, expected_error: type[Exception]
) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"\0" * 16)
    transport = FakeTransport(lambda _rng: response)
    with target.open("r+b") as fd:
        fetcher = make_fetcher(transport, fd, 0, 5, expected=16)
        with pytest.raises(expected_error):
            await fetcher.run()
    assert target.read_bytes() == b"\0" * 16  # not a byte written


async def test_piece_fetcher_reports_an_unexpected_status(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"\0" * 16)
    transport = FakeTransport(lambda _rng: FakeResponse(500, content_length=16))
    with target.open("r+b") as fd:
        fetcher = make_fetcher(transport, fd, 0, 5, expected=16)
        with pytest.raises(TransportError, match="unexpected HTTP 500"):
            await fetcher.run()


async def test_piece_fetcher_truncates_an_overlong_body_to_its_window(tmp_path: Path) -> None:
    """A server that streams past the window must not overwrite its neighbours."""
    target = tmp_path / "f.bin"
    target.write_bytes(b"\0" * 16)
    transport = FakeTransport(
        lambda rng: range_response(b"a" * 16, rng, chunks=(b"a" * 8, b"b" * 8, b"c" * 8))
    )
    seen: list[int] = []
    with target.open("r+b") as fd:
        fetcher = make_fetcher(transport, fd, 0, 9, expected=16, on_progress=seen.append)
        await fetcher.run()
    assert fetcher.completed is True
    assert target.read_bytes() == b"a" * 8 + b"b" * 2 + b"\0" * 6
    assert seen == [8, 2]


async def test_piece_fetcher_leaves_the_piece_incomplete_on_a_short_body(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"\0" * 16)
    transport = FakeTransport(lambda rng: range_response(b"a" * 16, rng, chunks=(b"abc",)))
    with target.open("r+b") as fd:
        fetcher = make_fetcher(transport, fd, 0, 9, expected=16)
        await fetcher.run()
    assert fetcher.completed is False
    assert target.read_bytes() == b"abc" + b"\0" * 13


async def test_piece_fetcher_paces_its_body_through_the_rate_limiter(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"\0" * 4)
    transport = FakeTransport(lambda rng: range_response(b"a" * 4, rng, chunks=(b"abcd",)))
    seen: list[int] = []
    with target.open("r+b") as fd:
        fetcher = make_fetcher(
            transport, fd, 0, 3, expected=4, rate=RateLimiter(1_000_000), on_progress=seen.append
        )
        await fetcher.run()
    assert fetcher.completed is True
    assert target.read_bytes() == b"abcd"
    assert seen == [4]


async def test_piece_fetcher_flags_a_full_body_as_no_range_support(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"\0" * 16)
    transport = FakeTransport(
        lambda _rng: FakeResponse(200, content_length=16, chunks=(b"z" * 16,))
    )
    with target.open("r+b") as fd:
        fetcher = make_fetcher(transport, fd, 0, 5, expected=16, if_range='"v1"')
        await fetcher.run()
    assert fetcher.supports_range is False
    assert fetcher.completed is False
    assert transport.ranges == [(0, 5)]
    assert target.read_bytes() == b"\0" * 16


async def test_piece_fetcher_skips_the_range_assert_for_non_http_transports(
    tmp_path: Path,
) -> None:
    """A local/SFTP transport answers a range with a headerless 206."""

    class LocalTransport(FakeTransport):
        """No ``sends_content_range``: the window cannot be asserted."""

        sends_content_range = False

    target = tmp_path / "f.bin"
    target.write_bytes(b"\0" * 8)
    transport = LocalTransport(lambda _rng: FakeResponse(206, chunks=(b"abcd",)))
    with target.open("r+b") as fd:
        fetcher = make_fetcher(transport, fd, 0, 3, expected=8)
        await fetcher.run()
    assert fetcher.completed is True
    assert target.read_bytes() == b"abcd" + b"\0" * 4


def test_request_headers_never_lets_a_caller_disable_if_range(tmp_path: Path) -> None:
    transport = FakeTransport(lambda _rng: FakeResponse(206))
    with (tmp_path / "f.bin").open("w+b") as fd:
        conditional = make_fetcher(
            transport, fd, 0, 5, if_range='"v1"', headers={"If-Range": '"user"', "X-A": "1"}
        )
        assert conditional.request_headers() == {"If-Range": '"v1"', "X-A": "1"}
        plain = make_fetcher(transport, fd, 0, 5, headers={"X-A": "1"})
        assert plain.request_headers() == {"X-A": "1"}


# --------------------------------------------------------------------------- #
# _probe_range_support
# --------------------------------------------------------------------------- #
async def test_probe_range_support_accepts_a_server_without_ranges() -> None:
    transport = FakeTransport(lambda _rng: FakeResponse(200, content_length=4, chunks=(b"abcd",)))
    assert await _probe_range_support(make_ctx(), transport, URL, {}) is None
    assert transport.ranges == [(0, 0)]


async def test_probe_range_support_reports_a_replaced_object() -> None:
    transport = FakeTransport(
        lambda _rng: FakeResponse(206, headers={"ETag": '"v2"'}, chunks=(b"a",))
    )
    with pytest.raises(ContentChangedError) as caught:
        await _probe_range_support(make_ctx(), transport, URL, {})
    assert caught.value.validator == '"v2"'
    assert caught.value.if_range == '"v2"'


# --------------------------------------------------------------------------- #
# _flatten / _classify / _raise_leaf
# --------------------------------------------------------------------------- #
def test_flatten_unwraps_nested_groups() -> None:
    inner = ExceptionGroup("inner", [ValueError("v"), OSError("o")])
    outer = BaseExceptionGroup("outer", [inner, KeyError("k")])
    flat = _flatten(outer)
    assert sorted(type(exc).__name__ for exc in flat) == ["KeyError", "OSError", "ValueError"]


def test_classify_picks_the_most_actionable_category() -> None:
    assert _classify([ContentChangedError(URL)]) == "changed"
    assert _classify([RangeNotSatisfiableError(URL)]) == "range"
    assert _classify([PermanentHTTPError(404, URL)]) == "permanent"
    assert _classify([TransportError("drop")]) == "transport"
    assert _classify([OSError("disk")]) == "os"
    assert _classify([DownctlError("boom")]) == "os"
    assert _classify([ValueError("plain")]) == "other"
    assert _classify([]) == "other"


def test_raise_leaf_reraises_the_most_specific_failure() -> None:
    with pytest.raises(ContentChangedError):
        _raise_leaf([OSError("disk"), ContentChangedError(URL)])
    with pytest.raises(ValueError):
        _raise_leaf([ValueError("plain")])
    with pytest.raises(RuntimeError, match="unknown transfer failure"):
        _raise_leaf([])


# --------------------------------------------------------------------------- #
# _download_stream: stalls, cancellation, short bodies
# --------------------------------------------------------------------------- #
async def test_stalled_stream_is_reported_as_a_stall_not_a_failure(tmp_path: Path) -> None:
    transport = FakeTransport(lambda _rng: FakeResponse(200, content_length=None, hang=True))
    warnings: list[str] = []
    with (tmp_path / "f.bin").open("w+b") as fd:
        result = await _download_stream(
            make_ctx(), transport, URL, fd, {}, None, None, warnings.append, 1
        )
    assert (result.complete, result.stalled) == (False, True)
    assert any("stalled 1s with no progress" in message for message in warnings)


async def test_cancelled_stream_propagates_cancellation(tmp_path: Path) -> None:
    transport = FakeTransport(lambda _rng: FakeResponse(200, content_length=None, hang=True))
    with (tmp_path / "f.bin").open("w+b") as fd:
        task = asyncio.create_task(
            _download_stream(make_ctx(), transport, URL, fd, {}, None, None, lambda _m: None, 30)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_stream_watchdog_keeps_waiting_while_the_clock_has_not_expired() -> None:
    """One 1-second poll without a stall must loop, not abort."""
    log: list[str] = []
    done = asyncio.Event()
    state = {"last": asyncio.get_running_loop().time() + 5}
    task = asyncio.create_task(_stream_watchdog(state, 30, log.append, done))
    await asyncio.sleep(1.05)
    assert task.done() is False
    done.set()
    await asyncio.wait_for(task, timeout=2)
    assert log == []


async def test_stream_reports_its_body_through_the_rate_limiter(tmp_path: Path) -> None:
    body = b"wxyz"
    transport = FakeTransport(
        lambda _rng: FakeResponse(200, content_length=len(body), chunks=(body,))
    )
    seen: list[int] = []
    with (tmp_path / "f.bin").open("w+b") as fd:
        result = await _download_stream(
            make_ctx(),
            transport,
            URL,
            fd,
            {},
            RateLimiter(1_000_000),
            seen.append,
            lambda _m: None,
            30,
        )
    assert (result.complete, result.stalled) == (True, False)
    assert seen == [len(body)]


async def test_stream_reraises_a_transport_failure_instead_of_reporting_success(
    tmp_path: Path,
) -> None:
    class Exploding(FakeResponse):
        async def iter_chunks(self, size: int = 65536) -> AsyncIterator[bytes]:
            raise TransportError("connection reset")
            yield b""  # pragma: no cover - unreachable, keeps this an async generator

    transport = FakeTransport(lambda _rng: Exploding(200, content_length=None))
    with (
        (tmp_path / "f.bin").open("w+b") as fd,
        pytest.raises(TransportError, match="connection reset"),
    ):
        await _download_stream(make_ctx(), transport, URL, fd, {}, None, None, lambda _m: None, 30)


async def test_transfer_rejects_a_body_shorter_than_its_declared_length(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    transport = FakeTransport(lambda _rng: FakeResponse(200, content_length=100, chunks=(b"abc",)))
    result = await transfer_file(make_ctx(), transport, URL, str(target), 0)
    assert (result.complete, result.stalled) == (False, False)
    assert target.read_bytes() == b"abc"
    assert transport.ranges == [None]


async def test_transfer_rejects_a_body_shorter_than_the_expected_size(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    transport = FakeTransport(
        lambda _rng: FakeResponse(200, content_length=None, chunks=(b"abcd",)),
        supports_ranges=False,
    )
    result = await transfer_file(make_ctx(), transport, URL, str(target), 10)
    assert (result.complete, result.stalled) == (False, False)
    assert target.read_bytes() == b"abcd"


# --------------------------------------------------------------------------- #
# transfer_file: grids, resumes, fallbacks
# --------------------------------------------------------------------------- #
async def test_transfer_rejects_an_unusable_piece_grid(tmp_path: Path) -> None:
    transport = FakeTransport(lambda _rng: FakeResponse(206))
    with pytest.raises(DownctlError, match="maximum"):
        await transfer_file(
            make_ctx(), transport, URL, str(tmp_path / "f.bin"), 10**12, piece_size=1024
        )
    assert transport.ranges == []


async def test_transfer_short_circuits_a_fully_complete_ledger(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    expected = 4096
    target.write_bytes(b"z" * expected)
    piece_size, pieces, connections = piece_store.layout(expected, 2)
    assert piece_store.save(
        str(target),
        piece_store.Bitmap(
            size=expected,
            pieces=pieces,
            bits=[1] * pieces,
            min_size=expected,
            piece_size=piece_size,
            url_sha256=IDENTITY,
        ),
    )
    transport = FakeTransport(lambda _rng: FakeResponse(206))
    result = await transfer_file(
        make_ctx(),
        transport,
        URL,
        str(target),
        expected,
        connections=connections,
        piece_size=piece_size,
        identity=IDENTITY,
    )
    # Nothing was fetched, so nothing was written: ``bytes_written`` counts the
    # bytes this attempt actually wrote, not the size of the object.
    assert (result.complete, result.bytes_written) == (True, 0)
    assert transport.ranges == []  # nothing left to fetch
    assert not Path(piece_store.path_for(str(target))).exists()


async def test_transfer_keeps_an_unfinished_piece_in_the_ledger(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    transport = FakeTransport(lambda rng: range_response(b"a" * 8, rng, chunks=(b"abc",)))
    result = await transfer_file(make_ctx(), transport, URL, str(target), 8, connections=1)
    assert result.complete is False
    bitmap = piece_store.load(str(target))
    assert bitmap is not None
    assert bitmap.bits == [0]
    assert bitmap.min_size == 3  # the durable high-water mark, not the full size


async def test_checkpoint_records_a_piece_that_finished_mid_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot must include pieces that completed before the tick."""
    target = tmp_path / "f.bin"
    data = b"c" * MIB

    def handler(byte_range: tuple[int, int] | None) -> FakeResponse:
        assert byte_range is not None
        start, end = byte_range
        delay = 0.0 if start == 0 else 0.3
        return range_response(data, byte_range, chunks=(data[start : end + 1],), delay=delay)

    saved: list[list[int]] = []
    real_save = piece_store.save

    def recording_save(path: str, bitmap: piece_store.Bitmap) -> bool:
        saved.append(list(bitmap.bits))
        return real_save(path, bitmap)

    monkeypatch.setattr(piece_store, "save", recording_save)
    result = await transfer_file(
        make_ctx(),
        FakeTransport(handler),
        URL,
        str(target),
        len(data),
        connections=2,
        checkpoint=0.05,
        log=lambda _message: None,
    )
    assert result.complete is True
    assert target.read_bytes() == data
    # A checkpoint ran while the slow piece was still in flight and recorded the
    # fast piece as complete.
    assert [1, 0] in saved


async def test_transfer_reports_a_stalled_piece_without_failing(tmp_path: Path) -> None:
    """A stall keeps the ledger and asks the caller to retry."""
    target = tmp_path / "f.bin"

    def handler(byte_range: tuple[int, int] | None) -> FakeResponse:
        assert byte_range is not None
        return range_response(b"a" * MIB, byte_range, hang=True)

    result = await transfer_file(
        make_ctx(),
        FakeTransport(handler),
        URL,
        str(target),
        MIB,
        connections=2,
        stall_limit=1,
        log=lambda _message: None,
    )
    assert (result.complete, result.stalled) == (False, True)
    assert result.bytes_written == 0
    assert piece_store.load(str(target)) is not None


async def test_transfer_survives_a_watchdog_handle_that_was_never_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the watchdog in ``finally`` must tolerate having no handle."""
    target = tmp_path / "f.bin"
    body = b"01234567"
    real_create_task = asyncio.TaskGroup.create_task

    def lose_the_handle(self: asyncio.TaskGroup, coro: object, **kwargs: object) -> None:
        real_create_task(self, coro, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio.TaskGroup, "create_task", lose_the_handle)
    transport = FakeTransport(lambda rng: range_response(body, rng, chunks=(body,)))
    result = await transfer_file(make_ctx(), transport, URL, str(target), len(body), connections=1)
    assert result.complete is True
    assert target.read_bytes() == body


async def test_transfer_propagates_cancellation_and_keeps_the_ledger(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"

    def handler(byte_range: tuple[int, int] | None) -> FakeResponse:
        assert byte_range is not None
        return range_response(b"a" * MIB, byte_range, hang=True)

    task = asyncio.create_task(
        transfer_file(
            make_ctx(),
            FakeTransport(handler),
            URL,
            str(target),
            MIB,
            connections=2,
            stall_limit=30,
            log=lambda _message: None,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    bitmap = piece_store.load(str(target))
    assert bitmap is not None
    assert bitmap.done_pieces == 0


async def test_transfer_falls_back_without_probing_when_the_request_was_unconditional(
    tmp_path: Path,
) -> None:
    """Only a conditional 200 needs the probe; an unconditional one is final."""
    target = tmp_path / "f.bin"
    body = b"01234567"
    transport = FakeTransport(
        lambda _rng: FakeResponse(200, content_length=len(body), chunks=(body,))
    )
    result = await transfer_file(
        make_ctx(), transport, URL, str(target), len(body), connections=1, identity=IDENTITY
    )
    assert result.complete is True
    assert target.read_bytes() == body
    # No probe request (0, 0) was made: the range request and the stream only.
    assert transport.ranges == [(0, 7), None]


async def test_transfer_discards_everything_when_if_range_loses_the_race(tmp_path: Path) -> None:
    """A completed old revision must never be spliced into the new one."""
    target = tmp_path / "f.bin"
    responses = [
        FakeResponse(200, content_length=8, chunks=(b"12345678",)),
        FakeResponse(206, headers={"ETag": '"v2"'}, chunks=(b"1",)),
    ]
    transport = FakeTransport(lambda _rng: responses.pop(0))
    with pytest.raises(ContentChangedError):
        await transfer_file(
            make_ctx(),
            transport,
            URL,
            str(target),
            8,
            connections=1,
            identity=IDENTITY,
            validator='"v1"',
            if_range='"v1"',
        )
    assert transport.ranges == [(0, 7), (0, 0)]
    assert target.read_bytes() == b""
    assert not Path(piece_store.path_for(str(target))).exists()


async def test_transfer_falls_back_to_a_single_stream_when_ranges_are_ignored(
    tmp_path: Path,
) -> None:
    target = tmp_path / "f.bin"
    body = b"01234567"
    transport = FakeTransport(
        lambda _rng: FakeResponse(200, content_length=len(body), chunks=(body,))
    )
    result = await transfer_file(
        make_ctx(),
        transport,
        URL,
        str(target),
        len(body),
        connections=1,
        identity=IDENTITY,
        validator='"v1"',
        if_range='"v1"',
    )
    assert result.complete is True
    assert target.read_bytes() == body
    assert transport.ranges == [(0, 7), (0, 0), None]
    assert not Path(piece_store.path_for(str(target))).exists()


# --------------------------------------------------------------------------- #
# checkpoint and final-save failures degrade to warnings
# --------------------------------------------------------------------------- #
async def test_checkpoint_fsync_failure_never_persists_unsynced_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bits whose data never reached disk must not survive in a ledger."""
    target = tmp_path / "f.bin"

    def handler(byte_range: tuple[int, int] | None) -> FakeResponse:
        assert byte_range is not None
        start, end = byte_range
        return range_response(b"x" * MIB, byte_range, chunks=(b"x" * 8,), delay=0.15)

    transport = FakeTransport(handler)
    warnings: list[str] = []

    def no_fsync(_fd: int) -> None:
        raise OSError("fsync refused")

    monkeypatch.setattr(transfer_mod.os, "fsync", no_fsync)
    result = await transfer_file(
        make_ctx(),
        transport,
        URL,
        str(target),
        MIB,
        connections=2,
        checkpoint=0.05,
        log=warnings.append,
    )
    assert (result.complete, result.stalled) == (False, False)
    assert any("could not checkpoint resume state (write failed)" in m for m in warnings)
    assert any("could not flush data to disk" in m for m in warnings)
    assert piece_store.load(str(target)) is None


async def test_a_raising_checkpoint_never_aborts_a_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.bin"
    data = bytes(range(256)) * 4096  # exactly 1 MiB

    def handler(byte_range: tuple[int, int] | None) -> FakeResponse:
        assert byte_range is not None
        start, end = byte_range
        return range_response(data, byte_range, chunks=(data[start : end + 1],), delay=0.2)

    def boom(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("ledger exploded")

    warnings: list[str] = []
    monkeypatch.setattr(piece_store, "save", boom)
    result = await transfer_file(
        make_ctx(),
        FakeTransport(handler),
        URL,
        str(target),
        len(data),
        connections=2,
        checkpoint=0.05,
        log=warnings.append,
    )
    assert result.complete is True
    assert target.read_bytes() == data
    assert any("could not checkpoint resume state: ledger exploded" in m for m in warnings)


async def test_final_ledger_save_failure_is_only_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.bin"
    data = b"a" * MIB

    def handler(byte_range: tuple[int, int] | None) -> FakeResponse:
        assert byte_range is not None
        start, end = byte_range
        body = data[start : end + 1] if start == 0 else b"short"
        return range_response(data, byte_range, chunks=(body,))

    def no_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("rename refused")

    warnings: list[str] = []
    monkeypatch.setattr(piece_store.os, "replace", no_replace)
    result = await transfer_file(
        make_ctx(),
        FakeTransport(handler),
        URL,
        str(target),
        len(data),
        connections=2,
        log=warnings.append,
    )
    assert (result.complete, result.stalled) == (False, False)
    assert target.read_bytes()[:16] == data[:16]
    assert any("could not save resume state (write failed)" in m for m in warnings)
    assert piece_store.load(str(target)) is None


async def test_a_failing_stat_degrades_min_size_but_keeps_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``getsize`` failure must not abandon resume state for a valid piece."""
    target = tmp_path / "f.bin"
    data = b"b" * MIB

    def handler(byte_range: tuple[int, int] | None) -> FakeResponse:
        assert byte_range is not None
        start, end = byte_range
        body = data[start : end + 1] if start == 0 else b"short"
        return range_response(data, byte_range, chunks=(body,), delay=0.3)

    real_getsize = os.path.getsize
    calls = {"n": 0}

    def failing_getsize(path: str) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_getsize(path)
        raise OSError("stat refused")

    monkeypatch.setattr(transfer_mod.os.path, "getsize", failing_getsize)
    result = await transfer_file(
        make_ctx(),
        FakeTransport(handler),
        URL,
        str(target),
        len(data),
        connections=2,
        checkpoint=0.05,
        log=lambda _message: None,
    )
    assert result.complete is False
    assert calls["n"] >= 3  # once in resolve, then in the checkpoint and the save
    bitmap = piece_store.load(str(target))
    assert bitmap is not None
    assert bitmap.done_pieces == 1  # the finished piece is still resumable
    assert bitmap.min_size == 0  # ... but nothing could be proven about the size
    assert target.stat().st_size > 0


async def test_a_watchdog_that_never_starts_still_saves_the_partial_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.bin"

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("watchdog unavailable")

    monkeypatch.setattr(transfer_mod, "watch", boom)
    transport = FakeTransport(lambda rng: range_response(b"a" * 8, rng, chunks=(b"abc",)))
    with pytest.raises(RuntimeError, match="watchdog unavailable"):
        await transfer_file(make_ctx(), transport, URL, str(target), 8, connections=1)
    bitmap = piece_store.load(str(target))
    assert bitmap is not None
    assert bitmap.done_pieces == 0
