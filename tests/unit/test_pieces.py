"""Tests for the resumable piece bitmap."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from downctl.core import pieces as pc


def test_layout_small_file_single_piece() -> None:
    piece_size, count, conns = pc.layout(100, 16)
    assert count >= 1
    assert piece_size <= pc.DEFAULT_PIECE_SIZE
    assert conns >= 1


def test_layout_large_file_caps_piece_size() -> None:
    expected = 200 * (1 << 20)  # 200 MiB
    piece_size, count, _ = pc.layout(expected, 16)
    assert piece_size == pc.DEFAULT_PIECE_SIZE
    assert count == (expected + piece_size - 1) // piece_size


def test_layout_unknown_size() -> None:
    assert pc.layout(0, 16) == (0, 0, 16)


def test_save_load_remove_roundtrip(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"x" * 10)
    bitmap = pc.Bitmap(size=10, pieces=3, bits=[1, 0, 1], min_size=10)
    assert pc.save(target, bitmap)
    loaded = pc.load(target)
    assert loaded == bitmap
    assert loaded is not None and loaded.done_pieces == 2
    pc.remove(target)
    assert pc.load(target) is None


def test_load_rejects_corrupt(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"x")
    Path(pc.path_for(target)).write_text("not json", encoding="utf-8")
    assert pc.load(target) is None


def test_resolve_trusts_out_of_order_bitmap(tmp_path: Path) -> None:
    """A bitmap with holes in it (1,0,1) must be trusted, not reset."""
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"a" * 100)
    bitmap = pc.Bitmap(size=100, pieces=4, bits=[1, 0, 1, 0], min_size=100)
    pc.save(target, bitmap)

    bits = pc.resolve(target, 100, 4)
    assert bits == [1, 0, 1, 0]
    assert os.path.getsize(target) == 100  # not truncated


def test_resolve_rejects_truncated_below_min_size(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"a" * 10)
    bitmap = pc.Bitmap(size=100, pieces=4, bits=[1, 1, 1, 1], min_size=100)
    pc.save(target, bitmap)

    bits = pc.resolve(target, 100, 4)
    assert bits == [0, 0, 0, 0]  # reset: full bitmap no longer trusted
    assert os.path.getsize(target) == 0


def test_resolve_trusts_full_non_sparse_file_without_bitmap(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"a" * 100)
    bits = pc.resolve(target, 100, 4)
    if os.name == "nt" or not hasattr(os, "SEEK_DATA"):
        pytest.skip("hole detection unavailable")
    assert bits == [1, 1, 1, 1]


def test_resolve_rejects_sparse_file_without_bitmap(tmp_path: Path) -> None:
    if not hasattr(os, "SEEK_DATA"):
        pytest.skip("hole detection unavailable")
    target = str(tmp_path / "f.bin")
    size = 1 << 20
    with open(target, "wb") as fh:
        fh.seek(size - 1)
        fh.write(b"z")  # large hole then one byte
    stat = os.stat(target)
    if getattr(stat, "st_blocks", None) == 0 or stat.st_blocks * 512 >= size:
        pytest.skip("filesystem did not create a sparse file")
    assert pc.trusted_complete(target, size) is False
    bits = pc.resolve(target, size, 1)
    assert all(bit == 0 for bit in bits)
    assert os.path.getsize(target) == 0


def test_trust_partial_env_forces_trust(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not hasattr(os, "SEEK_DATA"):
        pytest.skip("hole detection unavailable")
    target = str(tmp_path / "f.bin")
    size = 1 << 20
    with open(target, "wb") as fh:
        fh.seek(size - 1)
        fh.write(b"z")
    monkeypatch.setenv(pc.TRUST_PARTIAL_ENV, "1")
    assert pc.trusted_complete(target, size) is True
