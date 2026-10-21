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
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import TypeVar
from urllib.parse import unquote, urlsplit

import aiohttp

from downctl import __version__, console, digest
from downctl.config import (
    Config,
    CoreConfig,
    as_bool,
    config_path,
    load_config,
    write_default_config,
)
from downctl.core import pieces as piece_store
from downctl.core.context import RuntimeContext
from downctl.core.credentials import AuthError, AuthResolver, build_rules, host_from_input
from downctl.core.engine import (
    _completed_bytes,
    _observed_validator,
    download_all,
    refresh_url,
    refuse_self_copy,
    retry_delay,
    seal_identity,
    target_path,
)
from downctl.core.errors import (
    FAILURE_PERMANENT,
    FAILURE_UNKNOWN,
    DownctlError,
    PermanentHTTPError,
    ProbeError,
    classify_failure,
)
from downctl.core.job import Job
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
from downctl.plugin import credentials
from downctl.plugin.hooks import EventBus, apply_hook_options
from downctl.plugin.loader import discover
from downctl.plugin.registry import Registry
from downctl.probe import probe_metadata, probe_resource
from downctl.session import (
    AUTH_RESOLVER_ATTR,
    PROXY_ALL,
    PROXY_EXTERNAL,
    ProxyPolicy,
    make_session,
)
from downctl.urls import display_url, mask_mapping, redact_text, redact_url

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
    """One file that was judged, and what ``verify`` found."""

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
    #: True when this command itself compared something (a digest, a size).
    #: ``verify`` is offline: a version witness it prints came from the marker
    #: and was never compared with the server, so claiming "validator" without
    #: this flag would describe a comparison that did not happen.
    compared: bool = False
    #: True when ``--checksums`` named this file rather than the file claiming
    #: completion itself. Both are judged; a machine can tell them apart.
    from_manifest: bool = False

    def to_json(self) -> dict[str, object]:
        record: dict[str, object] = {
            "target": self.target,
            "status": self.status,
            "witness": self.witness,
            "compared": self.compared,
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
        if self.from_manifest:
            record["from_manifest"] = True
        return record


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _errors(message: str) -> None:
    console.error(message)


def _warn(message: str, *, always: bool = False) -> None:
    """Report a degraded path.

    ``always`` is for problems the user has to act on: a hook that could not be
    configured is a typo in their config, and ``-q`` (which means "no progress
    chatter") must not be able to hide it.
    """
    console.warn(message, always=always)


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
    """Parse ``--limit-rate``. ``0`` is a documented spelling of "no limit"."""
    try:
        parsed = parse_rate(value)
    except ValueError as exc:
        raise DownctlError(f"--limit-rate: {exc}") from exc
    if parsed is None or parsed < 0:
        raise DownctlError(f"--limit-rate must be >= 0 (0 = unlimited), got {value!r}")
    return parsed


def _build_config(args: argparse.Namespace, *, log: bool = True, warn: bool = True) -> Config:
    """Merge the config file with command-line overrides."""
    cfg = load_config()
    if warn:
        for warning in cfg.warnings:
            # A misspelled [core] key used to be dropped without a word while
            # the default quietly kept winning. It is a configuration error, so
            # `-q` must not be able to hide it.
            _warn(warning, always=True)

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
        cfg.proxy_source = "explicit"
    if getattr(args, "no_proxy", False):
        # "--no-proxy" promises to ignore a configured *or environment* proxy.
        # Applied before ``-p``, an explicit proxy silently won regardless of
        # argument order; a direct connection is the stronger statement.
        cfg.core.proxy = ""
        cfg.proxy_source = "explicit"
    elif cfg.core.proxy:
        cfg.proxy_source = "explicit"
    if getattr(args, "limit_rate", None) is not None:
        rate = _checked_rate(args.limit_rate)
        cfg.core.rate = rate if rate > 0 else None
    if getattr(args, "mirror", None):
        source, _, dest = args.mirror.partition("=")
        if not dest:
            raise DownctlError("--mirror must be FROM=TO, e.g. https://a=https://b")
        cfg.core.mirror[source] = dest
    if getattr(args, "router", None):
        cfg.core.router = str(args.router)

    if not cfg.core.dir:
        cfg.core.dir = DEFAULT_DIR
    cfg.core.dir = _local_path(os.path.expanduser(cfg.core.dir))

    if not cfg.core.proxy and not getattr(args, "no_proxy", False):
        env_proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
            or ""
        )
        if env_proxy:
            cfg.core.proxy = env_proxy
            cfg.proxy_source = "env"
    # A proxy nobody configured is a *guess* about how this machine reaches the
    # network. It is allowed to make outbound requests work; it is not allowed to
    # decide that a request for this machine should leave it, because the proxy
    # would resolve the address on the other side of the boundary.
    if not getattr(args, "no_proxy", False):
        guessed = detect_wsl_proxy(cfg.core.proxy)
        if guessed != cfg.core.proxy:
            cfg.core.proxy = guessed
            cfg.proxy_source = "wsl-guess"
    if log:
        _log_config(cfg)
    return cfg


def _log_config(cfg: Config) -> None:
    """Report the effective configuration under ``-v``.

    Separate from :func:`_build_config` so the *reporter* can be selected
    first: a machine-readable reporter owns stdout, and a config line printed
    before that decision corrupted the JSON stream.
    """
    console.verbose(
        f"config dir={cfg.core.dir} jobs={cfg.core.jobs} connections={cfg.core.connections} "
        f"stall={cfg.core.stall} checkpoint={cfg.core.checkpoint} "
        f"max_time={cfg.core.max_time or 'none'} probe_jobs={cfg.core.probe_jobs} "
        f"require_witness={cfg.core.require_witness} "
        f"piece_size={cfg.core.piece_size or 'auto'} "
        f"rate={cfg.core.rate} proxy={display_url(cfg.core.proxy) or '(none)'} "
        f"proxy_source={cfg.proxy_source or '(none)'}"
    )


def _proxy_policy(cfg: Config) -> ProxyPolicy:
    """How far the configured proxy may reach (see :class:`ProxyPolicy`)."""
    scope = PROXY_EXTERNAL if cfg.proxy_source == "wsl-guess" else PROXY_ALL
    return ProxyPolicy(cfg.core.proxy, scope)


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
        tqdm_enabled = as_bool(
            "plugins.tqdm.enabled", cfg.plugins.get("tqdm", {}).get("enabled"), True
        )
        name = "tqdm" if (use_bar and tqdm_enabled) else "quiet"
    reporter = registry.get("reporter", name)
    if reporter is None:
        raise DownctlError(f"unknown reporter plugin: {name}")
    console.set_machine_readable(getattr(reporter, "machine_readable", False) is True)
    return reporter


def _warn_unknown_plugin_options(cfg: Config, registry: Registry) -> None:
    """Name a ``[plugins.x]`` key the plugin does not declare.

    Plugin options used to be read with ``.get()`` at each call site, so a
    misspelled key was silently ignored while the default kept winning — the
    same failure 0.1.5 fixed for ``[core]``. Only plugins that declare
    ``OPTIONS`` can be checked without guessing; third-party plugins without
    the attribute are left alone.
    """
    for _category, plugins in registry.items():
        for name, plugin in plugins.items():
            declared = getattr(plugin, "OPTIONS", None)
            if not isinstance(declared, dict):
                continue
            for key in cfg.plugins.get(name, {}):
                if key not in declared:
                    _warn(
                        f"[plugins.{name}] unknown option {key!r} "
                        f"(known: {', '.join(sorted(declared))})",
                        always=True,
                    )


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
            _warn(f"unknown hook plugin: {name}", always=True)
            continue
        for problem in apply_hook_options(hook, cfg.plugins.get(name, {})):
            _warn(f"[plugins.{name}] {problem}", always=True)
        events.register_hook(hook)  # type: ignore[arg-type]
        enabled.append(name)
    return enabled


def _seal_identity(task: DownloadTask) -> None:
    """Bind resume state to the URL that will actually be fetched.

    The rule lives in the engine (:func:`downctl.core.engine.seal_identity`)
    because ``refresh_url`` has to apply it too; the app only calls it.
    """
    seal_identity(task)


def _apply_router(task: DownloadTask, cfg: Config, registry: Registry) -> None:
    """Run the configured router plugin (``mirror`` by default).

    The category used to be dead: the name was hardcoded to ``mirror`` and the
    plugin was only called when ``[core.mirror]`` was non-empty, so a router
    registered under any other name could never run. Selection is a config/CLI
    answer now, and the selected plugin is applied even with no mappings (a
    router may rewrite by its own rules).
    """
    name = getattr(cfg.core, "router", "mirror") or "mirror"
    router = registry.get("router", name)
    if router is None:
        _warn(f"unknown router plugin: {name}", always=True)
    else:
        if name == "mirror":
            # ``mappings`` belongs to the mirror router, not to the Router
            # protocol; assigning it to a slotted third-party shadow raised
            # AttributeError during the first download.
            router.mappings = dict(cfg.core.mirror)  # type: ignore[attr-defined]
        before = task.url
        task.url = router.rewrite(task.url)  # type: ignore[attr-defined]
        if task.url != before:
            redact = bool(task.meta.get("redact_url"))
            console.verbose(
                f"{name}: {display_url(before, redact)} -> {display_url(task.url, redact)}"
            )
    _seal_identity(task)


def _display_url(task: DownloadTask) -> str:
    """URL for logs; masks credentials and strips a presigned query."""
    return display_url(task.url, bool(task.meta.get("redact_url")))


def _persisted_url(task: DownloadTask) -> str:
    """URL stored in the failed log so a later ``--retry-failed`` can use it.

    Presigned URLs lose their whole query (it expires anyway; the origin is
    re-resolved). Every other URL keeps its structure but has credential-bearing
    parameters masked: a failed log is a file that outlives the run, and a
    ``?token=`` in it is a secret written to disk.

    Masking has a cost: a record whose URL carried a credential cannot be
    replayed. :func:`_url_credentialed` records that fact in the v6 column so a
    retry says so instead of replaying a redacted URL and reporting a 403.
    """
    return redact_url(task.url, strip_query=bool(task.meta.get("redact_url")))


def _url_credentialed(task: DownloadTask) -> bool:
    """Whether persisting this task lost a credential that retry cannot restore.

    A presigned S3 URL is deliberately stored without its query *because* its
    ``s3://`` origin can be re-signed: that record is replayable. Only a task
    whose origin is the same URL (a bare presigned ``https://`` target) has no
    other way back, and only that one is marked unreplayable.
    """
    lost_url = _persisted_url(task) != task.url
    origin = task.origin
    if not origin:
        return lost_url
    lost_origin = redact_url(origin, strip_query=bool(task.meta.get("redact_url"))) != origin
    if lost_origin:
        return True
    if not lost_url:
        return False
    # The URL lost something; the origin is a different, replayable target
    # (``s3://bucket/key``, ``hf://org/repo``) and can produce a fresh URL.
    return origin == task.url


