"""Turn :class:`~downctl.progress.model.RunView` into terminal lines.

This module is a pure function: the same view and width produce the same
lines. That is what makes a live terminal display testable without a
pseudo-terminal, and it keeps the reporter free to decide *when* to draw.

The layout follows three rules:

1. **One aggregate line, then the workers.** A single file gets a name line and
   one bar; several files get one run bar plus one row per working file. The
   old display drew an aggregate bar and a per-file bar showing the same
   numbers for a one-file run, and drew *only* the aggregate bar for a
   concurrent run, so the file that was slow or stuck was invisible.
2. **The row says what the file is doing.** ``downloading``, ``verifying`` and
   ``retrying in 5s`` are different states and must not all look like a
   stalled download.
3. **Missing information is shown as missing.** An unknown size has no honest
   percentage or ETA, so it prints ``--%`` and ``--:--``.

Every line is fitted to the requested width *before* colour is applied, so an
ANSI escape can never push a row into wrapping.
"""

from __future__ import annotations

from dataclasses import dataclass

from downctl.progress import text as T
from downctl.progress.model import RunView, TaskView, failure_label

#: The narrowest line we try to lay out. Below this a terminal is a log file
#: with a very small window; wrapping one short line is still better than a
#: layout made of ellipses.
MIN_WIDTH = 24
#: Fixed column widths (columns, not characters) used to budget a row.
_COL_BYTES = 17
_COL_RATE = 10
_COL_ETA = 9


@dataclass(frozen=True)
class RenderOptions:
    """Where and how to draw one frame."""

    width: int = 100
    height: int = 24
    color: bool = False
    unicode: bool = True
    now: float = 0.0


def _join(parts: list[str], separator: str = " · ") -> str:
    return separator.join(part for part in parts if part)


def _phase_word(view: RunView) -> str:
    return {
        "resolve": "resolving",
        "probe": "preparing",
        "download": "downloading",
        "finished": "done",
        "idle": "starting",
    }.get(view.phase, view.phase or "downloading")


def _header(view: RunView, options: RenderOptions) -> str:
    """The first line: what this run is doing, at a glance."""
    width = max(MIN_WIDTH, options.width)
    word = _phase_word(view)
    spinner = T.spinner(int(options.now * 8), unicode=options.unicode)
    parts = [f"{spinner} {word}"]
    if view.job:
        parts.append(f"job {view.job}")
    if view.phase == "probe" and view.phase_total:
        parts.append(f"{view.phase_done}/{view.phase_total} targets")
    elif view.total_tasks:
        parts.append(f"{view.total_tasks} file" + ("" if view.total_tasks == 1 else "s"))
    done = view.known_done()
    if view.total_bytes:
        parts.append(f"{T.human_size(done)} / {T.human_size(view.total_bytes)}")
    elif done:
        parts.append(T.human_size(done))
    rate = view.rate(options.now)
    if rate:
        parts.append(T.format_rate(rate))
    eta = view.eta(options.now)
    if eta is not None and view.remaining_bytes():
        parts.append(f"ETA {T.format_eta(eta)}")
    line = _join(parts)
    if T.display_width(line) <= width:
        return line
    # Drop the least important field first, never the phase word: fitting by
    # cutting the right edge would remove the part that says what is happening.
    for keep in (4, 3, 2, 1):
        line = _join(parts[:keep])
        if T.display_width(line) <= width:
            return line
    return T.ellipsize(parts[0], width, unicode=options.unicode)


def _counts(view: RunView) -> str:
    done, failed, skipped, active, queued = view.counts()
    parts = [f"{done}/{view.total_tasks} done"]
    if active:
        parts.append(f"{active} active")
    if queued:
        parts.append(f"{queued} queued")
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} skipped")
    return _join(parts)


