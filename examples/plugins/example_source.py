"""Example third-party plugin.

Copy this file into ``./downctl_plugins/`` (or ``~/.config/downctl/plugins/``)
and run a command with ``--local-plugins`` — ``downctl plugin list
--local-plugins`` or ``downctl get example://hello -d . --local-plugins``. The
flag belongs to the subcommand, not to ``downctl`` itself. Alternatively package
it with the entry-point declared below.

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
