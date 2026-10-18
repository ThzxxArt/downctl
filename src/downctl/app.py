"""Application orchestration for downctl.

Each ``cmd_*`` function maps to a CLI subcommand and receives the parsed
``argparse.Namespace``. They return a process exit code.

The download flow is: build config -> discover plugins -> resolve each target
into tasks via a source plugin -> probe sizes -> run the engine.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from urllib.parse import unquote, urlsplit

import aiohttp

from downctl import __version__, console
from downctl.config import Config, config_path, load_config, write_default_config
from downctl.core import pieces as piece_store
from downctl.core.context import RuntimeContext
from downctl.core.engine import _completed_bytes, download_all, retry_delay
from downctl.core.errors import DownctlError, PermanentHTTPError, ProbeError
from downctl.core.ratelimit import RateLimiter
from downctl.core.task import DownloadTask
from downctl.platform_utils import (
    detect_wsl_proxy,
    human_age,
    human_size,
    interrupt_on_sigterm,
    parse_rate,
    pid_alive,
    platform_kind,
    stdin_is_tty,
    stdout_is_tty,
    win_to_wsl_path,
)
from downctl.plugin.hooks import EventBus
from downctl.plugin.loader import discover
from downctl.plugin.registry import Registry
from downctl.probe import probe_metadata, probe_resource
from downctl.session import make_session
from downctl.urls import display_url, mask_mapping, redact_text, redact_url, url_identity

DEFAULT_DIR = str(Path.home() / "Downloads")
FAILED_LOG_NAME = ".downctl.failed"
STALL_LIMIT = 30
#: The probe stage never retries forever, and never backs off like a download:
#: unlike a transfer there is no partial progress to preserve, and a batch of
#: dead targets must not turn one HEAD request each into minutes of waiting.
#: ``-r 0`` ("retry the download forever") therefore still bounds probing here.
PROBE_ATTEMPT_LIMIT = 3
PROBE_BACKOFF_MAX = 5.0
#: Digest lengths for the algorithms the built-in verifiers know about.
DIGEST_LENGTHS = {"sha256": 64, "sha1": 40, "sha512": 128, "md5": 32}
#: Infer the algorithm of an unprefixed ``*sum`` line from its digest length.
_ALGO_BY_LENGTH = {length: algo for algo, length in DIGEST_LENGTHS.items()}
_HTTP_SCHEMES = ("http://", "https://")
#: Probe statuses that are the server's final answer, never retried.
_PERMANENT_PROBE_STATUSES = (401, 403, 404)
_HEX_RE = re.compile(r"[0-9a-fA-F]+")

ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")


@dataclass
class Incomplete:
    """A partially downloaded file discovered by ``downctl status``."""

    target: str
    done: int
    pieces: int
    size: int


@dataclass
class VerifyEntry:
    """One file whose completion was claimed, and what ``verify`` found."""

    target: str
    #: ``ok`` | ``mismatch`` | ``unverifiable`` | ``missing``
    status: str
    #: ``validator`` | ``checksum`` | ``none``
    witness: str
    algo: str = ""
    expected: str = ""
    actual: str = ""
    detail: str = ""
    repaired: bool = False

    def to_json(self) -> dict[str, object]:
        record: dict[str, object] = {
            "target": self.target,
            "status": self.status,
            "witness": self.witness,
        }
        if self.algo:
            record["algo"] = self.algo
        if self.expected:
            record["expected"] = self.expected
        if self.actual:
            record["actual"] = self.actual
        if self.detail:
            record["detail"] = self.detail
        if self.repaired:
            record["repaired"] = True
        return record


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _errors(message: str) -> None:
    console.error(message)


def _warn(message: str) -> None:
    console.warn(message)


def _checked_int(flag: str, value: object, minimum: int) -> int | None:
    """Validate an integer command-line override before it reaches the engine."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise DownctlError(f"{flag} must be an integer, got {value!r}")
    if value < minimum:
        raise DownctlError(f"{flag} must be >= {minimum}, got {value}")
    return value


def _checked_size(flag: str, value: str, minimum: int) -> int:
    """Parse a human size (``8M``) with a lower bound and clean errors."""
    try:
        parsed = parse_rate(value)
    except ValueError as exc:
        raise DownctlError(f"{flag}: {exc}") from exc
    if parsed is None:
        raise DownctlError(f"{flag} requires a size, e.g. 8M")
    size = int(parsed)
    if size < minimum:
        raise DownctlError(f"{flag} must be >= {minimum} bytes, got {size}")
    return size


def _checked_rate(value: str) -> float:
    try:
        parsed = parse_rate(value)
    except ValueError as exc:
        raise DownctlError(f"--limit-rate: {exc}") from exc
    if parsed is None or parsed <= 0:
        raise DownctlError(f"--limit-rate must be > 0, got {value!r}")
    return parsed


def _build_config(args: argparse.Namespace) -> Config:
    """Merge the config file with command-line overrides."""
    cfg = load_config()

    if getattr(args, "dir", None):
        cfg.core.dir = args.dir
    jobs = _checked_int("--jobs", getattr(args, "jobs", None), 1)
    if jobs is not None:
        cfg.core.jobs = jobs
    connections = _checked_int("--connections", getattr(args, "connections", None), 1)
    if connections is not None:
        cfg.core.connections = connections
    stall = _checked_int("--stall", getattr(args, "stall", None), 1)
    if stall is not None:
        cfg.core.stall = stall
    checkpoint = _checked_int("--checkpoint", getattr(args, "checkpoint", None), 0)
    if checkpoint is not None:
        cfg.core.checkpoint = checkpoint
    max_time = _checked_int("--max-time", getattr(args, "max_time", None), 0)
    if max_time is not None:
        cfg.core.max_time = max_time
    probe_jobs = _checked_int("--probe-jobs", getattr(args, "probe_jobs", None), 0)
    if probe_jobs is not None:
        cfg.core.probe_jobs = probe_jobs
    if getattr(args, "require_witness", False):
        cfg.core.require_witness = True
    if getattr(args, "piece_size", None):
        cfg.core.piece_size = _checked_size(
            "--piece-size", args.piece_size, piece_store.MIN_PIECE_SIZE
        )
    if getattr(args, "min_split_size", None):
        cfg.core.min_split_size = _checked_size("--min-split-size", args.min_split_size, 0)
    retries = _checked_int("--retries", getattr(args, "retries", None), 0)
    if retries is not None:
        cfg.core.retries = retries
    if getattr(args, "proxy", None) is not None:
        cfg.core.proxy = args.proxy
    if getattr(args, "limit_rate", None):
        cfg.core.rate = _checked_rate(args.limit_rate)
    if getattr(args, "mirror", None):
        source, _, dest = args.mirror.partition("=")
        if not dest:
            raise DownctlError("--mirror must be FROM=TO, e.g. https://a=https://b")
        cfg.core.mirror[source] = dest

    if not cfg.core.dir:
        cfg.core.dir = DEFAULT_DIR
    cfg.core.dir = _local_path(os.path.expanduser(cfg.core.dir))

    if not cfg.core.proxy:
        cfg.core.proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
            or ""
        )
    cfg.core.proxy = detect_wsl_proxy(cfg.core.proxy)
    console.verbose(
        f"config dir={cfg.core.dir} jobs={cfg.core.jobs} connections={cfg.core.connections} "
        f"stall={cfg.core.stall} checkpoint={cfg.core.checkpoint} "
        f"max_time={cfg.core.max_time or 'none'} probe_jobs={cfg.core.probe_jobs} "
        f"require_witness={cfg.core.require_witness} "
        f"piece_size={cfg.core.piece_size or 'auto'} "
        f"rate={cfg.core.rate} proxy={display_url(cfg.core.proxy) or '(none)'}"
    )
    return cfg


def _probe_jobs(cfg: Config) -> int:
    """Concurrency for the resolve/probe phase (``probe_jobs=0`` follows jobs)."""
    if cfg.core.probe_jobs > 0:
        return cfg.core.probe_jobs
    return max(1, cfg.core.jobs)


def _local_path(path: str) -> str:
    """Translate a Windows path when running under WSL (``D:\\x`` -> ``/mnt/d/x``)."""
    if not path:
        return path
    if platform_kind() != "WSL":
        return path
    try:
        return win_to_wsl_path(path)
    except ValueError as exc:
        raise DownctlError(str(exc)) from exc


