"""The tqdm progress reporter plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.task import DownloadTask


class TqdmReporter:
    """Render per-task progress bars with tqdm."""

    name = "tqdm"
    api_version = API_VERSION
    priority = 0

    def __init__(self) -> None:
        self._bars: dict[str, object] = {}

    def task_started(self, task: DownloadTask) -> None:
        from tqdm import tqdm

        self._bars[task.target] = tqdm(
            total=task.size or None,
            desc=task.target[:38],
            unit="B",
            unit_scale=True,
            leave=False,
            dynamic_ncols=True,
        )

    def task_progress(self, task: DownloadTask, delta: int) -> None:
        bar = self._bars.get(task.target)
        if bar is not None:
            bar.update(delta)  # type: ignore[attr-defined]

    def task_finished(self, task: DownloadTask, ok: bool) -> None:
        bar = self._bars.pop(task.target, None)
        if bar is not None:
            bar.close()  # type: ignore[attr-defined]
