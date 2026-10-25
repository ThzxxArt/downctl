"""The observable state behind the terminal progress display.

The reporter receives a stream of small events (a task started, a chunk
arrived, a retry is waiting) and keeps enough state to answer, at any moment:

* how much of each file is done, and how fast it is moving now;
* what every visible worker is doing (downloading, verifying, waiting to retry);
* how much of the whole run is done, and how long it has left.

Nothing in this module writes to a terminal. That split is deliberate: the
renderer can be tested as a pure function, and the reporter can keep collecting
events even when the output stream is a pipe or a broken terminal.

The state model is also careful about *what it does not know*:

* ``task_progress`` deltas are per attempt. A retry or a restart can resend
  bytes, so a task's displayed progress is clamped to its declared size, and a
  restart event resets the received count.
* An unknown-size transfer has no honest percentage or ETA. ``fraction`` and
  ``eta`` return ``None`` instead of inventing one, and the renderer prints
  ``--%`` / ``--:--``.
* ``run_resumed`` is an aggregate seed for bytes that were already complete
  before this run. When it is present, the aggregate counts the seed plus newly
  received bytes; it must not add each task's resume point a second time.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from downctl.core.task import DownloadTask


class Rate:
    """A sliding-window byte rate.

    A three-second window is short enough to show a stall and long enough that
    one slow chunk does not make the ETA jump. When there is no recent sample
    the rate is ``None``: "we do not know" is more useful than a frozen average
    from ten minutes ago.
    """

    __slots__ = ("_samples", "last_at", "total", "window")

    def __init__(self, window: float = 3.0) -> None:
        self.window = window
        self.total = 0
        self.last_at = 0.0
        self._samples: deque[tuple[float, int]] = deque(maxlen=128)

    def add(self, delta: int, now: float) -> None:
        if delta <= 0:
            return
        self.total += delta
        self.last_at = now
        self._samples.append((now, self.total))
        self._prune(now)

    def _prune(self, now: float) -> None:
        while len(self._samples) > 2 and self._samples[0][0] < now - self.window:
            self._samples.popleft()

    def rate(self, now: float) -> float | None:
        if self.last_at and now - self.last_at > self.window:
            # Every sample is older than the window: this transfer is not
            # moving. A frozen average from last minute is not a rate.
            return None
        self._prune(now)
        if len(self._samples) < 2:
            return None
        first_at, first_total = self._samples[0]
        last_at, last_total = self._samples[-1]
        elapsed = last_at - first_at
        if elapsed <= 0.05:
            return None
        return max(0.0, (last_total - first_total) / elapsed)

    def reset(self) -> None:
        self.total = 0
        self.last_at = 0.0
        self._samples.clear()


TERMINAL_STATES = frozenset({"done", "skipped", "failed"})
ACTIVE_STATES = frozenset({"active", "verifying", "retrying"})

#: Human labels for the engine's failure classes.
FAILURE_LABELS = {
    "permanent": "failed",
    "transient-exhausted": "gave up",
    "timeout": "timed out",
    "io": "I/O error",
    "unknown": "failed",
}


def failure_label(reason: str) -> str:
    """A short, non-alarming description of an engine failure class."""
    return FAILURE_LABELS.get(reason, reason or "failed")


@dataclass
class TaskView:
    """What the display knows about one file."""

    target: str
    size: int = 0
    received: int = 0
    resumed: int = 0
    state: str = "active"
    failure: str = ""
    detail: str = ""
    attempt: int = 0
    retry_until: float = 0.0
    retry_reason: str = ""
    started_at: float = 0.0
    finished_at: float | None = None
    last_at: float = 0.0
    connections: int = 0
    rate: Rate = field(default_factory=Rate)

    def done(self) -> int:
        """Bytes this file can be said to have (never more than its size)."""
        if self.size > 0:
            return min(self.size, self.resumed + self.received)
        return self.resumed + self.received

    def fraction(self) -> float | None:
        if self.size <= 0:
            return None
        return max(0.0, min(1.0, self.done() / self.size))

    def remaining(self) -> int | None:
        if self.size <= 0:
            return None
        return max(0, self.size - self.done())

    def eta(self, now: float) -> float | None:
        remaining = self.remaining()
        if remaining is None:
            return None
        speed = self.rate.rate(now)
        if not speed:
            return None
        return remaining / speed

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    def retry_seconds(self, now: float) -> int:
        return max(0, int(self.retry_until - now + 0.999))


@dataclass
class RunView:
    """The state of one run (one ``get`` or one batch job)."""

    total_tasks: int = 0
    total_bytes: int = 0
    jobs: int = 1
    job: str = ""
    unknown_tasks: int = 0
    phase: str = "idle"
    phase_done: int = 0
    phase_total: int = 0
    started_at: float = 0.0
    finished_at: float | None = None
    tasks: dict[int, TaskView] = field(default_factory=dict)
    aggregate: Rate = field(default_factory=Rate)
    seed: int = 0
    seed_seen: bool = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(
        self,
        total_tasks: int,
        total_bytes: int,
        jobs: int = 1,
        *,
        job: str = "",
        unknown: int = 0,
        now: float = 0.0,
    ) -> None:
        self.total_tasks = max(0, total_tasks)
        self.total_bytes = max(0, total_bytes)
        self.jobs = max(1, jobs)
        self.job = job
        self.unknown_tasks = max(0, unknown)
        self.phase = "download"
        self.phase_done = 0
        self.phase_total = 0
        self.started_at = now
        self.finished_at = None
        self.tasks.clear()
        self.aggregate = Rate()
        self.seed = 0
        self.seed_seen = False

    def finish(self, now: float) -> None:
        self.finished_at = now
        self.phase = "finished"

    def set_phase(self, name: str, done: int = 0, total: int = 0) -> None:
        if self.finished_at is not None:
            return
        if self.phase != name:
            self.phase_done = 0
            self.phase_total = 0
        self.phase = name
        if total > 0:
            self.phase_total = total
        if done > 0:
            self.phase_done = done
        elif self.phase_total and name != "probe":
            self.phase_done = min(self.phase_done, self.phase_total)

    # ------------------------------------------------------------------ #
    # task events
    # ------------------------------------------------------------------ #
    def task_started(self, task: DownloadTask, now: float) -> TaskView:
        key = id(task)
        view = self.tasks.get(key)
        if view is None:
            view = TaskView(target=task.target, started_at=now)
            self.tasks[key] = view
        if task.size > 0:
            view.size = task.size
        view.state = "active"
        view.last_at = now
        return view

    def task_progress(self, task: DownloadTask, delta: int, now: float) -> None:
        view = self.tasks.get(id(task))
        if view is None:
            view = self.task_started(task, now)
        if view.is_terminal():
            return
        if task.size > 0:
            view.size = task.size
        if delta > 0:
            view.received += delta
            view.rate.add(delta, now)
            self.aggregate.add(delta, now)
            view.last_at = now
            if view.state == "retrying":
                view.state = "active"
                view.retry_until = 0.0
                view.retry_reason = ""

    def task_resumed(self, task: DownloadTask, done: int, now: float) -> None:
        if done <= 0:
            return
        view = self.tasks.get(id(task)) or self.task_started(task, now)
        view.resumed = done
        if task.size > 0:
            view.size = task.size

    def run_resumed(self, done: int, now: float) -> None:
        if done <= 0:
            return
        self.seed = done
        self.seed_seen = True
        self.started_at = self.started_at or now

    def task_phase(self, task: DownloadTask, phase: str, now: float, detail: str = "") -> None:
        view = self.tasks.get(id(task))
        if view is None or view.is_terminal():
            return
        if phase == "verify":
            view.state = "verifying"
            view.detail = detail or "checksum"
        elif phase in ("download", "active"):
            view.state = "active"
            view.detail = ""

    def task_retrying(
        self,
        task: DownloadTask,
        attempt: int,
        delay: float,
        reason: str,
        now: float,
    ) -> None:
        view = self.tasks.get(id(task))
        if view is None or view.is_terminal():
            return
        view.state = "retrying"
        view.attempt = attempt
        view.retry_until = now + max(0.0, delay)
        view.retry_reason = reason

    def task_restarted(self, task: DownloadTask, now: float) -> None:
        view = self.tasks.get(id(task))
        if view is None or view.is_terminal():
            return
        if self.seed_seen:
            # The run-level seed included this file's resume point. Those
            # bytes are now abandoned, so they must leave the aggregate too.
            self.seed = max(0, self.seed - view.resumed)
        view.received = 0
        view.resumed = 0
        view.rate.reset()
        view.state = "active"
        view.detail = ""
        view.last_at = now

    def task_finished(self, task: DownloadTask, ok: bool, now: float) -> None:
        view = self.tasks.get(id(task))
        if view is None:
            view = self.task_started(task, now)
        if task.size > 0:
            view.size = task.size
        view.finished_at = now
        view.detail = ""
        view.retry_until = 0.0
        if ok:
            # A finished file with no bytes requested in this run was skipped:
            # it was already complete, and the witness in ``meta`` says how
            # that was proven. Calling that "downloaded 12 MB" would be false.
            view.state = "skipped" if view.received == 0 and view.size > 0 else "done"
        else:
            view.state = "failed"
            view.failure = str(task.meta.get("failure") or "")
            if view.size > 0 and view.received == 0:
                view.resumed = min(view.resumed, view.size)

    # ------------------------------------------------------------------ #
    # aggregates
    # ------------------------------------------------------------------ #
    def done_bytes(self) -> int:
        """Bytes complete across the run, including the resume seed.

        ``seed_seen`` matters: the engine reports the already-complete total
        once, then reports per-task resume points for those same bytes. Adding
        both is how a resumed run used to claim 150 % completion.
        """
        if self.seed_seen:
            received = sum(task.received for task in self.tasks.values())
            return (
                min(self.total_bytes, self.seed + received)
                if self.total_bytes
                else self.seed + received
            )
        return sum(task.done() for task in self.tasks.values())

    def known_done(self) -> int:
        if self.seed_seen:
            received = sum(t.received for t in self.tasks.values())
            total = self.seed + received
            return min(self.total_bytes, total) if self.total_bytes else total
        return sum(t.done() for t in self.tasks.values() if t.size > 0)

    def fraction(self) -> float | None:
        if self.unknown_tasks > 0 or self.total_bytes <= 0:
            return None
        return max(0.0, min(1.0, self.known_done() / self.total_bytes))

    def remaining_bytes(self) -> int | None:
        if self.unknown_tasks > 0:
            return None
        started_known = sum(t.size for t in self.tasks.values() if t.size > 0)
        queued_known = max(0, self.total_bytes - started_known)
        live = sum(
            max(0, task.size - task.done())
            for task in self.tasks.values()
            if task.size > 0 and not task.is_terminal()
        )
        return queued_known + live

    def eta(self, now: float) -> float | None:
        remaining = self.remaining_bytes()
        if remaining is None:
            return None
        speed = self.aggregate.rate(now)
        if not speed:
            return None
        return remaining / speed

    def rate(self, now: float) -> float | None:
        return self.aggregate.rate(now)

    def counts(self) -> tuple[int, int, int, int, int]:
        """``(done, failed, skipped, active, queued)``."""
        done = failed = skipped = active = 0
        for task in self.tasks.values():
            if task.state == "done":
                done += 1
            elif task.state == "skipped":
                skipped += 1
            elif task.state == "failed":
                failed += 1
            elif task.state in ACTIVE_STATES:
                active += 1
        queued = max(0, self.total_tasks - len(self.tasks))
        return done, failed, skipped, active, queued

    def elapsed(self, now: float) -> float:
        end = self.finished_at if self.finished_at is not None else now
        return max(0.0, end - self.started_at)
