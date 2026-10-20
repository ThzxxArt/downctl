"""Plugin discovery.

Sources of plugins, in ascending priority (later overrides earlier by name):

1. Built-in plugins (``downctl.builtin``)
2. Installed packages advertising ``downctl.*`` entry-point groups
3. Local directories (``./downctl_plugins`` and ``~/.config/downctl/plugins``)
"""

from __future__ import annotations

import importlib
import importlib.metadata as metadata
import importlib.util
import os
import sys
from pathlib import Path

from downctl.plugin.registry import CATEGORIES, Registry
from downctl.urls import redact_text

GROUP_PREFIX = "downctl."

#: Entry-point groups that existed in a released version and no longer map to a
#: plugin category. Packages declaring them must be *told*, not silently
#: ignored: their plugin would otherwise never run and never be missed.
RETIRED_GROUPS: dict[str, str] = {
    "downctl.sinks": (
        "the 'sink' plugin category was removed in 0.1.4 (the engine always wrote "
        "through core/transfer.py, so the contract never took effect)"
    ),
}


def _entry_point_groups() -> list[str]:
    return [f"{GROUP_PREFIX}{category}s" for category in CATEGORIES]


def warn_retired_groups() -> None:
    """Tell the operator about plugins that target a removed category."""
    for group, reason in RETIRED_GROUPS.items():
        try:
            declared = list(metadata.entry_points(group=group))
        except Exception:  # noqa: BLE001, S112 - discovery must never break a run
            continue
        if declared:
            names = ", ".join(sorted(entry.name for entry in declared))
            print(
                f"downctl: ignoring {group} plugin(s) ({names}): {reason}",
                file=sys.stderr,
            )


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
            except Exception as exc:  # noqa: BLE001 - a bad plugin must not abort discovery
                print(
                    f"downctl: skipping plugin {entry_point.name!r}: {redact_text(str(exc))}",
                    file=sys.stderr,
                )


def load_local_dirs(registry: Registry) -> None:
    """Register plugins from local directories (best effort, opt-in).

    .. warning::
        Local plugins execute arbitrary code. Only enable directories you
        trust.
    """
    dirs = [
        Path.cwd() / "downctl_plugins",
        Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        / "downctl"
        / "plugins",
    ]
    for directory in dirs:
        if not directory.is_dir():
            continue
        for module_file in sorted(directory.glob("*.py")):
            if module_file.name.startswith("_"):
                continue
            # Load under a unique module name so plugins with the same stem in
            # different directories do not collide in sys.modules.
            unique = f"downctl_plugin_{directory.name}_{module_file.stem}"
            spec = importlib.util.spec_from_file_location(unique, module_file)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[unique] = module
            try:
                spec.loader.exec_module(module)
            except Exception as exc:  # noqa: BLE001 - surface plugin errors, keep going
                sys.modules.pop(unique, None)
                print(
                    f"downctl: failed to load {module_file}: {redact_text(str(exc))}",
                    file=sys.stderr,
                )
                continue
            register_fn = getattr(module, "register", None)
            if callable(register_fn):
                try:
                    register_fn(registry)
                except Exception as exc:  # noqa: BLE001
                    print(
                        f"downctl: plugin {module_file.stem} rejected: {redact_text(str(exc))}",
                        file=sys.stderr,
                    )


def discover(*, include_local: bool = False) -> Registry:
    """Build a registry of all available plugins."""
    registry = Registry()
    load_builtins(registry)
    load_entry_points(registry)
    warn_retired_groups()
    if include_local:
        load_local_dirs(registry)
    return registry
