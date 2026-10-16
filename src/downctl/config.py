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


def _as_int(key: str, value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise DownctlError(f"config '{key}' must be an integer, got {value!r}") from exc


def _as_float(key: str, value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DownctlError(f"config '{key}' must be a number, got {value!r}") from exc


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
    retries: int = 0
    proxy: str = ""
    #: Per-file connection count for multi-connection downloads.
    connections: int = 16
    #: Stall watchdog limit in seconds.
    stall: int = 30
    #: Bytes-per-second limit (``None`` = unlimited).
    rate: float | None = None
    #: ``FROM=TO`` URL prefix rewrites applied before fetching.
    mirror: dict[str, str] = field(default_factory=dict)
    #: Names of hook plugins to enable for every run.
    hooks: list[str] = field(default_factory=list)


@dataclass
class Config:
    """Effective configuration: core options plus plugin namespaces."""

    core: CoreConfig = field(default_factory=CoreConfig)
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)


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
            jobs=_as_int("core.jobs", core.get("jobs"), 1),
            retries=_as_int("core.retries", core.get("retries"), 0),
            proxy=str(core.get("proxy", "")),
            connections=_as_int("core.connections", core.get("connections"), 16),
            stall=_as_int("core.stall", core.get("stall"), 30),
            rate=_as_float("core.rate", rate),
            mirror={str(k): str(v) for k, v in mirror.items()} if isinstance(mirror, dict) else {},
            hooks=[str(h) for h in hooks] if isinstance(hooks, list) else [],
        )

    plugins = raw.get("plugins", {})
    if isinstance(plugins, dict):
        cfg.plugins = {str(k): dict(v) for k, v in plugins.items() if isinstance(v, dict)}

    return cfg


DEFAULT_CONFIG_TOML = """\
[core]
dir = "~/Downloads"
jobs = 1
retries = 0
# connections = 16
# stall = 30
# rate = 5242880
# hooks = ["logfile"]

[plugins.huggingface]
# token = "${HF_TOKEN}"
# mirror = "https://hf-mirror.com"

[plugins.github]
# token = "${GITHUB_TOKEN}"

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
