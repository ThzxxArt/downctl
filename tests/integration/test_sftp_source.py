"""Integration tests for the SFTP source/transport against a local server."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest
import pytest_asyncio

from downctl import app
from downctl.builtin.sources.sftp_source import SFTPSource
from downctl.config import Config, CoreConfig
from downctl.core.context import RuntimeContext

asyncssh = pytest.importorskip("asyncssh")

FILE_A = b"A" * 100
FILE_B = b"B" * 5000


class _SSHServer(asyncssh.SSHServer):  # type: ignore[misc]
    def begin_auth(self, username: str) -> bool:
        return False


class _SFTPServer(asyncssh.SFTPServer):  # type: ignore[misc]
    def __init__(self, chan: object, root: str) -> None:
        super().__init__(chan, chroot=root)


@pytest_asyncio.fixture
async def sftp_root(tmp_path: Path) -> str:
    root = tmp_path / "srv"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_bytes(FILE_A)
    (root / "sub" / "b.bin").write_bytes(FILE_B)
    return str(root)


@pytest_asyncio.fixture
async def sftp_server(sftp_root: str):
    key = asyncssh.generate_private_key("ssh-ed25519")

    def factory(chan: object):
        return _SFTPServer(chan, sftp_root)

    server = await asyncssh.create_server(
        _SSHServer,
        "127.0.0.1",
        0,
        server_host_keys=[key],
        sftp_factory=factory,
        encoding=None,
    )
    port = server.get_port()
    try:
        yield port
    finally:
        server.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(server.wait_closed(), timeout=5)


def _ctx(session, tmp_path: Path) -> RuntimeContext:
    return RuntimeContext(
        session=session,
        config=Config(core=CoreConfig(dir=str(tmp_path))),
        options={"sftp": {"known_hosts": ""}},  # disable host-key check for the test server
    )


@pytest_asyncio.fixture
async def session():
    import aiohttp

    async with aiohttp.ClientSession() as client:
        yield client


async def test_resolve_single_file(sftp_server: int, session, tmp_path: Path) -> None:
    ctx = _ctx(session, tmp_path)
    tasks = await SFTPSource().resolve(f"sftp://127.0.0.1:{sftp_server}/a.txt", ctx)
    assert len(tasks) == 1
    assert tasks[0].target == "a.txt"
    assert tasks[0].size == len(FILE_A)
    assert tasks[0].transport == "sftp"
    assert "127.0.0.1" in tasks[0].url


async def test_resolve_directory(sftp_server: int, session, tmp_path: Path) -> None:
    ctx = _ctx(session, tmp_path)
    tasks = await SFTPSource().resolve(f"sftp://127.0.0.1:{sftp_server}/sub/", ctx)
    assert [t.target for t in tasks] == ["b.bin"]
    assert tasks[0].size == len(FILE_B)


async def test_end_to_end_download(
    sftp_server: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(app, "detect_wsl_proxy", lambda proxy: proxy)
    cfg = tmp_path / "sftp.toml"
    cfg.write_text('[plugins.sftp]\nknown_hosts = ""\n', encoding="utf-8")
    monkeypatch.setenv("DOWNCTL_CONFIG", str(cfg))

    destination = tmp_path / "dl"
    args = app._default_get_args()
    args.targets = [f"sftp://127.0.0.1:{sftp_server}/a.txt"]
    args.dir = str(destination)
    args.reporter = "quiet"

    assert await app._get(args) == 0
    assert (destination / "a.txt").read_bytes() == FILE_A
