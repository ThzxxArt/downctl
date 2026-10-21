"""The 0.1.6 plugin-kernel contracts.

Registration, selection, discovery and option handling are the parts every
other feature stands on. Each test here pins one promise:

- asking a plugin a question cannot abort the run (K1);
- two answers at the same priority are resolved the same way every time;
- a replacement is visible instead of silent (K2);
- an entry point may be a factory (K3);
- option kinds are validated, including for third-party hooks (K4/K7).
"""

from __future__ import annotations

from typing import Any

import pytest

from downctl.plugin import loader
from downctl.plugin.hooks import ConfigurableHook, apply_hook_options
from downctl.plugin.registry import CATEGORIES, Registry
from downctl.plugin.testing import check_plugin


class _Source:
    api_version = "1"

    def __init__(self, name: str, priority: int = 0, *, boom: bool = False) -> None:
        self.name = name
        self.priority = priority
        self.boom = boom

    def can_handle(self, target: str) -> bool:
        if self.boom:
            raise RuntimeError("plugin bug")
        return target.startswith("x://")

    async def resolve(self, target: str, ctx: Any) -> list[object]:
        return []


class _Hook:
    api_version = "1"
    name = "demo-hook"
    priority = 0

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        return None


def test_the_three_lifecycle_categories_are_plugins() -> None:
    assert set(CATEGORIES) == {
        "source",
        "transport",
        "verifier",
        "reporter",
        "router",
        "hook",
        "jobsource",
        "auth",
        "server",
    }


def test_a_broken_can_handle_is_skipped_and_reported_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = Registry()
    registry.register("source", _Source("bad", boom=True), origin="test:bad")
    registry.register("source", _Source("good"), origin="test:good")

    assert registry.select_source("x://a") is not None
    first = capsys.readouterr().err
    assert "raised while checking a target" in first

    # Asking again must not print the same warning twice.
    assert registry.select_source("x://b") is not None
    assert "raised while checking a target" not in capsys.readouterr().err


def test_selection_is_deterministic_on_a_priority_tie() -> None:
    registry = Registry()
    registry.register("source", _Source("zeta"), origin="test")
    registry.register("source", _Source("alpha"), origin="test")

    chosen = registry.select_source("x://a")
    assert getattr(chosen, "name", "") == "alpha"


def test_a_replacement_is_warned_and_the_winner_is_identifiable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = Registry()
    registry.register("hook", _Hook(), origin="builtin:_Hook")
    replacement = _Hook()
    registry.register("hook", replacement, origin="downctl.hooks:demo")

    assert registry.get("hook", "demo-hook") is replacement
    assert registry.origin_of("hook", "demo-hook") == "downctl.hooks:demo"
    assert registry.conflicts() == [("hook", "demo-hook", "builtin:_Hook", "downctl.hooks:demo")]
    assert "replaces the one from builtin:_Hook" in capsys.readouterr().err


class _EntryPoint:
    def __init__(self, name: str, value: object) -> None:
        self.name = name
        self._value = value

    def load(self) -> object:
        return self._value


class _DefaultSource(_Source):
    """A zero-argument class, the documented entry-point form."""

    def __init__(self) -> None:
        super().__init__("default")


def test_an_entry_point_factory_is_called(monkeypatch: pytest.MonkeyPatch) -> None:
    def points(group: str | None = None) -> list[_EntryPoint]:
        return [_EntryPoint("demo", lambda: _Source("demo"))] if group == "downctl.sources" else []

    monkeypatch.setattr(loader.metadata, "entry_points", points)
    registry = Registry()
    loader.load_entry_points(registry)

    plugin = registry.get("source", "demo")
    assert plugin is not None
    assert plugin.__class__.__name__ == "_Source"
    assert registry.origin_of("source", "demo") == "downctl.sources:demo"


def test_an_entry_point_class_and_instance_still_work(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _Source("as-instance")

    def points(group: str | None = None) -> list[_EntryPoint]:
        if group != "downctl.sources":
            return []
        return [_EntryPoint("klass", _DefaultSource), _EntryPoint("instance", instance)]

    monkeypatch.setattr(loader.metadata, "entry_points", points)
    registry = Registry()
    loader.load_entry_points(registry)

    assert registry.get("source", "default") is not None
    # The plugin's own ``name`` is the key; the entry-point name only labels it.
    assert registry.get("source", "as-instance") is instance


def test_check_plugin_names_the_missing_member() -> None:
    class _NoVersion:
        name = "bad"
        priority = 0

        def can_handle(self, target: str) -> bool:
            return False

        async def resolve(self, target: str, ctx: Any) -> list[object]:
            return []

    problems = check_plugin(_NoVersion(), "source")
    assert any("api_version" in problem for problem in problems)


def test_third_party_hook_options_are_type_checked() -> None:
    """``bool("false") is True`` applied to a hook without ``OPTIONS`` too."""

    class _Legacy:
        def __init__(self) -> None:
            self.enabled = True
            self.limit = 3

        def on_event(self, event: str, payload: dict[str, object]) -> None:
            return None

    hook = _Legacy()
    problems = apply_hook_options(hook, {"enabled": "false", "limit": True, "nope": 1})

    assert problems == [
        "enabled must be true or false",
        "limit must be an integer",
        "unknown option 'nope'",
    ]
    assert hook.enabled is True
    assert hook.limit == 3


def test_the_new_option_kinds_are_coerced_and_validated() -> None:
    class _Rich(ConfigurableHook):
        OPTIONS = {"ratio": "float", "mode": ("choice", ("a", "b")), "names": "list"}

        def __init__(self) -> None:
            self.ratio = 0.0
            self.mode = "a"
            self.names: list[str] = []

        def on_event(self, event: str, payload: dict[str, object]) -> None:
            return None

    rich = _Rich()
    assert rich.configure({"ratio": 1.5, "mode": "b", "names": "x"}) == []
    assert (rich.ratio, rich.mode, rich.names) == (1.5, "b", ["x"])

    assert rich.configure({"mode": "c"}) == ["mode must be one of a, b, got 'c'"]
    assert rich.configure({"ratio": True}) == ["ratio must be a number, got True"]
    assert rich.configure({"names": [1]}) == ["names must be a list of strings, got [1]"]


# --------------------------------------------------------------------------- #
# defaults have one owner: a new option must appear everywhere it is documented
# --------------------------------------------------------------------------- #
def test_the_config_template_mentions_every_core_option() -> None:
    from dataclasses import fields

    from downctl.config import DEFAULT_CONFIG_TOML, CoreConfig

    missing = [field.name for field in fields(CoreConfig) if field.name not in DEFAULT_CONFIG_TOML]
    assert missing == [], f"options missing from the default config template: {missing}"


def test_default_get_args_covers_every_cli_option() -> None:
    from downctl import app, cli

    parsed = cli.build_parser().parse_args(["get", "x"])
    defaults = app._default_get_args()
    missing = set(vars(parsed)) - {"command"} - set(vars(defaults))
    assert missing == set(), f"get options missing from _default_get_args: {sorted(missing)}"
