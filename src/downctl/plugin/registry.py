"""Plugin registry: registration, validation and selection."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from downctl import console
from downctl.plugin import protocols
from downctl.plugin.protocols import API_VERSION
from downctl.urls import redact_text

PluginT = TypeVar("PluginT")

#: The plugin categories, mapped to their Protocol.
CATEGORIES: dict[str, type] = {
    "source": protocols.Source,
    "transport": protocols.Transport,
    "verifier": protocols.Verifier,
    "reporter": protocols.Reporter,
    "router": protocols.Router,
    "hook": protocols.Hook,
    # 0.1.6: the two ends of the lifecycle.
    "jobsource": protocols.JobSource,
    "auth": protocols.AuthProvider,
    "server": protocols.Server,
}

#: Required callable members per category (structural protocols only check
#: attribute presence, so verify callability explicitly).
CATEGORY_METHODS: dict[str, tuple[str, ...]] = {
    "source": ("can_handle", "resolve"),
    "transport": ("supports_range", "request"),
    "verifier": ("verify",),
    "reporter": ("task_started", "task_progress", "task_finished"),
    "router": ("rewrite",),
    "hook": ("on_event",),
    "jobsource": ("can_handle", "load"),
    "auth": ("can_handle", "material"),
    "server": ("serve",),
}


class PluginError(Exception):
    """Raised when a plugin is invalid or incompatible."""


def integer_problem(plugin: object, field: str) -> str | None:
    """Describe why ``field`` is not an integer, or ``None`` when it is valid.

    ``bool`` is a subclass of ``int`` in Python, but ``priority = True`` is a
    mistake, not a priority of 1, so it is refused like any other wrong type.
    :meth:`Registry.register` and ``downctl.plugin.testing.check_plugin`` both
    apply this rule, so a plugin cannot pass one check and fail the other (or
    pass both and crash the selector later).
    """
    value = getattr(plugin, field, None)
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{field} must be an integer, got {value!r}"
    return None


class Registry:
    """Holds discovered plugins, keyed by category then name."""

    def __init__(self) -> None:
        self._plugins: dict[str, dict[str, object]] = {cat: {} for cat in CATEGORIES}
        #: Where each registered plugin came from (``builtin``, an entry-point
        #: group, a local directory). 0.1.5 lost this on the second
        #: registration of a built-in, so ``plugin info`` could not answer
        #: "which of these is the one running?".
        self._origins: dict[str, dict[str, str]] = {cat: {} for cat in CATEGORIES}
        #: Names whose registration replaced a different implementation. A
        #: silent overwrite is how two installed packages fight and the user
        #: sees only one of them.
        self._conflicts: list[tuple[str, str, str, str]] = []
        #: Sources whose ``can_handle`` raised. Reported once per run, not once
        #: per target: a broken plugin would otherwise print N warnings for N
        #: targets and look like a problem with the targets.
        self._rejected_sources: set[str] = set()

    def register(self, category: str, plugin: object, *, origin: str = "") -> None:
        """Validate and store ``plugin`` under ``category``."""
        if category not in CATEGORIES:
            raise PluginError(f"unknown plugin category: {category}")
        protocol = CATEGORIES[category]
        if not isinstance(plugin, protocol):
            raise PluginError(
                f"{origin or plugin!r} is not a valid {category} plugin "
                f"(missing required attributes/methods)"
            )
        for method in CATEGORY_METHODS[category]:
            if not callable(getattr(plugin, method, None)):
                raise PluginError(
                    f"{origin or plugin!r} is missing a callable {category}.{method}()"
                )
        version = getattr(plugin, "api_version", None)
        if version != API_VERSION:
            raise PluginError(
                f"{origin or plugin!r} has api_version={version!r}, expected {API_VERSION!r}"
            )
        name = getattr(plugin, "name", None)
        if not name:
            raise PluginError(f"{origin or plugin!r} is missing a 'name'")
        # ``priority`` used to be stored as declared and only cast during
        # selection, so ``priority = "high"`` registered cleanly and then made
        # every target raise ValueError comparing str with int. Refuse it here,
        # where the plugin (and its name) is still on screen.
        problem = integer_problem(plugin, "priority")
        if problem is not None:
            raise PluginError(f"{category} plugin {name!r} is invalid: {problem}")
        previous = self._origins[category].get(str(name))
        existing = self._plugins[category].get(str(name))
        if existing is not None and existing is not plugin:
            # One registration per name is the contract; replacing one is
            # allowed (that is how a plugin shadows a built-in) but never
            # silent: otherwise "why is my plugin not running" has no answer.
            # Comparing origins missed two packages whose entry points share a
            # name (the normal case): both produced the same origin string and
            # the second replaced the first without a word.
            self._conflicts.append(
                (category, str(name), previous or "unknown", origin or "unknown")
            )
            console.warn(
                f"{category} plugin {name!r} from {origin or 'unknown'} "
                f"replaces the one from {previous or 'unknown'}",
                always=True,
            )
        self._plugins[category][str(name)] = plugin
        self._origins[category][str(name)] = origin or "unknown"

    def get(self, category: str, name: str) -> object | None:
        return self._plugins.get(category, {}).get(name)

    def names(self, category: str) -> list[str]:
        return sorted(self._plugins.get(category, {}))

    def all(self, category: str) -> list[object]:
        return list(self._plugins.get(category, {}).values())

    def origin_of(self, category: str, name: str) -> str:
        """Where the winning registration of ``name`` came from."""
        return self._origins.get(category, {}).get(name, "")

    def conflicts(self) -> list[tuple[str, str, str, str]]:
        """Every replacement seen, as ``(category, name, previous, origin)``."""
        return list(self._conflicts)

    def select_source(self, target: str) -> object | None:
        """Return the highest-priority source that can handle ``target``.

        Asking a plugin a question must not be able to break the run: a third
        party's ``can_handle`` that raises used to abort every target, though
        the only thing it was asked is "is this yours?". Failures are reported
        once and the plugin is skipped for the rest of the run.

        Ties are broken by name so the same registry always answers the same
        way: ``max()`` alone returned whichever plugin happened to be first,
        which made selection depend on discovery order.
        """
        candidates: list[tuple[int, str, object]] = []
        for source in self.all("source"):
            name = str(getattr(source, "name", "?"))
            try:
                handled = bool(source.can_handle(target))  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - a plugin must not abort a run
                if name not in self._rejected_sources:
                    self._rejected_sources.add(name)
                    console.warn(
                        f"source plugin {name!r} raised while checking a target: "
                        f"{redact_text(str(exc))}; it is skipped for this run",
                        always=True,
                    )
                continue
            if handled:
                # Registration guarantees an int (see ``integer_problem``);
                # ``int()`` here would silently accept a string that a plugin
                # mutated after registering, which is the bug this replaced.
                candidates.append((getattr(source, "priority", 0), name, source))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][2]

    def items(self) -> Iterable[tuple[str, dict[str, object]]]:
        return self._plugins.items()

    def __len__(self) -> int:
        return sum(len(v) for v in self._plugins.values())
