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
import os
import platform
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from downctl import __version__, console
from downctl.config import Config, config_path, load_config, write_default_config
from downctl.core import pieces as piece_store
from downctl.core.context import RuntimeContext
from downctl.core.engine import download_all
from downctl.core.errors import DownctlError, PermanentHTTPError
from downctl.core.ratelimit import RateLimiter
from downctl.core.task import DownloadTask
from downctl.platform_utils import (
    detect_wsl_proxy,
    human_size,
    parse_rate,
    pid_alive,
    platform_kind,
    stdin_is_tty,
    stdout_is_tty,
)
from downctl.plugin.hooks import EventBus
from downctl.plugin.loader import discover
from downctl.plugin.registry import Registry
from downctl.probe import probe_size
from downctl.session import make_session

DEFAULT_DIR = str(Path.home() / "Downloads")
FAILED_LOG_NAME = ".downctl.failed"
STALL_LIMIT = 30
_HEX_RE = re.compile(r"[0-9a-fA-F]+")


@dataclass
class Incomplete:
    """A partially downloaded file discovered by ``downctl status``."""

    target: str
    done: int
    pieces: int
    size: int


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _errors(message: str) -> None:
    console.error(message)


def _warn(message: str) -> None:
    console.warn(message)


def _build_config(args: argparse.Namespace) -> Config:
    """Merge the config file with command-line overrides."""
    cfg = load_config()

    if getattr(args, "dir", None):
        cfg.core.dir = args.dir
    if getattr(args, "jobs", None) is not None:
        cfg.core.jobs = args.jobs
    if getattr(args, "connections", None) is not None:
        cfg.core.connections = args.connections
    if getattr(args, "stall", None) is not None:
        cfg.core.stall = args.stall
    if getattr(args, "retries", None) is not None:
        cfg.core.retries = args.retries
    if getattr(args, "proxy", None) is not None:
        cfg.core.proxy = args.proxy
    if getattr(args, "limit_rate", None):
        cfg.core.rate = parse_rate(args.limit_rate)
    if getattr(args, "mirror", None):
        source, _, dest = args.mirror.partition("=")
        if not dest:
            raise DownctlError("--mirror must be FROM=TO, e.g. https://a=https://b")
        cfg.core.mirror[source] = dest

    if not cfg.core.dir:
        cfg.core.dir = DEFAULT_DIR
    cfg.core.dir = os.path.expanduser(cfg.core.dir)

    if not cfg.core.proxy:
        cfg.core.proxy = (
            os.environ.get("https_proxy")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("http_proxy")
            or os.environ.get("HTTP_PROXY")
            or ""
        )
    cfg.core.proxy = detect_wsl_proxy(cfg.core.proxy)
    return cfg


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


def _apply_router(task: DownloadTask, cfg: Config, registry: Registry) -> None:
    if not cfg.core.mirror:
        return
    router = registry.get("router", "mirror")
    if router is None:
        return
    router.mappings = dict(cfg.core.mirror)  # type: ignore[attr-defined]
    task.url = router.rewrite(task.url)  # type: ignore[attr-defined]


def _display_url(task: DownloadTask) -> str:
    """URL for logs; strips the query for presigned/credential-bearing URLs."""
    if task.meta.get("redact_url"):
        return task.url.split("?", 1)[0]
    return task.url


def _fmt_url(task: DownloadTask) -> str:
    """Failed-log v2 line for a resolved task (preserves subpath + checksum)."""
    return "\t".join(
        [
            "url",
            _display_url(task),
            task.target,
            task.checksum_algo,
            task.checksum_hex,
            str(task.size),
            task.source,
        ]
    )


def _fmt_target(target: str) -> str:
    """Failed-log line for a target that could not be resolved."""
    return f"target\t{target}"


