"""Tests for the archive hook (extraction + hardening)."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

from downctl.builtin.hooks.archive_hook import ArchiveHook
from downctl.core.task import DownloadTask


def _make_zip(path: Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def _make_targz(path: Path, name: str, content: bytes) -> None:
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))


def test_extract_zip(tmp_path: Path) -> None:
    archive = tmp_path / "pkg.zip"
    _make_zip(archive, {"a.txt": "hello", "sub/b.txt": "world"})
    dest = ArchiveHook().extract(str(archive))
    assert dest == str(tmp_path / "pkg")
    assert (tmp_path / "pkg" / "a.txt").read_text() == "hello"
    assert (tmp_path / "pkg" / "sub" / "b.txt").read_text() == "world"


def test_extract_targz(tmp_path: Path) -> None:
    archive = tmp_path / "thing.tar.gz"
    _make_targz(archive, "c.txt", b"data")
    dest = ArchiveHook().extract(str(archive))
    assert dest == str(tmp_path / "thing")
    assert (tmp_path / "thing" / "c.txt").read_bytes() == b"data"


def test_rejects_zip_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    _make_zip(archive, {"../evil.txt": "boom"})
    assert ArchiveHook().extract(str(archive)) is None
    assert not (tmp_path.parent / "evil.txt").exists()


def test_rejects_tar_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "link.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    assert ArchiveHook().extract(str(archive)) is None


def test_keep_false_removes_archive(tmp_path: Path) -> None:
    archive = tmp_path / "gone.zip"
    _make_zip(archive, {"a.txt": "x"})
    ArchiveHook(keep=False).extract(str(archive))
    assert not archive.exists()


def test_non_archive_ignored(tmp_path: Path) -> None:
    plain = tmp_path / "a.txt"
    plain.write_text("nope")
    assert ArchiveHook().extract(str(plain)) is None


def test_on_event_uses_task_meta(tmp_path: Path) -> None:
    archive = tmp_path / "evt.zip"
    _make_zip(archive, {"z.txt": "z"})
    task = DownloadTask(url="u", target="evt.zip")
    task.meta["path"] = str(archive)
    ArchiveHook().on_event("on_task_finish", {"task": task, "ok": True})
    assert (tmp_path / "evt" / "z.txt").read_text() == "z"
