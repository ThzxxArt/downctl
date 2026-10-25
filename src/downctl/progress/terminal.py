"""A very small live terminal region.

The display is one block of lines that is redrawn in place. This module owns
the three things that are easy to get wrong and impossible to test in the
renderer:

* **cursor arithmetic** — move up exactly as many lines as were drawn, clear
  each line, and leave the cursor at a predictable place;
* **interleaving** — a warning printed while the block is alive must not be
  overwritten by the next frame; :meth:`Terminal.writing` clears the block,
  lets the caller print, and redraws after;
* **restoration** — the cursor must come back and the block must be erased on
  normal completion, on an exception, on ``Ctrl-C`` and at interpreter exit.

Non-interactive streams (a pipe, a log file, a test's ``StringIO``) never get
escape sequences: ``ansi`` is false and the caller uses plain lines instead.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import TextIO

#: A sane fallback when the stream does not expose a terminal size.
DEFAULT_WIDTH = 100
DEFAULT_HEIGHT = 24


class Terminal:
    """A redrawable block of lines on one output stream."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        width: int | None = None,
        height: int | None = None,
        force_ansi: bool | None = None,
    ) -> None:
        self.stream: TextIO = stream or sys.stderr
        self._lock = threading.RLock()
        self._fixed_width = width
        self._fixed_height = height
        self._force_ansi = force_ansi
        self._drawn = 0
        self._last_lines: list[str] = []
        self._suspended = False
        self._hidden = False
        self._closed = False
        self._disabled = False
        atexit.register(self.close)

    # ------------------------------------------------------------------ #
    # capabilities
    # ------------------------------------------------------------------ #
    @property
    def ansi(self) -> bool:
        """Whether cursor movement is safe on this stream."""
        if self._disabled:
            return False
        if self._force_ansi is not None:
            return self._force_ansi
        term = os.environ.get("TERM", "")
        if term.lower() == "dumb":
            return False
        try:
            return bool(self.stream.isatty())
        except (AttributeError, OSError, ValueError):
            return False

    def measure(self) -> tuple[int, int]:
        """The drawing size as ``(columns, rows)``."""
        width = self._fixed_width
        height = self._fixed_height
        if width is None or height is None:
            size = self._os_size()
            if size is not None:
                if width is None:
                    width = size[0]
                if height is None:
                    height = size[1]
        if width is None:
            width = _env_int("COLUMNS") or DEFAULT_WIDTH
        if height is None:
            height = _env_int("LINES") or DEFAULT_HEIGHT
        return max(20, min(240, width)), max(3, min(60, height))

    def _os_size(self) -> tuple[int, int] | None:
        candidates: list[int] = []
        with suppress(AttributeError, OSError, ValueError):
            candidates.append(self.stream.fileno())
        for stream in (sys.stderr, sys.stdout):
            with suppress(AttributeError, OSError, ValueError):
                candidates.append(stream.fileno())
        for fd in candidates:
            try:
                size = os.get_terminal_size(fd)
            except OSError:
                continue
            if size.columns > 0 and size.lines > 0:
                return size.columns, size.lines
        try:
            size = shutil.get_terminal_size((DEFAULT_WIDTH, DEFAULT_HEIGHT))
        except OSError:  # pragma: no cover - shutil already falls back
            return None
        if size.columns > 0 and size.lines > 0:
            return size.columns, size.lines
        return None

    # ------------------------------------------------------------------ #
    # drawing
    # ------------------------------------------------------------------ #
    def _write(self, text: str) -> None:
        if self._disabled:
            return
        try:
            self.stream.write(text)
            self.stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            # The consumer went away. Stop drawing, but do not raise from a
            # reporter callback: that would fail a download because a terminal
            # closed.
            self._disabled = True

    def _hide_cursor(self) -> None:
        if not self._hidden:
            self._write("\x1b[?25l")
            self._hidden = True

    def _show_cursor(self) -> None:
        if self._hidden:
            self._write("\x1b[?25h")
            self._hidden = False

    def write_frame(self, lines: list[str]) -> None:
        """Replace the current block with ``lines`` (at most the window)."""
        _, rows = self.measure()
        clipped = lines[: max(1, rows - 1)]
        with self._lock:
            if self._closed or self._disabled or not self.ansi:
                return
            self._last_lines = list(clipped)
            if self._suspended:
                return
            self._hide_cursor()
            if self._drawn:
                self._write(f"\x1b[{self._drawn}A")
            old = self._drawn
            new = len(clipped)
            for index in range(max(old, new)):
                text = clipped[index] if index < new else ""
                self._write("\x1b[2K" + text + "\n")
            if max(old, new) > new:
                self._write(f"\x1b[{max(old, new) - new}A")
            self._drawn = new

    def clear(self) -> None:
        """Erase the block and put the cursor where its first line was."""
        with self._lock:
            if self._drawn and not self._disabled:
                count = self._drawn
                self._write(f"\x1b[{count}A")
                for _ in range(count):
                    self._write("\x1b[2K\n")
                self._write(f"\x1b[{count}A")
            self._drawn = 0
            self._show_cursor()

    def close(self) -> None:
        """Erase the block, restore the cursor, and stay closed."""
        with self._lock:
            if self._closed:
                return
            self.clear()
            self._closed = True
            self._show_cursor()
            self._last_lines = []
        with suppress(Exception):
            atexit.unregister(self.close)

    # ------------------------------------------------------------------ #
    # interleaving
    # ------------------------------------------------------------------ #
    def suspend(self) -> None:
        """Make room for a log line: erase the block, show the cursor."""
        with self._lock:
            if not self.ansi or self._closed:
                return
            self.clear()
            self._suspended = True

    def resume(self) -> None:
        """Redraw the last frame after a log line."""
        with self._lock:
            self._suspended = False
            if self._closed or self._disabled or not self._last_lines:
                return
            if not self.ansi:
                return
            self.write_frame(self._last_lines)

    @contextmanager
    def writing(self) -> Iterator[None]:
        """Pause the block for the duration of a normal ``print``."""
        with self._lock:
            active = self.ansi and not self._closed and self._drawn > 0
            if not active:
                yield
                return
            self.suspend()
            try:
                yield
            finally:
                self.resume()


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


__all__ = ["Terminal"]
