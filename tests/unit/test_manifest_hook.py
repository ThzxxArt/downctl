"""Tests for the manifest hook."""

from __future__ import annotations

import hashlib
from pathlib import Path

from downctl.builtin.hooks.manifest_hook import ManifestHook
from downctl.core.task import DownloadTask


def _task(path: Path, target: str) -> DownloadTask:
    task = DownloadTask(url="u", target=target)
    task.meta["path"] = str(path)
    return task


def test_writes_manifest(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"A" * 10)
    b.write_bytes(b"B" * 20)

    hook = ManifestHook()
    hook.on_event("on_task_finish", {"task": _task(a, "a.bin"), "ok": True})
    hook.on_event("on_task_finish", {"task": _task(b, "b.bin"), "ok": True})
    hook.on_event("on_finish", {"succeeded": 2, "failed": 0})

    manifest = (tmp_path / ".downctl.manifest").read_text(encoding="utf-8")
    assert f"{hashlib.sha256(b'A' * 10).hexdigest()}  a.bin" in manifest
    assert f"{hashlib.sha256(b'B' * 20).hexdigest()}  b.bin" in manifest


def test_failed_task_excluded(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    a.write_bytes(b"A")
    hook = ManifestHook()
    hook.on_event("on_task_finish", {"task": _task(a, "a.bin"), "ok": False})
    hook.on_event("on_finish", {"succeeded": 0, "failed": 1})
    assert not (tmp_path / ".downctl.manifest").exists()


def test_no_paths_writes_nothing(tmp_path: Path) -> None:
    hook = ManifestHook()
    hook.on_event("on_finish", {"succeeded": 0, "failed": 0})
    assert not (tmp_path / ".downctl.manifest").exists()
