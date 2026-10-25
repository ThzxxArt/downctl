"""Download orchestration: turn tasks into completed files.

The engine owns the retry loop, disk checks, locking, transport selection and
checksum verification. It drives :mod:`downctl.core.transfer` and reports
through a reporter plugin.

Content truth
-------------
A skip ("this file is already the object") is only claimed with a *witness*,
reported as ``validator`` / ``checksum`` / ``none`` (:func:`_prove_complete`).
An object that was replaced between two runs is detected before any piece is
resumed, and an object replaced *during* a transfer is turned into a
one-shot restart instead of a spliced file.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from downctl import console
from downctl.core import disk
from downctl.core import pieces as piece_store
from downctl.core.disk import DISK_RESERVE, has_space
from downctl.core.errors import (
    FAILURE_IO,
    FAILURE_PERMANENT,
    FAILURE_TIMEOUT,
    FAILURE_TRANSIENT_EXHAUSTED,
    FAILURE_UNKNOWN,
    ChecksumError,
    ContentChangedError,
    DiskSpaceError,
    DownctlError,
    FutureStateError,
    PermanentError,
    PermanentHTTPError,
    RangeNotSatisfiableError,
    RangeSizeMismatchError,
    TransientHTTPError,
    TransportError,
    classify_failure,
)
from downctl.core.locks import FileLock, LockError
from downctl.core.ratelimit import RateLimiter
from downctl.core.transfer import TransferResult, transfer_file
from downctl.core.watchdog import DEFAULT_STALL_LIMIT
from downctl.plugin.callbacks import call_optional, call_optional_safe
from downctl.probe import probe_metadata
from downctl.urls import display_url, redact_text, url_identity

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext
    from downctl.core.task import DownloadTask
    from downctl.plugin.protocols import Reporter, Transport, Verifier

RETRY_BASE = 5
RETRY_BACKOFF_MAX = 60
#: Longest server hint we will honour. A broken or hostile ``Retry-After``
#: (``3600``) must not park a batch for an hour.
RETRY_AFTER_MAX = 300.0
CHECKSUM_FAIL_MAX = 2
DEFAULT_CONNECTIONS = 16
#: How many times a single target may be restarted because the remote object
#: changed or rejected our range. Bounded so a flapping server cannot loop.
MAX_RESETS = 1
#: How many times a task's URL may be re-signed during one download. A presigned
#: link carries its own expiry; more than a couple of renewals means the signer
#: is wrong rather than the clock.
REFETCH_MAX = 3

__all__ = ["DISK_RESERVE", "download_all", "download_task", "retry_delay"]


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    """Seconds to wait before retry number ``attempt`` (1-based).

    A server-supplied ``Retry-After`` wins over the exponential backoff, capped
    at :data:`RETRY_AFTER_MAX`. This is the single pacing policy for both the
    engine's retry loop and the app's probe retries.
    """
    if retry_after is not None:
        return min(max(0.0, retry_after), RETRY_AFTER_MAX)
    base = RETRY_BASE
    with contextlib.suppress(ValueError):
        base = int(os.environ.get("DOWNCTL_RETRY_BASE", RETRY_BASE))
    backoff = min(base * (1 << (attempt - 1)), RETRY_BACKOFF_MAX)
    return float(backoff + random.randint(0, backoff // 2 + 1))  # noqa: S311 - pacing jitter


def _observed_validator(task: DownloadTask, override: str | None = None) -> str | None:
    """The version witness to check this task's resume state against.

    ``None`` means "this run never saw the remote object" (a local or SFTP
    transfer), so no version check is possible. A ``str`` — including an empty
    one — is a real observation: the server named that version, or named none.
    """
    if override is not None:
        return override
    if "validator" in task.meta:
        return str(task.meta.get("validator") or "")
    return None


def _task_require_witness(ctx: RuntimeContext, task: DownloadTask) -> bool:
    """Whether unversioned state must be refused for this task.

    The engine owns the meaning of its own per-task flags so a direct caller
    can never get a weaker guarantee than the CLI: ``--recheck`` is "prove it or
    fetch it", which demands a witness as well as a digest.
    """
    if bool(ctx.config.core.require_witness):
        return True
    meta = task.meta
    return bool(meta.get("require_witness")) or bool(meta.get("recheck"))


@dataclass(frozen=True)
class CompletionProof:
    """Whether an existing file is provably the finished object, and how."""

    proven: bool
    #: ``"validator"``, ``"checksum"``, ``"none"``, or ``"incomplete"``.
    witness: str = "incomplete"
    #: Whether ``witness == "validator"`` rests on an actual comparison between
    #: a recorded witness and the one this run observed. A marker read without a
    #: probe (a local or SFTP transport) still *carries* a witness but never
    #: compared it, and stamping today's version on that basis is how a stale
    #: file earned a certificate it had not been given.
    compared: bool = False

    def __bool__(self) -> bool:
        return self.proven


def _recorded_proof(marker: Mapping[str, object] | None) -> tuple[str, str, str]:
    """The witness and digest an existing completion marker already records.

    Re-writing a marker must never *lose* proof it already carried. A run
    without ``--checksum`` used to blank the digest the original download had
    verified, and a run that could not compare a version blanked the witness;
    both left the next run's change detection weaker than it found it.
    """
    if marker is None:
        return "", "", ""
    return (
        str(marker.get("validator") or ""),
        str(marker.get("checksum_algo") or ""),
        str(marker.get("checksum_hex") or ""),
    )


def _carried_witness(record: Mapping[str, object], observed: str | None) -> str:
    """The witness a *skip* may claim for a marker, given what it compared.

    A marker records the version its bytes were obtained under. Claiming
    ``validator`` means this run compared that record with what the server names
    now, which needs both sides to exist; a marker read with no probe at all has
    nothing to compare, so the claim falls back to the content proof the marker
    carries instead of borrowing one it never earned.
    """
    if piece_store.witness_compared(record, observed):
        return "validator"
    return "checksum" if str(record.get("checksum_hex", "")) else "none"


def refuse_self_copy(task: DownloadTask, target: str) -> None:
    """Refuse a ``file://`` copy whose destination is the source file itself.

    Copying a file onto itself has no meaning, and it is not harmless: with
    ``--force`` the destination is truncated before the transfer reads it, so
    the only copy of those bytes is destroyed and the retry loop then chases a
    source it is itself emptying. Nothing else in the pipeline compares the two
    paths, so the refusal has to happen where the target is resolved — before
    any code decides the target file is disposable.
    """
    source = task.meta.get("source_path")
    if task.source != "file" or not isinstance(source, str) or not source:
        return
    try:
        same = os.path.samefile(target, source)
    except OSError:
        # A missing path cannot collide with anything (yet).
        return
    if same:
        raise DownctlError(f"refusing to copy {source} onto itself; choose a different destination")


def target_path(base: str, task: DownloadTask) -> str:
    """Where ``task`` lands under ``base``, without touching the filesystem.

    ``dry-run`` has to predict the *same* path a real run will use, so the rule
    lives here once: an absolute target is taken as given, and a source-provided
    one is confined to the destination (``..``, a symlink and a different drive
    all have to be refused). The previous dry-run copy of this logic used
    ``abspath`` and skipped the symlink check, so a directory that is a symlink
    made the prediction disagree with the download.
    """
    if task.allow_absolute:
        # Explicit user request (e.g. -o/--output).
        return os.path.abspath(task.target)
    root = os.path.realpath(os.path.expanduser(base or os.getcwd()))
    path = os.path.abspath(os.path.join(root, task.target))
    resolved = os.path.realpath(path)
    try:
        inside = os.path.commonpath([resolved, root]) == root
    except ValueError:  # different drive (Windows)
        inside = False
    if not inside:
        raise DownctlError(f"unsafe target path escapes the destination: {task.target!r}")
    return path


def _resolve_target(ctx: RuntimeContext, task: DownloadTask) -> str:
    path = target_path(ctx.config.core.dir, task)
    refuse_self_copy(task, path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _has_space(path: str, need: int) -> bool:
    return has_space(path, need)


def _task_identity(task: DownloadTask) -> str:
    """Stable identity of the remote object a task downloads.

    Computed from the URL *before* mirror rewriting (the app stores it in
    ``task.meta['identity']``); presigned URLs hash only their path, because
    their query rotates on every run.
    """
    stored = task.meta.get("identity") or task.meta.get("source_identity")
    if isinstance(stored, str) and stored:
        return stored
    return url_identity(task.url, redact=bool(task.meta.get("redact_url")))


def seal_identity(task: DownloadTask) -> None:
    """Bind resume state to the URL that will actually be fetched.

    Public because the app and the engine must agree: the app seals after a
    router rewrite, and :func:`refresh_url` must re-seal after a presigned URL
    is renewed, or the *new* signature's query would resume the *old* object's
    ledger (or refuse it for no reason).
    """
    redact = bool(task.meta.get("redact_url"))
    final = url_identity(task.url, redact=redact)
    source = str(task.meta.get("source_identity") or "")
    if source:
        task.meta["identity"] = hashlib.sha256(f"{source}\0{final}".encode()).hexdigest()
    else:
        task.meta["identity"] = final


def _safe_error(task: DownloadTask, exc: BaseException) -> str:
    """Format an error without leaking a presigned query or a token."""
    text = str(exc)
    if task.url and task.url in text:
        text = text.replace(task.url, display_url(task.url, bool(task.meta.get("redact_url"))))
    # The message may mention a *different* URL (a redirect target, a mirror,
    # a signed link from a plugin); redact every URL, not just this task's.
    return redact_text(text)


def _remaining_bytes(
    target: str,
    task: DownloadTask,
    connections: int,
    piece_size: int = 0,
    identity: str | None = None,
    *,
    validator: str | None = None,
    require_witness: bool = False,
) -> int:
    """Bytes still needed, based on the piece bitmap rather than file size.

    Out-of-order pieces can make the on-disk size look almost complete while
    many pieces are still missing (written at absolute offsets), so the file
    size would understate the remaining work. When the existing bytes cannot
    be resumed (no bitmap/marker, stale identity, replaced object, truncated
    file), the transfer resets the target and fetches the whole object, so the
    full size is reported instead of the misleading ``size - current``.
    """
    if task.size <= 0:
        return 0
    identity = _task_identity(task) if identity is None else identity
    observed = _observed_validator(task, validator)
    bitmap = piece_store.load(target)
    if bitmap is not None and bitmap.size == task.size:
        current = os.path.getsize(target) if os.path.isfile(target) else 0
        if not piece_store.bitmap_trusted(
            bitmap,
            current,
            task.size,
            identity,
            validator=observed,
            require_witness=require_witness,
        ):
            # Foreign/stale/replaced ledger or a data file that no longer
            # covers it.
            return task.size
        return max(0, task.size - bitmap.done_bytes())
    if piece_store.load_completion(target) is not None:
        valid = piece_store.completion_valid(
            target,
            task.size,
            identity,
            validator=observed,
            require_witness=require_witness,
        )
        return 0 if valid else task.size
    if piece_store.has_state(target):
        # A malformed/stale bitmap that could not even be loaded must not let
        # the weaker hole heuristic bless the file.
        return task.size
    current = os.path.getsize(target) if os.path.isfile(target) else 0
    if current == task.size and piece_store.trusted_complete(target, current):
        # A full-size, unmarked file is a *heuristic*, not a witness. Under
        # ``--require-witness`` it is exactly the claim the flag exists to
        # refuse: the file may be a copy, a different revision, or a sparse
        # write the filesystem cannot show. It used to be honoured anyway, so
        # the flag was bypassed on the one path that needs it most.
        return 0 if not require_witness else task.size
    # Anything else is discarded by ``pieces.resolve`` and fetched from zero.
    return task.size


def _completed_bytes(
    target: str,
    task: DownloadTask,
    connections: int,
    piece_size: int = 0,
    identity: str | None = None,
    *,
    validator: str | None = None,
    require_witness: bool = False,
) -> int:
    """Bytes already complete, for resume-aware progress reporting.

    Only counts bytes the transfer will actually *skip*: a valid completion
    marker, what the URL- and version-bound piece bitmap proves, or a full-size
    file with no holes. A partial/untrusted file is re-downloaded from scratch,
    so it must report ``0`` (otherwise the resume seed and the fresh bytes
    would be double-counted).
    """
    if task.size <= 0:
        return 0
    identity = _task_identity(task) if identity is None else identity
    observed = _observed_validator(task, validator)
    bitmap = piece_store.load(target)
    if bitmap is not None and bitmap.size == task.size:
        current = os.path.getsize(target) if os.path.isfile(target) else 0
        if not piece_store.bitmap_trusted(
            bitmap,
            current,
            task.size,
            identity,
            validator=observed,
            require_witness=require_witness,
        ):
            # Foreign URL, another revision, truncated below the ledger's
            # high-water mark, or a malformed grid: the transfer will reset,
            # so nothing is skipped.
            return 0
        return min(bitmap.done_bytes(), task.size)
    if piece_store.load_completion(target) is not None:
        valid = piece_store.completion_valid(
            target,
            task.size,
            identity,
            validator=observed,
            require_witness=require_witness,
        )
        return task.size if valid else 0
    if piece_store.has_state(target):
        return 0
    current = os.path.getsize(target) if os.path.isfile(target) else 0
    if (
        current == task.size
        and not require_witness
        and piece_store.trusted_complete(target, current)
    ):
        return task.size
    return 0


def _get_transport(ctx: RuntimeContext, task: DownloadTask) -> Transport:
    registry = ctx.registry
    name = task.transport or "http"
    transport = registry.get("transport", name) if registry is not None else None
    if transport is None:
        raise DownctlError(f"no transport plugin named {name!r}")
    return transport  # type: ignore[return-value]


def _get_verifier(ctx: RuntimeContext, algo: str) -> Verifier | None:
    if not algo or ctx.registry is None:
        return None
    return ctx.registry.get("verifier", algo)  # type: ignore[return-value]


def _emit(ctx: RuntimeContext, event: str, payload: dict[str, object]) -> None:
    if ctx.events is not None:
        ctx.events.emit(event, payload)


def _notify_resumed(reporter: Reporter, task: DownloadTask, done: int) -> None:
    """Tell a resume-aware reporter how much of ``task`` is already complete."""
    call_optional(reporter, "task_resumed", task, done)


def _notify_phase(
    reporter: Reporter | None, task: DownloadTask, phase: str, detail: str = ""
) -> None:
    """A presentation-only state change; never let it fail the transfer."""
    call_optional_safe(reporter, "task_phase", task, phase, detail=detail)


def _notify_retrying(
    reporter: Reporter | None,
    task: DownloadTask,
    attempt: int,
    delay: float,
    reason: str,
) -> None:
    call_optional_safe(reporter, "task_retrying", task, attempt, delay, reason)


def _notify_restarted(reporter: Reporter | None, task: DownloadTask) -> None:
    call_optional_safe(reporter, "task_restarted", task)


def _prove_complete(
    target: str,
    task: DownloadTask,
    identity: str,
    *,
    validator: str | None = None,
    require_witness: bool = False,
) -> CompletionProof:
    """Whether an existing file is *provably* the finished object.

    Size equality alone is never proof: an interrupted multi-connection
    download can leave a full-size file with holes. Only a completion marker, a
    matching all-complete bitmap, or a provably hole-free file count — and the
    returned witness names which of those it was, so a skip can be reported
    honestly instead of as unconditional proof.
    """
    observed = _observed_validator(task, validator)
    expected = task.size if task.size > 0 else 0
    if task.size > 0:
        done = _completed_bytes(
            target,
            task,
            0,
            0,
            identity,
            validator=observed,
            require_witness=require_witness,
        )
        if done < task.size:
            return CompletionProof(False)
    elif not piece_store.completion_valid(
        target, 0, identity, validator=observed, require_witness=require_witness
    ):
        return CompletionProof(False)

    record = piece_store.load_completion(target)
    if record is not None and piece_store.completion_valid(
        target, expected, identity, validator=observed, require_witness=require_witness
    ):
        return CompletionProof(
            True,
            _carried_witness(record, observed),
            piece_store.witness_compared(record, observed),
        )
    bitmap = piece_store.load(target)
    if bitmap is not None and bitmap.complete:
        compared = bool(bitmap.validator) and bool(observed)
        return CompletionProof(True, "validator" if compared else "none", compared)
    return CompletionProof(True, "none")


def _failed(
    ctx: RuntimeContext,
    task: DownloadTask,
    reporter: Reporter | None,
    reason: str,
    error: str,
) -> bool:
    """Classify why a target failed, report it, and return ``False``.

    Classifying and reporting are one step because the order between them is
    the whole point: the JSON reporter reads ``task.meta['failure']`` while it
    emits the ``finish`` record, so classifying *after* notifying produced
    ``finish`` lines with no ``reason`` at all. The documented field was
    therefore unreachable for every engine-stage failure, while probe failures
    (which classify first) did carry it.

    Always returns ``False`` so call sites read ``return _failed(...)``: the
    classification travels with the ``False`` instead of being a separate step
    a later edit could forget.
    """
    task.meta["failure"] = reason
    if reporter is not None:
        reporter.task_finished(task, False)
    _emit(ctx, "on_error", {"task": task, "error": error, "job": ctx.job})
    return False


async def download_task(
    ctx: RuntimeContext,
    task: DownloadTask,
    *,
    reporter: Reporter | None = None,
    retries: int | None = None,
    connections: int = DEFAULT_CONNECTIONS,
    stall_limit: int = DEFAULT_STALL_LIMIT,
    piece_size: int = 0,
    min_split_size: int = 0,
    checkpoint: int = 0,
    force: bool = False,
    skip_existing: bool = False,
) -> bool:
    """Download one task. Returns ``True`` on success.

    Raises nothing on ordinary failure; returns ``False`` instead. Permanent
    HTTP errors (401/403/404) also return ``False`` without retrying.

    ``ctx.config.core`` supplies three policies: ``retries`` (0 = retry
    forever), ``max_time`` (wall-clock cap for this one target, 0 = none) and
    ``require_witness`` (refuse to skip or resume state whose object version
    cannot be checked).
    """
    target = _resolve_target(ctx, task)
    task.meta["path"] = target
    identity = _task_identity(task)
    max_retries = ctx.config.core.retries if retries is None else retries
    max_time = int(getattr(ctx.config.core, "max_time", 0) or 0)
    require_witness = _task_require_witness(ctx, task)
    observed = _observed_validator(task)
    validator = observed or ""
    transport = _get_transport(ctx, task)
    rate_limiter = ctx.rate_limiter if ctx.rate_limiter is not None else RateLimiter(ctx.rate_limit)
    if reporter is not None:
        reporter.task_started(task)
    _emit(ctx, "on_task_start", {"task": task, "job": ctx.job})

    checksum_error = ""
    if task.checksum_hex and not task.checksum_algo:
        checksum_error = "checksum digest provided without an algorithm"
    elif task.checksum_hex and _get_verifier(ctx, task.checksum_algo) is None:
        checksum_error = f"no verifier for algorithm {task.checksum_algo!r}"
    if checksum_error:
        # Do not fetch a byte that cannot be verified: a missing verifier is a
        # configuration error, not a reason to re-download the file twice.
        console.error(checksum_error)
        return _failed(ctx, task, reporter, FAILURE_PERMANENT, checksum_error)

    lock = FileLock(target)
    try:
        lock.acquire()
    except LockError as exc:
        console.warn(str(exc))
        return _failed(ctx, task, reporter, FAILURE_PERMANENT, str(exc))

    # Everything from here to the end of the retry loop holds the lock, so the
    # release lives in one ``finally``: an exception from a reporter, a hook or a
    # third-party verifier on the skip path must not leave a stale `.lock`
    # behind (which would fail the next run for this target).
    try:
        succeeded = await _download_locked(
            ctx,
            task,
            target=target,
            identity=identity,
            transport=transport,
            rate_limiter=rate_limiter,
            reporter=reporter,
            max_retries=max_retries,
            max_time=max_time,
            require_witness=require_witness,
            observed=observed,
            validator=validator,
            force=force,
            skip_existing=skip_existing,
            connections=connections,
            stall_limit=stall_limit,
            piece_size=piece_size,
            min_split_size=min_split_size,
            checkpoint=checkpoint,
        )
    except Exception:
        # An unclassified escape (a third-party reporter, hook or verifier
        # raising) still failed this target, and the failed log has to be able
        # to say so instead of leaving a machine to parse the message.
        task.meta.setdefault("failure", FAILURE_PERMANENT)
        raise
    finally:
        lock.release()
    if not succeeded:
        # Every ``return False`` inside the locked body goes through
        # :func:`_failed`, which classifies *before* it notifies the reporter.
        # This default is the safety net for the remaining way out: an exception
        # escaping a reporter, hook or third-party verifier, where no call site
        # ever got to classify anything.
        task.meta.setdefault("failure", FAILURE_PERMANENT)
    return succeeded


async def _download_locked(
    ctx: RuntimeContext,
    task: DownloadTask,
    *,
    target: str,
    identity: str,
    transport: Transport,
    rate_limiter: RateLimiter,
    reporter: Reporter | None,
    max_retries: int,
    max_time: int,
    require_witness: bool,
    observed: str | None,
    validator: str,
    force: bool,
    skip_existing: bool,
    connections: int,
    stall_limit: int,
    piece_size: int,
    min_split_size: int,
    checkpoint: int,
) -> bool:
    """The body of :func:`download_task`, with the target lock already held."""
    if force:
        console.verbose(f"forcing re-download of {target}")
        # ``--force`` is the user's explicit "discard this target", so it is the
        # one path allowed to destroy state a newer build wrote.
        _discard(target, force=True)
        _notify_restarted(reporter, task)

    recheck = bool(task.meta.get("recheck"))
    if (skip_existing or recheck) and os.path.isfile(target):
        proof = _prove_complete(
            target,
            task,
            identity,
            validator=observed,
            require_witness=require_witness,
        )
        skip = proof.proven
        witness = proof.witness
        # User-supplied checksums are always re-checked. A source checksum
        # (HF LFS, GitHub digest) is trusted only when a completion marker
        # proves this exact download was verified; a complete bitmap or a
        # hole-scan proof can survive a kill between the last checkpoint and
        # verification, so it must be hashed before being blessed.
        marker = piece_store.load_completion(target)
        marker_ok = piece_store.completion_valid(
            target,
            task.size if task.size > 0 else 0,
            identity,
            validator=observed,
            require_witness=require_witness,
        )
        recheck = bool(task.meta.get("recheck"))
        if recheck and marker is not None and not task.checksum_hex:
            # ``--recheck`` means "prove it or fetch it again". When the source
            # no longer advertises a digest, the one recorded by the original
            # download is the only available proof, so it is re-used — but only
            # if a verifier for it still exists.
            recorded_algo = str(marker.get("checksum_algo") or "")
            recorded_hex = str(marker.get("checksum_hex") or "")
            if recorded_algo and recorded_hex and _get_verifier(ctx, recorded_algo) is not None:
                task.checksum_algo, task.checksum_hex = recorded_algo, recorded_hex
        marker_digest_ok = not task.checksum_hex or (
            marker is not None
            and str(marker.get("checksum_algo", "")) == (task.checksum_algo or "").lower()
            and str(marker.get("checksum_hex", "")) == (task.checksum_hex or "").lower()
        )
        must_verify = bool(task.checksum_hex) and (
            recheck or bool(task.meta.get("user_checksum")) or not marker_ok or not marker_digest_ok
        )
        size_on_disk = os.path.getsize(target)
        # A user-supplied digest proves the content even when the marker carries
        # no version witness. Refusing that proof made ``--recheck --checksum``
        # discard a complete, correct file and re-download it without ever
        # hashing it --- the opposite of what ``--recheck`` promises.
        complete_size = task.size > 0 and size_on_disk >= task.size
        hash_proves = recheck and complete_size and bool(task.checksum_hex)
        if (skip or hash_proves) and must_verify:
            try:
                skip = await _verify(ctx, task, target)
            except ChecksumError as exc:
                console.error(str(exc))
                return _failed(ctx, task, reporter, FAILURE_PERMANENT, str(exc))
            if skip:
                # The digest was recomputed and matched: that is content proof,
                # stronger than a version witness.
                witness = "checksum"
            else:
                console.warn(
                    f"existing {target} does not match the expected checksum; re-downloading"
                )
                _discard(target)
                _notify_restarted(reporter, task)
        if recheck and not skip and complete_size:
            # A *complete* file that nothing this run can check is discarded so
            # ``--recheck`` cannot bless a stale copy. A partial file is not
            # that case: its ledger still proves which pieces are valid, and
            # discarding it threw away resumable bytes (the run then fetched
            # them again from zero).
            console.warn(f"existing {target} could not be proven; re-downloading")
            _discard(target)
            _notify_restarted(reporter, task)
        if skip:
            # Only a witness this run actually *compared* may be stamped, and one
            # that was already recorded is never erased. A skip proven by a
            # digest is content proof, not a version claim, and a skip proven
            # with no probe at all (a local/SFTP transport) compared nothing:
            # stamping today's version onto either would silence the next change
            # detection, and blanking the recorded one would weaken it.
            stored_validator, stored_algo, stored_hex = _recorded_proof(marker)
            recorded = validator if proof.compared else stored_validator
            if witness == "none":
                console.warn(
                    f"skipping existing {target} (unversioned: no version witness to check)"
                )
            else:
                console.verbose(f"skipping existing {target} (proven by {witness})")
            if not piece_store.save_completion(
                target,
                identity,
                task.checksum_algo or stored_algo,
                task.checksum_hex or stored_hex,
                validator=recorded,
            ):
                console.verbose(f"could not write completion marker for {target}")
            # Set before the reporter runs: the JSON ``finish`` record is where
            # a machine reads the witness, and it must never be able to report
            # ``ok`` without saying what that claim rests on.
            task.meta["witness"] = witness
            task.meta["skipped"] = True
            if reporter is not None:
                reporter.task_finished(task, True)
            _emit(
                ctx,
                "on_task_finish",
                {"task": task, "ok": True, "skipped": True, "witness": witness, "job": ctx.job},
            )
            return True

    effective_connections = connections
    if min_split_size > 0 and 0 < task.size < min_split_size:
        effective_connections = 1

    retries_done = 0
    checksum_failures = 0
    resets_done = 0
    witness_drops = 0
    last_error = ""
    loop = asyncio.get_running_loop()
    deadline = _target_deadline(task, max_time, loop)
    # ``_completed_bytes`` only *reads* state (``load`` reports an unreadable
    # ledger and returns ``None``); the refusal that raises
    # :class:`FutureStateError` happens in ``transfer_file``'s ``resolve`` call,
    # which is handled below. A second handler here never ran.
    done = _completed_bytes(
        target,
        task,
        effective_connections,
        piece_size,
        identity,
        validator=observed,
        require_witness=require_witness,
    )
    if done > 0:
        if reporter is not None:
            _notify_resumed(reporter, task, done)
        console.verbose(f"resuming {target}: {done} byte(s) already complete ({task.size} total)")
    while True:
        if not _has_space(
            target,
            _remaining_bytes(
                target,
                task,
                effective_connections,
                piece_size,
                identity,
                validator=observed,
                require_witness=require_witness,
            ),
        ):
            error = DiskSpaceError(f"not enough free space for {target}")
            console.error(str(error))
            return _failed(ctx, task, reporter, FAILURE_PERMANENT, str(error))

        def _progress(delta: int, _task: DownloadTask = task) -> None:
            ctx.stats.bytes_transferred += delta
            if reporter is not None:
                reporter.task_progress(_task, delta)

        remaining = None if deadline is None else deadline - loop.time()
        if remaining is not None and remaining <= 0:
            return _give_up_timeout(ctx, task, target, max_time, reporter)

        retry_after: float | None = None

        # Bound explicitly: a restart inside this loop replaces the witness,
        # and each iteration must use the values current at its own start.
        async def _attempt(
            _validator: str = validator,
            _if_range: str = str(task.meta.get("if_range") or ""),
        ) -> tuple[TransferResult, bool]:
            _notify_phase(reporter, task, "download")
            transferred = await transfer_file(
                ctx,
                transport,
                task.url,
                target,
                task.size,
                connections=effective_connections,
                headers=task.headers,
                rate_limiter=rate_limiter,
                on_progress=_progress,
                stall_limit=stall_limit,
                piece_size=piece_size,
                identity=identity,
                checkpoint=checkpoint,
                validator=_validator,
                if_range=_if_range,
                require_witness=require_witness,
            )
            # Verifying a *partial* file hashed every retry's half-finished
            # bytes: the digest cannot match, so the transfer was restarted for
            # nothing and the run paid a full-file hash per attempt.
            if not transferred.complete:
                verified = True
            else:
                if task.checksum_hex:
                    # Hashing a multi-gigabyte file can take minutes after the
                    # bar reached 100 %. Without a phase event that looks
                    # exactly like a hang, and the user kills a download that
                    # is verifying.
                    _notify_phase(reporter, task, "verify", task.checksum_algo or "checksum")
                verified = await _verify(ctx, task, target)
            return transferred, verified

        try:
            if remaining is None:
                result, verified = await _attempt()
            else:
                async with asyncio.timeout(remaining):
                    result, verified = await _attempt()
        except TimeoutError:
            if deadline is None:
                raise
            return _give_up_timeout(ctx, task, target, max_time, reporter)
        except PermanentHTTPError as exc:
            if refresh_url(task, exc.status):
                continue
            message = _safe_error(task, exc)
            console.error(f"{message} (not retryable)")
            return _failed(ctx, task, reporter, FAILURE_PERMANENT, message)
        except PermanentError as exc:
            # A transport's own permanent verdict (SFTP: no such file,
            # permission denied) is not a transient error. It used to escape
            # this loop entirely: the reporter never saw ``task_finished`` and
            # ``on_error`` never fired, yet the task counted as failed.
            message = _safe_error(task, exc)
            console.error(f"{message} (not retryable)")
            return _failed(ctx, task, reporter, FAILURE_PERMANENT, message)
        except ContentChangedError as exc:
            if exc.validator == validator or await _witness_changed_again(ctx, task, exc.validator):
                # The server either rejects the witness it just reported, or
                # renames the object on every response. Neither can drive a
                # conditional request, so stop tracking a version for this
                # target instead of restarting forever (the declared size and
                # the per-piece window check still guard the file).
                witness_drops += 1
                if witness_drops > MAX_RESETS:
                    message = _safe_error(task, exc)
                    console.error(f"{message}; the version witness never settles, giving up")
                    return _failed(ctx, task, reporter, FAILURE_PERMANENT, message)
                console.warn(
                    f"  {display_url(task.url, bool(task.meta.get('redact_url')))}: "
                    "the server changes its version witness on every request; "
                    "continuing without one"
                )
                # The bytes on disk are a mixture of two revisions, so nothing
                # may be resumed. No destruction is needed here: the transfer
                # already abandoned its ledger when it noticed, and dropping the
                # witness makes the next ``resolve`` refuse that ledger too.
                observed = ""
                validator = ""
                task.meta["validator"] = ""
                task.meta["if_range"] = ""
                task.meta["witness_kind"] = "unstable"
                _notify_restarted(reporter, task)
                continue
            resets_done += 1
            if resets_done > MAX_RESETS:
                message = _safe_error(task, exc)
                console.error(f"{message}; giving up after {resets_done} restart(s)")
                return _failed(ctx, task, reporter, FAILURE_PERMANENT, message)
            console.warn(f"  {_safe_error(task, exc)}; restarting from the new revision")
            # The completed pieces belong to the revision the ledger names, and
            # the retry's ``resolve`` will refuse that ledger against the new
            # witness and pin it to zero. Truncating here instead would destroy
            # a complete file before its replacement had been fetched at all.
            observed = exc.validator
            validator = observed
            # Unconditional: a server that dropped its witness would otherwise
            # keep receiving the *previous* revision's If-Range and restart once
            # more for nothing.
            task.meta["if_range"] = exc.if_range
            task.meta["validator"] = observed
            _notify_restarted(reporter, task)
            continue
        except RangeSizeMismatchError as exc:
            resets_done += 1
            if resets_done > MAX_RESETS:
                message = _safe_error(task, exc)
                console.error(f"{message}; giving up after {resets_done} restart(s)")
                return _failed(ctx, task, reporter, FAILURE_PERMANENT, message)
            console.warn(f"  {_safe_error(task, exc)}; re-probing and restarting")
            # The size we probed is stale, exactly as for a 416: no amount of
            # retrying the same window can make the numbers agree, so the retry
            # has to be based on a fresh probe.
            await _reprobe(ctx, task)
            observed = _observed_validator(task)
            validator = observed or ""
            _notify_restarted(reporter, task)
            continue
        except RangeNotSatisfiableError as exc:
            resets_done += 1
            if resets_done > MAX_RESETS:
                message = _safe_error(task, exc)
                console.error(f"{message}; giving up after {resets_done} restart(s)")
                return _failed(ctx, task, reporter, FAILURE_PERMANENT, message)
            console.warn(f"  {_safe_error(task, exc)}; re-probing and restarting")
            # 416 says our ledger describes a larger object than the server now
            # has; the re-probe below supplies the size that ``resolve`` needs
            # to refuse it.
            await _reprobe(ctx, task)
            observed = _observed_validator(task)
            validator = observed or ""
            _notify_restarted(reporter, task)
            continue
        except ChecksumError as exc:
            console.error(str(exc))
            return _failed(ctx, task, reporter, FAILURE_PERMANENT, str(exc))
        except FutureStateError as exc:
            console.error(str(exc))
            return _failed(ctx, task, reporter, FAILURE_PERMANENT, str(exc))
        except DiskSpaceError as exc:
            console.error(str(exc))
            return _failed(ctx, task, reporter, FAILURE_PERMANENT, str(exc))
        except TransientHTTPError as exc:
            # The server told us how long to wait; honour it instead of
            # guessing with the exponential backoff.
            retry_after = exc.retry_after
            last_error = _safe_error(task, exc)
            console.warn(f"transient error: {last_error}; retrying")
            result = TransferResult(complete=False)
            verified = True
        except TransportError as exc:
            last_error = _safe_error(task, exc)
            console.warn(f"transient error: {last_error}; retrying")
            result = TransferResult(complete=False)
            verified = True
        except OSError as exc:
            # Permission denied, target is a directory, ENOSPC, ... are not
            # worth retrying forever; fail the file instead. Classified as
            # ``io`` (not ``permanent``): the network is fine, the destination
            # is not, and a different destination may work.
            console.error(f"cannot write {target}: {exc}")
            return _failed(ctx, task, reporter, FAILURE_IO, str(exc))
        except DownctlError as exc:
            # The remaining ``DownctlError`` subclasses (an ``AuthError`` from
            # the credential stage, a piece-count refusal, ...) can also end a
            # task. Without this clause they escaped the loop: the reporter
            # never saw ``task_finished`` and ``on_error`` never fired, so a
            # JSON consumer saw a run in which the file neither succeeded nor
            # failed. ``classify_failure`` owns the class; ``_safe_error``
            # redacts the message.
            message = _safe_error(task, exc)
            console.error(f"{message} (not retryable)")
            return _failed(ctx, task, reporter, classify_failure(exc), message)

        if result.complete and not result.witness_bound and result.fetched:
            # A server may omit the ETag on every 206 (RFC 9110 does not require
            # it). The bytes are kept because rejecting them broke resume
            # against such servers, but nothing ties them to a revision: no
            # witness is stamped, and a strict run refuses to call that proven.
            if require_witness and not task.checksum_hex:
                message = (
                    "the server omitted the version witness on range responses; "
                    "--require-witness cannot prove this file"
                )
                console.error(message)
                return _failed(ctx, task, reporter, FAILURE_PERMANENT, message)
            if not task.checksum_hex and not task.meta.get("unbound_warned"):
                task.meta["unbound_warned"] = True
                console.warn(
                    f"  {target}: the server omitted the version witness on range "
                    "responses; the completed file cannot be tied to a revision"
                )

        if result.complete and verified:
            marker = piece_store.load_completion(target)
            stored_validator, stored_algo, stored_hex = _recorded_proof(marker)
            # ``witness_bound`` is what separates "this run fetched the object"
            # from "this run found it already there": ``resolve`` can return an
            # all-ones grid from a marker whose recorded witness was never
            # compared, and then not one request is made. Stamping the witness
            # the probe just returned would certify content this run never
            # looked at, and ``--skip-existing --require-witness`` would report
            # it as proven from then on.
            if result.witness_bound:
                claimed = _fresh_witness(task, validator)
            elif not result.fetched:
                # Nothing was fetched: the completion rests on the marker, so
                # the only witness this run may name is the one it actually
                # compared. Saying "none" here starved every evidence hook of a
                # fact the marker already held, and made a re-run of unchanged
                # content look less proven than the run that produced it.
                claimed = _carried_witness(marker, observed) if marker else "none"
            else:
                claimed = "none"
            if not piece_store.save_completion(
                target,
                identity,
                task.checksum_algo or stored_algo,
                task.checksum_hex or stored_hex,
                validator=validator if result.witness_bound else stored_validator,
            ):
                console.verbose(f"could not write completion marker for {target}")
            task.meta["witness"] = claimed
            if reporter is not None:
                reporter.task_finished(task, True)
            _emit(
                ctx,
                "on_task_finish",
                {
                    "task": task,
                    "ok": True,
                    "witness": task.meta["witness"],
                    "job": ctx.job,
                },
            )
            return True

        if result.complete and not verified:
            checksum_failures += 1
            if checksum_failures >= CHECKSUM_FAIL_MAX:
                console.error(f"checksum failed {checksum_failures} times; giving up")
                _discard(target)
                return _failed(ctx, task, reporter, FAILURE_PERMANENT, "checksum")
            console.warn(f"checksum mismatch; re-downloading ({checksum_failures})")
            _discard(target)
            _notify_restarted(reporter, task)
            last_error = "checksum mismatch"
            result = TransferResult(complete=False)

        retries_done += 1
        if max_retries > 0 and retries_done > max_retries:
            console.warn(f"giving up after {retries_done} retries")
            return _failed(ctx, task, reporter, FAILURE_TRANSIENT_EXHAUSTED, "max retries")
        delay = retry_delay(retries_done, retry_after)
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - loop.time()))
        _notify_retrying(reporter, task, retries_done, delay, last_error)
        await asyncio.sleep(delay)


def _fresh_witness(task: DownloadTask, validator: str) -> str:
    """The witness a just-finished download can honestly claim."""
    if validator:
        return "validator"
    return "checksum" if task.checksum_hex else "none"


def _target_deadline(
    task: DownloadTask, max_time: int, loop: asyncio.AbstractEventLoop
) -> float | None:
    """When this target must be finished, shared with its probe.

    ``--max-time`` caps the *target*, not each phase. A probe that spends the
    whole budget and then hands the transfer a fresh one lets one target take
    twice what the user asked for (measured before this fix: ``--max-time 3``
    exited after 11.2s because the probe stage ignored the cap entirely). The
    app records the deadline on the task before probing, so whatever the probe
    leaves is what the transfer gets.
    """
    stored = task.meta.get("deadline")
    if isinstance(stored, float):
        return stored
    if max_time <= 0:
        return None
    return loop.time() + max_time


def _remaining_time(task: DownloadTask) -> float | None:
    """Seconds left of this target's ``--max-time`` budget, or ``None``."""
    stored = task.meta.get("deadline")
    if not isinstance(stored, float):
        return None
    return max(0.0, stored - asyncio.get_running_loop().time())


