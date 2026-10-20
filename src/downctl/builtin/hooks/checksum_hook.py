"""The checksum hook plugin: write a digest sidecar for each download.

On a successful ``on_task_finish`` a ``<file>.<algo>`` sidecar is written in the
``sha256sum`` line format (``<hex>  <basename>``), ready to be consumed by
``--checksums`` later.

A sidecar is a claim about *the remote object*, not merely about the local file:
``--checksums`` will later treat it as the expected digest and bless whatever
matches it. So it is only written for a completion whose version this run could
actually tie down (``witness`` is ``validator`` or ``checksum``); an unversioned
skip produces no sidecar, because it has no such claim to make.

Options (``[plugins.checksum]``):

- ``algo``:   hash algorithm, default ``sha256`` (``sha1``/``sha512``/``md5`` also work)
- ``events``: events that trigger writing (default ``["on_task_finish"]``)
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from downctl import console
from downctl.digest import hash_file
from downctl.plugin.hooks import ConfigurableHook, HookTasks
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    pass

_SUPPORTED = ("sha256", "sha1", "sha512", "md5")


class ChecksumHook(ConfigurableHook):
    """Write ``<file>.<algo>`` digest sidecars for completed downloads."""

    name = "checksum"
    api_version = API_VERSION
    priority = 0
    OPTIONS = {"algo": "str", "events": "events"}

    def __init__(self, algo: str = "sha256", events: list[str] | None = None) -> None:
        self.algo = algo
        self.events = events if events is not None else ["on_task_finish"]
        self._tasks = HookTasks(self.name)

    @property
    def algo(self) -> str:
        return self._algo

    @algo.setter
    def algo(self, value: str) -> None:
        # Unknown algorithms fall back rather than producing a sidecar nobody
        # can verify; ``configure`` rejects them outright so the fallback only
        # covers direct construction.
        algo = (value or "sha256").lower()
        self._algo = algo if algo in _SUPPORTED else "sha256"

    def validate_option(self, key: str, value: object) -> None:
        if key == "algo" and str(value).lower() not in _SUPPORTED:
            raise ValueError(f"algo must be one of {', '.join(_SUPPORTED)}, got {value!r}")

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        if event not in self.events or not payload.get("ok"):
            return
        task = payload.get("task")
        meta = getattr(task, "meta", {}) if task is not None else {}
        path = meta.get("path") if isinstance(meta, dict) else None
        if not path:
            return
        # ``witness`` is what the engine says the completion rests on. Only a
        # version comparison or a digest keeps the sidecar's promise; a skip
        # that could not check anything (``none``) would turn "the bytes are on
        # disk" into "the remote had these bytes".
        if str(payload.get("witness") or "none") == "none":
            console.verbose(
                f"checksum: not writing a sidecar for {path} "
                "(the completion is not tied to a version)"
            )
            return
        if not self._tasks.spawn_thread(self._write, str(path)):
            self._write(str(path))  # pragma: no cover - only without a loop

    async def aclose(self) -> int:
        return await self._tasks.aclose()

    def _write(self, path: str) -> str | None:
        if not os.path.isfile(path):
            return None
        try:
            digest = hash_file(path, self.algo)
            sidecar = f"{path}.{self.algo}"
            with open(sidecar, "w", encoding="utf-8") as fh:
                fh.write(f"{digest}  {os.path.basename(path)}\n")
        except (OSError, ValueError) as exc:
            console.warn(f"checksum: could not write sidecar for {path}: {exc}")
            self._tasks.record()
            return None
        console.info(f"  wrote {sidecar}")
        return sidecar
