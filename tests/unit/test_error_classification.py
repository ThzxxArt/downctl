"""The error hierarchy must be *classified*, never guessed at.

0.1.3 routed the probe stage around the transport's classification, so a single
500 became a permanent failure; and no test in the suite referenced
``TransportError`` / ``TransientHTTPError`` / ``PermanentHTTPError`` at all,
which is exactly why nothing noticed. These tests pin the mapping down.
"""

from __future__ import annotations

import email.utils
import time
from collections.abc import AsyncIterator

import pytest

from downctl import app
from downctl.builtin.transports.http_transport import HTTPTransport
from downctl.config import Config
from downctl.core.context import RuntimeContext
from downctl.core.engine import RETRY_AFTER_MAX, retry_delay
from downctl.core.errors import (
    FAILURE_PERMANENT,
    FAILURE_TIMEOUT,
    FAILURE_TRANSIENT_EXHAUSTED,
    FAILURE_UNKNOWN,
    DownctlError,
    PermanentHTTPError,
    ProbeError,
    RangeNotSatisfiableError,
    TransientHTTPError,
    TransportError,
)
from downctl.core.task import DownloadTask
from downctl.probe import probe_metadata, retry_after


class _Stream:
    def __init__(self, payload: bytes = b"") -> None:
        self._payload = payload

    def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        async def generate() -> AsyncIterator[bytes]:
            for offset in range(0, len(self._payload), size):
                yield self._payload[offset : offset + size]

        return generate()


class _Response:
    def __init__(self, status: int, headers: dict[str, str] | None = None, payload=b"") -> None:
        self.status = status
        self.headers = headers or {}
        self.content_length = len(payload) or None
        self.content = _Stream(payload)
        self.url = "http://h/f"

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def get(self, *_args: object, **_kwargs: object) -> _Response:
        return self.response

    def head(self, *_args: object, **_kwargs: object) -> _Response:
        return self.response

    def request(self, _method: str, *_args: object, **_kwargs: object) -> _Response:
        return self.response


def make_ctx(response: _Response) -> RuntimeContext:
    return RuntimeContext(session=_Session(response), config=Config())  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_permanent_statuses_are_permanent(status: int) -> None:
    ctx = make_ctx(_Response(status))
    with pytest.raises(PermanentHTTPError) as caught:
        async with HTTPTransport().request(ctx, "http://h/f"):
            pass
    assert caught.value.status == status


@pytest.mark.parametrize("status", [500, 503, 429, 408, 425])
async def test_retryable_statuses_are_transient(status: int) -> None:
    ctx = make_ctx(_Response(status))
    with pytest.raises(TransientHTTPError) as caught:
        async with HTTPTransport().request(ctx, "http://h/f"):
            pass
    assert caught.value.status == status
    assert isinstance(caught.value, TransportError)


async def test_transient_error_carries_retry_after() -> None:
    ctx = make_ctx(_Response(429, {"Retry-After": "7"}))
    with pytest.raises(TransientHTTPError) as caught:
        async with HTTPTransport().request(ctx, "http://h/f"):
            pass
    assert caught.value.retry_after == 7.0


async def test_416_is_recoverable_not_permanent() -> None:
    """A 416 must be distinguishable: it means "re-probe", not "give up"."""
    ctx = make_ctx(_Response(416, {"Content-Range": "bytes */10"}))
    with pytest.raises(RangeNotSatisfiableError) as caught:
        async with HTTPTransport().request(ctx, "http://h/f", byte_range=(5, 9)):
            pass
    assert caught.value.requested == "bytes=5-9"
    assert not isinstance(caught.value, PermanentHTTPError)


async def test_success_yields_the_response() -> None:
    ctx = make_ctx(_Response(206, {"Content-Range": "bytes 0-3/10"}, b"abcd"))
    async with HTTPTransport().request(ctx, "http://h/f", byte_range=(0, 3)) as response:
        assert response.status == 206
        assert b"".join([chunk async for chunk in response.iter_chunks(2)]) == b"abcd"


def test_retry_after_accepts_seconds_dates_and_garbage() -> None:
    assert retry_after({"Retry-After": "12"}) == 12.0
    assert retry_after({}) is None
    assert retry_after({"Retry-After": "soon"}) is None
    assert retry_after({"Retry-After": "0"}) == 0.0

    past = email.utils.formatdate(time.time() - 60, usegmt=True)
    assert retry_after({"Retry-After": past}) == 0.0

    future = email.utils.formatdate(time.time() + 30, usegmt=True)
    delay = retry_after({"Retry-After": future})
    assert delay is not None and 25 <= delay <= 31


