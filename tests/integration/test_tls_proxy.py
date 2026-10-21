"""TLS verification and proxy routing against hermetic local servers.

Two gaps motivated this module:

- downctl has no ``--insecure`` switch (verify the CLI: there is none), so the
  TLS contract is the one aiohttp's default context gives us: a self-signed
  certificate must be rejected. A second case trusts the test CA explicitly and
  downloads through the real engine, proving the HTTPS path itself works rather
  than only that the rejection path fires.
- the proxy policy is a *per-target* decision, and the unit tests only assert
  what a fake session was told. These tests put a real HTTP proxy on
  ``127.0.0.1`` and observe whether bytes actually travel through it: a local
  target must bypass an externally-scoped proxy, and an explicitly configured
  proxy must carry both the probe and the transfer (a ``CONNECT`` for HTTPS).

Everything listens on ``127.0.0.1``; no external network is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import re
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from downctl.builtin.reporters.quiet_reporter import QuietReporter
from downctl.config import Config, CoreConfig
from downctl.core.context import RuntimeContext
from downctl.core.engine import download_task
from downctl.core.task import DownloadTask
from downctl.plugin.loader import discover
from downctl.session import PROXY_ALL, PROXY_EXTERNAL, ProxyPolicy, make_session, policy_request

RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


# --------------------------------------------------------------------------- #
# local file server, plain and TLS
# --------------------------------------------------------------------------- #
def _file_app(data: bytes, hits: list[str]) -> web.Application:
    """A range-capable object with an ETag, recording every method it served."""

    async def handle(request: web.Request) -> web.StreamResponse:
        hits.append(request.method)
        headers = {"ETag": '"tls-v1"', "Accept-Ranges": "bytes"}
        if request.method == "HEAD":
            return web.Response(body=data, headers=headers)
        header = request.headers.get("Range")
        if not header:
            return web.Response(body=data, headers=headers)
        parsed = RANGE_RE.fullmatch(header.strip())
        if parsed is None:
            return web.Response(status=400, text="malformed Range header")
        start = int(parsed.group(1))
        end = int(parsed.group(2)) if parsed.group(2) else len(data) - 1
        end = min(end, len(data) - 1)
        if start >= len(data):
            return web.Response(
                status=416, headers={**headers, "Content-Range": f"bytes */{len(data)}"}
            )
        return web.Response(
            status=206,
            body=data[start : end + 1],
            headers={**headers, "Content-Range": f"bytes {start}-{end}/{len(data)}"},
        )

    app = web.Application()
    app.router.add_route("*", "/file", handle)
    return app


def _write_self_signed_cert(tmp_path: Path) -> tuple[Path, Path]:
    """Create a certificate for ``127.0.0.1`` that no system store trusts."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "self-signed.pem"
    key_path = tmp_path / "self-signed.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest_asyncio.fixture
async def plain_server():
    data = os.urandom(256 * 1024 + 17)
    hits: list[str] = []
    runner = web.AppRunner(_file_app(data, hits), shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"http://127.0.0.1:{port}/file", data, hits
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def tls_server(tmp_path: Path):
    data = os.urandom(256 * 1024 + 17)
    hits: list[str] = []
    cert_path, key_path = _write_self_signed_cert(tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_path, key_path)

    runner = web.AppRunner(_file_app(data, hits), shutdown_timeout=0.5)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=server_ctx)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"https://127.0.0.1:{port}/file", data, cert_path, hits
    finally:
        await runner.cleanup()


