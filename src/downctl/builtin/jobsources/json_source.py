"""The JSON job-file source.

Reads the same schema as the TOML source, in the form programmatic writers
produce::

    {
      "version": 1,
      "jobs": [
        {
          "name": "tool",
          "targets": ["https://example.com/tool.tar.gz"],
          "checksum_manifest": "checksums.txt",
          "hooks": ["archive"]
        }
      ]
    }

``"job"`` is accepted as an alias for the ``"jobs"`` array (a single-key
mistake should be understood, but giving both is ambiguous and refused). Job
validation, option aliases, strict types and relative-path resolution are
shared with the TOML source through
:func:`downctl.builtin.jobsources.toml_source._read_jobs`, so the two formats
cannot disagree about what a valid job is.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from downctl.builtin.jobsources.toml_source import _matches_format, _read_jobs
from downctl.core.errors import DownctlError
from downctl.core.job import Job
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext


class JsonJobSource:
    """Read JSON batch files into the jobs they describe."""

    name = "json"
    api_version = API_VERSION
    priority = 0
    formats = ("json",)

    def can_handle(self, path: str) -> bool:
        return _matches_format(path, self.formats)

    async def load(self, path: str, ctx: RuntimeContext) -> list[Job]:
        try:
            with open(path, encoding="utf-8-sig") as handle:
                document = json.load(handle)
        except OSError as exc:
            raise DownctlError(f"cannot read job file {path}: {exc}") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DownctlError(f"{path}: invalid JSON: {exc}") from exc
        return _read_jobs(document, path, kind="JSON")
