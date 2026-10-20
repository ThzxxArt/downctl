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


def _finished(path: Path, target: str, witness: str = "validator") -> dict[str, object]:
    """An ``on_task_finish`` payload as the engine emits it."""
    return {"task": _task(path, target), "ok": True, "witness": witness}


def test_writes_manifest(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"A" * 10)
    b.write_bytes(b"B" * 20)

    hook = ManifestHook()
    hook.on_event("on_task_finish", _finished(a, "a.bin"))
    hook.on_event("on_task_finish", _finished(b, "b.bin"))
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


def test_an_unverified_completion_is_not_listed(tmp_path: Path) -> None:
    """A ``witness=none`` skip must not become a digest the next run trusts.

    The manifest is fed back through ``--checksums``, so listing an entry that
    this run could not tie to a version would certify content nobody checked.
    """
    verified = tmp_path / "verified.bin"
    unverified = tmp_path / "unverified.bin"
    verified.write_bytes(b"V" * 8)
    unverified.write_bytes(b"U" * 8)

    hook = ManifestHook()
    hook.on_event("on_task_finish", _finished(verified, "verified.bin", "validator"))
    hook.on_event("on_task_finish", _finished(unverified, "unverified.bin", "none"))
    hook.on_event("on_finish", {"succeeded": 2, "failed": 0})

    manifest = (tmp_path / ".downctl.manifest").read_text(encoding="utf-8")
    assert "verified.bin" in manifest
    assert "unverified.bin" not in manifest


def test_a_run_of_only_unverified_completions_writes_no_manifest(tmp_path: Path) -> None:
    a = tmp_path / "a.bin"
    a.write_bytes(b"A")
    hook = ManifestHook()
    hook.on_event("on_task_finish", _finished(a, "a.bin", "none"))
    hook.on_event("on_finish", {"succeeded": 1, "failed": 0})
    assert not (tmp_path / ".downctl.manifest").exists()


def test_a_missing_witness_is_treated_as_unverified(tmp_path: Path) -> None:
    """A payload from an older or third-party event has no proof to offer."""
    a = tmp_path / "a.bin"
    a.write_bytes(b"A")
    hook = ManifestHook()
    hook.on_event("on_task_finish", {"task": _task(a, "a.bin"), "ok": True})
    hook.on_event("on_finish", {"succeeded": 1, "failed": 0})
    assert not (tmp_path / ".downctl.manifest").exists()


def test_no_paths_writes_nothing(tmp_path: Path) -> None:
    hook = ManifestHook()
    hook.on_event("on_finish", {"succeeded": 0, "failed": 0})
    assert not (tmp_path / ".downctl.manifest").exists()
