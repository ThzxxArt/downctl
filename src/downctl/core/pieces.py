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
- A bitmap also belongs to one *version* of that object: it stores the version
  witness observed when it was built (``validator``, since 0.1.4). If the server
  now names a different version, the ledger is discarded and the object is
  fetched again from zero. Resuming pieces of one version into a file that
  already holds pieces of another produces a spliced file that no size check
  can detect.
- The bitmap is authoritative: any distribution of ``1`` bits is trusted
  (out-of-order completion is normal). Only an external truncation below
  ``min_size`` or growth beyond ``expected`` invalidates it.
- Without a bitmap, a full-size file is only trusted when there is a
  completion marker for the same URL/size/mtime (see :func:`completion_valid`)
  or it provably has no holes (see :func:`trusted_complete`); otherwise it is
  re-downloaded to avoid silent corruption.
- State that cannot be trusted is *abandoned*, not destroyed: the marker is
  removed and an all-zero ledger is pinned in its place, so a failed
  replacement never costs the user a complete file they already had
  (:func:`abandon`).

Completion markers
------------------
When a file finishes (and, when requested, verifies), the engine writes a
``<file>.downctl-done`` JSON sidecar binding ``size``, ``mtime_ns``, the URL
identity, the version witness and (when one was verified) the checksum
algorithm/digest. This lets a later run skip a completed file on filesystems
where hole detection is impossible or too weak (WSL DrvFS, NTFS). The marker is
not a checksum: an external tool that rewrites the file and restores the exact
size *and* mtime can still fool it. Use ``--checksums`` for cryptographic
proof.

Witness states
--------------
Every marker carries exactly one of three witness states, reported as JSON so
a skip is never silently presented as proof (:func:`witness_of`):

``validator``
    The marker recorded the server's version witness and this run observed the
    same one. Strongest cheap proof: the object has not been replaced.
``checksum``
    No usable witness was recorded, but a verified digest is stored. Content
    proof, but only as strong as the digest the source advertised.
``none``
    Unversioned: nothing about the object version could be checked. A skip is
    still allowed (a completed file is never a *spliced* file), but it may be
    *stale*, and ``require_witness`` refuses to trust it at all.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field

from downctl import console
from downctl.core.errors import DownctlError, FutureStateError

BITMAP_SUFFIX = ".downctl-pieces"
COMPLETION_SUFFIX = ".downctl-done"
DEFAULT_PIECE_SIZE = 8 * (1 << 20)  # 8 MiB
MIN_PIECE_SIZE = 1024
#: Refuse grids that would make the JSON ledger absurdly large (100 GiB with
#: 1 KiB pieces would be ~100 M entries / hundreds of MB on disk).
MAX_PIECES = 2_000_000
#: Format versions are deliberately *not* bumped for the 0.1.4 ``validator``
#: field: it is additive, and an older reader must keep being able to read
#: these files. 0.1.3 refuses a marker with a higher version and then treats the
#: file as unproven state, which makes it truncate a *completed* download — a
#: version bump would therefore turn "downgrade" into data loss. The field's
#: presence is what 0.1.4 compares; an older build simply ignores it.
BITMAP_VERSION = 4
COMPLETION_VERSION = 2

#: State files this build has already warned about (a newer downctl wrote them).
#: ``load`` runs once per checkpoint, so the warning must not repeat.
_REPORTED_FUTURE_STATE: set[str] = set()