# --------------------------------------------------------------------------- #
# a real HTTP proxy, implemented with asyncio
# --------------------------------------------------------------------------- #
class _ProxyServer:
    """A minimal forward proxy that records what it is asked to reach.

    Plain HTTP arrives as an absolute-form request line; HTTPS arrives as
    ``CONNECT host:port``. Both are recorded, so a test can assert "nothing was
    sent through the proxy" without relying on a fake session's bookkeeping.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    @property
    def url(self) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._connections):
            task.cancel()
        await asyncio.gather(*self._connections, return_exceptions=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            method, target, version = request_line.decode("latin-1").strip().split(" ", 2)
            header_lines: list[bytes] = []
            while True:
                line = await reader.readline()
                if line in (b"", b"\r\n", b"\n"):
                    break
                header_lines.append(line)

            if method.upper() == "CONNECT":
                self.requests.append(("CONNECT", target))
                host, _, port_text = target.rpartition(":")
                upstream_reader, upstream_writer = await asyncio.open_connection(
                    host, int(port_text)
                )
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await self._relay_both(reader, writer, upstream_reader, upstream_writer)
                return

            self.requests.append((method.upper(), target))
            parsed = urlsplit(target)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            upstream_writer.write(f"{method} {path} {version}\r\n".encode("latin-1"))
            saw_host = False
            for line in header_lines:
                name = line.split(b":", 1)[0].strip().lower()
                if name in (b"proxy-connection", b"proxy-authorization", b"connection"):
                    continue
                saw_host = saw_host or name == b"host"
                upstream_writer.write(line)
            if not saw_host:
                upstream_writer.write(f"Host: {host}:{port}\r\n".encode("latin-1"))
            # One request per upstream connection keeps framing unambiguous:
            # the response ends when the server closes, which is exactly what
            # the relay below waits for.
            upstream_writer.write(b"Connection: close\r\n\r\n")
            await upstream_writer.drain()
            await self._pipe(upstream_reader, writer)
        except (ConnectionError, OSError, ValueError, asyncio.IncompleteReadError):
            # A client that gave up mid-request is not a proxy failure; the
            # test asserts on what was recorded, not on liveness.
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if task is not None:
                self._connections.discard(task)

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while chunk := await reader.read(64 * 1024):
                writer.write(chunk)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def _relay_both(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        tasks = {
            asyncio.create_task(self._pipe(client_reader, upstream_writer)),
            asyncio.create_task(self._pipe(upstream_reader, client_writer)),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)


@pytest_asyncio.fixture
async def proxy():
    server = _ProxyServer()
    await server.start()
    try:
        yield server
    finally:
        await server.close()


def _ctx(session: aiohttp.ClientSession, tmp_path: Path, policy: ProxyPolicy) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path))),
        registry=discover(),
        proxy_policy=policy,
    )


def _task(url: str, size: int) -> DownloadTask:
    return DownloadTask(url=url, target="object.bin", size=size)


async def _download(url: str, size: int, tmp_path: Path, policy: ProxyPolicy) -> bytes:
    async with make_session() as session:
        ctx = _ctx(session, tmp_path, policy)
        assert await download_task(
            ctx, _task(url, size), reporter=QuietReporter(), connections=2, stall_limit=5
        )
    return (tmp_path / "object.bin").read_bytes()


# --------------------------------------------------------------------------- #
# TLS: verification is on by default, but the HTTPS path is really used
# --------------------------------------------------------------------------- #
async def test_a_self_signed_certificate_is_rejected_by_the_default_session(
    tls_server,
) -> None:
    url, _data, _cert, _hits = tls_server
    async with make_session() as session:
        with pytest.raises(aiohttp.ClientConnectorCertificateError) as caught:
            async with policy_request(session, "GET", url):
                pass
    assert "certificate verify failed" in str(caught.value).lower()


async def test_the_engine_downloads_over_https_when_the_certificate_is_trusted(
    tls_server, tmp_path: Path
) -> None:
    url, data, cert_path, hits = tls_server
    client_ctx = ssl.create_default_context(cafile=str(cert_path))
    connector = aiohttp.TCPConnector(ssl=client_ctx)
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
        ctx = _ctx(session, tmp_path, ProxyPolicy())
        assert await download_task(
            ctx, _task(url, len(data)), reporter=QuietReporter(), connections=2, stall_limit=5
        )
    assert (tmp_path / "object.bin").read_bytes() == data
    assert "GET" in hits


# --------------------------------------------------------------------------- #
# proxy policy: the decision reaches the wire
# --------------------------------------------------------------------------- #
async def test_a_direct_policy_does_not_use_a_configured_proxy(
    plain_server, proxy: _ProxyServer, tmp_path: Path
) -> None:
    url, data, hits = plain_server
    assert await _download(url, len(data), tmp_path, ProxyPolicy()) == data
    assert proxy.requests == []
    assert "GET" in hits


async def test_an_externally_scoped_proxy_does_not_carry_a_local_target(
    plain_server, proxy: _ProxyServer, tmp_path: Path
) -> None:
    """The WSL-guess scope is a claim about reaching *other* machines."""
    url, data, hits = plain_server
    policy = ProxyPolicy(proxy.url, PROXY_EXTERNAL)
    assert await _download(url, len(data), tmp_path, policy) == data
    assert proxy.requests == []
    assert "GET" in hits


async def test_an_explicit_proxy_carries_the_plain_http_download(
    plain_server, proxy: _ProxyServer, tmp_path: Path
) -> None:
    url, data, _hits = plain_server
    policy = ProxyPolicy(proxy.url, PROXY_ALL)
    assert await _download(url, len(data), tmp_path, policy) == data

    assert ("GET", url) in proxy.requests, "the transfer bypassed the configured proxy"


async def test_an_explicit_proxy_tunnels_https_with_connect(
    tls_server, proxy: _ProxyServer, tmp_path: Path
) -> None:
    url, data, cert_path, _hits = tls_server
    tunnel_target = url.removeprefix("https://").split("/", 1)[0]
    client_ctx = ssl.create_default_context(cafile=str(cert_path))
    connector = aiohttp.TCPConnector(ssl=client_ctx)
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
        ctx = _ctx(session, tmp_path, ProxyPolicy(proxy.url, PROXY_ALL))
        assert await download_task(
            ctx, _task(url, len(data)), reporter=QuietReporter(), connections=2, stall_limit=5
        )
    assert (tmp_path / "object.bin").read_bytes() == data
    assert ("CONNECT", tunnel_target) in proxy.requests
