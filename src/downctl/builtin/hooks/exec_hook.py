"""The exec hook plugin: run a command when a lifecycle event fires.

Options (``[plugins.exec]``):

- ``command``:  shell command to run (required); the event details are exposed
  through the ``DOWNCTL_*`` environment variables
- ``events``:   which events trigger it (default ``["on_task_finish"]``)
- ``timeout``:  seconds before the command is killed (default 300)

Environment passed to the command: ``DOWNCTL_EVENT``, ``DOWNCTL_TARGET``,
``DOWNCTL_PATH``, ``DOWNCTL_URL``, ``DOWNCTL_ORIGIN`` and ``DOWNCTL_OK``.
``DOWNCTL_URL`` has credentials masked (and a presigned query stripped), so a
hook script cannot accidentally log a signature.

.. warning::
    The command runs with your privileges. Only configure commands you trust.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from downctl import console
from downctl.plugin.hooks import ConfigurableHook, HookTasks
from downctl.plugin.protocols import API_VERSION
from downctl.urls import display_url

if TYPE_CHECKING:
    pass


class ExecHook(ConfigurableHook):
    """Run a shell command in response to lifecycle events."""

    name = "exec"
    api_version = API_VERSION
    priority = 0
    OPTIONS = {"command": "str", "events": "events", "timeout": "int"}

    def __init__(
        self,
        command: str = "",
        events: list[str] | None = None,
        timeout: int = 300,
    ) -> None:
        self.command = command
        self.events = events if events is not None else ["on_task_finish"]
        self.timeout = timeout
        self._tasks = HookTasks(self.name)

    def on_event(self, event: str, payload: dict[str, object]) -> None:
        if not self.command or event not in self.events:
            return
        # A command that exits non-zero (or is killed on timeout) has to show up:
        # it used to be dropped together with its task handle, so a broken
        # post-processing script looked exactly like a successful run.
        if not self._tasks.spawn_thread(self._run, event, payload):
            self._run(event, payload)  # pragma: no cover - only without a loop

    async def aclose(self) -> int:
        return await self._tasks.aclose()

    def _run(self, event: str, payload: dict[str, object]) -> int | None:
        env = dict(os.environ)
        env["DOWNCTL_EVENT"] = event
        task = payload.get("task")
        if task is not None:
            meta = getattr(task, "meta", {})
            redact = bool(meta.get("redact_url")) if isinstance(meta, dict) else False
            env["DOWNCTL_TARGET"] = str(getattr(task, "target", ""))
            env["DOWNCTL_URL"] = display_url(str(getattr(task, "url", "")), redact)
            env["DOWNCTL_ORIGIN"] = display_url(str(getattr(task, "origin", "")))
            if isinstance(meta, dict) and meta.get("path"):
                env["DOWNCTL_PATH"] = str(meta["path"])
        if "ok" in payload:
            env["DOWNCTL_OK"] = "1" if payload["ok"] else "0"
        try:
            completed = subprocess.run(  # noqa: S602 - user-configured command
                self.command,
                shell=True,
                env=env,
                timeout=self.timeout,
                check=False,
                # A hook must not corrupt a machine-readable JSON stream;
                # keep its stdout visible on stderr in that mode.
                stdout=sys.stderr if console.is_machine_readable() else None,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            console.warn(f"exec: command failed: {exc}", always=True)
            self._tasks.record()
            return None
        if completed.returncode != 0:
            console.warn(f"exec: command exited with {completed.returncode}", always=True)
            self._tasks.record()
        return completed.returncode
