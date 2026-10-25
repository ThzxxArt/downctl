"""Configuration loading tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from downctl.config import Config, load_config, write_default_config
from downctl.core.errors import DownctlError


def test_load_missing_returns_defaults(tmp_path: Path) -> None:
    cfg = load_config(str(tmp_path / "nope.toml"))
    assert isinstance(cfg, Config)
    assert cfg.core.jobs == 1
    assert cfg.plugins == {}


def test_load_and_interpolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "secret")
    path = tmp_path / "config.toml"
    path.write_text(
        "[core]\njobs = 4\ndir = '/data'\n[plugins.huggingface]\ntoken = '${MY_TOKEN}'\n",
        encoding="utf-8",
    )
    cfg = load_config(str(path))
    assert cfg.core.jobs == 4
    assert cfg.core.dir == "/data"
    assert cfg.plugins["huggingface"]["token"] == "secret"


def test_checkpoint_setting_is_loaded(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[core]\ncheckpoint = 5\n", encoding="utf-8")
    assert load_config(str(path)).core.checkpoint == 5
    assert Config().core.checkpoint == 30


def test_write_default_config(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    written = write_default_config(str(target))
    assert written == str(target)
    assert target.is_file()
    with pytest.raises(FileExistsError):
        write_default_config(str(target))


def test_progress_mode_defaults_to_auto(tmp_path: Path) -> None:
    assert load_config(str(tmp_path / "missing.toml")).core.progress == "auto"


def test_progress_mode_is_loaded_and_validated(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[core]\nprogress = "plain"\n', encoding="utf-8")
    assert load_config(str(path)).core.progress == "plain"
    path.write_text('[core]\nprogress = "pretty"\n', encoding="utf-8")
    with pytest.raises(DownctlError, match="progress"):
        load_config(str(path))
