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
"""

from __future__ import annotations

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
            self._write()

    def _write(self) -> None:
        if self._unverified:
            console.verbose(
                f"manifest: {len(self._unverified)} file(s) left out "
                "(their completion is not tied to a version)"
            )
            self._unverified.clear()
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
            lines.append(f"{digest}  {rel}")
        if not lines:
            self._entries.clear()
            return
        target = base / self.filename
        try:
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:  # pragma: no cover - depends on filesystem
            console.warn(f"manifest: could not write {target}: {exc}", always=True)
            self._tasks.record()
            return
        console.info(f"  wrote checksum manifest {target} ({len(lines)} files)")
        self._entries.clear()
