"""The SHA-256 verifier plugin."""

from __future__ import annotations

import asyncio

from downctl.digest import hash_file
from downctl.plugin.protocols import API_VERSION


class SHA256Verifier:
    """Verify a file against an expected SHA-256 hex digest."""

    name = "sha256"
    api_version = API_VERSION
    priority = 0
    algo = "sha256"

    async def verify(self, path: str, expected_hex: str) -> bool:
        digest = await asyncio.to_thread(hash_file, path, self.algo)
        return digest.lower() == expected_hex.lower()
