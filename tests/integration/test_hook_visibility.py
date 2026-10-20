"""A hook failure must be visible, and ``strict_hooks`` must change the exit code.

0.1.5 moved hook failures onto the quiet-able warning channel, so ``-q`` turned
every one of them into nothing at all while ``docs/configuration.md`` promised
"Hook failures are always reported". ``strict_hooks`` had no test either: the
``return 1`` branch was never executed, so the setting could have stopped
working without a single red test.

Both are end-to-end facts about a run, so the tests drive ``app._get`` with a
real hook plugin whose command always fails.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from downctl import app, console


def make_args(**overrides: object) -> argparse.Namespace:
    namespace = app._default_get_args()
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


@pytest.fixture(autouse=True)
def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "does-not-exist.toml"))
    console.set_machine_readable(False)
    console.set_level(console.NORMAL)


def _failing_command() -> str:
    return f'"{sys.executable}" -c "import sys; sys.exit(3)"'


async def test_a_failing_hook_is_reported_even_under_quiet(
    file_server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from downctl.config import Config

    original = app._build_config

    def with_hook(args: argparse.Namespace) -> Config:
        cfg = original(args)
        cfg.core.hooks = ["exec"]
        cfg.plugins["exec"] = {"command": _failing_command()}
        return cfg

    monkeypatch.setattr(app, "_build_config", with_hook)
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        reporter="quiet",
        hook=["exec"],
    )
    # ``quiet`` on the namespace is applied by the CLI entry point, not by
    # ``_get``; set the console level the way ``-q`` does.
    console.set_level(console.QUIET)
    try:
        assert await app._get(args) == 0
    finally:
        console.set_level(console.NORMAL)

    captured = capsys.readouterr()
    assert "exec: command exited with 3" in captured.err


async def test_strict_hooks_turns_a_hook_failure_into_a_failed_run(
    file_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from downctl.config import Config

    original = app._build_config

    def with_hook(args: argparse.Namespace) -> Config:
        cfg = original(args)
        cfg.core.hooks = ["exec"]
        cfg.core.strict_hooks = True
        cfg.plugins["exec"] = {"command": _failing_command()}
        return cfg

    monkeypatch.setattr(app, "_build_config", with_hook)
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        reporter="quiet",
    )
    assert await app._get(args) == 1


async def test_without_strict_hooks_the_run_still_succeeds(
    file_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default must stay "warn, do not fail the download"."""
    from downctl.config import Config

    original = app._build_config

    def with_hook(args: argparse.Namespace) -> Config:
        cfg = original(args)
        cfg.core.hooks = ["exec"]
        cfg.plugins["exec"] = {"command": _failing_command()}
        return cfg

    monkeypatch.setattr(app, "_build_config", with_hook)
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        reporter="quiet",
    )
    assert await app._get(args) == 0