def _registry(include_local: bool) -> Registry:
    return discover(include_local=include_local)


def _select_reporter(args: argparse.Namespace, cfg: Config, registry: Registry) -> object | None:
    name = getattr(args, "reporter", None)
    if not name and console.is_quiet():
        name = "quiet"
    if not name:
        use_bar = stdout_is_tty() and os.environ.get("NO_COLOR") != "1"
        tqdm_enabled = bool(cfg.plugins.get("tqdm", {}).get("enabled", True))
        name = "tqdm" if (use_bar and tqdm_enabled) else "quiet"
    reporter = registry.get("reporter", name)
    if reporter is None:
        raise DownctlError(f"unknown reporter plugin: {name}")
    console.set_machine_readable(bool(getattr(reporter, "machine_readable", False)))
    return reporter


def _enable_hooks(
    args: argparse.Namespace, cfg: Config, registry: Registry, events: EventBus
) -> list[str]:
    """Register configured hook plugins on the event bus."""
    names = list(cfg.core.hooks)
    for extra in getattr(args, "hook", []) or []:
        if extra not in names:
            names.append(extra)

    enabled: list[str] = []
    for name in names:
        hook = registry.get("hook", name)
        if hook is None:
            _warn(f"unknown hook plugin: {name}")
            continue
        options = cfg.plugins.get(name, {})
        for key, value in options.items():
            if hasattr(hook, key):
                setattr(hook, key, value)
        events.register_hook(hook)  # type: ignore[arg-type]
        enabled.append(name)
    return enabled


def _seal_identity(task: DownloadTask) -> None:
    """Bind resume state to the URL that will actually be fetched.

    Called again after every router rewrite, so switching a mirror is a
    different content identity and cannot resume another mirror's pieces.
    A source-supplied identity (local file size+mtime) is combined with the
    final URL instead of being replaced.
    """
    redact = bool(task.meta.get("redact_url"))
    final = url_identity(task.url, redact=redact)
    source = str(task.meta.get("source_identity") or "")
    if source:
        task.meta["identity"] = hashlib.sha256(f"{source}\0{final}".encode()).hexdigest()
    else:
        task.meta["identity"] = final


def _apply_router(task: DownloadTask, cfg: Config, registry: Registry) -> None:
    if cfg.core.mirror:
        router = registry.get("router", "mirror")
        if router is not None:
            router.mappings = dict(cfg.core.mirror)  # type: ignore[attr-defined]
            before = task.url
            task.url = router.rewrite(task.url)  # type: ignore[attr-defined]
            if task.url != before:
                redact = bool(task.meta.get("redact_url"))
                console.verbose(
                    f"mirror: {display_url(before, redact)} -> {display_url(task.url, redact)}"
                )
    _seal_identity(task)


def _display_url(task: DownloadTask) -> str:
    """URL for logs; masks credentials and strips a presigned query."""
    return display_url(task.url, bool(task.meta.get("redact_url")))


def _persisted_url(task: DownloadTask) -> str:
    """URL stored in the failed log so a later ``--retry-failed`` can use it.

    For presigned URLs the query is dropped (it expires anyway; the origin is
    re-resolved). For ordinary URLs the full URL is kept, because masking a
    token in the *state file* would make retrying impossible. The file is
    written with owner-only permissions where the platform allows it.
    """
    return redact_url(task.url, strip_query=True) if task.meta.get("redact_url") else task.url


def _tsv(value: object) -> str:
    """Make a value safe for the tab-separated failed log."""
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _fmt_url(task: DownloadTask) -> str:
    """Failed-log v3 line for a resolved task (URL, target, checksum, origin)."""
    return "\t".join(
        _tsv(value)
        for value in (
            "url",
            _persisted_url(task),
            task.target,
            task.checksum_algo,
            task.checksum_hex,
            str(task.size),
            task.source,
            task.origin,
        )
    )


def _fmt_target(target: str) -> str:
    """Failed-log line for a target that could not be resolved."""
    return f"target\t{_tsv(target)}"


def _report_early_failures(reporter: object, lines: list[str], error: str) -> None:
    """Emit per-target JSON failure records for resolve/probe failures."""
    failure = getattr(reporter, "failure", None)
    if not callable(failure):
        return
    for line in lines:
        kind, payload = _parse_failed_line(line)
        if kind == "url" and isinstance(payload, dict):
            target = str(payload.get("target") or payload.get("url") or "")
            url = str(payload.get("url") or "")
        else:
            target = str(payload)
            url = target
        failure(target, error, url)


def _parse_failed_line(line: str) -> tuple[str, object]:
    """Parse a failed-log line. Returns ``("url", kwargs)`` or ``("target", str)``.

    v1 was a bare target, v2 added six tab-separated fields and v3 appends the
    user-supplied ``origin`` (so S3/presigned and private repos can be
    re-resolved instead of replaying an expired URL). All versions are read.
    """
    columns = line.split("\t")
    if len(columns) >= 6 and columns[0] == "url":
        try:
            size = int(columns[5] or 0)
        except ValueError:
            size = 0
        return "url", {
            "url": columns[1],
            "target": columns[2],
            "checksum_algo": columns[3],
            "checksum_hex": columns[4],
            "size": size,
            "source": columns[6] if len(columns) > 6 else "",
            "origin": columns[7] if len(columns) > 7 else "",
        }
    if len(columns) >= 2 and columns[0] == "target":
        return "target", columns[1]
    return "target", columns[0]  # legacy single-column format: a plain URL/target


def _prepare_task(task: DownloadTask, origin: str) -> None:
    """Attach the origin and prepare the content identity for sealing.

    Sources may supply their own identity (e.g. the local-file source binds the
    source path, size and mtime). It is kept as ``source_identity`` and combined
    with the *final* URL after mirror routing in :func:`_apply_router`, so a
    mirror switch cannot reuse another mirror's resume state.
    """
    task.origin = origin
    if not task.meta.get("source_identity"):
        task.meta["source_identity"] = str(task.meta.get("identity") or "")
    if not task.meta.get("remote_name"):
        # Remember the remote name *before* -o/--output renames the target, so
        # a --checksums manifest keyed by the original filename still matches.
        task.meta["remote_name"] = task.target
    task.meta.pop("identity", None)
    _seal_identity(task)


def _match_resolved(
    tasks: list[DownloadTask],
    target: str,
    url: str = "",
    *,
    allow_basename: bool = False,
) -> DownloadTask | None:
    """Find the re-resolved task that corresponds to a failed-log record.

    Exact target first. A basename/URL-basename fallback is only allowed when
    the stored target was renamed by ``-o`` (``allow_basename``); for ordinary
    source-relative targets a same-named file in a different directory is a
    different object, so it is treated as no match rather than guessing.
    """
    normalized = target.replace("\\", "/")
    for task in tasks:
        if task.target.replace("\\", "/") == normalized:
            return task
    if not allow_basename:
        return None
    candidates: list[DownloadTask] = []
    basename = normalized.rsplit("/", 1)[-1]
    if basename:
        candidates = [
            task for task in tasks if task.target.replace("\\", "/").rsplit("/", 1)[-1] == basename
        ]
    if not candidates and url:
        url_name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        if url_name:
            candidates = [
                task
                for task in tasks
                if task.target.replace("\\", "/").rsplit("/", 1)[-1] == url_name
            ]
    return candidates[0] if len(candidates) == 1 else None


