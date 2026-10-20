"""Regression tests for issues found in the full-project review."""

from __future__ import annotations

import argparse
import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from downctl import app, console
from downctl.builtin.hooks.logfile_hook import LogfileHook
from downctl.builtin.reporters.json_reporter import JSONReporter
from downctl.builtin.sources.url_source import filename_from_url
from downctl.config import Config, CoreConfig, load_config
from downctl.core import engine
from downctl.core import pieces as piece_store
from downctl.core.context import RuntimeContext
from downctl.core.errors import ChecksumError, DownctlError
from downctl.core.locks import FileLock, LockError
from downctl.core.task import DownloadTask
from downctl.plugin.loader import discover


# --- path traversal ------------------------------------------------------- #
def test_filename_from_url_strips_directories() -> None:
    assert filename_from_url("https://h/x%2F..%2F..%2Fetc%2Fpasswd") == "passwd"
    assert filename_from_url("https://h/a%2Fb%2Fc.bin") == "c.bin"
    assert filename_from_url("https://h/..%2Fsecret") == "secret"


def test_filename_from_url_empty_raises_downctl_error() -> None:
    with pytest.raises(DownctlError):
        filename_from_url("https://h/")


def test_engine_confines_relative_targets(tmp_path: Path) -> None:
    ctx = RuntimeContext(session=None, config=Config(core=CoreConfig(dir=str(tmp_path))))
    task = DownloadTask(url="u", target="../escape.bin")
    with pytest.raises(DownctlError, match="escapes the destination"):
        engine._resolve_target(ctx, task)


def test_engine_allows_absolute_target(tmp_path: Path) -> None:
    ctx = RuntimeContext(session=None, config=Config(core=CoreConfig(dir=str(tmp_path / "d"))))
    out = tmp_path / "explicit" / "x.bin"
    task = DownloadTask(url="u", target=str(out), allow_absolute=True)
    assert engine._resolve_target(ctx, task) == str(out)


def test_engine_rejects_source_absolute_target(tmp_path: Path) -> None:
    ctx = RuntimeContext(session=None, config=Config(core=CoreConfig(dir=str(tmp_path / "d"))))
    task = DownloadTask(url="u", target=str(tmp_path / "elsewhere" / "x.bin"))
    with pytest.raises(DownctlError, match="escapes the destination"):
        engine._resolve_target(ctx, task)


# --- piece bitmap validation ---------------------------------------------- #
def test_bitmap_from_dict_rejects_length_mismatch() -> None:
    assert piece_store.Bitmap.from_dict({"size": 100, "pieces": 4, "bits": [1, 1]}) is None


def test_bitmap_load_rejects_short_bits(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"x" * 100)
    Path(piece_store.path_for(target)).write_text(
        '{"size": 100, "pieces": 4, "bits": [1, 1]}', encoding="utf-8"
    )
    assert piece_store.load(target) is None


# --- lock race ------------------------------------------------------------ #
def test_lock_without_pidfile_is_held(tmp_path: Path) -> None:
    target = str(tmp_path / "a.bin")
    (Path(target).parent / "a.bin.lock").mkdir()
    with pytest.raises(LockError):
        FileLock(target).acquire()


# --- archive hardening ---------------------------------------------------- #
def test_archive_rejects_backslash_traversal(tmp_path: Path) -> None:
    from downctl.builtin.hooks.archive_hook import ArchiveHook

    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("..\\evil.txt", "boom")
    assert ArchiveHook().extract(str(archive)) is None
    assert not (tmp_path.parent / "evil.txt").exists()


# --- unknown verifier ----------------------------------------------------- #
async def test_unknown_checksum_algorithm_raises() -> None:
    ctx = RuntimeContext(
        session=None,  # type: ignore[arg-type]
        config=Config(),
        registry=discover(),
    )
    task = DownloadTask(url="u", target="f", checksum_algo="sha3-512", checksum_hex="00")
    with pytest.raises(ChecksumError, match="no verifier"):
        await engine._verify(ctx, task, str(Path("/nonexistent")))


async def test_known_but_mismatched_digest_fails_verification(tmp_path: Path) -> None:
    ctx = RuntimeContext(session=None, config=Config(), registry=discover())  # type: ignore[arg-type]
    target = tmp_path / "f.bin"
    target.write_bytes(b"payload")
    task = DownloadTask(url="u", target="f", checksum_algo="md5", checksum_hex="0" * 32)
    assert await engine._verify(ctx, task, str(target)) is False


