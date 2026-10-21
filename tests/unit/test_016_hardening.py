"""Regression tests for the 0.1.6 hardening fixes.

Each test pins a decision that used to have a second owner: the origin a
credential belongs to, the URL a failed log may store, and whether a transport
error is worth retrying.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from downctl import app
from downctl.builtin.transports import sftp_transport
from downctl.config import Config, CoreConfig
from downctl.core import engine
from downctl.core.context import RuntimeContext
from downctl.core.credentials import AuthMaterial
from downctl.core.errors import FAILURE_PERMANENT, PermanentError, TransportError
from downctl.core.task import DownloadTask
from downctl.plugin.hooks import EventBus
from downctl.session import _needs_hop_control


def test_failed_log_never_stores_an_origin_token() -> None:
    task = DownloadTask(
        url="https://example.test/a.bin?sig=deadbeef",
        target="a.bin",
        origin="https://example.test/a.bin?token=SUPERSECRET",
        source="url",
    )
    line = app._fmt_url(task, "permanent")
    assert "SUPERSECRET" not in line
    assert "a.bin" in line


def test_presigned_s3_record_stays_replayable() -> None:
    """The s3:// origin is the way back; stripping the signed URL is not a loss.

    Marking every presigned record unreplayable would have disabled
    ``--retry-failed`` for the one source that can always re-sign.
    """
    task = DownloadTask(
        url="https://bucket.s3.example/a.bin?X-Amz-Signature=s",
        target="a.bin",
        origin="s3://bucket/a.bin",
        source="s3",
    )
    task.meta["redact_url"] = True
    columns = app._fmt_url(task, "permanent").split("\t")
    assert columns[12] == ""


def test_a_bare_presigned_url_is_marked_unreplayable() -> None:
    task = DownloadTask(
        url="https://h/a.bin?token=SECRET",
        target="a.bin",
        origin="https://h/a.bin?token=SECRET",
        source="url",
    )
    columns = app._fmt_url(task, "permanent").split("\t")
    assert columns[12] == "1"


def test_a_user_header_forces_hop_control_but_range_does_not() -> None:
    # ``X-Api-Key`` has no aiohttp rule of its own, so the chain must be walked
    # here or a 302 hands it to the next host.
    assert _needs_hop_control(None, {}, None, {"X-Api-Key": "secret"}) is True
    # ``Range`` restates the HTTP request and is safe to forward; the fast path
    # stays available.
    assert _needs_hop_control(None, {}, None, {"Range": "bytes=0-99"}) is False


def test_hop_headers_merge_case_insensitively() -> None:
    """``authorization`` and ``Authorization`` are one header, not two."""
    from downctl.session import _hop_headers

    merged = _hop_headers(
        {"authorization": "user-supplied"},
        {"Authorization": "provider"},
        {},
        owns_origin=True,
    )
    assert merged == {"authorization": "user-supplied"}


class _SftpError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"sftp error {code}")
        self.code = code


def test_sftp_missing_file_is_permanent_not_transient() -> None:
    failure = sftp_transport._sftp_failure(_SftpError(2), "stat failed")
    assert isinstance(failure, PermanentError)


def test_sftp_connection_loss_stays_transient() -> None:
    failure = sftp_transport._sftp_failure(_SftpError(7), "connection lost")
    assert isinstance(failure, TransportError)
    assert not isinstance(failure, PermanentError)


def test_auth_check_reports_length_without_the_value() -> None:
    material = AuthMaterial(headers={"Authorization": "Bearer abcdef"}, source="env")
    assert app._material_length(material) == len("Bearer abcdef")
    basic = AuthMaterial(basic=("user", "pass"), source="netrc")
    assert app._material_length(basic) == len("user:pass")
    assert app._material_length(None) == 0


class _BoomTransport:
    """A transport whose very first request carries a permanent verdict."""

    name = "boom"
    api_version = "1"
    priority = 0
    sends_content_range = False

    def supports_range(self) -> bool:
        return True

    def request(self, *_args: object, **_kwargs: object) -> object:
        raise PermanentError("no such file")


class _RecordingReporter:
    name = "recording"
    api_version = "1"
    priority = 0

    def __init__(self) -> None:
        self.finished: list[bool] = []

    def task_started(self, _task: DownloadTask) -> None:
        pass

    def task_progress(self, _task: DownloadTask, _delta: int) -> None:
        pass

    def task_finished(self, _task: DownloadTask, ok: bool) -> None:
        self.finished.append(ok)


async def test_a_permanent_transport_error_is_reported_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SFTP's permanent verdict must reach the reporter and the failure class.

    It used to escape the retry loop, so ``task_finished`` never fired and
    ``on_error`` never emitted, while the task still counted as failed.
    """
    events = EventBus()
    ctx = RuntimeContext(
        session=object(),  # type: ignore[arg-type]
        config=Config(core=CoreConfig(dir=str(tmp_path))),
        events=events,
    )
    monkeypatch.setattr(engine, "_get_transport", lambda _ctx, _task: _BoomTransport())
    task = DownloadTask(url="https://h/f.bin", target="f.bin", size=7)
    reporter = _RecordingReporter()

    ok = await engine.download_task(
        ctx,
        task,
        reporter=reporter,
        retries=2,
        connections=1,
        stall_limit=5,
    )

    assert ok is False
    assert task.meta["failure"] == FAILURE_PERMANENT
    assert reporter.finished == [False]


class _Closer:
    def __init__(self) -> None:
        self.closed = 0

    def on_event(self, _event: str, _payload: dict[str, object]) -> None:
        pass

    async def aclose(self) -> int:
        self.closed += 1
        return 0


async def test_event_bus_close_is_idempotent() -> None:
    """The plan closes the bus to read ``strict_hooks``; the caller closes it again."""
    bus = EventBus()
    hook = _Closer()
    bus.register_hook(hook)  # type: ignore[arg-type]
    await bus.aclose()
    await bus.aclose()
    assert hook.closed == 1