def _bar_line(
    view: RunView,
    options: RenderOptions,
    fraction: float | None,
    extra: str,
) -> str:
    """The overall bar plus percentage/counts, fitted to the width."""
    width = max(MIN_WIDTH, options.width)
    percent = T.format_percent(fraction)
    if fraction is None:
        return T.fit(f"{percent}  {extra}", width, unicode=options.unicode)
    tail = f" {percent}  {extra}"
    available = width - T.display_width(tail) - 2
    bar_width = max(8, min(40, available))
    bar = T.make_bar(fraction, bar_width, unicode=options.unicode)
    line = f"[{bar.text}]{tail}"
    if T.display_width(line) <= width:
        return line
    # The counters can be long (failed + skipped + queued). Keep the
    # percentage and let the counters be cut; the summary still has them.
    bar_width = max(4, width - T.display_width(percent) - 4)
    bar = T.make_bar(fraction, bar_width, unicode=options.unicode)
    line = f"[{bar.text}] {percent}  {extra}"
    return T.ellipsize(line, width, unicode=options.unicode)


def _marker(task: TaskView, unicode: bool) -> str:
    if unicode:
        return {
            "active": "▶",
            "retrying": "↻",
            "verifying": "≡",
            "failed": "✗",
            "skipped": "✓",
            "done": "✓",
        }.get(task.state, "·")
    return {
        "active": ">",
        "retrying": "~",
        "verifying": "#",
        "failed": "x",
        "skipped": "v",
        "done": "v",
    }.get(task.state, ".")


def _detail(task: TaskView, now: float) -> str:
    """The state note that explains a wait, a hash or a failure."""
    if task.state == "retrying":
        note = f"retrying in {task.retry_seconds(now)}s"
        if task.attempt:
            note += f" (attempt {task.attempt}"
            if task.retry_reason:
                note += f", {task.retry_reason}"
            note += ")"
        return note
    if task.state == "verifying":
        return f"verifying {task.detail or 'content'}"
    if task.state == "failed":
        return failure_label(task.failure)
    if task.state == "skipped":
        return "skipped (already complete)"
    return ""


def _row(
    task: TaskView,
    name: str,
    width: int,
    *,
    now: float,
    unicode: bool,
) -> str:
    """One file line. Optional columns are chosen by available width."""
    has_bytes = width >= 56
    has_rate = width >= 70
    has_eta = width >= 84
    marker = _marker(task, unicode)
    percent = T.format_percent(task.fraction())
    fixed = 4 + 2 + 4  # marker + gap, gap, percentage
    if has_bytes:
        fixed += _COL_BYTES + 2
    if has_rate:
        fixed += _COL_RATE + 2
    if has_eta:
        fixed += _COL_ETA + 2
    name_width = max(10, width - fixed)
    name = T.shorten_path(name, name_width, unicode=unicode)
    detail = _detail(task, now)
    if detail:
        head = f"  {marker} {name}"
        facts = [detail]
        if task.size > 0:
            facts.append(f"{T.human_size(task.done())}/{T.human_size(task.size)}")
        facts.append(percent)
        available = width - T.display_width(head) - 2
        return head + "  " + T.ellipsize("  ".join(facts), max(0, available), unicode=unicode)
    parts = [f"  {marker} {name}"]
    if has_bytes:
        if task.size > 0:
            parts.append(f"{T.human_size(task.done())}/{T.human_size(task.size)}")
        else:
            parts.append(f"{T.human_size(task.done())}/?")
    parts.append(percent)
    if has_rate:
        parts.append(T.format_rate(task.rate.rate(now)))
    if has_eta:
        eta = task.eta(now)
        parts.append(f"ETA {T.format_eta(eta)}" if eta is not None else "ETA --:--")
    return T.ellipsize("  ".join(parts), width, unicode=unicode)


