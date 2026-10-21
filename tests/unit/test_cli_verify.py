"""``downctl verify``: proving a completed file still matches its source.

``verify`` is the offline repair channel 0.1.4 adds: it never needs
``--force``, it never guesses, and it reports the *kind* of proof it has
(``validator`` / ``checksum`` / ``none``) instead of a blanket "ok".

Exit codes are part of the contract: ``0`` everything proven, ``1`` something
is wrong (or missing), ``2`` nothing is wrong but nothing could be proven.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pytest

from downctl import app
from downctl.core import pieces as pc
from downctl.urls import url_identity

URL = "http://h/model.bin"
IDENTITY = url_identity(URL)


def verify_args(**overrides: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "path": None,
        "json": False,
        "checksums": None,
        "repair": False,
        "local_plugins": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def completed(target: Path, payload: bytes, *, algo: str = "", digest: str = "") -> None:
    """A file plus the completion marker a finished download leaves behind."""
    target.write_bytes(payload)
    assert pc.save_completion(
        str(target), IDENTITY, algo, digest, validator='"v1"' if not digest else ""
    )


def manifest(tmp_path: Path, name: str, payload: bytes) -> Path:
    path = tmp_path / "SHA256SUMS"
    path.write_text(f"{hashlib.sha256(payload).hexdigest()}  {name}\n", encoding="utf-8")
    return path


def test_verify_proves_a_file_against_a_manifest(tmp_path: Path) -> None:
    payload = b"the real bytes"
    target = tmp_path / "model.bin"
    completed(target, payload)
    sums = manifest(tmp_path, "model.bin", payload)

    assert app.cmd_verify(verify_args(path=str(tmp_path), checksums=str(sums))) == 0


def test_verify_detects_a_rewritten_file(tmp_path: Path) -> None:
    """The 0.1.3 failure mode: right size, right name, wrong bytes."""
    payload = b"the real bytes"
    target = tmp_path / "model.bin"
    completed(target, payload)
    target.write_bytes(b"x" * len(payload))  # same size, different content
    sums = manifest(tmp_path, "model.bin", payload)

    assert app.cmd_verify(verify_args(path=str(tmp_path), checksums=str(sums))) == 1


def test_verify_reports_the_expected_and_actual_digests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = b"good" * 8
    target = tmp_path / "model.bin"
    completed(target, payload)
    target.write_bytes(b"bad!" * 8)
    sums = manifest(tmp_path, "model.bin", payload)

    assert app.cmd_verify(verify_args(path=str(tmp_path), checksums=str(sums), json=True)) == 1
    record = json.loads(capsys.readouterr().out)
    entry = record["entries"][0]
    assert entry["status"] == "mismatch"
    assert entry["expected"] == hashlib.sha256(payload).hexdigest()
    assert entry["actual"] == hashlib.sha256(b"bad!" * 8).hexdigest()


def test_verify_without_a_digest_is_unverifiable_not_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "model.bin"
    completed(target, b"payload")  # no checksum recorded anywhere

    assert app.cmd_verify(verify_args(path=str(tmp_path), json=True)) == 3
    record = json.loads(capsys.readouterr().out)
    entry = record["entries"][0]
    assert entry["status"] == "unverifiable"
    assert entry["witness"] == "validator"  # a version witness, but no digest
    assert entry["compared"] is False  # verify never contacts the server
    assert record["unverifiable"] == 1
    assert "checksums" in entry["detail"]


def test_verify_uses_the_digest_recorded_in_the_marker(tmp_path: Path) -> None:
    payload = b"payload"
    target = tmp_path / "model.bin"
    completed(target, payload, algo="sha256", digest=hashlib.sha256(payload).hexdigest())

    assert app.cmd_verify(verify_args(path=str(tmp_path))) == 0

    target.write_bytes(b"tampered")
    assert app.cmd_verify(verify_args(path=str(tmp_path))) == 1


def test_verify_flags_a_file_touched_since_the_marker(tmp_path: Path) -> None:
    target = tmp_path / "model.bin"
    completed(target, b"payload")
    stat = target.stat()
    target.write_bytes(b"payload and more")  # size changed, marker still there
    assert target.stat().st_size != stat.st_size

    assert app.cmd_verify(verify_args(path=str(tmp_path))) == 1


def test_verify_repair_deletes_only_what_was_proven_bad(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = b"the real bytes"
    good = tmp_path / "good.bin"
    bad = tmp_path / "bad.bin"
    completed(good, payload)
    completed(bad, payload)
    bad.write_bytes(b"x" * len(payload))
    sums = manifest(tmp_path, "bad.bin", payload)

    assert app.cmd_verify(verify_args(path=str(tmp_path), checksums=str(sums), repair=True)) == 1
    assert good.exists()
    assert not bad.exists()
    assert not Path(pc.completion_path(str(bad))).exists()
    out = capsys.readouterr().out
    assert "repaired" in out


def test_verify_repair_removes_an_orphan_marker(tmp_path: Path) -> None:
    target = tmp_path / "gone.bin"
    completed(target, b"payload")
    target.unlink()

    assert app.cmd_verify(verify_args(path=str(tmp_path), repair=True)) == 1
    assert not Path(pc.completion_path(str(target))).exists()


def test_verify_repair_keeps_an_unverifiable_file(tmp_path: Path) -> None:
    """No proof of corruption means no deletion."""
    target = tmp_path / "model.bin"
    completed(target, b"payload")
    assert app.cmd_verify(verify_args(path=str(tmp_path), repair=True)) == 3
    assert target.exists()


def test_verify_reports_incomplete_downloads_without_failing(tmp_path: Path) -> None:
    target = tmp_path / "partial.bin"
    target.write_bytes(b"x" * 50)
    assert pc.save(str(target), pc.Bitmap(100, 4, [1, 0, 0, 0], 50, 25, url_sha256=IDENTITY))

    assert app.cmd_verify(verify_args(path=str(tmp_path))) == 0  # nothing claims to be done


def test_verify_reports_a_marker_with_an_unknown_algorithm(tmp_path: Path) -> None:
    target = tmp_path / "model.bin"
    completed(target, b"payload", algo="sha3-512", digest="a" * 128)

    assert app.cmd_verify(verify_args(path=str(tmp_path))) == 3


def test_verify_rejects_an_empty_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    completed(tmp_path / "model.bin", b"payload")
    empty = tmp_path / "EMPTY"
    empty.write_text("# nothing here\n", encoding="utf-8")

    assert app.cmd_verify(verify_args(path=str(tmp_path), checksums=str(empty))) == 1
    assert "no usable entries" in capsys.readouterr().err


def test_verify_rejects_an_unknown_algorithm_in_a_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    completed(tmp_path / "model.bin", b"payload")
    sums = tmp_path / "SUMS"
    sums.write_text(f"sha3-512:{'a' * 128}  model.bin\n", encoding="utf-8")

    assert app.cmd_verify(verify_args(path=str(tmp_path), checksums=str(sums))) == 1
    assert "unknown checksum algorithm" in capsys.readouterr().err


def test_verify_fails_closed_when_a_manifest_has_no_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A manifest that does not name a file must not silently skip proving it."""
    completed(tmp_path / "model.bin", b"payload")
    sums = tmp_path / "SUMS"
    sums.write_text(f"{'a' * 64}  someone-else.bin\n", encoding="utf-8")
    assert app.cmd_verify(verify_args(path=str(tmp_path), checksums=str(sums))) == 3


