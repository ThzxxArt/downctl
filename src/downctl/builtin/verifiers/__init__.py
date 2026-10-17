"""Built-in plugins: verifiers."""

from __future__ import annotations

from downctl.builtin.verifiers.digest_verifier import MD5Verifier, SHA1Verifier, SHA512Verifier
from downctl.builtin.verifiers.sha256_verifier import SHA256Verifier

__all__ = ["MD5Verifier", "SHA1Verifier", "SHA256Verifier", "SHA512Verifier"]
