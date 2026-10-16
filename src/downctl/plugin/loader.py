"""Plugin discovery.

Sources of plugins, in ascending priority (later overrides earlier by name):

1. Built-in plugins (``downctl.builtin``)
2. Installed packages advertising ``downctl.*`` entry-point groups
3. Local directories (``./downctl_plugins`` and ``~/.config/downctl/plugins``)
"""

from __future__ import annotations

import importlib
import importlib.metadata as metadata
import os
import sys
from pathlib import Path

from downctl.plugin.registry import CATEGORIES, PluginError, Registry

GROUP_PREFIX = "downctl."


def _entry_point_groups() -> list[str]:
    return [f"{GROUP_PREFIX}{category}s" for category in CATEGORIES]


def load_builtins(registry: Registry) -> None:
    """Register plugins shipped with downctl."""
    from downctl import builtin

    builtin.register_all(registry, origin="builtin")


def load_entry_points(registry: Registry) -> None:
    """Register plugins advertised by installed packages."""
    for group in _entry_point_groups():
        category = group[len(GROUP_PREFIX) :].rstrip("s")
        for entry_point in metadata.entry_points(group=group):
            try:
                factory = entry_point.load()
                plugin = factory() if isinstance(factory, type) else factory
                registry.register(category, plugin, origin=f"{group}:{entry_point.name}")
            except (PluginError, ImportError) as exc:
                print(f"downctl: skipping plugin {entry_point.name!r}: {exc}", file=sys.stderr)


def load_local_dirs(registry: Registry) -> None:
    """Register plugins from local directories (best effort, opt-in).

    .. warning::
        Local plugins execute arbitrary code. Only enable directories you
        trust.
    """
    dirs = [
        Path.cwd() / "downctl_plugins",
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "downctl" / "plugins",
    ]
    for directory in dirs:
        if not directory.is_dir():
            continue
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
        for module_file in sorted(directory.glob("*.py")):
            if module_file.name.startswith("_"):
                continue
            module_name = module_file.stem
            try:
                module = importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001 - surface plugin errors, keep going
                print(f"downctl: failed to load {module_file}: {exc}", file=sys.stderr)
                continue
            register_fn = getattr(module, "register", None)
            if callable(register_fn):
                try:
                    register_fn(registry)
                except PluginError as exc:
                    print(f"downctl: plugin {module_name} rejected: {exc}", file=sys.stderr)


def discover(*, include_local: bool = False) -> Registry:
    """Build a registry of all available plugins."""
    registry = Registry()
    load_builtins(registry)
    load_entry_points(registry)
    if include_local:
        load_local_dirs(registry)
    return registry
