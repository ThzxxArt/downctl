"""CLI surface tests."""

from __future__ import annotations

import pytest

from downctl import __version__
from downctl.cli import main


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


def test_get_stub(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["get", "https://example.com/a.bin"]) == 0
    assert "https://example.com/a.bin" in capsys.readouterr().out


def test_config_path_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "path"]) == 0
    assert "config.toml" in capsys.readouterr().out
