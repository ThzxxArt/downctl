"""Resumable piece bitmap — the authoritative ledger for partial downloads.

Multi-connection downloads complete pieces out of order. Judging resume state
by file size alone silently corrupts files (right size, holes in the middle).
The bitmap records exactly which pieces are complete so a retry re-fetches
only the missing ones.

Invariants
----------
- A ``1`` bit means the piece is fully written *and* the data file has been
  ``fsync``-ed, so a crash cannot leave the ledger claiming more than reality.
- A bitmap belongs to exactly one remote object: it stores a SHA-256 of the
  canonical URL (``url_sha256``). A bitmap written for another URL, or one
  written before identities existed (v3 and older), is never trusted.
- The bitmap is authoritative: any distribution of ``1`` bits is trusted
  (out-of-order completion is normal). Only an external truncation below
  ``min_size`` or growth beyond ``expected`` invalidates it.
- Without a bitmap, a full-size file is only trusted when there is a
  completion marker for the same URL/size/mtime (see :func:`completion_valid`)
  or it provably has no holes (see :func:`trusted_complete`); otherwise it is
  re-downloaded to avoid silent corruption.

Completion markers
------------------
When a file finishes (and, when requested, verifies), the engine writes a
``<file>.downctl-done`` JSON sidecar binding ``size``, ``mtime_ns``, the URL
identity and (when one was verified) the checksum algorithm/digest. This lets a later run skip a completed file on filesystems where
hole detection is impossible or too weak (WSL DrvFS, NTFS). The marker is not
a checksum: an external tool that rewrites the file and restores the exact
size *and* mtime can still fool it. Use ``--checksums`` for cryptographic
proof.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import tempfile
import threading
from dataclasses import dataclass, field

BITMAP_SUFFIX = ".downctl-pieces"
COMPLETION_SUFFIX = ".downctl-done"
DEFAULT_PIECE_SIZE = 8 * (1 << 20)  # 8 MiB
MIN_PIECE_SIZE = 1024
#: Refuse grids that would make the JSON ledger absurdly large (100 GiB with
#: 1 KiB pieces would be ~100 M entries / hundreds of MB on disk).
MAX_PIECES = 2_000_000
BITMAP_VERSION = 4
COMPLETION_VERSION = 2

#: Set to ``1`` to trust a local file as a complete contiguous prefix even when
#: hole detection is unavailable (e.g. a file fetched by another tool).
TRUST_PARTIAL_ENV = "DOWNCTL_TRUST_PARTIAL"

#: Serializes sidecar writes (checkpoint thread vs. the transfer's final save).
_SAVE_LOCK = threading.Lock()


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
    version: int = BITMAP_VERSION
    #: SHA-256 of the canonical source URL this bitmap belongs to (since v4).
    url_sha256: str = ""
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
            "url_sha256": self.url_sha256,
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
        if pieces > MAX_PIECES or min_size > size:
            return None
        if piece_size > 0 and (size + piece_size - 1) // piece_size != pieces:
            # A ledger whose grid cannot cover the object is corrupt/foreign.
            return None
        version = data.get("version", 2)
        if not isinstance(version, int) or version < 1:
            version = 2
        if version > BITMAP_VERSION:
            # A newer format may use different invariants; never guess.
            return None
        url_sha256 = data.get("url_sha256", "")
        if not isinstance(url_sha256, str):
            url_sha256 = ""
        return cls(
            size=size,
            pieces=pieces,
            bits=list(bits),
            min_size=min_size,
            piece_size=piece_size,
            version=version,
            url_sha256=url_sha256,
        )


def path_for(target: str) -> str:
    """Return the bitmap sidecar path for ``target``."""
    return target + BITMAP_SUFFIX


def completion_path(target: str) -> str:
    """Return the completion-marker path for ``target``."""
    return target + COMPLETION_SUFFIX


def has_state(target: str) -> bool:
    """Whether any resume/completion sidecar exists (valid or corrupt)."""
    return os.path.isfile(path_for(target)) or os.path.isfile(completion_path(target))


def layout(expected: int, connections: int, piece_size: int = 0) -> tuple[int, int, int]:
    """Return ``(piece_size, piece_count, connection_count)`` for a file size.

    Piece size is capped at :data:`DEFAULT_PIECE_SIZE` (and shrinks for small
    files) so an interrupted transfer loses at most ``connections`` pieces of
    work, rather than a multi-gigabyte slice. An explicit ``piece_size``
    (bytes) overrides the automatic choice.

    Raises :class:`ValueError` when an explicit piece size would produce more
    than :data:`MAX_PIECES` entries, because such a ledger is unusable in
    practice (and would be rewritten on every checkpoint).
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
    if pieces > MAX_PIECES:
        raise ValueError(
            f"piece size {size} would create {pieces} pieces for {expected} bytes "
            f"(maximum {MAX_PIECES}); use a larger --piece-size"
        )
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
    return _atomic_write_json(path_for(target), bitmap.to_dict())


