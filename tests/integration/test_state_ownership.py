"""State written by a newer build is protected, not destroyed.

0.1.5 added a guard that refused to *overwrite* a newer-build sidecar, and then
made the refusal itself destructive: ``resolve`` reported the file, ``abandon``
could not save, and the fallback path truncated the data to zero and deleted
both sidecars while printing "left untouched". These tests pin the opposite
rule: a refusal means nothing on disk changes, for every entry point that can
reach the guard.

The marker half is the other direction of the same bug. A finished multipart
download used to delete the completion marker along with the ledger, so the
engine re-read an empty record and rewrote the marker with a blank witness and
a blank digest. A second plain run therefore turned a proven file into an
unversioned one, and the checksum hook stopped producing sidecars for identical
content.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pytest

from downctl import app
from downctl.core import pieces as pc


def make_args(**overrides: object) -> argparse.Namespace:
    namespace = app._default_get_args()
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "does-not-exist.toml"))


def _future(path: Path, version: int) -> None:
    """Rewrite a sidecar as a newer build would have written it."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = version
    path.write_text(json.dumps(payload), encoding="utf-8")


async def _download(file_server, destination: Path, **overrides: object) -> int:
    args = make_args(
        targets=[f"{file_server.base_url}/versioned"],
        dir=str(destination),
        reporter="quiet",
        **overrides,
    )
    return await app._get(args)


# --------------------------------------------------------------------------- #
# each layer of the guard, on its own
# --------------------------------------------------------------------------- #
#: The tests below prove the *property* end to end; these pin the three layers
#: that implement it, so removing any single one fails a test that names it.
def _target_with_future_ledger(tmp_path: Path) -> tuple[str, Path]:
    target = tmp_path / "a.bin"
    target.write_bytes(b"data")
    ledger = Path(str(target) + pc.BITMAP_SUFFIX)
    ledger.write_text(json.dumps({"version": pc.BITMAP_VERSION + 1}), encoding="utf-8")
    return str(target), ledger


def test_resolve_refuses_before_it_decides_anything(tmp_path: Path) -> None:
    from downctl.core.errors import FutureStateError

    target, ledger = _target_with_future_ledger(tmp_path)
    with pytest.raises(FutureStateError, match="newer downctl"):
        pc.resolve(target, 4, 1, 4, "sha256:abc")
    assert ledger.is_file()
    assert Path(target).read_bytes() == b"data"


def test_remove_refuses_even_when_resolve_was_never_called(tmp_path: Path) -> None:
    from downctl.core.errors import FutureStateError

    target, ledger = _target_with_future_ledger(tmp_path)
    with pytest.raises(FutureStateError):
        pc.remove(target)
    assert ledger.is_file()


def test_remove_deletes_nothing_when_only_one_sidecar_is_from_a_newer_build(
    tmp_path: Path,
) -> None:
    """All-or-nothing: a half-applied reset is its own kind of damage.

    The per-file choke point raises on the *future* file, so without an
    all-or-nothing pre-check ``remove`` would already have deleted the readable
    ledger by the time it got there. The future marker is listed after the
    bitmap on purpose.
    """
    from downctl.core.errors import FutureStateError

    target = tmp_path / "b.bin"
    target.write_bytes(b"data")
    assert pc.save(str(target), pc.Bitmap(4, 1, [0], 0, 4, url_sha256="sha256:id"))
    marker = Path(str(target) + pc.COMPLETION_SUFFIX)
    marker.write_text(json.dumps({"version": pc.COMPLETION_VERSION + 1}), encoding="utf-8")

    with pytest.raises(FutureStateError):
        pc.remove(str(target))
    assert Path(str(target) + pc.BITMAP_SUFFIX).is_file()
    assert marker.is_file()


def test_discard_refuses_before_truncating(tmp_path: Path) -> None:
    from downctl.core.errors import FutureStateError

    target, ledger = _target_with_future_ledger(tmp_path)
    with pytest.raises(FutureStateError):
        pc.discard(target)
    assert Path(target).read_bytes() == b"data"
    assert ledger.is_file()


def test_the_unlink_choke_point_refuses_any_future_sidecar(tmp_path: Path) -> None:
    """Every deletion goes through ``unlink_state``; that is what makes it safe."""
    from downctl.core.errors import FutureStateError

    _target, ledger = _target_with_future_ledger(tmp_path)
    with pytest.raises(FutureStateError):
        pc.unlink_state(str(ledger))
    assert ledger.is_file()
    # A file that is not a sidecar carries no version and may be deleted.
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    pc.unlink_state(str(plain))
    assert not plain.exists()


async def test_a_future_ledger_is_refused_without_destroying_anything(
    file_server, tmp_path: Path
) -> None:
    destination = tmp_path / "dl"
    target = destination / "versioned"
    assert await _download(file_server, destination) == 0
    data = target.read_bytes()
    marker = Path(str(target) + pc.COMPLETION_SUFFIX)
    marker_before = marker.read_text(encoding="utf-8")

    ledger = Path(str(target) + pc.BITMAP_SUFFIX)
    ledger.write_text(
        json.dumps(
            {
                "version": pc.BITMAP_VERSION + 1,
                "size": len(data),
                "pieces": 1,
                "bits": [1],
                "min_size": len(data),
                "piece_size": len(data),
                "url_sha256": "",
            }
        ),
        encoding="utf-8",
    )
    ledger_before = ledger.read_text(encoding="utf-8")

    assert await _download(file_server, destination) == 1

    # Not one byte, and not one sidecar, may have moved.
    assert target.read_bytes() == data
    assert ledger.read_text(encoding="utf-8") == ledger_before
    assert marker.read_text(encoding="utf-8") == marker_before