def _give_up_timeout(
    ctx: RuntimeContext,
    task: DownloadTask,
    target: str,
    max_time: int,
    reporter: Reporter | None,
) -> bool:
    """Stop a target that exceeded ``--max-time``."""
    message = f"{target} exceeded --max-time {max_time}s"
    console.warn(message)
    return _failed(ctx, task, reporter, FAILURE_TIMEOUT, message)


async def _witness_changed_again(ctx: RuntimeContext, task: DownloadTask, observed: str) -> bool:
    """Whether the server names yet another version than ``observed``.

    Called only when a transfer reports a content change: a witness that moves
    twice in a row is unstable (per-response ETags, a load balancer with
    per-node tags), and using it for ``If-Range`` would restart forever. A
    server that cannot be reached on this extra probe is given the benefit of
    the doubt, so a network blip can never silently disable change detection.
    """
    if not task.url.startswith(("http://", "https://")):
        return False
    try:
        result = await probe_metadata(
            ctx.session, task.url, headers=task.headers, policy=ctx.proxy_policy
        )
    except Exception:  # noqa: BLE001 - an inconclusive probe must not decide
        return False
    if not result.reached:
        return False
    return result.validator != observed


def refresh_url(task: DownloadTask, status: int) -> bool:
    """Ask the source for a fresh URL when a presigned link has expired.

    A presigned URL carries its own expiry, and a download that outlives it
    starts receiving ``403`` for the ranges that are still missing. ``403`` is
    classified permanent, so without this the transfer gave up half way with a
    "forbidden" that looked like a permissions problem. Only ``403`` is renewed:
    ``401``/``404`` are about the object, not the signature.

    Public because the *probe* needs it too: a link that expires between source
    resolution and the first HEAD failed the whole target before the transfer —
    and therefore before the renewal — ever ran.

    The source opts in by putting a zero-argument callable in
    ``task.meta["refetch"]``; a task without one behaves exactly as before.
    """
    if status != 403:
        return False
    refetch = task.meta.get("refetch")
    if not callable(refetch):
        return False
    used = int(task.meta.get("refetched", 0) or 0)
    if used >= REFETCH_MAX:
        return False
    try:
        fresh = str(refetch())
    except Exception as error:  # noqa: BLE001 - a broken signer must not crash the run
        console.warn(f"could not re-sign the expired URL: {redact_text(str(error))}")
        return False
    if not fresh or fresh == task.url:
        return False
    task.meta["refetched"] = used + 1
    console.verbose("the presigned URL expired; it has been re-signed and the request resumes")
    task.url = fresh
    # The new signature is a different query string; a ledger sealed against
    # the old one would refuse to resume (or, worse, be accepted by a source
    # that redacts the query and describe another object). Re-sealing keeps
    # "which object is this?" one answer even after the URL moves.
    seal_identity(task)
    return True


