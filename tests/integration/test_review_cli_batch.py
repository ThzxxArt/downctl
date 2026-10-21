"""Regressions for the batch/CLI honesty fixes found in the 0.1.6 review.

Every test here is a *user-visible* contract: a command-line switch that a job
file must not undo, a usage error that has to be decided before the first byte
is fetched, a failed-log record that must survive a retry byte for byte, or a
secret that must not reach stdout/stderr. They exercise the same argparse
``Namespace`` the real CLI builds, so they fail when the seam between parsing,
planning and the engine drifts again.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from aiohttp import web

from downctl import app, cli, console
from downctl.core.task import DownloadTask


@pytest_asyncio.fixture
async def origin_server() -> AsyncIterator[tuple[str, bytes]]:
    """A local file server plus a path (``/fail.bin``) that always answers 404."""
    data = b"review batch payload: " + b"x" * 65536

    async def handle(request: web.Request) -> web.Response:
        if request.path == "/fail.bin":
            return web.Response(status=404, text="missing")
        return web.Response(body=data, headers={"Content-Length": str(len(data))})

    application = web.Application()
    application.router.add_get("/{name}", handle)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", data
    finally:
        await runner.cleanup()


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the user's real config and console globals out of these tests."""
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "absent.toml"))
    console.set_level(console.NORMAL)
    console.set_machine_readable(False)
    yield
    console.set_level(console.NORMAL)
    console.set_machine_readable(False)


def parse(*argv: str) -> argparse.Namespace:
    """Parse a real command line so the test sees exactly what the CLI sees."""
    return cli.build_parser().parse_args(list(argv))


def write_jobs(path: Path, body: str) -> Path:
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 1. ``batch --dry-run`` is the user's decision, not a job file's default
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_batch_dry_run_cannot_be_turned_off_by_a_job(
    origin_server: tuple[str, bytes], tmp_path: Path
) -> None:
    """A job saying ``dry_run = false`` must not re-enable downloading.

    Before the fix, ``_job_namespace`` let the job's false override the CLI's
    true, so ``batch --dry-run`` wrote the file the user asked it to preview.
    """
    base, _data = origin_server
    jobs = write_jobs(
        tmp_path / "jobs.toml",
        f"""
version = 1

[[job]]
name = "sneaky"
targets = ["{base}/payload.bin"]
dir = "out"
dry_run = false
""",
    )
    args = parse("batch", str(jobs), "--dry-run")
    args.reporter = "quiet"

    assert await asyncio.to_thread(app.cmd_batch, args) == 0
    assert not (tmp_path / "out" / "payload.bin").exists()
    # A dry run is not allowed to create the destination directory either.
    assert not (tmp_path / "out").exists()


# --------------------------------------------------------------------------- #
# 2. A per-job directory error is that job's failure, not the batch's aborter
# --------------------------------------------------------------------------- #
def test_batch_continue_survives_a_directory_creation_error(tmp_path: Path) -> None:
    """A ``dir`` under a regular file fails job 1; ``--continue`` still runs job 2.

    ``os.makedirs`` used to sit outside the per-job ``try``: the error escaped
    the loop and even ``--continue`` could not reach the remaining jobs.
    """
    first = tmp_path / "one.bin"
    first.write_bytes(b"first")
    second = tmp_path / "two.bin"
    second.write_bytes(b"second")
    # ``blocker`` is a regular file, so ``blocker/out`` cannot be a directory.
    (tmp_path / "blocker").write_bytes(b"not a directory")
    jobs = write_jobs(
        tmp_path / "jobs.toml",
        f"""
version = 1

[[job]]
name = "blocked"
targets = ["{first.as_uri()}"]
dir = "blocker/out"

[[job]]
name = "fine"
targets = ["{second.as_uri()}"]
dir = "out"
""",
    )

    stop = parse("batch", str(jobs))
    stop.reporter = "quiet"
    assert app.cmd_batch(stop) == 1
    assert not (tmp_path / "out" / "two.bin").exists()  # job 2 was never started

    keep = parse("batch", str(jobs), "--continue")
    keep.reporter = "quiet"
    assert app.cmd_batch(keep) == 1
    assert (tmp_path / "out" / "two.bin").read_bytes() == b"second"