async def test_force_is_the_explicit_way_to_discard_a_future_ledger(
    file_server, tmp_path: Path
) -> None:
    """``--force`` says "discard this target", so it may override the guard."""
    destination = tmp_path / "dl"
    target = destination / "versioned"
    assert await _download(file_server, destination) == 0
    ledger = Path(str(target) + pc.BITMAP_SUFFIX)
    ledger.write_text(json.dumps({"version": pc.BITMAP_VERSION + 1}), encoding="utf-8")

    assert await _download(file_server, destination, force=True) == 0
    assert target.read_bytes() == file_server.state.current
    assert not ledger.exists()


async def test_a_future_completion_marker_is_refused_too(file_server, tmp_path: Path) -> None:
    destination = tmp_path / "dl"
    target = destination / "versioned"
    assert await _download(file_server, destination) == 0
    data = target.read_bytes()
    marker = Path(str(target) + pc.COMPLETION_SUFFIX)
    _future(marker, pc.COMPLETION_VERSION + 1)
    marker_before = marker.read_text(encoding="utf-8")

    # Without ``--skip-existing`` a completed-but-unreadable marker used to be
    # thrown away as "untrustworthy state" — after truncating the file.
    assert await _download(file_server, destination) == 1
    assert target.read_bytes() == data
    assert marker.read_text(encoding="utf-8") == marker_before


async def test_the_single_stream_path_cannot_truncate_under_a_future_ledger(
    file_server, tmp_path: Path
) -> None:
    """An unknown-size download never calls ``resolve``, so the guard has to
    live in the deletion primitive itself."""
    destination = tmp_path / "dl"
    target = destination / "unknown"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"partial")
    Path(str(target) + pc.BITMAP_SUFFIX).write_text(
        json.dumps({"version": pc.BITMAP_VERSION + 1}), encoding="utf-8"
    )

    args = make_args(
        targets=[f"{file_server.base_url}/unknown"],
        dir=str(destination),
        reporter="quiet",
    )
    assert await app._get(args) == 1
    assert target.read_bytes() == b"partial"


async def test_verify_reports_a_future_marker_instead_of_nothing_to_verify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``verify`` exiting 0 with "nothing to verify" read as "all proven"."""
    target = tmp_path / "model.bin"
    target.write_bytes(b"x" * 10)
    assert pc.save_completion(str(target), "sha256:abc", validator='"v1"')
    marker = Path(str(target) + pc.COMPLETION_SUFFIX)
    _future(marker, pc.COMPLETION_VERSION + 1)

    args = argparse.Namespace(
        path=str(tmp_path), json=True, checksums=None, repair=False, local_plugins=False
    )
    assert await app._verify(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["unverifiable"] == 1
    assert payload["checked"] == 1
    assert "newer downctl" in payload["entries"][0]["detail"]
    # ``--repair`` must not delete a file it cannot judge.
    assert target.is_file()
    assert marker.is_file()


async def test_a_second_run_keeps_the_evidence_the_marker_carried(
    file_server, tmp_path: Path
) -> None:
    """The witness and digest a finished download earned survive a re-run."""
    destination = tmp_path / "dl"
    target = destination / "versioned"
    digest = hashlib.sha256(file_server.state.current).hexdigest()
    assert await _download(file_server, destination, checksum=f"sha256:{digest}") == 0
    marker = Path(str(target) + pc.COMPLETION_SUFFIX)
    first = json.loads(marker.read_text(encoding="utf-8"))
    assert first["validator"], "the download compared a version; the marker must say so"
    assert first["checksum_hex"] == digest

    # A plain re-run: no probe mismatch, nothing to fetch, nothing to verify.
    assert await _download(file_server, destination) == 0
    second = json.loads(marker.read_text(encoding="utf-8"))
    assert second["validator"] == first["validator"]
    assert second["checksum_hex"] == first["checksum_hex"]


async def test_a_second_run_still_writes_the_checksum_sidecar(
    file_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hook was starved of its witness by the same marker deletion."""
    destination = tmp_path / "dl"
    target = destination / "versioned"
    args = make_args(
        targets=[f"{file_server.base_url}/versioned"],
        dir=str(destination),
        reporter="quiet",
        hook=["checksum"],
    )
    assert await app._get(args) == 0
    sidecar = Path(str(target) + ".sha256")
    assert sidecar.is_file()
    digest = sidecar.read_text(encoding="utf-8").split()[0]

    sidecar.unlink()
    args = make_args(
        targets=[f"{file_server.base_url}/versioned"],
        dir=str(destination),
        reporter="quiet",
        hook=["checksum"],
    )
    assert await app._get(args) == 0
    assert sidecar.is_file(), "a re-run of unchanged content must still prove it"
    assert sidecar.read_text(encoding="utf-8").split()[0] == digest
