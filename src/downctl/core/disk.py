"""Disk-space pre-flight shared by the engine and the streaming fallback.

Kept in one place so the reserve policy is consistent no matter which code
path decides whether to start (or continue) a download.
"""

from __future__ import annotations

import os
import shutil

from downctl.core.errors import DiskSpaceError

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
