"""Disk-space pre-flight shared by the engine and the streaming fallback.

Kept in one place so the reserve policy is consistent no matter which code
path decides whether to start (or continue) a download. Two shapes are
offered: :func:`has_space` for one target and :func:`batch_budget` for a whole
run, because a per-file check cannot see that four 50 GB targets do not fit on
a 100 GB disk together.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass

from downctl.core.errors import DiskSpaceError
from downctl.platform_utils import human_size

#: Headroom kept free so a download cannot wedge the whole filesystem.
DISK_RESERVE = 200 * (1 << 20)  # 200 MiB


def free_space(path: str) -> int | None:
    """Free bytes on the filesystem holding ``path``, or ``None`` if unknown."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        return shutil.disk_usage(directory).free
    except OSError:
        return None


def has_space(path: str, need: int) -> bool:
    """Whether ``need`` more bytes (plus the reserve) fit for ``path``."""
    if need <= 0:
        return True
    free = free_space(path)
    if free is None:
        return True
    return need + DISK_RESERVE <= free


def ensure_space(path: str, need: int) -> None:
    """Raise :class:`DiskSpaceError` when there is not enough room."""
    if not has_space(path, need):
        raise DiskSpaceError(f"not enough free space for {path}")


def _device_key(path: str) -> str:
    """Identity of the filesystem a path will be written to."""
    probe = os.path.dirname(os.path.abspath(path)) or "."
    while True:
        try:
            return str(os.stat(probe).st_dev)
        except OSError:
            parent = os.path.dirname(probe)
            if parent == probe:
                return probe
            probe = parent


@dataclass(frozen=True)
class Shortfall:
    """One filesystem that cannot hold the targets assigned to it."""

    directory: str
    needed: int
    free: int
    targets: int


@dataclass(frozen=True)
class BatchBudget:
    """The result of pre-flighting a whole batch."""

    #: Parallel to the input entries: ``False`` means "do not even start".
    fits: tuple[bool, ...]
    shortfalls: tuple[Shortfall, ...] = ()

    @property
    def message(self) -> str:
        """One line per infeasible filesystem, naming need and availability."""
        lines = []
        for item in self.shortfalls:
            lines.append(
                f"not enough free space at {item.directory}: "
                f"{item.targets} target(s) need {human_size(item.needed)}, "
                f"{human_size(item.free)} free"
            )
        return "; ".join(lines)


def batch_budget(
    entries: Sequence[tuple[str, int]],
    *,
    reserve: int = DISK_RESERVE,
) -> BatchBudget:
    """Pre-flight a batch of ``(path, needed_bytes)`` before writing anything.

    Requirements are grouped per device, because two destinations on different
    mounts do not compete for space, and two targets that resolve to the *same*
    file are billed once (the largest requirement wins) because only one copy is
    ever written. A group whose total requirement (plus the reserve) exceeds the
    free space cannot finish however it is scheduled — every byte written stays
    on the disk — so each target in it is reported as not fitting instead of
    failing halfway through with ``ENOSPC``.

    Concurrency is deliberately *not* part of this decision: downloading fewer
    files at a time does not make a batch fit, it only delays the same failure.
    """
    needs = [max(0, int(need)) for _path, need in entries]
    if not entries or all(need <= 0 for need in needs):
        return BatchBudget(tuple(True for _ in entries))

    # One entry per distinct file: a mirror and its origin (or `--retry-failed`
    # alongside the original target) resolve to the same path, and writing it
    # twice does not cost twice the disk.
    per_file: dict[str, int] = {}
    for (path, _need), need in zip(entries, needs, strict=False):
        key = os.path.abspath(path)
        per_file[key] = max(per_file.get(key, 0), need)

    groups: dict[str, list[str]] = {}
    for path in per_file:
        groups.setdefault(_device_key(path), []).append(path)

    fits = [True] * len(entries)
    shortfalls: list[Shortfall] = []
    rejected: set[str] = set()
    for device, paths in groups.items():
        free = free_space(paths[0])
        if free is None:
            continue
        group_need = sum(per_file[path] for path in paths)
        if group_need + reserve <= free:
            continue
        rejected.add(device)
        shortfalls.append(
            Shortfall(
                directory=os.path.dirname(paths[0]) or ".",
                needed=group_need,
                free=free,
                targets=len(paths),
            )
        )
    if rejected:
        for index, (path, _need) in enumerate(entries):
            if _device_key(os.path.abspath(path)) in rejected:
                fits[index] = False
    return BatchBudget(tuple(fits), tuple(shortfalls))
