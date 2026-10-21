"""Built-in job-file readers.

A ``JobSource`` turns a batch file into the
:class:`~downctl.core.job.Job` objects ``downctl batch`` runs. Two syntaxes of
one schema ship here: TOML (``.toml``/``.tml``) for hand-written files and
JSON for files produced by other programs.
"""

from __future__ import annotations

from downctl.builtin.jobsources.json_source import JsonJobSource
from downctl.builtin.jobsources.toml_source import TomlJobSource

__all__ = ["JsonJobSource", "TomlJobSource"]
