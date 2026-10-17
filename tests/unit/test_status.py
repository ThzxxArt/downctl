"""Tests for the status scanner and hook enablement."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from downctl import app
from downctl.config import Config, CoreConfig
from downctl.core import pieces as piece_store
from downctl.plugin.hooks import EventBus
from downctl.plugin.loader import discover
from downctl.urls import url_identity


def test_collect_status_finds_incomplete_and_failed(tmp_path: Path) -> None:
    target = str(tmp_path / "a.bin")
    Path(target).write_bytes(b"x" * 10)
    piece_store.save(target, piece_store.Bitmap(10, 3, [1, 0, 0], 10))
    (tmp_path / ".downctl.failed").write_text(
        "url\thttps://h/f\tf.bin\t\t\t0\turl\n", encoding="utf-8"
    )

    incomplete, failed = app.collect_status(str(tmp_path))
    assert len(incomplete) == 1
    assert incomplete[0].target == "a.bin"
    assert (incomplete[0].done, incomplete[0].pieces) == (1, 3)
    assert failed == ["f.bin"]


def test_collect_status_empty(tmp_path: Path) -> None:
    incomplete, failed = app.collect_status(str(tmp_path))
    assert incomplete == []
    assert failed == []


def test_cmd_status_shows_age(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import os
    import time

    target = tmp_path / "old.bin"
    target.write_bytes(b"x" * 10)
    piece_store.save(str(target), piece_store.Bitmap(10, 3, [1, 0, 0], 10))
    old = time.time() - 7200
    os.utime(target, (old, old))

    assert app.cmd_status(argparse.Namespace(path=str(tmp_path))) == 0
    out = capsys.readouterr().out
    assert "incomplete" in out
    assert "ago" in out
    assert "33%" in out


def test_enable_hooks_registers_configured_hook(tmp_path: Path) -> None:
    registry = discover()
    events = EventBus()
    cfg = Config(
        core=CoreConfig(hooks=["logfile"]),
        plugins={"logfile": {"path": str(tmp_path / "h.log")}},
    )
    args = argparse.Namespace(hook=[])

    enabled = app._enable_hooks(args, cfg, registry, events)
    assert enabled == ["logfile"]

    events.emit("on_finish", {"succeeded": 1, "failed": 0})
    assert (tmp_path / "h.log").is_file()
    assert "on_finish" in (tmp_path / "h.log").read_text(encoding="utf-8")


def test_enable_hooks_from_cli(tmp_path: Path) -> None:
    registry = discover()
    events = EventBus()
    cfg = Config(plugins={"logfile": {"path": str(tmp_path / "cli.log")}})
    args = argparse.Namespace(hook=["logfile"])

    assert app._enable_hooks(args, cfg, registry, events) == ["logfile"]


def test_mirror_switch_changes_the_content_identity(tmp_path: Path) -> None:
    cfg = Config(
        core=CoreConfig(
            dir=str(tmp_path),
            mirror={"https://origin.example/": "https://mirror.example/"},
        )
    )
    registry = discover()
    task = app.DownloadTask(url="https://origin.example/file.bin", target="file.bin")
    app._prepare_task(task, "https://origin.example/file.bin")
    before = task.meta["identity"]
    assert before == url_identity("https://origin.example/file.bin")

    app._apply_router(task, cfg, registry)
    assert task.url == "https://mirror.example/file.bin"
    assert task.meta["identity"] == url_identity("https://mirror.example/file.bin")
    assert task.meta["identity"] != before


def test_source_identity_is_combined_with_the_final_url(tmp_path: Path) -> None:
    cfg = Config(core=CoreConfig(dir=str(tmp_path), mirror={"https://a/": "https://b/"}))
    registry = discover()
    task = app.DownloadTask(
        url="https://a/file.bin",
        target="file.bin",
        meta={"identity": "source-mtime-hash"},
    )
    app._prepare_task(task, "https://a/file.bin")
    app._apply_router(task, cfg, registry)
    assert task.meta["source_identity"] == "source-mtime-hash"
    assert task.meta["identity"] != url_identity("https://b/file.bin")
    assert task.meta["identity"] != url_identity("https://a/file.bin")


def test_retry_match_does_not_guess_a_same_basename_in_another_directory() -> None:
    other = app.DownloadTask(url="https://h/other/file.bin", target="other/file.bin")
    assert app._match_resolved([other], "sub/file.bin", "https://h/sub/file.bin") is None
    assert (
        app._match_resolved(
            [other],
            "/tmp/renamed.bin",
            "https://h/sub/file.bin",
            allow_basename=True,
        )
        is other
    )
