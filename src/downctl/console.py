"""Lightweight, level-based console output with optional color.

All human-facing messages go through here so ``-q/--quiet``, ``-v/--verbose``
and color behave consistently across the CLI, engine and plugins. Color is
enabled only on a TTY and when ``NO_COLOR`` is unset. Machine-readable output
(the JSON reporter) sets :func:`set_machine_readable`, which silences stdout
chatter while keeping warnings/errors on stderr.

A live progress block can be attached with :func:`attach_live`. Every message
then clears the block, prints, and lets the block redraw, so a warning raised
in the middle of a transfer does not tear the display apart. The console owns
this coordination rather than the renderer because *all* writers — the engine,
hooks on worker threads, and plugins — already go through this module.
"""

from __future__ import annotations

import contextlib
import os
import sys
from contextlib import AbstractContextManager
from typing import Protocol, TextIO

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


class _LiveRegion(Protocol):
    """What the console needs from a live display; no import cycle."""

    def writing(self) -> AbstractContextManager[None]: ...


_live: _LiveRegion | None = None


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
    """Whether human diagnostics may use colour.

    ``NO_COLOR`` is honoured by its *presence* (the standard says any value,
    including an empty one, disables colour). It says nothing about whether a
    progress display may exist; conflating the two made ``NO_COLOR=1`` turn a
    live transfer into a silent one.
    """
    if _color is not None:
        return _color
    if os.environ.get("NO_COLOR") is not None:
        return False
    forced = os.environ.get("FORCE_COLOR")
    if forced is not None and forced != "0":
        return True
    try:
        return bool(sys.stdout.isatty()) if sys.stdout is not None else False
    except Exception:
        return False


def _paint(text: str, code: str) -> str:
    return f"{code}{text}{_RESET}" if color_enabled() else text


def attach_live(region: _LiveRegion) -> None:
    """Make ``region`` the block that ordinary messages temporarily clear."""
    global _live
    _live = region


def detach_live(region: _LiveRegion | None = None) -> None:
    """Stop coordinating with ``region`` (or with whatever is attached)."""
    global _live
    if region is None or _live is region:
        _live = None


def reset_live() -> None:
    """Detach and close the live block, restoring the cursor.

    Called by the CLI guard on every exit path. A ``Ctrl-C`` can end a run
    without ``run_finished``; without this the block would stay on screen and
    the cursor would stay hidden.
    """
    global _live
    region = _live
    _live = None
    close = getattr(region, "close", None)
    if callable(close):
        # Restoring a terminal must never fail a command that already finished.
        with contextlib.suppress(Exception):
            close()


def _emit(message: str, stream: TextIO, *, flush: bool = False) -> None:
    region = _live
    if region is not None:
        try:
            with region.writing():
                print(message, file=stream, flush=flush)
        except Exception:  # noqa: S110 - fall back to a plain line
            pass
        else:
            return
    print(message, file=stream, flush=flush)


def info(message: str) -> None:
    """Normal informational output (hidden when quiet or machine-readable)."""
    if _level >= NORMAL and not _machine:
        _emit(message, sys.stdout)


def result(message: str) -> None:
    """Main command output: hidden by machine-readable mode, never by ``-q``.

    ``-q`` means "no progress chatter", not "throw away the answer": with the
    quiet level applied to these lines, ``downctl -q config show`` printed
    nothing and exited 0, and every ``status``/``clean``/``verify``/``plugin``
    answer disappeared the same way.
    """
    if not _machine:
        _emit(message, sys.stdout, flush=True)


def verbose(message: str) -> None:
    """Extra diagnostics (shown only with ``-v``, never in machine mode)."""
    if _level >= VERBOSE and not _machine:
        _emit(message, sys.stdout)


def warn(message: str, *, always: bool = False) -> None:
    """A warning to stderr (hidden when quiet).

    ``always=True`` keeps it visible under ``-q``. A warning that reports a
    failure of a documented feature is not chatter: silencing it is how
    ``-q`` turned every hook failure into nothing at all in 0.1.5, after the
    docs had promised they are always reported.
    """
    if always or _level >= NORMAL:
        _emit(_paint(f"downctl: warning: {message}", _YELLOW), sys.stderr)


def error(message: str) -> None:
    """An error to stderr (always shown)."""
    _emit(_paint(f"downctl: error: {message}", _RED), sys.stderr)