def test_verify_json_matches_the_status_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    completed(tmp_path / "model.bin", b"payload")
    assert app.cmd_verify(verify_args(path=str(tmp_path), json=True)) == 3
    record = json.loads(capsys.readouterr().out)
    assert record["directory"] == str(tmp_path)
    assert record["checked"] == 1
    assert set(record) >= {
        "directory",
        "checked",
        "ok",
        "mismatch",
        "unverifiable",
        "missing",
        "repaired",
        "entries",
        "incomplete",
    }


def test_verify_says_so_when_there_is_nothing_to_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert app.cmd_verify(verify_args(path=str(tmp_path))) == 0
    assert "nothing to verify" in capsys.readouterr().out


def test_verify_ignores_a_data_file_that_merely_looks_like_a_marker(
    tmp_path: Path,
) -> None:
    """A user file named ``*.downctl-done`` is not a completion claim."""
    (tmp_path / "notes.downctl-done").write_text("not json", encoding="utf-8")
    assert app.cmd_verify(verify_args(path=str(tmp_path))) == 0


def test_cli_verify_wires_the_command(tmp_path: Path) -> None:
    """The subcommand really is reachable from the command line."""
    from downctl import cli

    payload = b"the real bytes"
    completed(tmp_path / "model.bin", payload)
    sums = manifest(tmp_path, "model.bin", payload)
    assert cli.main(["verify", str(tmp_path), "--checksums", str(sums), "--json"]) == 0

    (tmp_path / "model.bin").write_bytes(b"x" * len(payload))
    assert cli.main(["verify", str(tmp_path), "--checksums", str(sums)]) == 1