def _declared_version(path: str) -> tuple[bool, int | None]:
    """Whether ``path`` is a state file, and the integer version it declares.

    The first element separates "not a state file" (absent, unreadable, not a
    JSON object, no ``version`` key) from "a state file whose version field this
    build cannot interpret". The second is ``None`` in the latter case — a
    string version, for example. Treating that as "no state here" was the same
    mistake as treating a newer integer version that way: the file is present
    and its owner is unknown, so it must not be overwritten.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False, None
    if not isinstance(data, dict) or "version" not in data:
        return False, None
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return True, None
    return True, version


def written_by_a_newer_downctl(path: str, current: int) -> bool:
    """Whether ``path`` is a state file this build does not know how to read.

    That is two cases, and both mean the same thing to a caller: the version is
    *higher* than this build writes, or there is a version field whose type this
    build does not understand. Such a file is *present*, not missing, and the
    difference matters: treating it as absent is how a downgrade turns into a
    silent overwrite of state a newer build is still using. Callers may ignore
    it; nobody may destroy it.
    """
    parsed, version = _declared_version(path)
    if not parsed:
        return False
    if version is None:
        return True
    return version > current


def _version_is_unreadable(version: object, current: int) -> bool:
    """Whether a *present* ``version`` field describes a format we cannot read.

    Used by the loaders, which already have the document in hand: reading the
    file a second time just to answer this question is what turned every
    checkpoint into two full parses of the ledger.
    """
    if isinstance(version, bool) or not isinstance(version, int):
        return True
    return version > current


#: The format version each sidecar suffix is written at. A sidecar's suffix is
#: what tells a deletion path which version applies, so the guard does not need
#: a second parameter that a caller could pass incorrectly.
_STATE_VERSIONS = {
    BITMAP_SUFFIX: BITMAP_VERSION,
    COMPLETION_SUFFIX: COMPLETION_VERSION,
}


def _stated_version(path: str) -> int | None:
    """The format version that governs ``path``, or ``None`` for a non-sidecar.

    A ``.tmp`` staging file is not a sidecar: it never carries a readable
    version and is always safe to delete.
    """
    for suffix, version in _STATE_VERSIONS.items():
        if path.endswith(suffix):
            return version
    return None


def _is_from_a_newer_build(path: str) -> bool:
    version = _stated_version(path)
    return version is not None and written_by_a_newer_downctl(path, version)


def future_state(target: str) -> str | None:
    """The path of a sidecar written by a newer downctl, or ``None``.

    This is the one question every destructive path has to ask first. Being
    unable to *read* a ledger is not the same as there being no ledger: the
    file is the only copy of what a newer build knew, so it outranks this
    run's wish to reset the target.
    """
    for path in (path_for(target), completion_path(target)):
        if _is_from_a_newer_build(path):
            return path
    return None


def unlink_state(path: str, *, force: bool = False) -> None:
    """Delete one state file, refusing anything a newer build wrote.

    The single choke point for every deletion of persisted state. Putting the
    guard here rather than in each caller is deliberate: 0.1.5 guarded
    ``save`` and left ``remove``/``discard`` unguarded, so the *failure* of
    the guard became the thing that destroyed the file.
    """
    suffix = _suffix_of(path)
    if not force and suffix and _is_from_a_newer_build(path):
        raise FutureStateError(path[: -len(suffix)], path)
    with contextlib.suppress(OSError):
        os.remove(path)


def _suffix_of(path: str) -> str:
    for suffix in _STATE_VERSIONS:
        if path.endswith(suffix):
            return suffix
    return ""


def _report_future_state(path: str) -> None:
    if path in _REPORTED_FUTURE_STATE:
        return
    _REPORTED_FUTURE_STATE.add(path)
    console.warn(
        f"{path} was written by a newer downctl; it is left untouched and not used. "
        "Upgrade to resume this file, or delete it to start over."
    )


#: Set to ``1`` to trust a local file as a complete contiguous prefix even when
#: hole detection is unavailable (e.g. a file fetched by another tool).
TRUST_PARTIAL_ENV = "DOWNCTL_TRUST_PARTIAL"

#: How a :class:`Resume` decision was reached. The caller needs this because
#: only some of these prove *which revision* the bytes on disk came from.
PROOF_FRESH = "fresh"
PROOF_LEDGER = "ledger"
PROOF_MARKER = "marker"
PROOF_HOLES = "holes"

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
    #: Version witness of the remote object this ledger was built from (added
    #: in 0.1.4, no format bump: the field is additive). Empty means the server
    #: offered no witness ("unversioned").
    validator: str = ""
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
            "validator": self.validator,
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
        validator = data.get("validator", "")
        if not isinstance(validator, str):
            validator = ""
        return cls(
            size=size,
            pieces=pieces,
            bits=list(bits),
            min_size=min_size,
            piece_size=piece_size,
            version=version,
            url_sha256=url_sha256,
            validator=validator,
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
    """Load the bitmap for ``target`` or ``None`` when absent/invalid.

    A ledger written by a *newer* downctl is reported and then ignored, never
    deleted: this build cannot interpret it, which is not the same as it being
    absent.
    """
    path = path_for(target)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if "version" in data and _version_is_unreadable(data.get("version"), BITMAP_VERSION):
        _report_future_state(path)
        return None
    return Bitmap.from_dict(data)


def save(target: str, bitmap: Bitmap) -> bool:
    """Atomically persist ``bitmap`` (fsync file + atomic replace).

    Returns ``False`` if the write failed (the caller keeps downloading; only
    resume ability is lost). It also returns ``False``, without writing, when
    the file on disk came from a newer downctl: replacing it would destroy the
    only copy of information this build cannot reproduce.
    """
    path = path_for(target)
    if written_by_a_newer_downctl(path, BITMAP_VERSION):
        _report_future_state(path)
        return False
    return _atomic_write_json(path, bitmap.to_dict())


def remove(target: str, *, force: bool = False) -> None:
    """Delete all persisted state for ``target`` (bitmap + completion marker).

    Raises :class:`FutureStateError` — deleting nothing — when either sidecar
    came from a newer build and ``force`` is not set.
    """
    future = future_state(target)
    if future is not None and not force:
        raise FutureStateError(target, future)
    for path in (
        path_for(target),
        path_for(target) + ".tmp",
        completion_path(target),
        completion_path(target) + ".tmp",
    ):
        unlink_state(path, force=force)


def remove_ledger(target: str, *, force: bool = False) -> None:
    """Delete the resume ledger only, leaving the completion marker alone.

    A finished multipart download has no use for its bitmap, but the marker
    still carries the version witness and digest it earned. Deleting both (as
    ``remove`` does) and then re-reading the marker loses exactly that proof:
    the engine's "never blank a recorded witness" rule had nothing left to
    read, so a plain re-run silently downgraded a proven file to unversioned.
    """
    future = future_state(target)
    if future is not None and not force:
        raise FutureStateError(target, future)
    unlink_state(path_for(target), force=force)
    unlink_state(path_for(target) + ".tmp", force=force)


def discard(target: str, *, force: bool = False) -> None:
    """Drop the data bytes *and* the resume state of ``target``.

    Removing only the sidecars would leave a full-size, hole-free file behind,
    which the no-state heuristic could later bless as complete. Truncating to
    zero makes the reset unambiguous.

    That truncation is exactly why the future-state check comes first: 0.1.5
    truncated the data and then failed to rewrite the ledger, so a downgrade
    turned a complete 100 kB file into 0 bytes while printing "left untouched".
    With a newer-build sidecar present this raises without touching anything.
    """
    future = future_state(target)
    if future is not None and not force:
        raise FutureStateError(target, future)
    if os.path.isfile(target):
        try:
            with open(target, "r+b") as fh:
                fh.truncate(0)
        except OSError as exc:
            # The sidecars are the only thing that says this file is not a
            # complete copy. Deleting them after a failed truncation would leave
            # a full-size file with no state, which the hole heuristic can later
            # bless as success — so the reset fails loudly instead.
            raise DownctlError(
                f"cannot reset {target}: {exc} (its resume state was left in place)"
            ) from exc
    remove(target, force=force)


def _fsync_dir(directory: str) -> None:
    """Best-effort fsync of a directory so a rename survives a crash.

    ``open(directory, "rb")`` looks like it should work and never did: Linux
    raises ``IsADirectoryError``, which a ``suppress(OSError)`` quietly ate —
    so the durability step this docstring promises never ran. A directory has
    to be opened with :func:`os.open`.
    """
    if os.name == "nt":  # directory fsync is not supported on Windows
        return
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.close(handle)


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
    *,
    validator: str = "",
) -> bool:
    """Record that ``target`` is complete for the object ``identity`` names.

    When a checksum was verified, its algorithm/digest are recorded too so a
    later skip can detect that a mutable source now advertises a different
    digest. ``validator`` is the version witness observed for this object, so a
    later run that sees a different one knows the file is stale.
    """
    try:
        stat = os.stat(target)
    except OSError:
        return False
    path = completion_path(target)
    if written_by_a_newer_downctl(path, COMPLETION_VERSION):
        # The marker is the only record of what a newer build verified; keeping
        # it costs this run one verbose line, replacing it costs that record.
        _report_future_state(path)
        return False
    return _atomic_write_json(
        path,
        {
            "version": COMPLETION_VERSION,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "url_sha256": identity,
            "checksum_algo": (checksum_algo or "").lower(),
            "checksum_hex": (checksum_hex or "").lower(),
            "validator": str(validator or ""),
        },
    )


def load_completion(target: str) -> dict[str, object] | None:
    """Load a completion marker, or ``None`` when missing/malformed.

    Markers from older formats (v1/v2, which have no version witness) are
    returned as-is; callers decide how much to trust them.
    """
    path = completion_path(target)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if "version" in data and _version_is_unreadable(data.get("version"), COMPLETION_VERSION):
        _report_future_state(path)
        return None
    version = data.get("version")
    size = data.get("size")
    mtime_ns = data.get("mtime_ns")
    identity = data.get("url_sha256", "")
    checksum_algo = data.get("checksum_algo", "")
    checksum_hex = data.get("checksum_hex", "")
    validator = data.get("validator", "")
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
        or not isinstance(validator, str)
    ):
        return None
    return data


def witness_of(record: Mapping[str, object] | None) -> str:
    """Which kind of version proof a marker *carries*.

    Returns ``"validator"``, ``"checksum"`` or ``"none"`` — the three states a
    skip must be able to name out loud instead of claiming blanket proof.

    This answers "what does the marker hold", not "what did this run check":
    a marker read without a probe (``downctl verify``, a local transport) still
    carries its witness. Callers that are about to *claim* a skip must use
    :func:`witness_compared` as well, because a carried witness is not a
    comparison.
    """
    if not record:
        return "none"
    if str(record.get("validator", "")):
        return "validator"
    if str(record.get("checksum_hex", "")):
        return "checksum"
    return "none"


def witness_compared(record: Mapping[str, object] | None, observed: str | None) -> bool:
    """Whether a marker's recorded witness was actually *compared* this run.

    A comparison needs both sides. ``observed is None`` means this run never saw
    the remote object (a local or SFTP transfer, ``downctl verify``), and an
    empty ``observed`` means the server offered no witness; neither can confirm
    or contradict what the marker recorded. Only a non-empty pair can, and the
    trust predicates accept that pair only when the two are equal — so a marker
    that passed them *with* both sides present was genuinely compared.
    """
    return record is not None and bool(str(record.get("validator", ""))) and bool(observed)


def completion_valid(
    target: str,
    expected: int = 0,
    identity: str | None = None,
    *,
    validator: str | None = None,
    require_witness: bool = False,
) -> bool:
    """Whether ``target`` has a marker proving it is complete.

    ``expected > 0`` additionally requires the recorded and current sizes to
    match it. ``identity`` is required: a marker without a URL identity (or an
    empty one) can never prove which object it belongs to.

    ``validator`` is the version witness this run observed for the object
    (``None`` when nothing was observed at all, e.g. no probe happened).

    A marker is a *completion record*, not a resume ledger, so its version rule
    is deliberately weaker than :func:`witness_matches`: a comparison is only
    possible when both sides have a witness. With a witness on both sides a
    mismatch means the object was replaced and the record is refused; with a
    witness missing on either side there is nothing to compare, so the marker
    is trusted (and reported as unversioned by :func:`witness_of`) unless the
    caller sets ``require_witness``. The worst case of that weaker rule is a
    *stale skip*, never a spliced file — and being stricter here would make
    every server that stopped (or started) sending ETags re-download
    everything.
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
    if record.get("url_sha256", "") != identity:
        return False
    stored = str(record.get("validator", ""))
    if not stored or not validator:
        # A marker written by 0.1.3 (or a server that offered no witness then, or
        # none now) leaves nothing to compare either way.
        return not require_witness
    return stored == validator


