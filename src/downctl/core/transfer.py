"""Multi-connection resumable transfer of a single file.

Implementation lands in P2. The public surface below is stable; plugins never
call it directly — the engine does — but it is documented here so the contract
is explicit.

Contract:
- Uses the transport plugin to fetch byte ranges.
- Records completion in the piece bitmap; a retry only re-fetches missing
  pieces.
- Returns ``(complete, stalled)``; ``complete`` is authoritative and must not
  be inferred from file size by callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp

    from downctl.core.ratelimit import RateLimiter
    from downctl.plugin.protocols import Transport

DEFAULT_PIECE_SIZE = 8 * (1 << 20)  # 8 MiB


@dataclass
class TransferResult:
    complete: bool
    stalled: bool = False
    bytes_written: int = 0


async def transfer_file(
    session: aiohttp.ClientSession,
    transport: Transport,
    url: str,
    target: str,
    expected: int,
    *,
    connections: int = 16,
    headers: dict[str, str] | None = None,
    rate_limiter: RateLimiter | None = None,
    label: str = "",
    use_bar: bool = True,
) -> TransferResult:
    """Download ``url`` into ``target``, resuming from the piece bitmap."""
    raise NotImplementedError("core.transfer lands in P2")