def test_repair_refuses_to_delete_a_file_it_cannot_disprove(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same length, newer mtime: nothing offline proves the *content* changed.

    ``--repair`` is authorized to delete what was *proven* bad, and a touched
    file is not that. Before this test existed, ``--repair`` deleted a
    byte-identical file: sha256 before == sha256 after, directory emptied.
    """
    payload = b"the real bytes"
    target = tmp_path / "model.bin"
    completed(target, payload)
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    stamp = target.stat().st_mtime_ns
    os.utime(target, ns=(stamp + 1_000_000_000, stamp + 1_000_000_000))

    assert app.cmd_verify(verify_args(path=str(tmp_path), repair=True)) == 3
    assert target.is_file(), "--repair deleted a file whose content it could not disprove"
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before
    assert "unverifiable" in capsys.readouterr().out


def test_repair_still_deletes_a_file_whose_length_changed(tmp_path: Path) -> None:
    """A different length *is* proof that the marker describes other bytes."""
    target = tmp_path / "model.bin"
    completed(target, b"the real bytes")
    target.write_bytes(b"short")

    assert app.cmd_verify(verify_args(path=str(tmp_path), repair=True)) == 1
    assert not target.exists()
    assert not (tmp_path / "model.bin.downctl-done").exists()


# --------------------------------------------------------------------------- #
# state written by a newer build: reported, never repaired, never two answers
# --------------------------------------------------------------------------- #
def test_verify_reports_a_ledger_from_a_newer_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the *marker* used to be counted as a blocked claim.

    A target whose ledger is unreadable was invisible here even though
    ``status`` reported it, so ``verify`` said "nothing to verify" and exited 0
    while a file it could not judge sat in the directory.
    """
    (tmp_path / "x.bin").write_bytes(b"data")
    (tmp_path / "x.bin.downctl-pieces").write_text('{"version": 99}', encoding="utf-8")

    assert app.cmd_verify(verify_args(path=str(tmp_path))) == 3
    out = capsys.readouterr().out
    assert "unverifiable" in out
    assert "downctl-pieces" in out


def test_verify_reports_a_blocked_file_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A manifest and a newer-build marker naming the same file are one answer.

    Reporting ``ok`` and ``unverifiable`` for one path made the summary
    contradict itself and the exit code depend on which loop ran second.
    """
    payload = b"good"
    target = tmp_path / "m.bin"
    target.write_bytes(payload)
    (tmp_path / "m.bin.downctl-done").write_text('{"version": 99}', encoding="utf-8")
    sums = manifest(tmp_path, "m.bin", payload)

    assert app.cmd_verify(verify_args(path=str(tmp_path), checksums=str(sums), json=True)) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["checked"] == 1
    assert record["ok"] == 1
    assert [entry["status"] for entry in record["entries"]] == ["ok"]


def test_repair_does_not_abort_at_a_file_it_may_not_touch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal used to end the whole run, hiding every file after it."""
    good = b"the real bytes"
    (tmp_path / "a.bin").write_bytes(b"wrong bytes!!!")  # proven bad by the manifest
    (tmp_path / "a.bin.downctl-done").write_text('{"version": 99}', encoding="utf-8")
    completed(tmp_path / "b.bin", good)
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(
        "".join(f"{hashlib.sha256(good).hexdigest()}  {name}\n" for name in ("a.bin", "b.bin")),
        encoding="utf-8",
    )

    assert app.cmd_verify(verify_args(path=str(tmp_path), checksums=str(sums), repair=True)) == 3
    out = capsys.readouterr().out
    assert "a.bin" in out and "b.bin" in out
    # The blocked file keeps its data and its marker: refusing is the point.
    assert (tmp_path / "a.bin").is_file()
    assert (tmp_path / "a.bin.downctl-done").is_file()
