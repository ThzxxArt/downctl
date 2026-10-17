"""Integration tests for the CLI download flow (app._get)."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import pytest

from downctl import app


def make_args(**overrides: object) -> argparse.Namespace:
    namespace = app._default_get_args()
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "does-not-exist.toml"))


async def test_get_downloads_file(file_server, tmp_path: Path) -> None:
    destination = tmp_path / "dl"
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(destination),
        reporter="quiet",
    )
    assert await app._get(args) == 0
    assert (destination / "file").read_bytes() == file_server.data


async def test_get_output_path(file_server, tmp_path: Path) -> None:
    out = tmp_path / "renamed.bin"
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path),
        output=str(out),
        reporter="quiet",
    )
    assert await app._get(args) == 0
    assert out.read_bytes() == file_server.data


async def test_get_verifies_checksum(file_server, tmp_path: Path) -> None:
    digest = hashlib.sha256(file_server.data).hexdigest()
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        checksum=f"sha256:{digest}",
        reporter="quiet",
    )
    assert await app._get(args) == 0


async def test_get_dry_run_writes_nothing(file_server, tmp_path: Path) -> None:
    destination = tmp_path / "dl"
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(destination),
        dry_run=True,
        reporter="quiet",
    )
    assert await app._get(args) == 0
    assert not (destination / "file").exists()


async def test_get_list_file(file_server, tmp_path: Path) -> None:
    listing = tmp_path / "targets.txt"
    listing.write_text(f"# comment\n{file_server.base_url}/file\n", encoding="utf-8")
    destination = tmp_path / "dl"
    args = make_args(list=str(listing), dir=str(destination), reporter="quiet")
    assert await app._get(args) == 0
    assert (destination / "file").read_bytes() == file_server.data


async def test_get_aborts_on_bad_target(file_server, tmp_path: Path) -> None:
    args = make_args(
        targets=[f"{file_server.base_url}/missing"],
        dir=str(tmp_path / "dl"),
        reporter="quiet",
    )
    with pytest.raises(Exception, match="HTTP 404"):
        await app._get(args)


async def test_get_continue_skips_bad_target(file_server, tmp_path: Path) -> None:
    destination = tmp_path / "dl"
    args = make_args(
        targets=[f"{file_server.base_url}/missing", f"{file_server.base_url}/file"],
        dir=str(destination),
        reporter="quiet",
        continue_=True,
    )
    assert await app._get(args) == 1  # partial success
    assert (destination / "file").read_bytes() == file_server.data
    failed = (destination / ".downctl.failed").read_text(encoding="utf-8")
    assert "missing" in failed


async def test_get_checksums_file(file_server, tmp_path: Path) -> None:
    digest = hashlib.sha256(file_server.data).hexdigest()
    sums = tmp_path / "SUMS"
    sums.write_text(f"{digest}  file\n", encoding="utf-8")
    destination = tmp_path / "dl"
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(destination),
        checksums=str(sums),
        reporter="quiet",
    )
    assert await app._get(args) == 0
    assert (destination / "file").read_bytes() == file_server.data


async def test_get_checksums_file_mismatch(
    file_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOWNCTL_RETRY_BASE", "1")
    sums = tmp_path / "SUMS"
    sums.write_text(f"{'0' * 64}  file\n", encoding="utf-8")
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        checksums=str(sums),
        reporter="quiet",
        retries=1,
    )
    assert await app._get(args) == 1


async def test_get_dry_run_reports_failures(file_server, tmp_path: Path) -> None:
    args = make_args(
        targets=[f"{file_server.base_url}/missing"],
        dir=str(tmp_path / "dl"),
        dry_run=True,
        continue_=True,
        reporter="quiet",
    )
    assert await app._get(args) == 1


async def test_retry_failed_uses_stored_task(file_server, tmp_path: Path) -> None:
    destination = tmp_path / "dl"
    destination.mkdir()
    digest = hashlib.sha256(file_server.data).hexdigest()
    line = "\t".join(
        [
            "url",
            f"{file_server.base_url}/file",
            "sub/renamed.bin",
            "sha256",
            digest,
            str(len(file_server.data)),
            "url",
        ]
    )
    (destination / ".downctl.failed").write_text(line + "\n", encoding="utf-8")

    args = make_args(dir=str(destination), reporter="quiet", retry_failed=True)
    assert await app._get(args) == 0
    assert (destination / "sub" / "renamed.bin").read_bytes() == file_server.data


async def test_get_list_from_stdin(
    file_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(f"{file_server.base_url}/file\n"))
    destination = tmp_path / "dl"
    args = make_args(list="-", dir=str(destination), reporter="quiet")
    assert await app._get(args) == 0
    assert (destination / "file").read_bytes() == file_server.data


async def test_get_checksum_multiple_targets_rejected(file_server, tmp_path: Path) -> None:
    from downctl.core.errors import DownctlError

    args = make_args(
        targets=[f"{file_server.base_url}/file", f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        checksum="sha256:" + hashlib.sha256(file_server.data).hexdigest(),
        reporter="quiet",
    )
    with pytest.raises(DownctlError, match="single target"):
        await app._get(args)


async def test_get_skip_existing(file_server, tmp_path: Path) -> None:
    destination = tmp_path / "dl"
    destination.mkdir()
    (destination / "file").write_bytes(file_server.data)
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(destination),
        reporter="quiet",
        skip_existing=True,
    )
    assert await app._get(args) == 0
    assert not any(rng is not None for rng in file_server.ranges)  # no ranged data fetch


async def test_get_force_redownloads(file_server, tmp_path: Path) -> None:
    destination = tmp_path / "dl"
    destination.mkdir()
    (destination / "file").write_bytes(b"stale")
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(destination),
        reporter="quiet",
        force=True,
    )
    assert await app._get(args) == 0
    assert (destination / "file").read_bytes() == file_server.data


async def test_probe_json(file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    args = app._default_get_args()
    args.targets = [f"{file_server.base_url}/file"]
    args.json = True
    assert await app._probe(args) == 0
    record = json.loads(capsys.readouterr().out.strip().splitlines()[0])
    assert record["target"].endswith("/file")
    assert record["size"] == len(file_server.data)
    assert record["reachable"] is True
    assert record["filename"] == "file"
    assert "size_human" in record


async def test_probe_text_shows_fields(
    file_server, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)
    args = app._default_get_args()
    args.targets = [f"{file_server.base_url}/file"]
    assert await app._probe(args) == 0
    out = capsys.readouterr().out
    assert "status=200" in out
    assert "filename=file" in out


async def test_get_summary_includes_dir(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "dl"
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(destination),
        reporter="quiet",
    )
    assert await app._get(args) == 0
    out = capsys.readouterr().out
    assert "done:" in out
    assert f"-> {destination}" in out
