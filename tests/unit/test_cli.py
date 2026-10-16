"""CLI surface tests."""

from __future__ import annotations

import pytest

from downctl import __version__, console
from downctl.cli import main


@pytest.fixture(autouse=True)
def _reset_console_level() -> None:
    console.set_level(console.NORMAL)
    yield
    console.set_level(console.NORMAL)


def test_version_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "get" in out
    assert "plugin" in out


def test_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_get_requires_targets(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["get"]) == 1
    assert "no targets" in capsys.readouterr().err


def test_plugin_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plugin", "list"]) == 0
    out = capsys.readouterr().out
    assert "source" in out
    assert "url" in out


def test_quiet_suppresses_plugin_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-q", "plugin", "list"]) == 0
    assert capsys.readouterr().out == ""


def test_verbose_flag_sets_level(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-v", "version"]) == 0
    assert console.get_level() == console.VERBOSE


def test_config_path_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "path"]) == 0
    assert "config.toml" in capsys.readouterr().out
