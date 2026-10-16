"""The manifest hook plugin: write sha256 sums of completed downloads.

After a run, writes a ``sha256sum``-style manifest next to the downloaded
files. Useful for later verification: ``downctl get <url> --checksums
.downctl.manifest``.

Options (``[plugins.manifest]``):

- ``filename``: manifest file name (default ``.downctl.manifest``)
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from downctl import console
from downctl.plugin.protocols import API_VERSION


class ManifestHook:
    """Collect completed files and write a sha256 manifest at run end."""

    name = "manifest"
    api_version = API_VERSION
    priority = 0

    def __init__(self, filename: str = ".downctl.manifest") -> None:
        self.filename = filename
        self._paths: list[str] = []

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        if event == "on_task_finish" and payload.get("ok"):
            task = payload.get("task")
            meta = getattr(task, "meta", {}) if task is not None else {}
            path = meta.get("path") if isinstance(meta, dict) else None
            if path:
                self._paths.append(str(path))
        elif event == "on_finish":
            self._write()

    def _write(self) -> None:
        if not self._paths:
            return
        base = Path(os.path.commonpath([str(Path(p).parent) for p in self._paths]) or ".")
        lines = []
        for path in self._paths:
            try:
                digest = _sha256(path)
                rel = os.path.relpath(path, base)
            except OSError:
                continue
            lines.append(f"{digest}  {rel}")
        if not lines:
            return
        target = base / self.filename
        try:
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:  # pragma: no cover - depends on filesystem
            console.warn(f"manifest: could not write {target}: {exc}")
            return
        console.info(f"  wrote checksum manifest {target} ({len(lines)} files)")
        self._paths.clear()


def _sha256(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