async def _retry_task(
    record: dict[str, object],
    args: argparse.Namespace,
    cfg: Config,
    ctx: RuntimeContext,
) -> DownloadTask:
    """Turn a failed-log record into a task, re-resolving its origin if possible."""
    assert ctx.registry is not None
    stored = DownloadTask(**record)  # type: ignore[arg-type]
    if stored.checksum_hex:
        stored.meta["user_checksum"] = True  # conservative: re-verify on skip
    if os.path.isabs(stored.target):
        stored.allow_absolute = True
    # Failed-log v1/v2 did not store the transport. Without an origin to
    # re-resolve, a local/SFTP record must not be handed to the HTTP transport.
    if not stored.transport:
        scheme = stored.url.partition(":")[0].lower()
        if scheme in ("file", "sftp"):
            stored.transport = scheme
    origin = stored.origin
    if not origin:
        if stored.source == "s3":
            _warn(
                "this S3 failure predates origin tracking and stores a redacted, "
                "expired presigned URL; re-run the original s3:// target instead"
            )
        elif stored.source not in ("", "url", "file"):
            _warn(
                "this failure predates origin tracking; the stored URL may lack "
                "refreshed credentials; re-run the original target if it fails"
            )
        _prepare_task(stored, "")
        _apply_router(stored, cfg, ctx.registry)
        return stored

    forced = getattr(args, "source", None)
    try:
        source = (
            ctx.registry.get("source", forced) if forced else ctx.registry.select_source(origin)
        )
        if source is None:
            raise DownctlError(f"no source plugin can handle: {origin}")
        resolved = await source.resolve(origin, ctx)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - fall back to the stored URL
        _warn(
            f"cannot re-resolve {display_url(origin)}: {redact_text(str(exc))}; "
            "trying the stored URL (may be expired)"
        )
        _prepare_task(stored, origin)
        _apply_router(stored, cfg, ctx.registry)
        return stored

    match = _match_resolved(
        resolved,
        stored.target,
        stored.url,
        allow_basename=stored.allow_absolute,
    )
    if match is None:
        _warn(
            f"re-resolved {display_url(origin)} but could not identify "
            f"{stored.target!r}; trying the stored URL (may be expired)"
        )
        _prepare_task(stored, origin)
        _apply_router(stored, cfg, ctx.registry)
        return stored

    match.target = stored.target
    match.allow_absolute = stored.allow_absolute
    if stored.checksum_hex:
        # The stored checksum is what the failed run was asked to verify. It
        # may be a user-supplied --checksum/--checksums value, so it must win
        # over a source-provided digest from the re-resolution (otherwise the
        # user's integrity check is silently replaced). Re-verify on skip:
        # the failed log does not record whether the digest was user or source.
        match.checksum_algo, match.checksum_hex = stored.checksum_algo, stored.checksum_hex
        match.meta["user_checksum"] = True
    _prepare_task(match, origin)
    _apply_router(match, cfg, ctx.registry)
    return match


