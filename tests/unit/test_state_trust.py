"""Tests for URL-bound resume state, completion markers and pacing.

These cover the 0.1.3 "state must not lie" guarantees: a ledger belongs to one
remote object, a marker is the only cheap proof of completion, and the rate
limiter never banks idle credit.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from downctl import console
from downctl.builtin.reporters.tqdm_reporter import TqdmReporter
from downctl.core import pieces as pc
from downctl.core.engine import _safe_error
from downctl.core.ratelimit import RateLimiter
from downctl.core.task import DownloadTask
from downctl.urls import display_url, mask_mapping, redact_text, url_identity

_SRC = str(Path(__file__).resolve().parents[2] / "src")


# --- URL display / identity ------------------------------------------------ #
def test_identity_is_stable_across_rotating_signatures() -> None:
    first = url_identity("https://h/b?X-Amz-Signature=1&X-Amz-Expires=60", redact=True)
    second = url_identity("https://h/b?X-Amz-Signature=2&X-Amz-Expires=60", redact=True)
    assert first == second
    assert first != url_identity("https://h/c?X-Amz-Signature=1", redact=True)


def test_identity_ignores_embedded_userinfo() -> None:
    assert url_identity("https://user:pass@h/x") == url_identity("https://h/x")


def test_display_url_masks_credentials() -> None:
    shown = display_url("http://user:pass@h/x?token=abc&keep=1")
    assert "user" not in shown
    assert "pass" not in shown
    assert "abc" not in shown
    assert "keep=1" in shown
    assert display_url("http://s3/b?X-Amz-Signature=x", True) == "http://s3/b"


def test_display_url_sanitizes_malformed_and_schemeless_credentials() -> None:
    malformed = display_url("http://alice:pw@[::1/x?token=SUPERSECRET")
    assert "pw" not in malformed
    assert "SUPERSECRET" not in malformed
    assert "token" not in malformed

    schemeless = display_url("alice:pw@proxy.example:8080")
    assert "alice" not in schemeless
    assert "pw" not in schemeless
    assert schemeless == "proxy.example:8080"


def test_mask_mapping_hides_secrets_recursively() -> None:
    masked = mask_mapping(
        {
            "token": "t",
            "nested": {"secret_key": "s", "region": "r"},
            "jobs": 2,
            "webhook": {"url": "https://h/hook?token=SECRET"},
            "urls": ["https://h/a?X-Amz-Signature=SIG"],
        }
    )
    assert masked["token"] == "***"
    assert masked["nested"] == {"secret_key": "***", "region": "r"}
    assert masked["jobs"] == 2
    assert masked["webhook"] == {"url": "https://h/hook?token=***"}
    assert masked["urls"] == ["https://h/a?X-Amz-Signature=***"]


def test_redact_text_masks_every_url_in_a_message() -> None:
    message = (
        "cannot fetch http://h/a?token=SECRET (redirected to "
        "https://m/b?X-Amz-Signature=SIG); retry"
    )
    cleaned = redact_text(message)
    assert "SECRET" not in cleaned
    assert "SIG" not in cleaned
    assert "token=***" in cleaned
    assert "X-Amz-Signature=***" in cleaned
    assert "(redirected to" in cleaned
    assert "); retry" in cleaned


def test_redact_text_leaves_plain_text_and_paths_alone() -> None:
    assert redact_text("downloaded /tmp/a.bin") == "downloaded /tmp/a.bin"
    assert redact_text("no URL here") == "no URL here"


def test_safe_error_redacts_a_presigned_url() -> None:
    task = DownloadTask(
        url="http://s3/b?X-Amz-Signature=SUPERSECRET",
        target="b",
        meta={"redact_url": True},
    )
    message = _safe_error(task, RuntimeError("GET http://s3/b?X-Amz-Signature=SUPERSECRET failed"))
    assert "SUPERSECRET" not in message
    assert "http://s3/b" in message


def test_failed_log_stays_retryable_but_hides_signatures() -> None:
    from downctl import app

    plain = DownloadTask(url="https://h/f?token=abc", target="f")
    # A failed log outlives the run, so a credential-bearing parameter is masked
    # even in an ordinary URL.
    ordinary = app._fmt_url(plain)
    assert "token=abc" not in ordinary
    assert "token=***" in ordinary

    presigned = DownloadTask(
        url="http://s3/b?X-Amz-Signature=abc", target="b", meta={"redact_url": True}
    )
    line = app._fmt_url(presigned)
    assert "X-Amz" not in line
    assert line.split("\t")[1] == "http://s3/b"


# --- bitmap identity ------------------------------------------------------- #
def _save_bitmap(target: str, size: int, bits: list[int], piece_size: int, identity: str) -> None:
    Path(target).write_bytes(b"\0" * size)
    pc.save(
        target,
        pc.Bitmap(size, len(bits), bits, size, piece_size, url_sha256=identity),
    )


def test_resolve_resets_bitmap_for_another_url(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    _save_bitmap(target, 100, [1, 1, 0, 0], 25, url_identity("https://h/a"))
    resume = pc.resolve(target, 100, 4, 25, url_identity("https://h/b"))
    assert resume.bits == [0, 0, 0, 0]
    assert os.path.getsize(target) == 100  # kept, pinned to 0 %


def test_resolve_discards_unbound_legacy_bitmap(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"\0" * 100)
    pc.save(target, pc.Bitmap(100, 4, [1, 1, 1, 1], 100, 25))
    assert pc.resolve(target, 100, 4, 25, url_identity("https://h/a")).bits == [0, 0, 0, 0]


def test_resolve_discards_a_bitmap_with_an_empty_identity(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    Path(target).write_bytes(b"\0" * 100)
    pc.save(target, pc.Bitmap(100, 4, [1, 1, 0, 0], 100, 25, url_sha256=""))
    assert pc.resolve(target, 100, 4, 25, "").bits == [0, 0, 0, 0]


def test_resolve_keeps_bound_bitmap(tmp_path: Path) -> None:
    target = str(tmp_path / "f.bin")
    identity = url_identity("https://h/a")
    _save_bitmap(target, 100, [1, 0, 1, 0], 25, identity)
    assert pc.resolve(target, 100, 4, 25, identity).bits == [1, 0, 1, 0]


def test_resolve_rejects_future_bitmap_version(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"\0" * 100)
    Path(pc.path_for(str(target))).write_text(
        '{"version": 99, "size": 100, "pieces": 4, "bits": [1,1,1,1], '
        '"min_size": 100, "piece_size": 25, "url_sha256": "x"}',
        encoding="utf-8",
    )
    assert pc.load(str(target)) is None


def test_layout_rejects_absurd_piece_count() -> None:
    with pytest.raises(ValueError, match="pieces"):
        pc.layout(100 * (1 << 30), 8, pc.MIN_PIECE_SIZE)


def test_trusted_complete_distrusts_windows_without_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "w.bin"
    target.write_bytes(b"x" * 100)
    monkeypatch.setattr(pc.os, "name", "nt")
    assert pc.trusted_complete(str(target), 100) is False


def test_trusted_complete_distrusts_drvfs_without_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "w.bin"
    target.write_bytes(b"x" * 100)
    monkeypatch.setattr(pc, "_on_drvfs", lambda path: True)
    assert pc.trusted_complete(str(target), 100) is False


# --- completion markers ---------------------------------------------------- #
def test_completion_marker_binds_size_mtime_and_identity(tmp_path: Path) -> None:
    target = tmp_path / "done.bin"
    target.write_bytes(b"x" * 100)
    identity = url_identity("https://h/a")
    assert pc.save_completion(str(target), identity)
    assert pc.completion_valid(str(target), 100, identity)
    assert not pc.completion_valid(str(target), 100, url_identity("https://h/b"))
    assert not pc.completion_valid(str(target), 101, identity)


def test_completion_marker_invalidated_by_external_modification(tmp_path: Path) -> None:
    target = tmp_path / "done.bin"
    target.write_bytes(b"x" * 100)
    identity = url_identity("https://h/a")
    assert pc.save_completion(str(target), identity)
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert not pc.completion_valid(str(target), 100, identity)


def test_marked_file_resolves_as_complete_without_hole_scan(tmp_path: Path) -> None:
    target = str(tmp_path / "done.bin")
    Path(target).write_bytes(b"x" * 100)
    identity = url_identity("https://h/a")
    assert pc.save_completion(target, identity)
    resume = pc.resolve(target, 100, 4, 25, identity)
    assert resume.bits == [1, 1, 1, 1]
    assert resume.proof == pc.PROOF_MARKER
    assert os.path.getsize(target) == 100  # never truncated


def test_marker_is_enough_on_drvfs_where_holes_are_undetectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = str(tmp_path / "done.bin")
    Path(target).write_bytes(b"x" * 100)
    identity = url_identity("https://h/a")
    monkeypatch.setattr(pc, "_on_drvfs", lambda path: True)
    assert pc.save_completion(target, identity)
    assert pc.resolve(target, 100, 4, 25, identity).bits == [1, 1, 1, 1]


def test_reset_clears_the_completion_marker(tmp_path: Path) -> None:
    target = str(tmp_path / "done.bin")
    Path(target).write_bytes(b"x" * 100)
    identity = url_identity("https://h/a")
    pc.save_completion(target, identity)
    pc.resolve(target, 100, 4, 25, url_identity("https://h/b"))
    assert pc.load_completion(target) is None


def test_remove_clears_both_sidecars(tmp_path: Path) -> None:
    target = str(tmp_path / "done.bin")
    Path(target).write_bytes(b"x" * 100)
    pc.save(target, pc.Bitmap(100, 4, [1, 0, 0, 0], 100, 25))
    pc.save_completion(target, url_identity("https://h/a"))
    pc.remove(target)
    assert pc.load(target) is None
    assert pc.load_completion(target) is None


def test_sidecar_writes_from_threads_never_corrupt_the_ledger(tmp_path: Path) -> None:
    import threading

    target = str(tmp_path / "threaded.bin")
    Path(target).write_bytes(b"x" * 100)

    def worker(index: int) -> None:
        pc.save(target, pc.Bitmap(100, 4, [index % 2, 1, 0, 0], 100, 25))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert pc.load(target) is not None


# --- SIGTERM handling ------------------------------------------------------ #
def test_sigterm_becomes_keyboard_interrupt() -> None:
    """Signalling *this* process proved nothing and could kill the suite.

    The old version sent ``SIGTERM`` to the pytest process itself: if the
    handler had not been installed yet, the whole run died, and either way it
    exercised a different code path from the CLI. Run it in a child process and
    assert on the exit code the shell sees.
    """
    if os.name == "nt":
        pytest.skip("Windows has no catchable SIGTERM for console processes")
    script = (
        "import os, signal\n"
        "from downctl.platform_utils import interrupt_on_sigterm\n"
        "try:\n"
        "    with interrupt_on_sigterm():\n"
        "        os.kill(os.getpid(), signal.SIGTERM)\n"
        "except KeyboardInterrupt:\n"
        "    raise SystemExit(130)\n"
        "raise SystemExit(1)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": _SRC},
        timeout=60,
        check=False,
    )
    assert completed.returncode == 130, completed.stderr[-400:]


# --- rate limiting --------------------------------------------------------- #
async def test_rate_limiter_has_no_idle_credit() -> None:
    limiter = RateLimiter(2000)
    await asyncio.sleep(0.15)  # idle time must not bank a burst
    started = time.monotonic()
    await limiter.wait(100)  # 100 B at 2000 B/s = 0.05 s
    await limiter.wait(100)  # and again: two units cost two units of time
    elapsed = time.monotonic() - started
    assert elapsed >= 0.08


# --- tqdm bar slots -------------------------------------------------------- #
def test_tqdm_allocates_distinct_positions(capsys: pytest.CaptureFixture[str]) -> None:
    console.set_color(False)
    reporter = TqdmReporter()
    reporter.run_started(2, 0, jobs=2)  # unknown total -> per-task bars
    first = DownloadTask(url="u1", target="one.bin", size=10)
    second = DownloadTask(url="u2", target="two.bin", size=10)
    reporter.task_started(first)
    reporter.task_started(second)
    assert reporter._slots[id(first)] != reporter._slots[id(second)]  # noqa: SLF001
    reporter.task_finished(first, True)
    reporter.task_finished(second, True)
    reporter.run_finished(2, 0)


# --- state written by a newer downctl -------------------------------------- #
def _future_ledger(tmp_path) -> tuple[str, str]:
    """A ledger whose format version this build does not understand."""
    target = str(tmp_path / "future.bin")
    path = pc.path_for(target)
    Path(path).write_text(
        '{"version": 99, "size": 100, "pieces": 1, "bits": [1], "min_size": 100}',
        encoding="utf-8",
    )
    return target, path


def test_a_ledger_from_a_newer_downctl_is_not_used(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not understood is not the same as absent, and it must say so."""
    target, _ = _future_ledger(tmp_path)
    assert pc.load(target) is None
    err = capsys.readouterr().err
    assert "written by a newer downctl" in err
    assert "not used" in err