def _tsv(value: object) -> str:
    """Make a value safe for the tab-separated failed log."""
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _failure_class(exc: BaseException) -> str:
    """Classify a resolve/probe/transfer failure for the failed log and JSON.

    Delegates to :func:`downctl.core.errors.classify_failure`: the app used to
    keep its own copy, and the two disagreed about timeouts, local I/O and
    locks. One classifier, one answer.
    """
    return classify_failure(exc)


def _fmt_url(task: DownloadTask, reason: str = FAILURE_UNKNOWN) -> str:
    """Failed-log line for a resolved task (v5).

    Columns: ``url``, printable URL, target, checksum algo/hex, size, source,
    origin, failure class, *header names* (values are never written), transport,
    the host that wrote the record, and (v6) whether the persisted URL lost a
    credential. v6 is a pure append over v5, so an older reader still sees the
    columns it knows.
    """
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
            # ``origin`` is the user-supplied target (``hf://…``, or a bare
            # URL that may carry ``?token=``). It is re-resolved on retry, so
            # the secret is never needed on disk; persist the redacted form.
            redact_url(task.origin, strip_query=bool(task.meta.get("redact_url"))),
            reason,
            ",".join(sorted(task.headers)),
            task.transport,
            _hostname(),
            "1" if _url_credentialed(task) else "",
        )
    )


def _hostname() -> str:
    """This machine's name, recorded so an absolute path is not replayed elsewhere."""
    import socket

    try:
        return socket.gethostname()
    except OSError:  # pragma: no cover - gethostname does not normally fail
        return ""


def _fmt_target(target: str, reason: str = FAILURE_UNKNOWN, error: str = "") -> str:
    """Failed-log line for a target that could not be resolved.

    The target is redacted like any other persisted URL: a bare ``https://...``
    target can carry ``?token=`` just as a resolved one can.
    """
    return "\t".join(
        (
            _tsv("target"),
            _tsv(redact_url(target)),
            _tsv(reason),
            _tsv(redact_text(error)),
            _hostname(),
        )
    )


def _report_early_failures(
    reporter: object,
    lines: list[str],
    error: str,
    *,
    reason: str | None = None,
) -> None:
    """Emit per-target JSON failure records for resolve/probe failures.

    ``reason`` is handed in whenever the caller has the exception in hand. When
    it is ``None`` the class is read from each formatted line instead: a
    ``--continue`` resolve collects several failures with different classes,
    and stamping them all ``unknown`` made the JSON stream disagree with the
    failed log written from the same lines.
    """
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
        classed = reason if reason is not None else _failure_reason(line)
        failure(target, error, url, reason=classed)


def _parse_failed_line(line: str) -> tuple[str, object]:
    """Parse a failed-log line. Returns ``("url", kwargs)`` or ``("target", str)``.

    v1 was a bare target, v2 added six tab-separated fields, v3 appended the
    user-supplied ``origin`` (so S3/presigned and private repos can be
    re-resolved instead of replaying an expired URL), v4 appends the failure
    class, v5 appends the header *names*, the transport and the writer's
    host, and v6 appends whether the URL/origin carried a masked credential.
    All versions are read; every trailing column is optional.
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
            "reason": columns[8] if len(columns) > 8 else "",
            "headers": [
                name for name in (columns[9].split(",") if len(columns) > 9 else []) if name
            ],
            "transport": columns[10] if len(columns) > 10 else "",
            "host": columns[11] if len(columns) > 11 else "",
            # v6: the persisted URL/origin carried a credential that was masked
            # before writing. Such a record is not replayable by design.
            "credentialed": len(columns) > 12 and columns[12] == "1",
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
    if ctx.registry is None:  # pragma: no cover - the app always builds one
        raise DownctlError("internal error: the runtime context has no plugin registry")
    payload = dict(record)
    credentialed = bool(payload.pop("credentialed", False))
    raw_headers = payload.pop("headers", [])
    header_names = (
        [str(name) for name in raw_headers] if isinstance(raw_headers, (list, tuple)) else []
    )
    writer_host = str(payload.pop("host", "") or "")
    payload.pop("reason", "")
    stored = DownloadTask(**payload)  # type: ignore[arg-type]
    if credentialed or "***" in stored.url or "***" in stored.origin:
        # The failed log deliberately does not store credentials. Replaying a
        # redacted URL made the retry hit the server without its token and
        # report a 403 as if the server had changed; say what actually happened
        # and keep the record instead.
        raise DownctlError(
            "this failure's URL carried a credential that is deliberately not "
            "stored in the failed log; re-run the original target (or supply "
            "the credential) instead of --retry-failed"
        )
    if record.get("transport"):
        # v5 records the transport the failed run used. Without it, a local or
        # SFTP record could be handed to the HTTP transport by the default.
        stored.transport = str(record["transport"])
    if stored.checksum_hex:
        stored.meta["user_checksum"] = True  # conservative: re-verify on skip
    if os.path.isabs(stored.target):
        if writer_host and writer_host != _hostname():
            raise DownctlError(
                f"this failure record was written on {writer_host!r}; its absolute "
                f"path {stored.target!r} cannot be replayed on {_hostname() or 'this machine'}"
            )
        stored.allow_absolute = True
    # The failed log never stores header *values* (a token would outlive the
    # run). It stores the names it used, so a retry that no longer supplies one
    # says so — instead of replaying the request without a credential and
    # reporting a 401 as if the server had changed.
    supplied = _parse_headers(getattr(args, "header", []) or [])
    supplied_lower = {key.lower(): value for key, value in supplied.items()}
    missing = [name for name in header_names if name.lower() not in supplied_lower]
    if missing:
        raise DownctlError(
            "this failure was produced with request header(s) "
            f"{', '.join(missing)} whose value is not stored; re-run --retry-failed "
            "with the same -H option(s) to retry it"
        )
    for name in header_names:
        value = supplied_lower.get(name.lower())
        if value is not None:
            stored.headers[name] = value
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
            # The user named a plugin; "no source can handle this URL" hides
            # the typo that actually caused it.
            raise DownctlError(
                f"unknown source plugin: {forced}; "
                f"available: {', '.join(ctx.registry.names('source')) or 'none'}"
            )
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
    retry_kept: list[str] | None = None,
) -> list[DownloadTask]:
    if ctx.registry is None:  # pragma: no cover - the app always builds one
        raise DownctlError("internal error: the runtime context has no plugin registry")
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
    #: Failed-log lines this retry could not replay. They stay in the log
    #: untouched; the count still makes the run fail. Writing a *new* target
    #: line for them (the old behaviour) replaced a header-name record with a
    #: bare path, so the next retry said "no source plugin can handle".
    retry_records: list[tuple[dict[str, object], str]] = []
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
                    retry_records.append((payload, line))
                elif isinstance(payload, str):
                    if "***" in payload:
                        # A bare target with a masked credential cannot be
                        # replayed either; keep the original record.
                        if retry_kept is not None:
                            retry_kept.append(line)
                        _warn(
                            f"cannot replay failed-log record {payload!r}: its URL "
                            "carried a credential that is not stored; re-run the "
                            "original target",
                            always=True,
                        )
                        continue
                    targets.append(payload)

    forced = getattr(args, "source", None)
    registry = ctx.registry

    async def resolve_one(target: str) -> tuple[str, list[DownloadTask]]:
        if forced:
            source = registry.get("source", forced)
            if source is None:
                # "no source plugin can handle: <target>" was technically true
                # and completely unhelpful: the user named a plugin, not a URL.
                raise DownctlError(
                    f"unknown source plugin: {forced}; "
                    f"available: {', '.join(registry.names('source')) or 'none'}"
                )
        else:
            source = registry.select_source(target)
        if source is None:
            raise DownctlError(f"no source plugin can handle: {target}")
        resolved = await source.resolve(target, ctx)  # type: ignore[attr-defined]
        return str(getattr(source, "name", "")), list(resolved)

    # Expansion is I/O bound (one request per file for a repo listing, plus a
    # HEAD per target), so it runs with bounded concurrency instead of paying
    # N round trips before the first byte. Order is preserved for reporting.
    limit = _probe_jobs(cfg)
    resolved_items = await _bounded_map(targets, limit, resolve_one)
    _filter_warned: set[str] = set()
    for target, resolved_outcome in zip(targets, resolved_items, strict=False):
        if isinstance(resolved_outcome, BaseException):
            failures.append(
                _fmt_target(target, _failure_class(resolved_outcome), str(resolved_outcome))
            )
            if not keep_going:
                raise resolved_outcome
            _warn(f"skipping {display_url(target)}: {redact_text(str(resolved_outcome))}")
            continue
        source_name, resolved = resolved_outcome
        if (
            (getattr(args, "include", None) or getattr(args, "exclude", None))
            and getattr(registry.get("source", source_name), "supports_filters", True) is False
            and source_name not in _filter_warned
        ):
            # The source has no listing to filter. Saying nothing made the
            # filters look applied; the files arrive unfiltered either way.
            # Warned once per source, not once per target.
            _filter_warned.add(source_name)
            _warn(
                f"source plugin {source_name!r} does not support --include/--exclude; "
                "the filter is ignored for this target",
                always=True,
            )
        for task in resolved:
            if not task.source:
                task.source = source_name
            _prepare_task(task, target)
            _apply_router(task, cfg, registry)
            tasks.append(task)

    async def retry_one(record: dict[str, object]) -> DownloadTask:
        return await _retry_task(record, args, cfg, ctx)

    retried = await _bounded_map(retry_records, limit, lambda item: retry_one(item[0]))
    for (record, original_line), retry_outcome in zip(retry_records, retried, strict=False):
        if isinstance(retry_outcome, BaseException):
            # One unusable record must not abort the whole replay: the other
            # records are still valid, and losing them turned a single bad line
            # into "nothing was retried". Keep the record as it was written.
            if retry_kept is not None:
                retry_kept.append(original_line)
            label = str(record.get("target") or record.get("url") or "")
            _warn(
                f"skipping failed-log record {label!r}: {redact_text(str(retry_outcome))}",
                always=True,
            )
            continue
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

    A deliverable is matched by its *path in the download*, never by its bare
    name: ``_apply_checksums`` used to accept ``data.bin`` for ``v2/data.bin``,
    attributing one file's digest to another, while ``verify`` correctly
    refused it. The two now use the same rule.
    """
    table = _load_checksums(path)
    if not table:
        raise DownctlError(f"checksum file has no usable entries: {path}")
    missing: list[str] = []
    for task in tasks:
        rel = task.target.replace(os.sep, "/")
        remote = str(task.meta.get("remote_name") or "").replace("\\", "/")
        candidates = [remote, rel]
        if rel.startswith("./"):
            candidates.append(rel[2:])
        entry = next((table[key] for key in candidates if key and key in table), None)
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
        key = key.strip()
        value = value.strip()
        # A newline or NUL in either part is rejected by aiohttp only when the
        # request is sent, i.e. after a batch job's earlier downloads already
        # ran. Refuse it where the header is written instead.
        if any(ord(char) < 32 or ord(char) == 127 for char in key + value):
            raise DownctlError(f"--header must not contain control characters, got {item!r}")
        if any(character in key for character in " \t:"):
            raise DownctlError(f"--header name is not a valid HTTP token: {key!r}")
        headers[key] = value
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


