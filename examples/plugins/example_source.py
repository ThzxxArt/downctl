"""Example third-party plugin.

Copy this file into ``./downctl_plugins/`` (or ``~/.config/downctl/plugins/``)
to load it, or package it with the entry-point declared in ``pyproject.toml``.

.. code-block:: toml

    [project.entry-points."downctl.sources"]
    example = "my_package.source:ExampleSource"
"""

from __future__ import annotations

from downctl.core.task import DownloadTask
from downctl.plugin.protocols import API_VERSION


class ExampleSource:
    """A toy source that handles ``example://`` targets."""

    name = "example"
    api_version = API_VERSION
    priority = 0

    def can_handle(self, target: str) -> bool:
        return target.startswith("example://")

    async def resolve(self, target: str, ctx: object) -> list[DownloadTask]:
        name = target[len("example://") :]
        return [DownloadTask(url=f"https://example.com/{name}", target=name, source=self.name)]


def register(registry: object) -> None:
    """Called by the local-directory loader."""
    registry.register("source", ExampleSource(), origin="example_source")  # type: ignore[attr-defined]
