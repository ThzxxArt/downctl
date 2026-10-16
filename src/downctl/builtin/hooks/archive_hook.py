"""The archive hook plugin: extract downloaded archives.

On a successful ``on_task_finish`` the archive is extracted next to itself.
Extraction is hardened against path traversal (``..`` / absolute paths).

Options (``[plugins.archive]``):

- ``into``:   destination directory (default: a sibling dir named after the
  archive, sans extension)
- ``keep``:   keep the archive after extraction (default ``true``)
- ``remove``: alias inversion of ``keep`` for convenience
"""

from __future__ import annotations

import contextlib
import tarfile
import zipfile
from pathlib import Path

from downctl import console
from downctl.plugin.protocols import API_VERSION

TAR_MODES = {
    ".tar": "r:",
    ".tar.gz": "r:gz",
    ".tgz": "r:gz",
    ".tar.bz2": "r:bz2",
    ".tbz2": "r:bz2",
    ".tar.xz": "r:xz",
    ".txz": "r:xz",
}


class ArchiveHook:
    """Extract zip/tar archives when their download finishes."""

    name = "archive"
    api_version = API_VERSION
    priority = 0

    def __init__(self, into: str = "", keep: bool = True) -> None:
        self.into = into
        self.keep = keep

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        if event != "on_task_finish" or not payload.get("ok"):
            return
        task = payload.get("task")
        path = ""
        if task is not None:
            meta = getattr(task, "meta", {})
            if isinstance(meta, dict):
                path = str(meta.get("path", ""))
        if path:
            self.extract(path)

    def extract(self, path: str) -> str | None:
        """Extract ``path`` if it is a supported archive. Returns the dest dir."""
        source = Path(path)
        if not source.is_file():
            return None
        dest = self._destination(source)

        try:
            if source.suffix.lower() == ".zip":
                self._extract_zip(source, dest)
            elif self._tar_mode(source) is not None:
                self._extract_tar(source, dest, self._tar_mode(source) or "r:")
            else:
                return None
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            console.warn(f"archive: failed to extract {source.name}: {exc}")
            return None

        console.info(f"  extracted {source.name} -> {dest}")
        if not self.keep:
            with contextlib.suppress(OSError):
                source.unlink()
        return str(dest)

    def _destination(self, source: Path) -> Path:
        if self.into:
            return Path(self.into).expanduser()
        stem = source.name
        for suffix in (".tar.gz", ".tar.bz2", ".tar.xz"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        else:
            stem = source.stem
        return source.parent / stem

    def _tar_mode(self, source: Path) -> str | None:
        for suffix, mode in TAR_MODES.items():
            if source.name.endswith(suffix):
                return mode
        return None

    def _extract_zip(self, source: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as archive:
            for member in archive.namelist():
                self._check_member(member, dest)
            archive.extractall(dest)  # noqa: S202 - names validated above

    def _extract_tar(self, source: Path, dest: Path, mode: str) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(source, mode) as archive:  # type: ignore[call-overload]
            for member in archive.getmembers():
                self._check_member(member.name, dest)
                if member.issym() or member.islnk():
                    raise tarfile.TarError(f"refusing link member: {member.name}")
            archive.extractall(dest)  # noqa: S202 - names validated above

    @staticmethod
    def _check_member(name: str, dest: Path) -> None:
        candidate = (dest / name).resolve()
        base = dest.resolve()
        if candidate != base and base not in candidate.parents:
            raise tarfile.TarError(f"unsafe archive member: {name}")