def _visible_rows(
    view: RunView,
    options: RenderOptions,
    budget: int,
) -> list[TaskView | str]:
    """Rows worth showing, most urgent first, limited by the window height."""
    tasks = list(view.tasks.values())
    if not tasks:
        return []
    urgent = [t for t in tasks if t.state in ("retrying", "verifying")]
    active = [t for t in tasks if t.state == "active"]
    failed = [t for t in tasks if t.state == "failed"]
    # Ordering inside a group is start order; a user watching several files
    # cares which one is stuck, not which dict key came first.
    active.sort(key=lambda item: item.started_at)
    failed.sort(key=lambda item: item.finished_at or item.started_at)
    candidates: list[TaskView | str] = [*urgent, *active, *failed]
    budget = max(1, budget)
    if len(candidates) <= budget:
        return candidates
    if budget <= 1:
        return candidates[:1]
    shown = candidates[: max(0, budget - 1)]
    hidden = len(candidates) - len(shown)
    if not shown:
        return [f"  … {hidden} more active" if options.unicode else f"  ... {hidden} more active"]
    suffix = f"  … {hidden} more active" if options.unicode else f"  ... {hidden} more active"
    return [*shown, suffix]


def _status_word(task: TaskView) -> str:
    if task.state == "retrying":
        return "retrying"
    if task.state == "verifying":
        return "verifying"
    if task.state == "failed":
        return "failed"
    if task.state == "skipped":
        return "skipped"
    return "downloading"


def _single_task_lines(
    view: RunView,
    task: TaskView | None,
    options: RenderOptions,
) -> list[str]:
    width = max(MIN_WIDTH, options.width)
    if task is None:
        # ``run_started`` arrives a moment before the first task does. An empty
        # 0 % bar for a file that has not begun is a lie about progress; say
        # what is actually true and let the first row replace it.
        spinner = T.spinner(int(options.now * 8), unicode=options.unicode)
        if view.total_tasks == 0:
            return [f"{spinner} nothing to download"]
        line = f"{spinner} downloading 1 file"
        if view.total_bytes:
            line += f" · {T.human_size(view.total_bytes)}"
        return [T.ellipsize(line, width, unicode=options.unicode)]
    else:
        status = _status_word(task)
        name = task.target.replace("\\", "/")
        fraction = task.fraction()
        done = task.done()
        rate = task.rate.rate(options.now)
        eta = task.eta(options.now)
        size = task.size
        detail = _detail(task, options.now)
        if task.state != "active":
            # A retry wait, a hash or a failure has no meaningful transfer
            # rate; showing the pre-wait rate next to "retrying" is a lie.
            rate = None
            eta = None
        if task.state == "retrying":
            detail = detail.removeprefix("retrying ")
        elif task.state == "verifying":
            detail = task.detail or "content"
        elif task.state == "failed":
            detail = task.failure or "failed"
        elif task.state == "skipped":
            detail = "already complete"
    name_width = max(8, width - T.display_width(status) - 1)
    headline = f"{status} {T.shorten_path(name, name_width, unicode=options.unicode)}"
    lines = [T.ellipsize(headline, width, unicode=options.unicode)]
    percent = T.format_percent(fraction)
    if fraction is None:
        details = [T.human_size(done)]
        if rate:
            details.append(T.format_rate(rate))
        details.append("size unknown")
        lines.append(T.fit(_join(details), width, unicode=options.unicode))
    else:
        tail = [percent]
        if size > 0:
            tail.append(f"{T.human_size(done)}/{T.human_size(size)}")
        if rate and width >= 52:
            tail.append(T.format_rate(rate))
        if eta is not None and width >= 64:
            tail.append(f"ETA {T.format_eta(eta)}")
        bar_width = max(8, min(40, width - T.display_width(_join(tail)) - 4))
        bar = T.make_bar(fraction, bar_width, unicode=options.unicode)
        lines.append(T.ellipsize(f"[{bar.text}] " + _join(tail), width, unicode=options.unicode))
    if detail:
        lines.append(T.ellipsize(f"  {detail}", width, unicode=options.unicode))
    return lines[: max(1, options.height)]


