"""Tests for the webhook hook."""

from __future__ import annotations

import asyncio

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
    assert site._server is not None  # noqa: SLF001
    port = site._server.sockets[0].getsockname()[1]
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
    assert received[0]["task"] == {"url": "https://h/f", "target": "f.bin", "source": "url"}


async def test_ignores_unselected_events(sink) -> None:
    url, received = sink
    hook = WebhookHook(url=url, events=["on_finish"])
    hook.on_event("on_start", {"targets": []})
    await asyncio.sleep(0.1)
    assert received == []


async def test_no_url_is_noop() -> None:
    WebhookHook().on_event("on_finish", {"succeeded": 1})


async def test_aclose_flushes_pending(sink) -> None:
    from downctl.plugin.hooks import EventBus

    url, received = sink
    hook = WebhookHook(url=url, events=["on_finish"])
    bus = EventBus()
    bus.register_hook(hook)
    bus.emit("on_finish", {"succeeded": 3, "failed": 1})
    await bus.aclose()
    assert received and received[0]["succeeded"] == 3
