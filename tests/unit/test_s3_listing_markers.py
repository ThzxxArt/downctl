"""S3 directory markers are prefixes, not objects.

A real listing contains keys ending in ``/`` (``dir/``). Treating them as
objects produced a task whose basename was empty -- a target that could only
fail after a presigned GET. The listing must skip them (and zero-length keys).
"""

from __future__ import annotations

from dataclasses import dataclass

from downctl.builtin.sources import s3_source
from downctl.builtin.sources.s3_source import S3Source
from downctl.config import Config
from downctl.core.context import RuntimeContext
from downctl.core.task import DownloadTask

S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"


@dataclass
class _FakeResponse:
    body: str
    status: int = 200

    async def text(self) -> str:
        return self.body


class _FakeRequest:
    def __init__(self, body: str) -> None:
        self._response = _FakeResponse(body)

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc: object) -> None:
        return None


def _listing(keys: list[str]) -> str:
    rows = "".join(f"<Contents><Key>{key}</Key><Size>{len(key)}</Size></Contents>" for key in keys)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<ListBucketResult xmlns="{S3_NS}">'
        "<IsTruncated>false</IsTruncated>"
        f"{rows}</ListBucketResult>"
    )


def _context() -> RuntimeContext:
    return RuntimeContext(
        session=object(),  # type: ignore[arg-type]
        config=Config(),
        options={"s3": {"access_key": "AKID", "secret_key": "SECRET"}},
    )


async def test_a_directory_marker_does_not_become_an_empty_target(
    monkeypatch,
) -> None:
    body = _listing(["dir/", "dir/a.bin", "b.bin"])

    def fake_request(*args: object, **kwargs: object) -> _FakeRequest:
        return _FakeRequest(body)

    monkeypatch.setattr(s3_source, "policy_request", fake_request)

    tasks: list[DownloadTask] = await S3Source().resolve("s3://bucket/dir/", _context())

    assert sorted(task.target for task in tasks) == ["a.bin", "b.bin"]
    assert all(task.target for task in tasks), "a task with an empty basename cannot download"


async def test_a_zero_length_key_is_not_a_task(monkeypatch) -> None:
    body = _listing(["", "a.bin"])

    def fake_request(*args: object, **kwargs: object) -> _FakeRequest:
        return _FakeRequest(body)

    monkeypatch.setattr(s3_source, "policy_request", fake_request)

    tasks: list[DownloadTask] = await S3Source().resolve("s3://bucket/", _context())

    assert [task.target for task in tasks] == ["a.bin"]
