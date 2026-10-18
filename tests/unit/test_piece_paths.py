"""The distrust paths of the piece ledger.

``pieces.py`` decides whether bytes already on disk may be reused, so every
branch in it is a corruption boundary: a permissive answer silently blesses a
spliced or holey file, and a crash while writing a sidecar must never look like
progress. ``test_pieces.py`` covers the happy paths; these tests pin the
defensive branches a normal run never executes — corrupt JSON, broken ``stat``,
a payload ``json`` cannot serialise, Windows/drvfs hole detection, a lagging
``getsize`` and the SEEK_DATA/SEEK_HOLE failure modes — because those are the
ones that silently change their answer when the surrounding code moves.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from downctl.core import pieces as pc
from downctl.urls import url_identity

URL = "https://example.test/file.bin"
IDENTITY = url_identity(URL)


def _grid(pieces: int = 4, size: int = 100) -> pc.Bitmap:
    """A fully complete, internally consistent ledger for ``size`` bytes."""
    piece_size = size // pieces
    return pc.Bitmap(
        size=size,
        pieces=pieces,
        bits=[1] * pieces,
        min_size=size,
        piece_size=piece_size,
        url_sha256=IDENTITY,
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Bitmap.from_dict / load: a ledger we cannot read is a ledger we must not use
# --------------------------------------------------------------------------- #
def test_from_dict_refuses_a_ledger_that_cannot_cover_the_object() -> None:
    """``min_size`` beyond the recorded size is internally impossible."""
    assert pc.Bitmap.from_dict({"size": 100, "pieces": 1, "bits": [1], "min_size": 200}) is None


def test_from_dict_normalises_unusable_optional_fields() -> None:
    bitmap = pc.Bitmap.from_dict(
        {
            "version": "v5",
            "size": 100,
            "pieces": 1,
            "bits": [1],
            "url_sha256": 12345,
            "validator": None,
        }
    )
    assert bitmap is not None
    # A ledger from an unknown format is read as the oldest understood one and
    # an unusable identity/witness is dropped rather than trusted.
    assert bitmap.version == 2
    assert bitmap.url_sha256 == ""
    assert bitmap.validator == ""
    assert pc.bitmap_trusted(bitmap, 100, 100, IDENTITY) is False


def test_load_returns_none_for_a_non_object_ledger(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    _write_json(Path(pc.path_for(target)), [1, 2, 3])
    assert pc.load(target) is None


# --------------------------------------------------------------------------- #
# discard
# --------------------------------------------------------------------------- #
def test_discard_truncates_the_data_file_and_drops_the_sidecars(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"x" * 50)
    _write_json(Path(pc.path_for(target)), {})
    _write_json(Path(pc.completion_path(target)), {})
    pc.discard(target)
    assert Path(target).read_bytes() == b""
    assert not Path(pc.path_for(target)).exists()
    assert not Path(pc.completion_path(target)).exists()


def test_discard_of_a_missing_target_still_clears_state(tmp_path: Path) -> None:
    """The data file may already be gone; the sidecars still must not survive."""
    target = str(tmp_path / "gone.bin")
    _write_json(Path(pc.path_for(target)), {})
    pc.discard(target)
    assert not Path(pc.path_for(target)).exists()


# --------------------------------------------------------------------------- #
# _fsync_dir / _atomic_write_json
# --------------------------------------------------------------------------- #
def test_fsync_dir_is_a_noop_on_windows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(pc.os, "name", "nt")
    monkeypatch.setattr(pc.os, "fsync", calls.append)
    pc._fsync_dir(str(tmp_path))
    assert calls == []


def test_fsync_dir_fsyncs_the_directory_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory really is fsynced — via ``os.open``, not ``open``.

    ``open(dir, "rb")`` raises ``IsADirectoryError``, which the old
    implementation swallowed, so the durability step it documented never ran.
    """
    calls: list[int] = []
    monkeypatch.setattr(pc.os, "fsync", calls.append)
    pc._fsync_dir(str(tmp_path))
    assert len(calls) == 1
    # The descriptor is closed again: a closed fd cannot be fstat-ed.
    with pytest.raises(OSError):
        os.fstat(calls[0])