async def _reprobe(ctx: RuntimeContext, task: DownloadTask) -> None:
    """Re-learn an HTTP task's size and version after the remote moved.

    Used after a ``416``: our size or offset no longer describes the object, so
    the retry must be based on fresh evidence rather than the same numbers.
    """
    if not task.url.startswith(("http://", "https://")):
        return
    try:
        async with asyncio.timeout(_remaining_time(task)):
            result = await probe_metadata(
                ctx.session, task.url, headers=task.headers, policy=ctx.proxy_policy
            )
    except TimeoutError:
        # Out of budget: the retry loop re-reads the deadline and gives up with
        # a timeout report rather than starting another unbounded probe.
        return
    if not result.reached:
        return
    if result.size > 0:
        task.size = result.size
    task.meta["validator"] = result.validator
    task.meta["if_range"] = result.if_range
    task.meta["witness_kind"] = result.validator_kind


async def _verify(ctx: RuntimeContext, task: DownloadTask, target: str) -> bool:
    if not task.checksum_hex or not task.checksum_algo:
        return True
    verifier = _get_verifier(ctx, task.checksum_algo)
    if verifier is None:
        # Retrying cannot conjure a verifier; fail immediately instead of
        # re-downloading the file twice before giving up.
        raise ChecksumError(f"no verifier for algorithm {task.checksum_algo!r}")
    try:
        return await verifier.verify(target, task.checksum_hex)
    except (OSError, ValueError) as exc:
        raise ChecksumError(f"cannot verify {target}: {exc}") from exc


