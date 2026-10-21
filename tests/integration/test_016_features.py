"""End-to-end tests for the 0.1.6 features: batch, auth, serve wiring, plugins.

These exercise the *CLI-shaped* path (the same ``Namespace`` argparse builds),
because the bugs worth catching here live at the seam between parsing, planning
and the engine — not inside any one module.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from downctl import app, cli, console
from downctl.config import Config, CoreConfig
from downctl.core import pieces as pc
from downctl.core.context import RuntimeContext
from downctl.core.engine import download_task
from downctl.core.task import DownloadTask
from downctl.plugin.loader import discover
from downctl.plugin.testing import check_plugin


@pytest_asyncio.fixture
async def session():
    async with aiohttp.ClientSession() as client:
        yield client


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "absent.toml"))


def parse(*argv: str) -> argparse.Namespace:
    """Parse a real command line, then let a test tweak fields."""
    return cli.build_parser().parse_args(list(argv))


def get_args(*targets: str, **overrides: object) -> argparse.Namespace:
    args = parse("get", *targets)
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def make_ctx(session, tmp_path: Path, **core: object) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path), **core)),  # type: ignore[arg-type]
        registry=discover(),
    )


@pytest.fixture(autouse=True)
def _quiet_console():
    console.set_level(console.NORMAL)
    console.set_machine_readable(False)
    yield
    console.set_level(console.NORMAL)
    console.set_machine_readable(False)


# --------------------------------------------------------------------------- #
# batch
# --------------------------------------------------------------------------- #
def write_jobs(path: Path, body: str) -> Path:
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


async def test_batch_runs_each_job_with_its_own_options(file_server, tmp_path: Path) -> None:
    jobs = write_jobs(
        tmp_path / "jobs.toml",
        f"""
version = 1
[[job]]
name = "first"
targets = ["{file_server.url("file")}"]
dir = "out/first"
[[job]]
name = "second"
targets = ["{file_server.url("versioned")}"]
dir = "out/second"
""",
    )
    args = parse("batch", str(jobs))
    args.reporter = "quiet"

    assert await app._batch(args) == 0
    assert (tmp_path / "out/first/file").read_bytes() == file_server.data
    assert (tmp_path / "out/second/versioned").is_file()


async def test_batch_job_filter_runs_only_the_named_job(file_server, tmp_path: Path) -> None:
    jobs = write_jobs(
        tmp_path / "jobs.toml",
        f"""
version = 1
[[job]]
name = "first"
targets = ["{file_server.url("file")}"]
dir = "out/first"
[[job]]
name = "second"
targets = ["{file_server.url("versioned")}"]
dir = "out/second"
""",
    )
    args = parse("batch", str(jobs), "--job", "second")
    args.reporter = "quiet"

    assert await app._batch(args) == 0
    assert (tmp_path / "out/second/versioned").is_file()
    assert not (tmp_path / "out/first/file").exists()


async def test_a_failed_job_stops_the_batch_unless_continue(file_server, tmp_path: Path) -> None:
    jobs = write_jobs(
        tmp_path / "jobs.toml",
        f"""
version = 1
[[job]]
name = "broken"
targets = ["{file_server.url("always-404")}"]
dir = "out/broken"
[[job]]
name = "fine"
targets = ["{file_server.url("file")}"]
dir = "out/fine"
""",
    )
    stop = parse("batch", str(jobs))
    stop.reporter = "quiet"
    stop.retries = 0
    assert await app._batch(stop) == 1
    assert not (tmp_path / "out/fine/file").exists()
    # The failed log lives in the *job's* destination, so
    # ``get --retry-failed -d out/broken`` replays it into the same place.
    assert (tmp_path / "out/broken" / ".downctl.failed").is_file()

    keep = parse("batch", str(jobs), "--continue")
    keep.reporter = "quiet"
    keep.retries = 0
    assert await app._batch(keep) == 1
    assert (tmp_path / "out/fine/file").read_bytes() == file_server.data


async def test_an_invalid_job_file_exits_2_and_downloads_nothing(tmp_path: Path) -> None:
    jobs = write_jobs(tmp_path / "jobs.toml", "version = 2")
    args = parse("batch", str(jobs))

    assert await asyncio.to_thread(app.cmd_batch, args) == 2
    assert not any(tmp_path.rglob("downctl-done"))


async def test_an_unknown_job_key_is_refused_before_any_download(
    file_server, tmp_path: Path
) -> None:
    jobs = write_jobs(
        tmp_path / "jobs.toml",
        f"""