def test_fsync_dir_never_raises_on_an_unusable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed directory fsync is best effort, never a download failure."""

    def boom(*_args: object, **_kwargs: object) -> int:
        raise OSError("cannot open")

    monkeypatch.setattr(pc.os, "open", boom)
    pc._fsync_dir(str(tmp_path))  # must not raise


def test_atomic_write_json_keeps_the_previous_file_on_failure(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    _write_json(path, {"old": True})
    assert pc._atomic_write_json(str(path), {"bad": object()}) is False
    # The failed write must neither clobber the good copy nor leave a temp file
    # behind for the next run to trip over.
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert [entry.name for entry in tmp_path.iterdir()] == ["ledger.json"]


# --------------------------------------------------------------------------- #
# completion markers
# --------------------------------------------------------------------------- #
def test_save_completion_returns_false_when_the_target_vanished(tmp_path: Path) -> None:
    assert pc.save_completion(str(tmp_path / "missing.bin"), IDENTITY) is False


def test_load_completion_rejects_non_object_json(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    _write_json(Path(pc.completion_path(target)), [])
    assert pc.load_completion(target) is None


@pytest.mark.parametrize(
    "broken",
    [
        {"version": "3"},
        {"version": 0},
        {"version": pc.COMPLETION_VERSION + 1},
        {"size": "10"},
        {"size": -1},
        {"mtime_ns": "now"},
        {"url_sha256": 1},
        {"checksum_algo": 1},
        {"checksum_hex": 1},
        {"validator": 1},
    ],
)
def test_load_completion_rejects_unusable_fields(tmp_path: Path, broken: dict[str, object]) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"0123456789")
    assert pc.save_completion(str(target), IDENTITY)
    marker = Path(pc.completion_path(str(target)))
    record = json.loads(marker.read_text(encoding="utf-8"))
    record.update(broken)
    _write_json(marker, record)
    assert pc.load_completion(str(target)) is None


def test_witness_of_names_the_proof_a_marker_carries() -> None:
    assert pc.witness_of(None) == "none"
    assert pc.witness_of({}) == "none"
    assert pc.witness_of({"validator": '"v1"'}) == "validator"
    assert pc.witness_of({"checksum_hex": "abc"}) == "checksum"


def test_completion_valid_is_false_when_the_data_file_disappeared(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"0123456789")
    assert pc.save_completion(str(target), IDENTITY)
    target.unlink()
    assert pc.completion_valid(str(target), 10, IDENTITY) is False


def test_completion_valid_requires_a_url_identity(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"0123456789")
    assert pc.save_completion(str(target), "")
    assert pc.completion_valid(str(target), 10, "") is False
    assert pc.completion_valid(str(target), 10, None) is False


def test_completion_valid_compares_the_version_witness(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"0123456789")
    assert pc.save_completion(str(target), IDENTITY, validator='"v1"')
    assert pc.completion_valid(str(target), 10, IDENTITY, validator='"v1"') is True
    assert pc.completion_valid(str(target), 10, IDENTITY, validator='"v2"') is False

    assert pc.save_completion(str(target), IDENTITY)
    # An unversioned marker may still be reused, but a run that demands a
    # witness must refuse it.
    assert pc.completion_valid(str(target), 10, IDENTITY) is True
    assert pc.completion_valid(str(target), 10, IDENTITY, require_witness=True) is False


# --------------------------------------------------------------------------- #
# bitmap_trusted: one refusal test per invariant
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("size", 999),
        ("version", pc.BITMAP_VERSION - 1),
        ("url_sha256", ""),
        ("pieces", 0),
        ("pieces", pc.MAX_PIECES + 1),
        ("bits", [1, 1]),
        ("piece_size", 0),
        ("piece_size", 50),
        ("min_size", 200),
    ],
)
def test_bitmap_trusted_refuses_a_ledger_that_cannot_prove_its_bytes(
    field: str, value: object
) -> None:
    bitmap = replace(_grid(), **{field: value})
    assert pc.bitmap_trusted(bitmap, 100, 100, IDENTITY) is False


def test_bitmap_trusted_refuses_an_unknown_object_size() -> None:
    assert pc.bitmap_trusted(_grid(), 100, 0, IDENTITY) is False


# --------------------------------------------------------------------------- #
# resolve: the one-shot re-check for a lagging filesystem
# --------------------------------------------------------------------------- #
def test_resolve_rechecks_the_size_after_a_stat_lag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DrvFS/9p can report a stale size once; the ledger must survive that."""
    target = tmp_path / "f.bin"
    target.write_bytes(b"x" * 100)
    assert pc.save(str(target), _grid())
    real_getsize = os.path.getsize
    calls = {"n": 0}

    def lagging(path: str) -> int:
        calls["n"] += 1
        return 0 if calls["n"] == 1 else real_getsize(path)

    monkeypatch.setattr(pc.os.path, "getsize", lagging)
    bits = pc.resolve(str(target), 100, 4, 25, IDENTITY, validator=None).bits
    assert bits == [1, 1, 1, 1]
    assert calls["n"] == 2
    assert Path(pc.path_for(str(target))).exists()


