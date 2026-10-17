"""The mirror router plugin.

Rewrites a URL's prefix according to a ``FROM=TO`` mapping (e.g.
``https://huggingface.co= https://hf-mirror.com``). Unmapped URLs pass
through unchanged.
"""

from __future__ import annotations

from downctl import console
from downctl.plugin.protocols import API_VERSION


class MirrorRouter:
    """Rewrite URL prefixes to route through mirrors."""

    name = "mirror"
    api_version = API_VERSION
    priority = 0

    def __init__(self, mappings: dict[str, str] | None = None) -> None:
        self.mappings = mappings or {}

    def rewrite(self, url: str) -> str:
        lowered = url.lower()
        for source, dest in self.mappings.items():
            if not source:
                continue
            base = source.rstrip("/")
            if not lowered.startswith(base.lower()):
                continue
            rest = url[len(base) :]
            # Require a host/path boundary so "https://a.com" does not match
            # "https://a.com.evil.com".
            if rest != "" and rest[0] not in "/?#":
                continue
            target = dest.rstrip("/")
            if not target or "://" not in target:
                console.warn(f"mirror: ignoring invalid destination {dest!r}")
                continue
            return target + rest
        return url
