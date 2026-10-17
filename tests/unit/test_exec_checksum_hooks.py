"""Tests for the exec and checksum hook plugins."""

from __future__ import annotations

import hashlib
from pathlib import Path

from downctl.builtin.hooks.checksum_hook import ChecksumHook
from downctl.builtin.hooks.exec_hook import ExecHook
from downctl.core.task import DownloadTask


def _task(path: Path, target: str = "a.bin") -> DownloadTask:
    task = DownloadTask(url="https://h/a.bin", target=target)
    task.meta["path"] = str(path)
    return task


# --- checksum hook -------------------------------------------------------- #
def test_checksum_hook_writes_sidecar(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    ChecksumHook().on_event("on_task_finish", {"task": _task(f), "ok": True})
    sidecar = tmp_path / "a.bin.sha256"
    assert sidecar.is_file()
    assert hashlib.sha256(b"abc").hexdigest() in sidecar.read_text(encoding="utf-8")


def test_checksum_hook_ignores_failure(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"abc")
    ChecksumHook().on_event("on_task_finish", {"task": _task(f), "ok": False})
    assert not (tmp_path / "a.bin.sha256").exists()


def test_checksum_hook_falls_back_to_sha256(tmp_path: Path) -> None:
    assert ChecksumHook(algo="nonsense").algo == "sha256"
    assert ChecksumHook(algo="sha512").algo == "sha512"


def test_checksum_hook_missing_path() -> None:
    task = DownloadTask(url="u", target="a.bin")  # no meta["path"]
    ChecksumHook().on_event("on_task_finish", {"task": task, "ok": True})


def test_checksum_hook_missing_file(tmp_path: Path) -> None:
    ChecksumHook().on_event("on_task_finish", {"task": _task(tmp_path / "nope.bin"), "ok": True})
    assert not (tmp_path / "nope.bin.sha256").exists()


# --- exec hook ------------------------------------------------------------ #
def test_exec_hook_runs_command(tmp_path: Path) -> None:
    import sys

    script = tmp_path / "rec.py"
    script.write_text(
        "import os, sys; open(sys.argv[1], 'w').write(os.environ['DOWNCTL_TARGET'])",
        encoding="utf-8",
    )
    marker = tmp_path / "out.txt"
    hook = ExecHook(command=f'"{sys.executable}" "{script}" "{marker}"')
    hook.on_event("on_task_finish", {"task": _task(tmp_path / "a.bin"), "ok": True})
    assert marker.read_text(encoding="utf-8") == "a.bin"


def test_exec_hook_no_command_is_noop(tmp_path: Path) -> None:
    marker = tmp_path / "out.txt"
    ExecHook(command="").on_event("on_task_finish", {"task": _task(tmp_path / "a.bin"), "ok": True})
    assert not marker.exists()


def test_exec_hook_ignores_unselected_event(tmp_path: Path) -> None:
    marker = tmp_path / "out.txt"
    hook = ExecHook(command=f'touch "{marker}"', events=["on_finish"])
    hook.on_event("on_task_finish", {"task": _task(tmp_path / "a.bin"), "ok": True})
    assert not marker.exists()


def test_exec_hook_reports_bad_command(tmp_path: Path) -> None:
    hook = ExecHook(command="this-command-does-not-exist-xyz")
    hook.on_event("on_task_finish", {"task": _task(tmp_path / "a.bin"), "ok": True})
