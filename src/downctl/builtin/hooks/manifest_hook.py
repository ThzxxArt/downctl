"""The manifest hook plugin: write sha256 sums of completed downloads.

After a run, writes a ``sha256sum``-style manifest next to the downloaded
files. Useful for later verification: ``downctl get <url> --checksums
.downctl.manifest``.

A manifest entry is a claim about *the remote object*: ``--checksums`` feeds it
back in as the expected digest. So an entry is only written for a completion the
run could tie to a version (``witness`` is ``validator`` or ``checksum``); a
file whose completion was never verified is counted and reported but not listed,
because listing it would hand the next run a proof it never earned. The witness
is therefore a *filter*, not a field of the entry, and the file format stays a
plain ``sha256sum`` list — which is what the offset parser reads back.

Options (``[plugins.manifest]``):

- ``filename``: manifest file name (default ``.downctl.manifest``)

Entries are *merged* into an existing manifest rather than replacing it: a
second run over one file in a directory used to erase every entry the first run
had proved. Hashing happens on a worker thread so a large file does not stall
the remaining tasks.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

from downctl import console
from downctl.digest import hash_file
from downctl.plugin.hooks import ConfigurableHook, HookTasks
from downctl.plugin.protocols import API_VERSION


@dataclass(frozen=True)
class _Entry:
    """One completed file whose completion the run could tie to a version."""

    path: str


class ManifestHook(ConfigurableHook):
    """Collect completed files and write a sha256 manifest at run end."""

    name = "manifest"
    api_version = API_VERSION
    priority = 0
    OPTIONS = {"filename": "str"}

    def __init__(self, filename: str = ".downctl.manifest") -> None:
        self.filename = filename
        self._entries: list[_Entry] = []
        self._unverified: list[str] = []
        # No background work, but the counter is what makes ``strict_hooks``
        # see a manifest that could not be written.
        self._tasks = HookTasks(self.name)

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        if event == "on_task_finish" and payload.get("ok"):
            task = payload.get("task")
            meta = getattr(task, "meta", {}) if task is not None else {}
            if not isinstance(meta, dict):
                return
            path = meta.get("path")
            if not path:
                return
            witness = str(payload.get("witness") or "none")
            if witness == "none":
                self._unverified.append(str(path))
                return
            self._entries.append(_Entry(path=str(path)))
        elif event == "on_finish":
            if not self._entries:
                self._report_unverified()
                return
            # Hashing is CPU-bound and the files can be large: doing it on the
            # event loop stalled every remaining task and the JSON reporter
            # while the run was "finished but not returning".
            if not self._tasks.spawn_thread(self._write):
                self._write()  # pragma: no cover - only without a loop

    async def aclose(self) -> int:
        return await self._tasks.aclose()

    def _report_unverified(self) -> None:
        if self._unverified:
            console.verbose(
                f"manifest: {len(self._unverified)} file(s) left out "
                "(their completion is not tied to a version)"
            )
            self._unverified.clear()

    def _write(self) -> None:
        self._report_unverified()
        if not self._entries:
            return
        parents = [str(Path(entry.path).parent) for entry in self._entries]
        try:
            base = Path(os.path.commonpath(parents))
        except ValueError:  # different drives/roots (Windows)
            base = Path(parents[0])
        lines = []
        for entry in self._entries:
            try:
                digest = hash_file(entry.path)
                rel = os.path.relpath(entry.path, base).replace(os.sep, "/")
            except OSError as exc:
                # A file that vanished (or became unreadable) between the finish
                # event and this pass is a manifest that silently omits what the
                # run reported as done. Say so, and let ``strict_hooks`` see it.
                console.warn(f"manifest: cannot hash {entry.path}: {exc}", always=True)
                self._tasks.record()
                continue
            lines.append((rel, digest))
        if not lines:
            self._entries.clear()
            return
        target = base / self.filename
        # Merge, don't overwrite: a second run over one file in a directory used
        # to erase every entry the first run had proved.
        merged = self._read_existing(target)
        for rel, digest in lines:
            merged[rel] = digest
        body = "".join(f"{digest}  {name}\n" for name, digest in sorted(merged.items()))
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:  # pragma: no cover - depends on filesystem
            with contextlib.suppress(OSError):
                tmp.unlink()
            console.warn(f"manifest: could not write {target}: {exc}", always=True)
            self._tasks.record()
            return
        console.info(f"  wrote checksum manifest {target} ({len(merged)} files)")
        self._entries.clear()

    @staticmethod
    def _read_existing(target: Path) -> dict[str, str]:
        """Entries already in the manifest, so a new run adds instead of erasing."""
        table: dict[str, str] = {}
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            return table
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            digest, _, name = line.partition("  ")
            if digest and name:
                table[name] = digest
        return table
