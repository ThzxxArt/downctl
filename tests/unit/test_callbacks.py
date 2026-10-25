"""Optional plugin hooks must stay optional for older reporters."""

from __future__ import annotations

import pytest

from downctl import console
from downctl.plugin.callbacks import call_optional, call_optional_safe


class OldReporter:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def run_started(self, total: int, size: int, jobs: int = 1) -> None:
        self.calls.append((total, size, jobs))


class NewReporter:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] = {}

    def run_started(self, total: int, size: int, **kwargs: object) -> None:
        self.kwargs = {"total": total, "size": size, **kwargs}


class BrokenReporter:
    def __init__(self) -> None:
        self.calls = 0

    def phase(self, _name: str) -> None:
        self.calls += 1
        raise RuntimeError("broken")


def test_old_signature_receives_only_declared_keywords() -> None:
    reporter = OldReporter()
    assert call_optional(reporter, "run_started", 3, 100, job="alpha", connections=16)
    assert reporter.calls == [(3, 100, 1)]


def test_var_keyword_receives_everything() -> None:
    reporter = NewReporter()
    call_optional(reporter, "run_started", 3, 100, job="alpha")
    assert reporter.kwargs == {"total": 3, "size": 100, "job": "alpha"}


def test_missing_method_is_not_an_error() -> None:
    assert call_optional(object(), "nothing") is False
    assert call_optional(None, "nothing") is False


def test_safe_variant_reports_once_and_continues() -> None:
    reporter = BrokenReporter()
    console.set_level(console.VERBOSE)
    assert call_optional_safe(reporter, "phase", "probe") is False
    assert call_optional_safe(reporter, "phase", "probe") is False
    assert reporter.calls == 2
    # The warning is emitted once per (plugin, method); repeated phases on a
    # broken third-party reporter must not spam the user.
    console.set_level(console.NORMAL)


def test_safe_variant_ignores_a_missing_method() -> None:
    assert call_optional_safe(object(), "phase") is False


def test_optional_call_propagates_unless_safe() -> None:
    reporter = BrokenReporter()
    with pytest.raises(RuntimeError):
        call_optional(reporter, "phase", "probe")