def _witness_label(task: DownloadTask, target: str, done: int) -> str:
    """How a run knows the bytes it already counts as present.

    Reported next to resume/skip numbers so "12 MB done" is never mistaken for
    proof when nothing about the object's version could be checked. It mirrors
    the engine's own rule: a marker counts as ``validator`` only when this run
    actually compared the recorded witness with the one it observed, since a
    dry run promising a comparison the real run cannot make is worse than no
    promise at all.
    """
    if done <= 0:
        return "none"
    observed = _observed_validator(task)
    record = piece_store.load_completion(target)
    if record is not None:
        if piece_store.witness_compared(record, observed):
            return "validator"
        return "checksum" if str(record.get("checksum_hex", "")) else "none"
    bitmap = piece_store.load(target)
    if bitmap is not None:
        return "validator" if (bitmap.validator and observed) else "none"
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

    ``--max-time`` applies *here too*, and the deadline is recorded on the task
    so the transfer inherits whatever the probe did not spend instead of
    starting a fresh budget of its own.
    """
    max_time = int(getattr(ctx.config.core, "max_time", 0) or 0)
    loop = asyncio.get_running_loop()
    deadline = (loop.time() + max_time) if max_time > 0 else None
    task.meta["deadline"] = deadline
    if not task.url.startswith(_HTTP_SCHEMES):
        return
    # ``retries`` counts download retries; a probe is bounded much lower and
    # its backoff is capped so a dead batch cannot stall the whole run.
    attempts = PROBE_ATTEMPT_LIMIT if retries <= 0 else min(retries + 1, PROBE_ATTEMPT_LIMIT)
    timeout_message = f"probe for {_display_url(task)} exceeded --max-time {max_time}s"
    attempt = 0
    while True:
        remaining = None if deadline is None else deadline - loop.time()
        if remaining is not None and remaining <= 0:
            raise ProbeError(timeout_message)
        attempt += 1
        try:
            if remaining is None:
                result = await probe_metadata(
                    ctx.session,
                    task.url,
                    headers=task.headers,
                    policy=ctx.proxy_policy,
                )
            else:
                async with asyncio.timeout(remaining):
                    result = await probe_metadata(
                        ctx.session,
                        task.url,
                        headers=task.headers,
                        policy=ctx.proxy_policy,
                    )
        except TimeoutError:
            raise ProbeError(timeout_message) from None
        if not result.ok and result.status == 403 and refresh_url(task, 403):
            # The link expired before the probe. Renewing here is the difference
            # between "re-signed during the run" and "failed before it started".
            console.verbose(f"{_display_url(task)}: the presigned URL expired; re-signed")
            continue
        if result.ok or not result.retryable:
            break
        if attempt >= attempts:
            reason = result.error or f"HTTP {result.status}"
            raise ProbeError(
                f"probe failed for {_display_url(task)} after {attempt} attempts ({reason})"
            )
        delay = retry_delay(attempt, result.retry_after)
        # A probe is bounded far lower than a download: an hour-long
        # ``Retry-After`` must not park a whole batch, and the cap applies to the
        # server's hint too, not only to our own backoff.
        delay = min(delay, PROBE_BACKOFF_MAX)
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - loop.time()))
        await asyncio.sleep(delay)
    if not result.ok:
        if result.status in _PERMANENT_PROBE_STATUSES:
            # A refused credential is the one 401/403 the status cannot explain:
            # the source may have *withheld* a token on purpose (a mirror is
            # configured), and the user is the only one who can undo that.
            hint = str(task.meta.get("credential_hint") or "")
            raise PermanentHTTPError(
                result.status,
                _display_url(task),
                hint=hint if result.status in (401, 403) else "",
            )
        if result.status is None:  # pragma: no cover - see below
            # ``probe_metadata`` reports every socket-level failure as
            # retryable, so a retryable result never leaves the loop above and
            # this branch is unreachable today. It stays because the invariant
            # lives in another module: if it ever changes, a target must fail
            # loudly here instead of surfacing as "HTTP None".
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
    """Where ``task`` would land, without creating directories.

    The path rule is the engine's (:func:`downctl.core.engine.target_path`), and
    the self-copy refusal is repeated here so a dry run predicts the failure
    instead of promising a copy that would destroy its own source.
    """
    path = target_path(cfg.core.dir, task)
    refuse_self_copy(task, path)
    return path


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
@dataclass
class PreparedChecksums:
    """Checksum validation done before a single byte is fetched."""

    early: tuple[str, str] | None = None
    file: str | None = None


def _prepare_checksums(args: argparse.Namespace, registry: Registry) -> PreparedChecksums:
    """Validate ``--checksum``/``--checksums`` without touching the network.

    Shared by ``get`` and ``batch`` so a job file's digests are checked before
    the first job starts, not when its turn comes.
    """
    request_checksum = getattr(args, "checksum", None)
    checksum_file = getattr(args, "checksums", None)
    if checksum_file:
        checksum_file = _local_path(checksum_file)
    prepared = PreparedChecksums(file=checksum_file)
    if request_checksum:
        algo, _, digest = request_checksum.partition(":")
        if not algo or not digest:
            raise DownctlError("--checksum must be ALGO:HEX, e.g. sha256:...")
        prepared.early = _validate_checksum_spec(algo, digest, registry, "--checksum")
    if checksum_file:
        table = _load_checksums(checksum_file)
        if not table:
            raise DownctlError(f"checksum file has no usable entries: {checksum_file}")
        for algo, digest in table.values():
            _validate_checksum_spec(algo, digest, registry, checksum_file)
    return prepared


@contextlib.asynccontextmanager
async def _session_for(
    session: aiohttp.ClientSession | None,
    *,
    cfg: Config,
    args: argparse.Namespace,
    headers: dict[str, str],
    auth: tuple[str, str] | None,
) -> AsyncIterator[aiohttp.ClientSession]:
    """Yield the shared session, creating one only when the caller has none.

    ``batch`` passes ``None`` for every job: a session carries the job's own
    headers, ``-u`` and User-Agent, so sharing one would apply the first job's
    credentials to the next. ``get`` also lets this helper own the lifecycle.
    """
    if session is not None:
        yield session
        return
    async with make_session(
        stall_limit=cfg.core.stall,
        user_agent=getattr(args, "user_agent", None) or "",
        headers=headers,
        auth=auth,
    ) as created:
        yield created


@dataclass(frozen=True)
class RunOutcome:
    """What one plan did, for ``get``'s exit code and ``batch``'s summary."""

    code: int
    ok: int = 0
    failed: int = 0
    bytes: int = 0
    elapsed: float = 0.0
    total: int = 0


async def _run_plan(
    args: argparse.Namespace,
    cfg: Config,
    registry: Registry,
    events: EventBus,
    reporter: object | None,
    session: aiohttp.ClientSession | None = None,
    *,
    failed_log: str | None = None,
    job: str = "",
) -> RunOutcome:
    """Resolve, probe and download one plan (one ``get`` or one batch job).

    The caller owns the event bus and the transports: ``batch`` runs several
    plans against one registry and one bus, so closing either here made the
    second job emit into a closed bus (or reconnect every transport).
    """
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
    checksums = _prepare_checksums(args, registry)
    early_checksum = checksums.early
    checksum_file = checksums.file
    extra_headers = _parse_headers(getattr(args, "header", []) or [])
    basic_auth = _parse_user(getattr(args, "user", None))

    async with _session_for(
        session, cfg=cfg, args=args, headers=extra_headers, auth=basic_auth
    ) as session:
        ctx = RuntimeContext(
            session=session,
            config=cfg,
            registry=registry,
            events=events,
            rate_limit=cfg.core.rate,
            rate_limiter=RateLimiter(cfg.core.rate),
            options=dict(cfg.plugins),
            proxy_policy=_proxy_policy(cfg),
            job=job,
            params={
                "include": list(getattr(args, "include", []) or []),
                "exclude": list(getattr(args, "exclude", []) or []),
                "revision": getattr(args, "revision", None),
            },
        )
        # The resolver is attached to the session (the one object every
        # request already has) and its material is applied per hop by
        # ``policy_request``. Each job has its own session now, but a caller may
        # still pass a shared one, so the previous run's rules are cleared when
        # this plan has none.
        auth_rules = build_rules(cfg.auth)
        if auth_rules:
            ctx.auth = AuthResolver(auth_rules, registry, ctx)
            setattr(session, AUTH_RESOLVER_ATTR, ctx.auth)
        else:
            setattr(session, AUTH_RESOLVER_ATTR, None)
        failed_log = failed_log or os.path.join(cfg.core.dir, FAILED_LOG_NAME)
        failures: list[str] = []
        #: Failed-log records a retry could not replay. They are not rewritten
        #: (their original line survives the merge) but they still fail the run.
        retry_kept: list[str] = []
        failed_total = 0
        #: The engine's classification of each failure, in the same order as
        #: ``failures``. The summary line uses it to avoid recommending a retry
        #: that cannot help.
        kinds: list[str] = []
        keep_going = bool(getattr(args, "continue_", False))
        tasks: list[DownloadTask] = []
        ok = 0
        elapsed = 0.0
        transferred = 0

        try:
            tasks = await _resolve_targets(
                args, cfg, ctx, failures=failures, keep_going=keep_going, retry_kept=retry_kept
            )
            failed_total = len(failures) + len(retry_kept)
            _report_early_failures(reporter, failures, "resolve failed")
        except Exception as exc:  # noqa: BLE001 - record the target before re-raising
            _report_early_failures(reporter, failures, str(exc), reason=_failure_class(exc))
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

        if job:
            for task in tasks:
                task.meta.setdefault("job", job)

        if extra_headers:
            # User headers travel in the session's origin-bound set, not
            # in ``task.headers`` — which meant the failed log recorded no
            # header names and ``--retry-failed`` replayed a request that
            # needed ``-H Authorization`` without it (a 401, not an
            # explanation). Copying them onto the task makes the name
            # visible to the log and lets an explicit ``-H`` override a
            # source plugin's own header of the same name.
            for task in tasks:
                for key, value in extra_headers.items():
                    for name in [k for k in task.headers if k.lower() == key.lower()]:
                        del task.headers[name]
                    task.headers[key] = value

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
                reason = _failure_class(outcome)
                failures.append(_fmt_url(task, reason))
                _report_early_failures(reporter, [failures[-1]], str(outcome), reason=reason)
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
        # Two URLs that land on the same *path* are one file. Deduping by
        # (url, target) missed the case where two different URLs resolve to
        # the same destination: with ``-j > 1`` both started and the second
        # failed on the target lock for a file the user asked for once. A
        # genuine conflict (different objects, one path) is now named as
        # such instead of surfacing as a lock error.
        unique: dict[str, DownloadTask] = {}
        for task in ready:
            try:
                key = os.path.normcase(os.path.abspath(target_path(cfg.core.dir, task)))
            except DownctlError:
                key = f"{task.url}\0{task.target}"
            existing = unique.get(key)
            if existing is None:
                unique[key] = task
                continue
            same_object = existing.url == task.url or str(
                existing.meta.get("identity") or ""
            ) == str(task.meta.get("identity") or "")
            if same_object:
                continue
            raise DownctlError(
                "two different targets resolve to the same file "
                f"{key}: {_display_url(existing)} and {_display_url(task)}; "
                "rename one with -o or download them into separate directories"
            )
        tasks = list(unique.values())

        if getattr(args, "dry_run", False):
            for task in tasks:
                target = _dry_run_target(cfg, task)
                done = 0
                if not force and task.size > 0:
                    # The same decision the real run makes: this used to
                    # call ``_completed_bytes`` without the witness rule, so
                    # a dry run promised to skip a file the real run would
                    # fetch again.
                    done = _completed_bytes(
                        target,
                        task,
                        cfg.core.connections,
                        cfg.core.piece_size,
                        validator=_observed_validator(task),
                        require_witness=bool(cfg.core.require_witness or task.meta.get("recheck")),
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
                                "witness": _witness_label(task, target, done),
                            },
                            ensure_ascii=False,
                        )
                    )
                else:
                    # A dry run's whole output *is* the plan; routing it through
                    # the quietable channel made ``-q --dry-run`` print nothing
                    # and look like a successful no-op.
                    console.result(f"[dry-run] {_display_url(task)}\n  -> {task.target}  {detail}")
            if failures or retry_kept:
                # A dry run must not mutate the failed log: previously this
                # overwrote the record of unrelated, still-failing targets.
                count = len(failures) + len(retry_kept)
                console.error(f"{count} target(s) failed")
                return RunOutcome(1, failed=count, total=len(tasks))
            return RunOutcome(0, total=len(tasks))

        def on_result(task: DownloadTask, success: bool) -> None:
            if not success:
                # The engine classifies every failure it reports; a missing
                # class is recorded as ``unknown`` rather than quietly
                # presented as if it had been decided.
                kind = str(task.meta.get("failure") or FAILURE_UNKNOWN)
                kinds.append(kind)
                failures.append(_fmt_url(task, kind))

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
            extra_failed=len(failures) + len(retry_kept),
        )
        elapsed = time.monotonic() - started
        transferred = ctx.stats.bytes_transferred

        merged = _merge_failed_log(failed_log, failures, tasks)
        _write_failed_log(failed_log, merged)

    failed_total = len(failures) + len(retry_kept)
    if failed_total:
        # "retry with --retry-failed" is only true when retrying can help. The
        # class is read back from each record, so a resolve/probe failure (which
        # never reaches ``kinds``) is included instead of being treated as
        # "nothing to retry". A record kept because it cannot be replayed is
        # itself permanent for this run: recommending another retry would loop.
        if any(_failure_reason(line) != FAILURE_PERMANENT for line in failures):
            # A batch job writes its failed log under the job's own ``dir``;
            # the bare hint sent the user to the default directory, where the
            # log does not exist. Name the directory whenever this is a job.
            retry = "downctl get --retry-failed"
            if job:
                retry += f" -d {cfg.core.dir}"
            console.error(f"{failed_total} target(s) failed (retry with: {retry})")
        else:
            console.error(
                f"{failed_total} target(s) failed; none of them is retryable "
                "(see .downctl.failed for the reason)"
            )
        return RunOutcome(
            1, ok=ok, failed=failed_total, bytes=transferred, elapsed=elapsed, total=len(tasks)
        )
    if cfg.core.strict_hooks:
        # ``on_finish`` hooks run inside ``download_all``, but hooks with worker
        # threads (checksum/exec/webhook) only report their failures when the
        # bus is closed. Checking before ``aclose`` therefore always read zero
        # for exactly the hooks most likely to fail.
        await events.aclose()
        if events.failures:
            console.error(f"{events.failures} hook failure(s) (core.strict_hooks is on)")
            return RunOutcome(
                1,
                ok=ok,
                failed=events.failures,
                bytes=transferred,
                elapsed=elapsed,
                total=len(tasks),
            )
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
    return RunOutcome(0, ok=ok, failed=0, bytes=transferred, elapsed=elapsed, total=len(tasks))


def _failure_reason(line: str) -> str:
    """The failure class a failed-log line records, or ``unknown``."""
    columns = line.split("\t")
    if len(columns) >= 9 and columns[0] == "url":
        return columns[8] or FAILURE_UNKNOWN
    if len(columns) >= 3 and columns[0] == "target":
        return columns[2] or FAILURE_UNKNOWN
    return FAILURE_UNKNOWN


async def _get(args: argparse.Namespace) -> int:
    """Build the shared run environment, then execute one plan."""
    # The reporter is selected before any ``-v`` line is printed: a
    # machine-readable reporter owns stdout, and a config line emitted first
    # corrupted the JSON stream.
    cfg = _build_config(args, log=False)
    registry = _registry(bool(getattr(args, "local_plugins", False)))
    _warn_unknown_plugin_options(cfg, registry)
    events = EventBus()
    _enable_hooks(args, cfg, registry, events)
    reporter = _select_reporter(args, cfg, registry)
    _log_config(cfg)
    try:
        outcome = await _run_plan(args, cfg, registry, events, reporter)
    finally:
        await events.aclose()
        _close_transports(registry)
    return outcome.code


def cmd_get(args: argparse.Namespace) -> int:
    def run() -> int:
        with interrupt_on_sigterm():
            return asyncio.run(_get(args))

    return _guard(run)


# --------------------------------------------------------------------------- #
# batch
# --------------------------------------------------------------------------- #
class BatchFileError(DownctlError):
    """The batch *file* is invalid; nothing was downloaded (exit 2).

    Kept distinct from ``DownctlError`` so ``batch`` can reserve exit 2 for the
    document: a bad ``[core]`` value or an unknown reporter is a normal run
    error (exit 1), exactly as it is for ``get``.
    """


async def _load_jobs(
    args: argparse.Namespace, registry: Registry, ctx: RuntimeContext
) -> list[Job]:
    """Read the job file with the configured ``JobSource``."""
    path = _local_path(args.file)
    if not os.path.isfile(path):
        raise DownctlError(f"job file not found: {path}")
    forced = getattr(args, "job_source", None)
    if forced:
        source = registry.get("jobsource", forced)
        if source is None:
            raise DownctlError(
                f"unknown job source plugin: {forced}; "
                f"available: {', '.join(registry.names('jobsource')) or 'none'}"
            )
    else:
        source = None
        for name in registry.names("jobsource"):
            candidate = registry.get("jobsource", name)
            try:
                if candidate.can_handle(path):  # type: ignore[attr-defined]
                    source = candidate
                    break
            except Exception as exc:  # noqa: BLE001 - asking must not abort
                _warn(
                    f"job source {name!r} raised while checking the file: {redact_text(str(exc))}",
                    always=True,
                )
        if source is None:
            raise DownctlError(
                f"no job source plugin can handle {path}; "
                f"available: {', '.join(registry.names('jobsource')) or 'none'}"
            )
    jobs = await source.load(path, ctx)  # type: ignore[attr-defined]
    return list(jobs)


def _select_jobs(jobs: list[Job], wanted: Sequence[str]) -> list[Job]:
    if not wanted:
        return jobs
    known = {job.name for job in jobs}
    missing = [name for name in wanted if name not in known]
    if missing:
        raise DownctlError(
            f"no job named {', '.join(missing)}; available: {', '.join(sorted(known)) or 'none'}"
        )
    selected = set(wanted)
    return [job for job in jobs if job.name in selected]


def _job_namespace(base: argparse.Namespace, job: Job) -> argparse.Namespace:
    """The ``get`` namespace one job runs with.

    The batch command's own options are the defaults; the job's options
    override them. Options that describe the batch itself are cleared.
    """
    namespace = argparse.Namespace(**vars(base))
    namespace.targets = list(job.targets)
    namespace.list = None
    namespace.retry_failed = False
    namespace.output = None
    for key, value in job.options.items():
        setattr(namespace, key, value)
    # ``--dry-run`` on the command line is the user's answer for this run: a job
    # file saying ``dry_run = false`` must not turn it back into a real
    # download (reproduced: ``batch --dry-run`` wrote a file).
    if getattr(base, "dry_run", False):
        namespace.dry_run = True
    return namespace


def _validate_job_options(namespace: argparse.Namespace, job: Job, registry: Registry) -> None:
    """Refuse a job whose options would fail *after* earlier jobs ran.

    The TOML/JSON reader checks types of scalars and lists, but a header that
    is not ``Name: value``, a ``-u`` without a colon, a misspelled ``source``
    or a hook that no plugin provides used to surface inside ``_run_plan`` —
    i.e. after the batch had already started downloading the previous job.
    """
    header = getattr(namespace, "header", []) or []
    if not isinstance(header, list) or any(not isinstance(item, str) for item in header):
        raise DownctlError(f"job {job.name!r}: header must be a list of 'Name: value' strings")
    _parse_headers(header)
    user = getattr(namespace, "user", None)
    if user is not None:
        if not isinstance(user, str):
            raise DownctlError(f"job {job.name!r}: user must be a string 'USER:PASSWORD'")
        _parse_user(user)
    user_agent = getattr(namespace, "user_agent", None)
    if user_agent is not None and not isinstance(user_agent, str):
        raise DownctlError(f"job {job.name!r}: user_agent must be a string")
    source = getattr(namespace, "source", None)
    if source and registry.get("source", source) is None:
        raise DownctlError(
            f"job {job.name!r}: unknown source plugin {source!r}; "
            f"available: {', '.join(registry.names('source')) or 'none'}"
        )
    hooks = getattr(namespace, "hook", []) or []
    if not isinstance(hooks, list) or any(not isinstance(name, str) for name in hooks):
        raise DownctlError(f"job {job.name!r}: hook must be a list of plugin names")
    for name in hooks:
        if registry.get("hook", name) is None:
            raise DownctlError(
                f"job {job.name!r}: unknown hook plugin {name!r}; "
                f"available: {', '.join(registry.names('hook')) or 'none'}"
            )


def _preflight_job_checksums(job: Job, namespace: argparse.Namespace) -> None:
    """Catch a manifest that cannot cover this job's direct file targets.

    ``_apply_checksums`` runs on *resolved* tasks, which is after an earlier
    job may have already downloaded. A target that names a file directly has a
    known basename before any source runs, so a missing entry is a job-file
    error and belongs in the pre-download pass. A directory/listing target is
    skipped: its file names exist only after the source expands it.
    """
    path = getattr(namespace, "checksums", None)
    if not path:
        return
    table = _load_checksums(path)
    missing: list[str] = []
    for target in job.targets:
        try:
            parts = urlsplit(target)
        except ValueError:
            continue
        if parts.path.endswith("/"):
            continue
        if parts.scheme.lower() in ("hf", "github"):
            # A repository/tag expands to many files; the resolved names are
            # what the manifest can be matched against.
            continue
        name = unquote(parts.path.rsplit("/", 1)[-1]).replace("\\", "/")
        if not name or name in (".", ".."):
            continue
        if name not in table:
            missing.append(name)
    if missing:
        shown = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        raise DownctlError(f"job {job.name!r}: checksum file has no entry for: {shown} ({path})")


def _plan_jobs(
    jobs: list[Job], base: argparse.Namespace, registry: Registry
) -> list[tuple[Job, argparse.Namespace, Config]]:
    """Validate every job *before* the first download starts (fail fast)."""
    plans: list[tuple[Job, argparse.Namespace, Config]] = []
    for job in jobs:
        namespace = _job_namespace(base, job)
        # Warnings were already printed once for the run; a config typo must
        # not be repeated once per job.
        cfg = _build_config(namespace, log=False, warn=False)
        # Checksums are validated here, so an unreadable manifest in the last
        # job does not surface after the first one already downloaded.
        _prepare_checksums(namespace, registry)
        _preflight_job_checksums(job, namespace)
        _validate_job_options(namespace, job, registry)
        if getattr(namespace, "force", False) and getattr(namespace, "skip_existing", False):
            # Validated here so the conflict is a usage error (exit 2) before any
            # earlier job downloaded; it used to surface inside the job's plan.
            raise DownctlError(
                f"job {job.name!r}: force and skip_existing contradict each other; pick one"
            )
        if getattr(namespace, "checksum", None) and len(job.targets) != 1:
            raise DownctlError(
                f"job {job.name!r}: checksum applies to a single target "
                f"(has {len(job.targets)}); use checksums FILE for multiple targets"
            )
        if getattr(namespace, "output", None) and len(job.targets) != 1:
            raise DownctlError(
                f"job {job.name!r}: output requires exactly one target (has {len(job.targets)})"
            )
        plans.append((job, namespace, cfg))
    return plans


async def _batch(args: argparse.Namespace) -> int:
    cfg = _build_config(args, log=False)
    registry = _registry(bool(getattr(args, "local_plugins", False)))
    _warn_unknown_plugin_options(cfg, registry)
    reporter = _select_reporter(args, cfg, registry)
    _log_config(cfg)

    # Only reading the job file uses this session. Each job then gets its own
    # session built from *its* headers/--user/User-Agent: a shared session
    # applies the first job's credentials to every later job (reproduced: a
    # job that requires ``X-Job`` kept getting 403).
    async with make_session(stall_limit=cfg.core.stall) as loader_session:
        ctx = RuntimeContext(
            session=loader_session,
            config=cfg,
            registry=registry,
            options=dict(cfg.plugins),
            proxy_policy=_proxy_policy(cfg),
        )
        try:
            jobs = _select_jobs(await _load_jobs(args, registry, ctx), list(args.job or []))
        except DownctlError as exc:
            raise BatchFileError(str(exc)) from exc
    if not jobs:
        raise BatchFileError(f"the job file defines no jobs: {args.file}")
    try:
        plans = _plan_jobs(jobs, args, registry)
    except DownctlError as exc:
        raise BatchFileError(str(exc)) from exc
    results: list[tuple[Job, RunOutcome]] = []
    try:
        for job, namespace, job_cfg in plans:
            try:
                if (
                    job_cfg.core.dir
                    and not getattr(namespace, "dry_run", False)
                    and not os.path.isdir(job_cfg.core.dir)
                ):
                    os.makedirs(job_cfg.core.dir, exist_ok=True)
            except OSError as exc:
                # A per-job I/O error is that job's failure. It used to escape
                # the loop (the ``makedirs`` sat outside this ``try``), aborting
                # every remaining job even under ``--continue``.
                console.error(f"job {job.name}: cannot create {job_cfg.core.dir}: {exc}")
                results.append((job, RunOutcome(1)))
                if not getattr(args, "continue_", False):
                    break
                continue
            # ``on_start``/``on_finish`` carry the job name, so a hook or a JSON
            # consumer can tell two jobs of one batch apart. The bus and its
            # hooks belong to the job: registering a job's ``hook`` on a shared
            # bus meant every later job ran it too, and closing the bus after
            # the first job closed hooks the second still needed.
            events = EventBus()
            _enable_hooks(namespace, job_cfg, registry, events)
            console.info(f"job {job.name}: {len(job.targets)} target(s) -> {job_cfg.core.dir}")
            try:
                try:
                    outcome = await _run_plan(
                        namespace,
                        job_cfg,
                        registry,
                        events,
                        reporter,
                        None,
                        # One log per *destination*: a job that sets its own
                        # ``dir`` must be retryable with
                        # ``get --retry-failed -d <that dir>``, and a single log
                        # in the batch directory would record a target relative
                        # to a directory the retry does not use. Jobs sharing a
                        # directory merge into the same log.
                        failed_log=os.path.join(job_cfg.core.dir, FAILED_LOG_NAME),
                        job=job.name,
                    )
                except DownctlError as exc:
                    # A target that fails hard (a probe 404 with no
                    # --continue) is a failed *job*, not an invalid job file:
                    # it already wrote its failed-log record.
                    console.error(redact_text(str(exc)))
                    outcome = RunOutcome(1)
            finally:
                await events.aclose()
            results.append((job, outcome))
            summary = (
                f"  {job.name:20s} {'ok' if outcome.code == 0 else 'failed'}  "
                f"{outcome.ok}/{outcome.total} files, {human_size(outcome.bytes)}"
            )
            if outcome.elapsed >= 0.05:
                summary += f" in {outcome.elapsed:.1f}s"
            console.result(summary)
            if outcome.code != 0 and not getattr(args, "continue_", False):
                break
    finally:
        _close_transports(registry)

    ok_jobs = sum(1 for _job, outcome in results if outcome.code == 0)
    failed_jobs = len(results) - ok_jobs
    skipped = len(plans) - len(results)
    if skipped:
        console.result(f"  ({skipped} job(s) not started)")
    total_bytes = sum(outcome.bytes for _job, outcome in results)
    console.result(
        f"batch: {ok_jobs} ok, {failed_jobs} failed of {len(plans)} job(s), "
        f"{human_size(total_bytes)}"
    )
    if failed_jobs:
        return 1
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    def run() -> int:
        try:
            with interrupt_on_sigterm():
                return asyncio.run(_batch(args))
        except BatchFileError as exc:
            # A job file that cannot be parsed or validated is a *usage* error:
            # nothing was downloaded, so it gets its own exit code instead of
            # being indistinguishable from "one target failed".
            console.error(redact_text(str(exc)))
            return 2
        except DownctlError as exc:
            # A configuration/reporter/registry error is not the job file's
            # fault: ``get`` returns 1 for the same class, and ``batch`` must
            # not tell a script "fix your file" when the file is fine.
            console.error(redact_text(str(exc)))
            return 1

    return _guard(run)


# --------------------------------------------------------------------------- #
# serve / auth
# --------------------------------------------------------------------------- #
def cmd_serve(args: argparse.Namespace) -> int:
    """Serve a local path through the configured ``server`` plugin."""

    async def run() -> int:
        cfg = _build_config(args, log=False)
        registry = _registry(False)
        server = registry.get("server", "http")
        if server is None:
            raise DownctlError("no server plugin named 'http' is available")
        root = _local_path(getattr(args, "path", None) or cfg.core.dir)
        if not os.path.exists(root):
            raise DownctlError(f"nothing to serve: {root} does not exist")
        options: dict[str, object] = {
            "bind": getattr(args, "bind", None) or "127.0.0.1",
            "port": 8080 if getattr(args, "port", None) is None else args.port,
            "index": bool(getattr(args, "index", False)),
            "quiet": bool(getattr(args, "serve_quiet", False)),
        }
        if getattr(args, "auth", None) is not None:
            credentials = str(args.auth)
            if not credentials.strip():
                # ``--auth ""`` used to be falsy and silently disabled
                # authentication; with ``--bind 0.0.0.0`` that served the tree
                # to anyone. An explicitly empty credential is a usage error.
                raise DownctlError(
                    "--auth needs USER:PASS; an empty value would disable authentication"
                )
            options["auth"] = credentials
        async with make_session() as session:
            ctx = RuntimeContext(
                session=session,
                config=cfg,
                registry=registry,
                options=dict(cfg.plugins),
            )
            await server.serve(root, ctx, options)  # type: ignore[attr-defined]
        return 0

    return _guard(lambda: asyncio.run(run()))


def _material_length(material: object) -> int:
    """How many characters of material exist, without printing any of them."""
    headers = getattr(material, "headers", {})
    basic = getattr(material, "basic", None)
    if basic is not None:
        return len(f"{basic[0]}:{basic[1]}")
    return sum(len(str(value)) for value in headers.values())


def cmd_auth(args: argparse.Namespace) -> int:
    """Show which ``[auth]`` rule and provider a host resolves to."""
    if getattr(args, "auth_action", None) != "check":
        # ``auth`` with no subcommand used to reach ``args.urls`` and raise
        # AttributeError, which the guard reported as an internal error.
        console.error("usage: downctl auth check <url>...")
        return 2
    as_json = bool(getattr(args, "json", False))
    console.set_machine_readable(as_json)
    cfg = _build_config(args, log=False)
    registry = _registry(False)
    rules = build_rules(cfg.auth)

    async def run() -> int:
        failed = False
        async with make_session(stall_limit=cfg.core.stall) as session:
            ctx = RuntimeContext(
                session=session, config=cfg, registry=registry, options=dict(cfg.plugins)
            )
            resolver = AuthResolver(rules, registry, ctx)
            for url in args.urls:
                # ``auth check huggingface.co`` must work without a scheme;
                # urlsplit() returns no hostname for a bare host, so the
                # resolver's own parser is the single owner of that decision.
                host = host_from_input(url) or ""
                rule, provider = resolver.describe(host)
                material = None
                error = ""
                if rule is not None:
                    try:
                        material = await resolver.material_for(url)
                    except AuthError as exc:
                        error = redact_text(str(exc))
                record: dict[str, object] = {
                    "url": display_url(url),
                    "host": host,
                    "rule": f'[auth."{rule.pattern}"]' if rule else "",
                    "provider": rule.provider if rule else "",
                    "provider_kind": type(provider).__name__ if provider is not None else "",
                    "material": (
                        {
                            "headers": sorted(material.headers),
                            "basic": bool(material.basic),
                            "source": material.source,
                            "length": _material_length(material),
                        }
                        if material is not None
                        else None
                    ),
                    "error": error,
                }
                if as_json:
                    # ``result`` is silenced in machine-readable mode (it is the
                    # human channel); the JSON answer is printed directly, as
                    # every other ``--json`` command does.
                    print(json.dumps(record, ensure_ascii=False))
                    failed = failed or bool(error)
                    continue
                console.result(f"{record['url']}")
                console.result(f"  host      {host or '(none)'}")
                console.result(f"  rule      {record['rule'] or '(no [auth] rule for this host)'}")
                console.result(
                    f"  provider  {record['provider'] or '-'}"
                    + (f" ({record['provider_kind']})" if record["provider_kind"] else "")
                )
                if error:
                    console.result(f"  material  (failed: {error})")
                    console.error(error)
                    failed = True
                elif material is None:
                    console.result("  material  (none)")
                else:
                    names = ", ".join(sorted(material.headers)) or "basic auth"
                    console.result(
                        f"  material  {names} (len {_material_length(material)}, "
                        f"from {material.source or 'unknown'})"
                    )
        return 1 if failed else 0

    return _guard(lambda: asyncio.run(run()))


async def _probe(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    # ``probe --json`` owns stdout just like the JSON reporter; select it
    # before config logging so ``-v`` cannot prepend a human line.
    console.set_machine_readable(as_json)
    cfg = _build_config(args)
    policy = _proxy_policy(cfg)
    failures = 0
    registry = _registry(False)
    auth_rules = build_rules(cfg.auth)
    async with make_session(stall_limit=STALL_LIMIT) as session:
        if auth_rules:
            # ``probe`` is the one command whose whole job is asking a private
            # server a question; it must use the same credential path as a
            # download or it reports 401 for a host the download can read.
            probe_ctx = RuntimeContext(
                session=session, config=cfg, registry=registry, options=dict(cfg.plugins)
            )
            setattr(session, AUTH_RESOLVER_ATTR, AuthResolver(auth_rules, registry, probe_ctx))
        for target in args.targets:
            if target.startswith(_HTTP_SCHEMES):
                detail = await probe_resource(session, target, policy=policy)
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
            # ``probe_resource`` embeds the request URL in its error text, and
            # the most common probe target is a presigned URL: printing the
            # error verbatim wrote ``?token=`` to a terminal and a CI log.
            shown_error = redact_text(str(detail.get("error") or ""))
            if as_json:
                record = dict(detail)
                record["target"] = shown_target
                record["final_url"] = shown_final
                record["size_human"] = human_size(size)
                record["error"] = shown_error
                print(json.dumps(record, ensure_ascii=False))
            else:
                if isinstance(status, int) and status >= 400:
                    # A 404/403 answered the question: the server is reachable.
                    # The old code called every error "unsupported", including
                    # a real HTTP status.
                    label = str(status)
                elif detail.get("error"):
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
                validator = str(detail.get("validator") or "")
                if validator:
                    lines.append(
                        f"  validator={validator} ({detail.get('validator_kind') or 'unknown'})"
                    )
                elif detail.get("reachable"):
                    lines.append("  validator=(none; the server offers no ETag/Last-Modified)")
                if shown_final != shown_target:
                    lines.append(f"  url={shown_final}")
                if shown_error:
                    lines.append(f"  error={shown_error}")
                    lines.append(f"  retryable={bool(detail.get('retryable'))}")
                console.result("\n".join(lines))
    return 1 if failures else 0


def cmd_probe(args: argparse.Namespace) -> int:
    return _guard(lambda: asyncio.run(_probe(args)))


def cmd_completions(args: argparse.Namespace) -> int:
    shell = getattr(args, "shell", "bash") or "bash"
    print(_completion_script(shell))
    return 0


def _completion_script(shell: str) -> str:
    """Return a dependency-free shell completion script for ``shell``."""
    commands = (
        "get batch probe serve status verify clean plugin config doctor wizard "
        "auth version completions"
    )
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


def collect_status(directory: str) -> tuple[list[Incomplete], list[tuple[str, str]], list[str]]:
    """Scan a directory for in-progress (bitmap), failed and blocked downloads.

    ``blocked`` names targets whose state was written by a newer downctl. They
    used to be omitted entirely, so ``status`` showed nothing at all for a file
    that is sitting there with a ledger this build cannot read.

    ``failed`` carries the reason recorded in the last column: v4 wrote it and
    nothing read it, so ``status`` could not say *why* a target was listed.
    """
    base = Path(directory)
    incomplete: list[Incomplete] = []
    blocked: list[str] = []
    if base.is_dir():
        for bitmap_file in sorted(base.rglob("*" + piece_store.BITMAP_SUFFIX)):
            target = str(bitmap_file)[: -len(piece_store.BITMAP_SUFFIX)]
            if piece_store.future_state(target) is not None:
                blocked.append(os.path.relpath(target, directory))
                continue
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
        # A *marker* from a newer build blocks the target just as a ledger does,
        # and a finished file usually has only the marker left.
        for marker_file in sorted(base.rglob("*" + piece_store.COMPLETION_SUFFIX)):
            if not marker_file.is_file():
                continue
            target = str(marker_file)[: -len(piece_store.COMPLETION_SUFFIX)]
            relative = os.path.relpath(target, directory)
            if relative in blocked:
                continue
            if piece_store.future_state(target) is not None:
                blocked.append(relative)

    failed: list[tuple[str, str]] = []
    failed_log = base / FAILED_LOG_NAME
    if failed_log.is_file():
        with open(failed_log, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                kind, payload = _parse_failed_line(line)
                if kind == "url" and isinstance(payload, dict):
                    failed.append(
                        (
                            str(payload.get("target") or payload.get("url")),
                            str(payload.get("reason") or FAILURE_UNKNOWN),
                        )
                    )
                else:
                    failed.append((str(payload), _failure_reason(line)))
    return incomplete, failed, blocked


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
    from downctl.core.locks import LOCK_MARKER

    for lock_dir in base.rglob("*.lock"):
        if not lock_dir.is_dir():
            continue
        # A ``*.lock`` directory is only a downctl lock when it carries the
        # marker downctl writes. Without this check a user directory named
        # ``build.lock`` that happened to contain a dead ``pid`` file was
        # deleted as "leftover state".
        if not (lock_dir / LOCK_MARKER).is_file():
            continue
        if not _lock_is_live(lock_dir):
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


def find_live_locks(directory: str) -> list[Path]:
    """Lock directories a running downctl still owns (reported, never removed)."""
    base = Path(directory)
    if not base.is_dir():
        return []
    from downctl.core.locks import LOCK_MARKER

    return sorted(
        lock
        for lock in base.rglob("*.lock")
        if lock.is_dir() and (lock / LOCK_MARKER).is_file() and _lock_is_live(lock)
    )


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
    cfg = _build_config(args, log=False)
    directory = _local_path(getattr(args, "path", None) or cfg.core.dir)
    items = find_cleanables(directory)
    live = find_live_locks(directory)
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
                    "live_locks": [str(path) for path in live],
                },
                ensure_ascii=False,
            )
        )
        return 1 if failed else 0
    if live:
        console.result(
            f"{len(live)} lock(s) held by a running process (kept; they are not leftover state)"
        )
        for path in live:
            console.verbose(f"  live lock  {path}")
    if not items:
        console.result(f"nothing to clean in {directory}")
        return 0
    if dry_run:
        for path in items:
            console.result(f"  would remove  {path}")
        console.result(f"{len(items)} item(s) would be removed")
        return 0
    console.result(f"removed {removed} item(s)")
    if failed:
        for record in failed:
            console.error(f"could not remove {record['path']}: {redact_text(record['error'])}")
        return 1
    return 0


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def _blocked_detail(state: str) -> str:
    """Why a target with an unreadable sidecar cannot be judged."""
    name = os.path.basename(state)
    return (
        f"{name} was written by a newer downctl; this build cannot read it, so it "
        "neither proves nor disproves the file"
    )


def _verify_candidates(
    directory: str,
) -> tuple[list[str], list[Incomplete], list[tuple[str, str]]]:
    """Files that claim completion, the still-incomplete ones, and the blocked.

    Only *recognized* completion markers count as a claim: a user data file
    whose name merely ends in the suffix must not be judged (or deleted). A
    sidecar written by a newer build is a claim this build cannot read; it is
    returned separately so ``verify`` can report it instead of silently
    concluding there was nothing to check and exiting 0. Both sidecars count:
    a target whose *ledger* is unreadable was invisible here even though
    ``status`` reported it, so ``verify`` said "nothing to verify" while a file
    it could not judge sat in the directory.
    """
    base = Path(directory)
    claims: list[str] = []
    incomplete: list[Incomplete] = []
    blocked: dict[str, str] = {}
    if not base.is_dir():
        return claims, incomplete, []

    def note_blocked(target: str) -> bool:
        state = piece_store.future_state(target)
        if state is None:
            return False
        blocked.setdefault(os.path.normpath(target), _blocked_detail(state))
        return True

    suffix = piece_store.COMPLETION_SUFFIX
    for marker in sorted(base.rglob("*" + suffix)):
        if not marker.is_file():
            continue
        target = str(marker)[: -len(suffix)]
        if note_blocked(target):
            continue
        if piece_store.load_completion(target) is not None:
            claims.append(target)
    for bitmap_file in sorted(base.rglob("*" + piece_store.BITMAP_SUFFIX)):
        if not bitmap_file.is_file():
            continue
        target = str(bitmap_file)[: -len(piece_store.BITMAP_SUFFIX)]
        if note_blocked(target):
            continue
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
    return claims, incomplete, list(blocked.items())


def _lookup_checksum(table: dict[str, tuple[str, str]], relative: str) -> tuple[str, str] | None:
    """Find the digest a manifest has for ``target``, or ``None``.

    Only *path* keys count. A manifest entry keyed by a bare file name describes
    a file at the manifest's root, so applying it to a file in a subdirectory
    attributes a digest to a file the manifest never named — and ``--repair``
    then deletes that file. Reproduced before this fix: a manifest naming only
    ``data.bin`` caused ``v2/data.bin`` (a different object, absent from the
    manifest) to be reported as a mismatch and removed.

    A file at the manifest's root still matches, because there its relative path
    *is* its bare name. An entry that cannot be attributed unambiguously stays
    unattributed, which reports the file as ``unverifiable`` rather than
    guessing.
    """
    rel = relative.replace(os.sep, "/")
    # The absolute path is deliberately *not* a candidate: an absolute
    # manifest entry is outside the manifest's root, and ``verify`` reports it
    # as "not judged and not deleted". Matching it here judged the same entry
    # twice (an ``ok`` line plus an ``unverifiable`` one) and, with
    # ``--repair``, deleted a file the report said was never judged.
    candidates = [rel]
    if rel.startswith("./"):
        candidates.append(rel[2:])
    for key in candidates:
        if key and key in table:
            return table[key]
    return None


def _manifest_targets(
    directory: str, table: dict[str, tuple[str, str]]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Files a ``--checksums`` manifest names that exist *inside* ``directory``.

    ``verify`` judges files that *claim* completion, but a manifest is a second,
    independent claim about what should be there. Only entries whose file is
    actually present are added — a manifest may legitimately be a superset of
    what was downloaded — and each is resolved exactly, never by bare file name.

    An entry that is absolute, contains ``..`` or *resolves* outside the
    directory is skipped and returned separately with the reason: ``base / key``
    can walk through a symlinked directory even though the key itself has no
    ``..``, and ``--repair`` would then delete a file the manifest never named.
    The caller reports it as unprovable instead of dropping it, so the exit code
    still says "not everything was proven". Silently skipping absolute/``..``
    entries made ``verify --checksums`` print "nothing to verify" and exit 0.
    """
    base = Path(directory)
    root = os.path.realpath(str(base))
    found: list[str] = []
    escaped: list[tuple[str, str]] = []
    for key in table:
        if not key:
            continue
        if os.path.isabs(key) or ".." in Path(key).parts:
            escaped.append(
                (
                    key,
                    f"--checksums entry {key!r} is not a relative path inside "
                    f"{directory}; it was not judged and not deleted",
                )
            )
            continue
        candidate = base / key
        if not candidate.is_file():
            continue
        try:
            inside = os.path.commonpath([os.path.realpath(str(candidate)), root]) == root
        except ValueError:  # different drive (Windows)
            inside = False
        if not inside:
            console.warn(
                f"--checksums names {key!r}, but it resolves outside {directory}; "
                "it is not judged (and never deleted)"
            )
            escaped.append(
                (
                    key,
                    f"--checksums names {key!r}, but it resolves outside {directory}; "
                    "it was not judged and not deleted",
                )
            )
            continue
        found.append(str(candidate))
    return found, escaped


