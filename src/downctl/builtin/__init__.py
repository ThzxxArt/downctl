"""Built-in plugins.

These ship with downctl but use the exact same plugin interface as
third-party plugins, so any of them can be shadowed or replaced by name.
"""

from __future__ import annotations

from downctl.builtin.hooks.archive_hook import ArchiveHook
from downctl.builtin.hooks.checksum_hook import ChecksumHook
from downctl.builtin.hooks.exec_hook import ExecHook
from downctl.builtin.hooks.logfile_hook import LogfileHook
from downctl.builtin.hooks.manifest_hook import ManifestHook
from downctl.builtin.hooks.webhook_hook import WebhookHook
from downctl.builtin.reporters.json_reporter import JSONReporter
from downctl.builtin.reporters.quiet_reporter import QuietReporter
from downctl.builtin.reporters.tqdm_reporter import TqdmReporter
from downctl.builtin.routers.mirror_router import MirrorRouter
from downctl.builtin.sources.file_source import FileSource
from downctl.builtin.sources.github_source import GitHubSource
from downctl.builtin.sources.huggingface_source import HuggingFaceSource
from downctl.builtin.sources.s3_source import S3Source
from downctl.builtin.sources.sftp_source import SFTPSource
from downctl.builtin.sources.url_source import URLSource
from downctl.builtin.transports.file_transport import FileTransport
from downctl.builtin.transports.http_transport import HTTPTransport
from downctl.builtin.transports.sftp_transport import SFTPTransport
from downctl.builtin.verifiers.digest_verifier import MD5Verifier, SHA1Verifier, SHA512Verifier
from downctl.builtin.verifiers.sha256_verifier import SHA256Verifier

#: category -> list of plugin classes
BUILTIN_PLUGINS: dict[str, list[type]] = {
    "source": [URLSource, HuggingFaceSource, GitHubSource, S3Source, SFTPSource, FileSource],
    "transport": [HTTPTransport, SFTPTransport, FileTransport],
    "verifier": [SHA256Verifier, SHA1Verifier, SHA512Verifier, MD5Verifier],
    "reporter": [TqdmReporter, JSONReporter, QuietReporter],
    "router": [MirrorRouter],
    "hook": [LogfileHook, ArchiveHook, WebhookHook, ManifestHook, ExecHook, ChecksumHook],
}


def register_all(registry: object, *, origin: str = "builtin") -> None:
    """Register every built-in plugin into ``registry``."""
    for category, classes in BUILTIN_PLUGINS.items():
        for cls in classes:
            registry.register(category, cls(), origin=f"{origin}:{cls.__name__}")  # type: ignore[attr-defined]


__all__ = ["BUILTIN_PLUGINS", "register_all"]
