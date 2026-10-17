"""Extra coverage for app helpers and small commands."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from downctl import app
from downctl.core.errors import DownctlError
from downctl.core.task import DownloadTask


def test_lock_is_live_variants(tmp_path: Path) -> None:
    live = tmp_path / "l.lock"
    live.mkdir()
    assert app._lock_is_live(live) is True  # no pid file -> treated as live
    (live / "pid").write_text("not-an-int", encoding="utf-8")
    assert app._lock_is_live(live) is True
    (live / "pid").write_text(str(os.getpid()), encoding="utf-8")
    assert app._lock_is_live(live) is True


def test_lock_is_live_dead(tmp_path: Path) -> None:
    dead = tmp_path / "d.lock"
    dead.mkdir()
    (dead / "pid").write_text("999999999", encoding="utf-8")
    assert app._lock_is_live(dead) is False


def test_failed_line_identity() -> None:
    assert app._failed_line_identity("url\thttps://h/f\tf.bin") == "https://h/f"
    assert app._failed_line_identity("plainurl") == "plainurl"


def test_merge_failed_log_keeps_untouched(tmp_path: Path) -> None:
    log = tmp_path / ".downctl.failed"
    log.write_text("url\thttps://h/old\told.bin\t\t\t0\turl\n", encoding="utf-8")
    merged = app._merge_failed_log(
        str(log),
        ["url\thttps://h/new\tnew.bin\t\t\t0\turl"],
        [DownloadTask(url="https://h/new", target="new.bin")],
    )
    assert any("old.bin" in line for line in merged)
    assert any("new.bin" in line for line in merged)


def test_load_checksums_missing(tmp_path: Path) -> None:
    with pytest.raises(DownctlError, match="checksum file not found"):
        app._load_checksums(str(tmp_path / "nope"))


def test_apply_checksums(tmp_path: Path) -> None:
    sums = tmp_path / "SUMS"
    sums.write_text(f"{'a' * 64}  sub/x.bin\n", encoding="utf-8")
    task = DownloadTask(url="u", target="sub/x.bin")
    app._apply_checksums([task], str(sums))
    assert task.checksum_algo == "sha256"
    assert task.checksum_hex == "a" * 64


def test_cmd_plugin_info(capsys: pytest.CaptureFixture[str]) -> None:
    assert app.cmd_plugin(argparse.Namespace(plugin_action="info", name="url")) == 0
    assert "url" in capsys.readouterr().out
    assert app.cmd_plugin(argparse.Namespace(plugin_action="info", name="nope")) == 1


def test_cmd_config_init_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("[core]\n", encoding="utf-8")
    monkeypatch.setenv("DOWNCTL_CONFIG", str(cfg))
    assert app.cmd_config(argparse.Namespace(config_action="init", force=False)) == 1
    assert "already exists" in capsys.readouterr().err


def test_find_cleanables_tmp_bitmap(tmp_path: Path) -> None:
    target = tmp_path / "a.bin"
    Path(str(target) + ".downctl-pieces.tmp").write_text("x", encoding="utf-8")
    Path(str(target) + ".downctl-done.tmp").write_text("x", encoding="utf-8")
    (tmp_path / ".downctl.failed.tmp").write_text("x", encoding="utf-8")
    names = {p.name for p in app.find_cleanables(str(tmp_path))}
    assert "a.bin.downctl-pieces.tmp" in names
    assert "a.bin.downctl-done.tmp" in names
    assert ".downctl.failed.tmp" in names


def test_find_cleanables_non_dir() -> None:
    assert app.find_cleanables("/no/such/dir") == []
