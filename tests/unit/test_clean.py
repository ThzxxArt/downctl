"""Tests for `downctl clean` and header/user parsing helpers."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from downctl import app
from downctl.core import pieces as piece_store


def test_parse_headers() -> None:
    assert app._parse_headers(["A: b", "C:  d "]) == {"A": "b", "C": "d"}
    assert app._parse_headers([]) == {}


def test_parse_headers_invalid() -> None:
    with pytest.raises(Exception, match="--header must be"):
        app._parse_headers(["nocolon"])


def test_parse_user() -> None:
    assert app._parse_user("u:p") == ("u", "p")
    assert app._parse_user(None) is None
    assert app._parse_user("u:p:q") == ("u", "p:q")


def test_parse_user_invalid() -> None:
    with pytest.raises(Exception, match="--user must be"):
        app._parse_user("nopassword")


def test_find_cleanables(tmp_path: Path) -> None:
    target = str(tmp_path / "a.bin")
    Path(target).write_bytes(b"x")
    piece_store.save(target, piece_store.Bitmap(1, 1, [0], 1))
    (tmp_path / "stale.lock").mkdir()
    (tmp_path / ".downctl.failed").write_text("x\n", encoding="utf-8")

    items = {p.name for p in app.find_cleanables(str(tmp_path))}
    assert "a.bin.downctl-pieces" in items
    assert ".downctl.failed" in items
    assert "stale.lock" in items


def test_clean_dry_run_keeps(tmp_path: Path) -> None:
    (tmp_path / ".downctl.failed").write_text("x\n", encoding="utf-8")
    rc = app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=True))
    assert rc == 0
    assert (tmp_path / ".downctl.failed").exists()


def test_clean_removes(tmp_path: Path) -> None:
    target = str(tmp_path / "a.bin")
    Path(target).write_bytes(b"x")
    piece_store.save(target, piece_store.Bitmap(1, 1, [0], 1))
    (tmp_path / "stale.lock").mkdir()
    (tmp_path / ".downctl.failed").write_text("x\n", encoding="utf-8")

    assert app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=False)) == 0
    assert not (tmp_path / ".downctl.failed").exists()
    assert not (tmp_path / "stale.lock").exists()
    assert not (tmp_path / "a.bin.downctl-pieces").exists()
    assert (tmp_path / "a.bin").exists()  # the data file is untouched


def test_clean_nothing(tmp_path: Path) -> None:
    assert app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=False)) == 0
