"""Regression tests for issues found in the full-project review."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from downctl import console
from downctl.builtin.hooks.logfile_hook import LogfileHook
from downctl.builtin.reporters.json_reporter import JSONReporter
from downctl.builtin.sources.url_source import filename_from_url
from downctl.config import Config, CoreConfig, load_config
from downctl.core import engine
from downctl.core import pieces as piece_store
from downctl.core.context import RuntimeContext
from downctl.core.errors import DownctlError
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
    task = DownloadTask(url="u", target=str(out))
    assert engine._resolve_target(ctx, task) == str(out)


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
async def test_unknown_checksum_algorithm_fails() -> None:
    ctx = RuntimeContext(
        session=None,  # type: ignore[arg-type]
        config=Config(),
        registry=discover(),
    )
    task = DownloadTask(url="u", target="f", checksum_algo="md5", checksum_hex="00")
    assert await engine._verify(ctx, task, str(Path("/nonexistent"))) is False


async def test_missing_checksum_is_ok() -> None:
    ctx = RuntimeContext(session=None, config=Config(), registry=discover())  # type: ignore[arg-type]
    task = DownloadTask(url="u", target="f")
    assert await engine._verify(ctx, task, "whatever") is True


# --- json reporter throttling --------------------------------------------- #
def test_json_reporter_throttles_progress() -> None:
    stream = io.StringIO()
    reporter = JSONReporter(stream)
    task = DownloadTask(url="u", target="a", size=100)
    reporter.task_started(task)
    for _ in range(100):
        reporter.task_progress(task, 1)  # 100 percent steps, size 100
    reporter.task_finished(task, True)
    lines = [ln for ln in stream.getvalue().splitlines() if ln]
    # start + (at most 100 percent changes) + finish; far fewer than 100 deltas
    assert len(lines) < 105
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
