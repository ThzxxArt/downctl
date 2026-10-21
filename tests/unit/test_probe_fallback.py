"""Deterministic probe fallback tests using a fake session."""

from __future__ import annotations

from aiohttp import ClientError

from downctl.probe import probe_resource, probe_size


class _Resp:
    def __init__(self, status: int, headers: dict[str, str], url: str = "http://h/f") -> None:
        self.status = status
        self.headers = headers
        self.url = url

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _Session:
    def __init__(self, head: object, get: object | None = None) -> None:
        self._head = head
        self._get = get

    def head(self, *_a: object, **_k: object) -> object:
        if isinstance(self._head, Exception):
            raise self._head
        return self._head

    def get(self, *_a: object, **_k: object) -> object:
        if isinstance(self._get, Exception):
            raise self._get
        return self._get

    def request(self, _method: str, *_a: object, **_k: object) -> object:
        raise AssertionError("the probe only falls back to GET")


async def test_probe_size_falls_back_to_ranged_get() -> None:
    head = _Resp(200, {})  # no Content-Length
    get = _Resp(206, {"Content-Range": "bytes 0-0/12345"})
    size, status = await probe_size(_Session(head, get), "http://h/f")
    assert (size, status) == (12345, 206)


async def test_probe_size_fallback_without_range_header() -> None:
    head = _Resp(200, {})
    get = _Resp(200, {"Content-Length": "77"})
    size, status = await probe_size(_Session(head, get), "http://h/f")
    assert (size, status) == (77, 200)


async def test_probe_size_client_error() -> None:
    size, status = await probe_size(_Session(ClientError("x")), "http://h/f")
    assert (size, status) == (0, None)


async def test_probe_resource_head_metadata() -> None:
    head = _Resp(
        200,
        {"Content-Length": "10", "Content-Type": "application/json"},
        url="http://h/a.bin",
    )
    detail = await probe_resource(_Session(head), "http://h/a.bin")
    assert detail["size"] == 10
    assert detail["filename"] == "a.bin"
    assert detail["content_type"] == "application/json"
    assert detail["reachable"] is True


async def test_probe_resource_fallback_metadata() -> None:
    head = _Resp(200, {})
    get = _Resp(
        206,
        {"Content-Range": "bytes 0-0/999", "Content-Type": "text/plain"},
        url="http://h/final.bin",
    )
    detail = await probe_resource(_Session(head, get), "http://h/f")
    assert detail["size"] == 999
    assert detail["final_url"] == "http://h/final.bin"
    assert detail["filename"] == "final.bin"
    assert detail["content_type"] == "text/plain"
    assert detail["reachable"] is True


async def test_probe_resource_client_error() -> None:
    detail = await probe_resource(_Session(ClientError("x")), "http://h/f")
    assert detail["reachable"] is False
    assert detail["status"] is None
