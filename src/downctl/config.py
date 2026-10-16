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

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


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
        cfg.core = CoreConfig(
            dir=str(core.get("dir", "")),
            jobs=int(core.get("jobs", 1)),
            retries=int(core.get("retries", 0)),
            proxy=str(core.get("proxy", "")),
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

[plugins.huggingface]
# token = "${HF_TOKEN}"
# mirror = "https://hf-mirror.com"

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
