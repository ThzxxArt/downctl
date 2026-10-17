"""downctl — a plugin-based universal command-line downloader.

Public API re-exports live here. The plugin contract is versioned via
``API_VERSION``; plugins declare the version they were built against.
"""

from __future__ import annotations

__version__ = "0.1.3"

API_VERSION = "1"

__all__ = ["__version__", "API_VERSION"]
