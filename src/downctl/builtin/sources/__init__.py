"""Built-in plugins: sources."""

from __future__ import annotations

from downctl.builtin.sources.huggingface_source import HuggingFaceSource
from downctl.builtin.sources.url_source import URLSource

__all__ = ["URLSource", "HuggingFaceSource"]
