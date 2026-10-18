"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

#: No test may pay real retry backoff. ``DOWNCTL_RETRY_BASE=0`` keeps the
#: jitter at 0-1s while leaving the retry *count* semantics untouched, so a test
#: that wants to observe "give up after N retries" still does. Individual tests
#: override this with ``monkeypatch.setenv`` when they assert on pacing.
os.environ.setdefault("DOWNCTL_RETRY_BASE", "0")


# --------------------------------------------------------------------------- #
# platform capabilities
#
# CI runs the suite on Linux, macOS and Windows. A test that needs something a
# platform cannot do must say so through one of these fixtures instead of
# asserting a Linux-only fact. The 0.1.4 cycle shipped ten tests that were green
# here and red on the other two runners because each one quietly assumed ext4.
# --------------------------------------------------------------------------- #
@pytest.fixture
def requires_hole_detection() -> None:
    """Skip unless the platform tells data from holes *and* downctl uses it.

    ``os.SEEK_DATA``/``SEEK_HOLE`` are POSIX-only, and
    :func:`downctl.core.pieces.trusted_complete` distrusts unmarked files on
    native Windows regardless, so a test asserting *precise* hole detection has
    nothing to assert on either count.
    """
    if os.name == "nt" or not hasattr(os, "SEEK_DATA"):
        pytest.skip("precise hole detection unavailable on this platform")


@pytest.fixture
def require_real_hole() -> Callable[[Path, int], None]:
    """Return a checker that skips unless a file really contains a hole.

    Writing past EOF only leaves an unallocated gap on a filesystem that
    supports sparse files (ext4, xfs). APFS and NTFS materialise the gap with
    zeros, and then there is no hole for the kernel to report. ``st_blocks``
    says what the filesystem actually allocated, so it can tell the two apart
    without asking the code under test.
    """

    def _check(path: Path, size: int) -> None:
        if os.name == "nt" or not hasattr(os, "SEEK_DATA"):
            pytest.skip("precise hole detection unavailable on this platform")
        blocks = getattr(path.stat(), "st_blocks", 0)
        if blocks == 0 or blocks * 512 >= size:
            pytest.skip("filesystem materialised the gap; no hole to walk past")

    return _check


@pytest.fixture
def requires_posix_paths() -> None:
    """Skip unless ``os.path`` is POSIX.

    ``os.path.abspath`` on Windows prefixes a drive and switches to
    backslashes, so path-shape assertions (``/mnt/...``) and directory fsync
    have no Windows equivalent.
    """
    if os.name == "nt":
        pytest.skip("POSIX path semantics required")
