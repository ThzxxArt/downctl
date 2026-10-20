"""Streaming file digests.

The same five-line loop — open, read in chunks, feed a hasher, hexdigest — used
to exist in five places (both verifiers, both evidence hooks, and the ``verify``
command). They had already drifted: different chunk sizes, and only one of them
turned an unknown algorithm into a clean ``""`` instead of a traceback. One
implementation means a digest computed anywhere is computed the same way.

The file is read in one pass: a multi-gigabyte model must never be loaded into
memory to be hashed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

#: Read size. Large enough that the syscall overhead is irrelevant, small enough
#: that hashing does not compete with a download for page cache.
READ_SIZE = 1 << 20


def supported(algo: str) -> bool:
    """Whether :mod:`hashlib` knows ``algo``."""
    try:
        hashlib.new(algo)
    except (ValueError, TypeError):
        return False
    return True


def hash_file(
    path: str,
    algo: str = "sha256",
    *,
    progress: Callable[[int], None] | None = None,
) -> str:
    """Return the hex digest of ``path`` with ``algo``.

    Raises :class:`ValueError` for an algorithm :mod:`hashlib` does not know and
    :class:`OSError` when the file cannot be read; callers decide whether that
    is a warning or a failure. ``progress`` receives the size of each chunk, so a
    verifier can report without a second pass over the file.
    """
    hasher = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(READ_SIZE), b""):
            hasher.update(chunk)
            if progress is not None:
                progress(len(chunk))
    return hasher.hexdigest()
