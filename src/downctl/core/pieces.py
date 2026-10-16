"""Resumable piece bitmap — the authoritative ledger for partial downloads.

Multi-connection downloads complete pieces out of order. Judging resume state
by file size alone silently corrupts files (right size, holes in the middle).
The bitmap records exactly which pieces are complete so a retry re-fetches
only the missing ones.

Implementation lands in P2. Public surface (stable):

- :func:`load` / :func:`save` / :func:`remove`
- :func:`resolve` — decide which pieces still need downloading
- :func:`layout` — compute piece size / count / connection count
"""

from __future__ import annotations

from dataclasses import dataclass

BITMAP_SUFFIX = ".downctl-pieces"


@dataclass
class Bitmap:
    """Piece completion state for one file."""

    size: int
    pieces: int
    bits: list[int]
    min_size: int = 0

    @property
    def complete(self) -> bool:
        return all(self.bits)


def path_for(target: str) -> str:
    """Return the bitmap sidecar path for ``target``."""
    return target + BITMAP_SUFFIX


def layout(expected: int, connections: int) -> tuple[int, int, int]:
    """Return ``(piece_size, piece_count, connection_count)`` for a file size."""
    raise NotImplementedError("pieces.layout lands in P2")


def load(target: str) -> Bitmap | None:
    """Load the bitmap for ``target`` or ``None`` if absent/invalid."""
    raise NotImplementedError("pieces.load lands in P2")


def save(target: str, bitmap: Bitmap) -> None:
    """Atomically persist ``bitmap`` (fsync data first)."""
    raise NotImplementedError("pieces.save lands in P2")


def remove(target: str) -> None:
    """Delete the bitmap sidecar (and any temp file)."""
    raise NotImplementedError("pieces.remove lands in P2")


def resolve(target: str, expected: int, pieces: int) -> list[int]:
    """Return the ``bits`` to use for this attempt (1 = already complete)."""
    raise NotImplementedError("pieces.resolve lands in P2")
