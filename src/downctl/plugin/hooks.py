"""Lifecycle event bus for Hook plugins.

The engine emits a small, fixed set of events. Hook plugins subscribe to the
ones they care about. Events are delivered synchronously and any exception a
hook raises is contained so it can never break a download.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from collections.abc import Callable

from downctl.plugin.protocols import Hook

#: Canonical lifecycle events.
EVENTS = (
    "on_start",  # payload: {"targets": [...]}
    "on_task_start",  # payload: {"task": DownloadTask}
    "on_task_finish",  # payload: {"task": DownloadTask, "ok": bool}
    "on_error",  # payload: {"task": DownloadTask, "error": str}
    "on_finish",  # payload: {"succeeded": int, "failed": int}
)

Handler = Callable[[dict[str, object]], None]


class EventBus:
    """Dispatches events to Hook plugins and ad-hoc handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def register_hook(self, hook: Hook) -> None:
        for event in EVENTS:

            def handler(
                payload: dict[str, object], _hook: Hook = hook, _event: str = event
            ) -> None:
                _hook.on_event(_event, payload)

            self._handlers[event].append(handler)

    def subscribe(self, event: str, handler: Handler) -> None:
        self._handlers[event].append(handler)

    def emit(self, event: str, payload: dict[str, object]) -> None:
        for handler in self._handlers.get(event, []):
            try:
                handler(payload)
            except Exception as exc:  # noqa: BLE001 - a hook must never break a run
                print(f"downctl: hook error on {event}: {exc}", file=sys.stderr)
