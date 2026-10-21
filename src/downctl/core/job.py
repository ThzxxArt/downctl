"""The batch-job model.

A :class:`Job` is *one group of targets that share a set of run options*: the
unit ``downctl batch`` executes and ``JobSource`` plugins produce. It belongs to
core rather than to a plugin for the same reason :class:`DownloadTask` does — it
is the shape the engine and the CLI agree on, and a plugin that invented its own
would be a second answer to the same question.

It is deliberately **not** a ``Source``: ``Source.resolve`` returns
``DownloadTask`` objects, while ``dir``/``include``/``force`` are *run-level*
parameters that cannot be attached to a single file without leaking into the
engine's contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Run options a job may set. Everything here is an option of ``downctl get``
#: that changes *how one group of targets is fetched*. Options that change the
#: meaning of the batch itself (``--list``, ``--retry-failed``, ``--reporter``,
#: ``--local-plugins``) are refused: a job cannot redefine the run that contains
#: it, and a per-job ``--list`` would be a second job model.
ALLOWED_JOB_OPTIONS: frozenset[str] = frozenset(
    {
        "dir",
        "jobs",
        "connections",
        "stall",
        "checkpoint",
        "max_time",
        "probe_jobs",
        "piece_size",
        "min_split_size",
        "limit_rate",
        "retries",
        "force",
        "skip_existing",
        "require_witness",
        "recheck",
        "dry_run",
        "continue_",
        "checksum",
        "checksums",
        "include",
        "exclude",
        "revision",
        "source",
        "proxy",
        "no_proxy",
        "router",
        "mirror",
        "hook",
        "header",
        "user",
        "user_agent",
        "output",
    }
)

#: Options refused in a job file, with the reason. Kept separate from
#: :data:`ALLOWED_JOB_OPTIONS` so the error can say *why* the key is absent
#: instead of only that it is unknown.
REFUSED_JOB_OPTIONS: dict[str, str] = {
    "list": "the batch file is already the list of targets",
    "retry_failed": (
        "the failed log is per destination directory; use "
        "`downctl get --retry-failed -d <that job's dir>`"
    ),
    "reporter": "one run has one reporter",
    "local_plugins": "local plugin directories are process-wide",
    "targets": "use `targets` as the job's own key, not an option",
}


@dataclass(frozen=True)
class Job:
    """One group of targets and the options they share."""

    #: Stable identifier, used by ``--job`` and carried in events.
    name: str
    #: Targets this job expands (same syntax as ``downctl get`` arguments).
    targets: list[str]
    #: Options that override the batch command's own options for this job.
    options: dict[str, object] = field(default_factory=dict)
    #: Where the job was read from (a file path), for error messages.
    origin: str = ""

    def label(self) -> str:
        """A printable identifier for summaries and events."""
        return self.name or "<unnamed>"