async def test_missing_checksum_is_ok() -> None:
    ctx = RuntimeContext(session=None, config=Config(), registry=discover())  # type: ignore[arg-type]
    task = DownloadTask(url="u", target="f")
    assert await engine._verify(ctx, task, "whatever") is True


# --- json reporter throttling --------------------------------------------- #
def test_json_reporter_throttles_progress() -> None:
    """One event per whole percent, not one per byte.

    The previous version used ``size=100`` and pushed 1 byte at a time, so every
    call *was* a new percent: the "far fewer than 100 deltas" assertion could not
    fail even with the throttle removed.
    """
    stream = io.StringIO()
    reporter = JSONReporter(stream)
    task = DownloadTask(url="u", target="a", size=1000)
    reporter.task_started(task)
    for _ in range(100):
        reporter.task_progress(task, 1)  # 100 bytes of 1000: ten percent steps
    reporter.task_finished(task, True)

    lines = [ln for ln in stream.getvalue().splitlines() if ln]
    progress = [ln for ln in lines if '"event": "progress"' in ln]
    assert 0 < len(progress) <= 15, f"expected ~11 progress events, got {len(progress)}"
    assert len(progress) < 100
    assert any('"event": "finish"' in ln for ln in lines)


# --- config validation ---------------------------------------------------- #
def test_config_invalid_int_raises(tmp_path: Path) -> None:
    path = tmp_path / "c.toml"
    path.write_text("[core]\njobs = 'many'\n", encoding="utf-8")
    with pytest.raises(DownctlError, match="jobs"):
        load_config(str(path))


# --- logfile tilde expansion ---------------------------------------------- #
def test_logfile_expands_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    hook = LogfileHook(path="~/run.log")
    hook.on_event("on_finish", {"succeeded": 1, "failed": 0})
    assert (tmp_path / "run.log").is_file()


# --- tqdm reporter keys by task identity ---------------------------------- #
def test_tqdm_reporter_distinct_tasks_same_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from downctl.builtin.reporters.tqdm_reporter import TqdmReporter

    console.set_color(False)
    reporter = TqdmReporter()
    reporter.run_started(2, 20)
    t1 = DownloadTask(url="u1", target="same", size=10)
    t2 = DownloadTask(url="u2", target="same", size=10)
    reporter.task_started(t1)
    reporter.task_started(t2)
    assert len(reporter._bars) == 2  # noqa: SLF001
    reporter.task_finished(t1, True)
    reporter.task_finished(t2, True)
    reporter.run_finished(2, 0)


# --- mirror host boundary ------------------------------------------------- #
def test_mirror_router_requires_host_boundary() -> None:
    from downctl.builtin.routers.mirror_router import MirrorRouter

    router = MirrorRouter({"https://a.com": "https://b"})
    assert router.rewrite("https://a.com/x") == "https://b/x"
    assert router.rewrite("https://a.com") == "https://b"
    assert router.rewrite("https://a.com.evil.com/x") == "https://a.com.evil.com/x"


# --- webhook explicit empty events ---------------------------------------- #
def test_webhook_explicit_empty_events() -> None:
    from downctl.builtin.hooks.webhook_hook import WebhookHook

    assert WebhookHook(events=None).events == ["on_finish"]
    assert WebhookHook(events=[]).events == []


# --- sftp url decoding ---------------------------------------------------- #
def test_sftp_parse_url_decodes_credentials() -> None:
    from downctl.builtin.transports._sftp import parse_url

    target = parse_url("sftp://u%40x:p%2Fw@host:2222/dir/f.bin", {})
    assert target.user == "u@x"
    assert target.password == "p/w"
    assert target.port == 2222


# --- archive rejects FIFO on 3.11 too ------------------------------------- #
def test_archive_rejects_fifo_member(tmp_path: Path) -> None:
    from downctl.builtin.hooks.archive_hook import ArchiveHook

    archive = tmp_path / "fifo.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("pipe")
        info.type = tarfile.FIFOTYPE
        tar.addfile(info)
    assert ArchiveHook().extract(str(archive)) is None


# --- session header casing ------------------------------------------------ #
async def test_session_authorization_header_is_not_duplicated() -> None:
    from downctl.session import make_session, origin_headers

    async with make_session(token="bearer", headers={"authorization": "Basic xyz"}) as session:
        # The credential is not a session default (a default would follow every
        # redirect hop), and the caller's header still replaces the token
        # instead of being added next to it.
        assert "Authorization" not in session.headers
        assert origin_headers(session) == {"authorization": "Basic xyz"}


