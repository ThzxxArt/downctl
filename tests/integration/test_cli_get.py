"""Integration tests for the CLI download flow (app._get)."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from downctl import app, console
from downctl.core import pieces as pc
from downctl.core.errors import DownctlError
from downctl.urls import url_identity


def make_args(**overrides: object) -> argparse.Namespace:
    namespace = app._default_get_args()
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "does-not-exist.toml"))
    console.set_machine_readable(False)
    yield
    console.set_machine_readable(False)


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
    assert not destination.exists()  # dry run creates no state directories either


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


async def test_get_md5_checksums_file(file_server, tmp_path: Path) -> None:
    digest = hashlib.md5(file_server.data).hexdigest()
    sums = tmp_path / "SUMS"
    sums.write_text(f"md5:{digest}  file\n", encoding="utf-8")
    destination = tmp_path / "dl"
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(destination),
        checksums=str(sums),
        reporter="quiet",
    )
    assert await app._get(args) == 0
    assert (destination / "file").read_bytes() == file_server.data


async def test_get_md5sum_style_file_without_algorithm_prefix(file_server, tmp_path: Path) -> None:
    # Ordinary ``md5sum file > SUMS`` output has no ``algo:`` prefix; the
    # digest length must identify the algorithm.
    digest = hashlib.md5(file_server.data).hexdigest()
    sums = tmp_path / "MD5SUMS"
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


async def test_get_checksums_with_output_uses_the_remote_name(file_server, tmp_path: Path) -> None:
    digest = hashlib.sha256(file_server.data).hexdigest()
    sums = tmp_path / "SUMS"
    sums.write_text(f"{digest}  file\n", encoding="utf-8")
    output = tmp_path / "renamed.bin"
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        output=str(output),
        checksums=str(sums),
        reporter="quiet",
    )
    assert await app._get(args) == 0
    assert output.read_bytes() == file_server.data


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


async def test_get_checksums_file_without_an_entry_fails_closed(
    file_server, tmp_path: Path
) -> None:
    sums = tmp_path / "SUMS"
    sums.write_text(f"{'0' * 64}  another-file\n", encoding="utf-8")
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        checksums=str(sums),
        reporter="quiet",
    )
    with pytest.raises(DownctlError, match="no entry for"):
        await app._get(args)
    assert not (tmp_path / "dl" / "file").exists()


async def test_get_checksums_file_empty_fails_closed(file_server, tmp_path: Path) -> None:
    sums = tmp_path / "SUMS"
    sums.write_text("# empty manifest\n", encoding="utf-8")
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        checksums=str(sums),
        reporter="quiet",
    )
    with pytest.raises(DownctlError, match="no usable entries"):
        await app._get(args)


async def test_get_checksums_absolute_path_entry_fails_closed(file_server, tmp_path: Path) -> None:
    digest = hashlib.sha256(file_server.data).hexdigest()
    sums = tmp_path / "SUMS"
    sums.write_text(f"{digest}  /absolute/path/file\n", encoding="utf-8")
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        checksums=str(sums),
        reporter="quiet",
    )
    with pytest.raises(DownctlError, match="no entry for"):
        await app._get(args)


async def test_checksum_without_an_algorithm_is_rejected_before_download(
    file_server, tmp_path: Path
) -> None:
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        checksum=":abc",
        reporter="quiet",
    )
    with pytest.raises(DownctlError, match="ALGO:HEX"):
        await app._get(args)
    assert not (tmp_path / "dl" / "file").exists()


async def test_get_dry_run_reports_failures(file_server, tmp_path: Path) -> None:
    args = make_args(
        targets=[f"{file_server.base_url}/missing"],
        dir=str(tmp_path / "dl"),
        dry_run=True,
        continue_=True,
        reporter="quiet",
    )
    assert await app._get(args) == 1


async def test_get_dry_run_does_not_overwrite_the_failed_log(file_server, tmp_path: Path) -> None:
    destination = tmp_path / "dl"
    destination.mkdir()
    old = "url\thttps://old.example/f\told.bin\t\t\t0\turl\t\n"
    (destination / ".downctl.failed").write_text(old, encoding="utf-8")

    args = make_args(
        targets=[f"{file_server.base_url}/missing"],
        dir=str(destination),
        dry_run=True,
        continue_=True,
        reporter="quiet",
    )
    assert await app._get(args) == 1
    assert (destination / ".downctl.failed").read_text(encoding="utf-8") == old


async def test_get_dry_run_with_json_reporter_emits_a_record(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        dry_run=True,
        reporter="json",
    )
    assert await app._get(args) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert records == [
        {
            "event": "dry_run",
            "target": "file",
            "url": f"{file_server.base_url}/file",
            "size": len(file_server.data),
            "done": 0,
            "pending": len(file_server.data),
            "witness": "none",
        }
    ]


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
    first = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(destination),
        reporter="quiet",
    )
    assert await app._get(first) == 0
    before = len(file_server.ranges)
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(destination),
        reporter="quiet",
        skip_existing=True,
    )
    assert await app._get(args) == 0
    fetched_after = [rng for rng in file_server.ranges[before:] if rng is not None]
    assert fetched_after == []  # no data fetch on the second run (HEAD probe is fine)


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


async def test_probe_json_stays_pure_with_verbose(
    file_server, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)
    args = app._default_get_args()
    args.targets = [f"{file_server.base_url}/file"]
    args.json = True
    console.set_level(console.VERBOSE)
    try:
        assert await app._probe(args) == 0
    finally:
        console.set_level(console.NORMAL)
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert lines
    assert all(json.loads(line)["target"] for line in lines)


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


async def test_success_does_not_create_a_failed_log(file_server, tmp_path: Path) -> None:
    destination = tmp_path / "dl"
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(destination),
        reporter="quiet",
    )
    assert await app._get(args) == 0
    assert not (destination / ".downctl.failed").exists()


async def test_retry_failed_reresolves_the_origin(file_server, tmp_path: Path) -> None:
    """A stale URL is replaced by a fresh resolution of the recorded origin."""
    destination = tmp_path / "dl"
    destination.mkdir()
    renamed = destination / "renamed.bin"
    line = "\t".join(
        [
            "url",
            "http://127.0.0.1:1/file",  # stale, but the basename still matches
            str(renamed),  # -o targets are recorded as absolute paths
            "",
            "",
            "0",
            "url",
            f"{file_server.base_url}/file",
        ]
    )
    (destination / ".downctl.failed").write_text(line + "\n", encoding="utf-8")

    args = make_args(dir=str(destination), reporter="quiet", retry_failed=True)
    assert await app._get(args) == 0
    assert renamed.read_bytes() == file_server.data


async def test_json_reporter_keeps_stdout_pure(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        reporter="json",
    )
    assert await app._get(args) == 0
    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.splitlines() if line]
    assert records
    assert all("event" in record for record in records)
    assert any(record["event"] == "run_finish" for record in records)
    assert "done:" not in out


async def test_json_reporter_stays_pure_with_verbose(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    console.set_level(console.VERBOSE)
    try:
        args = make_args(
            targets=[f"{file_server.base_url}/file"],
            dir=str(tmp_path / "dl"),
            reporter="json",
        )
        assert await app._get(args) == 0
    finally:
        console.set_level(console.NORMAL)
    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.splitlines() if line]
    assert records
    assert all("event" in record for record in records)
    assert "config dir=" not in out


async def test_dry_run_accounts_for_completed_pieces(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "dl"
    destination.mkdir()
    url = f"{file_server.base_url}/file"
    data = file_server.data
    piece_size = 1 << 20
    _, pieces, _ = pc.layout(len(data), 2, piece_size)
    with open(destination / "file", "wb") as fh:
        fh.write(data[:piece_size])
        fh.truncate(len(data))
    pc.save(
        str(destination / "file"),
        pc.Bitmap(
            len(data),
            pieces,
            [1] + [0] * (pieces - 1),
            len(data),
            piece_size,
            url_sha256=url_identity(url),
        ),
    )

    args = make_args(
        targets=[url],
        dir=str(destination),
        reporter="quiet",
        dry_run=True,
        connections=2,
        piece_size="1M",
    )
    assert await app._get(args) == 0
    out = capsys.readouterr().out
    assert "to fetch" in out
    assert "1.0 MB done" in out


async def test_dry_run_force_ignores_existing_completion(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "dl"
    destination.mkdir()
    url = f"{file_server.base_url}/file"
    target = destination / "file"
    target.write_bytes(file_server.data)
    assert pc.save_completion(str(target), url_identity(url))

    args = make_args(
        targets=[url],
        dir=str(destination),
        reporter="quiet",
        dry_run=True,
        force=True,
    )
    assert await app._get(args) == 0
    out = capsys.readouterr().out
    assert "0 B done" in out
    assert "0 B to fetch" not in out


async def test_force_and_skip_existing_are_rejected(file_server, tmp_path: Path) -> None:
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        reporter="quiet",
        force=True,
        skip_existing=True,
    )
    with pytest.raises(DownctlError, match="contradict"):
        await app._get(args)


async def test_unknown_checksum_algorithm_is_rejected_before_download(
    file_server, tmp_path: Path
) -> None:
    args = make_args(
        targets=[f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        reporter="quiet",
        checksum="sha3-512:" + "a" * 128,
    )
    with pytest.raises(DownctlError, match="unknown checksum algorithm"):
        await app._get(args)


async def test_probe_failure_is_recorded_for_retry(file_server, tmp_path: Path) -> None:
    destination = tmp_path / "dl"
    args = make_args(
        targets=[f"{file_server.base_url}/missing"],
        dir=str(destination),
        reporter="quiet",
    )
    with pytest.raises(DownctlError):
        await app._get(args)
    log = destination / ".downctl.failed"
    assert log.is_file()
    text = log.read_text(encoding="utf-8")
    assert "/missing" in text
    assert "\t" in text


async def test_resolve_failure_is_recorded_for_retry(tmp_path: Path) -> None:
    destination = tmp_path / "dl"
    args = make_args(targets=["foo://nothing"], dir=str(destination), reporter="quiet")
    with pytest.raises(DownctlError):
        await app._get(args)
    log = destination / ".downctl.failed"
    assert log.is_file()
    assert "foo://nothing" in log.read_text(encoding="utf-8")


async def test_json_reports_pre_engine_probe_failures(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = make_args(
        targets=[f"{file_server.base_url}/missing", f"{file_server.base_url}/file"],
        dir=str(tmp_path / "dl"),
        reporter="json",
        continue_=True,
    )
    assert await app._get(args) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    failure = [record for record in records if record.get("event") == "failure"]
    assert failure and failure[0]["target"] == "missing"
    finish = [record for record in records if record.get("event") == "run_finish"]
    assert finish and finish[0] == {"event": "run_finish", "succeeded": 1, "failed": 1}


async def test_json_reports_a_resolve_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = make_args(
        targets=["foo://nothing"],
        dir=str(tmp_path / "dl"),
        reporter="json",
    )
    with pytest.raises(DownctlError):
        await app._get(args)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert any(
        record.get("event") == "failure" and record.get("target") == "foo://nothing"
        for record in records
    )


async def test_retry_of_a_legacy_lossy_source_warns(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "dl"
    destination.mkdir()
    url = f"{file_server.base_url}/file"
    # A 0.1.2-style record: it knows the resolved URL and source name, but has
    # no origin, so credentials/signatures cannot be refreshed.
    (destination / ".downctl.failed").write_text(
        f"url\t{url}\tfile\t\t\t0\thuggingface\t\n",
        encoding="utf-8",
    )
    args = make_args(retry_failed=True, dir=str(destination), reporter="quiet")
    assert await app._get(args) == 0
    assert "predates origin tracking" in capsys.readouterr().err


def test_invalid_numeric_options_are_rejected(tmp_path: Path) -> None:
    for override in (
        {"jobs": 0},
        {"connections": 0},
        {"stall": 0},
        {"retries": -1},
        {"checkpoint": -1},
        {"max_time": -1},
        {"probe_jobs": -1},
        {"piece_size": "1"},
        {"limit_rate": "0"},
    ):
        with pytest.raises(DownctlError):
            app._build_config(make_args(**override))


async def test_get_follows_a_redirect_in_the_download_chain(file_server, tmp_path: Path) -> None:
    """Probe, pieces and completion must all survive a 302."""
    args = make_args(
        targets=[file_server.url("redirect")], dir=str(tmp_path / "dl"), reporter="quiet"
    )
    assert await app._get(args) == 0
    assert (tmp_path / "dl" / "redirect").read_bytes() == file_server.data
    assert file_server.state.count("redirect") >= 1


async def test_get_handles_a_unicode_target_name(file_server, tmp_path: Path) -> None:
    """A CJK destination path must reach the filesystem intact.

    The marker, the ledger path and the lock all derive from the target name,
    so an encoding mistake anywhere in that chain shows up here.
    """
    name = "模型-ダウンロード-Ω.bin"
    destination = tmp_path / "下载目录"
    args = make_args(
        targets=[file_server.url("file")],
        dir=str(destination),
        output=str(destination / name),
        reporter="quiet",
    )
    assert await app._get(args) == 0
    written = destination / name
    assert written.read_bytes() == file_server.data

    marker = pc.load_completion(str(written))
    assert marker is not None
    assert marker["size"] == len(file_server.data)
    assert not Path(str(written) + ".lock").exists()


async def test_get_reports_a_missing_directory_cleanly(tmp_path: Path) -> None:
    """A destination that cannot be created is a clean error, not a traceback."""
    args = make_args(
        targets=["file:///nonexistent/path/x.bin"],
        dir=str(tmp_path / "dl"),
        reporter="quiet",
        continue_=True,
    )
    assert await app._get(args) == 1


async def test_mirror_flag_rewrites_what_is_fetched(file_server, tmp_path: Path) -> None:
    """``--mirror`` must change the URL actually fetched, not just the config."""
    args = make_args(
        targets=["http://origin.example/file"],
        dir=str(tmp_path / "dl"),
        reporter="quiet",
        mirror=f"http://origin.example={file_server.base_url}",
    )
    assert await app._get(args) == 0
    assert (tmp_path / "dl" / "file").read_bytes() == file_server.data
    assert file_server.ranges  # the mirror really was used


async def test_piece_size_flag_shapes_the_ledger(file_server, tmp_path: Path) -> None:
    """``--piece-size`` must reach the piece grid, not only the config object."""
    args = make_args(
        targets=[file_server.url("slow")],
        dir=str(tmp_path / "dl2"),
        reporter="quiet",
        piece_size="1M",
        connections=1,
        max_time=1,  # interrupt mid-transfer: the ledger is the evidence
    )
    file_server.state.chunk_delay = 0.2

    assert await app._get(args) == 1
    bitmap = pc.load(str(tmp_path / "dl2" / "slow"))
    assert bitmap is not None
    assert bitmap.piece_size == 1 << 20
    assert bitmap.pieces == 6


async def test_checkpoint_zero_disables_periodic_snapshots(file_server, tmp_path: Path) -> None:
    """``--checkpoint 0`` keeps the ledger off disk until the transfer ends."""
    import asyncio
    import contextlib

    import aiohttp

    from downctl.config import Config, CoreConfig
    from downctl.core.context import RuntimeContext
    from downctl.core.engine import download_task
    from downctl.core.task import DownloadTask as Task
    from downctl.plugin.loader import discover

    file_server.state.chunk_delay = 0.05
    target = str(tmp_path / "nocheckpoint.bin")
    async with aiohttp.ClientSession() as client:
        ctx = RuntimeContext(
            session=client,
            config=Config(core=CoreConfig(dir=str(tmp_path))),
            registry=discover(),
        )
        job = asyncio.create_task(
            download_task(
                ctx,
                Task(
                    url=file_server.url("slow"),
                    target="nocheckpoint.bin",
                    size=len(file_server.data),
                ),
                connections=1,
                piece_size=1 << 20,
                checkpoint=0,
                stall_limit=30,
            )
        )
        try:
            await asyncio.sleep(1.0)
            assert pc.load(target) is None, "checkpoint=0 must not snapshot mid-transfer"
        finally:
            job.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job
    # The interrupted transfer still persisted its state on the way out.
    assert pc.load(target) is not None
