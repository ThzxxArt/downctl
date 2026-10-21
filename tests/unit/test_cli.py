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


def test_quiet_still_prints_the_answer(capsys: pytest.CaptureFixture[str]) -> None:
    """``-q`` silences progress chatter, not the result of the command.

    ``plugin list`` under ``-q`` printed nothing at all, so a script asking
    "what is installed?" got an empty answer and exit 0.
    """
    assert main(["-q", "plugin", "list"]) == 0
    out = capsys.readouterr().out
    assert "source" in out
    assert "hook" in out


def test_verbose_flag_sets_level(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-v", "version"]) == 0
    assert console.get_level() == console.VERBOSE


def test_config_path_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "path"]) == 0
    assert "config.toml" in capsys.readouterr().out


def test_status_subcommand(capsys: pytest.CaptureFixture[str], tmp_path) -> None:
    assert main(["status", str(tmp_path)]) == 0
    assert "no incomplete" in capsys.readouterr().out


def test_clean_subcommand(capsys: pytest.CaptureFixture[str], tmp_path) -> None:
    assert main(["clean", str(tmp_path)]) == 0
    assert "nothing to clean" in capsys.readouterr().out


def test_main_turns_config_errors_into_a_clean_exit(
    capsys: pytest.CaptureFixture[str], tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[core]\njobs = 0\n", encoding="utf-8")
    monkeypatch.setenv("DOWNCTL_CONFIG", str(config))
    assert main(["status", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "config 'core.jobs'" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completions_subcommand(capsys: pytest.CaptureFixture[str], shell: str) -> None:
    assert main(["completions", shell]) == 0
    out = capsys.readouterr().out
    assert "downctl" in out
    assert "get" in out


def test_checkpoint_option_is_parsed() -> None:
    from downctl.cli import build_parser

    args = build_parser().parse_args(["get", "--checkpoint", "5", "http://h/x"])
    assert args.checkpoint == 5


def test_plugin_local_plugins_flag_works_before_and_after_the_action() -> None:
    from downctl.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["plugin", "list", "--local-plugins"]).local_plugins is True
    assert parser.parse_args(["plugin", "--local-plugins", "info", "url"]).local_plugins is True
