"""The webhook hook plugin: POST lifecycle events to an HTTP endpoint.

Fire-and-forget: each selected event schedules a POST so a slow endpoint never
blocks a download. Options (``[plugins.webhook]``):

- ``url``:    endpoint to POST to (required to do anything)
- ``events``: list of events to send (default ``["on_finish"]``)
- ``timeout``: request timeout in seconds (default 30)
"""

from __future__ import annotations

import asyncio
from typing import Any

from downctl import console
from downctl.plugin.protocols import API_VERSION


class WebhookHook:
    """POST JSON payloads for selected lifecycle events."""

    name = "webhook"
    api_version = API_VERSION
    priority = 0

    def __init__(self, url: str = "", events: list[str] | None = None, timeout: int = 30) -> None:
        self.url = url
        self.events = events or ["on_finish"]
        self.timeout = timeout
        self._pending: set[asyncio.Task[None]] = set()

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        if not self.url or event not in self.events:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - only in sync contexts
            return
        task = loop.create_task(self._post(event, payload))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def aclose(self) -> None:
        """Wait for any in-flight POSTs so late events are not lost."""
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    async def _post(self, event: str, payload: dict[str, object]) -> None:
        import aiohttp

        body: dict[str, Any] = {"event": event}
        body.update({key: _serialize(value) for key, value in payload.items()})
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await session.post(self.url, json=body)
        except Exception as exc:  # noqa: BLE001 - a notification failure is not fatal
            console.warn(f"webhook: POST to {self.url} failed: {exc}")


def _serialize(value: object) -> object:
    if hasattr(value, "target") and hasattr(value, "url"):
        task = value
        return {
            "url": getattr(task, "url", ""),
            "target": getattr(task, "target", ""),
            "source": getattr(task, "source", ""),
        }
    return value