def remove(target: str) -> None:
    """Delete all persisted state for ``target`` (bitmap + completion marker)."""
    for path in (
        path_for(target),
        path_for(target) + ".tmp",
        completion_path(target),
        completion_path(target) + ".tmp",
    ):
        with contextlib.suppress(OSError):
            os.remove(path)


def _fsync_dir(directory: str) -> None:
    """Best-effort fsync of a directory so a rename survives a crash."""
    if os.name == "nt":  # directory fsync is not supported on Windows
        return
    with contextlib.suppress(OSError), open(directory, "rb") as handle:
        os.fsync(handle.fileno())


def _atomic_write_json(path: str, payload: dict[str, object]) -> bool:
    # Checkpoints run in a worker thread while the transfer's ``finally`` may
    # save synchronously; serializing writes prevents two coroutines/threads
    # from clobbering the same temp path.
    directory = os.path.dirname(path) or "."
    with _SAVE_LOCK:
        try:
            tmp_file = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed below
                "w",
                encoding="utf-8",
                dir=directory,
                prefix=os.path.basename(path) + ".",
                suffix=".tmp",
                delete=False,
            )
        except OSError:
            return False
        tmp = tmp_file.name
        try:
            with tmp_file:
                json.dump(payload, tmp_file)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp, path)
            _fsync_dir(directory)
            return True
        except (OSError, TypeError, ValueError):
            with contextlib.suppress(OSError):
                tmp_file.close()
            with contextlib.suppress(OSError):
                os.remove(tmp)
            return False


# --------------------------------------------------------------------------- #
# completion markers
# --------------------------------------------------------------------------- #
def save_completion(
    target: str,
    identity: str,
    checksum_algo: str = "",
    checksum_hex: str = "",
) -> bool:
    """Record that ``target`` is complete for the object ``identity`` names.

    When a checksum was verified, its algorithm/digest are recorded too so a
    later skip can detect that a mutable source now advertises a different
    digest.
    """
    try:
        stat = os.stat(target)
    except OSError:
        return False
    return _atomic_write_json(
        completion_path(target),
        {
            "version": COMPLETION_VERSION,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "url_sha256": identity,
            "checksum_algo": (checksum_algo or "").lower(),
            "checksum_hex": (checksum_hex or "").lower(),
        },
    )