def witness_matches(stored: str, observed: str | None, require_witness: bool = False) -> bool:
    """Whether a stored version witness agrees with the one just observed.

    ``observed is None`` means this run never saw the remote object (a local or
    SFTP transfer, a direct API call), so no version check is possible and the
    stored state is accepted unless ``require_witness`` forbids unversioned
    state. An empty *observed* witness is a real observation ("the server
    offers none") and must match an empty stored one.
    """
    if observed is None:
        return bool(stored) or not require_witness
    if stored != observed:
        return False
    return bool(stored) or not require_witness


def bitmap_trusted(
    bitmap: Bitmap,
    current: int,
    expected: int,
    identity: str | None = None,
    *,
    validator: str | None = None,
    require_witness: bool = False,
) -> bool:
    """Whether ``bitmap`` describes a data file of ``current`` bytes.

    This is the single trust predicate for resumable state: the grid must be
    internally consistent, the file must still cover every ``1`` bit (the
    high-water mark), it must not have shrunk below the last durable snapshot
    (``min_size``), and a complete bitmap requires the exact final size.
    ``identity`` (when supplied) must match the recorded URL identity, and
    ``validator`` (when supplied) must match the recorded version witness — a
    ledger built from another revision of the same URL would splice two
    objects together.
    """
    if expected <= 0 or bitmap.size != expected:
        return False
    if bitmap.version < BITMAP_VERSION or not bitmap.url_sha256:
        return False
    if identity is not None and bitmap.url_sha256 != identity:
        return False
    if not witness_matches(bitmap.validator, validator, require_witness):
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
@dataclass(frozen=True)
class Resume:
    """What an attempt may skip, and what that decision rests on.

    ``proof`` is load-bearing, not decoration. Only ``fresh`` and ``ledger``
    mean the bytes now on disk were obtained (or compared) under the version
    witness this run observed. A ``marker`` or ``holes`` decision can hand back
    a file this run never looked at, so the caller must not stamp today's
    witness onto it — doing exactly that is how a stale file acquired a
    certificate it had never earned.
    """

    bits: list[int]
    proof: str = PROOF_FRESH
    #: Set when a completion marker was removed, i.e. a *finished* file is about
    #: to be replaced because the URL now names a different revision. The caller
    #: reports it: a completed file that quietly starts reading as 0 % complete
    #: is exactly the kind of silent state change this project refuses.
    dropped_claim: bool = False


