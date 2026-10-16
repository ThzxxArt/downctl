"""Tests for the status scanner and hook enablement."""

from __future__ import annotations

import argparse
from pathlib import Path

from downctl import app
from downctl.config import Config, CoreConfig
from downctl.core import pieces as piece_store
from downctl.plugin.hooks import EventBus
from downctl.plugin.loader import discover


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
