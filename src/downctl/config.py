"""Configuration loading for downctl.

Configuration lives in a TOML file (default ``~/.config/downctl/config.toml``,
overridable via ``DOWNCTL_CONFIG``). Plugin-specific options live under the
``[plugins.<name>]`` namespace. Values support ``${VAR}`` environment
interpolation.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from downctl.core.errors import DownctlError

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _as_int(key: str, value: Any, default: int, *, minimum: int | None = None) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise DownctlError(f"config '{key}' must be an integer, got {value!r}")
    result = int(value)
    if minimum is not None and result < minimum:
        raise DownctlError(f"config '{key}' must be >= {minimum}, got {result}")
    return result


def _as_float(key: str, value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise DownctlError(f"config '{key}' must be a number, got {value!r}")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DownctlError(f"config '{key}' must be a number, got {value!r}") from exc
    return None if result <= 0 else result


def as_bool(key: str, value: Any, default: bool) -> bool:
    """A strict boolean for one configuration value.

    Public because plugin options are read by their plugins, and a plugin must
    not decide this for itself: ``bool("false")`` is ``True``, so a value that
    only *looks* off — ``token_to_mirror = "false"`` in a hand-written file —
    would quietly do the opposite of what it says. A typo has to be an error.
    """
    if value is None:
        return default
    if not isinstance(value, bool):
        raise DownctlError(f"config '{key}' must be true or false, got {value!r}")
    return value


_as_bool = as_bool


def config_path() -> str:
    """Return the effective config file path."""
    override = os.environ.get("DOWNCTL_CONFIG")
    if override:
        return override
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return str(base / "downctl" / "config.toml")


@dataclass
class CoreConfig:
    """Options owned by the core engine (non-plugin)."""

    dir: str = ""
    jobs: int = 1
    #: Retries per target (``0`` = retry forever). Finite by default: an
    #: unbounded retry loop turns a permanently failing server into a CI hang.
    retries: int = 10
    proxy: str = ""
    #: Per-file connection count for multi-connection downloads.
    connections: int = 16
    #: Stall watchdog limit in seconds.
    stall: int = 30
    #: Explicit piece size in bytes (0 = choose automatically).
    piece_size: int = 0
    #: Files smaller than this are fetched with a single connection (0 = always split).
    min_split_size: int = 0
    #: Seconds between durable resume checkpoints (0 = only on exit).
    checkpoint: int = 30
    #: Wall-clock cap in seconds for a single target (0 = unlimited).
    max_time: int = 0
    #: How many targets are resolved/probed concurrently (0 = follow ``jobs``).
    probe_jobs: int = 8
    #: Refuse to skip or resume state whose object version cannot be checked.
    require_witness: bool = False
    #: Bytes-per-second limit (``None`` = unlimited).
    rate: float | None = None
    #: ``FROM=TO`` URL prefix rewrites applied before fetching.
    mirror: dict[str, str] = field(default_factory=dict)
    #: Names of hook plugins to enable for every run.
    hooks: list[str] = field(default_factory=list)
    #: Treat a failing hook (an extraction that raised, a command that exited
    #: non-zero) as a failed run. Off by default: a hook must never break a
    #: download, but its failure is always reported and counted either way.
    strict_hooks: bool = False


@dataclass
class Config:
    """Effective configuration: core options plus plugin namespaces."""

    core: CoreConfig = field(default_factory=CoreConfig)
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Where :attr:`CoreConfig.proxy` came from — ``"explicit"`` (config file or
    #: ``-p``), ``"env"`` (an environment variable) or ``"wsl-guess"`` (the WSL
    #: host detection filled it in). A run-time fact about *this* load, never
    #: written back to the file: it decides whether the proxy may carry a
    #: request to a service on this machine, which a guessed proxy may not.
    proxy_source: str = ""


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def load_config(path: str | None = None) -> Config:
    """Load configuration, returning defaults when the file is absent."""
    cfg = Config()
    target = path or config_path()
    if not Path(target).is_file():
        return cfg

    with open(target, "rb") as fh:
        raw = tomllib.load(fh)
    raw = _interpolate(raw)

    core = raw.get("core", {})
    if isinstance(core, dict):
        rate = core.get("rate")
        mirror = core.get("mirror", {})
        hooks = core.get("hooks", [])
        cfg.core = CoreConfig(
            dir=str(core.get("dir", "")),
            jobs=_as_int("core.jobs", core.get("jobs"), 1, minimum=1),
            retries=_as_int("core.retries", core.get("retries"), 10, minimum=0),
            proxy=str(core.get("proxy", "")),
            connections=_as_int("core.connections", core.get("connections"), 16, minimum=1),
            stall=_as_int("core.stall", core.get("stall"), 30, minimum=1),
            piece_size=_as_int("core.piece_size", core.get("piece_size"), 0, minimum=0),
            min_split_size=_as_int("core.min_split_size", core.get("min_split_size"), 0, minimum=0),
            checkpoint=_as_int("core.checkpoint", core.get("checkpoint"), 30, minimum=0),
            max_time=_as_int("core.max_time", core.get("max_time"), 0, minimum=0),
            probe_jobs=_as_int("core.probe_jobs", core.get("probe_jobs"), 8, minimum=0),
            require_witness=_as_bool("core.require_witness", core.get("require_witness"), False),
            rate=_as_float("core.rate", rate),
            mirror={str(k): str(v) for k, v in mirror.items()} if isinstance(mirror, dict) else {},
            hooks=[str(h) for h in hooks] if isinstance(hooks, list) else [],
            strict_hooks=_as_bool("core.strict_hooks", core.get("strict_hooks"), False),
        )

    plugins = raw.get("plugins", {})
    if isinstance(plugins, dict):
        cfg.plugins = {str(k): dict(v) for k, v in plugins.items() if isinstance(v, dict)}

    return cfg


DEFAULT_CONFIG_TOML = """\
[core]
dir = "~/Downloads"
jobs = 1
# Retries per target; 0 = retry forever.
retries = 10
# connections = 16
# stall = 30
# checkpoint = 30
# Give up on a single target after this many seconds (0 = no limit).
# max_time = 0
# How many targets are resolved and probed at the same time.
# probe_jobs = 8
# Refuse to skip or resume a file whose object version cannot be checked
# (servers that send neither ETag nor Last-Modified).
# require_witness = false
# piece_size = 8388608
# min_split_size = 1048576
# rate = 5242880
# hooks = ["logfile"]

[plugins.huggingface]
# token = "${HF_TOKEN}"
# mirror = "https://hf-mirror.com"

[plugins.github]
# token = "${GITHUB_TOKEN}"
# api = "https://api.github.com"

[plugins.s3]
# region = "us-east-1"
# endpoint = "https://s3.us-east-1.amazonaws.com"
# access_key = "${AWS_ACCESS_KEY_ID}"
# secret_key = "${AWS_SECRET_ACCESS_KEY}"
# path_style = true

[plugins.sftp]
# known_hosts = "~/.ssh/known_hosts"

[plugins.tqdm]
enabled = true
"""


def write_default_config(path: str | None = None, *, force: bool = False) -> str:
    """Write the default config file. Returns the path written."""
    target = Path(path or config_path())
    if target.exists() and not force:
        raise FileExistsError(f"config already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return str(target)
