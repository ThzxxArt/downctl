"""The quiet reporter plugin — a no-op for scripting and tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.task import DownloadTask


class QuietReporter:
    """Suppress all progress output."""

    name = "quiet"
    api_version = API_VERSION
    priority = 0

    def task_started(self, task: DownloadTask) -> None:
        pass

    def task_progress(self, task: DownloadTask, delta: int) -> None:
        pass

    def task_finished(self, task: DownloadTask, ok: bool) -> None:
        pass
