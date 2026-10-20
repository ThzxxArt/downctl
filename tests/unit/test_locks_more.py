"""Extra coverage for the file lock."""

from __future__ import annotations

from pathlib import Path

import pytest

from downctl.core.locks import FileLock, LockError


def test_lock_takes_over_dead_owner(tmp_path: Path) -> None:
    target = str(tmp_path / "a.bin")
    lock_dir = Path(target + ".lock")
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("999999999", encoding="utf-8")

    lock = FileLock(target)
    lock.acquire()
    assert lock.held
    lock.release()
    assert not lock.held
    assert not lock_dir.exists()


def test_lock_cannot_create_raises(tmp_path: Path) -> None:
    with pytest.raises(LockError, match="cannot create lock"):
        FileLock(str(tmp_path / "missing" / "a.bin")).acquire()


def test_lock_release_when_not_held(tmp_path: Path) -> None:
    """Releasing a lock nobody holds is a no-op, and creates nothing."""
    FileLock(str(tmp_path / "a.bin")).release()
    assert not (tmp_path / "a.bin.lock").exists()


def test_lock_held_by_live_process(tmp_path: Path) -> None:
    import os

    target = str(tmp_path / "a.bin")
    lock_dir = Path(target + ".lock")
    lock_dir.mkdir()
    (lock_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
    with pytest.raises(LockError, match="another process holds"):
        FileLock(target).acquire()
