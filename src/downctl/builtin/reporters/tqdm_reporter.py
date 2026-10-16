"""The tqdm progress reporter plugin.

Shows a per-file bar plus, when sizes are known, an overall bar spanning all
files in the run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.task import DownloadTask

BAR_FORMAT = "{l_bar}{bar:30}{r_bar}"


class TqdmReporter:
    """Render progress bars with tqdm."""

    name = "tqdm"
    api_version = API_VERSION
    priority = 0

    def __init__(self) -> None:
        self._bars: dict[str, object] = {}
        self._overall: object | None = None

    def run_started(self, total_tasks: int, total_bytes: int) -> None:
        if total_bytes <= 0:
            return
        from tqdm import tqdm

        self._overall = tqdm(
            total=total_bytes,
            position=0,
            desc=f"total ({total_tasks} files)",
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            leave=True,
            dynamic_ncols=True,
            bar_format=BAR_FORMAT,
        )

    def task_started(self, task: DownloadTask) -> None:
        from tqdm import tqdm

        self._bars[task.target] = tqdm(
            total=task.size or None,
            desc=task.target[:38],
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            dynamic_ncols=True,
            leave=False,
            position=1 if self._overall is not None else 0,
            mininterval=0.2,
            bar_format=BAR_FORMAT,
        )

    def task_progress(self, task: DownloadTask, delta: int) -> None:
        bar = self._bars.get(task.target)
        if bar is not None:
            bar.update(delta)  # type: ignore[attr-defined]
        if self._overall is not None:
            self._overall.update(delta)  # type: ignore[attr-defined]

    def task_finished(self, task: DownloadTask, ok: bool) -> None:
        bar = self._bars.pop(task.target, None)
        if bar is not None:
            bar.close()  # type: ignore[attr-defined]

    def run_finished(self, succeeded: int, failed: int) -> None:
        if self._overall is not None:
            self._overall.close()  # type: ignore[attr-defined]
            self._overall = None