# --------------------------------------------------------------------------- #
# _remap
# --------------------------------------------------------------------------- #
def test_remap_skips_new_pieces_beyond_the_object_end() -> None:
    assert pc._remap([1], 10, 3, 10, 5) == [1, 0, 0]


def test_remap_refuses_to_guess_beyond_the_old_grid() -> None:
    assert pc._remap([1], 10, 1, 100, 100) == [0]


# --------------------------------------------------------------------------- #
# trusted_complete / _on_drvfs / _has_holes
# --------------------------------------------------------------------------- #
def test_trusted_complete_refuses_an_empty_file(tmp_path: Path) -> None:
    assert pc.trusted_complete(str(tmp_path / "f.bin"), 0) is False


def test_trusted_complete_honours_the_escape_hatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"x" * 10)
    monkeypatch.setenv(pc.TRUST_PARTIAL_ENV, "1")
    assert pc.trusted_complete(str(target), 10) is True


def test_trusted_complete_distrusts_native_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"x" * 10)
    monkeypatch.delenv(pc.TRUST_PARTIAL_ENV, raising=False)
    monkeypatch.setattr(pc, "_on_drvfs", lambda _path: False)
    monkeypatch.setattr(pc.os, "name", "nt")
    assert pc.trusted_complete(str(target), 10) is False


def test_on_drvfs_is_false_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc.platform, "system", lambda: "Darwin")
    assert pc._on_drvfs("/mnt/d/f.bin") is False


def test_on_drvfs_is_false_when_the_kernel_is_not_wsl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pc.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        pc, "open", lambda *_args, **_kwargs: io.StringIO("Linux version 6.8.0"), raising=False
    )
    assert pc._on_drvfs("/mnt/d/f.bin") is False


def test_on_drvfs_is_false_when_proc_version_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> object:
        raise OSError("no /proc mounted")

    monkeypatch.setattr(pc.platform, "system", lambda: "Linux")
    monkeypatch.setattr(pc, "open", boom, raising=False)
    assert pc._on_drvfs("/mnt/d/f.bin") is False


def test_on_drvfs_detects_wsl_drive_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pc.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        pc, "open", lambda *_args, **_kwargs: io.StringIO("Microsoft WSL2"), raising=False
    )
    assert pc._on_drvfs("/mnt/d/f.bin") is True
    assert pc._on_drvfs("/home/user/f.bin") is False


def test_has_holes_is_true_without_seek_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"x" * 4096)
    monkeypatch.delattr(pc.os, "SEEK_DATA", raising=False)
    assert pc._has_holes(str(target), 4096) is True


def test_has_holes_distrusts_a_file_it_cannot_seek(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"x" * 4096)

    def boom(*_args: object) -> int:
        raise OSError("EINVAL")

    monkeypatch.setattr(pc.os, "lseek", boom)
    assert pc._has_holes(str(target), 4096) is True  # the scan stopped short
    assert pc._has_holes(str(target), 0) is False  # nothing was skipped


def test_has_holes_is_true_when_the_hole_seek_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"x" * 4096)
    real_lseek = os.lseek

    def only_data(fd: int, pos: int, whence: int) -> int:
        if whence == pc.os.SEEK_HOLE:
            raise OSError("ENXIO")
        return real_lseek(fd, pos, whence)

    monkeypatch.setattr(pc.os, "lseek", only_data)
    assert pc._has_holes(str(target), 4096) is True


def test_has_holes_walks_past_an_interior_hole(tmp_path: Path) -> None:
    target = tmp_path / "sparse.bin"
    size = 1 << 20
    with open(target, "wb") as fh:
        fh.write(b"a" * 4096)
        fh.seek(size - 4096)
        fh.write(b"z" * 4096)
    assert target.stat().st_size == size
    assert pc._has_holes(str(target), size) is True


def test_has_holes_is_false_for_a_fully_written_file(tmp_path: Path) -> None:
    target = tmp_path / "solid.bin"
    target.write_bytes(b"a" * 8192)
    assert pc._has_holes(str(target), 8192) is False


def test_has_holes_is_true_when_the_file_cannot_be_opened(tmp_path: Path) -> None:
    assert pc._has_holes(str(tmp_path / "missing.bin"), 10) is True