def resolve(
    target: str,
    expected: int,
    pieces: int,
    piece_size: int = 0,
    identity: str | None = None,
    *,
    validator: str | None = None,
    require_witness: bool = False,
) -> Resume:
    """Decide the ``bits`` for this attempt (``1`` = already complete).

    When the piece grid changed (e.g. the user altered ``--connections`` or
    ``--piece-size``) but the bitmap knows its old piece size, the completion
    state is *resliced* by byte range instead of discarded, so changing the
    connection count no longer throws away progress.

    ``identity`` binds the ledger to a remote object. Any bitmap whose recorded
    identity differs (including a pre-v4 bitmap that has no identity at all) is
    discarded, because it cannot prove the bytes belong to this URL. An empty
    or missing identity never trusts existing state at all (production callers
    always pass one).

    ``validator`` binds it to a *version* of that object: when this run
    observed a witness (``str``, possibly empty for "the server offers none")
    it must equal the recorded one, otherwise the ledger describes bytes of a
    different revision and resuming it would splice two objects together. A
    ``None`` validator means nothing was observed and no check is possible.

    On any state that cannot be trusted the target is abandoned — see
    :func:`abandon` — and an all-zero grid is returned (never extend a partial
    file into a full-size sparse file: that would be mistaken for success).

    Raises :class:`FutureStateError` when a sidecar came from a newer build.
    That is not "untrustworthy state to be reset": it is state this build
    cannot read and must not destroy, so the decision belongs to the user.
    """
    future = future_state(target)
    if future is not None:
        raise FutureStateError(target, future)
    if not identity:
        # Without an identity there is nothing to pin the old bytes to, so the
        # ambiguity cannot be described and they have to go. Production callers
        # always pass one (``transfer_file`` derives it from the URL).
        discard(target)
        return Resume([0] * pieces)
    base = load(target)
    current = os.path.getsize(target) if os.path.isfile(target) else 0

    if base is not None and base.size == expected:
        if base.version < BITMAP_VERSION or base.url_sha256 != identity:
            return abandon(target, expected, pieces, piece_size, identity, validator)
        if not witness_matches(base.validator, validator, require_witness):
            # The object behind this URL was replaced (or this run refuses
            # unversioned state): the completed bits belong to another
            # revision, so they must not be reused.
            return abandon(target, expected, pieces, piece_size, identity, validator)
        same_grid = base.pieces == pieces and (
            piece_size == 0 or base.piece_size in (0, piece_size)
        )
        if same_grid:
            if bitmap_trusted(
                base,
                current,
                expected,
                identity,
                validator=validator,
                require_witness=require_witness,
            ):
                return Resume(list(base.bits), PROOF_LEDGER)
            # Some filesystems (WSL DrvFS/9p) lag on stat metadata; re-check once.
            current = os.path.getsize(target) if os.path.isfile(target) else 0
            if bitmap_trusted(
                base,
                current,
                expected,
                identity,
                validator=validator,
                require_witness=require_witness,
            ):
                return Resume(list(base.bits), PROOF_LEDGER)
            return abandon(target, expected, pieces, piece_size, identity, validator)
        if (
            base.piece_size > 0
            and piece_size > 0
            and bitmap_trusted(
                base,
                current,
                expected,
                identity,
                validator=validator,
                require_witness=require_witness,
            )
        ):
            remapped = _remap(base.bits, base.piece_size, pieces, piece_size, expected)
            return Resume(remapped, PROOF_LEDGER)
        return abandon(target, expected, pieces, piece_size, identity, validator)

    if current >= expected and completion_valid(
        target, expected, identity, validator=validator, require_witness=require_witness
    ):
        return Resume([1] * pieces, PROOF_MARKER)
    if base is not None or has_state(target):
        # A stale bitmap for another size, a malformed ledger or a marker for
        # another URL/file is positive evidence not to trust this data file
        # with the weaker hole heuristic. Reset and re-fetch.
        return abandon(target, expected, pieces, piece_size, identity, validator)
    # A file larger than the expected object is not a completed copy of it;
    # reset instead of truncating an unproven prefix and calling it success.
    if current == expected and trusted_complete(target, current) and not require_witness:
        return Resume([1] * pieces, PROOF_HOLES)
    return abandon(target, expected, pieces, piece_size, identity, validator)


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


