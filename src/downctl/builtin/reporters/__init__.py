"""Built-in plugins: reporters."""

from __future__ import annotations

from downctl.builtin.reporters.json_reporter import JSONReporter
from downctl.builtin.reporters.quiet_reporter import QuietReporter
from downctl.builtin.reporters.tqdm_reporter import TqdmReporter

__all__ = ["TqdmReporter", "JSONReporter", "QuietReporter"]
