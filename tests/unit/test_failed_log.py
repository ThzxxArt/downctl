"""Tests for the failed-log format and round-tripping."""

from __future__ import annotations

import argparse
import os
import stat
from pathlib import Path

import pytest

from downctl import app
from downctl.config import Config
from downctl.core.context import RuntimeContext
from downctl.core.task import DownloadTask
from downctl.plugin.loader import discover


def test_url_line_roundtrip_preserves_fields() -> None:
    task = DownloadTask(
        url="https://host/x/y.bin",
        target="sub/dir/y.bin",
        checksum_algo="sha256",
        checksum_hex="abc123",
        size=42,
        source="huggingface",
    )
    line = app._fmt_url(task)
    assert "None" not in line
    kind, payload = app._parse_failed_line(line)
    assert kind == "url"
    # v5/v6 append bookkeeping columns that are not DownloadTask fields; the
    # retry path pops them before construction, and so does this test.
    for key in ("reason", "headers", "transport", "host", "credentialed"):
        payload.pop(key, None)  # type: ignore[union-attr]
    assert DownloadTask(**payload) == task  # type: ignore[arg-type]


def test_target_line_roundtrip() -> None:
    kind, payload = app._parse_failed_line(app._fmt_target("hf://org/repo"))
    assert kind == "target"
    assert payload == "hf://org/repo"


def test_legacy_single_column_is_target() -> None:
    kind, payload = app._parse_failed_line("https://example.com/a.bin")
    assert kind == "target"
    assert payload == "https://example.com/a.bin"


def test_target_record_is_removed_after_the_target_resolves(tmp_path: Path) -> None:
    log = tmp_path / ".downctl.failed"
    log.write_text("target\thf://org/repo\n", encoding="utf-8")
    merged = app._merge_failed_log(
        str(log),
        [],
        [DownloadTask(url="https://h/f", target="f.bin", origin="hf://org/repo")],
    )
    assert merged == []


def test_failed_log_is_written_with_owner_only_permissions(tmp_path: Path) -> None:
    log = tmp_path / ".downctl.failed"
    app._write_failed_log(str(log), ["url\thttps://h/f\tf.bin\t\t\t0\turl\t"])
    if os.name != "nt":
        assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_failed_log_temp_is_removed_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / ".downctl.failed"

    def fail_fsync(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="disk full"):
        app._write_failed_log(
            str(log),
            ["url\thttps://h/f?token=SUPERSECRET\tf.bin\t\t\t0\turl\t"],
        )
    assert not log.exists()
    assert not list(tmp_path.glob(".downctl.failed.*.tmp"))
    assert not (tmp_path / ".downctl.failed.tmp").exists()


@pytest.mark.parametrize(
    ("source", "url"),
    [
        ("file", "file:///tmp/x.bin"),
        ("sftp", "sftp://example.test/x.bin"),
    ],
)
async def test_legacy_retry_infers_a_non_http_transport(source: str, url: str) -> None:
    # v2 records have no origin and no transport field. Falling back to the
    # stored URL must not send a file:// or sftp:// URL to the HTTP transport.
    record = {
        "url": url,
        "target": "x.bin",
        "checksum_algo": "",
        "checksum_hex": "",
        "size": 0,
        "source": source,
        "origin": "",
    }
    cfg = Config()
    ctx = RuntimeContext(
        session=None,  # type: ignore[arg-type]
        config=cfg,
        registry=discover(),
    )
    task = await app._retry_task(record, argparse.Namespace(source=None), cfg, ctx)
    assert task.transport == source
