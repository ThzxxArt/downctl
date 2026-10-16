"""Lightweight, level-based console output.

All human-facing messages go through here so ``-q/--quiet`` and
``-v/--verbose`` behave consistently across the CLI, engine and plugins.
Machine-readable output (the JSON reporter) does not use this.
"""

from __future__ import annotations

import sys

QUIET = 0
NORMAL = 1
VERBOSE = 2

_level = NORMAL


def set_level(level: int) -> None:
    global _level
    _level = level


def get_level() -> int:
    return _level


def is_quiet() -> bool:
    return _level <= QUIET


def info(message: str) -> None:
    """Normal informational output (hidden when quiet)."""
    if _level >= NORMAL:
        print(message)


def verbose(message: str) -> None:
    """Extra diagnostics (shown only with ``-v``)."""
    if _level >= VERBOSE:
        print(message)


def warn(message: str) -> None:
    """A warning to stderr (hidden when quiet)."""
    if _level >= NORMAL:
        print(f"downctl: warning: {message}", file=sys.stderr)


def error(message: str) -> None:
    """An error to stderr (always shown)."""
    print(f"downctl: error: {message}", file=sys.stderr)
