"""The logfile hook plugin: append lifecycle events to a file."""

from __future__ import annotations

import time
from pathlib import Path

from downctl.plugin.protocols import API_VERSION


class LogfileHook:
    """Append one line per lifecycle event to a log file."""

    name = "logfile"
    api_version = API_VERSION
    priority = 0

    def __init__(self, path: str = "downctl.log") -> None:
        self.path = Path(path)

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp}\t{event}\t{_summarize(payload)}\n"
        with Path(self.path).expanduser().open("a", encoding="utf-8") as fh:
            fh.write(line)


def _summarize(payload: dict[str, object]) -> str:
    task = payload.get("task")
    if task is not None and hasattr(task, "target"):
        return str(task.target)
    return " ".join(f"{k}={v}" for k, v in payload.items())
