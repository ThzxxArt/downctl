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

from downctl.plugin.registry import CATEGORIES, CATEGORY_METHODS, Registry
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
    """Register plugins advertised by installed packages.

    A plugin may be advertised as a class, an instance, or a zero-argument
    factory. The factory form is the most common one in packaging examples
    (``name = "pkg.module:make_plugin"``) and used to be registered *as the
    plugin*: the callable passed the structural check by accident, then failed
    on the first ``resolve()`` with a message about missing attributes. Calling
    it is what the entry point says to do.
    """
    for group in _entry_point_groups():
        category = group[len(GROUP_PREFIX) :].rstrip("s")
        for entry_point in metadata.entry_points(group=group):
            try:
                factory = entry_point.load()
                plugin = _materialize(factory)
                origin = f"{group}:{entry_point.name}"
                owner = getattr(entry_point, "dist", None)
                owner_name = getattr(getattr(owner, "metadata", None), "get", lambda _k: "")("Name")
                if owner_name:
                    origin += f" ({owner_name})"
                registry.register(category, plugin, origin=origin)
            except Exception as exc:  # noqa: BLE001 - a bad plugin must not abort discovery
                print(
                    f"downctl: skipping plugin {entry_point.name!r}: {redact_text(str(exc))}",
                    file=sys.stderr,
                )


def _materialize(factory: object) -> object:
    """Turn what an entry point loaded into a plugin instance.

    Order matters: a class is callable, and calling it is the documented way to
    get an instance; an object that already advertises ``api_version`` is an
    instance and must not be called again; anything else callable is a
    zero-argument factory.
    """
    if isinstance(factory, type):
        return factory()
    if hasattr(factory, "api_version"):
        return factory
    if callable(factory):
        return factory()
    return factory


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


def load_target(target: str) -> list[tuple[str, str, object]]:
    """Load the plugins named by ``target`` for ``plugin check``.

    ``target`` is ``module``, ``module:attribute`` or an installed distribution
    name. Returns ``(category, label, plugin)`` triples; ``category`` is empty
    when the caller has to detect it. Raises :class:`PluginError` when nothing
    can be loaded.
    """
    from downctl.plugin.registry import PluginError

    module_part, _, attribute = target.partition(":")
    found: list[tuple[str, str, object]] = []
    try:
        module = importlib.import_module(module_part)
    except ImportError:
        module = None
    if module is not None:
        if attribute:
            value = getattr(module, attribute, None)
            if value is None:
                raise PluginError(f"{module_part!r} has no attribute {attribute!r}")
            found.append(("", f"{module_part}:{attribute}", _materialize(value)))
            return found
        methods = {method for names in CATEGORY_METHODS.values() for method in names}
        for name, value in vars(module).items():
            if name.startswith("_"):
                continue
            plugin: object | None = None
            if isinstance(value, type):
                # Only classes *defined here* are candidates: instantiating an
                # imported class (a client session, a task model) can open
                # resources or raise for reasons that have nothing to do with
                # the plugin being checked.
                if value.__module__ != module.__name__:
                    continue
                # A helper class with a ``name`` attribute is not a plugin.
                # Instantiating every such class made a module's own helper
                # fail ``plugin check`` with "does not satisfy any protocol".
                # A genuine plugin has at least one category method (or the
                # metadata of one, so a missing ``api_version`` is still
                # diagnosed).
                if not (
                    hasattr(value, "api_version")
                    or any(callable(getattr(value, method, None)) for method in methods)
                ):
                    continue
                try:
                    plugin = value()
                except Exception:  # noqa: BLE001, S112 - a class needing arguments is not a plugin
                    continue
            elif hasattr(value, "api_version"):
                plugin = value
            if plugin is not None and hasattr(plugin, "name"):
                found.append(("", name, plugin))
        if found:
            return found
        # A distribution whose importable name is an empty ``__init__`` (the
        # ``plugin new`` layout) reaches here. Falling through to the
        # distribution scan is what makes ``plugin check <dist-name>`` work.

    try:
        distribution = metadata.distribution(target)
    except metadata.PackageNotFoundError as exc:
        if module is not None:
            raise PluginError(f"no plugins found in {module_part!r}") from exc
        raise PluginError(
            f"{module_part!r} is not importable and no distribution named {target!r} is installed"
        ) from exc
    distribution_name = (distribution.metadata["Name"] or "").lower()
    for group in _entry_point_groups():
        category = group[len(GROUP_PREFIX) :].rstrip("s")
        for entry_point in metadata.entry_points(group=group):
            owner = entry_point.dist
            if owner is None or (owner.metadata["Name"] or "").lower() != distribution_name:
                continue
            # Include the owning distribution: two packages may advertise the
            # same entry-point name (the plugin name), and an origin string
            # without it cannot tell the replacement from the original.
            origin = f"{group}:{entry_point.name}"
            owner_name = owner.metadata["Name"]
            if owner_name:
                origin += f" ({owner_name})"
            found.append((category, origin, _materialize(entry_point.load())))
    return found