async def _resolve_targets(
    args: argparse.Namespace,
    cfg: Config,
    ctx: RuntimeContext,
    *,
    failures: list[str],
    keep_going: bool,
) -> list[DownloadTask]:
    assert ctx.registry is not None
    targets = list(getattr(args, "targets", []) or [])

    list_file = getattr(args, "list", None)
    if list_file:
        if list_file == "-":
            for line in sys.stdin:
                line = line.strip()
                if line and not line.startswith("#"):
                    targets.append(line)
        else:
            list_file = _local_path(list_file)
            if not os.path.isfile(list_file):
                raise DownctlError(f"target list file not found: {list_file}")
            with open(list_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        targets.append(line)

    tasks: list[DownloadTask] = []
    retry_records: list[dict[str, object]] = []
    if getattr(args, "retry_failed", False):
        failed_log = os.path.join(cfg.core.dir, FAILED_LOG_NAME)
        if not os.path.isfile(failed_log):
            raise DownctlError(f"no failed log to retry: {failed_log}")
        with open(failed_log, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                kind, payload = _parse_failed_line(line)
                if kind == "url" and isinstance(payload, dict):
                    retry_records.append(payload)
                elif isinstance(payload, str):
                    targets.append(payload)

    forced = getattr(args, "source", None)
    registry = ctx.registry

    async def resolve_one(target: str) -> tuple[str, list[DownloadTask]]:
        source = registry.get("source", forced) if forced else registry.select_source(target)
        if source is None:
            raise DownctlError(f"no source plugin can handle: {target}")
        resolved = await source.resolve(target, ctx)  # type: ignore[attr-defined]
        return str(getattr(source, "name", "")), list(resolved)

    # Expansion is I/O bound (one request per file for a repo listing, plus a
    # HEAD per target), so it runs with bounded concurrency instead of paying
    # N round trips before the first byte. Order is preserved for reporting.
    limit = _probe_jobs(cfg)
    resolved_items = await _bounded_map(targets, limit, resolve_one)
    for target, resolved_outcome in zip(targets, resolved_items, strict=False):
        if isinstance(resolved_outcome, BaseException):
            failures.append(_fmt_target(target))
            if not keep_going:
                raise resolved_outcome
            _warn(f"skipping {display_url(target)}: {redact_text(str(resolved_outcome))}")
            continue
        source_name, resolved = resolved_outcome
        for task in resolved:
            if not task.source:
                task.source = source_name
            _prepare_task(task, target)
            _apply_router(task, cfg, registry)
            tasks.append(task)

    async def retry_one(record: dict[str, object]) -> DownloadTask:
        return await _retry_task(record, args, cfg, ctx)

    retried = await _bounded_map(retry_records, limit, retry_one)
    for retry_outcome in retried:
        if isinstance(retry_outcome, BaseException):
            # ``_retry_task`` falls back to the stored URL instead of raising
            # for ordinary failures; anything reaching here is unexpected and
            # must not be swallowed.
            raise retry_outcome
        tasks.append(retry_outcome)

    for task in tasks:
        size = f"{task.size} bytes" if task.size > 0 else "(size probed later)"
        console.verbose(f"resolved [{task.source}] {_display_url(task)} -> {task.target} {size}")
    return tasks


def _load_checksums(path: str) -> dict[str, tuple[str, str]]:
    """Parse a checksum file (``sha256sum`` style) into ``name -> (algo, hex)``.

    Accepts ``<hex>  <name>``, ``<hex> *<name>`` and ``<algo>:<hex>  <name>``.
    Unprefixed digests are matched to md5/sha1/sha256/sha512 by their length,
    so ordinary ``md5sum``/``sha1sum``/``sha512sum`` files work too.
    """
    table: dict[str, tuple[str, str]] = {}
    if not os.path.isfile(path):
        raise DownctlError(f"checksum file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            digest, name = parts
            name = name.strip().lstrip("*").replace("\\", "/")
            if name.startswith("./"):
                name = name[2:]
            algo, sep, hex_value = digest.partition(":")
            if not sep:
                algo = _ALGO_BY_LENGTH.get(len(digest), "sha256")
                hex_value = digest
            if not hex_value or not _HEX_RE.fullmatch(hex_value):
                continue  # malformed digest; skip rather than mis-verify
            table[name] = (algo.lower(), hex_value.lower())
    return table


def _apply_checksums(tasks: list[DownloadTask], path: str) -> None:
    """Attach user checksums to every task, failing closed on a gap.

    An explicit ``--checksums`` manifest is an integrity request: silently
    downloading a target that has no entry would turn verification into a
    no-op, so a missing entry (including an absolute path in the manifest that
    cannot match a relative target) is an error.
    """
    table = _load_checksums(path)
    if not table:
        raise DownctlError(f"checksum file has no usable entries: {path}")
    missing: list[str] = []
    for task in tasks:
        rel = task.target.replace(os.sep, "/")
        candidates = [rel, os.path.basename(rel)]
        remote = str(task.meta.get("remote_name") or "").replace("\\", "/")
        if remote:
            candidates.extend([remote, os.path.basename(remote)])
        url_name = unquote(urlsplit(task.url).path.rsplit("/", 1)[-1])
        if url_name:
            candidates.append(url_name)
        entry = next((table[key] for key in candidates if key in table), None)
        if entry is None:
            missing.append(task.target)
            continue
        task.checksum_algo, task.checksum_hex = entry
        task.meta["user_checksum"] = True
    if missing:
        shown = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        raise DownctlError(f"checksum file has no entry for: {shown} ({path})")


def _validate_checksum_spec(
    algo: str, digest: str, registry: Registry, label: str
) -> tuple[str, str]:
    """Validate one ``ALGO:HEX`` pair against the verifier registry."""
    algo = (algo or "").lower()
    if not algo:
        raise DownctlError(f"checksum digest provided without an algorithm for {label}")
    if registry.get("verifier", algo) is None:
        raise DownctlError(
            f"unknown checksum algorithm {algo!r} for {label}; "
            f"available: {', '.join(registry.names('verifier'))}"
        )
    digest = (digest or "").lower()
    if not digest or not _HEX_RE.fullmatch(digest):
        raise DownctlError(f"invalid checksum digest for {label}: {digest!r}")
    expected = DIGEST_LENGTHS.get(algo)
    if expected is not None and len(digest) != expected:
        raise DownctlError(
            f"invalid {algo} digest length for {label}: "
            f"expected {expected} hex characters, got {len(digest)}"
        )
    return algo, digest


def _validate_checksums(tasks: list[DownloadTask], registry: Registry) -> None:
    """Reject unknown algorithms / malformed digests *before* downloading."""
    for task in tasks:
        if not task.checksum_algo and not task.checksum_hex:
            continue
        task.checksum_algo, task.checksum_hex = _validate_checksum_spec(
            task.checksum_algo, task.checksum_hex, registry, task.target
        )


def _parse_headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in values:
        key, _, value = item.partition(":")
        if not key or not value:
            raise DownctlError(f"--header must be 'Key: Value', got {item!r}")
        headers[key.strip()] = value.strip()
    return headers


def _parse_user(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    user, sep, password = value.partition(":")
    if not sep:
        raise DownctlError("--user must be USER:PASSWORD")
    return user, password


def _close_transports(registry: Registry) -> None:
    """Close any transport plugins that hold long-lived connections."""
    for name in registry.names("transport"):
        transport = registry.get("transport", name)
        close = getattr(transport, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


def _witness_label(target: str, done: int) -> str:
    """How a run knows the bytes it already counts as present.

    Reported next to resume/skip numbers so "12 MB done" is never mistaken for
    proof when nothing about the object's version could be checked.
    """
    if done <= 0:
        return "none"
    record = piece_store.load_completion(target)
    if record is not None:
        return piece_store.witness_of(record)
    bitmap = piece_store.load(target)
    if bitmap is not None:
        return "validator" if bitmap.validator else "none"
    return "none"


async def _probe_task(
    ctx: RuntimeContext,
    task: DownloadTask,
    *,
    retries: int,
) -> None:
    """Fill in ``task.size`` and record the object's version witness.

    A definitive 4xx raises :class:`PermanentHTTPError` right away; a transient
    failure (5xx/429/408/425 or a network drop) is retried with the engine's
    pacing policy, honouring ``Retry-After``, and finally raises
    :class:`ProbeError`. Retrying *here* is the point: 0.1.3 routed probing
    around the transport's classification, so a single 500 was reported as a
    permanent failure and the target was dropped after two requests.

    Every HTTP(S) target is probed, *including* the ones whose size a source
    already reported (an S3 listing, a HuggingFace API response, a GitHub
    asset). The version witness is what makes the transfer conditional and a
    completion claim checkable, and it cannot be obtained without asking: with
    a known size this probe used to be skipped, which left the transfer sending
    unconditional range requests and produced a marker that could never notice a
    later replacement. One HEAD per target (or one ranged GET where HEAD is not
    usable) is nothing next to the file it protects.
    """
    if not task.url.startswith(_HTTP_SCHEMES):
        return
    # ``retries`` counts download retries; a probe is bounded much lower and
    # its backoff is capped so a dead batch cannot stall the whole run.
    attempts = PROBE_ATTEMPT_LIMIT if retries <= 0 else min(retries + 1, PROBE_ATTEMPT_LIMIT)
    attempt = 0
    while True:
        attempt += 1
        result = await probe_metadata(ctx.session, task.url)
        if result.ok or not result.retryable:
            break
        if attempt >= attempts:
            reason = result.error or f"HTTP {result.status}"
            raise ProbeError(
                f"probe failed for {_display_url(task)} after {attempt} attempts ({reason})"
            )
        delay = retry_delay(attempt, result.retry_after)
        if result.retry_after is None:
            delay = min(delay, PROBE_BACKOFF_MAX)
        await asyncio.sleep(delay)
    if not result.ok:
        if result.status in _PERMANENT_PROBE_STATUSES:
            raise PermanentHTTPError(result.status, _display_url(task))
        if result.status is None:
            raise ProbeError(
                f"probe failed for {_display_url(task)}: {result.error or 'unreachable'}"
            )
        raise DownctlError(f"probe failed: HTTP {result.status} for {_display_url(task)}")
    task.meta["validator"] = result.validator
    task.meta["if_range"] = result.if_range
    task.meta["witness_kind"] = result.validator_kind
    if result.validator_kind == "none":
        console.verbose(
            f"{_display_url(task)}: the server offers no version witness "
            "(ETag/Last-Modified); this download cannot be checked against a "
            "later replacement"
        )
    if result.size > 0:
        if task.size > 0 and task.size != result.size:
            console.warn(
                f"{_display_url(task)}: the remote size changed from "
                f"{human_size(task.size)} to {human_size(result.size)}; "
                "fetching the current object"
            )
        task.size = result.size


def _dry_run_target(cfg: Config, task: DownloadTask) -> str:
    """Where ``task`` would land, without creating directories."""
    if task.allow_absolute:
        return os.path.abspath(task.target)
    return os.path.abspath(os.path.join(cfg.core.dir, task.target))


async def _bounded_map(
    items: Sequence[ItemT],
    limit: int,
    action: Callable[[ItemT], Awaitable[ResultT]],
) -> list[ResultT | BaseException]:
    """Run ``action`` over ``items`` with bounded concurrency, in input order.

    Order is preserved for reporting: failures reach the failed log in the
    order the user typed the targets, not in completion order. Exceptions are
    returned rather than raised so the caller decides whether the first failure
    aborts the run (``--continue`` semantics stay exactly as before, except that
    targets already in flight are allowed to finish).
    """
    if limit <= 1:
        sequential: list[ResultT | BaseException] = []
        for item in items:
            try:
                sequential.append(await action(item))
            except Exception as exc:  # noqa: BLE001 - handed back to the caller
                sequential.append(exc)
        return sequential
    semaphore = asyncio.Semaphore(limit)

    async def run(item: ItemT) -> ResultT | BaseException:
        async with semaphore:
            try:
                return await action(item)
            except Exception as exc:  # noqa: BLE001 - handed back to the caller
                return exc

    return list(await asyncio.gather(*(run(item) for item in items)))


def _write_failed_log(path: str, lines: list[str]) -> None:
    """Atomically write the failed log, removing it when there is nothing to keep."""
    if not lines:
        with contextlib.suppress(OSError):
            os.remove(path)
        return
    directory = os.path.dirname(path) or "."
    # The failed log can contain credentials from the original URL. Create the
    # temp file with 0600 from the start (never a predictable name that could
    # be pre-created as a symlink), and remove it on any failure.
    tmp_file = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed below
        "w",
        encoding="utf-8",
        dir=directory,
        prefix=os.path.basename(path) + ".",
        suffix=".tmp",
        delete=False,
    )
    tmp = tmp_file.name
    try:
        with tmp_file:
            for line in lines:
                tmp_file.write(line + "\n")
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_file.close()
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
async def _get(args: argparse.Namespace) -> int:
    # The JSON reporter owns stdout. Select that *before* config logging so
    # ``-v --reporter json`` cannot emit a human line into the JSON stream.
    if getattr(args, "reporter", None) == "json":
        console.set_machine_readable(True)
    cfg = _build_config(args)
    if not (
        getattr(args, "targets", None)
        or getattr(args, "list", None)
        or getattr(args, "retry_failed", False)
    ):
        raise DownctlError("no targets given")
    force = bool(getattr(args, "force", False))
    skip_existing = bool(getattr(args, "skip_existing", False))
    if force and skip_existing:
        raise DownctlError("--force and --skip-existing contradict each other; pick one")
    registry = _registry(getattr(args, "local_plugins", False))
    events = EventBus()
    _enable_hooks(args, cfg, registry, events)
    reporter = _select_reporter(args, cfg, registry)

    request_checksum = getattr(args, "checksum", None)
    checksum_file = getattr(args, "checksums", None)
    if checksum_file:
        checksum_file = _local_path(checksum_file)
    early_checksum: tuple[str, str] | None = None
    if request_checksum:
        algo, _, digest = request_checksum.partition(":")
        if not algo or not digest:
            raise DownctlError("--checksum must be ALGO:HEX, e.g. sha256:...")
        early_checksum = _validate_checksum_spec(algo, digest, registry, "--checksum")
    if checksum_file:
        table = _load_checksums(checksum_file)
        if not table:
            raise DownctlError(f"checksum file has no usable entries: {checksum_file}")
        for algo, digest in table.values():
            _validate_checksum_spec(algo, digest, registry, checksum_file)

    extra_headers = _parse_headers(getattr(args, "header", []) or [])
    basic_auth = _parse_user(getattr(args, "user", None))

    async with make_session(
        cfg.core.proxy,
        stall_limit=cfg.core.stall,
        user_agent=getattr(args, "user_agent", None) or "",
        headers=extra_headers,
        auth=basic_auth,
    ) as session:
        ctx = RuntimeContext(
            session=session,
            config=cfg,
            registry=registry,
            events=events,
            rate_limit=cfg.core.rate,
            rate_limiter=RateLimiter(cfg.core.rate),
            options=dict(cfg.plugins),
            params={
                "include": list(getattr(args, "include", []) or []),
                "exclude": list(getattr(args, "exclude", []) or []),
                "revision": getattr(args, "revision", None),
            },
        )
        failed_log = os.path.join(cfg.core.dir, FAILED_LOG_NAME)
        failures: list[str] = []
        keep_going = bool(getattr(args, "continue_", False))
        tasks: list[DownloadTask] = []
        ok = 0
        elapsed = 0.0
        transferred = 0

        try:
            try:
                tasks = await _resolve_targets(
                    args, cfg, ctx, failures=failures, keep_going=keep_going
                )
                _report_early_failures(reporter, failures, "resolve failed")
            except Exception as exc:  # noqa: BLE001 - record the target before re-raising
                _report_early_failures(reporter, failures, str(exc))
                if failures and not getattr(args, "dry_run", False):
                    try:
                        os.makedirs(cfg.core.dir, exist_ok=True)
                        _write_failed_log(
                            failed_log,
                            _merge_failed_log(failed_log, failures, []),
                        )
                    except OSError as log_exc:
                        console.warn(f"could not write failed log: {log_exc}")
                raise
            if not getattr(args, "dry_run", False):
                os.makedirs(cfg.core.dir, exist_ok=True)

            if getattr(args, "output", None):
                if len(tasks) != 1:
                    raise DownctlError("-o/--output requires exactly one target")
                tasks[0].target = os.path.abspath(_local_path(args.output))
                tasks[0].allow_absolute = True

            if early_checksum is not None:
                if len(tasks) != 1:
                    raise DownctlError(
                        "--checksum applies to a single target; "
                        "use --checksums FILE for multiple targets"
                    )
                for task in tasks:
                    task.checksum_algo, task.checksum_hex = early_checksum
                    task.meta["user_checksum"] = True

            if checksum_file:
                _apply_checksums(tasks, checksum_file)
            _validate_checksums(tasks, registry)
            if getattr(args, "recheck", False):
                # Prove every pre-existing file before trusting it: the engine
                # re-hashes when a digest exists and refuses unversioned state
                # otherwise (it derives "require witness" from this flag).
                for task in tasks:
                    task.meta["recheck"] = True

            ready: list[DownloadTask] = []
            probe_retries = cfg.core.retries
            probed = await _bounded_map(
                tasks,
                _probe_jobs(cfg),
                lambda task: _probe_task(ctx, task, retries=probe_retries),
            )
            for task, outcome in zip(tasks, probed, strict=False):
                if isinstance(outcome, BaseException):
                    failures.append(_fmt_url(task))
                    _report_early_failures(reporter, [failures[-1]], str(outcome))
                    if not keep_going:
                        # A probe failure (404, expired token, ...) is still a
                        # failed download: record it so --retry-failed can try
                        # again later instead of losing the target.
                        if not getattr(args, "dry_run", False):
                            try:
                                _write_failed_log(
                                    failed_log,
                                    _merge_failed_log(failed_log, failures, []),
                                )
                            except OSError as log_exc:
                                console.warn(f"could not write failed log: {log_exc}")
                        raise outcome
                    _warn(f"skipping {_display_url(task)}: {outcome}")
                    continue
                ready.append(task)
            tasks = ready

            if getattr(args, "dry_run", False):
                for task in tasks:
                    target = _dry_run_target(cfg, task)
                    done = (
                        0
                        if force
                        else _completed_bytes(
                            target,
                            task,
                            cfg.core.connections,
                            cfg.core.piece_size,
                        )
                        if task.size > 0
                        else 0
                    )
                    if task.size > 0:
                        pending = max(0, task.size - done)
                        detail = (
                            f"{human_size(task.size)} total, {human_size(done)} done, "
                            f"{human_size(pending)} to fetch"
                        )
                    else:
                        pending = 0
                        detail = "size unknown, full download needed"
                    if console.is_machine_readable():
                        print(
                            json.dumps(
                                {
                                    "event": "dry_run",
                                    "target": task.target,
                                    "url": _display_url(task),
                                    "size": task.size,
                                    "done": done,
                                    "pending": pending if task.size > 0 else None,
                                    "witness": _witness_label(target, done),
                                },
                                ensure_ascii=False,
                            )
                        )
                    else:
                        console.info(
                            f"[dry-run] {_display_url(task)}\n  -> {task.target}  {detail}"
                        )
                if failures:
                    # A dry run must not mutate the failed log: previously this
                    # overwrote the record of unrelated, still-failing targets.
                    console.error(f"{len(failures)} target(s) failed")
                    return 1
                return 0

            def on_result(task: DownloadTask, success: bool) -> None:
                if not success:
                    failures.append(_fmt_url(task))

            started = time.monotonic()
            ok, _failed = await download_all(
                ctx,
                tasks,
                jobs=cfg.core.jobs,
                reporter=reporter,  # type: ignore[arg-type]
                connections=cfg.core.connections,
                stall_limit=cfg.core.stall,
                piece_size=cfg.core.piece_size,
                min_split_size=cfg.core.min_split_size,
                checkpoint=cfg.core.checkpoint,
                force=force,
                skip_existing=skip_existing,
                on_result=on_result,
                extra_failed=len(failures),
            )
            elapsed = time.monotonic() - started
            transferred = ctx.stats.bytes_transferred

            merged = _merge_failed_log(failed_log, failures, tasks)
            _write_failed_log(failed_log, merged)
        finally:
            await events.aclose()
            _close_transports(registry)

    if failures:
        console.error(f"{len(failures)} target(s) failed (retry with: downctl get --retry-failed)")
        return 1
    summary = f"done: {ok}/{len(tasks)} files"
    if elapsed >= 0.05:
        summary += f" in {elapsed:.1f}s"
        if transferred > 0:
            summary += f" ({human_size(transferred / elapsed)}/s)"
    if getattr(args, "output", None) and len(tasks) == 1:
        summary += f" -> {tasks[0].target}"
    else:
        summary += f" -> {cfg.core.dir}"
    console.info(summary)
    for task in tasks:
        console.verbose(f"  {task.target}")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    def run() -> int:
        with interrupt_on_sigterm():
            return asyncio.run(_get(args))

    return _guard(run)


async def _probe(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    # ``probe --json`` owns stdout just like the JSON reporter; select it
    # before config logging so ``-v`` cannot prepend a human line.
    console.set_machine_readable(as_json)
    cfg = _build_config(args)
    failures = 0
    async with make_session(cfg.core.proxy, "", stall_limit=STALL_LIMIT) as session:
        for target in args.targets:
            if target.startswith(_HTTP_SCHEMES):
                detail = await probe_resource(session, target)
            else:
                detail = {
                    "target": target,
                    "status": None,
                    "size": 0,
                    "content_type": "",
                    "final_url": target,
                    "filename": "",
                    "reachable": False,
                    "error": "unsupported target (probe only handles http/https)",
                }
            raw_size = detail.get("size")
            size = float(raw_size) if isinstance(raw_size, (int, float)) else 0.0
            status = detail.get("status")
            if not detail.get("reachable") or (isinstance(status, int) and status >= 400):
                failures += 1
            shown_target = display_url(str(detail.get("target", target)))
            shown_final = display_url(str(detail.get("final_url", target)))
            if as_json:
                record = dict(detail)
                record["target"] = shown_target
                record["final_url"] = shown_final
                record["size_human"] = human_size(size)
                print(json.dumps(record, ensure_ascii=False))
            else:
                if detail.get("error"):
                    label = "unsupported"
                elif detail.get("reachable"):
                    label = str(detail.get("status"))
                else:
                    label = "unreachable"
                lines = [
                    shown_target,
                    f"  status={label}  size={human_size(size)}",
                ]
                if detail.get("content_type"):
                    lines.append(f"  type={detail['content_type']}")
                if detail.get("filename"):
                    lines.append(f"  filename={detail['filename']}")
                if shown_final != shown_target:
                    lines.append(f"  url={shown_final}")
                if detail.get("error"):
                    lines.append(f"  error={detail['error']}")
                console.info("\n".join(lines))
    return 1 if failures else 0


def cmd_probe(args: argparse.Namespace) -> int:
    return _guard(lambda: asyncio.run(_probe(args)))


def cmd_completions(args: argparse.Namespace) -> int:
    shell = getattr(args, "shell", "bash") or "bash"
    print(_completion_script(shell))
    return 0


def _completion_script(shell: str) -> str:
    """Return a dependency-free shell completion script for ``shell``."""
    commands = "get probe status verify clean plugin config doctor wizard version completions"
    if shell == "fish":
        return (
            f"# downctl fish completion\n"
            f"complete -c downctl -f\n"
            f"complete -c downctl -n '__fish_use_subcommand' -a '{commands}'\n"
            f"complete -c downctl -l help -s h\n"
            f"complete -c downctl -l version -s V\n"
        )
    if shell == "zsh":
        return (
            "#compdef downctl\n"
            "# downctl zsh completion\n"
            "_downctl() {\n"
            f"  local -a commands; commands=({commands})\n"
            "  if (( CURRENT == 2 )); then\n"
            "    _describe 'command' commands\n"
            "  fi\n"
            "}\n"
            "compdef _downctl downctl\n"
        )
    return (
        "# downctl bash completion\n"
        "_downctl() {\n"
        "  local cur prev\n"
        "  COMPREPLY=()\n"
        '  cur="${COMP_WORDS[COMP_CWORD]}"\n'
        '  if [ "$COMP_CWORD" -eq 1 ]; then\n'
        f'    COMPREPLY=( $(compgen -W "{commands}" -- "$cur") )\n'
        "    return 0\n"
        "  fi\n"
        "}\n"
        "complete -F _downctl downctl\n"
    )


def collect_status(directory: str) -> tuple[list[Incomplete], list[str]]:
    """Scan a directory for in-progress (bitmap) and failed downloads."""
    base = Path(directory)
    incomplete: list[Incomplete] = []
    if base.is_dir():
        for bitmap_file in sorted(base.rglob("*" + piece_store.BITMAP_SUFFIX)):
            target = str(bitmap_file)[: -len(piece_store.BITMAP_SUFFIX)]
            bitmap = piece_store.load(target)
            if bitmap is None:
                continue
            incomplete.append(
                Incomplete(
                    target=os.path.relpath(target, directory),
                    done=bitmap.done_pieces,
                    pieces=bitmap.pieces,
                    size=bitmap.size,
                )
            )

    failed: list[str] = []
    failed_log = base / FAILED_LOG_NAME
    if failed_log.is_file():
        with open(failed_log, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                kind, payload = _parse_failed_line(line)
                if kind == "url" and isinstance(payload, dict):
                    failed.append(str(payload.get("target") or payload.get("url")))
                else:
                    failed.append(str(payload))
    return incomplete, failed


def _artifact_target(path: Path) -> str | None:
    """The data target a bitmap/marker/temp artifact belongs to.

    Anchor on the *last* complete suffix so a directory or filename that
    merely contains ``.downctl-pieces`` cannot redirect the live-lock check.
    """
    name = str(path)
    for suffix in (piece_store.BITMAP_SUFFIX, piece_store.COMPLETION_SUFFIX):
        index = name.rfind(suffix)
        while index > 0:
            rest = name[index + len(suffix) :]
            if rest == "" or rest == ".tmp" or (rest.startswith(".") and rest.endswith(".tmp")):
                return name[:index]
            index = name.rfind(suffix, 0, index)
    return None


def _artifact_is_live(path: Path) -> bool:
    target = _artifact_target(path)
    if target is None:
        return False
    lock = Path(target + ".lock")
    return lock.is_dir() and _lock_is_live(lock)


def find_cleanables(directory: str) -> list[Path]:
    """Return leftover artifact paths (bitmaps, failed log, lock dirs)."""
    base = Path(directory)
    if not base.is_dir():
        return []
    found: set[Path] = set()
    for pattern in (
        f"*{piece_store.BITMAP_SUFFIX}.tmp",
        f"*{piece_store.BITMAP_SUFFIX}.*.tmp",
        f"*{piece_store.COMPLETION_SUFFIX}.tmp",
        f"*{piece_store.COMPLETION_SUFFIX}.*.tmp",
        f"{FAILED_LOG_NAME}.*.tmp",
    ):
        # Only regular files are artifacts. A user directory named
        # ``something.downctl-pieces`` must never be removed by clean.
        found.update(path for path in base.rglob(pattern) if path.is_file())
    bitmap_suffix = piece_store.BITMAP_SUFFIX
    for bitmap in base.rglob(f"*{bitmap_suffix}"):
        if not bitmap.is_file():
            continue
        target = str(bitmap)[: -len(bitmap_suffix)]
        # Only recognized ledgers are artifacts; a user data file that merely
        # ends in the suffix is not deleted.
        if piece_store.load(target) is not None:
            found.add(bitmap)
    for lock_dir in base.rglob("*.lock"):
        if lock_dir.is_dir() and not _lock_is_live(lock_dir):
            found.add(lock_dir)
    suffix = piece_store.COMPLETION_SUFFIX
    for marker in base.rglob(f"*{suffix}"):
        if not marker.is_file():
            continue
        data_file = Path(str(marker)[: -len(suffix)])
        # Only remove a file that is *actually* a completion marker. A user
        # data file whose name merely ends in the suffix must never be deleted
        # by clean; malformed sidecars are left for the user.
        record = piece_store.load_completion(str(data_file))
        if not data_file.is_file() and record is not None and record.get("url_sha256"):
            found.add(marker)  # orphan marker: its data file is gone
    for name in (FAILED_LOG_NAME, FAILED_LOG_NAME + ".tmp"):
        failed = base / name
        if failed.is_file():
            found.add(failed)
    # Never delete the resume state of a transfer that is still running.
    return sorted(path for path in found if not _artifact_is_live(path))


def _lock_is_live(lock_dir: Path) -> bool:
    """Whether a lock directory is (still) owned by a running process.

    A missing or unparseable pid file is treated as live so `clean` never
    removes a directory it cannot prove belongs to a dead downctl process.
    """
    pidfile = lock_dir / "pid"
    if not pidfile.is_file():
        return True
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return True
    return pid > 0 and pid_alive(pid)


def cmd_clean(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    console.set_machine_readable(as_json)
    cfg = _build_config(args)
    directory = _local_path(getattr(args, "path", None) or cfg.core.dir)
    items = find_cleanables(directory)
    dry_run = bool(getattr(args, "dry_run", False))
    removed = 0
    failed: list[dict[str, str]] = []
    if not dry_run:
        for path in items:
            try:
                if path.is_dir():
                    # Only lock directories are directories by construction;
                    # never rmtree an arbitrary glob match.
                    if not path.name.endswith(".lock"):
                        continue
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed += 1
            except FileNotFoundError:
                removed += 1  # it disappeared since find_cleanables()
            except OSError as exc:
                failed.append({"path": str(path), "error": redact_text(str(exc))})
    if as_json:
        # ``--json`` changes the *format*, not the command: a real clean still
        # removes the artifacts (``--dry-run --json`` is the non-destructive
        # combination).
        print(
            json.dumps(
                {
                    "directory": directory,
                    "dry_run": dry_run,
                    "items": [str(path) for path in items],
                    "count": len(items),
                    "removed": removed,
                    "failed": failed,
                },
                ensure_ascii=False,
            )
        )
        return 1 if failed else 0
    if not items:
        console.info(f"nothing to clean in {directory}")
        return 0
    if dry_run:
        for path in items:
            console.info(f"  would remove  {path}")
        console.info(f"{len(items)} item(s) would be removed")
        return 0
    console.info(f"removed {removed} item(s)")
    if failed:
        for record in failed:
            console.error(f"could not remove {record['path']}: {redact_text(record['error'])}")
        return 1
    return 0


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _verify_candidates(directory: str) -> tuple[list[str], list[Incomplete]]:
    """Files that claim completion, plus the still-incomplete ones.

    Only *recognized* completion markers count as a claim: a user data file
    whose name merely ends in the suffix must not be judged (or deleted).
    """
    base = Path(directory)
    claims: list[str] = []
    incomplete: list[Incomplete] = []
    if not base.is_dir():
        return claims, incomplete
    suffix = piece_store.COMPLETION_SUFFIX
    for marker in sorted(base.rglob("*" + suffix)):
        if not marker.is_file():
            continue
        target = str(marker)[: -len(suffix)]
        if piece_store.load_completion(target) is not None:
            claims.append(target)
    for bitmap_file in sorted(base.rglob("*" + piece_store.BITMAP_SUFFIX)):
        if not bitmap_file.is_file():
            continue
        target = str(bitmap_file)[: -len(piece_store.BITMAP_SUFFIX)]
        bitmap = piece_store.load(target)
        if bitmap is None:
            continue
        incomplete.append(
            Incomplete(
                target=os.path.relpath(target, directory),
                done=bitmap.done_pieces,
                pieces=bitmap.pieces,
                size=bitmap.size,
            )
        )
    return claims, incomplete


def _lookup_checksum(
    table: dict[str, tuple[str, str]], relative: str, target: str
) -> tuple[str, str] | None:
    """Find the digest a manifest has for ``target`` (rel path or basename)."""
    rel = relative.replace(os.sep, "/")
    candidates = [rel, os.path.basename(rel), target.replace(os.sep, "/")]
    if rel.startswith("./"):
        candidates.append(rel[2:])
    for key in candidates:
        if key and key in table:
            return table[key]
    return None


async def _digest_file(path: str, algo: str) -> str:
    """Hex digest of ``path``, or ``""`` when the algorithm is not a hash."""
    try:
        hasher = hashlib.new(algo)
    except ValueError:
        return ""

    def read() -> str:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    return await asyncio.to_thread(read)


def _remove_proven_bad(target: str) -> None:
    """Delete a file that was proven not to be the expected object."""
    piece_store.discard(target)
    with contextlib.suppress(OSError):
        os.remove(target)


async def _verify_one(
    ctx: RuntimeContext,
    directory: str,
    target: str,
    table: dict[str, tuple[str, str]],
    repair: bool,
) -> VerifyEntry:
    """Judge one completion claim, optionally repairing a proven-bad file."""
    relative = os.path.relpath(target, directory).replace(os.sep, "/")
    record = piece_store.load_completion(target)
    witness = piece_store.witness_of(record)
    if not os.path.isfile(target):
        entry = VerifyEntry(
            relative, "missing", witness, detail="completion marker without its data file"
        )
        if repair:
            piece_store.remove(target)
            entry.repaired = True
        return entry

    found = _lookup_checksum(table, relative, target)
    if found is not None:
        algo, digest = found
    elif record is not None:
        algo = str(record.get("checksum_algo") or "")
        digest = str(record.get("checksum_hex") or "")
    else:  # pragma: no cover - a claim always has a record by construction
        algo = digest = ""

    if algo and digest:
        registry = ctx.registry
        verifier = registry.get("verifier", algo) if registry is not None else None
        if verifier is None:
            return VerifyEntry(
                relative,
                "unverifiable",
                witness,
                algo=algo,
                expected=digest,
                detail=f"no verifier for algorithm {algo!r}",
            )
        try:
            matched = await verifier.verify(target, digest)  # type: ignore[attr-defined]
        except (OSError, ValueError) as exc:
            return VerifyEntry(
                relative,
                "unverifiable",
                witness,
                algo=algo,
                expected=digest,
                detail=f"cannot read the file: {exc}",
            )
        if matched:
            return VerifyEntry(relative, "ok", "checksum", algo=algo, expected=digest)
        entry = VerifyEntry(
            relative,
            "mismatch",
            witness,
            algo=algo,
            expected=digest,
            actual=await _digest_file(target, algo),
            detail="content does not match the recorded digest",
        )
        if repair:
            _remove_proven_bad(target)
            entry.repaired = True
        return entry

    # No digest anywhere: the marker's size/mtime binding is all that can be
    # checked offline, which is enough to catch external edits but is *not*
    # proof of content.
    stat = os.stat(target)
    if record is not None and (
        record["size"] != stat.st_size or record["mtime_ns"] != stat.st_mtime_ns
    ):
        entry = VerifyEntry(
            relative,
            "mismatch",
            witness,
            detail="file changed since the completion marker was written",
        )
        if repair:
            _remove_proven_bad(target)
            entry.repaired = True
        return entry
    return VerifyEntry(
        relative,
        "unverifiable",
        witness,
        detail="no digest recorded; pass --checksums FILE to prove content",
    )


async def _verify(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    console.set_machine_readable(as_json)
    cfg = _build_config(args)
    directory = _local_path(getattr(args, "path", None) or cfg.core.dir)
    registry = _registry(bool(getattr(args, "local_plugins", False)))
    repair = bool(getattr(args, "repair", False))

    table: dict[str, tuple[str, str]] = {}
    checksum_file = getattr(args, "checksums", None)
    if checksum_file:
        checksum_file = _local_path(checksum_file)
        table = _load_checksums(checksum_file)
        if not table:
            raise DownctlError(f"checksum file has no usable entries: {checksum_file}")
        for algo, digest in table.values():
            _validate_checksum_spec(algo, digest, registry, checksum_file)

    claims, incomplete = _verify_candidates(directory)
    entries: list[VerifyEntry] = []
    repaired = 0
    async with make_session(cfg.core.proxy, "", stall_limit=STALL_LIMIT) as session:
        ctx = RuntimeContext(session=session, config=cfg, registry=registry)
        for target in claims:
            entry = await _verify_one(ctx, directory, target, table, repair)
            if entry.repaired:
                repaired += 1
            entries.append(entry)

    counts = {
        "ok": sum(1 for entry in entries if entry.status == "ok"),
        "mismatch": sum(1 for entry in entries if entry.status == "mismatch"),
        "unverifiable": sum(1 for entry in entries if entry.status == "unverifiable"),
        "missing": sum(1 for entry in entries if entry.status == "missing"),
    }
    if as_json:
        print(
            json.dumps(
                {
                    "directory": directory,
                    "checked": len(entries),
                    "ok": counts["ok"],
                    "mismatch": counts["mismatch"],
                    "unverifiable": counts["unverifiable"],
                    "missing": counts["missing"],
                    "repaired": repaired,
                    "entries": [entry.to_json() for entry in entries],
                    "incomplete": [
                        {
                            "target": item.target,
                            "done": item.done,
                            "pieces": item.pieces,
                            "size": item.size,
                        }
                        for item in incomplete
                    ],
                },
                ensure_ascii=False,
            )
        )
    else:
        console.info(f"downctl verify — {directory}")
        if not entries:
            console.info("  no completed file carries a marker; nothing to verify")
        for entry in entries:
            detail = f"  ({entry.detail})" if entry.detail else ""
            algo = f" {entry.algo}" if entry.algo else ""
            verb = "repaired " if entry.repaired else ""
            console.info(
                f"  {verb}{entry.status:12s} {entry.target}  witness={entry.witness}{algo}{detail}"
            )
        if entries:
            console.info(
                f"  {counts['ok']} proven, {counts['mismatch']} mismatch, "
                f"{counts['missing']} missing, {counts['unverifiable']} unverifiable"
            )
        if incomplete:
            console.verbose(f"  {len(incomplete)} file(s) still incomplete (see: downctl status)")
        if counts["unverifiable"] and not table:
            console.info("  hint: --checksums FILE turns unverifiable files into proof")

    if counts["mismatch"] or counts["missing"]:
        return 1
    if counts["unverifiable"]:
        return 2
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    return _guard(lambda: asyncio.run(_verify(args)))


def _failed_line_identity(line: str) -> str:
    """The printable URL/target of a failed-log line (legacy dedup key)."""
    columns = line.split("\t")
    return columns[1] if len(columns) >= 2 and columns[0] in ("url", "target") else columns[0]


def _task_keys(task: DownloadTask) -> set[str]:
    """Keys that identify the failed-log records a task supersedes."""
    keys = {_persisted_url(task)}
    if task.origin:
        keys.add(f"{task.origin}\t{task.target}")
    return keys


def _line_keys(line: str) -> set[str]:
    """Keys a stored failed-log line can be matched by."""
    columns = line.split("\t")
    keys = {_failed_line_identity(line)}
    if len(columns) >= 8 and columns[7]:
        keys.add(f"{columns[7]}\t{columns[2]}")
    return keys


def _merge_failed_log(failed_log: str, failures: list[str], tasks: list[DownloadTask]) -> list[str]:
    """Preserve previous failures for targets not touched by this run.

    A record is removed when this run either re-attempted the same
    ``origin + target`` pair or handled the same printable URL. Matching by
    ``origin`` alone would drop unrelated files of the same repository, so the
    pair is used whenever available.
    """
    processed: set[str] = set()
    for line in failures:
        processed.update(_line_keys(line))
    for task in tasks:
        processed.update(_task_keys(task))
        if task.origin:
            processed.add(task.origin)
    kept: list[str] = []
    if os.path.isfile(failed_log):
        with open(failed_log, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                if not (_line_keys(line) & processed):
                    kept.append(line)
    return kept + failures


def cmd_status(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    console.set_machine_readable(as_json)
    cfg = _build_config(args)
    directory = _local_path(getattr(args, "path", None) or cfg.core.dir)
    incomplete, failed = collect_status(directory)
    # Legacy failed-log records may be the raw URL a target was resolved from;
    # never print a credential from one. Plain paths pass through unchanged.
    failed = [display_url(item) for item in failed]

    records: list[dict[str, object]] = []
    ages: list[int | None] = []
    for item in incomplete:
        pieces = item.pieces or 1
        record: dict[str, object] = {
            "target": item.target,
            "done": item.done,
            "pieces": item.pieces,
            "percent": item.done * 100 // pieces,
            "size": item.size,
        }
        age: int | None = None
        try:
            mtime = os.path.getmtime(os.path.join(directory, item.target))
            age = int(time.time() - mtime)
            record["age_seconds"] = age
        except OSError:
            pass
        ages.append(age)
        records.append(record)

    if as_json:
        print(
            json.dumps(
                {"directory": directory, "incomplete": records, "failed": failed},
                ensure_ascii=False,
            )
        )
        return 0

    console.info(f"downctl status — {directory}")
    if not incomplete and not failed:
        console.info("  no incomplete or failed downloads")
        return 0

    for index, item in enumerate(incomplete):
        age_text = ""
        age_seconds = ages[index]
        if age_seconds is not None:
            age_text = f"  last modified {human_age(float(age_seconds))}"
        console.info(
            f"  incomplete  {item.target}  "
            f"{item.done}/{item.pieces} pieces ({records[index]['percent']}%)  "
            f"{human_size(float(item.size))}{age_text}"
        )
    for target in failed:
        console.info(f"  failed      {target}")
    return 0


def cmd_plugin(args: argparse.Namespace) -> int:
    console.set_machine_readable(False)
    registry = _registry(bool(getattr(args, "local_plugins", False)))
    action = getattr(args, "plugin_action", None)
    if action == "info":
        found = False
        for category, plugins in registry.items():
            if args.name in plugins:
                found = True
                plugin = plugins[args.name]
                console.info(f"{category}: {args.name} (priority={getattr(plugin, 'priority', 0)})")
        if not found:
            _errors(f"no plugin named {args.name!r}")
            return 1
        return 0
    for category, plugins in registry.items():
        names = ", ".join(sorted(plugins)) or "-"
        console.info(f"{category:10s} {names}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    console.set_machine_readable(False)
    action = getattr(args, "config_action", None) or "show"
    if action == "path":
        console.info(config_path())
        return 0
    if action == "init":
        try:
            written = write_default_config(force=getattr(args, "force", False))
        except FileExistsError as exc:
            _errors(str(exc))
            return 1
        console.info(f"wrote {written}")
        return 0
    cfg = load_config()
    console.info(f"# {config_path()}")
    console.info(f"dir     = {cfg.core.dir or DEFAULT_DIR}")
    console.info(f"jobs    = {cfg.core.jobs}")
    console.info(f"retries = {cfg.core.retries}")
    console.info(f"connections = {cfg.core.connections}")
    console.info(f"stall   = {cfg.core.stall}")
    console.info(f"checkpoint = {cfg.core.checkpoint}")
    console.info(f"piece_size = {cfg.core.piece_size or 'auto'}")
    console.info(f"min_split_size = {cfg.core.min_split_size}")
    console.info(f"proxy   = {display_url(cfg.core.proxy) or '(none)'}")
    console.info(f"rate    = {cfg.core.rate}")
    if cfg.core.mirror:
        shown_mirror = {
            display_url(key): display_url(value) for key, value in cfg.core.mirror.items()
        }
        console.info(f"mirror  = {shown_mirror}")
    if cfg.core.hooks:
        console.info(f"hooks   = {cfg.core.hooks}")
    for name, options in cfg.plugins.items():
        console.info(f"[plugins.{name}] {mask_mapping(options)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:  # noqa: ARG001
    console.set_machine_readable(False)
    registry = _registry(False)
    console.info(f"downctl  {__version__}")
    console.info(f"python   {platform.python_version()}")
    console.info(f"platform {platform_kind()}")
    try:
        import aiohttp

        console.info(f"aiohttp  {aiohttp.__version__}")
    except ImportError:  # pragma: no cover
        console.info("aiohttp  NOT INSTALLED")
    try:
        import tqdm

        console.info(f"tqdm     {tqdm.__version__}")
    except ImportError:  # pragma: no cover
        console.info("tqdm     NOT INSTALLED")
    console.info(f"config   {config_path()}")
    console.info(
        f"plugins  {len(registry)} loaded ({', '.join(registry.names('source')) or '-'} sources)"
    )
    proxy = detect_wsl_proxy(
        load_config().core.proxy
        or os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("HTTP_PROXY")
        or ""
    )
    console.info(f"proxy    {display_url(proxy) or '(none detected)'}")
    return 0


def cmd_wizard(args: argparse.Namespace) -> int:  # noqa: ARG001
    if not stdin_is_tty():
        _errors("wizard needs an interactive terminal")
        return 1
    console.info("downctl wizard — enter one target per line, blank to finish")
    try:
        raw = input("  targets: ")
    except EOFError:
        return 1
    targets = [t.strip() for t in raw.split() if t.strip()]
    if not targets:
        _errors("no targets given")
        return 1
    namespace = _default_get_args()
    namespace.targets = targets
    try:
        dest = input(f"  destination [{DEFAULT_DIR}]: ").strip()
    except EOFError:
        dest = ""
    if dest:
        namespace.dir = dest
    return cmd_get(namespace)


def _default_get_args() -> argparse.Namespace:
    return argparse.Namespace(
        targets=[],
        dir=None,
        output=None,
        jobs=None,
        connections=None,
        stall=None,
        piece_size=None,
        min_split_size=None,
        checkpoint=None,
        max_time=None,
        probe_jobs=None,
        force=False,
        skip_existing=False,
        require_witness=False,
        recheck=False,
        limit_rate=None,
        retries=None,
        checksum=None,
        checksums=None,
        proxy=None,
        mirror=None,
        source=None,
        reporter=None,
        include=[],
        exclude=[],
        revision=None,
        dry_run=False,
        retry_failed=False,
        continue_=False,
        hook=[],
        user_agent=None,
        header=[],
        user=None,
        list=None,
        local_plugins=False,
    )


def _guard(action: Callable[[], int]) -> int:
    """Run ``action`` translating errors into clean messages and exit codes."""
    try:
        return action()
    except DownctlError as exc:
        console.error(redact_text(str(exc)))
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; progress is kept, re-run to resume", file=sys.stderr)
        return 130
    except BrokenPipeError:
        # ``downctl ... | head`` closes stdout early; this is not an error.
        return 0
    except aiohttp.ClientError as exc:
        console.error(f"network error: {redact_text(str(exc))}")
        return 1
    except OSError as exc:
        console.error(f"local I/O error: {redact_text(str(exc))}")
        return 1
    except Exception as exc:  # noqa: BLE001 - keep the CLI from dumping tracebacks
        if console.get_level() >= console.VERBOSE:
            raise
        console.error(f"{type(exc).__name__}: {redact_text(str(exc))} (use -v for a traceback)")
        return 1
