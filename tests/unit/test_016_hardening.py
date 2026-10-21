"""Regression tests for the 0.1.6 hardening fixes.

Each test pins a decision that used to have a second owner: the origin a
credential belongs to, the URL a failed log may store, and whether a transport
error is worth retrying.
"""

from __future__ import annotations

from downctl import app
from downctl.builtin.transports import sftp_transport
from downctl.core.credentials import AuthMaterial
from downctl.core.errors import PermanentError, TransportError
from downctl.core.task import DownloadTask
from downctl.session import _needs_hop_control


def test_failed_log_never_stores_an_origin_token() -> None:
    task = DownloadTask(
        url="https://example.test/a.bin?sig=deadbeef",
        target="a.bin",
        origin="https://example.test/a.bin?token=SUPERSECRET",
        source="url",
    )
    line = app._fmt_url(task, "permanent")
    assert "SUPERSECRET" not in line
    assert "a.bin" in line


def test_a_user_header_forces_hop_control_but_range_does_not() -> None:
    # ``X-Api-Key`` has no aiohttp rule of its own, so the chain must be walked
    # here or a 302 hands it to the next host.
    assert _needs_hop_control(None, {}, None, {"X-Api-Key": "secret"}) is True
    # ``Range`` restates the HTTP request and is safe to forward; the fast path
    # stays available.
    assert _needs_hop_control(None, {}, None, {"Range": "bytes=0-99"}) is False


class _SftpError(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"sftp error {code}")
        self.code = code


def test_sftp_missing_file_is_permanent_not_transient() -> None:
    failure = sftp_transport._sftp_failure(_SftpError(2), "stat failed")
    assert isinstance(failure, PermanentError)


def test_sftp_connection_loss_stays_transient() -> None:
    failure = sftp_transport._sftp_failure(_SftpError(7), "connection lost")
    assert isinstance(failure, TransportError)
    assert not isinstance(failure, PermanentError)


def test_auth_check_reports_length_without_the_value() -> None:
    material = AuthMaterial(headers={"Authorization": "Bearer abcdef"}, source="env")
    assert app._material_length(material) == len("Bearer abcdef")
    basic = AuthMaterial(basic=("user", "pass"), source="netrc")
    assert app._material_length(basic) == len("user:pass")
    assert app._material_length(None) == 0
