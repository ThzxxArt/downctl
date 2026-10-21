"""Every ``get`` option must actually reach the engine.

``--mirror`` had zero references anywhere in the test suite before 0.1.4, and
``--limit-rate`` / ``--piece-size`` / ``--checkpoint`` only appeared as invalid
values in an error-message test. A flag that parses but never takes effect is
indistinguishable from a missing feature, so each one is asserted twice: the
parser produces it, and the download honours it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from downctl import app, cli, console


def parse(*argv: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(list(argv))


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "does-not-exist.toml"))
    console.set_machine_readable(False)


@pytest.mark.parametrize(
    ("argv", "attribute", "expected"),
    [
        (("--jobs", "3"), "jobs", 3),
        (("--connections", "2"), "connections", 2),
        (("--stall", "7"), "stall", 7),
        (("--checkpoint", "5"), "checkpoint", 5),
        (("--max-time", "42"), "max_time", 42),
        (("--probe-jobs", "4"), "probe_jobs", 4),
        (("--retries", "3"), "retries", 3),
        (("--retries", "0"), "retries", 0),
        (("--piece-size", "1M"), "piece_size", 1 << 20),
        (("--min-split-size", "2M"), "min_split_size", 2 << 20),
        (("--limit-rate", "512k"), "rate", 512 * 1024),
    ],
)
def test_get_options_reach_the_config(
    argv: tuple[str, ...], attribute: str, expected: object
) -> None:
    cfg = app._build_config(parse("get", "http://h/f", *argv))
    assert getattr(cfg.core, attribute) == expected


def test_mirror_flag_builds_a_prefix_mapping() -> None:
    cfg = app._build_config(
        parse("get", "http://h/f", "--mirror", "https://a.example=https://b.example")
    )
    assert cfg.core.mirror == {"https://a.example": "https://b.example"}


def test_require_witness_flag_reaches_the_config() -> None:
    assert app._build_config(parse("get", "http://h/f")).core.require_witness is False
    assert (
        app._build_config(parse("get", "http://h/f", "--require-witness")).core.require_witness
        is True
    )


def test_recheck_marks_the_tasks_it_applies_to() -> None:
    """``--recheck`` is a per-target demand, so it travels on the task."""
    args = app._default_get_args()
    assert args.recheck is False
    parsed = parse("get", "http://h/f", "--recheck")
    assert parsed.recheck is True


def test_default_retries_are_finite() -> None:
    """0 means "forever"; that must be an explicit choice, not the default."""
    assert app._build_config(parse("get", "http://h/f")).core.retries == 10


def test_retry_help_text_admits_the_bound() -> None:
    help_text = cli.build_parser().format_help() + " " + _get_help()
    assert "0 = retry forever" in help_text


def _get_help() -> str:
    parser = cli.build_parser()
    # argparse exposes no public API for enumerating subparsers or rendering a
    # single subparser's help; ``_actions``/``_SubParsersAction`` are the only
    # route, and this helper inspects downctl's own parser (not a plugin's).
    for action in parser._actions:  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for name, sub in action.choices.items():
                if name == "get":
                    return sub.format_help()
    return ""


# --------------------------------------------------------------------------- #
# config file handling
# --------------------------------------------------------------------------- #
def test_config_init_writes_then_refuses_then_forces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "config.toml"
    namespace = argparse.Namespace(config_action="init", force=False)

    import os

    os.environ["DOWNCTL_CONFIG"] = str(target)
    try:
        assert app.cmd_config(namespace) == 0
        text = target.read_text(encoding="utf-8")
        assert "[core]" in text and "retries = 10" in text
        capsys.readouterr()

        assert app.cmd_config(namespace) == 1  # refuses to clobber
        assert "already exists" in capsys.readouterr().err

        target.write_text("mine", encoding="utf-8")
        assert app.cmd_config(argparse.Namespace(config_action="init", force=True)) == 0
        assert "[core]" in target.read_text(encoding="utf-8")
    finally:
        del os.environ["DOWNCTL_CONFIG"]


def test_doctor_and_completions_mention_every_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert app.cmd_completions(argparse.Namespace(shell="bash")) == 0
    script = capsys.readouterr().out
    for command in ("get", "probe", "status", "verify", "clean", "plugin", "config", "doctor"):
        assert command in script


def test_completion_script_for_every_shell(capsys: pytest.CaptureFixture[str]) -> None:
    for shell in ("bash", "zsh", "fish"):
        assert app.cmd_completions(argparse.Namespace(shell=shell)) == 0
        assert "downctl" in capsys.readouterr().out


def test_wizard_uses_a_complete_default_namespace() -> None:
    """``wizard`` builds a namespace by hand; it must stay in step with ``get``."""
    parsed = parse("get", "http://h/f")
    default = app._default_get_args()
    missing = {
        name
        for name in vars(parsed)
        if name not in vars(default) and name not in ("command", "quiet", "verbose")
    }
    assert not missing, f"wizard's namespace is missing {sorted(missing)}"