def load_completion(target: str) -> dict[str, object] | None:
    """Load a completion marker, or ``None`` when missing/malformed."""
    try:
        with open(completion_path(target), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    size = data.get("size")
    mtime_ns = data.get("mtime_ns")
    identity = data.get("url_sha256", "")
    checksum_algo = data.get("checksum_algo", "")
    checksum_hex = data.get("checksum_hex", "")
    if (
        not isinstance(version, int)
        or version < 1
        or version > COMPLETION_VERSION
        or not isinstance(size, int)
        or size < 0
        or not isinstance(mtime_ns, int)
        or not isinstance(identity, str)
        or not isinstance(checksum_algo, str)
        or not isinstance(checksum_hex, str)
    ):
        return None
    return data


def completion_valid(target: str, expected: int = 0, identity: str | None = None) -> bool:
    """Whether ``target`` has a marker proving it is complete.

    ``expected > 0`` additionally requires the recorded and current sizes to
    match it. ``identity`` is required: a marker without a URL identity (or an
    empty one) can never prove which object it belongs to.
    """
    record = load_completion(target)
    if record is None:
        return False
    try:
        stat = os.stat(target)
    except OSError:
        return False
    if record["size"] != stat.st_size or record["mtime_ns"] != stat.st_mtime_ns:
        return False
    if expected > 0 and stat.st_size != expected:
        return False
    if not identity:
        # A marker without a URL identity cannot prove which object it
        # belongs to; never trust it.
        return False
    return record.get("url_sha256", "") == identity


def bitmap_trusted(
    bitmap: Bitmap, current: int, expected: int, identity: str | None = None
) -> bool:
    """Whether ``bitmap`` describes a data file of ``current`` bytes.

    This is the single trust predicate for resumable state: the grid must be
    internally consistent, the file must still cover every ``1`` bit (the
    high-water mark), it must not have shrunk below the last durable snapshot
    (``min_size``), and a complete bitmap requires the exact final size.
    ``identity`` (when supplied) must match the recorded URL identity.
    """
    if expected <= 0 or bitmap.size != expected:
        return False
    if bitmap.version < BITMAP_VERSION or not bitmap.url_sha256:
        return False
    if identity is not None and bitmap.url_sha256 != identity:
        return False
    if current <= 0 or current > expected:
        return False
    if bitmap.pieces <= 0 or bitmap.pieces > MAX_PIECES:
        return False
    if len(bitmap.bits) != bitmap.pieces:
        return False
    if bitmap.piece_size <= 0:
        return False
    if (expected + bitmap.piece_size - 1) // bitmap.piece_size != bitmap.pieces:
        return False
    if bitmap.min_size < 0 or bitmap.min_size > expected:
        return False
    if current < bitmap.min_size:
        return False
    high_water = 0
    for index, bit in enumerate(bitmap.bits):
        if bit:
            high_water = max(high_water, min((index + 1) * bitmap.piece_size, expected))
    if current < high_water:
        return False
    return not bitmap.complete or current == expected


# --------------------------------------------------------------------------- #
# resume resolution
# --------------------------------------------------------------------------- #
def resolve(
    target: str,
    expected: int,
    pieces: int,
    piece_size: int = 0,
    identity: str | None = None,
) -> list[int]:
    """Return the ``bits`` to use for this attempt (``1`` = already complete).

    When the piece grid changed (e.g. the user altered ``--connections`` or
    ``--piece-size``) but the bitmap knows its old piece size, the completion
    state is *resliced* by byte range instead of discarded, so changing the
    connection count no longer throws away progress.

    ``identity`` binds the ledger to a remote object. Any bitmap whose recorded
    identity differs (including a pre-v4 bitmap that has no identity at all) is
    discarded, because it cannot prove the bytes belong to this URL. An empty
    or missing identity never trusts existing state at all (production callers
    always pass one).

    On any state that cannot be trusted, the target is truncated to zero and a
    fresh all-zero bitmap is returned (never extend a partial file into a
    full-size sparse file — that would be mistaken for success).
    """
    if not identity:
        return _reset(target, pieces)
    base = load(target)
    current = os.path.getsize(target) if os.path.isfile(target) else 0

    if base is not None and base.size == expected:
        if base.version < BITMAP_VERSION or base.url_sha256 != identity:
            return _reset(target, pieces)
        same_grid = base.pieces == pieces and (
            piece_size == 0 or base.piece_size in (0, piece_size)
        )
        if same_grid:
            if bitmap_trusted(base, current, expected, identity):
                return list(base.bits)
            # Some filesystems (WSL DrvFS/9p) lag on stat metadata; re-check once.
            current = os.path.getsize(target) if os.path.isfile(target) else 0
            if bitmap_trusted(base, current, expected, identity):
                return list(base.bits)
            return _reset(target, pieces)
        if (
            base.piece_size > 0
            and piece_size > 0
            and bitmap_trusted(base, current, expected, identity)
        ):
            return _remap(base.bits, base.piece_size, pieces, piece_size, expected)
        return _reset(target, pieces)

    if current >= expected and completion_valid(target, expected, identity):
        return [1] * pieces
    if base is not None or has_state(target):
        # A stale bitmap for another size, a malformed ledger or a marker for
        # another URL/file is positive evidence not to trust this data file
        # with the weaker hole heuristic. Reset and re-fetch.
        return _reset(target, pieces)
    # A file larger than the expected object is not a completed copy of it;
    # reset instead of truncating an unproven prefix and calling it success.
    if current == expected and trusted_complete(target, current):
        return [1] * pieces
    return _reset(target, pieces)


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
    """Whether a full-size, unmarked file can be trusted as complete.

    - ``DOWNCTL_TRUST_PARTIAL=1``: the user vouches for it.
    - WSL ``/mnt`` (DrvFS/9p): holes are materialized, undetectable — distrust.
    - Native Windows: no ``SEEK_HOLE`` and the sparse flag is not enough to
      prove that seek-written ranges are present — distrust.
    - Other POSIX: precise ``SEEK_HOLE``/``SEEK_DATA`` scan.

    Files downloaded by downctl do not need this heuristic: they carry a
    completion marker (:func:`completion_valid`).
    """
    if size <= 0:
        return False
    if os.environ.get(TRUST_PARTIAL_ENV) == "1":
        return True
    if _on_drvfs(path):
        return False
    if os.name == "nt":
        return False
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
