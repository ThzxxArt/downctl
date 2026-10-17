"""Lightweight, level-based console output with optional color.

All human-facing messages go through here so ``-q/--quiet``, ``-v/--verbose``
and color behave consistently across the CLI, engine and plugins. Color is
enabled only on a TTY and when ``NO_COLOR`` is unset. Machine-readable output
(the JSON reporter) sets :func:`set_machine_readable`, which silences stdout
chatter while keeping warnings/errors on stderr.
"""

from __future__ import annotations

import os
import sys

QUIET = 0
NORMAL = 1
VERBOSE = 2

_RED = "\033[31m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_RESET = "\033[0m"

_level = NORMAL
_color: bool | None = None
_machine = False


def set_level(level: int) -> None:
    global _level
    _level = level


def get_level() -> int:
    return _level


def is_quiet() -> bool:
    return _level <= QUIET


def set_machine_readable(enabled: bool) -> None:
    """Silence human-readable stdout output (machine consumers own stdout).

    Warnings and errors still go to stderr: a JSON pipeline keeps a clean
    stream *and* the operator still sees problems.
    """
    global _machine
    _machine = enabled


def is_machine_readable() -> bool:
    return _machine


def set_color(enabled: bool | None) -> None:
    """Force color on/off (mainly for tests); ``None`` restores auto-detect."""
    global _color
    _color = enabled


def color_enabled() -> bool:
    if _color is not None:
        return _color
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty()) if sys.stdout is not None else False
    except Exception:
        return False


def _paint(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}" if color_enabled() else text


def info(message: str) -> None:
    """Normal informational output (hidden when quiet or machine-readable)."""
    if _level >= NORMAL and not _machine:
        print(message)


def success(message: str) -> None:
    """A success message (hidden when quiet or machine-readable)."""
    if _level >= NORMAL and not _machine:
        print(_paint(message, _GREEN))


def verbose(message: str) -> None:
    """Extra diagnostics (shown only with ``-v``, never in machine mode)."""
    if _level >= VERBOSE and not _machine:
        print(message)


def warn(message: str) -> None:
    """A warning to stderr (hidden when quiet)."""
    if _level >= NORMAL:
        print(_paint(f"downctl: warning: {message}", _YELLOW), file=sys.stderr)


def error(message: str) -> None:
    """An error to stderr (always shown)."""
    print(_paint(f"downctl: error: {message}", _RED), file=sys.stderr)