# --- checksum file malformed entries -------------------------------------- #
def test_load_checksums_skips_malformed(tmp_path: Path) -> None:
    sums = tmp_path / "SUMS"
    sums.write_text("sha256:  a.bin\nabc123  b.bin\n", encoding="utf-8")
    table = app._load_checksums(str(sums))
    assert "a.bin" not in table
    assert table["b.bin"] == ("sha256", "abc123")


# --- config show includes mirror/hooks ------------------------------------ #
def test_config_show_includes_mirror_and_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[core]\nmirror = { "https://a" = "https://b" }\nhooks = ["logfile"]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("DOWNCTL_CONFIG", str(cfg))
    assert app.cmd_config(argparse.Namespace(config_action="show")) == 0
    out = capsys.readouterr().out
    assert "mirror" in out
    assert "hooks" in out


# --- rate limiter normalization ------------------------------------------- #
def test_rate_limiter_zero_and_negative_are_unlimited() -> None:
    from downctl.core.ratelimit import RateLimiter

    assert RateLimiter(-1).rate is None
    assert RateLimiter(0).rate is None


# --- config rejects bad numbers ------------------------------------------- #
def test_config_rejects_negative_and_bool(tmp_path: Path) -> None:
    for body in ("[core]\njobs = -1\n", "[core]\nconnections = true\n"):
        path = tmp_path / "c.toml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(DownctlError):
            load_config(str(path))


# --- file:// netloc on POSIX ---------------------------------------------- #
def test_file_local_path_keeps_authority_on_posix() -> None:
    import os as _os

    from downctl.builtin.transports.file_transport import local_path

    if _os.name != "nt":
        assert local_path("file://host/share/x") == "//host/share/x"


