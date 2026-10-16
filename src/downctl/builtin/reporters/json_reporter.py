"""The JSON-lines reporter plugin (machine-readable progress)."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, TextIO

from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.task import DownloadTask


class JSONReporter:
    """Emit one JSON object per line for tooling and log pipelines."""

    name = "json"
    api_version = API_VERSION
    priority = 0

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def _emit(self, event: str, task: DownloadTask, **extra: object) -> None:
        record = {"event": event, "target": task.target, "url": task.url, **extra}
        self._stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._stream.flush()

    def task_started(self, task: DownloadTask) -> None:
        self._emit("start", task, size=task.size)

    def task_progress(self, task: DownloadTask, delta: int) -> None:
        self._emit("progress", task, delta=delta)

    def task_finished(self, task: DownloadTask, ok: bool) -> None:
        self._emit("finish", task, ok=ok)
