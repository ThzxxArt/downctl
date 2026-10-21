"""Downloading an object the server stores with ``Content-Encoding``.

An object can be *stored* compressed, and a server then sets
``Content-Encoding: gzip`` on the answer regardless of what ``Accept-Encoding``
asked for. ``Content-Length`` describes the stored representation, and every
offset in the piece ledger is an offset into those stored bytes.

aiohttp decompresses such a response transparently unless told not to, which
created two failures at once: the body no longer had the announced length, and a
range request returned a slice of a gzip stream that cannot be inflated at all.
Both are invisible in the headers, so the download failed with a length mismatch
that pointed at the server.
"""

from __future__ import annotations

import argparse
import hashlib

from downctl import app
from downctl.probe import probe_metadata


def _get_args(target: str, destination) -> argparse.Namespace:
    args = app._default_get_args()
    args.targets = [target]
    args.dir = str(destination)
    args.reporter = "quiet"
    return args


async def test_an_object_stored_with_content_encoding_downloads_intact(
    file_server, tmp_path
) -> None:
    """The saved bytes are exactly the bytes that were served."""
    destination = tmp_path / "out"

    assert await app._get(_get_args(file_server.url("encoded"), destination)) == 0

    saved = (destination / "encoded").read_bytes()
    assert saved == file_server.state.encoded
    assert (
        hashlib.sha256(saved).hexdigest() == hashlib.sha256(file_server.state.encoded).hexdigest()
    )


async def test_the_probe_reports_the_stored_length(file_server) -> None:
    """A decoded length would disagree with every range the transfer asks for."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        result = await probe_metadata(session, file_server.url("encoded"))

    assert result.ok, result.error
    assert result.size == len(file_server.state.encoded)
