"""The JSON-lines reporter plugin (machine-readable progress).

Progress is throttled (one line per whole percent, or per 8 MiB when the size
is unknown) so a large download does not flood stdout.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, TextIO

from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.task import DownloadTask

UNKNOWN_SIZE_STEP = 8 * 1024 * 1024


class JSONReporter:
    """Emit one JSON object per line for tooling and log pipelines."""

    name = "json"
    api_version = API_VERSION
    priority = 0

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout
        self._downloaded: dict[int, int] = {}
        self._last_percent: dict[int, int] = {}

    def _emit(self, event: str, task: DownloadTask, **extra: object) -> None:
        record = {"event": event, "target": task.target, "url": task.url, **extra}
        self._stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._stream.flush()

    def task_started(self, task: DownloadTask) -> None:
        self._downloaded[id(task)] = 0
        self._last_percent[id(task)] = -1
        self._emit("start", task, size=task.size)

    def run_resumed(self, done_bytes: int) -> None:
        if done_bytes > 0:
            self._stream.write(
                json.dumps({"event": "run_resume", "downloaded": done_bytes}, ensure_ascii=False)
                + "\n"
            )
            self._stream.flush()

    def task_resumed(self, task: DownloadTask, done_bytes: int) -> None:
        if done_bytes <= 0:
            return
        self._downloaded[id(task)] = done_bytes
        total = task.size or 0
        self._last_percent[id(task)] = done_bytes * 100 // total if total > 0 else done_bytes
        self._emit("resume", task, downloaded=done_bytes, size=total)

    def task_progress(self, task: DownloadTask, delta: int) -> None:
        downloaded = self._downloaded.get(id(task), 0) + delta
        self._downloaded[id(task)] = downloaded
        total = task.size or 0
        if total > 0:
            percent = downloaded * 100 // total
            if percent == self._last_percent.get(id(task)):
                return
            self._last_percent[id(task)] = percent
        else:
            last = self._last_percent.get(id(task), 0)
            if downloaded - last < UNKNOWN_SIZE_STEP:
                return
            self._last_percent[id(task)] = downloaded
        self._emit("progress", task, downloaded=downloaded, size=total)

    def task_finished(self, task: DownloadTask, ok: bool) -> None:
        self._emit("finish", task, ok=ok, downloaded=self._downloaded.get(id(task), 0))
        self._downloaded.pop(id(task), None)
        self._last_percent.pop(id(task), None)