def _discard(target: str, *, force: bool = False) -> bool:
    """Drop a target completely: data bytes, resume ledger and marker.

    The bytes are truncated *before* the unlink attempt so a failed removal
    cannot leave a full-size, hole-free file for the no-state heuristic to
    bless later.

    Returns ``True`` once the target is gone. Raises
    :class:`~downctl.core.errors.FutureStateError` without touching anything
    when the target's state came from a newer build and ``force`` is not set:
    the data file is removed only after :func:`downctl.core.pieces.discard`
    agreed to the reset, because removing it anyway would destroy the bytes the
    refused ledger describes.
    """
    piece_store.discard(target, force=force)
    with contextlib.suppress(OSError):
        os.remove(target)
    return True


async def download_all(
    ctx: RuntimeContext,
    tasks: Sequence[DownloadTask],
    *,
    jobs: int = 1,
    reporter: Reporter | None = None,
    connections: int = DEFAULT_CONNECTIONS,
    stall_limit: int = DEFAULT_STALL_LIMIT,
    piece_size: int = 0,
    min_split_size: int = 0,
    checkpoint: int = 0,
    force: bool = False,
    skip_existing: bool = False,
    on_result: Callable[[DownloadTask, bool], None] | None = None,
    extra_failed: int = 0,
) -> tuple[int, int]:
    """Download many tasks with a concurrency limit. Returns ``(ok, failed)``.

    ``extra_failed`` counts targets that already failed before the engine ran
    (resolve/probe), so the reporter's ``run_finish`` reflects the whole run.

    Before a byte is written the whole batch is budgeted against the disks it
    will land on (:func:`downctl.core.disk.batch_budget`): targets that cannot
    fit are failed immediately with one aggregated message naming the total need
    and the free space. Concurrency is deliberately not adjusted — bytes already
    written stay on the disk, so fewer files at a time would not make a batch
    fit, only delay the same failure.
    """
    _emit(
        ctx,
        "on_start",
        {
            "targets": [display_url(task.url, bool(task.meta.get("redact_url"))) for task in tasks],
            "job": ctx.job,
        },
    )
    started_at = time.monotonic()
    ok = failed = 0

    entries: list[tuple[str, int]] = []
    for task in tasks:
        entry_target = str(task.meta.get("path") or "")
        if not entry_target:
            try:
                entry_target = _resolve_target(ctx, task)
                task.meta["path"] = entry_target
            except DownctlError:
                entry_target = ""
            except OSError:
                # A target whose parent cannot be created (permissions, a file
                # in the way) must fail on its own, not abort the budget pass
                # for every other task in the batch. The worker re-raises it
                # and records the failure per task.
                entry_target = ""
        needs = (
            _remaining_bytes(
                entry_target,
                task,
                connections,
                piece_size,
                _task_identity(task),
                validator=_observed_validator(task),
                require_witness=_task_require_witness(ctx, task),
            )
            if entry_target
            else 0
        )
        entries.append((entry_target or task.target, needs))

    budget = disk.batch_budget(entries)
    blocked = [index for index, fits in enumerate(budget.fits) if not fits]
    runnable = [task for index, task in enumerate(tasks) if index not in blocked]
    if blocked:
        failed += len(blocked)

    call_optional(
        reporter,
        "run_started",
        len(runnable),
        sum(max(0, task.size) for task in runnable),
        jobs,
        job=ctx.job,
        connections=connections,
        unknown=sum(1 for task in runnable if task.size <= 0),
    )

    if blocked:
        console.error(budget.message or "not enough free space for this batch")
        for index in blocked:
            task = tasks[index]
            console.verbose(f"  skipping {task.target}: does not fit on disk")
            # A task refused before it starts is still a failed task: it must
            # carry a failure class (the failed log and the JSON stream read
            # it), notify the reporter and emit ``on_error``. It used to only
            # reach ``on_result``, so a hook and a JSON consumer saw a run in
            # which the file neither succeeded nor failed. Reported *after*
            # ``run_started`` and with a matching ``task_started``, or a JSON
            # consumer saw a ``finish`` before the run it belongs to began.
            if reporter is not None:
                started = getattr(reporter, "task_started", None)
                if callable(started):
                    started(task)
            _failed(
                ctx,
                task,
                reporter,
                FAILURE_PERMANENT,
                budget.message or "not enough free space for this batch",
            )
            if on_result is not None:
                on_result(task, False)

    # Report already-complete bytes up front so an overall bar reflects resume
    # progress instead of assuming all bytes are still to fetch.
    if reporter is not None and not force:
        done_total = 0
        for task in runnable:
            target = task.meta.get("path")
            if not target:
                try:
                    target = _resolve_target(ctx, task)
                except DownctlError:
                    continue
            try:
                done_total += _completed_bytes(
                    str(target),
                    task,
                    connections,
                    piece_size,
                    _task_identity(task),
                    validator=_observed_validator(task),
                    # The run-level seed must obey the same witness rule as
                    # the per-task path below. Without this, --require-witness
                    # seeded the bar with bytes the engine would re-download.
                    require_witness=_task_require_witness(ctx, task),
                )
            except DownctlError:
                # A refusal here (state from a newer build) is reported by
                # the task itself; the progress estimate must not abort the
                # batch over it.
                continue
        if done_total > 0:
            call_optional(reporter, "run_resumed", done_total)

    def _kwargs() -> dict[str, object]:
        return {
            "reporter": reporter,
            "connections": connections,
            "stall_limit": stall_limit,
            "piece_size": piece_size,
            "min_split_size": min_split_size,
            "checkpoint": checkpoint,
            "force": force,
            "skip_existing": skip_existing,
        }

    if jobs <= 1:
        for task in runnable:
            try:
                success = await download_task(ctx, task, **_kwargs())  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 - one file must not abort the batch
                console.error(f"task failed: {redact_text(str(exc))}")
                task.meta.setdefault("failure", FAILURE_PERMANENT)
                success = False
            ok += success
            failed += not success
            if on_result is not None:
                on_result(task, success)
    else:
        semaphore = asyncio.Semaphore(jobs)

        async def worker(task: DownloadTask) -> bool:
            async with semaphore:
                try:
                    return await download_task(ctx, task, **_kwargs())  # type: ignore[arg-type]
                except Exception as exc:  # noqa: BLE001
                    console.error(f"task failed: {redact_text(str(exc))}")
                    task.meta.setdefault("failure", FAILURE_PERMANENT)
                    return False

        results = await asyncio.gather(*(worker(task) for task in runnable), return_exceptions=True)
        for task, outcome in zip(runnable, results, strict=False):
            if isinstance(outcome, BaseException):
                console.error(f"task failed: {redact_text(str(outcome))}")
                success = False
            else:
                success = outcome
            ok += success
            failed += not success
            if on_result is not None:
                on_result(task, bool(success))

    if ctx.events is not None:
        # ``on_finish`` reports the failure count, so the work behind it has to
        # be finished first; otherwise the event always says zero. The bus owns
        # the number (it drains what it knows about and returns the total); the
        # engine puts it in the payload instead of letting emit guess.
        hooks_failed = await ctx.events.finalize()
    else:
        hooks_failed = 0
    # A single ``reason="permanent"`` series claimed every failure had the same
    # cause. Report the classes the engine actually assigned, plus the
    # remainder (resolve/probe failures that never became tasks) as
    # ``unresolved`` so the reason series still add up to the failed count.
    failures_by_reason: dict[str, int] = {}
    for task in tasks:
        reason = str(task.meta.get("failure") or "")
        if reason:
            failures_by_reason[reason] = failures_by_reason.get(reason, 0) + 1
    unresolved = (failed + extra_failed) - sum(failures_by_reason.values())
    if unresolved > 0:
        failures_by_reason[FAILURE_UNKNOWN] = (
            failures_by_reason.get(FAILURE_UNKNOWN, 0) + unresolved
        )
    elapsed = time.monotonic() - started_at
    _emit(
        ctx,
        "on_finish",
        {
            "succeeded": ok,
            "failed": failed + extra_failed,
            "hooks_failed": hooks_failed,
            "bytes": ctx.stats.bytes_transferred,
            "elapsed": elapsed,
            "job": ctx.job,
            "failures_by_reason": failures_by_reason,
        },
    )
    if ctx.events is not None:
        # Hooks may spawn work *from* ``on_finish`` (manifest/notify do). The
        # count put in that event cannot include it — the event triggers it —
        # but ``run_finish`` comes after and is read as the final number. Drain
        # again so a failure there is not reported as ``hooks_failed=0`` while
        # ``strict_hooks`` exits 1.
        hooks_failed = await ctx.events.finalize()
    call_optional(
        reporter,
        "run_finished",
        ok,
        failed + extra_failed,
        job=ctx.job,
        hooks_failed=hooks_failed,
    )
    return ok, failed + extra_failed