# --------------------------------------------------------------------------- #
# 3. Batch pre-validation: exit 2, fail fast, download nothing
# --------------------------------------------------------------------------- #
def test_batch_rejects_force_plus_skip_existing_before_downloading(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Contradictory per-job flags are a usage error (2), not a failed job (1)."""
    source = tmp_path / "a.bin"
    source.write_bytes(b"payload")
    jobs = write_jobs(
        tmp_path / "jobs.toml",
        f"""
version = 1

[[job]]
name = "contradiction"
targets = ["{source.as_uri()}"]
dir = "out"
force = true
skip_existing = true
""",
    )
    args = parse("batch", str(jobs))
    args.reporter = "quiet"

    assert app.cmd_batch(args) == 2
    assert "contradict" in capsys.readouterr().err
    assert not (tmp_path / "out" / "a.bin").exists()


def test_batch_rejects_a_checksum_for_multiple_targets_before_downloading(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A single digest cannot describe two files; refuse the whole batch first."""
    first = tmp_path / "a.bin"
    first.write_bytes(b"a")
    second = tmp_path / "b.bin"
    second.write_bytes(b"b")
    jobs = write_jobs(
        tmp_path / "jobs.toml",
        f"""
version = 1

[[job]]
name = "two-targets"
targets = ["{first.as_uri()}", "{second.as_uri()}"]
dir = "out"
checksum = "sha256:{"0" * 64}"
""",
    )
    args = parse("batch", str(jobs))
    args.reporter = "quiet"

    assert app.cmd_batch(args) == 2
    assert "checksum applies to a single target" in capsys.readouterr().err
    assert not (tmp_path / "out" / "a.bin").exists()
    assert not (tmp_path / "out" / "b.bin").exists()


def test_batch_rejects_unknown_top_level_keys_before_downloading(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``dir`` above ``[[job]]`` used to be ignored, silently using the default."""
    source = tmp_path / "a.bin"
    source.write_bytes(b"payload")
    jobs = write_jobs(
        tmp_path / "jobs.toml",
        f"""
version = 1
dir = "x"

[[job]]
name = "x"
targets = ["{source.as_uri()}"]
dir = "out"
""",
    )
    args = parse("batch", str(jobs))
    args.reporter = "quiet"

    assert app.cmd_batch(args) == 2
    assert "unknown top-level key" in capsys.readouterr().err
    assert not (tmp_path / "out" / "a.bin").exists()


# --------------------------------------------------------------------------- #
# 4. ``--retry-failed`` must not replace a header-bound record with a bare path
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_retry_failed_keeps_a_header_bound_record_byte_identical(
    origin_server: tuple[str, bytes],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The retry without ``-H`` cannot replay the record, so it must keep it.

    The old behaviour appended a ``target<TAB>...`` line for the skipped record,
    which replaced the URL/header-name record and made the next retry say "no
    source plugin can handle". The warning also has to ask for the header again
    rather than pretending the server changed.
    """
    base, _data = origin_server
    out = tmp_path / "out"
    first = parse("get", f"{base}/fail.bin", "-d", str(out), "-H", "X-Job: secret")
    first.reporter = "quiet"
    assert await asyncio.to_thread(app.cmd_get, first) == 1

    failed_log = out / ".downctl.failed"
    assert failed_log.is_file()
    before = failed_log.read_bytes()
    lines = [line for line in before.decode(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    columns = lines[0].split("\t")
    assert columns[0] == "url"
    # Column 9 is the comma-separated set of header *names* (values are never
    # written to disk), which is what makes a retry without -H decidable.
    assert "X-Job" in columns[9].split(",")
    capsys.readouterr()  # discard the first run's failure message

    retry = parse("get", "--retry-failed", "-d", str(out))
    retry.reporter = "quiet"
    assert await asyncio.to_thread(app.cmd_get, retry) == 1

    assert failed_log.read_bytes() == before
    err = capsys.readouterr().err
    assert "X-Job" in err
    assert "not stored" in err
    assert "same -H option(s)" in err


# --------------------------------------------------------------------------- #
# 5. A record whose URL lost a credential is refused, never replayed redacted
# --------------------------------------------------------------------------- #
def test_retry_failed_refuses_a_masked_credential_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``?token=`` is masked on disk, so replaying it cannot work by design."""
    out = tmp_path / "out"
    out.mkdir()
    line = app._fmt_url(DownloadTask(url="https://h/x?token=SECRET", target="x", size=1))
    failed_log = out / ".downctl.failed"
    app._write_failed_log(str(failed_log), [line])
    before = failed_log.read_bytes()

    retry = parse("get", "--retry-failed", "-d", str(out))
    retry.reporter = "quiet"
    assert app.cmd_get(retry) == 1

    assert failed_log.read_bytes() == before
    err = capsys.readouterr().err
    assert "deliberately not stored" in err
    assert "SECRET" not in err  # the masked URL is not un-masked in the message


# --------------------------------------------------------------------------- #
# 6. ``probe`` redacts the URL embedded in an error, including in JSON
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_probe_never_prints_a_url_credential(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The probe error carries the request URL; ``?token=`` must not survive it."""
    secret = "SECRET"
    url = f"http://h/x?token={secret}"

    async def fake_probe(
        session: object, target: str, *, proxy: str | None = None, policy: object = None
    ) -> dict[str, object]:
        return {
            "target": target,
            "final_url": target,
            "status": None,
            "reachable": False,
            "size": 0,
            "content_type": "",
            "filename": "",
            "error": f"Connection timeout to host {target}",
        }

    monkeypatch.setattr(app, "probe_resource", fake_probe)
    assert await app._probe(parse("probe", url)) == 1
    out = capsys.readouterr().out
    assert secret not in out
    assert "***" in out

    assert await app._probe(parse("probe", url, "--json")) == 1
    payload = json.loads(capsys.readouterr().out)
    assert secret not in payload["target"]
    assert secret not in payload["final_url"]
    assert secret not in payload["error"]
    assert "***" in payload["error"]


# --------------------------------------------------------------------------- #
# 7. ``config show`` masks plugin passphrases and auth commands
# --------------------------------------------------------------------------- #
def test_config_show_masks_passphrases_and_auth_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``command`` can embed a token, so its whole value is hidden, not scanned."""
    config = tmp_path / "config.toml"
    config.write_text(
        """
[plugins.logfile]
key_passphrase = "hunter2"

[auth."h.example"]
provider = "command"
command = "echo ghp_SECRET"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOWNCTL_CONFIG", str(config))

    assert app.cmd_config(parse("config", "show")) == 0
    captured = capsys.readouterr()
    shown = captured.out + captured.err
    assert "hunter2" not in shown
    assert "ghp_SECRET" not in shown
    assert "***" in shown


# --------------------------------------------------------------------------- #
# 8. ``verify --checksums`` cannot walk outside the directory it was given
# --------------------------------------------------------------------------- #
def test_verify_marks_absolute_and_parent_manifest_entries_unverifiable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An entry outside the tree is not proof; it is unverifiable, and exit 3.

    The old code skipped such keys silently, so a manifest that named only
    absolute/``..`` paths made ``verify`` print "nothing to verify" and exit 0.
    """
    directory = tmp_path / "downloads"
    directory.mkdir()
    content = b"inside"
    (directory / "a.bin").write_bytes(content)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    digest = hashlib.sha256(content).hexdigest()
    placeholder = "0" * 64
    manifest = tmp_path / "checksums.txt"
    manifest.write_text(
        f"{digest}  a.bin\n{placeholder}  {outside.as_posix()}\n{placeholder}  ../escape.bin\n",
        encoding="utf-8",
    )

    args = parse("verify", str(directory), "--checksums", str(manifest))
    assert app.cmd_verify(args) == 3
    out = capsys.readouterr().out
    assert "unverifiable" in out
    assert "nothing to verify" not in out

    # The old failure mode exactly: when *only* out-of-tree entries are named,
    # the old code dropped them all and printed "nothing to verify" with exit 0.
    outside_only = tmp_path / "outside-only.txt"
    outside_only.write_text(
        f"{placeholder}  {outside.as_posix()}\n{placeholder}  ../escape.bin\n",
        encoding="utf-8",
    )
    only_args = parse("verify", str(directory), "--checksums", str(outside_only))
    assert app.cmd_verify(only_args) == 3
    only_out = capsys.readouterr().out
    assert "unverifiable" in only_out
    assert "nothing to verify" not in only_out

    json_args = parse("verify", str(directory), "--checksums", str(manifest), "--json")
    assert app.cmd_verify(json_args) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] == 1
    assert payload["unverifiable"] == 2
    details = " ".join(str(entry.get("detail", "")) for entry in payload["entries"])
    assert "not a relative path inside" in details


# --------------------------------------------------------------------------- #
# 9. ``downctl auth`` without a subcommand is a usage error, not a crash
# --------------------------------------------------------------------------- #
def test_auth_without_a_subcommand_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    """It used to reach ``args.urls`` and raise AttributeError (reported as 1)."""
    assert app.cmd_auth(parse("auth")) == 2
    assert "usage: downctl auth check" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 10. ``--no-proxy`` is stronger than an explicit ``-p``, whatever the order
# --------------------------------------------------------------------------- #
def test_no_proxy_wins_over_an_explicit_proxy() -> None:
    """A direct connection must not be silently overridden by ``-p``."""
    args = parse("get", "http://example.invalid/x", "--no-proxy", "-p", "http://proxy:1")
    cfg = app._build_config(args, log=False)
    assert cfg.core.proxy == ""


# --------------------------------------------------------------------------- #
# 11. Completion scripts know every public command
# --------------------------------------------------------------------------- #
def test_completion_script_lists_batch_serve_and_auth(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``batch``/``serve``/``auth`` were missing from the generated command list."""
    assert app.cmd_completions(parse("completions")) == 0
    out = capsys.readouterr().out
    assert "batch" in out
    assert "serve" in out
    assert "auth" in out


# --------------------------------------------------------------------------- #
# 12. ``serve --auth ""`` cannot silently disable authentication
# --------------------------------------------------------------------------- #
class _ExplodingServer:
    """A server plugin that fails loudly if ``serve`` is ever reached."""

    async def serve(self, root: str, ctx: object, options: dict[str, object]) -> None:
        raise AssertionError("server started despite an empty --auth")


class _FakeRegistry:
    """Only the one lookup ``cmd_serve`` needs; never starts a real server."""

    def get(self, category: str, name: str) -> object | None:
        return _ExplodingServer() if category == "server" else None

    def names(self, category: str) -> list[str]:
        return []


def test_serve_refuses_an_empty_auth_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty string is falsy; treating it as "no auth" exposed the tree."""
    monkeypatch.setattr(app, "_registry", lambda include_local: _FakeRegistry())
    args = parse("serve", str(tmp_path), "--auth", "")
    assert app.cmd_serve(args) == 1
    err = capsys.readouterr().err
    assert "--auth needs USER:PASS" in err
    assert "empty value" in err
