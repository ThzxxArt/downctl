"""The webhook hook plugin: POST lifecycle events to an HTTP endpoint.

Fire-and-forget: each selected event schedules a POST so a slow endpoint never
blocks a download. Options (``[plugins.webhook]``):

- ``url``:    endpoint to POST to (required to do anything)
- ``events``: list of events to send (default ``["on_finish"]``)
- ``timeout``: request timeout in seconds (default 30)
"""

from __future__ import annotations

from typing import Any

from downctl import console
from downctl.plugin.hooks import ConfigurableHook, HookTasks
from downctl.plugin.protocols import API_VERSION
from downctl.urls import display_url, redact_text


class WebhookHook(ConfigurableHook):
    """POST JSON payloads for selected lifecycle events."""

    name = "webhook"
    api_version = API_VERSION
    priority = 0
    OPTIONS = {"url": "str", "events": "events", "timeout": "int"}

    def __init__(self, url: str = "", events: list[str] | None = None, timeout: int = 30) -> None:
        self.url = url
        self.events = events if events is not None else ["on_finish"]
        self.timeout = timeout
        self._tasks = HookTasks(self.name)

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        if not self.url or event not in self.events:
            return
        self._tasks.spawn(lambda: self._post(event, payload))

    async def aclose(self) -> int:
        """Wait for any in-flight POSTs so late events are not lost."""
        return await self._tasks.aclose()

    async def _post(self, event: str, payload: dict[str, object]) -> None:
        import aiohttp

        body: dict[str, Any] = {"event": event}
        body.update({key: _serialize(value) for key, value in payload.items()})
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(self.url, json=body) as response,
            ):
                if response.status >= 400:
                    console.warn(
                        f"webhook: POST to {display_url(self.url)} returned HTTP {response.status}",
                        always=True,
                    )
                    self._tasks.record()
        except Exception as exc:  # noqa: BLE001 - a notification failure is not fatal
            console.warn(
                f"webhook: POST to {display_url(self.url)} failed: {redact_text(str(exc))}",
                always=True,
            )
            self._tasks.record()


def _serialize(value: object) -> object:
    if hasattr(value, "target") and hasattr(value, "url"):
        task = value
        meta = getattr(task, "meta", {})
        redact = bool(meta.get("redact_url")) if isinstance(meta, dict) else False
        return {
            "url": display_url(str(getattr(task, "url", "")), redact),
            "target": _serialize(getattr(task, "target", "")),
            "source": _serialize(getattr(task, "source", "")),
            "origin": _serialize(display_url(str(getattr(task, "origin", "")))),
        }
    if isinstance(value, str):
        # Hook payloads can contain errors or arbitrary plugin strings; redact
        # URLs embedded in them so a webhook receiver never gets a signature.
        return redact_text(value)
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    return value