def abandon(
    target: str,
    expected: int,
    pieces: int,
    piece_size: int,
    identity: str,
    validator: str | None,
) -> Resume:
    """Give up on the state of ``target`` without destroying bytes needlessly.

    The data file used to be truncated right here, before a single replacement
    byte had been requested. When the object had been replaced and the new
    revision could not be fetched at all (a 503, a dropped network), a
    *complete* file of the previous revision was gone and nothing took its
    place — reproduced before this fix: 1 MiB complete file, server's ETag
    changed, every GET 503 -> size 0, exit 1, no replacement.

    What actually has to hold is narrower than "the old bytes are gone": they
    must never be *mistaken* for the new object. Pinning the state achieves
    that. The completion marker — which describes the old revision — is removed,
    and an explicit all-zero ledger bound to this URL and this run's witness is
    written, so the file reads as 0 % complete until the transfer overwrites it
    piece by piece. A crash, a SIGKILL or a failed run leaves a file that is
    still honestly described, and the next run re-fetches every piece.

    If that ledger cannot be written the ambiguity is real, and only then are
    the bytes discarded: an undescribed full-size file is exactly what the
    hole heuristic would later bless as complete.

    A brand-new target (no file, no sidecars) has nothing to describe and is
    left alone, so an ordinary first download still writes no ledger until it
    has something to record.
    """
    dropped_claim = os.path.isfile(completion_path(target))
    had_state = dropped_claim or os.path.isfile(path_for(target))
    unlink_state(completion_path(target))
    try:
        current = os.path.getsize(target)
    except OSError:
        current = 0
    if piece_size <= 0 and expected > 0 and pieces > 0:
        # A direct caller may fix the piece *count* without fixing the grid.
        # Derive the grid that count implies so the pinned ledger is still well
        # formed; the next run's ``resolve`` reslices or ignores it either way.
        piece_size = (expected + pieces - 1) // pieces
    # Anything already on disk — a stale prefix, a finished file, an
    # undescribed full-size file — could later be read as a complete copy of
    # this object, so it has to be described before it is overwritten.
    if had_state or current > 0:
        describable = expected > 0 and pieces > 0 and piece_size > 0 and identity
        if describable and save(
            target,
            Bitmap(
                size=expected,
                pieces=pieces,
                bits=[0] * pieces,
                min_size=0,
                piece_size=piece_size,
                url_sha256=identity,
                validator=str(validator or ""),
            ),
        ):
            return Resume([0] * pieces, dropped_claim=dropped_claim)
        discard(target)
    return Resume([0] * pieces, dropped_claim=dropped_claim)


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
