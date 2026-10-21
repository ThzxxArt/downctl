"""The JSON-lines reporter plugin (machine-readable progress).

Progress is throttled (one line per whole percent, or per 8 MiB when the size
is unknown) so a large download does not flood stdout.

Every line is a standalone JSON object on **stdout**; selecting this reporter
also silences human-readable stdout chatter (warnings/errors stay on stderr),
so ``downctl get --reporter json | jq`` works without filtering.
"""

from __future__ import annotations

import json
import os
import sys
from typing import TYPE_CHECKING, TextIO

from downctl.core.errors import FAILURE_PERMANENT
from downctl.plugin.protocols import API_VERSION
from downctl.urls import display_url, redact_text

if TYPE_CHECKING:
    from downctl.core.task import DownloadTask

UNKNOWN_SIZE_STEP = 8 * 1024 * 1024


class JSONReporter:
    """Emit one JSON object per line for tooling and log pipelines."""

    name = "json"
    api_version = API_VERSION
    priority = 0
    #: Tells the CLI that stdout belongs to this reporter alone.
    machine_readable = True

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout
        self._downloaded: dict[int, int] = {}
        self._last_percent: dict[int, int] = {}
        self._broken = False

    def _write(self, record: dict[str, object]) -> None:
        if self._broken:
            return
        try:
            self._stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._stream.flush()
        except BrokenPipeError:
            # ``downctl ... --reporter json | head`` closes stdout early. Keep
            # the transfer running; Python's final flush must not raise again.
            self._broken = True
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                try:
                    os.dup2(devnull, self._stream.fileno())
                finally:
                    os.close(devnull)
            except (AttributeError, OSError, ValueError):
                pass

    def _emit(self, event: str, task: DownloadTask, **extra: object) -> None:
        record: dict[str, object] = {
            "event": event,
            "target": task.target,
            "url": display_url(task.url, bool(task.meta.get("redact_url"))),
            "origin": display_url(task.origin),
        }
        job = str(task.meta.get("job") or "")
        if job:
            # ``downctl batch`` runs several jobs in one process; a consumer
            # that only sees ``target`` cannot tell which run a line belongs to.
            record["job"] = job
        record.update(extra)
        self._write(record)

    def run_started(
        self, total_tasks: int, total_bytes: int, jobs: int = 1, *, job: str = ""
    ) -> None:
        record: dict[str, object] = {
            "event": "run_start",
            "tasks": total_tasks,
            "bytes": total_bytes,
            "jobs": jobs,
        }
        if job:
            record["job"] = job
        self._write(record)

    def run_finished(
        self, succeeded: int, failed: int, *, job: str = "", hooks_failed: int = 0
    ) -> None:
        record: dict[str, object] = {
            "event": "run_finish",
            "succeeded": succeeded,
            "failed": failed,
            "hooks_failed": hooks_failed,
        }
        if job:
            record["job"] = job
        self._write(record)

    def failure(
        self,
        target: str,
        error: str = "",
        url: str = "",
        *,
        reason: str = FAILURE_PERMANENT,
    ) -> None:
        """A target that failed before the engine (resolve/probe).

        ``reason`` is ``permanent``, ``transient-exhausted`` or ``timeout``, so
        a machine can tell a retryable ``5xx`` whose budget ran out from a
        ``404`` without parsing ``error``.
        """
        self._write(
            {
                "event": "failure",
                "target": display_url(target),
                "url": display_url(url) if url else display_url(target),
                "error": redact_text(error),
                "reason": reason,
            }
        )

    def task_started(self, task: DownloadTask) -> None:
        self._downloaded[id(task)] = 0
        self._last_percent[id(task)] = -1
        self._emit("start", task, size=task.size)

    def run_resumed(self, done_bytes: int) -> None:
        if done_bytes > 0:
            self._write({"event": "run_resume", "downloaded": done_bytes})

    def task_resumed(self, task: DownloadTask, done_bytes: int) -> None:
        if done_bytes <= 0:
            return
        self._downloaded[id(task)] = done_bytes
        total = task.size or 0
        self._last_percent[id(task)] = done_bytes * 100 // total if total > 0 else done_bytes
        self._emit("resume", task, downloaded=done_bytes, size=total)

    def task_progress(self, task: DownloadTask, delta: int) -> None:
        downloaded = self._downloaded.get(id(task), 0) + delta
        self._downloaded[id(task)] = downloaded
        total = task.size or 0
        if total > 0:
            percent = downloaded * 100 // total
            if percent == self._last_percent.get(id(task)):
                return
            self._last_percent[id(task)] = percent
        else:
            last = self._last_percent.get(id(task), 0)
            if downloaded - last < UNKNOWN_SIZE_STEP:
                return
            self._last_percent[id(task)] = downloaded
        self._emit("progress", task, downloaded=downloaded, size=total)

    def task_finished(self, task: DownloadTask, ok: bool) -> None:
        record: dict[str, object] = {
            "ok": ok,
            "downloaded": self._downloaded.get(id(task), 0),
        }
        # ``witness`` is how the bytes on disk are known to be the right
        # object's: ``validator`` (a version witness proved it), ``checksum``
        # (a digest did) or ``none`` (nothing did). Without it, ``ok: true`` is
        # indistinguishable from an unversioned skip — a success a machine
        # would be wrong to trust.
        witness = task.meta.get("witness")
        if witness is not None:
            record["witness"] = witness
        # ``reason`` is the mirror image: why a target that did *not* finish
        # failed, so a CI job can tell "retry this" from "stop".
        reason = task.meta.get("failure")
        if reason is not None:
            record["reason"] = reason
        self._emit("finish", task, **record)
        self._downloaded.pop(id(task), None)
        self._last_percent.pop(id(task), None)