def _parse_failed_line(line: str) -> tuple[str, object]:
    """Parse a failed-log line. Returns ``("url", kwargs)`` or ``("target", str)``."""
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
        }
    if len(columns) >= 2 and columns[0] == "target":
        return "target", columns[1]
    return "target", columns[0]  # legacy single-column format: a plain URL/target


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
        if not os.path.isfile(list_file):
            raise DownctlError(f"target list file not found: {list_file}")
        with open(list_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    targets.append(line)

    tasks: list[DownloadTask] = []
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
                    task = DownloadTask(**payload)
                    if os.path.isabs(task.target):
                        task.allow_absolute = True
                    _apply_router(task, cfg, ctx.registry)
                    tasks.append(task)
                elif isinstance(payload, str):
                    targets.append(payload)

    forced = getattr(args, "source", None)
    for target in targets:
        try:
            source = (
                ctx.registry.get("source", forced) if forced else ctx.registry.select_source(target)
            )
            if source is None:
                raise DownctlError(f"no source plugin can handle: {target}")
            resolved = await source.resolve(target, ctx)  # type: ignore[attr-defined]
        except DownctlError as exc:
            if not keep_going:
                raise
            _warn(f"skipping {target}: {exc}")
            failures.append(_fmt_target(target))
            continue
        for task in resolved:
            if not task.source:
                task.source = getattr(source, "name", "")
            _apply_router(task, cfg, ctx.registry)
            tasks.append(task)
    return tasks


def _load_checksums(path: str) -> dict[str, tuple[str, str]]:
    """Parse a checksum file (``sha256sum`` style) into ``name -> (algo, hex)``.

    Accepts ``<hex>  <name>``, ``<hex> *<name>`` and ``<algo>:<hex>  <name>``.
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
                algo, hex_value = "sha256", digest
            if not hex_value or not _HEX_RE.fullmatch(hex_value):
                continue  # malformed digest; skip rather than mis-verify
            table[name] = (algo, hex_value)
    return table


def _apply_checksums(tasks: list[DownloadTask], path: str) -> None:
    table = _load_checksums(path)
    for task in tasks:
        rel = task.target.replace(os.sep, "/")
        entry = table.get(rel) or table.get(os.path.basename(rel))
        if entry is not None:
            task.checksum_algo, task.checksum_hex = entry


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


async def _probe_task(ctx: RuntimeContext, task: DownloadTask) -> int:
    """Fill in ``task.size`` from the network. Returns the HTTP status."""
    if task.size > 0 or not task.url.startswith(("http://", "https://")):
        return 200
    size, status = await probe_size(ctx.session, task.url)
    task.size = size
    return status or 0


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
async def _get(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    if not (
        getattr(args, "targets", None)
        or getattr(args, "list", None)
        or getattr(args, "retry_failed", False)
    ):
        raise DownctlError("no targets given")
    registry = _registry(getattr(args, "local_plugins", False))
    events = EventBus()
    _enable_hooks(args, cfg, registry, events)
    reporter = _select_reporter(args, cfg, registry)

    request_checksum = getattr(args, "checksum", None)

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

        try:
            tasks = await _resolve_targets(args, cfg, ctx, failures=failures, keep_going=keep_going)
            os.makedirs(cfg.core.dir, exist_ok=True)

            if getattr(args, "output", None):
                if len(tasks) != 1:
                    raise DownctlError("-o/--output requires exactly one target")
                tasks[0].target = os.path.abspath(args.output)
                tasks[0].allow_absolute = True

            if request_checksum:
                algo, _, digest = request_checksum.partition(":")
                if not digest:
                    raise DownctlError("--checksum must be ALGO:HEX, e.g. sha256:...")
                for task in tasks:
                    task.checksum_algo, task.checksum_hex = algo, digest

            checksum_file = getattr(args, "checksums", None)
            if checksum_file:
                _apply_checksums(tasks, checksum_file)

            ready: list[DownloadTask] = []
            for task in tasks:
                try:
                    status = await _probe_task(ctx, task)
                    if status in (401, 403, 404):
                        raise PermanentHTTPError(status, task.url)
                    if status >= 400:
                        raise DownctlError(f"probe failed: HTTP {status} for {task.url}")
                    if status == 0 and task.url.startswith(("http://", "https://")):
                        raise DownctlError(f"probe failed for {task.url}")
                except DownctlError as exc:
                    if not keep_going:
                        raise
                    _warn(f"skipping {task.url}: {exc}")
                    failures.append(_fmt_url(task))
                    continue
                ready.append(task)
            tasks = ready

            if getattr(args, "dry_run", False):
                for task in tasks:
                    pending = task.size if task.size > 0 else 0
                    console.info(
                        f"[dry-run] {_display_url(task)}\n  -> {task.target}  {human_size(pending)}"
                    )
                if failures:
                    with open(failed_log, "w", encoding="utf-8") as fh:
                        for line in failures:
                            fh.write(line + "\n")
                    console.error(f"{len(failures)} target(s) failed")
                    return 1
                return 0

            def on_result(task: DownloadTask, ok: bool) -> None:
                if not ok:
                    failures.append(_fmt_url(task))

            ok, _failed = await download_all(
                ctx,
                tasks,
                jobs=cfg.core.jobs,
                reporter=reporter,  # type: ignore[arg-type]
                connections=cfg.core.connections,
                stall_limit=cfg.core.stall,
                on_result=on_result,
            )

            merged = _merge_failed_log(failed_log, failures, tasks)
            with open(failed_log, "w", encoding="utf-8") as fh:
                for line in merged:
                    fh.write(line + "\n")
        finally:
            await events.aclose()
            _close_transports(registry)

    if failures:
        console.error(f"{len(failures)} target(s) failed (retry with: downctl get --retry-failed)")
        return 1
    console.info(f"done: {ok}/{len(tasks)} files")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    return _guard(lambda: asyncio.run(_get(args)))


async def _probe(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    async with make_session(cfg.core.proxy, "", stall_limit=STALL_LIMIT) as session:
        for target in args.targets:
            size, status = await probe_size(session, target)
            label = status if status is not None else "unreachable"
            console.info(f"{target}\n  status={label}  size={human_size(size)}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    return _guard(lambda: asyncio.run(_probe(args)))


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


def find_cleanables(directory: str) -> list[Path]:
    """Return leftover artifact paths (bitmaps, failed log, lock dirs)."""
    base = Path(directory)
    if not base.is_dir():
        return []
    found: set[Path] = set()
    for pattern in (
        f"*{piece_store.BITMAP_SUFFIX}",
        f"*{piece_store.BITMAP_SUFFIX}.tmp",
    ):
        found.update(base.rglob(pattern))
    for lock_dir in base.rglob("*.lock"):
        if lock_dir.is_dir() and not _lock_is_live(lock_dir):
            found.add(lock_dir)
    failed = base / FAILED_LOG_NAME
    if failed.is_file():
        found.add(failed)
    return sorted(found)


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
    cfg = _build_config(args)
    directory = getattr(args, "path", None) or cfg.core.dir
    items = find_cleanables(directory)
    if not items:
        console.info(f"nothing to clean in {directory}")
        return 0
    dry_run = getattr(args, "dry_run", False)
    for path in items:
        if dry_run:
            console.info(f"  would remove  {path}")
            continue
        with contextlib.suppress(OSError):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink()
    if dry_run:
        console.info(f"{len(items)} item(s) would be removed")
    else:
        console.info(f"removed {len(items)} item(s)")
    return 0


def _failed_line_identity(line: str) -> str:
    """The key a failed-log line is de-duplicated by (its URL or target)."""
    columns = line.split("\t")
    return columns[1] if len(columns) >= 2 and columns[0] in ("url", "target") else columns[0]


def _merge_failed_log(failed_log: str, failures: list[str], tasks: list[DownloadTask]) -> list[str]:
    """Preserve previous failures for targets not touched by this run."""
    processed = {_failed_line_identity(line) for line in failures}
    processed.update(task.url for task in tasks)
    kept: list[str] = []
    if os.path.isfile(failed_log):
        with open(failed_log, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    continue
                if _failed_line_identity(line) not in processed:
                    kept.append(line)
    return kept + failures


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    directory = getattr(args, "path", None) or cfg.core.dir
    console.info(f"downctl status — {directory}")
    incomplete, failed = collect_status(directory)

    if not incomplete and not failed:
        console.info("  no incomplete or failed downloads")
        return 0

    for item in incomplete:
        pieces = item.pieces or 1
        percent = item.done * 100 // pieces
        console.info(
            f"  incomplete  {item.target}  "
            f"{item.done}/{item.pieces} pieces ({percent}%)  "
            f"{human_size(float(item.size))}"
        )
    for target in failed:
        console.info(f"  failed      {target}")
    return 0


def cmd_plugin(args: argparse.Namespace) -> int:
    registry = _registry(True)
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
    console.info(f"proxy   = {cfg.core.proxy or '(none)'}")
    console.info(f"rate    = {cfg.core.rate}")
    if cfg.core.mirror:
        console.info(f"mirror  = {cfg.core.mirror}")
    if cfg.core.hooks:
        console.info(f"hooks   = {cfg.core.hooks}")
    for name, options in cfg.plugins.items():
        console.info(f"[plugins.{name}] {options}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:  # noqa: ARG001
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
    proxy = detect_wsl_proxy(os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY") or "")
    console.info(f"proxy    {proxy or '(none detected)'}")
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
        console.error(str(exc))
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; progress is kept, re-run to resume", file=sys.stderr)
        return 130
    except (aiohttp.ClientError, OSError) as exc:
        console.error(f"network error: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - keep the CLI from dumping tracebacks
        if console.get_level() >= console.VERBOSE:
            raise
        console.error(f"{type(exc).__name__}: {exc} (use -v for a traceback)")
        return 1
