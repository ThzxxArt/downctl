"""Application orchestration for downctl.

Each ``cmd_*`` function maps to a CLI subcommand and receives the parsed
``argparse.Namespace``. They return a process exit code.

The download flow is: build config -> discover plugins -> resolve each target
into tasks via a source plugin -> probe sizes -> run the engine.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys
from collections.abc import Callable
from pathlib import Path

from downctl import __version__
from downctl.config import Config, config_path, load_config, write_default_config
from downctl.core.context import RuntimeContext
from downctl.core.engine import download_all
from downctl.core.errors import DownctlError, PermanentHTTPError
from downctl.core.task import DownloadTask
from downctl.platform_utils import (
    detect_wsl_proxy,
    human_size,
    parse_rate,
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


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _errors(message: str) -> None:
    print(f"downctl: error: {message}", file=sys.stderr)


def _build_config(args: argparse.Namespace) -> Config:
    """Merge the config file with command-line overrides."""
    cfg = load_config()

    if getattr(args, "dir", None):
        cfg.core.dir = args.dir
    if getattr(args, "jobs", None) is not None:
        cfg.core.jobs = args.jobs
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
    if not name:
        use_bar = cfg.core.jobs <= 1 and stdout_is_tty() and os.environ.get("NO_COLOR") != "1"
        tqdm_enabled = bool(cfg.plugins.get("tqdm", {}).get("enabled", True))
        name = "tqdm" if (use_bar and tqdm_enabled) else "quiet"
    reporter = registry.get("reporter", name)
    if reporter is None:
        raise DownctlError(f"unknown reporter plugin: {name}")
    return reporter


def _apply_router(task: DownloadTask, cfg: Config, registry: Registry) -> None:
    if not cfg.core.mirror:
        return
    router = registry.get("router", "mirror")
    if router is None:
        return
    router.mappings = dict(cfg.core.mirror)  # type: ignore[attr-defined]
    task.url = router.rewrite(task.url)  # type: ignore[attr-defined]


async def _resolve_targets(
    args: argparse.Namespace, cfg: Config, ctx: RuntimeContext
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

    if getattr(args, "retry_failed", False):
        failed_log = os.path.join(cfg.core.dir, FAILED_LOG_NAME)
        if not os.path.isfile(failed_log):
            raise DownctlError(f"no failed log to retry: {failed_log}")
        with open(failed_log, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    targets.append(line)

    if not targets:
        raise DownctlError("no targets given")

    forced = getattr(args, "source", None)
    tasks: list[DownloadTask] = []
    for target in targets:
        source = (
            ctx.registry.get("source", forced) if forced else ctx.registry.select_source(target)
        )
        if source is None:
            raise DownctlError(f"no source plugin can handle: {target}")
        resolved = await source.resolve(target, ctx)  # type: ignore[attr-defined]
        for task in resolved:
            if not task.source:
                task.source = getattr(source, "name", "")
            _apply_router(task, cfg, ctx.registry)
            tasks.append(task)
    return tasks


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
    reporter = _select_reporter(args, cfg, registry)

    token = ""
    hf_options = cfg.plugins.get("huggingface", {})
    if isinstance(hf_options, dict):
        token = str(hf_options.get("token", "") or "")

    request_checksum = getattr(args, "checksum", None)

    async with make_session(cfg.core.proxy, token, stall_limit=STALL_LIMIT) as session:
        ctx = RuntimeContext(
            session=session,
            config=cfg,
            registry=registry,
            events=events,
            rate_limit=cfg.core.rate,
            options={},
        )
        tasks = await _resolve_targets(args, cfg, ctx)
        os.makedirs(cfg.core.dir, exist_ok=True)

        if getattr(args, "output", None):
            if len(tasks) != 1:
                raise DownctlError("-o/--output requires exactly one target")
            tasks[0].target = os.path.abspath(args.output)

        if request_checksum:
            algo, _, digest = request_checksum.partition(":")
            if not digest:
                raise DownctlError("--checksum must be ALGO:HEX, e.g. sha256:...")
            for task in tasks:
                task.checksum_algo, task.checksum_hex = algo, digest

        for task in tasks:
            status = await _probe_task(ctx, task)
            if status in (401, 403, 404):
                raise PermanentHTTPError(status, task.url)

        if getattr(args, "dry_run", False):
            for task in tasks:
                pending = task.size if task.size > 0 else 0
                print(f"[dry-run] {task.url}\n  -> {task.target}  {human_size(pending)}")
            return 0

        failed_log = os.path.join(cfg.core.dir, FAILED_LOG_NAME)
        failures: list[str] = []
        with open(failed_log, "w", encoding="utf-8"):
            pass

        def on_result(task: DownloadTask, ok: bool) -> None:
            if not ok:
                failures.append(task.url)

        ok, failed = await download_all(
            ctx,
            tasks,
            jobs=cfg.core.jobs,
            reporter=reporter,  # type: ignore[arg-type]
            on_result=on_result,
        )

        if failures:
            with open(failed_log, "a", encoding="utf-8") as fh:
                for url in failures:
                    fh.write(url + "\n")

    total = len(tasks)
    if failed:
        print(f"failed: {failed}/{total} (retry with: downctl get --retry-failed)")
        return 1
    print(f"done: {ok}/{total} files")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    return _guard(lambda: asyncio.run(_get(args)))


async def _probe(args: argparse.Namespace) -> int:
    cfg = _build_config(args)
    async with make_session(cfg.core.proxy, "", stall_limit=STALL_LIMIT) as session:
        for target in args.targets:
            size, status = await probe_size(session, target)
            label = status if status is not None else "unreachable"
            print(f"{target}\n  status={label}  size={human_size(size)}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    return _guard(lambda: asyncio.run(_probe(args)))


def cmd_plugin(args: argparse.Namespace) -> int:
    registry = _registry(True)
    action = getattr(args, "plugin_action", None)
    if action == "info":
        found = False
        for category, plugins in registry.items():
            if args.name in plugins:
                found = True
                plugin = plugins[args.name]
                print(f"{category}: {args.name} (priority={getattr(plugin, 'priority', 0)})")
        if not found:
            _errors(f"no plugin named {args.name!r}")
            return 1
        return 0
    for category, plugins in registry.items():
        names = ", ".join(sorted(plugins)) or "-"
        print(f"{category:10s} {names}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    action = getattr(args, "config_action", None) or "show"
    if action == "path":
        print(config_path())
        return 0
    if action == "init":
        try:
            written = write_default_config(force=getattr(args, "force", False))
        except FileExistsError as exc:
            _errors(str(exc))
            return 1
        print(f"wrote {written}")
        return 0
    cfg = load_config()
    print(f"# {config_path()}")
    print(f"dir     = {cfg.core.dir or DEFAULT_DIR}")
    print(f"jobs    = {cfg.core.jobs}")
    print(f"retries = {cfg.core.retries}")
    print(f"proxy   = {cfg.core.proxy or '(none)'}")
    print(f"rate    = {cfg.core.rate}")
    for name, options in cfg.plugins.items():
        print(f"[plugins.{name}] {options}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:  # noqa: ARG001
    registry = _registry(False)
    print(f"downctl  {__version__}")
    print(f"python   {platform.python_version()}")
    print(f"platform {platform_kind()}")
    try:
        import aiohttp

        print(f"aiohttp  {aiohttp.__version__}")
    except ImportError:  # pragma: no cover
        print("aiohttp  NOT INSTALLED")
    try:
        import tqdm

        print(f"tqdm     {tqdm.__version__}")
    except ImportError:  # pragma: no cover
        print("tqdm     NOT INSTALLED")
    print(f"config   {config_path()}")
    print(f"plugins  {len(registry)} loaded ({', '.join(registry.names('source')) or '-'} sources)")
    proxy = detect_wsl_proxy(os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY") or "")
    print(f"proxy    {proxy or '(none detected)'}")
    return 0


def cmd_wizard(args: argparse.Namespace) -> int:  # noqa: ARG001
    if not stdin_is_tty():
        _errors("wizard needs an interactive terminal")
        return 1
    print("downctl wizard — enter one target per line, blank to finish")
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
        limit_rate=None,
        retries=None,
        checksum=None,
        proxy=None,
        mirror=None,
        source=None,
        reporter=None,
        include=[],
        exclude=[],
        dry_run=False,
        retry_failed=False,
        list=None,
        local_plugins=False,
    )


def _guard(action: Callable[[], int]) -> int:
    """Run ``action`` translating downctl errors into exit codes."""
    try:
        return action()
    except DownctlError as exc:
        _errors(str(exc))
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; progress is kept, re-run to resume", file=sys.stderr)
        return 130
