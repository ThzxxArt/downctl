"""Tests for the webhook hook."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from aiohttp import web

from downctl.builtin.hooks.webhook_hook import WebhookHook
from downctl.core.task import DownloadTask


@pytest_asyncio.fixture
async def sink():
    received: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        received.append(await request.json())
        return web.Response(text="ok")

    application = web.Application()
    application.router.add_post("/hook", handler)
    runner = web.AppRunner(application, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"http://127.0.0.1:{port}/hook", received
    finally:
        await runner.cleanup()


async def _wait(received: list, count: int = 1) -> None:
    for _ in range(100):
        if len(received) >= count:
            return
        await asyncio.sleep(0.02)


async def test_posts_selected_event(sink) -> None:
    url, received = sink
    hook = WebhookHook(url=url, events=["on_finish"])
    hook.on_event("on_finish", {"succeeded": 2, "failed": 0})
    await _wait(received)
    assert received[0] == {"event": "on_finish", "succeeded": 2, "failed": 0}


async def test_serializes_task_payload(sink) -> None:
    url, received = sink
    task = DownloadTask(url="https://h/f", target="f.bin", source="url")
    hook = WebhookHook(url=url, events=["on_task_finish"])
    hook.on_event("on_task_finish", {"task": task, "ok": True})
    await _wait(received)
    assert received[0]["task"] == {
        "url": "https://h/f",
        "target": "f.bin",
        "source": "url",
        "origin": "",
    }


async def test_ignores_unselected_events(sink) -> None:
    """An unselected event must not be posted — proven without a sleep window.

    The old version slept 100 ms and then asserted, so a hook that simply
    posted slowly would have passed. ``aclose`` flushes every pending post, so
    an empty inbox afterwards is real evidence.
    """
    url, received = sink
    hook = WebhookHook(url=url, events=["on_finish"])
    hook.on_event("on_start", {"targets": []})
    await hook.aclose()
    assert received == []


async def test_redacts_a_presigned_task_url(sink) -> None:
    url, received = sink
    task = DownloadTask(
        url="http://s3/b?X-Amz-Signature=SECRET",
        target="b",
        origin="s3://bucket/b",
        meta={"redact_url": True},
    )
    hook = WebhookHook(url=url, events=["on_task_finish"])
    hook.on_event("on_task_finish", {"task": task, "ok": True})
    await _wait(received)
    assert "SECRET" not in received[0]["task"]["url"]
    assert received[0]["task"]["url"] == "http://s3/b"
    assert received[0]["task"]["origin"] == "s3://bucket/b"


async def test_redacts_credentials_in_the_origin(sink) -> None:
    url, received = sink
    task = DownloadTask(
        url="https://h/f",
        target="f",
        origin="https://h/f?token=SUPERSECRET",
    )
    hook = WebhookHook(url=url, events=["on_task_finish"])
    hook.on_event("on_task_finish", {"task": task, "ok": True})
    await _wait(received)
    assert "SUPERSECRET" not in received[0]["task"]["origin"]
    assert "token=***" in received[0]["task"]["origin"]


async def test_redacts_urls_in_an_error_payload(sink) -> None:
    url, received = sink
    hook = WebhookHook(url=url, events=["on_error"])
    hook.on_event(
        "on_error",
        {"error": "GET http://s3/b?X-Amz-Signature=SECRET failed"},
    )
    await _wait(received)
    assert "SECRET" not in received[0]["error"]
    assert "X-Amz-Signature=***" in received[0]["error"]


async def test_warns_when_the_endpoint_returns_http_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=503, text="unavailable")

    application = web.Application()
    application.router.add_post("/hook", handler)
    runner = web.AppRunner(application, shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        hook = WebhookHook(url=f"http://127.0.0.1:{port}/hook", timeout=1)
        await hook._post("on_finish", {})  # noqa: SLF001
    finally:
        await runner.cleanup()
    assert "HTTP 503" in capsys.readouterr().err


async def test_no_url_is_noop() -> None:
    WebhookHook().on_event("on_finish", {"succeeded": 1})


async def test_failure_warning_masks_webhook_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook = WebhookHook(url="http://127.0.0.1:1/hook?token=SUPERSECRET", timeout=1)
    await hook._post("on_finish", {})  # noqa: SLF001
    err = capsys.readouterr().err
    assert "SUPERSECRET" not in err
    assert "token=***" in err


async def test_aclose_flushes_pending(sink) -> None:
    from downctl.plugin.hooks import EventBus

    url, received = sink
    hook = WebhookHook(url=url, events=["on_finish"])
    bus = EventBus()
    bus.register_hook(hook)
    bus.emit("on_finish", {"succeeded": 3, "failed": 1})
    await bus.aclose()
    assert received and received[0]["succeeded"] == 3