async def _digest_file(path: str, algo: str) -> str:
    """Hex digest of ``path``, or ``""`` when the algorithm is not a hash."""
    if not digest.supported(algo):
        return ""
    return await asyncio.to_thread(digest.hash_file, path, algo)


def _remove_proven_bad(target: str, base: str) -> None:
    """Delete a file that was proven not to be the expected object.

    The deletion is confined to ``base``: a manifest key (or a symlinked
    directory) could otherwise point outside it, and ``--repair`` would delete
    a file the user never asked about.
    """
    root = os.path.realpath(base)
    try:
        inside = os.path.commonpath([os.path.realpath(target), root]) == root
    except ValueError:  # different drive (Windows)
        inside = False
    if not inside:
        raise DownctlError(f"refusing to delete {target}: it resolves outside {base}")
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
    found = _lookup_checksum(table, relative)
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
            return VerifyEntry(
                relative, "ok", "checksum", algo=algo, expected=digest, compared=True
            )
        entry = VerifyEntry(
            relative,
            "mismatch",
            witness,
            algo=algo,
            expected=digest,
            actual=await _digest_file(target, algo),
            detail="content does not match the recorded digest",
            compared=True,
        )
        if repair:
            _remove_proven_bad(target, directory)
            entry.repaired = True
        return entry

    # No digest anywhere: the marker's size/mtime binding is all that can be
    # checked offline. It is a change *detector*, not proof of content, and its
    # two ways of disagreeing are not equally strong.
    stat = os.stat(target)
    if record is not None and record["size"] != stat.st_size:
        # A different length proves the bytes are not the object the marker
        # describes, so this is a genuine mismatch and ``--repair`` may delete.
        entry = VerifyEntry(
            relative,
            "mismatch",
            witness,
            detail="file size changed since the completion marker was written",
        )
        if repair:
            _remove_proven_bad(target, directory)
            entry.repaired = True
        return entry
    if record is not None and record["mtime_ns"] != stat.st_mtime_ns:
        # Same length, different mtime: the file was touched, copied or
        # rewritten, and nothing here can tell whether its *content* changed.
        # Calling that a mismatch let ``--repair`` delete a byte-identical file
        # (reproduced before this fix: sha256 unchanged, directory emptied), so
        # it is reported as unprovable instead, and never repaired.
        return VerifyEntry(
            relative,
            "unverifiable",
            witness,
            detail=(
                "file changed since the completion marker was written, but its "
                "length is unchanged, so its content could not be disproved"
            ),
        )
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
    # Failing open on a mistyped path reported "nothing to verify" and exited 0,
    # which reads exactly like "everything is proven".
    if not os.path.isdir(directory):
        raise DownctlError(f"not a directory: {directory}")

    table: dict[str, tuple[str, str]] = {}
    checksum_file = getattr(args, "checksums", None)
    if checksum_file:
        checksum_file = _local_path(checksum_file)
        table = _load_checksums(checksum_file)
        if not table:
            raise DownctlError(f"checksum file has no usable entries: {checksum_file}")
        for algo, digest in table.values():
            _validate_checksum_spec(algo, digest, registry, checksum_file)

    claims, incomplete, blocked = _verify_candidates(directory)
    # ``--checksums`` also defines *what* must be checked: an entry whose file is
    # present is judged even without a completion marker. Order is stable —
    # marker-carrying claims first, then whatever only the manifest names.
    wanted: dict[str, bool] = {}
    for target in claims:
        wanted.setdefault(os.path.normpath(target), False)
    escaped: list[tuple[str, str]] = []
    if table:
        manifest_files, escaped = _manifest_targets(directory, table)
        for target in manifest_files:
            wanted.setdefault(os.path.normpath(target), True)
    entries: list[VerifyEntry] = []
    repaired = 0
    #: Targets this run actually judged. A target can be named by a manifest
    #: *and* carry a sidecar from a newer build; reporting it twice (once
    #: ``ok``, once ``unverifiable``) made the summary contradict itself.
    judged: set[str] = set()
    async with make_session(stall_limit=STALL_LIMIT) as session:
        ctx = RuntimeContext(
            session=session, config=cfg, registry=registry, proxy_policy=_proxy_policy(cfg)
        )
        for target, from_manifest in wanted.items():
            try:
                entry = await _verify_one(ctx, directory, target, table, repair)
            except DownctlError as exc:
                # Refusing to delete is the correct answer for a sidecar this
                # build cannot read — but it must not abort the files after it.
                entry = VerifyEntry(
                    os.path.relpath(target, directory).replace(os.sep, "/"),
                    "unverifiable",
                    "none",
                    detail=f"{exc}",
                )
            entry.from_manifest = from_manifest
            if entry.repaired:
                repaired += 1
            judged.add(os.path.normpath(target))
            entries.append(entry)

    # A sidecar from a newer build is a claim this build cannot judge. Saying
    # "nothing to verify" and exiting 0 was indistinguishable from "everything
    # is proven"; it is unverifiable by construction, so it is counted as such
    # and the run exits 2.
    for target, detail in blocked:
        if os.path.normpath(target) in judged:
            continue
        judged.add(os.path.normpath(target))
        entries.append(
            VerifyEntry(
                os.path.relpath(target, directory).replace(os.sep, "/"),
                "unverifiable",
                "none",
                detail=detail,
            )
        )
    for key, detail in escaped:
        entries.append(
            VerifyEntry(
                key,
                "unverifiable",
                "none",
                detail=detail,
                from_manifest=True,
            )
        )

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
        console.result(f"downctl verify — {directory}")
        if not entries:
            console.result(
                "  nothing to verify: no completion marker, and no manifest entry present"
            )
        for entry in entries:
            detail = f"  ({entry.detail})" if entry.detail else ""
            algo = f" {entry.algo}" if entry.algo else ""
            verb = "repaired " if entry.repaired else ""
            source = "  [named by --checksums]" if entry.from_manifest else ""
            # ``verify`` can compare a digest but never contacts the server, so
            # a version witness comes from the marker: say so instead of
            # printing ``witness=validator`` as if this run had checked it.
            witness = entry.witness
            if witness == "validator" and not entry.compared:
                witness += " (recorded, not compared with the server)"
            console.result(
                f"  {verb}{entry.status:12s} {entry.target}  "
                f"witness={witness}{algo}{source}{detail}"
            )
        if entries:
            console.result(
                f"  {counts['ok']} proven, {counts['mismatch']} mismatch, "
                f"{counts['missing']} missing, {counts['unverifiable']} unverifiable"
            )
        if incomplete:
            console.verbose(f"  {len(incomplete)} file(s) still incomplete (see: downctl status)")
        if counts["unverifiable"] and not table:
            console.result("  hint: --checksums FILE turns unverifiable files into proof")

    if counts["mismatch"] or counts["missing"]:
        return 1
    if counts["unverifiable"]:
        # 3, not 2: argparse uses 2 for a usage error, so ``verify`` returning
        # 2 for "could not prove it" made the two indistinguishable to a script.
        return 3
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
    cfg = _build_config(args, log=False)
    directory = _local_path(getattr(args, "path", None) or cfg.core.dir)
    incomplete, failed_items, blocked = collect_status(directory)
    # Legacy failed-log records may be the raw URL a target was resolved from;
    # never print a credential from one. Plain paths pass through unchanged.
    failed = [{"target": display_url(target), "reason": reason} for target, reason in failed_items]

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
                {
                    "directory": directory,
                    "incomplete": records,
                    "failed": failed,
                    "blocked": blocked,
                },
                ensure_ascii=False,
            )
        )
        # A machine asking "is anything wrong here?" must be able to answer
        # from the exit code: this used to return 0 while listing failures.
        return 1 if (failed or blocked) else 0

    console.result(f"downctl status — {directory}")
    if not incomplete and not failed and not blocked:
        console.result("  no incomplete or failed downloads")
        return 0

    for index, item in enumerate(incomplete):
        age_text = ""
        age_seconds = ages[index]
        if age_seconds is not None:
            age_text = f"  last modified {human_age(float(age_seconds))}"
        console.result(
            f"  incomplete  {item.target}  "
            f"{item.done}/{item.pieces} pieces ({records[index]['percent']}%)  "
            f"{human_size(float(item.size))}{age_text}"
        )
    for failure in failed:
        reason = str(failure["reason"])
        console.result(f"  failed      {failure['target']}  [{reason}]")
    for target in blocked:
        console.result(
            f"  blocked     {target}  (resume state was written by a newer downctl; "
            "upgrade to resume it)"
        )
    return 1 if (failed or blocked) else 0