version = 1
[[job]]
name = "x"
targets = ["{file_server.url("file")}"]
dir = "out"
weird = true
""",
    )
    args = parse("batch", str(jobs))

    assert await asyncio.to_thread(app.cmd_batch, args) == 2
    assert not (tmp_path / "out/file").exists()


async def test_batch_events_carry_the_job_name(
    file_server, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs = write_jobs(
        tmp_path / "jobs.toml",
        f"""
version = 1
[[job]]
name = "alpha"
targets = ["{file_server.url("file")}"]
dir = "out/a"
[[job]]
name = "beta"
targets = ["{file_server.url("versioned")}"]
dir = "out/b"
""",
    )
    args = parse("batch", str(jobs), "--reporter", "json")
    assert await app._batch(args) == 0

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    starts = [record["job"] for record in records if record["event"] == "run_start"]
    finishes = [record["job"] for record in records if record["event"] == "run_finish"]
    assert starts == ["alpha", "beta"]
    assert finishes == ["alpha", "beta"]
    assert all(record["succeeded"] == 1 for record in records if record["event"] == "run_finish")
    # Per-task records carry the job too, so a JSON consumer never has to infer
    # which run a line belongs to.
    task_records = [record for record in records if record["event"] in {"start", "finish"}]
    assert task_records
    assert {record["job"] for record in task_records} == {"alpha", "beta"}


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def write_auth_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


async def test_a_provider_authenticates_a_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole chain: config -> provider -> policy_request -> transport."""
    seen: list[str | None] = []
    data = b"provider-authenticated bytes"

    async def handler(request: web.Request) -> web.StreamResponse:
        seen.append(request.headers.get("X-Api-Key"))
        if request.headers.get("X-Api-Key") != "let-me-in":
            return web.Response(status=401, text="no")
        headers = {"ETag": '"v1"', "Accept-Ranges": "bytes"}
        raw = request.headers.get("Range", "")
        if raw.startswith("bytes="):
            start_text, _, end_text = raw[len("bytes=") :].partition("-")
            start = int(start_text)
            end = int(end_text) if end_text else len(data) - 1
            return web.Response(
                status=206,
                body=data[start : end + 1],
                headers={**headers, "Content-Range": f"bytes {start}-{end}/{len(data)}"},
            )
        return web.Response(body=data, headers=headers)

    application = web.Application()
    application.router.add_route("*", "/private", handler)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        config = write_auth_config(
            tmp_path,
            """
[auth."127.0.0.1"]
provider = "env"
env = "DEMO_TOKEN"
header = "X-Api-Key"
prefix = ""
""",
        )
        monkeypatch.setenv("DOWNCTL_CONFIG", str(config))
        monkeypatch.setenv("DEMO_TOKEN", "let-me-in")
        args = get_args(
            f"http://127.0.0.1:{port}/private",
            dir=str(tmp_path / "dl"),
            reporter="quiet",
        )
        assert await app._get(args) == 0
        assert (tmp_path / "dl/private").read_bytes() == data
        assert seen and all(value == "let-me-in" for value in seen)
    finally:
        await runner.cleanup()


async def test_auth_check_names_the_header_but_never_the_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_auth_config(
        tmp_path,
        """
[auth."example.test"]
provider = "env"
env = "DEMO_TOKEN"
header = "X-Api-Key"
prefix = ""
""",
    )
    monkeypatch.setenv("DOWNCTL_CONFIG", str(config))
    monkeypatch.setenv("DEMO_TOKEN", "SECRET-VALUE")
    args = parse("auth", "check", "https://example.test/x")

    assert await asyncio.to_thread(app.cmd_auth, args) == 0
    out = capsys.readouterr().out
    assert "X-Api-Key" in out
    assert "env DEMO_TOKEN" in out
    assert "SECRET-VALUE" not in out


async def test_auth_check_reports_a_missing_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_auth_config(
        tmp_path,
        """
[auth."example.test"]
provider = "does-not-exist"
""",
    )
    monkeypatch.setenv("DOWNCTL_CONFIG", str(config))
    args = parse("auth", "check", "https://example.test/x")

    assert await asyncio.to_thread(app.cmd_auth, args) == 1
    err = capsys.readouterr().err
    assert "unknown auth provider" in err


async def test_probe_uses_the_configured_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``probe`` asks the same private server a download would read."""

    async def handler(request: web.Request) -> web.StreamResponse:
        if request.headers.get("X-Api-Key") != "let-me-in":
            return web.Response(status=401, text="no")
        return web.Response(body=b"x" * 10, headers={"Content-Length": "10", "ETag": '"v1"'})

    application = web.Application()
    application.router.add_route("*", "/private", handler)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        config = write_auth_config(
            tmp_path,
            """
