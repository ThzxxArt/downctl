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
