"""Tests for the rate limiter, mirror router and sha256 verifier."""

from __future__ import annotations

import hashlib
from pathlib import Path

from downctl.builtin.routers.mirror_router import MirrorRouter
from downctl.builtin.verifiers.sha256_verifier import SHA256Verifier
from downctl.core.ratelimit import RateLimiter


async def test_rate_limiter_disabled_is_noop() -> None:
    limiter = RateLimiter(None)
    await limiter.wait(1 << 20)


def test_mirror_router_rewrites_and_passes_through() -> None:
    router = MirrorRouter({"https://huggingface.co": "https://hf-mirror.com"})
    assert router.rewrite("https://huggingface.co/org/repo") == "https://hf-mirror.com/org/repo"
    assert router.rewrite("https://example.com/x") == "https://example.com/x"


async def test_sha256_verifier(tmp_path: Path) -> None:
    target = tmp_path / "data.bin"
    payload = b"downctl"
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    verifier = SHA256Verifier()
    assert verifier.algo == "sha256"
    assert await verifier.verify(str(target), digest)
    assert not await verifier.verify(str(target), "0" * 64)


def test_file_lock_roundtrip(tmp_path: Path) -> None:
    from downctl.core.locks import FileLock

    target = str(tmp_path / "a.bin")
    lock = FileLock(target)
    lock.acquire()
    assert lock.held
    lock.release()
    assert not lock.held

    with FileLock(target):
        pass
