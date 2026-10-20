"""The logfile hook plugin: append lifecycle events to a file."""

from __future__ import annotations

import time
from pathlib import Path

from downctl.plugin.hooks import ConfigurableHook
from downctl.plugin.protocols import API_VERSION


class LogfileHook(ConfigurableHook):
    """Append one line per lifecycle event to a log file."""

    name = "logfile"
    api_version = API_VERSION
    priority = 0
    OPTIONS = {"path": "str"}

    def __init__(self, path: str = "downctl.log") -> None:
        self.path = path

    @property
    def path(self) -> Path:
        return self._path

    @path.setter
    def path(self, value: str | Path) -> None:
        # Keep the attribute a ``Path`` however it is set: a configured
        # ``path = "x.log"`` used to arrive as a plain string.
        self._path = Path(value)

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp}\t{event}\t{_summarize(payload)}\n"
        with Path(self.path).expanduser().open("a", encoding="utf-8") as fh:
            fh.write(line)


def _summarize(payload: dict[str, object]) -> str:
    parts: list[str] = []
    task = payload.get("task")
    if task is not None and hasattr(task, "target"):
        parts.append(f"target={task.target}")
    for key in ("ok", "succeeded", "failed", "error"):
        if key in payload:
            parts.append(f"{key}={payload[key]}")
    if not parts:
        parts = [f"{k}={v}" for k, v in payload.items()]
    text = " ".join(parts)
    # Keep the log TSV-safe (no tabs/newlines from filenames or messages).
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")
