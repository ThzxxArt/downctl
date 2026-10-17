"""Unit tests for the SFTP transport/source helpers (no real server)."""

from __future__ import annotations

import pytest

from downctl.builtin.transports._sftp import SftpTarget, connect, host_key, parse_url
from downctl.core.errors import DownctlError


# --- parse_url ------------------------------------------------------------ #
def test_parse_url_full() -> None:
    t = parse_url("sftp://u:p@host:2222/dir/f.bin", {})
    assert (t.host, t.port, t.user, t.password, t.path) == ("host", 2222, "u", "p", "/dir/f.bin")


def test_parse_url_from_options() -> None:
    t = parse_url("sftp://host/f.bin", {"user": "op", "password": "pw", "port": 2200})
    assert (t.user, t.password, t.port) == ("op", "pw", 2200)


def test_parse_url_bad_scheme() -> None:
    with pytest.raises(DownctlError, match="not an sftp URL"):
        parse_url("http://h/f", {})


def test_parse_url_missing_host() -> None:
    with pytest.raises(DownctlError, match="missing host"):
        parse_url("sftp:///f", {})


def test_parse_url_bad_port_option() -> None:
    with pytest.raises(DownctlError, match="port must be an integer"):
        parse_url("sftp://host/f", {"port": "not-a-port"})


def test_host_key_excludes_secret() -> None:
    t = SftpTarget("h", 22, "u", "sekret", "/x")
    assert host_key(t) == "u@h:22"


# --- connect -------------------------------------------------------------- #
async def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    import asyncssh

    captured: dict[str, object] = {}

    async def fake_connect(**kwargs: object) -> str:
        captured.update(kwargs)
        return "conn"

    monkeypatch.setattr(asyncssh, "connect", fake_connect)
    return captured


async def test_connect_disables_known_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _capture(monkeypatch)
    await connect(SftpTarget("h", 22, "u", "p", "/x"), {"known_hosts": ""})
    assert captured["known_hosts"] is None
    assert captured["username"] == "u"
    assert captured["password"] == "p"


async def test_connect_known_hosts_none_word(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _capture(monkeypatch)
    await connect(SftpTarget("h", 22, "u", "", "/x"), {"known_hosts": "none"})
    assert captured["known_hosts"] is None


async def test_connect_known_hosts_path(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _capture(monkeypatch)
    monkeypatch.setenv("HOME", "/home/tester")
    await connect(SftpTarget("h", 22, "u", "", "/x"), {"known_hosts": "~/kh"})
    assert captured["known_hosts"] == "/home/tester/kh"


async def test_connect_default_known_hosts_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _capture(monkeypatch)
    monkeypatch.setattr("os.path.isfile", lambda _p: True)
    await connect(SftpTarget("h", 22, "u", "", "/x"), {})
    assert str(captured["known_hosts"]).endswith("known_hosts")


async def test_connect_bypass_when_no_known_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _capture(monkeypatch)
    monkeypatch.setattr("os.path.isfile", lambda _p: False)
    await connect(SftpTarget("h", 22, "u", "", "/x"), {})
    assert captured["known_hosts"] is None


async def test_connect_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = await _capture(monkeypatch)
    monkeypatch.setenv("HOME", "/home/tester")
    await connect(
        SftpTarget("h", 22, "u", "", "/x"),
        {"known_hosts": "", "key": "~/.ssh/id_ed25519", "key_passphrase": "secret"},
    )
    assert captured["client_keys"] == ["/home/tester/.ssh/id_ed25519"]
    assert captured["passphrase"] == "secret"


async def test_connect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncssh

    async def boom(**_kwargs: object) -> str:
        raise OSError("refused")

    monkeypatch.setattr(asyncssh, "connect", boom)
    with pytest.raises(DownctlError, match="connection to h failed"):
        await connect(SftpTarget("h", 22, "u", "", "/x"), {"known_hosts": ""})