[auth."127.0.0.1"]
provider = "env"
env = "DEMO_TOKEN"
header = "X-Api-Key"
prefix = ""
""",
        )
        monkeypatch.setenv("DOWNCTL_CONFIG", str(config))
        monkeypatch.setenv("DEMO_TOKEN", "let-me-in")
        args = parse("probe", f"http://127.0.0.1:{port}/private", "--json")
        assert await app._probe(args) == 0
        record = json.loads(capsys.readouterr().out)
        assert record["reachable"] is True
        assert record["size"] == 10
    finally:
        await runner.cleanup()


# --------------------------------------------------------------------------- #
# serve wiring and plugin tooling
# --------------------------------------------------------------------------- #
def test_serve_selects_the_http_server_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from downctl.builtin.servers.http_server import HTTPFileServer

    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_serve(self, root: str, ctx: RuntimeContext, options: dict[str, object]) -> None:
        calls.append((root, dict(options)))

    monkeypatch.setattr(HTTPFileServer, "serve", fake_serve)
    args = parse("serve", str(tmp_path), "--port", "18099", "--auth", "u:p", "--index")

    assert app.cmd_serve(args) == 0
    assert calls == [
        (
            str(tmp_path),
            {"bind": "127.0.0.1", "port": 18099, "index": True, "quiet": False, "auth": "u:p"},
        )
    ]


def test_plugin_new_produces_a_plugin_that_passes_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from downctl.plugin.scaffold import scaffold

    files = scaffold("demo", "source", str(tmp_path))
    assert len(files) == 5
    monkeypatch.syspath_prepend(str(tmp_path / "demo_plugin"))
    from demo_plugin.plugin import DemoSource  # type: ignore[import-not-found]

    assert check_plugin(DemoSource(), "source") == []

    args = parse("plugin", "check", "demo_plugin.plugin")
    assert app.cmd_plugin(args) == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "api_version 1" in out


def test_plugin_check_fails_for_a_broken_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "broken_plugin"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "plugin.py").write_text(
        "class Broken:\n"
        "    name = 'broken'\n"
        "    priority = 0\n"
        "    def can_handle(self, target):\n"
        "        return False\n"
        "    async def resolve(self, target, ctx):\n"
        "        return []\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    args = parse("plugin", "check", "broken_plugin.plugin")
    assert app.cmd_plugin(args) == 1
    assert "api_version" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# promises that used to be bypassed
# --------------------------------------------------------------------------- #
async def test_require_witness_refuses_a_full_size_file_without_state(
    file_server, session, tmp_path: Path
) -> None:
    """A ``cp``-ed file is a heuristic, not a witness.

    The hole-scan branch of ``_completed_bytes`` used to be honoured even under
    ``--require-witness``, so the flag was bypassed on the one path that needs
    it most.
    """
    url = file_server.url("no-etag")
    target = tmp_path / "n.bin"
    target.write_bytes(file_server.data)  # full size, hole-free, no sidecars
    assert not pc.has_state(str(target))

    task = DownloadTask(url=url, target="n.bin", size=len(file_server.data))
    task.meta["validator"] = ""
    ok = await download_task(
        make_ctx(session, tmp_path, require_witness=True),
        task,
        skip_existing=True,
        stall_limit=5,
    )
    assert ok
    assert file_server.ranges, "the file was skipped without a witness"


def test_repair_never_follows_a_manifest_through_a_symlinked_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.bin"
    secret.write_bytes(b"do not delete me")

    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    sums = tmp_path / "SUMS"
    sums.write_text(f"{'a' * 64}  link/secret.bin\n", encoding="utf-8")
    args = parse("verify", str(root), "--checksums", str(sums), "--repair")

    # The entry resolves outside the directory: it is reported as unprovable
    # (exit 3), never judged against the digest, and never deleted.
    assert app.cmd_verify(args) == 3
    assert secret.read_bytes() == b"do not delete me"
    assert "resolves outside" in capsys.readouterr().err


def test_quiet_still_prints_config_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DOWNCTL_CONFIG", str(tmp_path / "absent.toml"))
    console.set_level(console.QUIET)
    try:
        assert app.cmd_config(parse("config", "show")) == 0
    finally:
        console.set_level(console.NORMAL)
    out = capsys.readouterr().out
    assert "dir" in out
    assert "retries" in out
