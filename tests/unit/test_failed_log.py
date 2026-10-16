"""Tests for the failed-log format and round-tripping."""

from __future__ import annotations

from downctl import app
from downctl.core.task import DownloadTask


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
    assert DownloadTask(**payload) == task  # type: ignore[arg-type]


def test_target_line_roundtrip() -> None:
    kind, payload = app._parse_failed_line(app._fmt_target("hf://org/repo"))
    assert kind == "target"
    assert payload == "hf://org/repo"


def test_legacy_single_column_is_target() -> None:
    kind, payload = app._parse_failed_line("https://example.com/a.bin")
    assert kind == "target"
    assert payload == "https://example.com/a.bin"
