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
    #: Piece size in bytes (0 = unknown, e.g. a bitmap written by an older
    #: version). Recorded since v3 so a change in the connection count can
    #: reslice the bitmap instead of discarding progress.
    piece_size: int = 0
    version: int = 3
    _extra: dict[str, object] = field(default_factory=dict, repr=False)

    @property
    def complete(self) -> bool:
        return all(self.bits)

    @property
    def done_pieces(self) -> int:
        return sum(self.bits)

    def done_bytes(self) -> int:
        """Bytes proven complete by this bitmap (capped at ``size``)."""
        if self.piece_size > 0:
            return min(self.done_pieces * self.piece_size, self.size)
        if self.pieces <= 0:
            return 0
        approx = (self.size + self.pieces - 1) // self.pieces
        return min(self.done_pieces * approx, self.size)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "size": self.size,
            "pieces": self.pieces,
            "bits": self.bits,
            "min_size": self.min_size,
            "piece_size": self.piece_size,
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
            and len(bits) == pieces
        ):
            return None
        min_size = data.get("min_size", 0)
        if not isinstance(min_size, int) or min_size < 0:
            min_size = 0
        piece_size = data.get("piece_size", 0)
        if not isinstance(piece_size, int) or piece_size < 0:
            piece_size = 0
        version = data.get("version", 2)
        if not isinstance(version, int):
            version = 2
        return cls(
            size=size,
            pieces=pieces,
            bits=list(bits),
            min_size=min_size,
            piece_size=piece_size,
            version=version,
        )


def path_for(target: str) -> str:
    """Return the bitmap sidecar path for ``target``."""
    return target + BITMAP_SUFFIX


def layout(expected: int, connections: int, piece_size: int = 0) -> tuple[int, int, int]:
    """Return ``(piece_size, piece_count, connection_count)`` for a file size.

    Piece size is capped at :data:`DEFAULT_PIECE_SIZE` (and shrinks for small
    files) so an interrupted transfer loses at most ``connections`` pieces of
    work, rather than a multi-gigabyte slice. An explicit ``piece_size``
    (bytes) overrides the automatic choice.
    """
    if expected <= 0:
        return 0, 0, max(1, connections)
    conn_n = max(1, min(connections, expected // (1 << 20) + 1))
    if piece_size and piece_size > 0:
        size = max(MIN_PIECE_SIZE, int(piece_size))
    else:
        size = min(
            DEFAULT_PIECE_SIZE,
            max(MIN_PIECE_SIZE, (expected + conn_n - 1) // conn_n),
        )
    pieces = (expected + size - 1) // size
    return size, pieces, conn_n


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
        _fsync_dir(os.path.dirname(path_for(target)) or ".")
        return True
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return False


def _fsync_dir(directory: str) -> None:
    """Best-effort fsync of a directory so a rename survives a crash."""
    if os.name == "nt":  # directory fsync is not supported on Windows
        return
    with contextlib.suppress(OSError), open(directory, "rb") as handle:
        os.fsync(handle.fileno())


def remove(target: str) -> None:
    """Delete the bitmap sidecar (and any temp file)."""
    for path in (path_for(target), path_for(target) + ".tmp"):
        with contextlib.suppress(OSError):
            os.remove(path)


def resolve(target: str, expected: int, pieces: int, piece_size: int = 0) -> list[int]:
    """Return the ``bits`` to use for this attempt (``1`` = already complete).

    When the piece grid changed (e.g. the user altered ``--connections`` or
    ``--piece-size``) but the bitmap knows its old piece size, the completion
    state is *resliced* by byte range instead of discarded, so changing the
    connection count no longer throws away progress.

    On any state that cannot be trusted, the target is truncated to zero and a
    fresh all-zero bitmap is returned (never extend a partial file into a
    full-size sparse file — that would be mistaken for success).
    """
    base = load(target)
    current = os.path.getsize(target) if os.path.isfile(target) else 0

    if base is not None and base.size == expected:
        same_grid = base.pieces == pieces and (
            piece_size == 0 or base.piece_size in (0, piece_size)
        )
        if same_grid:
            if _size_ok(base.min_size, current, expected):
                return list(base.bits)
            # Some filesystems (WSL DrvFS/9p) lag on stat metadata; re-check once.
            current = os.path.getsize(target) if os.path.isfile(target) else 0
            if _size_ok(base.min_size, current, expected):
                return list(base.bits)
            return _reset(target, pieces)
        if base.piece_size > 0 and piece_size > 0 and base.min_size <= current <= expected:
            return _remap(base.bits, base.piece_size, pieces, piece_size, expected)
        return _reset(target, pieces)

    if current >= expected and trusted_complete(target, current):
        return [1] * pieces
    return _reset(target, pieces)


def _size_ok(min_size: int, current: int, expected: int) -> bool:
    return min_size <= current <= expected


def _remap(
    old_bits: list[int],
    old_piece_size: int,
    new_pieces: int,
    new_piece_size: int,
    size: int,
) -> list[int]:
    """Reslice completion bits onto a new piece grid.

    A new piece is complete iff every old piece overlapping its byte range is
    complete (old complete pieces are fsync-backed, so the bytes exist).
    """
    new_bits = [0] * new_pieces
    old_count = len(old_bits)
    for index in range(new_pieces):
        start = index * new_piece_size
        end = min(start + new_piece_size, size)
        if end <= start:
            continue
        first = start // old_piece_size
        last = (end - 1) // old_piece_size
        if first >= old_count or last >= old_count:
            continue
        if all(old_bits[o] for o in range(first, last + 1)):
            new_bits[index] = 1
    return new_bits


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
