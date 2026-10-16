"""The HuggingFace source plugin.

Understands ``hf://<repo>[@<revision>]`` targets and expands a repository into
its file list via the Hub API (implemented in P4). Auth tokens and mirror
support are read from ``[plugins.huggingface]`` through the runtime context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from downctl.core.task import DownloadTask
from downctl.plugin.protocols import API_VERSION

if TYPE_CHECKING:
    from downctl.core.context import RuntimeContext

SCHEME = "hf://"


class HuggingFaceSource:
    """Expand a HuggingFace repository into download tasks."""

    name = "huggingface"
    api_version = API_VERSION
    priority = 10

    def can_handle(self, target: str) -> bool:
        return target.startswith(SCHEME)

    async def resolve(self, target: str, ctx: RuntimeContext) -> list[DownloadTask]:
        raise NotImplementedError("huggingface source lands in P4")
