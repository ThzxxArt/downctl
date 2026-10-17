"""The checksum hook plugin: write a digest sidecar for each download.

On a successful ``on_task_finish`` a ``<file>.<algo>`` sidecar is written in the
``sha256sum`` line format (``<hex>  <basename>``), ready to be consumed by
``--checksums`` later.

Options (``[plugins.checksum]``):

- ``algo``:   hash algorithm, default ``sha256`` (``sha1``/``sha512``/``md5`` also work)
- ``events``: events that trigger writing (default ``["on_task_finish"]``)
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from typing import TYPE_CHECKING

from downctl import console
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    pass

READ_SIZE = 1 << 20
_SUPPORTED = ("sha256", "sha1", "sha512", "md5")


class ChecksumHook:
    """Write ``<file>.<algo>`` digest sidecars for completed downloads."""

    name = "checksum"
    api_version = API_VERSION
    priority = 0

    def __init__(self, algo: str = "sha256", events: list[str] | None = None) -> None:
        algo = (algo or "sha256").lower()
        self.algo = algo if algo in _SUPPORTED else "sha256"
        self.events = events if events is not None else ["on_task_finish"]
        self._pending: set[asyncio.Task[object]] = set()

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        if event not in self.events or not payload.get("ok"):
            return
        task = payload.get("task")
        meta = getattr(task, "meta", {}) if task is not None else {}
        path = meta.get("path") if isinstance(meta, dict) else None
        if not path:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - sync contexts
            self._write(str(path))
            return
        bg = loop.create_task(asyncio.to_thread(self._write, str(path)))
        self._pending.add(bg)
        bg.add_done_callback(self._pending.discard)

    async def aclose(self) -> None:
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    def _write(self, path: str) -> str | None:
        if not os.path.isfile(path):
            return None
        try:
            digest = _hash_file(path, self.algo)
            sidecar = f"{path}.{self.algo}"
            with open(sidecar, "w", encoding="utf-8") as fh:
                fh.write(f"{digest}  {os.path.basename(path)}\n")
        except (OSError, ValueError) as exc:
            console.warn(f"checksum: could not write sidecar for {path}: {exc}")
            return None
        console.info(f"  wrote {sidecar}")
        return sidecar


def _hash_file(path: str, algo: str) -> str:
    hasher = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(READ_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
