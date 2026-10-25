"""Terminal progress rendering for downctl.

The package is split so each part can be reasoned about (and tested) alone:

``text``
    Cell-accurate width, truncation, bars and human formats.
``model``
    The observable run/task state: bytes, rates, ETA, phases, retries.
``render``
    A pure ``RunView -> list[str]`` function.
``terminal``
    The only part that owns a stream, cursor movement and interleaving.
"""

from __future__ import annotations

from downctl.progress.model import Rate, RunView, TaskView
from downctl.progress.render import RenderOptions, render
from downctl.progress.terminal import Terminal
from downctl.progress.text import display_width

__all__ = [
    "Rate",
    "RenderOptions",
    "RunView",
    "TaskView",
    "Terminal",
    "display_width",
    "render",
]