def _detect_category(plugin: object) -> str | None:
    """The category whose structural protocol ``plugin`` satisfies."""
    from downctl.plugin.registry import CATEGORIES

    for category, protocol in CATEGORIES.items():
        if isinstance(plugin, protocol):
            return category
    return None


def _plugin_check(target: str) -> int:
    """Load a plugin and run the registry's contract checks on it."""
    from downctl.plugin.loader import load_target
    from downctl.plugin.testing import capabilities, check_plugin, option_names

    try:
        entries = load_target(target)
    except Exception as exc:  # noqa: BLE001 - this command reports, never tracebacks
        _errors(f"cannot load {target!r}: {redact_text(str(exc))}")
        return 1
    if not entries:
        _errors(f"no plugins found in {target!r}")
        return 1
    failed = False
    for category, label, plugin in entries:
        detected = category or _detect_category(plugin)
        if detected is None:
            # Report the closest protocol's problems instead of only "no
            # protocol matched": the author needs to know *what* is missing.
            from downctl.plugin.registry import CATEGORIES

            best = min(CATEGORIES, key=lambda name: len(check_plugin(plugin, name)))
            console.result(f"{label}: does not satisfy any known plugin protocol")
            for problem in check_plugin(plugin, best):
                console.result(f"  FAIL        {problem}")
            failed = True
            continue
        problems = check_plugin(plugin, detected)
        console.result(f"{detected:10s} {getattr(plugin, 'name', label)!r}  ({label})")
        console.result(f"  origin      {target}")
        console.result(f"  api_version {getattr(plugin, 'api_version', '?')}")
        console.result(f"  priority    {getattr(plugin, 'priority', 0)}")
        opts = option_names(plugin)
        if opts:
            console.result(f"  options     {', '.join(opts)}")
        caps = capabilities(plugin, detected)
        if caps:
            console.result(f"  capabilities {', '.join(caps)}")
        for problem in problems:
            console.result(f"  FAIL        {problem}")
            failed = True
        if not problems:
            console.result("  OK")
    return 1 if failed else 0


