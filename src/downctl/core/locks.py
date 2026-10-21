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


#: Marker file inside every lock directory downctl creates. ``clean`` used to
#: treat any ``*.lock`` directory with a dead pid as garbage, so a user's own
#: directory that happened to contain a ``pid`` file could be deleted. Only a
#: directory carrying this marker is a downctl lock.
LOCK_MARKER = "downctl-lock"


class FileLock:
    """A cooperative lock backed by ``<target>.lock`` directory existence."""

    #: How many times one ``acquire`` may remove a stale lock and retry. A
    #: recursive retry was unbounded: if ``rmtree`` kept failing (a file
    #: appearing inside the directory, a permission change), the stack grew
    #: until Python gave up.
    TAKEOVER_ATTEMPTS = 3

    def __init__(self, target: str) -> None:
        self.lock_dir = target + ".lock"
        self.held = False

    def acquire(self) -> None:
        for _attempt in range(self.TAKEOVER_ATTEMPTS):
            try:
                os.mkdir(self.lock_dir)
            except FileExistsError:
                pass
            except OSError as exc:
                raise LockError(f"cannot create lock {self.lock_dir}: {exc}") from exc
            else:
                with open(os.path.join(self.lock_dir, "pid"), "w", encoding="utf-8") as fh:
                    fh.write(str(os.getpid()))
                with open(os.path.join(self.lock_dir, LOCK_MARKER), "w", encoding="utf-8") as fh:
                    fh.write("downctl\n")
                self.held = True
                return

            # Lock exists: only take it over when the owning pid is known to be
            # dead. A missing pid file is treated as held (a competing process
            # may be mid-acquire) — stealing it could let two writers share the
            # file.
            owner = self._owner()
            if owner is not None and not pid_alive(owner):
                shutil.rmtree(self.lock_dir, ignore_errors=True)
                if not os.path.exists(self.lock_dir):
                    continue
                # It survived the removal: report rather than recurse. Trying
                # forever turns a stuck directory into a hung download.
                raise LockError(
                    f"cannot take over the stale lock {self.lock_dir}: "
                    "the directory could not be removed"
                )
            name = os.path.basename(self.lock_dir)
            if owner == os.getpid():
                # Two tasks in one run resolved to the same target (a URL alias,
                # or a source that lists a file twice). "Another process" sent
                # the user looking for a process that does not exist.
                raise LockError(f"another task in this run is writing {name}: {self.lock_dir}")
            holder = (
                f"another downctl process (pid {owner})" if owner else "another downctl process"
            )
            raise LockError(f"{holder} holds the lock for {name}: {self.lock_dir}")
        raise LockError(f"could not acquire the lock {self.lock_dir} after retries")

    def _owner(self) -> int | None:
        """The pid recorded in the lock, or ``None`` when it has none/is invalid."""
        pidfile = os.path.join(self.lock_dir, "pid")
        if not os.path.isfile(pidfile):
            return None
        try:
            with open(pidfile, encoding="utf-8") as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self.held:
            shutil.rmtree(self.lock_dir, ignore_errors=True)
            self.held = False

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
