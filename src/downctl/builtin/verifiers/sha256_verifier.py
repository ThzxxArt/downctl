"""The SHA-256 verifier plugin."""

from __future__ import annotations

import asyncio
import hashlib

from downctl.plugin.protocols import API_VERSION

READ_SIZE = 1 << 20


class SHA256Verifier:
    """Verify a file against an expected SHA-256 hex digest."""

    name = "sha256"
    api_version = API_VERSION
    priority = 0
    algo = "sha256"

    async def verify(self, path: str, expected_hex: str) -> bool:
        digest = await asyncio.to_thread(_sha256_of, path)
        return digest.lower() == expected_hex.lower()


def _sha256_of(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(READ_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
