"""The tqdm progress reporter plugin.

Shows a per-file bar when running files sequentially, and a single overall bar
(with a completed-files count) when several files download concurrently.
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
        self._bars: dict[int, object] = {}
        self._overall: object | None = None
        self._per_task = True
        self._total_tasks = 0
        self._completed = 0

    def run_started(self, total_tasks: int, total_bytes: int, jobs: int = 1) -> None:
        # Without a known total size there is no overall bar, so fall back to
        # per-task bars even when running concurrently.
        self._per_task = jobs <= 1 or total_bytes <= 0
        self._total_tasks = total_tasks
        self._completed = 0
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

    def run_resumed(self, done_bytes: int) -> None:
        """Advance the overall bar to account for already-complete bytes."""
        if self._overall is not None and done_bytes > 0:
            self._overall.update(done_bytes)  # type: ignore[attr-defined]

    def task_resumed(self, task: DownloadTask, done_bytes: int) -> None:
        """Jump a per-file bar to the resume point."""
        bar = self._bars.get(id(task))
        if bar is not None and done_bytes > 0:
            bar.update(done_bytes)  # type: ignore[attr-defined]

    def task_started(self, task: DownloadTask) -> None:
        if not self._per_task:
            return
        from tqdm import tqdm

        self._bars[id(task)] = tqdm(
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
        bar = self._bars.get(id(task))
        if bar is not None:
            bar.update(delta)  # type: ignore[attr-defined]
        if self._overall is not None:
            self._overall.update(delta)  # type: ignore[attr-defined]

    def task_finished(self, task: DownloadTask, ok: bool) -> None:
        bar = self._bars.pop(id(task), None)
        if bar is not None:
            bar.close()  # type: ignore[attr-defined]
        self._completed += 1
        if self._overall is not None:
            self._overall.set_description(  # type: ignore[attr-defined]
                f"total ({self._completed}/{self._total_tasks} files)"
            )

    def run_finished(self, succeeded: int, failed: int) -> None:
        if self._overall is not None:
            self._overall.close()  # type: ignore[attr-defined]
            self._overall = None
