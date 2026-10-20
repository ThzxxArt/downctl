"""Directory-based single-file / single-instance locks.

A lock is a directory next to the target. If the owning pid is dead, the lock
is considered stale and taken over. This prevents two downctl processes from
writing the same file concurrently.
"""

from __future__ import annotations

import os
import shutil

from downctl.core.errors import DownctlError
from downctl.platform_utils import pid_alive


class LockError(DownctlError):
    """Raised when a lock is held by a live process."""


class FileLock:
    """A cooperative lock backed by ``<target>.lock`` directory existence."""

    def __init__(self, target: str) -> None:
        self.lock_dir = target + ".lock"
        self.held = False

    def acquire(self) -> None:
        try:
            os.mkdir(self.lock_dir)
        except FileExistsError:
            pass
        except OSError as exc:
            raise LockError(f"cannot create lock {self.lock_dir}: {exc}") from exc
        else:
            with open(os.path.join(self.lock_dir, "pid"), "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            self.held = True
            return

        # Lock exists: only take it over when the owning pid is known to be dead.
        # A missing pid file is treated as held (a competing process may be
        # mid-acquire) — stealing it could let two writers share the file.
        stale = False
        owner: int | None = None
        pidfile = os.path.join(self.lock_dir, "pid")
        if os.path.isfile(pidfile):
            try:
                with open(pidfile, encoding="utf-8") as fh:
                    owner = int(fh.read().strip())
                stale = not pid_alive(owner)
            except ValueError:
                stale = False
        if stale:
            shutil.rmtree(self.lock_dir, ignore_errors=True)
            self.acquire()
            return
        name = os.path.basename(self.lock_dir)
        if owner == os.getpid():
            # Two tasks in one run resolved to the same target (a URL alias, or
            # a source that lists a file twice). "Another process" sent the user
            # looking for a process that does not exist.
            raise LockError(f"another task in this run is writing {name}: {self.lock_dir}")
        holder = f"another downctl process (pid {owner})" if owner else "another downctl process"
        raise LockError(f"{holder} holds the lock for {name}: {self.lock_dir}")

    def release(self) -> None:
        if self.held:
            shutil.rmtree(self.lock_dir, ignore_errors=True)
            self.held = False

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
