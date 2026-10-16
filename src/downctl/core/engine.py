"""Download orchestration: turn tasks into completed files.

The engine owns the retry loop, disk checks, locking and checksum
verification. It selects a transport per task, drives
:mod:`downctl.core.transfer`, and reports progress through the reporter
plugin.

Implementation lands in P2. Public surface (stable):

- :func:`download_task` — download a single :class:`DownloadTask`
- :func:`download_all` — run many tasks subject to a job limit
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext
    from downctl.core.task import DownloadTask
    from downctl.plugin.protocols import Reporter


async def download_task(
    ctx: RuntimeContext,
    task: DownloadTask,
    *,
    reporter: Reporter | None = None,
) -> bool:
    """Download one task. Returns ``True`` on success."""
    raise NotImplementedError("core.engine.download_task lands in P2")


async def download_all(
    ctx: RuntimeContext,
    tasks: Sequence[DownloadTask],
    *,
    jobs: int = 1,
    reporter: Reporter | None = None,
) -> tuple[int, int]:
    """Download many tasks with a concurrency limit.

    Returns ``(succeeded, failed)``.
    """
    raise NotImplementedError("core.engine.download_all lands in P2")
