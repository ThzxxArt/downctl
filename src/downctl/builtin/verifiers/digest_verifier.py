"""Hash verifiers for SHA-1, SHA-512 and MD5 checksum files.

``--checksums`` accepts ordinary ``sha256sum``/``sha1sum``/``md5sum``-style
manifests, and the ``checksum`` hook can write any of these algorithms. Rather
than pretending only SHA-256 exists, each supported algorithm has a verifier
so a sidecar written by ``checksum`` can always be consumed later.

MD5/SHA-1 are **not** suitable against a malicious publisher; they exist for
compatibility with upstream checksum files. Prefer SHA-256 when available.
"""

from __future__ import annotations

import asyncio
import hashlib

from downctl.plugin.protocols import API_VERSION

READ_SIZE = 1 << 20


def _digest_of(path: str, algo: str) -> str:
    hasher = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(READ_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _verify(path: str, expected_hex: str, algo: str) -> bool:
    digest = _digest_of(path, algo)
    return digest.lower() == expected_hex.strip().lower()


class _HashVerifier:
    """Verify a file against a hex digest of one algorithm."""

    api_version = API_VERSION
    priority = 0
    algo = ""
    hex_length = 0

    async def verify(self, path: str, expected_hex: str) -> bool:
        if self.hex_length and len(expected_hex.strip()) != self.hex_length:
            return False
        return await asyncio.to_thread(_verify, path, expected_hex, self.algo)


class SHA1Verifier(_HashVerifier):
    """SHA-1 verifier (compatibility with ``sha1sum`` manifests)."""

    name = "sha1"
    algo = "sha1"
    hex_length = 40


class SHA512Verifier(_HashVerifier):
    """SHA-512 verifier (compatibility with ``sha512sum`` manifests)."""

    name = "sha512"
    algo = "sha512"
    hex_length = 128


class MD5Verifier(_HashVerifier):
    """MD5 verifier (compatibility with ``md5sum`` manifests)."""

    name = "md5"
    algo = "md5"
    hex_length = 32


__all__ = ["MD5Verifier", "SHA1Verifier", "SHA512Verifier"]
