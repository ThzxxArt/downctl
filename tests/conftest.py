"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

#: No test may pay real retry backoff. ``DOWNCTL_RETRY_BASE=0`` keeps the
#: jitter at 0-1s while leaving the retry *count* semantics untouched, so a test
#: that wants to observe "give up after N retries" still does. Individual tests
#: override this with ``monkeypatch.setenv`` when they assert on pacing.
os.environ.setdefault("DOWNCTL_RETRY_BASE", "0")