def cmd_plugin(args: argparse.Namespace) -> int:
    console.set_machine_readable(False)
    action = getattr(args, "plugin_action", None)
    if action == "check":
        return _plugin_check(args.target)
    if action == "new":
        from downctl.plugin.scaffold import scaffold

        try:
            files = scaffold(args.name, args.kind, getattr(args, "dir", "."))
        except DownctlError as exc:
            _errors(str(exc))
            return 1
        for path in files:
            console.result(path)
        console.result(
            f"created {len(files)} file(s); next: pip install -e {args.dir}/{args.name}_plugin"
        )
        return 0
    registry = _registry(bool(getattr(args, "local_plugins", False)))
    if action == "info":
        found = False
        for category, plugins in registry.items():
            if args.name in plugins:
                found = True
                from downctl.plugin.testing import capabilities, option_names

                plugin = plugins[args.name]
                console.result(
                    f"{category}: {args.name} (priority={getattr(plugin, 'priority', 0)})"
                )
                console.result(f"  origin      {registry.origin_of(category, args.name)}")
                console.result(f"  api_version {getattr(plugin, 'api_version', '?')}")
                opts = option_names(plugin)
                if opts:
                    console.result(f"  options     {', '.join(opts)}")
                caps = capabilities(plugin, category)
                if caps:
                    console.result(f"  capabilities {', '.join(caps)}")
        if not found:
            _errors(f"no plugin named {args.name!r}")
            return 1
        return 0
    for category, plugins in registry.items():
        names = ", ".join(sorted(plugins)) or "-"
        console.result(f"{category:10s} {names}")
    return 0


