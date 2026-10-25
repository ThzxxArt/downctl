"""Small, dependency-free text primitives for the terminal progress display.

The progress renderer has to describe itself in *cells*, not characters: a
CJK filename occupies two columns, a combining mark occupies none, and an ANSI
escape occupies none but is several bytes. Everything in this module therefore
works on :func:`display_width`, and every line the renderer produces is fitted
to the terminal width before it is coloured.

Keeping this separate from :mod:`downctl.progress.render` means the width and
truncation rules can be tested without a terminal, and colour can be added to an
already-correct line instead of being mixed into the layout arithmetic.
"""

from __future__ import annotations

import re
import unicodedata

from downctl.platform_utils import human_size as human_size

#: ANSI SGR sequences (the only escapes this package emits).
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"

#: A partial block for each eighth of a cell, from 1/8 to 7/8.
_PARTIALS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")
_FULL = "█"
_EMPTY = "░"
_ASCII_FULL = "#"
_ASCII_EMPTY = "-"


def strip_ansi(text: str) -> str:
    """Remove the SGR escapes this package emits."""
    return _ANSI.sub("", text)


def char_width(character: str) -> int:
    """Columns occupied by one character (0, 1 or 2)."""
    if character == "\t":
        return 4
    if unicodedata.combining(character):
        return 0
    if ord(character) < 32 or ord(character) == 127:
        return 0
    return 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1


def display_width(text: str) -> int:
    """Columns ``text`` occupies once escapes are ignored."""
    return sum(char_width(character) for character in strip_ansi(text))


def paint(text: str, style: str, enabled: bool) -> str:
    """Wrap ``text`` in ``style`` when colour is enabled."""
    return f"{style}{text}{RESET}" if enabled and text else text


def ellipsis(unicode: bool = True) -> str:
    return "…" if unicode else "..."


def ellipsize(text: str, width: int, *, unicode: bool = True) -> str:
    """Truncate ``text`` to ``width`` columns, marking the cut."""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    marker = ellipsis(unicode)
    if display_width(marker) > width:
        return ""
    budget = width - display_width(marker)
    kept: list[str] = []
    used = 0
    for character in text:
        size = char_width(character)
        if used + size > budget:
            break
        kept.append(character)
        used += size
    return "".join(kept) + marker


def ellipsize_middle(text: str, width: int, *, unicode: bool = True) -> str:
    """Keep both ends of ``text`` (the extension survives when possible)."""
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    marker = ellipsis(unicode)
    if display_width(marker) >= width:
        return marker[:width]
    budget = width - display_width(marker)
    left_budget = (budget + 1) // 2
    right_budget = budget - left_budget
    # A Windows path is a path on any platform: normalise before measuring.
    text = text.replace("\\", "/")
    left = ""
    used = 0
    for character in text:
        size = char_width(character)
        if used + size > left_budget:
            break
        left += character
        used += size
    right = ""
    used = 0
    for character in reversed(text):
        size = char_width(character)
        if used + size > right_budget:
            break
        right = character + right
        used += size
    return left + marker + right


def shorten_path(path: str, width: int, *, unicode: bool = True) -> str:
    """Fit a path into ``width`` columns, preferring its basename.

    A progress row is about *which file* is moving, so the tail is the part
    worth keeping: ``/very/long/prefix/model.bin`` becomes
    ``…/prefix/model.bin`` and then ``model.bin``. Only when the basename itself
    is too wide is it cut in the middle, where the extension still survives.
    """
    normalised = path.replace("\\", "/")
    if display_width(normalised) <= width:
        return normalised
    name = normalised.rsplit("/", 1)[-1]
    marker = ellipsis(unicode)
    if display_width(name) + display_width(marker) + 1 <= width:
        return marker + "/" + name
    return ellipsize_middle(name, width, unicode=unicode)


def fit(text: str, width: int, *, align: str = "left", unicode: bool = True) -> str:
    """Truncate and pad ``text`` to exactly ``width`` columns."""
    if width <= 0:
        return ""
    clipped = ellipsize(text, width, unicode=unicode)
    padding = width - display_width(clipped)
    if align == "right":
        return " " * padding + clipped
    if align == "center":
        left = padding // 2
        return " " * left + clipped + " " * (padding - left)
    return clipped + " " * padding


def format_rate(rate: float | None) -> str:
    """Bytes per second, or ``--`` when there is no recent measurement."""
    if rate is None or rate <= 0:
        return "--"
    return f"{human_size(rate)}/s"


def format_eta(seconds: float | None) -> str:
    """A compact countdown (``00:12`` / ``1:02:03``), or ``--:--``."""
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds + 0.5)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_elapsed(seconds: float) -> str:
    """A human elapsed time for the non-live summary (``12s`` / ``1m 02s``)."""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}s"
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs:02d}s"


def format_percent(fraction: float | None) -> str:
    """A whole-percent string, or ``--%`` when the total is unknown."""
    if fraction is None:
        return "--%"
    return f"{int(max(0.0, min(1.0, fraction)) * 100):3d}%"


class Bar:
    """A rendered bar kept as two pieces so colour cannot change its width."""

    __slots__ = ("empty", "filled")

    def __init__(self, filled: str, empty: str) -> None:
        self.filled = filled
        self.empty = empty

    @property
    def text(self) -> str:
        return self.filled + self.empty

    def __len__(self) -> int:
        return display_width(self.text)


def make_bar(fraction: float | None, width: int, *, unicode: bool = True) -> Bar:
    """Build a bar of ``width`` cells.

    ``fraction is None`` (an unknown-size transfer) draws an empty bar rather
    than a full one: a bar that looks complete while bytes are still arriving
    is the one lie a progress display must not tell.
    """
    width = max(0, width)
    if width == 0:
        return Bar("", "")
    if fraction is None:
        return Bar("", (_EMPTY if unicode else _ASCII_EMPTY) * width)
    fraction = max(0.0, min(1.0, fraction))
    filled_cells = fraction * width
    whole = int(filled_cells)
    remainder = filled_cells - whole
    if unicode:
        filled = _FULL * whole
        if whole < width and remainder >= 0.125:
            index = min(7, int(remainder * 8))
            if index:
                filled += _PARTIALS[index]
        empty = _EMPTY * max(0, width - display_width(filled))
        return Bar(filled, empty)
    whole = min(width, int(fraction * width + 0.5))
    return Bar(_ASCII_FULL * whole, _ASCII_EMPTY * (width - whole))


def spinner(frame: int, *, unicode: bool = True) -> str:
    """A single-cell activity indicator; silence would look like a hang."""
    frames: tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    if not unicode:
        frames = ("|", "/", "-", "\\")
    return frames[frame % len(frames)]
