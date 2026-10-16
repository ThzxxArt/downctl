"""Built-in plugins: transports."""

from __future__ import annotations

from downctl.builtin.transports.file_transport import FileTransport
from downctl.builtin.transports.http_transport import HTTPTransport
from downctl.builtin.transports.sftp_transport import SFTPTransport

__all__ = ["HTTPTransport", "SFTPTransport", "FileTransport"]
