"""Tests for the resumable piece bitmap."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from downctl.core import pieces as pc
from downctl.urls import url_identity

IDENTITY = url_identity("https://example.test/file.bin")


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
    bitmap = pc.Bitmap(
        size=100,
        pieces=4,
        bits=[1, 0, 1, 0],
        min_size=100,
        piece_size=25,
        url_sha256=IDENTITY,
    )
    pc.save(target, bitmap)

    resume = pc.resolve(target, 100, 4, identity=IDENTITY)
    assert resume.bits == [1, 0, 1, 0]
    assert resume.proof == pc.PROOF_LEDGER
    assert os.path.getsize(target) == 100  # not truncated


def test_resolve_requires_an_identity(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"a" * 100)
    pc.save(
        target,
        pc.Bitmap(
            size=100,
            pieces=4,
            bits=[1, 0, 1, 0],
            min_size=100,
            piece_size=25,
            url_sha256=IDENTITY,
        ),
    )
    assert pc.resolve(target, 100, 4).bits == [0, 0, 0, 0]
    # Without an identity there is nothing to pin the old bytes to, so this is
    # the one path that still has to discard them.
    assert os.path.getsize(target) == 0


def test_resolve_rejects_geometry_that_cannot_cover_the_file(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"\0" * 4096)
    pc.save(
        target,
        pc.Bitmap(
            size=4096,
            pieces=4,
            bits=[1, 0, 0, 0],
            min_size=4096,
            piece_size=4096,  # impossible: 4 pieces cannot fit in 4096 bytes
            url_sha256=IDENTITY,
        ),
    )
    assert pc.load(target) is None  # rejected at load time
    assert pc.resolve(target, 4096, 4, 1024, IDENTITY).bits == [0, 0, 0, 0]


def test_resolve_rejects_a_complete_bitmap_with_a_short_file(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"a" * 25)
    pc.save(
        target,
        pc.Bitmap(
            size=100,
            pieces=4,
            bits=[1, 1, 1, 1],
            min_size=0,  # stale/foreign ledger
            piece_size=25,
            url_sha256=IDENTITY,
        ),
    )
    resume = pc.resolve(target, 100, 4, 25, IDENTITY)
    assert resume.bits == [0, 0, 0, 0]
    # The bytes survive and are pinned instead of truncated: the fresh ledger
    # says 0 % of the object, so the short file cannot be mistaken for a
    # complete one even if the re-download never happens.
    assert os.path.getsize(target) == 25
    pinned = pc.load(target)
    assert pinned is not None
    assert not any(pinned.bits)


def test_resolve_rejects_a_partial_bitmap_below_its_high_water(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"a" * 50)
    pc.save(
        target,
        pc.Bitmap(
            size=100,
            pieces=4,
            bits=[1, 0, 1, 0],  # claims 75 bytes; file has only 50
            min_size=0,
            piece_size=25,
            url_sha256=IDENTITY,
        ),
    )
    resume = pc.resolve(target, 100, 4, 25, IDENTITY)
    assert resume.bits == [0, 0, 0, 0]
    assert os.path.getsize(target) == 50


def test_resolve_rejects_truncated_below_min_size(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"a" * 10)
    bitmap = pc.Bitmap(
        size=100,
        pieces=4,
        bits=[1, 1, 1, 1],
        min_size=100,
        piece_size=25,
        url_sha256=IDENTITY,
    )
    pc.save(target, bitmap)

    resume = pc.resolve(target, 100, 4, 25, IDENTITY)
    assert resume.bits == [0, 0, 0, 0]  # reset: full bitmap no longer trusted
    assert os.path.getsize(target) == 10


def test_resolve_trusts_full_non_sparse_file_without_bitmap(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"a" * 100)
    resume = pc.resolve(target, 100, 4, identity=IDENTITY)
    if os.name == "nt" or not hasattr(os, "SEEK_DATA"):
        pytest.skip("hole detection unavailable")
    assert resume.bits == [1, 1, 1, 1]
    assert resume.proof == pc.PROOF_HOLES


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
    resume = pc.resolve(target, size, 1, identity=IDENTITY)
    assert not any(resume.bits)
    assert os.path.getsize(target) == size


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


# --- piece-size override / reslicing -------------------------------------- #
def test_layout_explicit_piece_size() -> None:
    size = 40 * (1 << 20)
    piece_size, pieces, _ = pc.layout(size, 8, piece_size=4 << 20)
    assert piece_size == 4 << 20
    assert pieces == 10


def test_layout_clamps_tiny_piece_size() -> None:
    piece_size, _, _ = pc.layout(10 << 20, 8, piece_size=1)
    assert piece_size == pc.MIN_PIECE_SIZE


def test_bitmap_done_bytes_uses_piece_size(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"x" * 100)
    bitmap = pc.Bitmap(size=100, pieces=4, bits=[1, 1, 0, 0], min_size=100, piece_size=25)
    assert bitmap.done_bytes() == 50
    assert pc.save(target, bitmap)
    loaded = pc.load(target)
    assert loaded is not None and loaded.piece_size == 25
    assert loaded.version == pc.BITMAP_VERSION
    assert loaded.url_sha256 == ""
    assert loaded.validator == ""


def test_resolve_reslices_on_connection_change(tmp_path: Path) -> None:
    size = 16 * (1 << 20)
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"\0" * size)

    old_size, old_pieces, _ = pc.layout(size, 16)
    bits = [1] * 8 + [0] * (old_pieces - 8)
    pc.save(
        target,
        pc.Bitmap(
            size=size,
            pieces=old_pieces,
            bits=bits,
            min_size=size,
            piece_size=old_size,
            url_sha256=IDENTITY,
        ),
    )

    new_size, new_pieces, _ = pc.layout(size, 4)
    assert (old_size, old_pieces) == (1 << 20, 16)
    assert (new_size, new_pieces) == (4 << 20, 4)
    resume = pc.resolve(target, size, new_pieces, new_size, IDENTITY)
    # 8 MiB done -> first two 4 MiB pieces complete, the rest not.
    assert resume.bits == [1, 1, 0, 0]
    assert resume.proof == pc.PROOF_LEDGER
    assert os.path.getsize(target) == size  # not truncated


def test_resolve_resets_when_old_piece_size_unknown(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"a" * 100)
    # v2 bitmap: no piece_size recorded.
    pc.save(
        target,
        pc.Bitmap(
            size=100,
            pieces=4,
            bits=[1, 1, 1, 1],
            min_size=100,
            url_sha256=IDENTITY,
        ),
    )
    resume = pc.resolve(target, 100, 2, 50, IDENTITY)
    assert resume.bits == [0, 0]
    assert os.path.getsize(target) == 100
