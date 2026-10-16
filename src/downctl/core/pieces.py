"""Resumable piece bitmap — the authoritative ledger for partial downloads.

Multi-connection downloads complete pieces out of order. Judging resume state
by file size alone silently corrupts files (right size, holes in the middle).
The bitmap records exactly which pieces are complete so a retry re-fetches
only the missing ones.

Invariants
----------
- A ``1`` bit means the piece is fully written *and* the data file has been
  ``fsync``-ed, so a crash cannot leave the ledger claiming more than reality.
- The bitmap is authoritative: any distribution of ``1`` bits is trusted
  (out-of-order completion is normal). Only an external truncation below
  ``min_size`` or growth beyond ``expected`` invalidates it.
- Without a bitmap, a full-size file is only trusted when it provably has no
  holes (see :func:`trusted_complete`); otherwise it is re-downloaded to avoid
  silent corruption.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
from dataclasses import dataclass, field

BITMAP_SUFFIX = ".downctl-pieces"
DEFAULT_PIECE_SIZE = 8 * (1 << 20)  # 8 MiB
MIN_PIECE_SIZE = 1024

#: Set to ``1`` to trust a local file as a complete contiguous prefix even when
#: hole detection is unavailable (e.g. a file fetched by another tool).
TRUST_PARTIAL_ENV = "DOWNCTL_TRUST_PARTIAL"


@dataclass
class Bitmap:
    """Piece completion state for one file."""

    size: int
    pieces: int
    bits: list[int]
    min_size: int = 0
    version: int = 2
    _extra: dict[str, object] = field(default_factory=dict, repr=False)

    @property
    def complete(self) -> bool:
        return all(self.bits)

    @property
    def done_pieces(self) -> int:
        return sum(self.bits)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "size": self.size,
            "pieces": self.pieces,
            "bits": self.bits,
            "min_size": self.min_size,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Bitmap | None:
        bits = data.get("bits")
        size = data.get("size")
        pieces = data.get("pieces")
        if not (
            isinstance(bits, list)
            and bits
            and all(isinstance(b, int) and b in (0, 1) for b in bits)
            and isinstance(size, int)
            and size > 0
            and isinstance(pieces, int)
            and pieces > 0
        ):
            return None
        min_size = data.get("min_size", 0)
        if not isinstance(min_size, int) or min_size < 0:
            min_size = 0
        version = data.get("version", 2)
        if not isinstance(version, int):
            version = 2
        return cls(size=size, pieces=pieces, bits=list(bits), min_size=min_size, version=version)


def path_for(target: str) -> str:
    """Return the bitmap sidecar path for ``target``."""
    return target + BITMAP_SUFFIX


def layout(expected: int, connections: int) -> tuple[int, int, int]:
    """Return ``(piece_size, piece_count, connection_count)`` for a file size.

    Piece size is capped at :data:`DEFAULT_PIECE_SIZE` (and shrinks for small
    files) so an interrupted transfer loses at most ``connections`` pieces of
    work, rather than a multi-gigabyte slice.
    """
    if expected <= 0:
        return 0, 0, max(1, connections)
    conn_n = max(1, min(connections, expected // (1 << 20) + 1))
    piece_size = min(
        DEFAULT_PIECE_SIZE,
        max(MIN_PIECE_SIZE, (expected + conn_n - 1) // conn_n),
    )
    pieces = (expected + piece_size - 1) // piece_size
    return piece_size, pieces, conn_n


def load(target: str) -> Bitmap | None:
    """Load the bitmap for ``target`` or ``None`` when absent/invalid."""
    try:
        with open(path_for(target), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return Bitmap.from_dict(data)


def save(target: str, bitmap: Bitmap) -> bool:
    """Atomically persist ``bitmap`` (fsync file + atomic replace).

    Returns ``False`` if the write failed (the caller keeps downloading; only
    resume ability is lost).
    """
    tmp = path_for(target) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(bitmap.to_dict(), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path_for(target))
        return True
    except OSError:
        return False


def remove(target: str) -> None:
    """Delete the bitmap sidecar (and any temp file)."""
    for path in (path_for(target), path_for(target) + ".tmp"):
        with contextlib.suppress(OSError):
            os.remove(path)


def resolve(target: str, expected: int, pieces: int) -> list[int]:
    """Return the ``bits`` to use for this attempt (``1`` = already complete).

    On any state that cannot be trusted, the target is truncated to zero and a
    fresh all-zero bitmap is returned (never extend a partial file into a
    full-size sparse file — that would be mistaken for success).
    """
    base = load(target)
    current = os.path.getsize(target) if os.path.isfile(target) else 0

    if base is not None and base.size == expected and base.pieces == pieces:
        if base.min_size <= current <= expected:
            return list(base.bits)
        # Some filesystems (WSL DrvFS/9p) lag on stat metadata; re-check once.
        current = os.path.getsize(target) if os.path.isfile(target) else 0
        if base.min_size <= current <= expected:
            return list(base.bits)
        return _reset(target, pieces)

    if current >= expected and trusted_complete(target, current):
        return [1] * pieces
    return _reset(target, pieces)


def _reset(target: str, pieces: int) -> list[int]:
    if os.path.isfile(target):
        with contextlib.suppress(OSError), open(target, "r+b") as fh:
            fh.truncate(0)
    remove(target)
    return [0] * pieces


def trusted_complete(path: str, size: int) -> bool:
    """Whether a full-size, bitmap-less file can be trusted as complete.

    - ``DOWNCTL_TRUST_PARTIAL=1``: the user vouches for it.
    - WSL ``/mnt`` (DrvFS/9p): holes are materialized, undetectable — distrust.
    - Native Windows: no ``SEEK_HOLE``; fall back to the NTFS sparse flag.
    - Other POSIX: precise ``SEEK_HOLE``/``SEEK_DATA`` scan.
    """
    if size <= 0:
        return False
    if os.environ.get(TRUST_PARTIAL_ENV) == "1":
        return True
    if _on_drvfs(path):
        return False
    if os.name == "nt":
        return not _win_sparse_flag(path)
    return not _has_holes(path, size)


def _on_drvfs(path: str) -> bool:
    if platform.system() != "Linux":
        return False
    try:
        with open("/proc/version", encoding="utf-8", errors="ignore") as fh:
            if "microsoft" not in fh.read().lower():
                return False
    except OSError:
        return False
    return os.path.abspath(path).startswith("/mnt/")


def _win_sparse_flag(path: str) -> bool:
    try:
        import ctypes

        attrs = ctypes.windll.kernel32.GetFileAttributesW(os.path.abspath(path))  # type: ignore[attr-defined]
        if attrs == 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES
            return True
        return bool(attrs & 0x200)  # FILE_ATTRIBUTE_SPARSE_FILE
    except Exception:  # pragma: no cover - non-Windows
        return True


def _has_holes(path: str, size: int) -> bool:
    if not hasattr(os, "SEEK_DATA"):
        return True
    try:
        with open(path, "rb") as fh:
            fd = fh.fileno()
            pos = 0
            while True:
                try:
                    data = os.lseek(fd, pos, os.SEEK_DATA)
                except OSError:
                    return pos < size
                if data > pos:
                    return True
                try:
                    hole = os.lseek(fd, data, os.SEEK_HOLE)
                except OSError:
                    return True
                if hole >= size:
                    return False
                pos = hole
    except OSError:
        return True