def test_a_ledger_from_a_newer_downctl_is_never_overwritten(tmp_path) -> None:
    """Replacing it would destroy the only copy of information we cannot read."""
    target, path = _future_ledger(tmp_path)
    before = Path(path).read_text(encoding="utf-8")

    assert pc.save(target, pc.Bitmap(100, 1, [0], 0)) is False
    assert Path(path).read_text(encoding="utf-8") == before


def test_a_marker_from_a_newer_downctl_is_not_trusted_but_is_kept(tmp_path) -> None:
    target = str(tmp_path / "future.bin")
    Path(target).write_bytes(b"x" * 10)
    marker = pc.completion_path(target)
    Path(marker).write_text(
        '{"version": 99, "size": 10, "mtime_ns": 0, "url_sha256": "abc"}',
        encoding="utf-8",
    )
    before = Path(marker).read_text(encoding="utf-8")

    assert pc.load_completion(target) is None
    assert pc.completion_valid(target, 10, "abc") is False
    assert pc.save_completion(target, "abc") is False
    assert Path(marker).read_text(encoding="utf-8") == before


def test_the_future_state_warning_is_emitted_once(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``load`` runs once per checkpoint; a warning per call would be noise."""
    target, _ = _future_ledger(tmp_path)
    for _ in range(5):
        assert pc.load(target) is None
    assert capsys.readouterr().err.count("written by a newer downctl") == 1
