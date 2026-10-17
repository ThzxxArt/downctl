"""Extra coverage for the piece bitmap helpers."""

from __future__ import annotations

from pathlib import Path

from downctl.core import pieces as pc


def test_save_failure_returns_false(tmp_path: Path) -> None:
    # Parent directory does not exist -> the temp write fails.
    assert pc.save(str(tmp_path / "gone" / "f.bin"), pc.Bitmap(100, 1, [0], 0)) is False


def test_remove_missing_is_noop(tmp_path: Path) -> None:
    pc.remove(str(tmp_path / "n.bin"))  # no error


def test_from_dict_invalid_variants() -> None:
    assert pc.Bitmap.from_dict({"size": 100, "pieces": 4, "bits": [2, 0, 0, 0]}) is None
    assert pc.Bitmap.from_dict({"size": 0, "pieces": 1, "bits": [0]}) is None
    assert pc.Bitmap.from_dict({"size": 100, "pieces": 0, "bits": []}) is None
    assert pc.Bitmap.from_dict({"size": 100, "pieces": 2, "bits": "xx"}) is None


def test_from_dict_clamps_fields() -> None:
    bitmap = pc.Bitmap.from_dict(
        {"size": 100, "pieces": 2, "bits": [1, 0], "min_size": -5, "piece_size": -1}
    )
    assert bitmap is not None
    assert bitmap.min_size == 0
    assert bitmap.piece_size == 0


def test_done_bytes_without_piece_size() -> None:
    assert pc.Bitmap(100, 4, [1, 1, 0, 0]).done_bytes() == 50


def test_done_bytes_zero_pieces() -> None:
    assert pc.Bitmap(10, 0, []).done_bytes() == 0


def test_remap_conservative_when_spanning_incomplete() -> None:
    # old grid: 2 pieces of 50 bytes; only the first complete.
    new_bits = pc._remap([1, 0], 50, 1, 100, 100)
    assert new_bits == [0]
