"""The live terminal progress reporter.

``live`` is the default on an interactive terminal. It keeps the run state in
:class:`~downctl.progress.model.RunView`, redraws it from a small asyncio
ticker (so the screen keeps moving while a transfer is stalled), and asks
:mod:`downctl.progress.render` for the lines. It never writes a byte of display
state itself, which is why a slow terminal cannot block a piece fetcher.

``plain`` is the same state model for a log: one periodic line instead of a
redrawn block, and one line when a file fails or is skipped. It is what an
explicit ``--progress plain`` selects and what a non-TTY fallback uses.

Both modes are exception-safe. A display is an observer of a download, never a
reason for one to fail: every public hook contains its own failure and turns
the display off (with one warning) instead of propagating.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable, Mapping
from typing import TextIO

from downctl import console
from downctl.core.errors import DownctlError
from downctl.core.task import DownloadTask
from downctl.plugin.protocols import API_VERSION
from downctl.progress import text as T
from downctl.progress.model import RunView, failure_label
from downctl.progress.render import RenderOptions, render
from downctl.progress.terminal import Terminal

#: Default redraw rate. Ten frames a second is smooth without spending a
#: measurable part of the event loop on terminal writes.
DEFAULT_FPS = 8
#: In plain mode one line every this many seconds, plus one per finished file.
DEFAULT_PLAIN_INTERVAL = 15


class LiveReporter:
    """Draw the run as a live block (or as periodic plain lines)."""

    name = "live"
    api_version = API_VERSION
    priority = 0
    #: Read by ``plugin info``/``_warn_unknown_plugin_options``. The values are
    #: defaults; types are validated in :meth:`configure`.
    OPTIONS = {
        "enabled": True,
        "fps": DEFAULT_FPS,
        "unicode": "auto",
        "color": "auto",
        "plain_interval": DEFAULT_PLAIN_INTERVAL,
    }

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.stream: TextIO = stream or sys.stderr
        self.view = RunView()
        self.fps = DEFAULT_FPS
        self.unicode_mode = "auto"
        self.color_mode = "auto"
        self.plain_interval = DEFAULT_PLAIN_INTERVAL
        self._terminal: Terminal | None = None
        self._ticker: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active = False
        self._last_draw = 0.0
        self._last_plain = 0.0
        self._closed = False
        self._warned = False
        self._default_plain = False
        self._plain = False
        self._clock = clock or time.monotonic

    # ------------------------------------------------------------------ #
    # configuration
    # ------------------------------------------------------------------ #
    def configure(self, options: Mapping[str, object]) -> None:
        """Apply ``[plugins.live]`` options, refusing a value that lies."""
        fps = options.get("fps")
        if fps is not None:
            if isinstance(fps, bool) or not isinstance(fps, int) or not 1 <= fps <= 30:
                raise DownctlError(
                    f"[plugins.{self.name}] fps must be an integer 1..30, got {fps!r}"
                )
            self.fps = fps
        unicode_mode = options.get("unicode")
        if unicode_mode is not None:
            if unicode_mode not in ("auto", "always", "never"):
                raise DownctlError(
                    f"[plugins.{self.name}] unicode must be auto/always/never, got {unicode_mode!r}"
                )
            self.unicode_mode = str(unicode_mode)
        color_mode = options.get("color")
        if color_mode is not None:
            if color_mode not in ("auto", "always", "never"):
                raise DownctlError(
                    f"[plugins.{self.name}] color must be auto/always/never, got {color_mode!r}"
                )
            self.color_mode = str(color_mode)
        interval = options.get("plain_interval")
        if interval is not None:
            if isinstance(interval, bool) or not isinstance(interval, int) or interval < 1:
                raise DownctlError(
                    f"[plugins.{self.name}] plain_interval must be a positive integer, got {interval!r}"
                )
            self.plain_interval = interval
        # A reconfigured reporter must use the new settings on its next run.
        self._drop_terminal()

    @property
    def mode(self) -> str:
        return "plain" if self._plain else "live"

    def _unicode(self) -> bool:
        if self.unicode_mode == "always":
            return True
        if self.unicode_mode == "never":
            return False
        encoding = (getattr(self.stream, "encoding", "") or "").lower()
        return "utf" in encoding.replace("-", "")

    def _color(self) -> bool:
        if self.color_mode == "always":
            return True
        if self.color_mode == "never":
            return False
        # ``console.color_enabled`` is the project's single answer for
        # NO_COLOR/FORCE_COLOR/TTY, and honours ``set_color`` in tests. A
        # second copy here disagreed with it (and ignored a forced value).
        return console.color_enabled()

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def run_started(
        self,
        total_tasks: int,
        total_bytes: int,
        jobs: int = 1,
        *,
        job: str = "",
        unknown: int = 0,
        connections: int = 0,
    ) -> None:
        try:
            self._stop_ticker()
            # Reuse the block the resolve/probe phase already created. Closing
            # and reopening it here showed the cursor for one frame and made
            # the first download frame flicker.
            now = self._clock()
            self.view.start(
                total_tasks,
                total_bytes,
                jobs,
                job=job,
                unknown=unknown,
                now=now,
            )
            self._active = True
            self._closed = False
            self._plain = self._default_plain or not self._live_capable()
            if self._plain:
                self._plain_line(force=True)
            self._ensure_ticker()
            if not self._plain:
                self._draw(force=True)
        except Exception as exc:  # noqa: BLE001 - a display must not fail a run
            self._disable(exc)

    def run_resumed(self, done_bytes: int) -> None:
        self.view.run_resumed(done_bytes, self._clock())
        self._maybe_draw()

    def task_started(self, task: DownloadTask) -> None:
        try:
            self.view.task_started(task, self._clock())
            self._maybe_draw()
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)

    def task_resumed(self, task: DownloadTask, done_bytes: int) -> None:
        try:
            self.view.task_resumed(task, done_bytes, self._clock())
            self._maybe_draw()
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)

    def task_progress(self, task: DownloadTask, delta: int) -> None:
        """The hot path: update numbers, never draw from here.

        Drawing on every 64 KiB chunk was how the old tqdm reporter spent a
        measurable slice of the event loop on the terminal. The ticker owns
        the frame rate; this method is a few integer additions.
        """
        try:
            self.view.task_progress(task, delta, self._clock())
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)

    def task_finished(self, task: DownloadTask, ok: bool) -> None:
        try:
            self.view.task_finished(task, ok, self._clock())
            if self._plain:
                self._plain_task(task, ok)
            else:
                self._maybe_draw()
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)

    def run_finished(
        self,
        succeeded: int,
        failed: int,
        *,
        job: str = "",
        hooks_failed: int = 0,
    ) -> None:
        try:
            self._stop_ticker()
            self.view.finish(self._clock())
            if self._plain:
                self._plain_line(force=True)
            self._close_terminal()
            self._active = False
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)

    # ------------------------------------------------------------------ #
    # optional presentation events
    # ------------------------------------------------------------------ #
    def phase(self, name: str, done: int = 0, total: int = 0) -> None:
        """A pre-download stage (resolving, probing)."""
        try:
            if not self._active or self.view.finished_at is not None:
                if name not in ("resolve", "probe"):
                    return
                self._begin_phase(name, done, total)
                return
            self.view.set_phase(name, done, total)
            self._maybe_draw(force=True)
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)

    def task_phase(self, task: DownloadTask, phase: str, detail: str = "") -> None:
        try:
            self.view.task_phase(task, phase, self._clock(), detail)
            self._maybe_draw()
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)

    def task_retrying(
        self, task: DownloadTask, attempt: int, delay: float, reason: str = ""
    ) -> None:
        try:
            self.view.task_retrying(task, attempt, delay, reason, self._clock())
            self._maybe_draw(force=True)
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)

    def task_restarted(self, task: DownloadTask) -> None:
        try:
            self.view.task_restarted(task, self._clock())
        except Exception as exc:  # noqa: BLE001
            self._disable(exc)

    def failure(self, _target: str, _error: str = "", _url: str = "", *, reason: str = "") -> None:
        """Resolve/probe failures are already printed by the app; no row."""

    def close(self) -> None:
        """Erase the block and restore the cursor; safe to call twice."""
        self._stop_ticker()
        self._close_terminal()
        self._active = False
        self._closed = True

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #
    def _begin_phase(self, name: str, done: int, total: int) -> None:
        now = self._clock()
        self._stop_ticker()
        self.view = RunView()
        self.view.started_at = now
        self.view.set_phase(name, done, total)
        self._active = True
        self._closed = False
        self._plain = self._default_plain or not self._live_capable()
        if self._plain:
            self._plain_line(force=True)
        self._ensure_ticker()
        if not self._plain:
            self._draw(force=True)

    def _ensure_ticker(self) -> None:
        if self._ticker is not None:
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            # Synchronous caller (a unit test, a library user): fall back to
            # drawing from the callbacks, still throttled.
            return
        self._ticker = self._loop.create_task(self._tick())

    async def _tick(self) -> None:
        interval = 1.0 / max(1, self.fps)
        try:
            while self._active and not self._closed:
                await asyncio.sleep(interval)
                if self._plain:
                    self._plain_line()
                else:
                    self._draw()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - never fail a run from the clock
            self._disable(exc)

    def _stop_ticker(self) -> None:
        ticker = self._ticker
        self._ticker = None
        self._loop = None
        if ticker is not None and not ticker.done():
            ticker.cancel()

    def _live_capable(self) -> bool:
        return self._ensure_terminal().ansi

    def _ensure_terminal(self) -> Terminal:
        if self._terminal is None or self._closed:
            self._terminal = Terminal(self.stream)
        return self._terminal

    def _drop_terminal(self) -> None:
        if self._terminal is not None:
            self._terminal.close()
            self._terminal = None
        self._closed = False

    def _close_terminal(self) -> None:
        if self._terminal is not None:
            self._terminal.close()
            self._terminal = None
        console.detach_live()

    def _draw(self, *, force: bool = False) -> None:
        if not self._active or self._closed or self._plain:
            return
        now = self._clock()
        if not force and now - self._last_draw < 1.0 / max(1, self.fps):
            return
        terminal = self._ensure_terminal()
        if not terminal.ansi:
            self._plain = True
            return
        width, height = terminal.measure()
        lines = render(
            self.view,
            RenderOptions(
                width=width,
                height=height,
                color=self._color(),
                unicode=self._unicode(),
                now=now,
            ),
        )
        terminal.write_frame(lines)
        self._last_draw = now
        if console.get_level() >= console.NORMAL:
            console.attach_live(terminal)

    def _maybe_draw(self, *, force: bool = False) -> None:
        if self._ticker is None and not self._plain:
            self._draw(force=force)

    def _plain_line(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last_plain < self.plain_interval:
            return
        if self.view.phase in ("resolve", "probe") and not self.view.tasks:
            if self.view.phase == "probe" and self.view.phase_total:
                text = f"preparing {self.view.phase_done}/{self.view.phase_total} targets"
            else:
                text = "resolving targets"
            self._write_plain(f"downctl: {text}")
            self._last_plain = now
            return
        done, failed, skipped, active, queued = self.view.counts()
        fraction = self.view.fraction()
        percent = T.format_percent(fraction).strip()
        total = T.human_size(self.view.known_done())
        if self.view.total_bytes:
            total += f"/{T.human_size(self.view.total_bytes)}"
        rate = T.format_rate(self.view.rate(now))
        eta = self.view.eta(now)
        parts = [
            percent,
            total,
            rate,
            f"ETA {T.format_eta(eta)}" if eta is not None else "ETA --:--",
            f"{done + skipped}/{self.view.total_tasks} done",
        ]
        if active:
            parts.append(f"{active} active")
        if queued:
            parts.append(f"{queued} queued")
        if failed:
            parts.append(f"{failed} failed")
        self._write_plain("downctl: " + "  ".join(parts))
        self._last_plain = now

    def _plain_task(self, task: DownloadTask, ok: bool) -> None:
        target = task.target
        self._last_plain = 0.0  # a completed file is worth an immediate line
        if ok:
            if task.size > 0 and self.view.tasks.get(id(task), None) is not None:
                view = self.view.tasks[id(task)]
                if view.state == "skipped":
                    self._write_plain(f"downctl: skipped {target} (already complete)")
            return
        reason = str(task.meta.get("failure") or "")
        self._write_plain(f"downctl: failed {target} ({failure_label(reason)})")

    def _write_plain(self, line: str) -> None:
        try:
            print(line, file=self.stream, flush=True)
        except (BrokenPipeError, OSError, ValueError):
            self._plain = True

    def _disable(self, exc: BaseException) -> None:
        self._stop_ticker()
        self._close_terminal()
        self._active = False
        self._closed = True
        if not self._warned:
            self._warned = True
            console.warn(
                f"progress display stopped: {type(exc).__name__}: {exc}; the download continues"
            )


class PlainReporter(LiveReporter):
    """The same state model, rendered as periodic log lines."""

    name = "plain"
    priority = 0

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(stream, clock=clock)
        self._default_plain = True
        self._plain = True


__all__ = ["DEFAULT_FPS", "DEFAULT_PLAIN_INTERVAL", "LiveReporter", "PlainReporter"]