def _core_option_lines(core: CoreConfig) -> list[str]:
    """``config show`` lines for *every* core option, derived from the dataclass.

    The list used to be hand-written, so ``max_time``, ``probe_jobs`` and
    ``require_witness`` were added in 0.1.4 without ever appearing in the output
    a user checks against their file. Iterating ``CoreConfig`` removes the drift
    itself rather than the three lines that happened to drift: a new option is
    printed with its plain value whether or not anyone remembers to format it.
    """
    mirror = {display_url(key): display_url(url) for key, url in core.mirror.items()}
    formatted: dict[str, object] = {
        "dir": core.dir or DEFAULT_DIR,
        "proxy": display_url(core.proxy) or "(none)",
        "piece_size": core.piece_size or "auto",
        "rate": core.rate if core.rate is not None else "(unlimited)",
        "mirror": mirror or "(none)",
        "hooks": core.hooks or "(none)",
    }
    return [
        f"{info.name:15s} = {formatted.get(info.name, getattr(core, info.name))}"
        for info in dataclass_fields(core)
    ]


def cmd_config(args: argparse.Namespace) -> int:
    console.set_machine_readable(False)
    action = getattr(args, "config_action", None) or "show"
    if action == "path":
        console.result(config_path())
        return 0
    if action == "init":
        try:
            written = write_default_config(force=getattr(args, "force", False))
        except FileExistsError as exc:
            _errors(str(exc))
            return 1
        console.result(f"wrote {written}")
        return 0
    # The *effective* config: defaults, file, environment and CLI overrides as
    # one run will see them. Reading the file alone showed values a run may
    # never use (an overridden dir, an environment proxy) and looked
    # authoritative while disagreeing with the next command.
    cfg = _build_config(args, log=False)
    console.result(f"# {config_path()}")
    for line in _core_option_lines(cfg.core):
        console.result(line)
    for name, options in cfg.plugins.items():
        console.result(f"[plugins.{name}] {mask_mapping(options)}")
    for host, options in cfg.auth.items():
        # ``command`` is the one auth option whose value is a shell command and
        # may embed a token (``command = "echo ghp_..."``). Hiding the whole
        # value is the only safe display: scanning a command line for
        # "secret-looking" substrings would still print the secret it missed.
        console.result(f'[auth."{host}"] {mask_mapping(options, extra_secrets=("command",))}')
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    console.set_machine_readable(False)
    registry = _registry(False)
    console.result(f"downctl  {__version__}")
    console.result(f"python   {platform.python_version()}")
    console.result(f"platform {platform_kind()}")
    try:
        import aiohttp

        console.result(f"aiohttp  {aiohttp.__version__}")
    except ImportError:  # pragma: no cover
        console.result("aiohttp  NOT INSTALLED")
    try:
        import tqdm

        console.result(f"tqdm     {tqdm.__version__}")
    except ImportError:  # pragma: no cover
        console.result("tqdm     NOT INSTALLED")
    console.result(f"config   {config_path()}")
    console.result(
        f"plugins  {len(registry)} loaded ({', '.join(registry.names('source')) or '-'} sources)"
    )
    # One owner for the decision: ``_proxy_policy`` is what every request in a
    # run consults. Re-deriving the precedence here (as this command used to)
    # was a second answer to "which proxy am I using", and it could disagree
    # with the run while looking authoritative. ``_build_config`` is the owner:
    # it is the one place that reads the environment and the WSL guess, so this
    # command now reports the proxy a run would actually use *and* where it came
    # from — ``proxy_source`` is only ever assigned there, and ``load_config``
    # alone left it empty, so ``doctor`` reported "(none detected)" while the
    # run was using an environment proxy.
    doctor_cfg = _build_config(args, log=False)
    policy = _proxy_policy(doctor_cfg)
    console.result(
        f"proxy    {display_url(policy.url) or '(none detected)'}"
        f"  source={doctor_cfg.proxy_source or 'none'}  scope={policy.scope}"
    )
    # Which credential a run will actually use — a 401 is usually explained by
    # "a different token is the one being picked up" rather than by the URL.
    for name, envs in (
        ("huggingface", credentials.HF_TOKEN_ENVS),
        ("github", credentials.GITHUB_TOKEN_ENVS),
    ):
        found = credentials.resolve(doctor_cfg.plugins.get(name, {}), "token", envs)
        if found:
            console.result(f"{name:8s} token from {found.source} ({found.origin})")
        else:
            console.result(f"{name:8s} token (none)")
    # The per-host rules a run would consult, and whether the provider each one
    # names actually exists. "Why is this host unauthenticated" is answered here
    # without running a download.
    rules = build_rules(doctor_cfg.auth)
    if not rules:
        console.result("auth     (no [auth] rules configured)")
    else:
        console.result(f"auth     {len(rules)} host rule(s)")
        for rule in rules:
            provider = registry.get("auth", rule.provider)
            state = type(provider).__name__ if provider is not None else "MISSING PROVIDER"
            console.result(f'         [auth."{rule.pattern}"] -> {rule.provider} ({state})')
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
    """The namespace a bare ``downctl get`` produces.

    Derived from the parser rather than written out a second time: the wizard
    used a hand-copied literal, so a new ``get`` option was ``None`` there
    forever and the two could disagree about which options exist at all.
    """
    from downctl import cli

    return cli.build_parser().parse_args(["get"])


def _guard(action: Callable[[], int]) -> int:
    """Run ``action`` translating errors into clean messages and exit codes."""
    try:
        return action()
    except DownctlError as exc:
        console.error(redact_text(str(exc)))
        return 1
    except KeyboardInterrupt:
        # A single-stream fallback has no ledger to resume from, so "progress is
        # kept" was a promise only half the pipeline may make. Say what is true.
        print(
            "\ninterrupted; re-run the same command to continue "
            "(single-stream downloads restart from the beginning)",
            file=sys.stderr,
        )
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