def _display_name(task: TaskView, view: RunView) -> str:
    """A path tail that is unambiguous inside this run.

    ``a/data.bin`` and ``b/data.bin`` are different files; showing both as
    ``data.bin`` would make a progress row actively misleading. Only the last
    two components are used when the basenames collide.
    """
    normalised = task.target.replace("\\", "/")
    basename = normalised.rsplit("/", 1)[-1]
    duplicates = sum(
        1
        for other in view.tasks.values()
        if other.target.replace("\\", "/").rsplit("/", 1)[-1] == basename
    )
    if duplicates <= 1:
        return basename
    parts = normalised.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else normalised


def _colour(lines: list[str], options: RenderOptions) -> list[str]:
    """Apply colour after layout, so escapes never change the width."""
    if not options.color:
        return lines
    coloured: list[str] = []
    for line in lines:
        style = ""
        if line.startswith(("  ✗", "  x ")):
            style = T.RED
        elif line.startswith(("  ↻", "  ~ ")):
            style = T.YELLOW
        elif line.startswith(("  ≡", "  # ")):
            style = T.MAGENTA
        elif line.startswith(("  ✓", "  v ")):
            style = T.GREEN
        if style:
            coloured.append(T.paint(line, style, True))
            continue
        # A bar is the one place where colour carries state: filled is
        # progress, empty is background. Everything else stays readable
        # without colour.
        start = line.find("[")
        end = line.find("]", start + 1) if start >= 0 else -1
        if start >= 0 and end > start:
            inner = line[start + 1 : end]
            if inner and set(inner) <= set("█▏▎▍▌▋▊▉░#-"):
                filled = "".join(ch for ch in inner if ch in "█▏▎▍▌▋▊▉#")
                empty = "".join(ch for ch in inner if ch in "░-")
                if filled:
                    inner = T.paint(filled, T.GREEN, True) + T.paint(empty, T.DIM, True)
                    line = line[: start + 1] + inner + line[end:]
        coloured.append(line)
    return coloured


def render(view: RunView, options: RenderOptions) -> list[str]:
    """Render one frame as plain text + colour, never wider than ``width``."""
    width = max(MIN_WIDTH, min(240, options.width))
    height = max(3, options.height)
    opts = RenderOptions(
        width=width,
        height=height,
        color=options.color,
        unicode=options.unicode,
        now=options.now,
    )
    if view.phase in ("resolve", "probe") and not view.tasks:
        spinner = T.spinner(int(opts.now * 8), unicode=opts.unicode)
        if view.phase == "probe" and view.phase_total:
            head = f"{spinner} preparing {view.phase_done}/{view.phase_total} targets"
            lines = [T.ellipsize(head, width, unicode=opts.unicode)]
            phase_fraction = view.phase_done / view.phase_total
            bar_width = max(8, min(40, width - 22))
            bar = T.make_bar(phase_fraction, bar_width, unicode=opts.unicode)
            lines.append(f"[{bar.text}] {T.format_percent(phase_fraction)}")
            return _colour(lines, opts)
        head = f"{spinner} {_phase_word(view)}"
        if view.job:
            head += f" · job {view.job}"
        return [T.ellipsize(head, width, unicode=opts.unicode)]

    if view.total_tasks <= 1:
        task = next(iter(view.tasks.values()), None)
        return _colour(_single_task_lines(view, task, opts), opts)

    header = _header(view, opts)
    fraction = view.fraction()
    bar_line = _bar_line(view, opts, fraction, _counts(view))
    budget = max(1, height - 2)
    rows = _visible_rows(view, opts, budget)
    rendered: list[str] = []
    for item in rows:
        if isinstance(item, str):
            rendered.append(T.ellipsize(item, width, unicode=opts.unicode))
        else:
            rendered.append(
                _row(
                    item,
                    _display_name(item, view),
                    width,
                    now=opts.now,
                    unicode=opts.unicode,
                )
            )
    return _colour([header, bar_line, *rendered], opts)


__all__ = ["RenderOptions", "render"]
