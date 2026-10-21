"""Tests for `downctl clean` and header/user parsing helpers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from downctl import app
from downctl.core import pieces as piece_store
from downctl.core.locks import LOCK_MARKER


def _make_lock(directory: Path, pid: int) -> Path:
    """A lock directory laid out exactly as FileLock writes one."""
    directory.mkdir()
    (directory / "pid").write_text(str(pid), encoding="utf-8")
    (directory / LOCK_MARKER).write_text("downctl\n", encoding="utf-8")
    return directory


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
    stale = _make_lock(tmp_path / "stale.lock", 999999999)  # dead pid
    (tmp_path / ".downctl.failed").write_text("x\n", encoding="utf-8")

    items = {p.name for p in app.find_cleanables(str(tmp_path))}
    assert "a.bin.downctl-pieces" in items
    assert ".downctl.failed" in items
    assert stale.name in items


def test_find_cleanables_ignores_unverifiable_lock(tmp_path: Path) -> None:
    (tmp_path / "keep.lock").mkdir()  # no pid file -> not proven dead
    assert list(app.find_cleanables(str(tmp_path))) == []


def test_find_cleanables_never_touches_a_user_lock_directory(tmp_path: Path) -> None:
    """A ``*.lock`` directory without downctl's marker is the user's, not ours.

    Reproduced before this fix: a user directory named ``build.lock`` that
    happened to contain a dead ``pid`` file was deleted by ``clean``.
    """
    user_dir = tmp_path / "build.lock"
    user_dir.mkdir()
    (user_dir / "pid").write_text("999999999", encoding="utf-8")
    important = user_dir / "Makefile"
    important.write_text("all:\n", encoding="utf-8")

    assert list(app.find_cleanables(str(tmp_path))) == []
    assert app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=False)) == 0
    assert important.exists()


def test_find_cleanables_keeps_the_state_of_a_live_transfer(tmp_path: Path) -> None:
    target = str(tmp_path / "live.bin")
    Path(target).write_bytes(b"x")
    piece_store.save(target, piece_store.Bitmap(1, 1, [0], 1))
    _make_lock(tmp_path / "live.bin.lock", os.getpid())
    names = {p.name for p in app.find_cleanables(str(tmp_path))}
    assert "live.bin.downctl-pieces" not in names
    assert "live.bin.lock" not in names


def test_clean_never_removes_a_directory_named_like_a_bitmap(tmp_path: Path) -> None:
    user_dir = tmp_path / "sub.downctl-pieces"
    user_dir.mkdir()
    important = user_dir / "important.txt"
    important.write_text("keep me", encoding="utf-8")
    assert app.find_cleanables(str(tmp_path)) == []
    assert app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=False)) == 0
    assert important.read_text(encoding="utf-8") == "keep me"


def test_live_bitmap_inside_a_suffix_named_directory_is_kept(tmp_path: Path) -> None:
    user_dir = tmp_path / "sub.downctl-pieces"
    user_dir.mkdir()
    target = str(user_dir / "live.bin")
    Path(target).write_bytes(b"x")
    piece_store.save(target, piece_store.Bitmap(1, 1, [0], 1))
    _make_lock(user_dir / "live.bin.lock", os.getpid())

    names = {str(p) for p in app.find_cleanables(str(tmp_path))}
    assert str(user_dir / "live.bin.downctl-pieces") not in names
    assert app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=False)) == 0
    assert (user_dir / "live.bin.downctl-pieces").is_file()


def test_clean_dry_run_keeps(tmp_path: Path) -> None:
    (tmp_path / ".downctl.failed").write_text("x\n", encoding="utf-8")
    rc = app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=True))
    assert rc == 0
    assert (tmp_path / ".downctl.failed").exists()


def test_clean_removes(tmp_path: Path) -> None:
    target = str(tmp_path / "a.bin")
    Path(target).write_bytes(b"x")
    piece_store.save(target, piece_store.Bitmap(1, 1, [0], 1))
    _make_lock(tmp_path / "stale.lock", 999999999)
    (tmp_path / ".downctl.failed").write_text("x\n", encoding="utf-8")

    assert app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=False)) == 0
    assert not (tmp_path / ".downctl.failed").exists()
    assert not (tmp_path / "stale.lock").exists()
    assert not (tmp_path / "a.bin.downctl-pieces").exists()
    assert (tmp_path / "a.bin").exists()  # the data file is untouched


def test_clean_nothing(tmp_path: Path) -> None:
    assert app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=False)) == 0


def test_clean_removes_only_orphan_completion_markers(tmp_path: Path) -> None:
    kept_data = tmp_path / "kept.bin"
    kept_data.write_bytes(b"x")
    assert piece_store.save_completion(str(kept_data), "identity")
    kept_marker = tmp_path / "kept.bin.downctl-done"

    orphan_data = tmp_path / "gone.bin"
    orphan_data.write_bytes(b"x")
    assert piece_store.save_completion(str(orphan_data), "identity")
    orphan = tmp_path / "gone.bin.downctl-done"
    orphan_data.unlink()

    names = {p.name for p in app.find_cleanables(str(tmp_path))}
    assert "gone.bin.downctl-done" in names
    assert "kept.bin.downctl-done" not in names

    assert app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=False)) == 0
    assert not orphan.exists()
    assert kept_marker.exists()


def test_clean_never_removes_user_data_named_like_a_completion_marker(tmp_path: Path) -> None:
    # The suffix is reserved for sidecars, but a data file that merely *looks*
    # like one (and is not a valid marker) must survive clean.
    user_file = tmp_path / "report.downctl-done"
    user_file.write_text("user data, not a marker\n", encoding="utf-8")

    assert app.find_cleanables(str(tmp_path)) == []
    assert app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=False)) == 0
    assert user_file.read_text(encoding="utf-8") == "user data, not a marker\n"


def test_clean_never_removes_user_data_named_like_a_bitmap(tmp_path: Path) -> None:
    user_file = tmp_path / "report.downctl-pieces"
    user_file.write_text("user data, not a ledger\n", encoding="utf-8")

    assert app.find_cleanables(str(tmp_path)) == []
    assert app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=False)) == 0
    assert user_file.read_text(encoding="utf-8") == "user data, not a ledger\n"


def test_clean_reports_a_failed_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / ".downctl.failed"
    target.write_text("x\n", encoding="utf-8")
    original_unlink = Path.unlink

    def deny(self: Path, *args: object, **kwargs: object) -> None:
        if self == target:
            raise PermissionError("permission denied")
        original_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", deny)
    assert app.cmd_clean(argparse.Namespace(path=str(tmp_path), dry_run=False, json=True)) == 1
    record = json.loads(capsys.readouterr().out)
    assert record["removed"] == 0
    assert record["failed"][0]["path"] == str(target)
    assert target.exists()
