"""The archive hook plugin: extract downloaded archives.

On a successful ``on_task_finish`` the archive is extracted next to itself.
Extraction is hardened against path traversal (``..`` / absolute paths).

Options (``[plugins.archive]``):

- ``into``:   destination directory (default: a sibling dir named after the
  archive, sans extension)
- ``keep``:   keep the archive after extraction (default ``true``)
- ``max_entries``: refuse an archive declaring more entries (default 200000;
  ``0`` disables the bound)
- ``max_total_bytes``: refuse an archive declaring more uncompressed bytes
  (default 20 GiB; ``0`` disables the bound)

A declared expansion more than 1000x the archive size is refused regardless.
"""

from __future__ import annotations

import contextlib
import sys
import tarfile
import zipfile
from pathlib import Path

from downctl import console
from downctl.plugin.hooks import ConfigurableHook, HookTasks
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


#: Refuse an archive whose *declared* expansion is implausible. A zip bomb is
#: cheap to declare and expensive to write; the check happens before extraction.
DEFAULT_MAX_ENTRIES = 200_000
DEFAULT_MAX_TOTAL_BYTES = 20 * (1 << 30)
#: Declared uncompressed/compressed ratio above which an archive is refused.
MAX_COMPRESSION_RATIO = 1000


class ArchiveHook(ConfigurableHook):
    """Extract zip/tar archives when their download finishes."""

    name = "archive"
    api_version = API_VERSION
    priority = 0
    OPTIONS = {"into": "str", "keep": "bool", "max_total_bytes": "int", "max_entries": "int"}

    def __init__(
        self,
        into: str = "",
        keep: bool = True,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.into = into
        self.keep = keep
        #: ``0`` disables the bound (for archives the user knows are huge).
        self.max_total_bytes = max_total_bytes
        self.max_entries = max_entries
        self._tasks = HookTasks(self.name)

    def validate_option(self, key: str, value: object) -> None:
        if key in ("max_total_bytes", "max_entries"):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
            if value < 0:
                raise ValueError(f"{key} must be >= 0 (0 = no limit)")

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        if event != "on_task_finish" or not payload.get("ok"):
            return
        if payload.get("skipped"):
            # A skip re-announces a file whose archive was already extracted by
            # the run that actually produced it. Extracting again either
            # overwrote newer contents or failed on the now-existing directory.
            return
        task = payload.get("task")
        path = ""
        if task is not None:
            meta = getattr(task, "meta", {})
            if isinstance(meta, dict):
                path = str(meta.get("path", ""))
        if not path:
            return
        # Extraction can be slow; run it off the event loop. ``HookTasks`` keeps
        # the task *and* reads its result, so a corrupt or encrypted archive is
        # reported instead of disappearing with the handle.
        if not self._tasks.spawn_thread(self.extract, path):
            self.extract(path)  # pragma: no cover - only without a loop

    async def aclose(self) -> int:
        return await self._tasks.aclose()

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
            console.warn(f"archive: failed to extract {source.name}: {exc}", always=True)
            self._tasks.record()
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
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            self._check_size(len(infos), sum(info.file_size for info in infos), source)
            for info in infos:
                self._check_member(info.filename, dest)
            dest.mkdir(parents=True, exist_ok=True)
            archive.extractall(dest)  # noqa: S202 - names validated above

    def _extract_tar(self, source: Path, dest: Path, mode: str) -> None:
        with tarfile.open(source, mode) as archive:  # type: ignore[call-overload]
            members = archive.getmembers()
            self._check_size(len(members), sum(member.size for member in members), source)
            for member in members:
                self._check_member(member.name, dest)
                if not (member.isfile() or member.isdir()):
                    # Reject symlinks, hardlinks, devices, FIFOs, ...
                    raise tarfile.TarError(f"refusing non-regular member: {member.name}")
            dest.mkdir(parents=True, exist_ok=True)
            if sys.version_info >= (3, 12):
                archive.extractall(dest, filter="data")
            else:  # pragma: no cover - Python 3.11
                archive.extractall(dest)  # noqa: S202 - names validated above

    def _check_size(self, entries: int, total: int, source: Path) -> None:
        """Refuse an archive whose declared expansion exceeds the limits."""
        if self.max_entries and entries > self.max_entries:
            raise tarfile.TarError(
                f"{source.name} declares {entries} entries "
                f"(limit {self.max_entries}); raise [plugins.archive] max_entries"
            )
        if self.max_total_bytes and total > self.max_total_bytes:
            raise tarfile.TarError(
                f"{source.name} declares {total} uncompressed bytes "
                f"(limit {self.max_total_bytes}); raise [plugins.archive] max_total_bytes"
            )
        try:
            compressed = source.stat().st_size
        except OSError:
            return
        if compressed > 0 and total // compressed > MAX_COMPRESSION_RATIO:
            raise tarfile.TarError(
                f"{source.name} expands more than {MAX_COMPRESSION_RATIO}x its size; "
                "refusing to extract it"
            )

    @staticmethod
    def _check_member(name: str, dest: Path) -> None:
        # Normalize Windows separators so "..\\evil" cannot slip past the check.
        normalized = name.replace("\\", "/")
        if normalized.startswith("/") or normalized.startswith("../") or "/../" in normalized:
            raise tarfile.TarError(f"unsafe archive member: {name}")
        candidate = (dest / normalized).resolve()
        base = dest.resolve()
        if candidate != base and base not in candidate.parents:
            raise tarfile.TarError(f"unsafe archive member: {name}")