# --- tqdm unknown-size concurrency falls back to per-task ----------------- #
def test_tqdm_unknown_size_concurrent_uses_per_task(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from downctl.builtin.reporters.tqdm_reporter import TqdmReporter

    console.set_color(False)
    reporter = TqdmReporter()
    reporter.run_started(2, 0, jobs=2)
    assert reporter._per_task is True  # noqa: SLF001
    reporter.run_finished(0, 0)


# --- logfile summarization is TSV-safe ------------------------------------ #
def test_logfile_summary_escapes_control_chars() -> None:
    from downctl.builtin.hooks.logfile_hook import _summarize

    text = _summarize({"error": "boom\there\nnow", "ok": False})
    assert "\t" not in text and "\n" not in text
    assert "error=boom here now" in text
    assert "ok=False" in text


# --- mirror invalid destination is ignored -------------------------------- #
def test_mirror_ignores_schemeless_destination() -> None:
    from downctl.builtin.routers.mirror_router import MirrorRouter

    assert MirrorRouter({"https://a": "b"}).rewrite("https://a/x") == "https://a/x"


# --- registry requires callable members ----------------------------------- #
def test_registry_rejects_non_callable_member() -> None:
    from downctl.plugin.registry import PluginError, Registry

    class Bad:
        name = "bad"
        api_version = "1"
        priority = 0
        can_handle = True  # not callable

        async def resolve(self, target: str, ctx: object) -> list:  # pragma: no cover
            return []

    with pytest.raises(PluginError, match="callable"):
        Registry().register("source", Bad())


# --- presigned URLs are redacted in logs ---------------------------------- #
def test_presigned_url_is_redacted() -> None:
    task = DownloadTask(
        url="http://s3/b?X-Amz-Signature=secret&X-Amz-Credential=c",
        target="b",
        meta={"redact_url": True},
    )
    assert app._display_url(task) == "http://s3/b"
    assert "X-Amz" not in app._fmt_url(task)


# --- round two: findings from the second full review ----------------------- #
def test_a_string_version_is_treated_as_unreadable_state(tmp_path: Path) -> None:
    """The *type* of ``version`` is part of the format.

    ``{"version": "9"}`` used to parse as "no version", which made an unknown
    format look like no state at all — the same mistake as ignoring a newer
    integer version.
    """
    from downctl.core.errors import FutureStateError

    target = tmp_path / "x.bin"
    target.write_bytes(b"x")
    ledger = tmp_path / "x.bin.downctl-pieces"
    ledger.write_text('{"version": "9", "size": 1, "pieces": 1, "bits": [0]}', encoding="utf-8")

    assert piece_store.future_state(str(target)) == str(ledger)
    with pytest.raises(FutureStateError):
        piece_store.discard(str(target))
    assert target.read_bytes() == b"x"


@pytest.mark.skipif(
    # ``os.getuid`` does not exist on Windows, and importing this module must
    # not depend on which platform collected it.
    not hasattr(os, "getuid") or os.getuid() == 0,
    reason="root (or Windows) ignores these file permissions",
)
def test_discard_keeps_the_sidecars_when_the_bytes_cannot_be_truncated(
    tmp_path: Path,
) -> None:
    """A failed truncation must not be followed by deleting the state.

    The sidecars are the only thing that says this file is not a complete copy.
    Deleting them after a failed truncation leaves a full-size file with no
    state, which the hole heuristic can later bless as success.
    """
    target = tmp_path / "x.bin"
    target.write_bytes(b"payload")
    assert piece_store.save(str(target), piece_store.Bitmap(size=7, pieces=1, bits=[0]))
    os.chmod(target, 0o444)
    try:
        with pytest.raises(DownctlError, match="cannot reset"):
            piece_store.discard(str(target))
        assert os.path.isfile(piece_store.path_for(str(target)))
        assert target.read_bytes() == b"payload"
    finally:
        os.chmod(target, 0o644)


def test_doctor_reports_the_proxy_the_run_would_use(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``doctor`` is not allowed to answer the proxy question itself.

    It used to call ``load_config`` while ``proxy_source`` is only assigned by
    ``_build_config``, so it printed "(none detected)" while the run was using
    an environment proxy.
    """
    for key in (
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "all_proxy",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:3128")
    monkeypatch.setattr(app, "load_config", lambda: Config(core=CoreConfig()))
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)

    assert app.cmd_doctor(argparse.Namespace()) == 0

    out = capsys.readouterr().out
    assert "http://10.9.9.9:3128" in out
    assert "source=env" in out


def test_hook_configuration_problems_survive_quiet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo in the hook config is the one message ``-q`` must not hide."""
    from downctl.plugin.hooks import EventBus

    monkeypatch.setattr(console, "_level", console.QUIET)
    cfg = Config(core=CoreConfig(hooks=["definitely-not-a-hook"]))
    app._enable_hooks(argparse.Namespace(hook=[]), cfg, discover(include_local=False), EventBus())

    assert "unknown hook plugin" in capsys.readouterr().err


def test_the_manifest_hook_reports_a_file_it_cannot_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from downctl.builtin.hooks.manifest_hook import ManifestHook

    hook = ManifestHook()
    task = DownloadTask(
        url="http://h/missing.bin",
        target="missing.bin",
        meta={"path": str(tmp_path / "missing.bin")},
    )
    hook.on_event("on_task_finish", {"task": task, "ok": True, "witness": "validator"})
    hook.on_event("on_finish", {})

    assert "cannot hash" in capsys.readouterr().err
    assert hook._tasks.failed == 1


def test_a_hook_that_raises_is_reported_even_when_quiet(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A synchronous hook failure goes through the one report that cannot be muted."""
    from downctl.plugin.hooks import EventBus

    bus = EventBus()

    def boom(_payload: dict[str, object]) -> None:
        raise RuntimeError("hook blew up")

    bus.subscribe("on_start", boom)
    monkeypatch.setattr(console, "_level", console.QUIET)

    bus.emit("on_start", {})

    assert bus.failures == 1
    assert "hook blew up" in capsys.readouterr().err


async def test_the_event_bus_counts_thread_failures_before_the_final_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from downctl.plugin.hooks import EventBus, HookTasks

    class _Threaded:
        name = "threaded"

        def __init__(self) -> None:
            self._tasks = HookTasks("threaded")

        def on_event(self, event: str, payload: dict[str, object]) -> None:
            pass

    hook = _Threaded()
    bus = EventBus()
    bus.register_hook(hook)  # type: ignore[arg-type]

    def _boom() -> None:
        raise RuntimeError("thread work failed")

    hook._tasks.spawn_thread(_boom)
    await bus.drain()

    assert bus.failures == 1
    assert "threaded" in capsys.readouterr().err


async def test_a_run_finishes_hook_work_before_it_reports_its_own_summary() -> None:
    """``on_finish.hooks_failed`` has to be final, not a snapshot taken early."""
    from downctl.plugin.hooks import EventBus, HookTasks

    class _Threaded:
        name = "threaded"

        def __init__(self) -> None:
            self._tasks = HookTasks("threaded")

        def on_event(self, event: str, payload: dict[str, object]) -> None:
            pass

    hook = _Threaded()
    bus = EventBus()
    bus.register_hook(hook)  # type: ignore[arg-type]
    seen: dict[str, object] = {}
    bus.subscribe("on_finish", lambda payload: seen.update(payload))
    hook._tasks.spawn_thread(lambda: 1 / 0)

    ctx = RuntimeContext(session=None, config=Config(core=CoreConfig(dir=".")), events=bus)
    await engine.download_all(ctx, [])

    assert seen["hooks_failed"] == 1


class _FakeHeadResponse:
    def __init__(self, status: int, headers: dict[str, str], url: str) -> None:
        self.status = status
        self.headers = dict(headers)
        self.url = url
        self.content_length = int(self.headers.get("Content-Length", 0) or 0)

    async def __aenter__(self) -> _FakeHeadResponse:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _RecordingHeadSession:
    def __init__(self, response: _FakeHeadResponse) -> None:
        self.response = response
        self.calls: list[dict[str, str]] = []

    def head(self, _url: str, **kwargs: object) -> _FakeHeadResponse:
        self.calls.append(dict(kwargs.get("headers") or {}))  # type: ignore[arg-type]
        return self.response


async def test_the_probe_drops_a_caller_supplied_range_but_keeps_credentials() -> None:
    """``-H "Range: …"`` made the probe report a slice as the object's size."""
    from downctl.probe import probe_metadata

    session = _RecordingHeadSession(
        _FakeHeadResponse(200, {"Content-Length": "39254"}, "http://h/f")
    )

    result = await probe_metadata(  # type: ignore[arg-type]
        session,
        "http://h/f",
        headers={"Range": "bytes=0-99", "Authorization": "Bearer t"},
    )

    assert result.size == 39254
    sent = session.calls[0]
    assert "Range" not in sent
    assert sent["Authorization"] == "Bearer t"


async def test_a_witness_recheck_carries_the_task_credentials() -> None:
    """The credential has to reach the probe, or a private target answers 401."""
    session = _RecordingHeadSession(
        _FakeHeadResponse(200, {"Content-Length": "3", "ETag": '"v2"'}, "http://h/f")
    )
    ctx = RuntimeContext(session=session, config=Config(core=CoreConfig(dir=".")))  # type: ignore[arg-type]
    task = DownloadTask(url="http://h/f", target="f", headers={"Authorization": "Bearer t"})

    assert await engine._witness_changed_again(ctx, task, '"v1"') is True
    assert session.calls[0]["Authorization"] == "Bearer t"

    # The same holds for the re-probe after a 416/206 mismatch: it is the other
    # probe that dropped the headers, and both report the size the transfer uses.
    await engine._reprobe(ctx, task)
    assert task.size == 3
    assert session.calls[1]["Authorization"] == "Bearer t"


async def test_a_401_names_the_withheld_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """A withheld token is the one 401 the status alone cannot explain.

    The docs promise that such a 401 names ``token_to_mirror``; that promise
    used to hold only for the repository *listing*, so the same repository
    failing at the first block request left the user with a bare status.
    """
    from downctl.core.errors import PermanentHTTPError
    from downctl.probe import ProbeResult

    async def denied(*_args: object, **_kwargs: object) -> ProbeResult:
        return ProbeResult(status=401, reached=True, error="HTTP 401")

    monkeypatch.setattr(app, "probe_metadata", denied)
    ctx = RuntimeContext(session=None, config=Config(core=CoreConfig(dir=".")))  # type: ignore[arg-type]
    task = DownloadTask(
        url="http://h/x",
        target="x",
        meta={"credential_hint": "set token_to_mirror = true to send it"},
    )

    with pytest.raises(PermanentHTTPError) as caught:
        await app._probe_task(ctx, task, retries=0)

    assert "token_to_mirror" in str(caught.value)

    plain = DownloadTask(url="http://h/y", target="y")
    with pytest.raises(PermanentHTTPError) as caught_plain:
        await app._probe_task(ctx, plain, retries=0)

    assert "token_to_mirror" not in str(caught_plain.value)