def test_retry_delay_honours_retry_after_and_caps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_RETRY_BASE", "4")
    assert retry_delay(1) >= 4  # exponential base plus jitter
    assert retry_delay(3) >= retry_delay(1)
    assert retry_delay(1, retry_after=2.5) == 2.5  # the server's hint wins
    assert retry_delay(1, retry_after=0) == 0.0
    assert retry_delay(1, retry_after=10**6) == RETRY_AFTER_MAX


@pytest.mark.parametrize("status", [500, 503, 429])
async def test_probe_classifies_transient_failures(status: int) -> None:
    result = await probe_metadata(_Session(_Response(status)), "http://h/f")
    assert result.status == status
    assert result.retryable is True
    assert result.reached is True
    assert result.ok is False


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_probe_reports_permanent_failures_as_final(status: int) -> None:
    result = await probe_metadata(_Session(_Response(status)), "http://h/f")
    assert result.status == status
    assert result.retryable is False
    assert result.error == f"HTTP {status}"


async def test_probe_reports_a_network_failure_as_retryable() -> None:
    class _Boom:
        def head(self, *_args: object, **_kwargs: object) -> object:
            raise OSError("no route to host")

        def get(self, *_args: object, **_kwargs: object) -> object:
            raise OSError("no route to host")

        def request(self, _method: str, *_args: object, **_kwargs: object) -> object:
            raise OSError("no route to host")

    result = await probe_metadata(_Boom(), "http://h/f")
    assert result.status is None
    assert result.retryable is True
    assert "no route" in result.error


async def test_probe_collects_the_version_witness() -> None:
    response = _Response(200, {"Content-Length": "10", "ETag": '"v3"'}, b"0123456789")
    result = await probe_metadata(_Session(response), "http://h/f")
    assert result.ok
    assert (result.size, result.validator, result.if_range) == (10, '"v3"', '"v3"')


# --------------------------------------------------------------------------- #
# the failure class a machine reads (T10a)
# --------------------------------------------------------------------------- #
def test_the_failure_classes_are_distinct() -> None:
    """These strings reach the failed log and the JSON stream verbatim."""
    assert len({FAILURE_PERMANENT, FAILURE_TRANSIENT_EXHAUSTED, FAILURE_TIMEOUT}) == 3
    assert FAILURE_UNKNOWN not in (
        FAILURE_PERMANENT,
        FAILURE_TRANSIENT_EXHAUSTED,
        FAILURE_TIMEOUT,
    )


def test_a_probe_error_is_classified_transient_exhausted() -> None:
    """``ProbeError`` is raised only once the probe retries have run out."""
    exc = ProbeError("probe failed for http://h/f after 3 attempts (HTTP 500)")
    assert app._failure_class(exc) == FAILURE_TRANSIENT_EXHAUSTED


def test_a_4xx_probe_failure_is_classified_permanent() -> None:
    assert app._failure_class(PermanentHTTPError(404, "http://h/f")) == FAILURE_PERMANENT


def test_a_target_that_never_starts_is_classified_permanent() -> None:
    """Anything that stops a target before the engine will not start next time."""
    assert app._failure_class(DownctlError("no source plugin can handle: foo://x")) == (
        FAILURE_PERMANENT
    )


def test_the_failed_log_line_carries_the_class_in_a_ninth_column() -> None:
    """Appended, never inserted: an 0.1.3 reader still parses the first eight."""
    task = DownloadTask(url="http://h/f", target="f.bin", size=10)
    line = app._fmt_url(task, FAILURE_TRANSIENT_EXHAUSTED)
    columns = line.split("\t")
    # v5 appends header names, transport and host after the class; v6 appends
    # the "credential was masked" flag. The first eight are byte-identical to
    # 0.1.3 and the ninth is still the class.
    assert len(columns) == 13
    assert columns[0] == "url" and columns[2] == "f.bin"
    assert columns[8] == FAILURE_TRANSIENT_EXHAUSTED
    assert columns[10] == ""  # transport, unset for a plain HTTP task
    # The 0.1.3 reader sees exactly what it always saw.
    kind, payload = app._parse_failed_line(line)
    assert kind == "url" and isinstance(payload, dict)
    assert payload["target"] == "f.bin"
    assert payload["reason"] == FAILURE_TRANSIENT_EXHAUSTED
    assert payload["headers"] == []


def test_an_unresolvable_target_line_carries_its_class_too() -> None:
    columns = app._fmt_target("foo://x", FAILURE_PERMANENT).split("\t")
    assert columns[:3] == ["target", "foo://x", FAILURE_PERMANENT]
    # v5 appends the error text (redacted) and the writer's host.
    assert len(columns) == 5
